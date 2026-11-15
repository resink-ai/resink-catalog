"""Startup housekeeping: remove the leftovers of interrupted imports.

An import writes its data files before the catalog commits them. If the process dies in between, the files stay in
`warehouse/` and nothing references them. `remove_orphans` deletes warehouse files that no snapshot of any table
references and that are older than `min_age_seconds` (default 24 hours): a file that young may belong to an import
another process is running right now. Run at `serve` start and by `resink-catalog cleanup`.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .backup import referenced_files
from .catalog import Catalog

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
