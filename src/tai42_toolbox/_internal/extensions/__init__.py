"""Private helper modules backing the toolbox tool extensions.

Not part of the public API: each module holds one concern (signature composition,
socket routing, the proxy configuration parse, the cache store, the Prometheus
counters, and the batch/chain executors) that a thin registered entrypoint in
``tai42_toolbox.extensions`` delegates to. Nothing here registers through ``tai42_app``.
"""
