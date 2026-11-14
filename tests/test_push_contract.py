"""The push protocol contract is valid OpenAPI 3.1 and carries every documented error code."""

from pathlib import Path

import pytest
import yaml
from openapi_spec_validator import validate

CONTRACT = Path(__file__).parent.parent / "contracts" / "push-protocol-v0.openapi.yaml"

# Error codes of design section 8 (newbase repo, catalog specs, 2026-11-13-push-protocol.md).
DOC_ERROR_CODES = {
    "invalid_request",
    "unauthenticated",
    "forbidden",
    "quota_exceeded",
    "not_found",
    "push_in_progress",
    "offset_mismatch",
    "idempotency_conflict",
    "history_diverged",
    "schema_conflict",
    "table_exists",
    "session_expired",
    "file_too_large",
    "checksum_mismatch",
    "unsupported_table_feature",
    "invalid_parquet",
    "workspace_paused",
    "rate_limited",
    "unavailable",
}


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(CONTRACT.read_text())


def test_is_valid_openapi_31(spec):
    assert spec["openapi"].startswith("3.1")
    validate(spec)


def test_every_documented_error_code_is_in_the_yaml(spec):
    yaml_codes = set(spec["components"]["schemas"]["ErrorCode"]["enum"])
    assert DOC_ERROR_CODES <= yaml_codes
    assert yaml_codes <= DOC_ERROR_CODES, "YAML has codes the design does not document"


def test_push_permission_is_named_push_not_scope(spec):
    text = CONTRACT.read_text()
    assert "push:write" not in text
