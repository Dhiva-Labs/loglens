"""Smoke test for the parsers package."""

import loglens.parsers


def test_parsers_module_imports() -> None:
    assert loglens.parsers is not None
