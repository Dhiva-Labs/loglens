"""Smoke test for the cluster module."""

import loglens.cluster
from loglens import __version__


def test_cluster_module_imports() -> None:
    assert loglens.cluster is not None


def test_version_is_a_non_empty_string() -> None:
    assert isinstance(__version__, str)
    assert __version__
