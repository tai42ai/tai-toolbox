"""The ``cache`` tool extension (WRAPPER kind).

Branches a tool into a ``<tool>_cache`` variant that memoizes results by call
arguments. The variant adds one control kwarg, ``exp`` (seconds-to-live), which
is declared as a reserved param so the apply site treats the branch as
schema-preserving: the wrapper presents the wrapped tool's input schema unchanged
apart from ``exp``.

Concurrent identical calls single-flight on a per-key lock, so an overlapping
burst computes the wrapped tool once and the late callers read the freshly
written value instead of stampeding the underlying tool. The per-branch value
store and key serialization live in :mod:`tai_toolbox._internal.extensions.cache_store`.

The store is process-local, but the extension does NOT register
``requires_body_locality``: results stay correct wherever the wrapper executes.
Bound inside an execution-relocating (BACKEND-kind) extension, each worker
process holds its own store, so a value cached in one worker is a miss in
another and single-flight only collapses callers within one process — a
hit-rate cost, never a wrong result.
"""

import inspect

from makefun import create_function
from tai_contract.app import tai_app
from tai_contract.extensions import ExtensionKind

from tai_toolbox._internal.extensions.cache_store import MISS, CacheStore, compute_key
from tai_toolbox._internal.extensions.signature import with_added_params


@tai_app.extensions.extension(kind=ExtensionKind.WRAPPER, name="cache")
def cache(func, name, description):
    """Branch ``func`` into a memoizing ``<name>_cache`` variant."""
    store = CacheStore()

    async def wrapper(*args, **kwargs):
        exp = kwargs.pop("exp", None)
        key = compute_key(*args, **kwargs)

        # 1. Fast-path read (no lock).
        value = store.read(key)
        if value is not MISS:
            return value

        # 2. Single-flight: the first caller for a key computes and writes; any
        # concurrent callers for the same key wait on the lock, then read the
        # freshly written value instead of re-executing the wrapped tool.
        lock = store.key_lock(key)
        try:
            async with lock:
                value = store.read(key)
                if value is not MISS:
                    return value

                if inspect.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)

                store.write(key, result, exp)
                return result
        finally:
            store.drop_lock(key)

    # Present the wrapped tool's signature plus the reserved ``exp`` control kwarg.
    sig = inspect.signature(func)
    exp_param = inspect.Parameter("exp", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=float | None)
    new_sig = with_added_params(sig, exp_param)

    new_name = f"{name}_{cache.__name__}"

    return create_function(
        func_signature=new_sig,
        func_impl=wrapper,
        func_name=new_name,
        qualname=new_name,
        module_name=func.__module__,
        doc=description,
    )


# The apply site subtracts these reserved control kwargs from the branch's input
# schema before checking that a WRAPPER preserves the wrapped tool's schema. The
# attribute rides on the factory (registration returns it unchanged).
cache.reserved_params = frozenset({"exp"})
