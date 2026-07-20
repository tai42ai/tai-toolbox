"""Registration-capture fixture for the tool entrypoint tests.

Toolbox is contract-facing and cannot import the skeleton app, so a capturing
``tai_app`` records what each module registers when it is (re)imported, letting a
test assert the tool name. The server, curl, guard, and missing-import fixtures
these tests also use live in the root ``conftest`` because the ``_internal``
helper tests share them.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from tai_contract.app import tai_app


class _CaptureTools:
    """Records every tool registered through ``tai_app.tools.tool``."""

    def __init__(self) -> None:
        self.registered: dict[str, Callable[..., Any]] = {}

    def tool(self, func: Callable[..., Any] | None = None, /, *args: Any, **kwargs: Any) -> Any:
        def register(f: Callable[..., Any]) -> Callable[..., Any]:
            self.registered[f.__name__] = f
            return f

        return register(func) if callable(func) else register


class _CaptureApp:
    def __init__(self) -> None:
        self.tools = _CaptureTools()


@pytest.fixture
def load_registrations() -> Iterator[Callable[[str], _CaptureApp]]:
    """Return a loader that (re)imports a module under a capturing app and hands
    back the app so the test can read ``app.tools.registered``.

    The previously bound app is restored afterwards so other tests are
    unaffected.
    """
    previous = cast("Any", tai_app)._impl

    def load(module_name: str) -> _CaptureApp:
        app = _CaptureApp()
        tai_app.bind(app)
        if module_name in sys.modules:
            importlib.reload(sys.modules[module_name])
        else:
            importlib.import_module(module_name)
        return app

    yield load
    tai_app.bind(previous)
