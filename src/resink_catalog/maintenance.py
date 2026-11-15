"""Housekeeping: remove the leftovers of interrupted imports.

An import writes its data files before the catalog commits them. If the process dies in between, the files stay in
`warehouse/` and nothing references them.

Safety does not come from file age. `cleanup` takes the catalog directory's EXCLUSIVE lock, the one `restore` takes:
every process that can write (serve, mcp, an import, a REST client's server) holds it shared while it has the catalog
open, so while we hold it exclusively nothing can be creating files, and an unreferenced file really is a leftover,
whatever its age (`min_age_seconds=0` is therefore safe). If any process has the catalog open, cleanup refuses.
`min_age_seconds` (default 24 h) is kept only for the one case no lock can cover: a REST client that wrote data files
before the server stopped and commits them after it restarts."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .backup import referenced_files
from .catalog import Catalog
from .lock import CatalogInUse, CatalogLock
from .restore_journal import recover

log = logging.getLogger(__name__)
DEFAULT_MIN_AGE_SECONDS = 24 * 3600


def remove_orphans(cat: Catalog, min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS, dry_run: bool = False) -> list[Path]:
    """Delete (or with dry_run only list) the unreferenced old files under <home>/warehouse; returns them."""
    warehouse = (cat.home / "warehouse").resolve()
    keep = {f.resolve() for f in referenced_files(cat)}  # raises before deleting anything if the catalog is odd
    cutoff = time.time() - min_age_seconds
    orphans = []
    for f in sorted(warehouse.rglob("*")):
        if f.is_file() and not f.is_symlink() and f.resolve() not in keep and f.stat().st_mtime <= cutoff:
            orphans.append(f)
    if not dry_run:
        for f in orphans:
            f.unlink()
        for d in sorted((p for p in warehouse.rglob("*") if p.is_dir()), reverse=True):
            if not any(d.iterdir()):  # directories the removal emptied (never the warehouse itself)
                d.rmdir()
    if orphans:
        log.info("%s %d unreferenced file(s) from interrupted imports", "found" if dry_run else "removed", len(orphans))
    return orphans


class CleanupRefused(Exception):
    """Some process has the catalog open, so cleanup cannot know that no import is running."""


def cleanup(home: Path | str, min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS, dry_run: bool = False) -> list[Path]:
    home = Path(home)
    try:
        lock = CatalogLock(home, exclusive=True)
    except CatalogInUse:
        raise CleanupRefused(
            f"the catalog at {home} is open in another process (a running `serve` or `mcp`, or an import); "
            "stop it and run cleanup again"
        ) from None
    try:
        recover(home)
        with Catalog(home, lock=False) as cat:
            return remove_orphans(cat, min_age_seconds, dry_run)
    finally:
        lock.release()
