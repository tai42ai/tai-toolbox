"""Private helper modules backing the toolbox tools.

Not part of the public API: the module here holds the ``request`` tool's concern
— the pinning HTTP client (curl session execution, response serialization, and
SSRF pinning against tai-kit's guard) — that the thin registered ``request``
entrypoint in ``tai_toolbox.tools`` delegates to. Nothing here registers through
``tai_app``.
"""
