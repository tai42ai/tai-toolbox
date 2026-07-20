"""The ``chain`` tool extension (TRANSFORMER kind).

Branches a tool into a ``<tool>_chain`` variant that calls the wrapped tool,
transforms its output with a jq expression, then calls a second named tool with
the transformed result. The variant presents its OWN composed signature (built
with :func:`makefun.create_function`): the wrapped tool's parameters plus
``jq_expression`` and ``next_tool_name``. The chain runner lives in
:mod:`tai_toolbox._internal.extensions.chain_executor`, which needs the ``chain`` extra
(jq, via ``tai-kit[jq]``) and fails loudly at import with an install hint when jq
is absent, never a silent skip.
"""

import inspect
from typing import Any

from makefun import create_function
from tai_contract.app import tai_app
from tai_contract.extensions import ExtensionKind

from tai_toolbox._internal.extensions.chain_executor import execute_chain
from tai_toolbox._internal.extensions.signature import with_added_params


@tai_app.extensions.extension(kind=ExtensionKind.TRANSFORMER, name="chain")
def chain(func, orig_name, orig_desc):
    """Branch ``func`` into a chained ``<orig_name>_chain`` variant."""
    sig = inspect.signature(func)

    new_name = f"{orig_name}_{chain.__name__}"

    # Keyword-only so the two required controls can legally follow any defaulted
    # parameter the wrapped tool declares, and always arrive in ``kwargs``.
    composed_sig = with_added_params(
        sig.replace(return_annotation=Any),
        inspect.Parameter("jq_expression", inspect.Parameter.KEYWORD_ONLY, annotation=str),
        inspect.Parameter("next_tool_name", inspect.Parameter.KEYWORD_ONLY, annotation=str),
    )

    async def func_impl(*args: Any, **kwargs: Any):
        jq_expression = kwargs.pop("jq_expression")
        next_tool_name = kwargs.pop("next_tool_name")
        return await execute_chain(
            orig_name,
            kwargs,
            jq_expression=jq_expression,
            next_tool_name=next_tool_name,
        )

    description = f"""Chain extension for '{orig_name}'.
Calls the original tool, applies a jq expression to its output, then calls a second tool with the result.

Args:
    jq_expression: jq expression to transform the first tool's output into arguments for the next tool.
    next_tool_name: Name of the tool to call with the transformed output.

Original doc:
{orig_desc}
"""

    return create_function(
        func_signature=composed_sig,
        func_impl=func_impl,
        func_name=new_name,
        qualname=new_name,
        module_name=func.__module__,
        doc=description,
    )
