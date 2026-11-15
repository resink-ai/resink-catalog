"""The DuckDB tests skip where the extensions are optional and fail where the release CI requires them."""

import pytest
from _pytest.outcomes import Failed, Skipped
from extension_policy import REQUIRE_EXTENSIONS_ENV, skip_or_fail


def test_a_missing_extension_skips_by_default(monkeypatch):
    monkeypatch.delenv(REQUIRE_EXTENSIONS_ENV, raising=False)
    with pytest.raises(Skipped):
        skip_or_fail("extension x missing")


def test_a_missing_extension_fails_when_the_ci_requires_them(monkeypatch):
    monkeypatch.setenv(REQUIRE_EXTENSIONS_ENV, "1")
    with pytest.raises(Failed, match="pre-installed"):
        skip_or_fail("extension x missing")


def test_the_duckdb_tests_use_it_and_never_skip_directly():
    import inspect

    import test_duckdb_rest

    src = inspect.getsource(test_duckdb_rest)
    assert "skip_or_fail(" in src and "pytest.skip(" not in src
