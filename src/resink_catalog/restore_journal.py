"""The restore journal: a restore that is killed half-way is finished or undone the next time the catalog opens.

`restore` writes `<home>/.restore-journal.json` (atomically, flushed to disk) BEFORE it moves anything. It lists every
rename of the swap, in order: first the old files going into a fresh `.restore-replaced-*` directory, then the staged
files coming in. Its state is `swapping` until the restored catalog has passed its final check, then `committed`.

`recover(home)` runs before any catalog initialisation (Catalog.__init__, `serve`, restore itself):
- `swapping`: undo. Every rename is reversed newest first; a rename is only reversed when its target exists and its
  source does not, so recovery can itself be killed and re-run.
- `committed`: the new catalog is in place; only the staging directory and the journal are left to remove.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .lock import recovery_guard

JOURNAL = ".restore-journal.json"


class RestoreRecoveryError(Exception):
    """The journal cannot be read or names paths outside the catalog directory; nothing was touched."""


def write_journal(home: Path, data: dict) -> None:
    tmp = home / (JOURNAL + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, home / JOURNAL)
    if os.name == "posix":  # make the rename itself durable
        fd = os.open(home, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def read_journal(home: Path) -> dict | None:
    try:
        return json.loads((home / JOURNAL).read_text())
    except FileNotFoundError:
        return None
    except ValueError as e:
        raise RestoreRecoveryError(f"{home / JOURNAL} is unreadable ({e}); fix or remove it by hand") from e


def _inside(home: Path, p: str) -> Path:
    path = Path(p)
    if home.resolve() not in path.resolve().parents:
        raise RestoreRecoveryError(f"the restore journal names {p}, which is outside {home}")
    return path


def undo(moves: list[list[str]]) -> None:
    """Reverse the renames, newest first, skipping any that did not happen (or were already reversed)."""
    for src, dst in reversed(moves):
        s, d = Path(src), Path(dst)
        if os.path.lexists(d) and not os.path.lexists(s):
            os.rename(d, s)


def finish(home: Path, journal: dict, keep_replaced: bool) -> None:
    shutil.rmtree(journal["stage"], ignore_errors=True)
    replaced = Path(journal["replaced"])
    if not keep_replaced:
        try:
            replaced.rmdir()  # only when empty: the old catalog is never deleted
        except OSError:
            pass
    try:
        (home / JOURNAL).unlink()
    except FileNotFoundError:
        pass


def recover(home: Path) -> str | None:
    """Finish or undo an interrupted restore. Returns "rolled back", "completed" or None (nothing to do)."""
    home = Path(home)
    if not (home / JOURNAL).exists():
        return None
    with recovery_guard(home):  # several processes may start at once; one recovers, the others find it done
        journal = read_journal(home)
        if journal is None:
            return None
        for key in ("stage", "replaced"):
            _inside(home, journal[key])
        for src, dst in journal["moves"]:
            _inside(home, src)
            _inside(home, dst)
        if journal["state"] == "committed":
            finish(home, journal, keep_replaced=True)
            return "completed"
        undo(journal["moves"])
        finish(home, journal, keep_replaced=False)
        return "rolled back"
