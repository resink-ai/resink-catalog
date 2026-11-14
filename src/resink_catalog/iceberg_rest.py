"""Iceberg REST catalog v1 over the local PyIceberg SQL catalog (design section 4).

In scope: config; namespaces (create, list, get, drop, properties); tables (list, create, load, exists, commit,
drop, rename). Out of scope: views, multi-table transactions, scan planning, credential vending, `prefix`.
Clients write data and manifest files themselves (the files are local); the server stores the table metadata.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from pyiceberg.catalog import Catalog as IcebergCatalog
from pyiceberg.exceptions import (
    CommitFailedException,
    NamespaceAlreadyExistsError,
    NamespaceNotEmptyError,
    NoSuchNamespaceError,
    NoSuchTableError,
    TableAlreadyExistsError,
)
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import CommitTableRequest
from pyiceberg.table.sorting import SortOrder
from pyiceberg.table.update import AssertCreate

SEP = "\x1f"  # separator of namespace levels in a path, as the spec says


def _error(status: int, type_: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": type_, "code": status}}, status_code=status)


def _ns(raw: str) -> tuple[str, ...]:
    return tuple(raw.split(SEP)) if raw else ()


def _table_response(table, extra: dict | None = None) -> dict:
    body = {
        "metadata-location": table.metadata_location,
        "metadata": json.loads(table.metadata.model_dump_json()),
        "config": {},
    }
    return body | (extra or {})


def build_router(get_catalog: Callable[[], IcebergCatalog]) -> APIRouter:
    r = APIRouter(prefix="/iceberg/v1")

    def guard(fn):
        """Map catalog exceptions to the error documents the spec defines."""

        @functools.wraps(fn)
        async def wrapper(*a, **kw):
            try:
                return await fn(*a, **kw)
            except NoSuchNamespaceError as e:
                return _error(404, "NoSuchNamespaceException", str(e))
            except NoSuchTableError as e:
                return _error(404, "NoSuchTableException", str(e))
            except NamespaceAlreadyExistsError as e:
                return _error(409, "AlreadyExistsException", str(e))
            except TableAlreadyExistsError as e:
                return _error(409, "AlreadyExistsException", str(e))
            except NamespaceNotEmptyError as e:
                return _error(409, "NamespaceNotEmptyException", str(e))
            except CommitFailedException as e:
                return _error(409, "CommitFailedException", str(e))
            except (ValidationError, ValueError) as e:
                return _error(400, "BadRequestException", str(e))

        return wrapper

    @r.get("/config")
    async def config(warehouse: str | None = None) -> dict:
        # No `endpoints` list: clients then assume the v1 defaults, and we document the supported subset in the README.
        return {"defaults": {}, "overrides": {}}

    @r.get("/namespaces")
    @guard
    async def list_namespaces(parent: str | None = None) -> dict:
        found = get_catalog().list_namespaces(_ns(parent) if parent else ())
        return {"namespaces": [list(n) for n in found]}

    @r.post("/namespaces")
    @guard
    async def create_namespace(request: Request) -> Any:
        body = await request.json()
        ns = tuple(body["namespace"])
        get_catalog().create_namespace(ns, body.get("properties") or {})
        return {"namespace": list(ns), "properties": get_catalog().load_namespace_properties(ns)}

    @r.get("/namespaces/{namespace}")
    @guard
    async def load_namespace(namespace: str) -> dict:
        ns = _ns(namespace)
        return {"namespace": list(ns), "properties": get_catalog().load_namespace_properties(ns)}

    @r.head("/namespaces/{namespace}")
    async def namespace_exists(namespace: str) -> Response:
        try:
            get_catalog().load_namespace_properties(_ns(namespace))
            return Response(status_code=204)
        except NoSuchNamespaceError:
            return Response(status_code=404)

    @r.delete("/namespaces/{namespace}")
    @guard
    async def drop_namespace(namespace: str) -> Response:
        get_catalog().drop_namespace(_ns(namespace))
        return Response(status_code=204)

    @r.post("/namespaces/{namespace}/properties")
    @guard
    async def update_properties(namespace: str, request: Request) -> dict:
        body = await request.json()
        res = get_catalog().update_namespace_properties(
            _ns(namespace), set(body.get("removals") or []), body.get("updates") or {}
        )
        return {"updated": res.updated, "removed": res.removed, "missing": res.missing}

    @r.get("/namespaces/{namespace}/tables")
    @guard
    async def list_tables(namespace: str) -> dict:
        found = get_catalog().list_tables(_ns(namespace))
        return {"identifiers": [{"namespace": list(i[:-1]), "name": i[-1]} for i in found]}

    @r.post("/namespaces/{namespace}/tables")
    @guard
    async def create_table(namespace: str, request: Request) -> dict:
        body = await request.json()
        ns = _ns(namespace)
        cat = get_catalog()
        cat.load_namespace_properties(ns)  # 404 when missing
        spec = body.get("partition-spec")
        order = body.get("write-order")
        # `stage-create` is honoured by creating the table now: the client's later commit then finds it.
        table = cat.create_table(
            (*ns, body["name"]),
            schema=Schema.model_validate(body["schema"]),
            location=body.get("location"),
            partition_spec=PartitionSpec.model_validate(spec) if spec else PartitionSpec(),
            sort_order=SortOrder.model_validate(order) if order else SortOrder(),
            properties=body.get("properties") or {},
        )
        return _table_response(table)

    @r.get("/namespaces/{namespace}/tables/{table}")
    @guard
    async def load_table(namespace: str, table: str) -> dict:
        return _table_response(get_catalog().load_table((*_ns(namespace), table)))

    @r.head("/namespaces/{namespace}/tables/{table}")
    async def table_exists(namespace: str, table: str) -> Response:
        try:
            get_catalog().load_table((*_ns(namespace), table))
            return Response(status_code=204)
        except NoSuchTableError:
            return Response(status_code=404)

    @r.post("/namespaces/{namespace}/tables/{table}")
    @guard
    async def commit_table(namespace: str, table: str, request: Request) -> dict:
        body = await request.json()
        cat = get_catalog()
        current = cat.load_table((*_ns(namespace), table))
        req = CommitTableRequest.model_validate(body)
        # AssertCreate was satisfied when `stage-create` created the table; anything else is checked for real.
        requirements = tuple(x for x in req.requirements if not isinstance(x, AssertCreate))
        result = cat.commit_table(current, requirements, req.updates)
        return {
            "metadata-location": result.metadata_location,
            "metadata": json.loads(result.metadata.model_dump_json()),
        }

    @r.delete("/namespaces/{namespace}/tables/{table}")
    @guard
    async def drop_table(namespace: str, table: str) -> Response:
        get_catalog().drop_table((*_ns(namespace), table))
        return Response(status_code=204)

    @r.post("/tables/rename")
    @guard
    async def rename_table(request: Request) -> Response:
        body = await request.json()
        src, dst = body["source"], body["destination"]
        get_catalog().rename_table((*src["namespace"], src["name"]), (*dst["namespace"], dst["name"]))
        return Response(status_code=204)

    return r
