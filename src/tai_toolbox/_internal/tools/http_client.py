"""Request execution for the ``request`` tool, backed by tai-kit's pooled curl
client.

Serializes a curl response to a plain dict, decodes bodies against the declared
charset, and — when the SSRF guard is on — pins curl to the validated address and
streams the body under the size cap. The curl client lives in tai-kit behind its
``curl`` extra; this module's backing dependency is opt-in as
``pip install 'tai-toolbox[http]'``. Missing it raises a loud install hint at
import time rather than a silent skip.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from urllib.parse import urlparse

try:
    from curl_cffi import CurlOpt
    from tai_kit.clients.impl.curl import CurlClient
except ImportError as exc:
    raise ImportError(
        "tai-toolbox 'http' tool requires the 'http' optional dependency "
        "(tai-kit[curl]). "
        "Install it with: pip install 'tai-toolbox[http]'"
    ) from exc

from tai_contract.app import tai_app
from tai_kit.net import url_guard

# Body fields (``content``/``text``) are set explicitly, not by this loop, so the
# streaming path can supply an already-read body that the response no longer
# exposes.
_RESPONSE_FIELDS = (
    "url",
    "status_code",
    "reason",
    "ok",
    "encoding",
    "charset",
    "charset_encoding",
    "primary_ip",
    "primary_port",
    "local_ip",
    "local_port",
    "redirect_count",
    "redirect_url",
    "http_version",
    "download_size",
    "upload_size",
    "header_size",
    "request_size",
    "response_size",
)


def _decode_body(body: bytes, resp: Any) -> str:
    """Decode ``body`` to text using the response's declared charset.

    Falls back to the response's default encoding, then UTF-8, and always
    replaces undecodable bytes rather than raising. A malformed charset label
    (an unknown codec name) is caught so a bad ``Content-Type`` never crashes the
    tool — it degrades to a safe decode, not a raised ``LookupError``.
    """
    for label in (resp.charset, resp.encoding, getattr(resp, "default_encoding", None), "utf-8"):
        if not label:
            continue
        try:
            return body.decode(label, errors="replace")
        except LookupError:
            continue
    return body.decode("utf-8", errors="replace")


def _serialize_response(
    resp: Any,
    body: bytes | None = None,
    elapsed: float | None = None,
    download_size: int | None = None,
    response_size: int | None = None,
) -> dict[str, Any]:
    """Serialize a curl response to a plain dict.

    When ``body`` is given (the streaming path), the response body has already
    been read off the wire and is no longer re-readable, so ``content``/``text``
    are taken from ``body``. On that path curl's own transfer metrics reflect
    only the header-received moment (time-to-first-byte), so ``elapsed``,
    ``download_size``, and ``response_size`` measured across the full body read
    are passed in and override the stale curl values. Otherwise the body and
    metrics are read from the response directly.
    """
    data = {field: value for field in _RESPONSE_FIELDS if (value := getattr(resp, field, None)) is not None}
    if body is None:
        data["content"] = resp.content
        data["text"] = resp.text
    else:
        data["content"] = body
        data["text"] = _decode_body(body, resp)
    if download_size is not None:
        data["download_size"] = download_size
    if response_size is not None:
        data["response_size"] = response_size
    data.update(
        {
            "headers": dict(resp.headers),
            "cookies": dict(resp.cookies),
            "history": [r.url for r in resp.history],
            "elapsed": elapsed if elapsed is not None else resp.elapsed.total_seconds(),
        }
    )
    return data


async def _validate_proxies(session_params: dict[str, Any]) -> list[str]:
    """Resolve and validate every proxy host in ``session_params`` and return the
    curl ``--resolve`` pins that hold each proxy to its validated address.

    curl carries a caller-supplied proxy as ``proxy`` (a URL string) or ``proxies``
    (a scheme-to-URL mapping). A proxy host is caller-supplied by definition, so
    each is resolved and validated: the request cannot be steered through a proxy at
    an internal address while its destination is pinned. A rejected host raises
    ``UrlGuardError``; the path is validated, never denied outright.

    Validation alone leaves curl free to re-resolve the proxy hostname at connect,
    so a host that answered a public address during the check could answer an
    internal one at connect (DNS-rebinding on the proxy hop). Each proxy is
    therefore pinned to the exact validated address with a ``host:port:address``
    entry — curl's ``--resolve`` applies to proxy hostnames as well as the
    destination — mirroring the destination pin. The proxy port comes from the URL
    (or the scheme default: https 443, socks 1080, else 80).
    """
    proxy_urls: list[str] = []
    proxies = session_params.get("proxies")
    if isinstance(proxies, dict):
        proxy_urls.extend(value for value in proxies.values() if value)
    proxy = session_params.get("proxy")
    if proxy:
        proxy_urls.append(proxy)
    pins: list[str] = []
    for proxy_url in proxy_urls:
        parsed = urlparse(proxy_url)
        proxy_host = parsed.hostname
        if not proxy_host:
            raise url_guard.UrlGuardError(
                f"SSRF guard: proxy URL has no host to check: {_redact_userinfo(proxy_url)!r}"
            )
        scheme = parsed.scheme.lower()
        if scheme.startswith("socks"):
            default_port = 1080
        elif scheme == "https":
            default_port = 443
        else:
            default_port = 80
        # Read the port before resolving so a malformed one fails fast with a
        # domain-specific error rather than a bare "Port out of range" after a DNS
        # lookup has already run.
        try:
            explicit_port = parsed.port
        except ValueError as exc:
            raise url_guard.UrlGuardError(
                f"SSRF guard: proxy URL has an out-of-range port: {_redact_userinfo(proxy_url)!r}"
            ) from exc
        proxy_port = explicit_port or default_port
        validated_ip = await url_guard.resolve_and_validate(proxy_host)
        pins.append(_resolve_pin_entry(proxy_host, proxy_port, validated_ip))
    return pins


def _redact_userinfo(url: str) -> str:
    """Return ``url`` with any ``user:pass@`` credentials replaced by ``***@`` so a
    proxy URL echoed in an error message never leaks the caller's credentials. The
    host and everything after it are preserved."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    netloc, slash, tail = rest.partition("/")
    _creds, at, hostport = netloc.rpartition("@")
    if not at:
        return url
    return f"{scheme}://***@{hostport}{slash}{tail}"


def _resolve_pin_entry(host: str, port: int, validated_ip: str) -> str:
    """Build curl's ``--resolve`` mapping entry pinning ``host:port`` to the
    validated address. An IPv6 literal — on either side — is wrapped in brackets so
    curl parses the pin unambiguously from the ``host:port:address`` triple (an
    unbracketed IPv6 literal's own colons would collide with the field
    separators)."""
    pinned_host = f"[{host}]" if ":" in host else host
    pinned_ip = f"[{validated_ip}]" if ":" in validated_ip else validated_ip
    return f"{pinned_host}:{port}:{pinned_ip}"


# curl reads ``session.curl_options`` only after an internal await inside
# ``session.request``, and the pooled ``CurlClient`` hands the same
# ``AsyncSession`` to every caller sharing a ``session_key``. Two concurrent
# same-key requests would therefore race on the shared RESOLVE pin: the second
# overwrites the first's mapping before the first's curl reads it, unpinning the
# first host so curl re-resolves it via DNS at connect — reopening the
# DNS-rebinding hole the pin exists to close. An exclusive per-``session_key``
# lock, held across [set RESOLVE + request + read the streamed body], serializes
# same-key requests so each keeps its own pin for the whole exchange.
#
# ``asyncio.Lock`` binds to the loop that first uses it, so the registry is keyed
# by the running loop (each loop gets its own ``session_key`` -> entry map);
# reusing one module-global lock across loops would raise "attached to a
# different loop".
#
# Each entry is a mutable ``[lock, refcount]``: the refcount counts live
# holders and waiters of that ``session_key`` and the entry is evicted the moment
# it falls to zero, so the map only ever holds keys with an in-flight guard and
# shrinks to empty when idle — the same lock-lifetime discipline as
# ``CacheStore.drop_lock``.
_session_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, list[Any]]] = weakref.WeakKeyDictionary()


@asynccontextmanager
async def _session_guard(session_key: str) -> AsyncIterator[None]:
    """Serialize same-``session_key`` guarded requests on the current loop under a
    refcounted lock that is evicted once no holder or waiter references it.

    Refcount mutations are synchronous on the loop (no await between the check and
    the mutate), so concurrent guards for the same key cannot interleave their
    bookkeeping: the entry lives exactly while at least one holder/waiter
    references it, preserving the serialization guarantee while the map shrinks to
    zero when idle.
    """
    loop = asyncio.get_running_loop()
    locks = _session_locks.get(loop)
    if locks is None:
        locks = {}
        _session_locks[loop] = locks
    entry = locks.get(session_key)
    if entry is None:
        entry = [asyncio.Lock(), 0]
        locks[session_key] = entry
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            locks.pop(session_key, None)


async def _perform_pinned(
    session: Any, url: str, method: str, pin_entries: list[str], request_params: dict[str, Any]
) -> dict[str, Any]:
    """Pin curl to the validated addresses, then stream the size-capped body.

    Setting curl's ``--resolve`` mapping makes curl connect to the validated
    address instead of re-resolving the hostname (which an attacker could answer
    differently at connect time), closing DNS-rebinding. ``pin_entries`` holds the
    destination pin and one pin per caller-supplied proxy, so both the destination
    and every proxy hop connect to the exact address that was validated. The body
    is streamed and size-capped chunk by chunk so an over-cap response is refused
    the moment it crosses the limit, never after buffering the whole body. curl
    reports its transfer metrics at the header-received moment, so full-body
    ``elapsed``/``download_size``/``response_size`` are measured here and override
    the stale curl values.
    """
    session.curl_options = {**session.curl_options, CurlOpt.RESOLVE: pin_entries}
    start = time.perf_counter()
    resp = await session.request(url=url, method=cast("Any", method.upper()), stream=True, **request_params)
    try:
        buffer = bytearray()
        async for chunk in resp.aiter_content():
            buffer.extend(chunk)
            url_guard.enforce_size(len(buffer))
    finally:
        await resp.aclose()
    elapsed = time.perf_counter() - start
    download_size = len(buffer)
    response_size = (getattr(resp, "header_size", None) or 0) + download_size
    return _serialize_response(
        resp,
        body=bytes(buffer),
        elapsed=elapsed,
        download_size=download_size,
        response_size=response_size,
    )


async def perform_request(
    url: str,
    method: str = "GET",
    session_key: str | None = None,
    clear_session_cookies: bool = True,
    session_params: dict[str, Any] | None = None,
    request_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute an HTTP request through a curl-backed session and return the
    response data (status, body, headers, cookies, timing, and transfer metrics)."""
    session_params = dict(session_params or {})
    request_params = dict(request_params or {})

    guard_on = url_guard.guard_enabled()
    if guard_on:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            raise url_guard.UrlGuardError(f"SSRF guard: URL has no host to check: {url!r}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        # Resolve and validate once, then pin curl to this exact address below.
        validated_ip = await url_guard.resolve_and_validate(host)
        request_params["allow_redirects"] = False
        # A caller-supplied proxy would otherwise reach the (pinned) destination
        # through an unvalidated, re-resolvable hop; validate every proxy host and
        # capture its pin so the proxy hop connects to its validated address too.
        proxy_pins = await _validate_proxies(session_params)

    if session_key is not None:
        session_params["session_key"] = session_key
        session_ctx = tai_app.clients.client_ctx(CurlClient, session_params=session_params)
    else:
        session_ctx = tai_app.clients.client_ctx(CurlClient, session_params=session_params, fresh=True)

    async with session_ctx as session:
        if clear_session_cookies:
            session.cookies.clear()
        if not guard_on:
            resp = await session.request(url=url, method=cast("Any", method.upper()), **request_params)
            return _serialize_response(resp)

        # curl's ``--resolve`` pin is keyed by host:port and read per request from
        # ``session.curl_options``, so it is set right before the request (mirroring
        # the per-request ``cookies.clear`` above), letting a pooled session reused
        # across hosts pin each request to its own validated address rather than
        # only the first host it saw.
        # The destination pin plus one pin per caller-supplied proxy: curl's
        # ``--resolve`` applies to proxy hostnames too, so every hop connects to
        # the address that was validated rather than a re-resolved one.
        pin_entries = [_resolve_pin_entry(host, port, validated_ip), *proxy_pins]

        # A pooled (keyed) session is shared by every same-key caller, so the pins
        # and the request-plus-body-read that consumes them run under the per-key
        # lock: this serializes concurrent same-key requests so a second one
        # cannot clobber this request's RESOLVE mapping mid-flight and unpin its
        # host (see ``_session_guard``). A fresh (keyless) session is never shared,
        # so it needs no lock.
        if session_key is not None:
            async with _session_guard(session_key):
                return await _perform_pinned(session, url, method, pin_entries, request_params)
        return await _perform_pinned(session, url, method, pin_entries, request_params)
