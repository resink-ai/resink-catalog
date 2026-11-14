"""Consistent backup and restore (design section 5).

`backup` takes the catalog's write lock (a SQLite `BEGIN IMMEDIATE`, so imports, REST commits and snapshot expiry
wait), copies `catalog.db` with SQLite's online backup API, then copies exactly the warehouse files the catalog
references: metadata files, manifest lists, manifests and data files of every snapshot. Files nobody references (the
leftovers of an interrupted import) are not copied. The access token and any credentials are never included.

`restore` checks every file against the recorded checksum, copies the files first and `catalog.db` last, and then
verifies that every file the restored catalog references exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from .catalog import Catalog
from .home import SCHEMA_VERSION

BACKUP_FORMAT = 1
MANIFEST = "manifest.json"
LOCK_WAIT_SECONDS = 60


class BackupError(Exception):
    """A backup or restore that cannot proceed; the message says what to do."""


def _local_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme not in ("", "file"):
        raise BackupError(f"cannot back up {uri}: only local files are supported")
    return Path(unquote(parsed.path))


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
    cat = Catalog(home)
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
    for e in entries:
        e["sha256"] = _sha256(dest / e["path"])
    manifest = {
        "format": BACKUP_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "home": str(home),
        "catalog_db_sha256": _sha256(dest / "catalog.db"),
        "files": entries,
    }
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2))  # last: its presence marks a complete backup
    return manifest


def verify_backup(src: Path | str) -> dict:
    """Check a backup directory: manifest present, every file there and matching its checksum."""
    src = Path(src)
    try:
        manifest = json.loads((src / MANIFEST).read_text())
    except (OSError, ValueError) as e:
        raise BackupError(f"{src} is not a complete backup (no readable {MANIFEST}): {e}") from e
    if manifest.get("format") != BACKUP_FORMAT:
        raise BackupError(f"unknown backup format {manifest.get('format')!r}")
    if manifest["schema_version"] > SCHEMA_VERSION:
        raise BackupError(f"backup has schema version {manifest['schema_version']}; upgrade resink-catalog first")
    if not (src / "catalog.db").is_file() or _sha256(src / "catalog.db") != manifest["catalog_db_sha256"]:
        raise BackupError("catalog.db in the backup is missing or does not match its checksum")
    for e in manifest["files"]:
        f = src / e["path"]
        if not f.is_file():
            raise BackupError(f"backup is missing {e['path']}")
        if f.stat().st_size != e["size"] or _sha256(f) != e["sha256"]:
            raise BackupError(f"{e['path']} does not match its checksum; the backup is damaged")
    return manifest


def restore(src: Path | str, home: Path | str, replace: bool = False) -> dict:
    """Restore the backup `src` into `home`. Refuses a home that already has a catalog unless `replace` is set, in which
    case the old catalog.db and warehouse are moved aside (never deleted)."""
    src, home = Path(src).resolve(), Path(home).resolve()
    manifest = verify_backup(src)
    if Path(manifest["home"]) != home:
        raise BackupError(
            f"this backup was made from {manifest['home']}. Catalog files record absolute paths, so it can only be "
            f"restored to that same location (use --home {manifest['home']})."
        )
    home.mkdir(parents=True, exist_ok=True)
    existing = [p for p in (home / "catalog.db", home / "warehouse") if p.exists()]
    if existing and not replace:
        raise BackupError(f"{home} already has a catalog; pass --replace to move it aside and restore")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for p in existing:
        p.rename(p.with_name(f"{p.name}.replaced-{stamp}"))
    for e in manifest[
        "files"
    ]:  # files first: a catalog.db without its files is the bad state, files alone are harmless
        target = home / e["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / e["path"], target)
    tmp = home / "catalog.db.restoring"
    shutil.copy2(src / "catalog.db", tmp)
    with closing(sqlite3.connect(tmp)) as c:
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            tmp.unlink()
            raise BackupError("the restored catalog.db failed SQLite's integrity check")
    os.replace(tmp, home / "catalog.db")
    missing = [str(f) for f in referenced_files(Catalog(home)) if not f.is_file()]
    if missing:
        raise BackupError(f"restored catalog references {len(missing)} missing file(s), for example {missing[0]}")
    return manifest
