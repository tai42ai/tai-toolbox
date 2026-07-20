"""Test that the prometheus counters module raises the ``prometheus`` extra
install hint when prometheus-client is absent."""

import importlib
import sys

import pytest

import tai_toolbox._internal.extensions.prometheus_counters as counters_module


def test_missing_extra_raises_install_hint(monkeypatch):
    # Simulate the prometheus extra being absent: the module must fail loudly at
    # import with a copy-pasteable install hint, never a silent skip.
    monkeypatch.setitem(sys.modules, "prometheus_client", None)
    with pytest.raises(ImportError, match=r"tai-toolbox\[prometheus\]"):
        importlib.reload(counters_module)

    # Restore a clean module for the rest of the suite.
    monkeypatch.undo()
    importlib.reload(counters_module)
