"""Smoke test for the tailer module."""

import loglens.tailer


def test_tailer_module_imports() -> None:
    assert loglens.tailer is not None
