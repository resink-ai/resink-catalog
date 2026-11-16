"""`resink-catalog push <table>`: send the local snapshots the cloud does not have yet (push protocol sections 4, 5, 7).

capabilities -> table-state -> the local snapshots after the cloud's last one -> refuse unsupported tables before
anything is sent -> per snapshot a delta (data files ADDED and DELETED in its manifests, its schema) -> session with
an Idempotency-Key -> upload what the session says is missing, in chunks with Content-Range -> commit -> poll until
committed or failed -> `push_state` in catalog.db, a cache only (the cloud's table-state is the truth).

Resuming needs no local state. The Idempotency-Key is derived from the device id and the request body, so a rerun
over the same local table sends the same key and gets the same session back, whose `GET /pushes/{id}` lists how many
bytes of each file the receiver already has. If that session ended (expired, aborted, failed) the next key in a chain
derived from its push_id opens a new one.

Choices where the contract and the design are silent (L5a):
- No new local snapshot: no session is created (no `POST /pushes`); the receiver's `200 up_to_date` is still handled.
- History divergence the client can see (the cloud's last pushed snapshot is not in the local history) is refused
  locally with the same code and resolution the receiver would give.
- `409 push_in_progress` carrying a push_id of this device whose session is `open` (made from an older local state):
  the client aborts it and opens its own, once. Anything else is shown to the user.
- More snapshots or files than `/capabilities` allows in one session: consecutive sessions, oldest snapshots first.
- `422 checksum_mismatch`: the file is sent once more from 0, then the push fails.
- A lost connection is retried like 429/503, within the same 5-minute budget (every push call is safe to repeat).
- The idempotency chain's position (the last ended session) is kept in catalog.db (`push_chain`), so it keeps
  advancing across reruns; it is only an optimisation: without it the chain is walked from the start.
- push_id has no format in the contract: the client accepts unreserved URL characters only (one path segment).
- A first push of a table whose oldest snapshots were expired is refused (the deltas would miss live files).
- `over_included` admission: the CLI asks (or `--yes`); without a terminal it cancels and aborts the session.
- The commit is polled every second for up to 15 minutes (a commit runs at most 10).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote

from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError
from pyiceberg.manifest import DataFileContent, FileFormat, ManifestContent, ManifestEntryStatus

from ..catalog import Catalog
from ..store import split_name
from .http import Api, CloudError, fail
from .signin import Device

PROTOCOL_VERSION = "0"
DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024  # section 7: default 8 MiB, within the bounds of /capabilities
HASH_BLOCK = 1024 * 1024
POLL_SECONDS = 1.0
COMMIT_WAIT_SECONDS = 15 * 60  # a commit runs at most 10 minutes (section 7)
SESSION_ATTEMPTS = 20  # ended sessions walked per run; the chain position is kept in catalog.db, so reruns go on
MAX_STALLS = 3  # upload answers in a row that do not move a file forward
# The contract gives push_id no format: accept only unreserved URL characters, so it is always one path segment.
PUSH_ID = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
FILE_ID = re.compile(r"^[0-9a-f]{64}$")  # the contract's FileId
ENDED = ("expired", "aborted", "failed")


@dataclass
class LocalFile:
    file_id: str
    path: str
    size: int
    record_count: int
    io: object


@dataclass
class PushReport:
    table: str
    target: str
    workspace_id: str
    snapshots: int = 0
    files_uploaded: int = 0
    bytes_uploaded: int = 0
    last_pushed_snapshot_id: int | None = None
    cloud_table_id: str | None = None
    sessions: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------------------------- local planning


def ancestry(table) -> tuple[list, bool]:
    """The main branch's snapshots, oldest first, and whether that history is complete. It is not when older
    snapshots were expired: the oldest retained one then names a parent that is gone, or (PyIceberg's expiry clears
    the parent id) its manifests still list live files that an earlier snapshot added."""
    out, snap = [], table.current_snapshot()
    complete = True
    while snap is not None:
        out.append(snap)
        if snap.parent_snapshot_id is None:
            break
        parent = table.snapshot_by_id(snap.parent_snapshot_id)
        if parent is None:
            complete = False
        snap = parent
    out.reverse()
    if complete and out:
        root = out[0]
        complete = all(
            entry.status == ManifestEntryStatus.ADDED and entry.snapshot_id == root.snapshot_id
            for manifest in root.manifests(table.io)
            for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True)
        )
    return out, complete


def new_snapshots(table, cloud_last: int | None) -> list:
    history, complete = ancestry(table)
    if cloud_last is None:
        if not complete:
            # The oldest retained snapshot's delta would leave out every file added before it that is still live:
            # the cloud table would silently miss data. v0 sends deltas only, so refuse.
            raise CloudError(
                "incomplete local history",
                {
                    "title": "Incomplete local history",
                    "detail": f"the table's history starts at snapshot {history[0].snapshot_id}: older snapshots "
                    "were expired. A first push needs the table's whole history, else data files added by the "
                    "expired snapshots would be missing in the cloud. Nothing was sent.",
                    "resolution": [
                        {"action": "use_new_table_name", "detail": "copy the table to a new local table and push that"}
                    ],
                },
            )
        return history
    ids = [s.snapshot_id for s in history]
    if cloud_last not in ids:
        raise CloudError(
            "history diverged",
            {
                "title": "History diverged",
                "code": "history_diverged",
                "detail": f"the cloud's last pushed snapshot {cloud_last} is not in the local table's history "
                "(its snapshots expired, or the table was dropped and recreated). v0 does not repair this.",
                "resolution": [
                    {"action": "use_new_table_name", "detail": "push to a new name with --to NAMESPACE.NAME"}
                ],
            },
        )
    return history[ids.index(cloud_last) + 1 :]


def unsupported_features(table, snapshots) -> list[str]:
    """Section 4, rule 7: format version 1 or 2, Parquet only, unpartitioned, no delete files. Checked over every
    manifest of every snapshot to be pushed, so nothing is uploaded for a table the receiver would refuse."""
    found: list[str] = []

    def note(feature: str) -> None:
        if feature not in found:
            found.append(feature)

    if table.metadata.format_version not in (1, 2):
        note(f"format version {table.metadata.format_version}")
    specs = table.specs()
    if not table.spec().is_unpartitioned():
        note("partitioned")
    for snap in snapshots:
        for manifest in snap.manifests(table.io):
            if manifest.content != ManifestContent.DATA:
                note("delete files")
                continue
            if not specs[manifest.partition_spec_id].is_unpartitioned():
                note("partitioned")
            for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True):
                f = entry.data_file
                if f.content != DataFileContent.DATA:
                    note("delete files")
                elif f.file_format != FileFormat.PARQUET:
                    note("non-Parquet data files")
    return found


def unsupported_problem(features: list[str]) -> CloudError:
    return CloudError(
        "unsupported table",
        {
            "title": "Unsupported table feature",
            "code": "unsupported_table_feature",
            "feature": ", ".join(features),
            "detail": f"push v0 cannot send this table: {', '.join(features)}. Supported: Iceberg format version 1 "
            "or 2, Parquet data files, unpartitioned, no delete files.",
        },
    )


class Hasher:
    """SHA-256 of a data file's bytes (its `file_id`), streamed in 1 MiB blocks; each file is read once per run."""

    def __init__(self, io):
        self.io, self.cache = io, {}

    def __call__(self, path: str, record_count: int = 0) -> LocalFile:
        if path not in self.cache:
            inp = self.io.new_input(path)
            h, size = hashlib.sha256(), 0
            with inp.open() as stream:
                while block := stream.read(HASH_BLOCK):
                    h.update(block)
                    size += len(block)
            self.cache[path] = LocalFile(h.hexdigest(), path, size, record_count, self.io)
        return self.cache[path]


def snapshot_delta(table, snap, hasher: Hasher) -> tuple[dict, list[LocalFile]]:
    """The data files the snapshot ADDED and DELETED (entries of its own manifests that carry its id), its schema."""
    added: list[LocalFile] = []
    removed: list[str] = []
    for manifest in snap.manifests(table.io):
        if manifest.added_snapshot_id != snap.snapshot_id:
            continue
        for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=False):
            if entry.snapshot_id != snap.snapshot_id:
                continue
            if entry.status == ManifestEntryStatus.ADDED:
                added.append(hasher(entry.data_file.file_path, entry.data_file.record_count))
            elif entry.status == ManifestEntryStatus.DELETED:
                removed.append(hasher(entry.data_file.file_path).file_id)
    schemas = table.schemas()
    schema = schemas.get(snap.schema_id) if snap.schema_id is not None else None
    delta = {
        "snapshot_id": snap.snapshot_id,
        "parent_snapshot_id": snap.parent_snapshot_id,
        "sequence_number": snap.sequence_number or 0,
        "timestamp_ms": snap.timestamp_ms,
        "operation": snap.summary.operation.value,
        "summary": {k: str(v) for k, v in snap.summary.additional_properties.items()},
        "schema": json.loads((schema or table.schema()).model_dump_json()),
        "added_files": [
            {"file_id": f.file_id, "size_bytes": f.size, "record_count": f.record_count, "format": "PARQUET"}
            for f in added
        ],
        "removed_file_ids": sorted(set(removed)),
    }
    return delta, added


def batches(snapshots: list, caps: dict, files_of: Callable[[object], int]) -> list[list]:
    """Split the snapshots so that no session exceeds max_snapshots_per_push or max_files_per_push."""
    max_snaps = caps.get("max_snapshots_per_push") or len(snapshots) or 1
    max_files = caps.get("max_files_per_push")
    out: list[list] = [[]]
    files = 0
    for s in snapshots:
        n = files_of(s)
        if out[-1] and (len(out[-1]) >= max_snaps or (max_files and files + n > max_files)):
            out.append([])
            files = 0
        out[-1].append(s)
        files += n
    return [b for b in out if b]


# ----------------------------------------------------------------------------------------------- push_state cache


def record_push_state(cat: Catalog, table: str, workspace: str, target: str, last: int | None, cloud_id: str | None):
    with closing(sqlite3.connect(cat.db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO push_state (table_name, workspace_id, target, cloud_table_id, last_pushed_snapshot_id, "
            "pushed_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (table_name, workspace_id, target) DO UPDATE SET "
            "cloud_table_id = coalesce(excluded.cloud_table_id, push_state.cloud_table_id), "
            "last_pushed_snapshot_id = excluded.last_pushed_snapshot_id, pushed_at = excluded.pushed_at",
            (table, workspace, target, cloud_id, last, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def push_state(cat: Catalog, table: str | None = None) -> list[dict]:
    with closing(sqlite3.connect(cat.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        q = "SELECT * FROM push_state" + (" WHERE table_name = ?" if table else "") + " ORDER BY table_name, target"
        return [dict(r) for r in conn.execute(q, (table,) if table else ())]


def load_chain(cat: Catalog, table: str, workspace: str, target: str, digest: str) -> str:
    with closing(sqlite3.connect(cat.db_path)) as conn:
        row = conn.execute(
            "SELECT body_digest, previous FROM push_chain WHERE table_name = ? AND workspace_id = ? AND target = ?",
            (table, workspace, target),
        ).fetchone()
    return row[1] if row and row[0] == digest else ""


def save_chain(cat: Catalog, table: str, workspace: str, target: str, digest: str, previous: str) -> None:
    with closing(sqlite3.connect(cat.db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO push_chain (table_name, workspace_id, target, body_digest, previous) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (table_name, workspace_id, target) DO UPDATE SET body_digest = excluded.body_digest, "
            "previous = excluded.previous",
            (table, workspace, target, digest, previous),
        )


def push_path(push_id: object, *rest: str) -> str:
    """/push/v0/pushes/{push_id}[/...]: a server-supplied push_id must be one plain path segment."""
    if not isinstance(push_id, str) or not PUSH_ID.match(push_id) or set(push_id) <= {"."}:
        raise CloudError(f"the receiver sent an invalid push_id {push_id!r}")
    return "/".join(["/push/v0/pushes", quote(push_id, safe=""), *rest])


def file_path(push_id: object, file_id: object) -> str:
    if not isinstance(file_id, str) or not FILE_ID.match(file_id):
        raise CloudError(f"the receiver sent an invalid file_id {file_id!r}")
    return push_path(push_id, "files", file_id)


def _received(value: object, f: LocalFile) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= f.size:
        raise CloudError(f"the receiver reported {value!r} bytes received for a file of {f.size} bytes ({f.file_id})")
    return value


def _check_status(status: dict, f: LocalFile) -> int:
    """The receiver's view of one file must be consistent: within bounds, and complete only when every byte is in."""
    received = _received(status.get("received_bytes"), f)
    state = status.get("state")
    if (state == "complete" and received != f.size) or (state in ("needed", "partial") and received >= f.size):
        raise CloudError(f"the receiver reported file {f.file_id} as {state!r} with {received} of {f.size} bytes")
    return received


# ----------------------------------------------------------------------------------------------- the protocol


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def idempotency_key(device_id: str, body: dict, previous: str = "") -> str:
    """Same device, same body: the same key, so a rerun finds its session. `previous` (an ended session's push_id)
    moves the chain on to a fresh session."""
    digest = hashlib.sha256(_canonical(body)).hexdigest()
    return hashlib.sha256(f"resink-catalog/push/v0\n{device_id}\n{digest}\n{previous}".encode()).hexdigest()


class Pusher:
    def __init__(
        self,
        cat: Catalog,
        device: Device,
        *,
        push_url: str | None = None,
        transport=None,
        confirm: Callable[[dict], bool] = lambda admission: False,
        out: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    ):
        self.cat, self.device, self.confirm, self.out, self.sleep = cat, device, confirm, out, sleep
        self.clock = clock
        self.chunk_bytes = chunk_bytes
        self.api = Api(
            push_url or device.push_url,
            transport=transport,
            token=device.access_token,
            refresh=device.refresh,
            sleep=sleep,
            clock=clock,
        )

    def close(self) -> None:
        self.api.close()

    def _get(self, path: str, **kw) -> dict:
        r = self.api.request("GET", path, **kw)
        if r.status_code != 200:
            raise fail(r)
        return r.json()

    def capabilities(self) -> dict:
        caps = self._get("/push/v0/capabilities")
        if PROTOCOL_VERSION not in caps.get("protocol_versions", []):
            raise CloudError(
                f"the cloud speaks push protocol {caps.get('protocol_versions')}, this client speaks "
                f"{PROTOCOL_VERSION}: upgrade resink-catalog"
            )
        return caps

    def push(self, name: str, *, workspace: str | None = None, target: str | None = None) -> PushReport:
        try:
            ns, tbl = split_name(name)
            tns, tname = split_name(target) if target else (ns, tbl)
        except ValueError as e:
            raise CloudError(str(e)) from None
        local = f"{ns}.{tbl}"
        ws = workspace or self.device.default_workspace()
        report = PushReport(local, f"{tns}.{tname}", ws)
        try:
            table = self.cat.iceberg.load_table((ns, tbl))
        except (NoSuchTableError, NoSuchNamespaceError):
            raise CloudError(f"no such table: {local}") from None
        caps = self.capabilities()
        state = self._get("/push/v0/table-state", params={"workspace_id": ws, "namespace": [tns], "name": tname})
        cloud_last = state.get("last_pushed_source_snapshot_id") if state.get("exists") else None
        todo = new_snapshots(table, cloud_last)
        if not todo:
            if state.get("exists"):
                record_push_state(self.cat, local, ws, report.target, cloud_last, None)
            report.last_pushed_snapshot_id = cloud_last
            return report
        features = unsupported_features(table, todo)
        if features:
            raise unsupported_problem(features)
        hasher = Hasher(table.io)
        base = cloud_last
        for batch in batches(todo, caps, lambda s: int(s.summary.additional_properties.get("added-data-files", 1))):
            deltas, files = [], {}
            for snap in batch:
                delta, added = snapshot_delta(table, snap, hasher)
                deltas.append(delta)
                files.update({f.file_id: f for f in added})
            too_big = [f for f in files.values() if f.size > caps["max_file_bytes"]]
            if too_big:
                raise CloudError(
                    f"{len(too_big)} data file(s) exceed the cloud's limit of {caps['max_file_bytes']} bytes per file "
                    f"(largest {max(f.size for f in too_big)} bytes); rewrite the table with smaller files"
                )
            body = {
                "target_workspace_id": ws,
                "table": {"namespace": [tns], "name": tname},
                "source_table_uuid": str(table.metadata.table_uuid),
                "format_version": table.metadata.format_version,
                "base_snapshot_id": base,
                "snapshots": deltas,
            }
            session = self._open_session(body, report)
            if session is None:  # the receiver says nothing is new
                break
            report.sessions.append(session["push_id"])
            result = self._finish_session(session, files, caps, report)
            base = deltas[-1]["snapshot_id"]
            report.snapshots += len(deltas)
            report.cloud_table_id = result.get("cloud_table_id")
            report.last_pushed_snapshot_id = base
            record_push_state(self.cat, local, ws, report.target, base, report.cloud_table_id)
        return report

    def _open_session(self, body: dict, report: PushReport) -> dict | None:
        digest = hashlib.sha256(_canonical(body)).hexdigest()
        where = (self.cat, report.table, report.workspace_id, report.target)
        previous, aborted = load_chain(*where, digest), False
        for _ in range(SESSION_ATTEMPTS):
            key = idempotency_key(self.device.info["device_id"], body, previous)
            r = self.api.request("POST", "/push/v0/pushes", json=body, headers={"Idempotency-Key": key})
            if r.status_code == 200:
                return None
            if r.status_code == 201:
                s = r.json()
                push_path(s["push_id"])
                if s["state"] in ENDED:
                    previous = s["push_id"]
                    save_chain(*where, digest, previous)  # the next attempt, here or in a rerun, starts after it
                    continue
                return s
            err = fail(r)
            if err.code == "push_in_progress" and err.problem.get("push_id") and not aborted:
                # an unfinished session of this device for the same table, made from an older local state
                other = self.api.request("GET", push_path(err.problem["push_id"]))
                if other.status_code == 200 and other.json()["state"] == "open":
                    d = self.api.request("DELETE", push_path(err.problem["push_id"]))
                    if d.status_code == 204:
                        self.out(f"aborted an unfinished earlier push of this table ({err.problem['push_id']})")
                        aborted = True
                        continue
            raise err
        raise CloudError(
            f"could not open a push session: {SESSION_ATTEMPTS} ended sessions in a row; run push again to continue"
        )

    def _finish_session(self, session: dict, files: dict[str, LocalFile], caps: dict, report: PushReport) -> dict:
        push_id = session["push_id"]
        session = self._get(push_path(push_id))  # the receiver's current byte counts: resume from them
        over = session["admission"]["outcome"] == "over_included"
        if session["state"] == "open":
            if over and not self.confirm(session["admission"]):
                self.api.request("DELETE", push_path(push_id))
                cost = _money(session["admission"].get("estimated_extra_monthly_cost"))
                raise CloudError(
                    "push cancelled: it would exceed your organization's included storage "
                    f"(estimated extra monthly cost {cost}); rerun with --yes to accept"
                )
            chunk = max(caps["chunk_bytes_min"], min(self.chunk_bytes, caps["chunk_bytes_max"]))
            for status in session["files"]:
                f = files.get(status.get("file_id"))
                if f is None:
                    raise CloudError(f"the receiver listed a file this push did not declare: {status.get('file_id')!r}")
                offset = _check_status(status, f)
                if status["state"] in ("needed", "partial"):
                    self._upload(push_id, f, offset, chunk, report)
            r = self.api.request(
                "POST", push_path(push_id, "commit"), json={"confirm_over_included": True} if over else None
            )
            if r.status_code not in (200, 202):
                raise fail(r)
            session = r.json()
        deadline = self.clock() + COMMIT_WAIT_SECONDS
        while session["state"] in ("open", "committing"):
            if self.clock() > deadline:
                raise CloudError(f"the commit of push {push_id} is still running; run push again later to see it end")
            self.sleep(POLL_SECONDS)
            session = self._get(push_path(push_id))
        if session["state"] == "committed":
            return session.get("result") or {}
        problem = session.get("error") or {"title": f"push {session['state']}"}
        through = session.get("committed_through")
        if through is not None:
            record_push_state(self.cat, report.table, report.workspace_id, report.target, through, None)
            problem = {
                **problem,
                "detail": f"{problem.get('detail', '')} Snapshots up to {through} are committed; "
                "run push again to send the rest.".strip(),
            }
        raise CloudError(f"push {session['state']}", problem)

    def _upload(self, push_id: str, f: LocalFile, offset: int, chunk: int, report: PushReport) -> None:
        """Chunks from the receiver's offset. Memory stays at one chunk whatever the file size. Every offset the
        receiver reports is checked (0 <= offset <= size, `complete` only with every byte), and the upload stops if
        MAX_STALLS answers in a row do not move the file forward."""
        path = file_path(push_id, f.file_id)
        restarted, stalls = False, 0
        with f.io.new_input(f.path).open() as stream:
            while offset < f.size:
                stream.seek(offset)
                data = stream.read(min(chunk, f.size - offset))
                headers = {
                    "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{f.size}",
                    "Content-Type": "application/octet-stream",
                }
                r = self.api.request("PATCH", path, content=data, headers=headers)
                before = offset
                if r.status_code == 200:
                    status = r.json()
                    report.bytes_uploaded += len(data)
                    offset = _check_status(status, f)
                    if status["state"] in ("complete", "present"):
                        break
                else:
                    err = fail(r)
                    if err.code == "offset_mismatch" and "received_bytes" in err.problem:
                        offset = _received(err.problem["received_bytes"], f)
                    elif err.code == "checksum_mismatch" and not restarted:
                        restarted, offset = True, 0  # the receiver reset the file; send it once more
                        continue
                    else:
                        raise err
                stalls = stalls + 1 if offset <= before else 0
                if stalls >= MAX_STALLS:
                    raise CloudError(
                        f"the upload of {f.file_id} makes no progress (the receiver stays at {offset} of {f.size} "
                        "bytes); run push again later"
                    )
        report.files_uploaded += 1


def _money(m: dict | None) -> str:
    return f"{m['amount']} {m['currency']}" if m else "unknown"
