"""Task-scoped socket routing for the ``proxy`` tool extension.

The routing core dispatches on a :class:`~contextvars.ContextVar`, so a proxied
call routes only its own connections and never a concurrent unrelated task's.
:func:`install_dispatcher` assigns the :class:`RoutingSocket` subclass to
``socket.socket`` once; every socket the process then creates reads the
``active_route`` contextvar at creation and configures itself for that task's
route, or stays an ordinary socket when no route is active. Because the route
lives in a contextvar, concurrent routed and unrouted calls each carry their own
configuration — there is no process-global switch to serialize on.

An HTTP/HTTPS route tunnels through the proxy with a stdlib-only ``CONNECT``
handshake (:meth:`RoutingSocket.connect`). A SOCKS route needs PySocks'
negotiation, so it dispatches to :class:`RoutingSocksSocket`, a lazily-defined
``socks.socksocket``/:class:`RoutingSocket` subclass built only when PySocks is
importable — keeping the HTTP/HTTPS path stdlib-only, per the ``proxy`` extra.

Propagation follows the asyncio task tree: child tasks and ``asyncio.to_thread``
inherit the active route; ``loop.run_in_executor`` and a raw ``threading.Thread``
do NOT, so a tool that offloads network work that way escapes routing. The
dispatcher is Python-level and cannot see sockets opened by a C-level network
stack (a libcurl-class client). The route is captured at socket CREATION, so a
tool reusing a keep-alive connection from a pool built outside the routed window
is not re-routed. A tool that needs routing to bind reliably must open its
connections inside the routed window.
"""

from __future__ import annotations

import base64
import socket
import ssl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

# CONNECT response headers are small; a proxy that streams past this without ever
# terminating the header block is refused rather than buffered without bound.
_MAX_PROXY_HEADER_BYTES = 64 * 1024


@dataclass(frozen=True)
class RouteConfig:
    """One parsed proxy route: where to connect and how to negotiate.

    ``connect_address`` is what the socket actually connects to — an SSRF-validated
    IP for a caller-supplied proxy, or the proxy hostname for a trusted operator-pool
    entry. ``proxy_host`` keeps the original hostname for an HTTPS proxy's TLS SNI
    and certificate verification, which must use the hostname even when the TCP
    connection targets the validated IP.
    """

    is_socks: bool
    is_https: bool
    proxy_host: str
    proxy_port: int
    connect_address: str
    username: str | None
    password: str | None
    rdns: bool
    connect_timeout: int
    # The PySocks proxy-type constant (``socks.SOCKS5``/``socks.SOCKS4``) for a
    # SOCKS route; ``None`` for the HTTP/HTTPS path.
    socks_type: int | None


active_route: ContextVar[RouteConfig | None] = ContextVar("active_route", default=None)


def load_socks():
    """Import PySocks, surfacing the ``proxy`` extra hint when it is absent."""
    try:
        import socks
    except ImportError as exc:
        raise ImportError(
            "SOCKS proxy support needs the PySocks library. Install it with: pip install 'tai42-toolbox[proxy]'"
        ) from exc
    return socks


# Built on first SOCKS route so the HTTP/HTTPS path never imports PySocks.
_routing_socks_socket_cls: type[RoutingSocket] | None = None


class RoutingSocket(socket.socket):
    """The socket class installed on ``socket.socket``.

    At creation each instance reads ``active_route``: an HTTP/HTTPS route tunnels
    through the proxy in :meth:`connect`; a SOCKS route is dispatched by
    :meth:`__new__` to :class:`RoutingSocksSocket`; no route leaves an ordinary
    socket. ``isinstance(sock, socket.socket)`` holds either way.
    """

    def __new__(cls, family: int = -1, type: int = -1, proto: int = -1, fileno: int | None = None):
        route = active_route.get()
        if cls is RoutingSocket and route is not None and route.is_socks:
            cls = _routing_socks_socket()
        # __new__ only allocates; __init__ opens the descriptor from the arguments.
        return super().__new__(cls)  # type: ignore[arg-type]

    def __init__(self, family: int = -1, type: int = -1, proto: int = -1, fileno: int | None = None):
        super().__init__(family, type, proto, fileno)
        self._route = active_route.get()

    def connect(self, address):
        route = self._route
        if route is None:
            super().connect(address)
            return
        self._connect_via_http_proxy(route, address)

    def _connect_via_http_proxy(self, route: RouteConfig, address) -> None:
        """Tunnel to ``address`` through an HTTP/HTTPS proxy with a ``CONNECT``
        handshake, then hand back a socket the caller uses as the tunnel.

        The TCP connection targets ``route.connect_address`` (the validated IP for a
        caller-supplied proxy, else the proxy hostname). For an HTTPS proxy the
        connection is TLS-wrapped using ``route.proxy_host`` for SNI and certificate
        verification — never the IP, which would fail verification. The whole
        negotiation is bounded by a single ``route.connect_timeout`` deadline: the
        TCP connect, the TLS handshake, and each header ``recv`` each run under the
        time remaining until it, so a slow proxy — one that dribbles the header a
        byte at a time, or stalls the TLS handshake — cannot wedge the connect past
        that one deadline; the buffered header is also capped at
        ``_MAX_PROXY_HEADER_BYTES``. On any
        failure the descriptor-owning socket is closed before the error propagates;
        the caller's original timeout is restored on an established socket.
        """
        dest_host, dest_port = address
        if not route.rdns:
            dest_host = socket.gethostbyname(dest_host)

        original_timeout = self.gettimeout()
        deadline = time.monotonic() + route.connect_timeout
        sock: socket.socket = self
        try:
            # Every blocking phase of the negotiation — the TCP connect, the HTTPS
            # TLS handshake, and each header recv below — runs under the time left
            # until this one deadline, so the whole negotiation is bounded by
            # ``connect_timeout`` total rather than each phase getting its own.
            self.settimeout(max(0.0, deadline - time.monotonic()))
            super().connect((route.connect_address, route.proxy_port))
            if route.is_https:
                # Verify the proxy's own certificate against the system trust store,
                # using the original hostname (SNI) even though the TCP connection
                # went to the validated IP.
                self.settimeout(max(0.0, deadline - time.monotonic()))
                context = ssl.create_default_context()
                sock = context.wrap_socket(self, server_hostname=route.proxy_host)
            connect_str = f"CONNECT {dest_host}:{dest_port} HTTP/1.1\r\nHost: {dest_host}:{dest_port}\r\n"
            if route.username and route.password:
                auth = f"{route.username}:{route.password}"
                auth_b64 = base64.b64encode(auth.encode()).decode()
                connect_str += f"Proxy-Authorization: Basic {auth_b64}\r\n"
            connect_str += "\r\n"
            sock.sendall(connect_str.encode())
            response = b""
            while True:
                # Bound each recv by the time left until the negotiation deadline; a
                # non-positive remaining makes recv raise promptly rather than block.
                sock.settimeout(max(0.0, deadline - time.monotonic()))
                data = sock.recv(4096)
                if not data:
                    raise OSError("Connection closed by proxy")
                response += data
                if b"\r\n\r\n" in response:
                    break
                if len(response) > _MAX_PROXY_HEADER_BYTES:
                    raise OSError(f"Proxy response headers exceeded {_MAX_PROXY_HEADER_BYTES} bytes")
            header = response.split(b"\r\n\r\n")[0]
            status = header.split(b"\r\n")[0]
            parts = status.split()
            if len(parts) < 2:
                raise OSError(f"Malformed proxy CONNECT response: {status!r}")
            try:
                code = int(parts[1])
            except ValueError as exc:
                raise OSError(f"Malformed proxy CONNECT response: {status!r}") from exc
            if code != 200:
                raise OSError(f"Proxy rejected connection: {status!r}")
            if sock is not self:
                # A TLS wrap replaced ``sock`` (the HTTPS-proxy path) and detached this
                # instance's file descriptor onto it. Forward the wrapped socket's I/O
                # onto this instance so callers keep using the object they were handed.
                self.send = sock.send
                self.sendto = sock.sendto
                self.sendall = sock.sendall
                self.recv = sock.recv
                self.recvfrom = sock.recvfrom
                self.recv_into = sock.recv_into if hasattr(sock, "recv_into") else self.recv_into
                self.close = sock.close
                self.makefile = sock.makefile

                def dummy(*args, **kwargs):
                    raise NotImplementedError("Method not supported in proxied socket")

                self.connect = dummy
        except BaseException:
            # Close the descriptor-owning socket on any negotiation failure so a
            # rejected, malformed, timed-out, or half-open tunnel never leaks its
            # file descriptor to the caller. ``close`` is idempotent, so a caller
            # that also closes is safe. A failed TLS wrap detaches this instance's
            # descriptor (``fileno() == -1``); guard against closing a socket that
            # no longer owns one, mirroring the timeout-restore below.
            if sock.fileno() != -1:
                sock.close()
            raise
        finally:
            # Restore the caller's timeout on the socket that owns the file
            # descriptor. A failed TLS wrap leaves this instance detached (no live
            # descriptor); the socket is being discarded with the raised error, so
            # there is no timeout to restore.
            if sock.fileno() != -1:
                sock.settimeout(original_timeout)


def _routing_socks_socket() -> type[RoutingSocket]:
    """Return the lazily-defined SOCKS routing socket class, building it on first use
    (only reachable once a SOCKS route has selected this path, so PySocks is present).
    """
    global _routing_socks_socket_cls
    if _routing_socks_socket_cls is None:
        socks = load_socks()

        class RoutingSocksSocket(socks.socksocket, RoutingSocket):
            """A ``socks.socksocket`` configured per instance from ``active_route``.

            Per-instance ``set_proxy`` leaves PySocks' class-global ``default_proxy``
            untouched, so concurrent SOCKS routes never collide. The negotiation runs
            under ``route.connect_timeout`` when the caller left the socket's timeout
            unset, restoring it afterward so the timeout is never left as a permanent
            read timeout on a pooled socket.
            """

            def __init__(self, family: int = -1, type: int = -1, proto: int = -1, fileno: int | None = None):
                # socksocket validates ``type`` and hardcodes stream/AF_INET defaults,
                # so the ``-1`` sentinels socket.socket accepts are normalized first.
                if family == -1:
                    family = socket.AF_INET
                if type == -1:
                    type = socket.SOCK_STREAM
                if proto == -1:
                    proto = 0
                super().__init__(family, type, proto, fileno)  # type: ignore[arg-type]
                route = active_route.get()
                self._route = route
                if route is not None and route.is_socks:
                    self.set_proxy(
                        route.socks_type,
                        route.connect_address,
                        route.proxy_port,
                        rdns=route.rdns,
                        username=route.username,
                        password=route.password,
                    )

            def connect(self, dest_pair, *args, **kwargs):
                route = self._route
                original_timeout = self.gettimeout()
                # PySocks forces a non-blocking socket to blocking for the whole
                # negotiation and, with no timeout, hangs unbounded; inject the
                # negotiation timeout only when the caller left one unset.
                inject = route is not None and original_timeout in (None, 0.0)
                if inject and route is not None:
                    self.settimeout(route.connect_timeout)
                try:
                    return super().connect(dest_pair, *args, **kwargs)
                finally:
                    if inject:
                        self.settimeout(original_timeout)

        _routing_socks_socket_cls = RoutingSocksSocket
    return _routing_socks_socket_cls


def install_dispatcher() -> None:
    """Install the routing dispatcher on ``socket.socket`` (idempotent).

    One atomic assignment swaps in :class:`RoutingSocket`. With no active route it
    produces ordinary sockets, so installing it is harmless when nothing is routed;
    the proxy extension factory calls this at bind time so a process that builds a
    proxied tool self-installs once at boot.
    """
    if socket.socket is not RoutingSocket:
        socket.socket = RoutingSocket  # type: ignore[misc]


@contextmanager
def route(cfg: RouteConfig) -> Iterator[RouteConfig]:
    """Make ``cfg`` the active route for the duration of the block.

    Every socket created inside the block (in this task and tasks that inherit its
    context) routes through ``cfg``; the route is reset on exit, on success and on
    exception alike.
    """
    token = active_route.set(cfg)
    try:
        yield cfg
    finally:
        active_route.reset(token)
