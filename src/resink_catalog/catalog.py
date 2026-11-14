"""The Local Catalog as a library: tables, sources, import and queries. The CLI, the HTTP API and scripts use this."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import warnings
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchNamespaceError, NoSuchTableError
from pyiceberg.expressions import EqualTo

from .home import default_home, ensure_home, init_db
from .query import query_arrow, run_query
from .readers import ReadError, apply_types, detect_format, file_hash, read_file, type_name
from .store import DEFAULT_NAMESPACE, open_iceberg, slug, split_name

log = logging.getLogger(__name__)

SOURCE_COLUMN = "_source_file"  # present in tables built from several export files of one source
SOURCE_FIELDS = (
    "id root_id path format content_hash target_table mode last_import status error rows column_types".split()
)
DATE_PARTS = re.compile(r"(\d{4}[-_]?\d{2}[-_]?\d{2}|\d{8}|\d{4}[-_]\d{2}|\d+)")


SOURCES_OF_TEXT = ("user", "ai")  # who wrote a tag or note
MAX_TAG, MAX_TAGS_PER_CALL, MAX_NOTE = 64, 20, 5000


class ImportErrorForUser(Exception):
    """Raised for problems the user can act on (unknown folder, nothing proposed)."""


@dataclass
class ImportResult:
    source_id: int
    path: str
    table: str
    outcome: str  # created | updated | unchanged | error
    rows: int | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _group_key(path: Path) -> str:
    """'sales_2026-10-01.csv' and 'sales_2026-10-02.csv' share the key 'sales_#'."""
    return DATE_PARTS.sub("#", path.stem.lower())


class Catalog:
    def __init__(self, home: Path | str | None = None):
        self.home = ensure_home(Path(home) if home else default_home())
        self.db_path = init_db(self.home)
        self.iceberg = open_iceberg(self.home)
        self.migrated = self._migrate_default_namespace()

    def _migrate_default_namespace(self) -> list[str]:
        """Catalogs made before the rename have their tables in `default`: move them to `main` (nothing is released
        yet, but a developer's catalog should keep working). Returns the moved table names; logs what it did."""
        old, new = "default", DEFAULT_NAMESPACE
        if (old,) not in self.iceberg.list_namespaces() or (new,) in self.iceberg.list_namespaces():
            return []
        self._ensure_namespace(new)
        moved = []
        for _, name in self.iceberg.list_tables(old):
            self.iceberg.rename_table((old, name), (new, name))  # the catalog row moves; data files stay in place
            moved.append(f"{new}.{name}")
        if not self.iceberg.list_tables(old):
            self.iceberg.drop_namespace(old)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for m in moved:
                o = f"{old}.{m.split('.', 1)[1]}"
                conn.execute("UPDATE data_sources SET target_table = ? WHERE target_table = ?", (m, o))
                conn.execute("UPDATE tags SET table_name = ? WHERE table_name = ?", (m, o))
                conn.execute("UPDATE notes SET target = ? WHERE target = ?", (m, o))
                conn.execute(
                    "UPDATE notes SET target = ? || substr(target, ?) WHERE target LIKE ?", (m, len(o) + 1, o + ".%")
                )
        log.info("moved %d table(s) from namespace %r to %r: %s", len(moved), old, new, ", ".join(moved))
        return moved

    # ------------------------------------------------------------------ tables

    def _ensure_namespace(self, ns: str) -> None:
        try:
            self.iceberg.create_namespace(ns)
        except NamespaceAlreadyExistsError:
            pass

    def list_tables(self) -> list[dict]:
        tags: dict[str, list[dict]] = defaultdict(list)
        for t in self.list_tags():
            tags[t["table"]].append({"tag": t["tag"], "source": t["source"]})
        out = []
        for ns in self.iceberg.list_namespaces():
            for ident in self.iceberg.list_tables(ns):
                t = self.iceberg.load_table(ident)
                snap = t.current_snapshot()
                out.append(
                    {
                        "name": ".".join(ident),
                        "columns": [f.name for f in t.schema().fields],
                        "snapshots": len(t.metadata.snapshots),
                        "current_snapshot_id": snap.snapshot_id if snap else None,
                        "rows": int(snap.summary["total-records"]) if snap and snap.summary else 0,
                        "tags": tags.get(".".join(ident), []),
                    }
                )
        return sorted(out, key=lambda d: d["name"])

    def push_blockers(self, name: str) -> list[str]:
        """Reasons push protocol v0 would refuse this table (empty list: it can be pushed)."""
        t = self.iceberg.load_table(split_name(name))
        blockers = []
        if not t.spec().is_unpartitioned():
            blockers.append("partitioned")
        tasks = list(t.scan().plan_files())
        if any(task.delete_files for task in tasks):
            blockers.append("delete files")  # written by merge-on-read engines such as DuckDB DELETE and UPDATE
        if any(task.file.file_format.name != "PARQUET" for task in tasks):
            blockers.append("non-Parquet data files")
        if t.metadata.format_version not in (1, 2):
            blockers.append(f"format version {t.metadata.format_version}")
        return blockers

    # -------------------------------------------------------------- tags and notes (design section 5)

    def _canonical(self, name: str) -> str:
        """'t' -> 'main.t'; raises when the table does not exist, so annotations never point at nothing."""
        ns, tbl = split_name(name)
        try:
            self.iceberg.load_table((ns, tbl))
        except (NoSuchTableError, NoSuchNamespaceError):
            raise ImportErrorForUser(f"no such table: {ns}.{tbl}") from None
        return f"{ns}.{tbl}"

    def _target(self, target: str) -> str:
        """A note is about a table ('shop.orders') or one of its columns ('shop.orders.amount')."""
        parts = target.split(".")
        if len(parts) >= 3:
            table, column = ".".join(parts[:2]), ".".join(parts[2:])
        elif len(parts) == 2 and not self._exists(target):
            raise ImportErrorForUser(f"no such table: {target}")
        else:
            table, column = target, None
        canonical = self._canonical(table)
        if column is not None:
            names = [f.name for f in self.iceberg.load_table(split_name(canonical)).schema().fields]
            if column not in names:
                raise ImportErrorForUser(f"table {canonical} has no column {column!r}")
            return f"{canonical}.{column}"
        return canonical

    def add_tags(self, table: str, tags: list[str], source: str = "user") -> list[dict]:
        """Add tags to a table (existing ones are kept; a tag keeps the source that first wrote it)."""
        if source not in SOURCES_OF_TEXT:
            raise ValueError("source must be 'user' or 'ai'")
        cleaned = list(dict.fromkeys(t.strip().lower() for t in tags if t and t.strip()))
        if not cleaned or len(cleaned) > MAX_TAGS_PER_CALL:
            raise ImportErrorForUser(f"give between 1 and {MAX_TAGS_PER_CALL} tags")
        if any(len(t) > MAX_TAG or any(c in t for c in "\n\r\t") for t in cleaned):
            raise ImportErrorForUser(f"a tag is at most {MAX_TAG} characters on one line")
        name = self._canonical(table)
        with closing(self._conn()) as conn, conn:
            for tag in cleaned:
                conn.execute(
                    "INSERT OR IGNORE INTO tags (table_name, tag, source, created_at) VALUES (?, ?, ?, ?)",
                    (name, tag, source, _now()),
                )
        return self.list_tags(name)

    def list_tags(self, table: str | None = None) -> list[dict]:
        with closing(self._conn()) as conn:
            q = 'SELECT id, table_name AS "table", tag, source, created_at FROM tags'
            args: tuple = ()
            if table is not None:
                q += " WHERE table_name = ?"
                args = (self._canonical(table),)
            return [dict(r) for r in conn.execute(q + " ORDER BY table_name, tag", args)]

    def remove_tag(self, table: str, tag: str) -> bool:
        with closing(self._conn()) as conn, conn:
            cur = conn.execute(
                "DELETE FROM tags WHERE table_name = ? AND tag = ?", (self._canonical(table), tag.strip().lower())
            )
        return cur.rowcount > 0

    def write_note(self, target: str, body: str, author: str = "user") -> dict:
        """Add a note (Markdown) about a table or one column. Notes are kept, never overwritten."""
        if author not in SOURCES_OF_TEXT:
            raise ValueError("author must be 'user' or 'ai'")
        body = body.strip()
        if not body or len(body) > MAX_NOTE:
            raise ImportErrorForUser(f"a note has between 1 and {MAX_NOTE} characters")
        where = self._target(target)
        with closing(self._conn()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO notes (target, body, author, created_at) VALUES (?, ?, ?, ?)",
                (where, body, author, _now()),
            )
        return self.get_note(cur.lastrowid)

    def get_note(self, note_id: int) -> dict:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            raise ImportErrorForUser(f"no such note: {note_id}")
        return dict(row)

    def list_notes(self, table: str | None = None) -> list[dict]:
        """Notes about a table and its columns, oldest first."""
        with closing(self._conn()) as conn:
            if table is None:
                rows = conn.execute("SELECT * FROM notes ORDER BY id")
            else:
                name = self._canonical(table)
                rows = conn.execute(
                    "SELECT * FROM notes WHERE target = ? OR target LIKE ? ORDER BY id", (name, name + ".%")
                )
            return [dict(r) for r in rows]

    def delete_note(self, note_id: int) -> bool:
        with closing(self._conn()) as conn, conn:
            return conn.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount > 0

    def table_info(self, name: str) -> dict:
        ns, tbl = split_name(name)
        t = self.iceberg.load_table((ns, tbl))
        blockers = self.push_blockers(name)
        return {
            "tags": [{"tag": t["tag"], "source": t["source"]} for t in self.list_tags(f"{ns}.{tbl}")],
            "notes": self.list_notes(f"{ns}.{tbl}"),
            "push_ready": not blockers,
            "push_blockers": blockers,
            "name": f"{ns}.{tbl}",
            "schema": [{"name": f.name, "type": str(f.field_type), "required": f.required} for f in t.schema().fields],
            "snapshots": [
                {
                    "snapshot_id": s.snapshot_id,
                    "timestamp_ms": s.timestamp_ms,
                    "operation": s.summary.operation.value,
                    **_counts(s),
                }
                for s in t.metadata.snapshots
            ],
            "location": t.location(),
        }

    def _write(self, name: str, data: pa.Table, mode: str, source_file: str | None = None) -> tuple[bool, int]:
        """Write an Arrow table. Returns (created, rows). `source_file` replaces only that file's rows."""
        ns, tbl = split_name(name)
        self._ensure_namespace(ns)
        created = False
        try:
            table = self.iceberg.load_table((ns, tbl))
        except NoSuchTableError:
            table = self.iceberg.create_table((ns, tbl), schema=data.schema)
            created = True
        if not created:
            with table.update_schema() as update:
                update.union_by_name(data.schema)
            table = self.iceberg.load_table((ns, tbl))
            data = _align(data, table)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Delete operation did not match any records")
            if source_file is not None:
                table.overwrite(data, overwrite_filter=EqualTo(SOURCE_COLUMN, source_file))
            elif mode == "append":
                table.append(data)
            else:
                table.overwrite(data)
        return created, data.num_rows

    def write_table(self, name: str, data: Any, mode: str = "overwrite") -> None:
        """Write a DataFrame (pandas) or Arrow table as an Iceberg table. `mode` is 'overwrite' or 'append'."""
        if mode not in ("overwrite", "append"):
            raise ValueError("mode must be 'overwrite' or 'append'")
        arrow = data if isinstance(data, pa.Table) else pa.Table.from_pandas(data, preserve_index=False)
        self._write(name, arrow, mode)

    def read_table(self, name: str):
        """The current snapshot as a pandas DataFrame."""
        return self.iceberg.load_table(split_name(name)).scan().to_pandas()

    def query(self, sql: str, limit: int | None = None):
        """Read-only SQL over all tables as a pandas DataFrame. Tables are 'namespace.table', or just 'table'
        for the default namespace."""
        return query_arrow(self.iceberg, sql, limit).to_pandas()

    def query_json(self, sql: str, limit: int = 1000) -> dict:
        return run_query(self.iceberg, sql, limit=limit)

    # ----------------------------------------------------------------- sources

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def sources(self, status: str | None = None) -> list[dict]:
        with closing(self._conn()) as conn:
            q = f"SELECT {', '.join(SOURCE_FIELDS)} FROM data_sources"
            rows = conn.execute(
                q + (" WHERE status = ?" if status else "") + " ORDER BY path",
                (status,) if status else (),
            )
            return [_source_dict(r) for r in rows]

    def roots(self) -> list[dict]:
        """Added folders with the number of sources in each status."""
        with closing(self._conn()) as conn:
            roots = [dict(r) for r in conn.execute("SELECT id, path, name, last_scan FROM data_roots ORDER BY path")]
            for r in roots:
                counts = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM data_sources WHERE root_id = ? GROUP BY status", (r["id"],)
                )
                r["counts"] = {c["status"]: c["n"] for c in counts}
        return roots

    def source(self, source_id: int) -> dict:
        with closing(self._conn()) as conn:
            row = conn.execute(
                f"SELECT {', '.join(SOURCE_FIELDS)} FROM data_sources WHERE id = ?", (source_id,)
            ).fetchone()
        if row is None:
            raise ImportErrorForUser(f"unknown source {source_id}")
        return _source_dict(row)

    def preview(self, source_id: int, limit: int = 20) -> dict:
        """What importing this source would produce: inferred types, the user's overrides and sample rows."""
        src = self.source(source_id)
        out: dict[str, Any] = {"source": src, "columns": [], "rows": [], "error": None}
        try:
            inferred = read_file(Path(src["path"]), src["format"])
        except (ReadError, OSError) as e:
            out["error"] = str(e)
            return out
        overrides = src["column_types"] or {}
        out["columns"] = [
            {"name": f.name, "inferred": type_name(f.type), "type": overrides.get(f.name, type_name(f.type))}
            for f in inferred.schema
        ]
        out["total_rows"] = inferred.num_rows
        try:
            shown = apply_types(inferred, overrides)
        except ReadError as e:
            out["error"] = str(e)  # the override does not fit the data; show the data as inferred
            shown = inferred
        out["rows"] = json.loads(json.dumps(shown.slice(0, limit).to_pylist(), default=str))
        return out

    def set_types(self, source_id: int, column_types: dict[str, str]) -> dict:
        """Save the user's column type choices for a source; they are used by every import of it."""
        from .readers import TYPE_NAMES

        bad = {c: t for c, t in column_types.items() if t not in TYPE_NAMES}
        if bad:
            raise ImportErrorForUser(f"unknown type {bad}; choose one of {', '.join(TYPE_NAMES)}")
        self.source(source_id)
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "UPDATE data_sources SET column_types = ? WHERE id = ?",
                (json.dumps(column_types) if column_types else None, source_id),
            )
        return self.source(source_id)

    def sample(self, name: str, limit: int = 20) -> dict:
        t = self.iceberg.load_table(split_name(name))
        data = t.scan(limit=limit).to_arrow()
        return {
            "columns": data.column_names,
            "rows": json.loads(json.dumps([list(r.values()) for r in data.to_pylist()], default=str)),
        }

    def add_folder(self, folder: Path | str) -> list[dict]:
        """Propose: find data files, group exports of the same source, choose target tables. Imports nothing."""
        root = Path(folder).expanduser().resolve()
        if not root.is_dir():
            raise ImportErrorForUser(f"not a folder: {root}")
        files = sorted(p for p in root.rglob("*") if p.is_file() and detect_format(p) and not p.name.startswith("."))
        ns = slug(root.name, "imported")
        plans = _plan(files, ns)
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT INTO data_roots (path, name, last_scan) VALUES (?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET last_scan = excluded.last_scan",
                (str(root), root.name, _now()),
            )
            root_id = conn.execute("SELECT id FROM data_roots WHERE path = ?", (str(root),)).fetchone()[0]
            for path, plan in plans.items():
                conn.execute(
                    "INSERT INTO data_sources (root_id, path, format, target_table, mode, status, error) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(path) DO NOTHING",
                    (
                        root_id,
                        str(path),
                        plan["format"],
                        plan["table"],
                        plan["mode"],
                        plan["status"],
                        plan["error"],
                    ),
                )
        return [s for s in self.sources() if s["root_id"] == root_id]

    def register_file(self, path: Path | str, table: str | None = None) -> int:
        """Make a single file a source (status `proposed`) without importing it. Returns the source id."""
        p = Path(path).expanduser().resolve()
        fmt = detect_format(p)
        if not p.is_file() or fmt is None:
            raise ImportErrorForUser(f"not a CSV or Parquet file: {p}")
        with closing(self._conn()) as conn, conn:
            row = conn.execute("SELECT id, target_table FROM data_sources WHERE path = ?", (str(p),)).fetchone()
            if row is None:
                target = table or f"{DEFAULT_NAMESPACE}.{slug(p.stem)}"
                cur = conn.execute(
                    "INSERT INTO data_sources (path, format, target_table, mode, status) "
                    "VALUES (?, ?, ?, 'overwrite', 'proposed')",
                    (str(p), fmt, target),
                )
                source_id = cur.lastrowid
            else:
                source_id = row["id"]
                if table and table != row["target_table"]:
                    conn.execute("UPDATE data_sources SET target_table = ? WHERE id = ?", (table, source_id))
        return source_id

    def import_file(
        self, path: Path | str, table: str | None = None, column_types: dict[str, str] | None = None
    ) -> ImportResult:
        """Import one file now. Without `table` it goes to main.<file name>. `column_types` overrides the
        inferred types (and is remembered for later imports of the same file)."""
        source_id = self.register_file(path, table)
        if column_types is not None:
            self.set_types(source_id, column_types)
        return self.import_source(source_id)

    def import_folder(self, folder: Path | str) -> list[ImportResult]:
        """Apply the proposal for a folder: import every source of the root that is proposed or changed."""
        root = Path(folder).expanduser().resolve()
        with closing(self._conn()) as conn:
            r = conn.execute("SELECT id FROM data_roots WHERE path = ?", (str(root),)).fetchone()
        if r is None:
            raise ImportErrorForUser(f"{root} was not added yet; run `resink-catalog add-folder {root}` first")
        todo = [
            s for s in self.sources() if s["root_id"] == r["id"] and s["status"] in ("proposed", "changed", "error")
        ]
        return [self.import_source(s["id"]) for s in todo]

    def import_source(self, source_id: int) -> ImportResult:
        with closing(self._conn()) as conn:
            src = conn.execute("SELECT * FROM data_sources WHERE id = ?", (source_id,)).fetchone()
        if src is None:
            raise ImportErrorForUser(f"unknown source {source_id}")
        path = Path(src["path"])
        target = src["target_table"]
        try:
            digest = file_hash(path)
        except OSError as e:
            return self._finish(src, "error", error=f"cannot read file: {e}", status="missing")
        if src["status"] == "imported" and src["content_hash"] == digest and self._exists(target):
            return ImportResult(source_id, src["path"], target, "unchanged", src["rows"])
        try:
            data = apply_types(read_file(path, src["format"]), json.loads(src["column_types"] or "{}"))
            source_file = None
            if src["mode"] == "append":
                data = data.append_column(SOURCE_COLUMN, pa.array([path.name] * data.num_rows, pa.string()))
                source_file = path.name
            created, rows = self._write(target, data, src["mode"], source_file)
        except ReadError as e:
            return self._finish(src, "error", error=str(e))
        except Exception as e:  # a schema clash or Iceberg refusal: report it on the source, keep going
            hint = ""
            if "type" in str(e).lower() and self._exists(target):
                hint = " (a column type cannot change on a table that already exists: drop the table and import again)"
            return self._finish(src, "error", error=f"{type(e).__name__}: {e}{hint}")
        return self._finish(src, "created" if created else "updated", rows=rows, digest=digest)

    def _exists(self, name: str) -> bool:
        try:
            self.iceberg.load_table(split_name(name))
            return True
        except NoSuchTableError:
            return False

    def _finish(self, src, outcome, rows=None, error=None, digest=None, status=None) -> ImportResult:
        ok = outcome in ("created", "updated")
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "UPDATE data_sources SET status = ?, error = ?, rows = COALESCE(?, rows), "
                "content_hash = COALESCE(?, content_hash), last_import = COALESCE(?, last_import) WHERE id = ?",
                (
                    status or ("imported" if ok else "error"),
                    error,
                    rows,
                    digest,
                    _now() if ok else None,
                    src["id"],
                ),
            )
        return ImportResult(src["id"], src["path"], src["target_table"], outcome, rows, error)

    def scan(self, apply: bool = False) -> dict:
        """Re-check every source and every added folder. Marks changed and missing files, and proposes new ones.
        With apply=True the changed sources are re-imported."""
        report: dict[str, list] = {
            "changed": [],
            "missing": [],
            "unchanged": [],
            "new": [],
            "imported": [],
        }
        with closing(self._conn()) as conn:
            roots = [r["path"] for r in conn.execute("SELECT path FROM data_roots")]
        known = {s["path"] for s in self.sources()}
        for root in roots:
            for s in self.add_folder(root):
                if s["path"] not in known:
                    report["new"].append(s["path"])
        for s in self.sources():
            if s["status"] not in ("imported", "changed"):
                continue
            path = Path(s["path"])
            if not path.exists():
                self._set_status(s["id"], "missing")
                report["missing"].append(s["path"])
            elif file_hash(path) != s["content_hash"]:
                self._set_status(s["id"], "changed")
                report["changed"].append(s["path"])
            else:
                report["unchanged"].append(s["path"])
        if apply:
            for p in report["changed"]:
                with closing(self._conn()) as conn:
                    sid = conn.execute("SELECT id FROM data_sources WHERE path = ?", (p,)).fetchone()[0]
                report["imported"].append(self.import_source(sid).as_dict())
        return report

    def _set_status(self, source_id: int, status: str) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute("UPDATE data_sources SET status = ? WHERE id = ?", (status, source_id))


def _counts(snapshot) -> dict:
    props = snapshot.summary.additional_properties if snapshot.summary else {}
    return {
        "added_records": int(props.get("added-records", 0)),
        "deleted_records": int(props.get("deleted-records", 0)),
        "total_records": int(props.get("total-records", 0)),
    }


def _source_dict(row) -> dict:
    d = dict(row)
    d["column_types"] = json.loads(d["column_types"]) if d.get("column_types") else {}
    return d


def _align(data: pa.Table, table) -> pa.Table:
    """Cast incoming columns to the existing table's types and add missing columns as nulls."""
    target = table.schema().as_arrow()
    columns = []
    for field in target:
        if field.name in data.schema.names:
            columns.append(data.column(field.name).cast(field.type))
        else:
            columns.append(pa.nulls(data.num_rows, field.type))
    return pa.table(columns, schema=target)


def _plan(files: list[Path], namespace: str) -> dict[Path, dict]:
    """Choose target tables. Files whose names differ only by digits or dates and whose columns match are exports
    of one source: they share one table and are appended one file at a time."""
    from .readers import read_csv  # local import keeps `import resink_catalog` light

    heads: dict[Path, list[str] | None] = {}
    for f in files:
        try:
            if detect_format(f) == "csv":
                heads[f] = read_csv(f).schema.names
            else:
                import pyarrow.parquet as pq

                heads[f] = pq.read_schema(f).names
        except Exception:
            heads[f] = None  # reported when imported
    groups: dict[tuple, list[Path]] = defaultdict(list)
    for f in files:
        groups[(f.parent, _group_key(f), tuple(heads[f] or ()))].append(f)
    plans: dict[Path, dict] = {}
    used: dict[str, int] = defaultdict(int)

    def unique(name: str) -> str:  # unrelated sources never share a table name
        used[name] += 1
        return name if used[name] == 1 else f"{name}_{used[name]}"

    for (_, key, cols), members in groups.items():
        if not cols:
            for f in members:
                plans[f] = _entry(
                    f,
                    unique(f"{namespace}.{slug(f.stem)}"),
                    "overwrite",
                    "error",
                    "file could not be read; fix it and run scan",
                )
        elif len(members) > 1 and "#" in key:
            table = unique(f"{namespace}.{slug(key.replace('#', ''))}")
            for f in members:
                plans[f] = _entry(f, table, "append")
        else:
            for f in members:
                plans[f] = _entry(f, unique(f"{namespace}.{slug(f.stem)}"), "overwrite")
    return plans


def _entry(path: Path, table: str, mode: str, status: str = "proposed", error: str | None = None) -> dict:
    return {
        "format": detect_format(path),
        "table": table,
        "mode": mode,
        "status": status,
        "error": error,
    }
