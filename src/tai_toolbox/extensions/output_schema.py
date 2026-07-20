"""The ``output_schema`` tool extension (TRANSFORMER kind).

Branches a tool into an ``<tool>_output_schema`` variant that forces the tool's
advertised OUTPUT schema to a caller-supplied JSON Schema and validates every
result against it. This is the first config-accepting toolbox extension: the
schema is supplied through the extension config under a ``"schema"`` key (a JSON
Schema dict), so the factory takes the four-argument ``(func, name, description,
config)`` form the platform introspects for and passes ``config`` to by keyword.

The variant presents the wrapped tool's own input signature with its
``return_annotation`` replaced by a pydantic model synthesized from the configured
schema, so the branch ADVERTISES that schema through the platform's
return-annotation output-schema channel. At runtime it calls the wrapped tool and
validates the result against the configured schema with tai-kit's faithful
draft-2020-12 validator, returning the result unchanged on a match and raising
loudly (with a JSON-path) on any mismatch — never a silent pass or degrade. The
runtime runner lives in
:mod:`tai_toolbox._internal.extensions.output_schema_validator`.

The configured schema is meta-schema-checked at bind time: a config missing the
``"schema"`` key, or a ``"schema"`` that fails the draft-2020-12 meta-schema,
fails the bind loudly and distinctly.
"""

import inspect
from typing import Any

from makefun import create_function
from tai_contract.app import tai_app
from tai_contract.extensions import ExtensionKind
from tai_kit.utils.data import snake_to_pascal
from tai_kit.utils.data.json_schema_util import (
    check_json_schema,
    json_schema_to_pydantic_model,
)

from tai_toolbox._internal.extensions.output_schema_validator import enforce_output_schema


def _configured_schema(config: dict[str, Any]) -> dict[str, Any]:
    """The JSON Schema dict from an ``output_schema`` combo's config, validated at
    bind time.

    Raises loudly and distinctly on the two authoring mistakes: a config without a
    ``"schema"`` key (``ValueError``), and a ``"schema"`` that is not a valid
    draft-2020-12 JSON Schema (``InvalidJsonSchemaError`` from the kit validator).
    """
    try:
        schema = config["schema"]
    except (KeyError, TypeError):
        raise ValueError(
            "the 'output_schema' tool extension requires a 'schema' key in its config carrying a JSON Schema dict"
        ) from None

    check_json_schema(schema)
    return schema


@tai_app.extensions.extension(kind=ExtensionKind.TRANSFORMER, name="output_schema")
def output_schema(func, orig_name, orig_desc, config):
    """Branch ``func`` into an ``<orig_name>_output_schema`` variant that advertises
    and validates its output against the configured JSON Schema."""
    schema = _configured_schema(config)

    new_name = f"{orig_name}_{output_schema.__name__}"
    output_model = json_schema_to_pydantic_model(schema, f"{snake_to_pascal(orig_name)}Output")

    # Present the wrapped tool's signature with its return annotation forced to the
    # configured schema, so the branch advertises that schema through the existing
    # return-annotation derive path.
    composed_sig = inspect.signature(func).replace(return_annotation=output_model)

    async def func_impl(*args: Any, **kwargs: Any) -> Any:
        return await enforce_output_schema(func, args, kwargs, schema)

    description = f"""Output-schema extension for '{orig_name}'.
Calls the original tool and validates its result against a fixed JSON Schema, raising on any mismatch.

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
