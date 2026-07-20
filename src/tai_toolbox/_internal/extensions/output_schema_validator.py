"""Runtime validation for the ``output_schema`` tool extension.

Calls the wrapped tool, then validates its result against the configured JSON
Schema with tai-kit's faithful draft-2020-12 validator. A conforming result is
returned unchanged; a mismatch raises ``JsonSchemaValidationError`` (carrying the
JSON-path of the first offending node) loudly, never a silent pass or degrade.
The composed signature the extension presents lives with its factory; this module
holds only the runner.
"""

import inspect
from collections.abc import Callable
from typing import Any

from pydantic_core import to_jsonable_python
from tai_kit.utils.data.json_schema_util import validate_against_json_schema


async def enforce_output_schema(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    schema: dict[str, Any],
) -> Any:
    """Run ``func`` and validate its result against ``schema``, returning the
    result unchanged on a match.

    The result is validated in its JSON-able form — the shape the schema describes
    and the platform serializes over the wire — so a return carrying pydantic
    models, datetimes, or other rich values is checked against the schema exactly
    as it is emitted. ``func`` may be sync or async; a mismatch propagates the
    validator's loud typed error."""
    if inspect.iscoroutinefunction(func):
        result = await func(*args, **kwargs)
    else:
        result = func(*args, **kwargs)
    validate_against_json_schema(to_jsonable_python(result), schema)
    return result
