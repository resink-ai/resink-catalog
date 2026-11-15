"""Consistent backup and restore (design section 5).

`backup` takes the catalog's write lock (a SQLite `BEGIN IMMEDIATE`, so imports, REST commits and snapshot expiry
wait), copies `catalog.db` with SQLite's online backup API, then copies exactly the warehouse files the catalog
references: metadata files, manifest lists, manifests and data files of every snapshot. Files nobody references (the
leftovers of an interrupted import) are not copied. The access token and any credentials are never included.

`restore` validates the manifest (relative paths under `warehouse/` only, no symlinks), checks every file against its
checksum while staging a full copy inside the home directory, then swaps the staged files in with renames and moves
the old ones aside. It holds the catalog's exclusive lock (so it refuses while a server or any other process has the
catalog open), and any failure during the swap or the final reference check renames everything back.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
from urllib.request import url2pathname

from .catalog import Catalog
from .home import SCHEMA_VERSION
from .lock import CatalogInUse, CatalogLock

BACKUP_FORMAT = 1
MANIFEST = "manifest.json"
LOCK_WAIT_SECONDS = 60


class BackupError(Exception):
    """A backup or restore that cannot proceed; the message says what to do."""


def _local_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme not in ("", "file"):
        raise BackupError(f"cannot back up {uri}: only local files are supported")
    return Path(url2pathname(parsed.path))  # file:///C:/x on Windows, file:///home/x elsewhere


def referenced_files(cat: Catalog) -> set[Path]:
    """Every file any snapshot of any table needs, as absolute paths (all must be under <home>/warehouse)."""
    files: set[Path] = set()
    for namespace in cat.iceberg.list_namespaces():
        for ident in cat.iceberg.list_tables(namespace):
            table = cat.iceberg.load_table(ident)
            uris = {table.metadata_location, *(m.metadata_file for m in table.metadata.metadata_log)}
            for snap in table.metadata.snapshots:
                uris.add(snap.manifest_list)
                for manifest in snap.manifests(table.io):
                    uris.add(manifest.manifest_path)
                    for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=False):
                        uris.add(entry.data_file.file_path)
            files |= {_local_path(u) for u in uris}
    warehouse = (cat.home / "warehouse").resolve()
    for f in files:
        if warehouse not in f.resolve().parents:
            raise BackupError(f"{f} is outside {warehouse}; backup copies only the catalog's own warehouse")
    return files


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def backup(home: Path | str, dest: Path | str) -> dict:
    """Copy the catalog at `home` into the new or empty directory `dest`. Returns the manifest."""
    home, dest = Path(home).resolve(), Path(dest).resolve()
    if dest.exists() and any(dest.iterdir()):
        raise BackupError(f"{dest} is not empty; choose a new directory")
    if dest == home or home in dest.parents:
        raise BackupError("the backup directory must be outside the catalog's own directory")
    cat = Catalog(home)  # shared lock: a restore cannot start while we copy
    dest.mkdir(parents=True, exist_ok=True)
    lock = sqlite3.connect(cat.db_path, timeout=LOCK_WAIT_SECONDS, isolation_level=None)
    try:
        try:
            lock.execute("BEGIN IMMEDIATE")  # the write lock: other writers wait until we roll back
        except sqlite3.OperationalError as e:
            raise BackupError(f"could not get the write lock within {LOCK_WAIT_SECONDS}s: {e}") from e
        with closing(sqlite3.connect(cat.db_path)) as src, closing(sqlite3.connect(dest / "catalog.db")) as out:
            src.backup(out)  # SQLite's online backup API, a consistent copy
        entries = []
        warehouse = home / "warehouse"
        for f in sorted(referenced_files(cat)):
            rel = f.resolve().relative_to(warehouse.resolve())
            target = dest / "warehouse" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            entries.append({"path": f"warehouse/{rel.as_posix()}", "size": target.stat().st_size})
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)  # never leave a backup that looks complete
        raise
    finally:
        lock.rollback() if lock.in_transaction else None
        lock.close()
        cat.close()
    for e in entries:
        e["sha256"] = _sha256(dest / e["path"])
    manifest = {
        "format": BACKUP_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "home": str(home),
        "catalog_db_sha256": _sha256(dest / "catalog.db"),
        "files": entries,
    }
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2))  # last: its presence marks a complete backup
    return manifest


def _safe_relpath(rel: object) -> PurePosixPath:
    """A manifest path must be a plain relative POSIX path of at least two parts under `warehouse/`."""
    if not isinstance(rel, str) or not rel or "\\" in rel or "\0" in rel or ":" in rel.split("/")[0]:
        raise BackupError(f"unsafe path in the backup manifest: {rel!r}")
    p = PurePosixPath(rel)
    if p.is_absolute() or len(p.parts) < 2 or p.parts[0] != "warehouse" or any(x in ("", ".", "..") for x in p.parts):
        raise BackupError(f"unsafe path in the backup manifest: {rel!r} (only files under warehouse/ are allowed)")
    return p


def verify_backup(src: Path | str) -> dict:
    """Check a backup directory: manifest present, paths safe and unique, every file there and matching its checksum.
    Nothing is read from outside the backup directory and symlinks are refused."""
    src = Path(src)
    root = src.resolve()
    try:
        manifest = json.loads((src / MANIFEST).read_text())
    except (OSError, ValueError) as e:
        raise BackupError(f"{src} is not a complete backup (no readable {MANIFEST}): {e}") from e
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise BackupError(f"unknown backup format {manifest.get('format') if isinstance(manifest, dict) else None!r}")
    try:
        schema, home_rec, db_sha, files = (
            manifest["schema_version"],
            manifest["home"],
            manifest["catalog_db_sha256"],
            manifest["files"],
        )
        if not (isinstance(schema, int) and isinstance(home_rec, str) and isinstance(db_sha, str)):
            raise TypeError
        if not isinstance(files, list):
            raise TypeError
    except (KeyError, TypeError) as e:
        raise BackupError(f"the backup manifest is malformed ({type(e).__name__})") from e
    if schema > SCHEMA_VERSION:
        raise BackupError(f"backup has schema version {schema}; upgrade resink-catalog first")
    db = src / "catalog.db"
    if db.is_symlink() or not db.is_file() or _sha256(db) != db_sha:
        raise BackupError("catalog.db in the backup is missing or does not match its checksum")
    seen: set[str] = set()
    for e in files:
        if not isinstance(e, dict) or not isinstance(e.get("size"), int) or not isinstance(e.get("sha256"), str):
            raise BackupError(f"malformed file entry in the backup manifest: {e!r}")
        rel = _safe_relpath(e.get("path"))
        if rel.as_posix() in seen:
            raise BackupError(f"{rel} is listed twice in the backup manifest")
        seen.add(rel.as_posix())
        f = src / rel
        cur = src
        for part in rel.parts:  # no component of the path may be a symbolic link
            cur = cur / part
            if cur.is_symlink():
                raise BackupError(f"{rel} contains a symbolic link; backups made by this program contain none")
        if root not in f.resolve().parents:
            raise BackupError(f"{rel} resolves outside the backup directory")
        if not f.is_file():
            raise BackupError(f"backup is missing {rel}")
        if f.stat().st_size != e["size"] or _sha256(f) != e["sha256"]:
            raise BackupError(f"{rel} does not match its checksum; the backup is damaged")
    return manifest


def _rename(a: Path, b: Path) -> None:
    """The one place the swap renames; tests make it fail part-way."""
    a.rename(b)


def _stage(src: Path, stage: Path, manifest: dict) -> None:
    """Copy and re-check everything into `stage` (inside home, so the final renames stay on one filesystem)."""
    stage_root = stage.resolve()
    for e in manifest["files"]:
        rel = _safe_relpath(e["path"])
        target = stage / rel
        if stage_root / "warehouse" not in target.resolve().parents:
            raise BackupError(f"{rel} would be written outside the restore directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, target)
        if _sha256(target) != e["sha256"]:  # the backup changed between verify and copy
            raise BackupError(f"{rel} does not match its checksum; the backup is damaged")
    (stage / "warehouse").mkdir(exist_ok=True)
    shutil.copy2(src / "catalog.db", stage / "catalog.db")
    with closing(sqlite3.connect(stage / "catalog.db")) as c:
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("the restored catalog.db failed SQLite's integrity check")
        try:
            current = {r[0] for r in c.execute("SELECT metadata_location FROM iceberg_tables")}
        except sqlite3.OperationalError:
            current = set()  # a catalog without tables yet
    listed = {_local_path(u).as_posix() for u in current}
    have = {(Path(manifest["home"]) / e["path"]).as_posix() for e in manifest["files"]}
    missing = sorted(listed - have)
    if missing:
        raise BackupError(f"the backup does not contain {len(missing)} table metadata file(s), e.g. {missing[0]}")


def restore(src: Path | str, home: Path | str, replace: bool = False) -> dict:
    """Restore the backup `src` into `home`. Refuses a home that already has a catalog unless `replace` is set, in which
    case the old catalog.db (with its -wal/-shm/-journal files) and warehouse are moved aside, never deleted. Refuses
    while any process has the catalog open. All-or-nothing: on any failure the previous state is put back."""
    src, home = Path(src).resolve(), Path(home).resolve()
    manifest = verify_backup(src)
    if Path(manifest["home"]) != home:
        raise BackupError(
            f"this backup was made from {manifest['home']}. Catalog files record absolute paths, so it can only be "
            f"restored to that same location (use --home {manifest['home']})."
        )
    home.mkdir(parents=True, exist_ok=True)
    if (home / "catalog.db").exists() and not replace:
        raise BackupError(f"{home} already has a catalog; pass --replace to move it aside and restore")
    try:
        lock = CatalogLock(home, exclusive=True)
    except CatalogInUse:
        raise BackupError(
            f"the catalog at {home} is open in another process (a running `serve` or `mcp`, or a command); "
            "stop it and run restore again"
        ) from None
    sidecars = [home / f"catalog.db{x}" for x in ("-wal", "-shm", "-journal")]  # leftovers: nobody has it open
    existing = [p for p in (home / "catalog.db", *sidecars, home / "warehouse") if p.exists()]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stage = home / f".restore-{stamp}"
    done: list[tuple[Path, Path]] = []
    try:
        _stage(src, stage, manifest)  # nothing in `home` has changed yet
        for p in existing:
            aside = p.with_name(f"{p.name}.replaced-{stamp}")
            _rename(p, aside)
            done.append((p, aside))
        for name in ("warehouse", "catalog.db"):
            _rename(stage / name, home / name)
            done.append((stage / name, home / name))
        with closing(Catalog(home, lock=False)) as cat:  # we hold the exclusive lock ourselves
            missing = [str(f) for f in referenced_files(cat) if not f.is_file()]
        if missing:
            raise BackupError(f"restored catalog references {len(missing)} missing file(s), for example {missing[0]}")
    except BaseException:
        for a, b in reversed(done):  # put the previous state back, newest rename first
            try:
                _rename(b, a)
            except OSError:  # nothing more can be done; the moved-aside files are still there
                pass
        shutil.rmtree(stage, ignore_errors=True)
        raise
    finally:
        lock.release()
    shutil.rmtree(stage, ignore_errors=True)
    return manifest
