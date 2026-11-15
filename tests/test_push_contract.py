"""P1: the push protocol contract is valid OpenAPI 3.1, carries every error code of design section 8 (plan v2.3),
closes every request object, and states the ownership 404s, retry headers and revocation wording."""

import copy
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate

CONTRACT = Path(__file__).parent.parent / "contracts" / "push-protocol-v0.openapi.yaml"

# design section 8 (newbase repo, catalog specs, 2026-11-13-push-protocol.md): HTTP status -> codes
DOC_ERRORS = {
    400: {"invalid_request"},
    401: {"unauthenticated"},
    403: {"forbidden"},
    404: {"not_found"},
    409: {
        "push_in_progress",
        "offset_mismatch",
        "idempotency_conflict",
        "history_diverged",
        "schema_conflict",
        "table_exists",
        "confirmation_required",  # added after the design text: commit of an over_included session without confirmation
    },
    410: {"session_expired"},
    413: {"file_too_large", "storage_ceiling"},
    422: {"checksum_mismatch", "unsupported_table_feature", "invalid_parquet"},
    423: {"workspace_paused"},
    429: {"rate_limited", "staging_full"},
    503: {"unavailable"},
}
DOC_ERROR_CODES = set().union(*DOC_ERRORS.values())
OPERATIONS = ("get", "post", "put", "patch", "delete")


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(CONTRACT.read_text())


def operations(spec):
    for path, item in spec["paths"].items():
        for method in OPERATIONS:
            if method in item:
                yield path, method, item[method], item


def resolve(spec, node):
    while isinstance(node, dict) and "$ref" in node:
        target = spec
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = target
    return node


def test_is_valid_openapi_31(spec):
    assert spec["openapi"].startswith("3.1")
    validate(spec)


def test_the_error_codes_are_exactly_those_of_the_design(spec):
    yaml_codes = set(spec["components"]["schemas"]["ErrorCode"]["enum"])
    assert DOC_ERROR_CODES <= yaml_codes, f"missing: {DOC_ERROR_CODES - yaml_codes}"
    assert yaml_codes <= DOC_ERROR_CODES, f"not in the design: {yaml_codes - DOC_ERROR_CODES}"
    assert "quota_exceeded" not in CONTRACT.read_text()  # replaced by storage_ceiling in v2.3


def test_every_documented_status_is_declared_and_its_codes_are_named(spec):
    declared: dict[int, str] = {}
    for _, _, op, _ in operations(spec):
        for status, response in op["responses"].items():
            declared[int(status)] = (
                declared.get(int(status), "") + yaml.dump(response) + yaml.dump(resolve(spec, response))
            )
    for status in DOC_ERRORS:
        assert status in declared, f"no operation declares {status}"
    assert 402 not in declared, "402 is gone: an exhausted hold is 423 workspace_paused (design section 6)"
    text = yaml.dump(spec)
    for code in DOC_ERROR_CODES:
        assert code in text


def test_push_permission_is_named_push_not_scope():
    assert "push:write" not in CONTRACT.read_text()


# ---------------------------------------------------------------- every request object is closed


def request_schemas(spec):
    for path, method, op, _ in operations(spec):
        body = op.get("requestBody")
        if body:
            for media in body["content"].values():
                yield f"{method.upper()} {path}", media["schema"]


def closed_problems(spec, node, where, seen=frozenset()):
    """Objects with named properties that do not say additionalProperties: false."""
    if "$ref" in node:
        if node["$ref"] in seen:
            return []
        seen = seen | {node["$ref"]}
        return closed_problems(spec, resolve(spec, node), node["$ref"], seen)
    out = []
    if node.get("type") == "object" and "properties" in node and node.get("additionalProperties") is not False:
        out.append(where)
    for name, child in node.get("properties", {}).items():
        out += closed_problems(spec, child, f"{where}.{name}", seen)
    if isinstance(node.get("items"), dict):
        out += closed_problems(spec, node["items"], f"{where}[]", seen)
    for key in ("allOf", "oneOf", "anyOf"):
        for child in node.get(key, []):
            out += closed_problems(spec, child, where, seen)
    if isinstance(node.get("additionalProperties"), dict):  # a map: its values may be objects
        out += closed_problems(spec, node["additionalProperties"], f"{where}{{}}", seen)
    return out


def test_every_request_object_has_additional_properties_false(spec):
    bodies = list(request_schemas(spec))
    assert len(bodies) >= 3
    problems = [p for where, schema in bodies for p in closed_problems(spec, schema, where)]
    assert problems == []


def validator(spec, schema_name):
    return Draft202012Validator({"$ref": f"#/components/schemas/{schema_name}", "components": spec["components"]})


SNAPSHOT = {
    "snapshot_id": 11,
    "sequence_number": 1,
    "timestamp_ms": 1760000000000,
    "operation": "append",
    "summary": {"added-records": "3"},
    "schema": {"type": "struct", "schema-id": 0, "fields": [{"id": 1, "name": "a", "required": False, "type": "long"}]},
    "added_files": [{"file_id": "a" * 64, "size_bytes": 10, "record_count": 3, "format": "PARQUET"}],
    "removed_file_ids": [],
}
PUSH = {
    "target_workspace_id": "ws_1",
    "table": {"namespace": ["main"], "name": "orders"},
    "source_table_uuid": "6a1f3c52-0d4e-4c1c-9a53-0c8d4f1b2e77",
    "format_version": 2,
    "base_snapshot_id": None,
    "snapshots": [SNAPSHOT],
}


def test_a_well_formed_push_request_validates(spec):
    assert list(validator(spec, "CreatePushRequest").iter_errors(PUSH)) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(extra=1),
        lambda r: r["table"].update(extra=1),
        lambda r: r["snapshots"][0].update(extra=1),
        lambda r: r["snapshots"][0]["added_files"][0].update(extra=1),
        lambda r: r["snapshots"][0]["schema"].update(extra=1),
        lambda r: r["snapshots"][0]["schema"]["fields"][0].update(extra=1),
        lambda r: r["snapshots"][0]["schema"]["fields"][0].pop("id"),  # not a valid Iceberg schema
        lambda r: r["snapshots"][0]["schema"]["fields"].append(
            {"id": 2, "name": "m", "required": True, "type": {"type": "map", "key-id": 3, "key": "string"}}
        ),
        lambda r: r["snapshots"][0]["added_files"][0].update(format="ORC"),
        lambda r: r.update(format_version=3),
    ],
)
def test_unknown_fields_and_invalid_schemas_are_refused(spec, mutate):
    bad = copy.deepcopy(PUSH)
    mutate(bad)
    assert list(validator(spec, "CreatePushRequest").iter_errors(bad)), bad


def test_the_sign_in_requests_are_closed_too(spec):
    code = {
        "grant_type": "authorization_code",
        "client_id": "resink-catalog",
        "code": "c",
        "code_verifier": "v" * 43,
        "redirect_uri": "http://127.0.0.1:5000/callback",
    }
    assert list(validator(spec, "AuthorizationCodeGrant").iter_errors(code)) == []
    assert list(validator(spec, "AuthorizationCodeGrant").iter_errors({**code, "scope": "all"}))
    refresh = {"grant_type": "refresh_token", "client_id": "resink-catalog", "refresh_token": "r"}
    assert list(validator(spec, "RefreshGrant").iter_errors(refresh)) == []
    assert list(validator(spec, "RefreshGrant").iter_errors({**refresh, "device_id": "x"}))


# ---------------------------------------------------------------- ownership, retry and revocation


def test_session_operations_answer_404_for_other_devices_and_say_so(spec):
    session_ops = [(p, m, op) for p, m, op, _ in operations(spec) if "{push_id}" in p]
    assert {(m, p.rsplit("/", 1)[-1]) for p, m, _ in session_ops} >= {("get", "{push_id}"), ("post", "commit")}
    for path, method, op in session_ops:
        assert "404" in op["responses"], (method, path)
        assert "another device" in op["description"] and "404 not_found" in op["description"], (method, path)


def test_table_state_answers_404_for_other_orgs_and_not_402_423(spec):
    _, _, op, _ = next(o for o in operations(spec) if o[2].get("operationId") == "getTableState")
    assert "404" in op["responses"] and "402" not in op["responses"]
    assert "other workspace" in op["description"] or "any other workspace" in op["description"]
    assert "403" in spec["paths"]["/push/v0/pushes"]["post"]["responses"]  # a workspace of another org: 403 (P12)


def test_429_and_503_carry_retry_after(spec):
    for path, method, op, _ in operations(spec):
        if not path.startswith("/push/v0"):
            continue
        for status in ("429", "503"):
            if status in op["responses"]:
                resolved = resolve(spec, op["responses"][status])
                assert "Retry-After" in resolved.get("headers", {}), (method, path, status)


def test_revocation_wording_says_within_the_poll_interval_not_at_exp(spec):
    text = yaml.dump(spec["paths"]["/v1/device/revoke"]) + yaml.dump(spec["components"]["securitySchemes"])
    assert "poll interval" in text and "5 minutes" in text
    assert "stay valid until" not in text
    assert "/v1/devices/revoked" in text


def test_admission_replaces_quota_in_the_session(spec):
    session = spec["components"]["schemas"]["PushSession"]
    assert "admission" in session["properties"] and "quota" not in session["properties"]
    outcomes = spec["components"]["schemas"]["Admission"]["properties"]["outcome"]["enum"]
    assert outcomes == ["accepted", "over_included"]
    ceiling = resolve(spec, spec["components"]["schemas"]["StorageCeilingProblem"])["allOf"][1]
    assert {"ceiling_bytes", "used_bytes", "reserved_bytes", "incoming_bytes"} <= set(ceiling["properties"])
    assert "committed_through" in session["properties"]


def test_commit_of_an_over_included_session_needs_confirmation(spec):
    commit = spec["paths"]["/push/v0/pushes/{push_id}/commit"]["post"]
    assert "confirm_over_included" in commit["description"] and "confirmation_required" in commit["description"]
    assert "409" in commit["responses"] and "confirmation_required" in commit["responses"]["409"]["description"]
    body = commit["requestBody"]["content"]["application/json"]["schema"]
    v = validator(spec, body["$ref"].rsplit("/", 1)[-1])
    assert list(v.iter_errors({})) == [] and list(v.iter_errors({"confirm_over_included": True})) == []
    assert list(v.iter_errors({"confirm_over_included": "yes"})) and list(v.iter_errors({"confirm": True}))
    problem = spec["components"]["schemas"]["Problem"]["properties"]
    assert "estimated_extra_monthly_cost" in problem  # the refusal names the cost
