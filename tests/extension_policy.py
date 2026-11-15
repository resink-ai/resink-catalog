"""Skip or fail for a missing DuckDB extension."""

import os

import pytest

REQUIRE_EXTENSIONS_ENV = "RESINK_CATALOG_REQUIRE_DUCKDB_EXTENSIONS"


def skip_or_fail(reason: str) -> None:
    """A missing DuckDB extension skips the test on a developer's machine (no network in tests, D10) but FAILS it
    where the environment promises the extensions: the release CI sets RESINK_CATALOG_REQUIRE_DUCKDB_EXTENSIONS=1
    after pre-installing them, so a silently skipped DuckDB test can never let a release through."""
    if os.environ.get(REQUIRE_EXTENSIONS_ENV) == "1":
        pytest.fail(f"{reason} ({REQUIRE_EXTENSIONS_ENV}=1: the extensions must be pre-installed)", pytrace=False)
    pytest.skip(reason)
