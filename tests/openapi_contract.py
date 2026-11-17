"""Checks HTTP requests and responses against contracts/push-protocol-v0.openapi.yaml (design section 9: "a contract
test validates both sides' requests and responses against the YAML"). Used by the fake cloud on every exchange."""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

import httpx2
import yaml
from jsonschema import Draft202012Validator

CONTRACT = Path(__file__).parent.parent / "contracts" / "push-protocol-v0.openapi.yaml"
METHODS = ("get", "post", "put", "patch", "delete")


class ContractViolation(AssertionError):
    pass


@cache
def spec() -> dict:
    return yaml.safe_load(CONTRACT.read_text())


def resolve(node):
    while isinstance(node, dict) and "$ref" in node:
        target = spec()
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = target
    return node


def _errors(schema: dict, value) -> list[str]:
    v = Draft202012Validator({**schema, "components": spec()["components"]})
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in v.iter_errors(value)]


def _template_regex(template: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(template)) + "$")


def operation(method: str, path: str) -> tuple[str, dict, dict, dict]:
    """(template, operation, path item, path parameters) for a concrete request path."""
    for template, item in spec()["paths"].items():
        m = _template_regex(template).match(path)
        if m and method.lower() in item:
            return template, item[method.lower()], item, m.groupdict()
    raise ContractViolation(f"{method} {path} is not an operation of the contract")


def _coerce(schema: dict, raw: str):
    schema = resolve(schema)
    if schema.get("type") == "integer":
        try:
            return int(raw)
        except ValueError:
            return raw
    return raw


def check_request(req: httpx2.Request) -> tuple[str, dict]:
    method, path = req.method, req.url.path
    template, op, item, path_params = operation(method, path)
    problems = []
    params = [resolve(p) for p in item.get("parameters", []) + op.get("parameters", [])]
    for p in params:
        where, name, schema = p["in"], p["name"], p.get("schema", {})
        if where == "path":
            values = [path_params.get(name)]
        elif where == "query":
            values = req.url.params.get_list(name)
        elif where == "header":
            values = [req.headers[name]] if name in req.headers else []
        else:
            continue
        values = [v for v in values if v is not None]
        if not values:
            if p.get("required"):
                problems.append(f"missing {where} parameter {name}")
            continue
        value = (
            [_coerce(schema.get("items", {}), v) for v in values]
            if resolve(schema).get("type") == "array"
            else (_coerce(schema, values[0]))
        )
        problems += [f"{where} {name}: {e}" for e in _errors(schema, value)]
    known_query = {p["name"] for p in params if p["in"] == "query"}
    problems += [f"unknown query parameter {k}" for k in req.url.params if k not in known_query]
    secured = op.get("security", spec().get("security")) != []
    if secured and not req.headers.get("Authorization", "").startswith("Bearer "):
        problems.append("missing bearer token")
    body = req.content
    rb = op.get("requestBody")
    if rb is None:
        if body:
            problems.append("the contract declares no request body")
    elif not body:
        if rb.get("required"):
            problems.append("request body required")
    else:
        ctype = req.headers.get("Content-Type", "").split(";")[0].strip()
        if ctype not in rb["content"]:
            problems.append(f"content type {ctype!r} not in {list(rb['content'])}")
        elif ctype.endswith("json"):
            problems += [f"body {e}" for e in _errors(rb["content"][ctype]["schema"], json.loads(body))]
    if problems:
        raise ContractViolation(f"request {method} {path}: " + "; ".join(problems))
    return template, op


def check_response(req: httpx2.Request, resp: httpx2.Response) -> None:
    _, op, _, _ = operation(req.method, req.url.path)
    declared = op["responses"].get(str(resp.status_code)) or op["responses"].get("default")
    where = f"response {resp.status_code} to {req.method} {req.url.path}"
    if declared is None:
        raise ContractViolation(f"{where}: status not declared")
    declared = resolve(declared)
    problems = []
    for name, h in declared.get("headers", {}).items():
        h = resolve(h)
        if (h.get("required") or name == "Retry-After") and name not in resp.headers:
            problems.append(f"missing header {name}")
    content = declared.get("content")
    if content:
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
        if ctype not in content:
            problems.append(f"content type {ctype!r} not in {list(content)}")
        else:
            problems += [f"body {e}" for e in _errors(content[ctype]["schema"], json.loads(resp.content))]
    elif resp.content and resp.status_code != 302:
        problems.append("the contract declares no response body")
    if problems:
        raise ContractViolation(f"{where}: " + "; ".join(problems))
