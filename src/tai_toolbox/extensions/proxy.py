"""The ``proxy`` tool extension (WRAPPER kind).

Branches a tool into a ``<tool>_proxy`` variant that runs the wrapped tool with a
task-scoped route active, so any connection the tool opens is routed through a
proxy while unrelated concurrent tasks are untouched. The variant adds one control
kwarg, ``proxies`` (a proxy-URL pool), declared as a reserved param so the branch
stays schema-preserving.

Routing is dispatched on a contextvar (see
:mod:`tai_toolbox._internal.extensions.socket_routing`), so it follows the asyncio
task tree: child tasks and ``asyncio.to_thread`` inherit it, but
``loop.run_in_executor`` and a raw ``threading.Thread`` do not, and a C-level
network stack (a libcurl-class client, such as the ``request`` tool's own curl
transport — use its native ``proxies`` session param there) is not covered.

The extension registers with ``requires_body_locality=True``: the wrapper routes
only when it executes in the process running the tool body it wraps (the route
contextvar and the installed socket dispatcher are process-local). In a stacked
combo it must therefore bind INSIDE any execution-relocating extension
(``ExtensionKind.relocates_execution``), so the wrapper travels with the body to
the worker; the platform's bind engine reads the marker and rejects the wrong
order at bind time.
"""

import inspect

from makefun import create_function
from tai_contract.app import tai_app
from tai_contract.extensions import ExtensionKind

from tai_toolbox._internal.extensions.proxy_context import build_route
from tai_toolbox._internal.extensions.signature import with_added_params
from tai_toolbox._internal.extensions.socket_routing import install_dispatcher, route


@tai_app.extensions.extension(kind=ExtensionKind.WRAPPER, name="proxy", requires_body_locality=True)
def proxy(func, name, description):
    """Branch ``func`` into a proxy-routed ``<name>_proxy`` variant.

    Each call routes independently on a contextvar, so concurrent proxy-routed calls
    run in parallel without colliding. A tool that needs its connections routed must
    open them inside the call (the route follows the task tree, not a shared
    connection pool built outside it).
    """
    install_dispatcher()

    async def wrapper(*args, **kwargs):
        proxies = kwargs.pop("proxies", None)
        cfg = await build_route(proxies)
        with route(cfg):
            if inspect.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            return func(*args, **kwargs)

    sig = inspect.signature(func)
    proxies_param = inspect.Parameter(
        "proxies", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=list[str] | None
    )
    new_sig = with_added_params(sig, proxies_param)

    new_name = f"{name}_{proxy.__name__}"

    routed_doc = (
        f"{description}\n\n"
        "Note: this proxy-routed tool routes the connections it opens through a "
        "proxy; each call routes independently and can run in parallel with others."
    )

    return create_function(
        func_signature=new_sig,
        func_impl=wrapper,
        func_name=new_name,
        qualname=new_name,
        module_name=func.__module__,
        doc=routed_doc,
    )


# Subtracted from the branch's input schema by the apply site so the WRAPPER
# schema-preservation check ignores the injected control kwarg.
proxy.reserved_params = frozenset({"proxies"})
