"""The restore journal: a restore that is killed half-way is finished or undone the next time the catalog opens.

`restore` writes `<home>/.restore-journal.json` (atomically, flushed to disk) BEFORE it moves anything. It lists every
rename of the swap, in order: first the old files going into a fresh `.restore-replaced-*` directory, then the staged
files coming in. Its state is `swapping` until the restored catalog has passed its final check, then `committed`;
an undo ends with `rolled_back`.

`recover(home)` runs before any catalog initialisation (Catalog.__init__, `serve`, restore itself):
- `swapping`: undo. Every rename is reversed newest first; a rename is only reversed when its target exists and its
  source does not, so recovery can itself be killed and re-run.
- `rolled_back`: the old catalog is back (and durably so); only the staging directory and the journal are left to
  remove. Recovery of this state never moves a file: the old files are in place, and the journal's moves would be
  misread as forward moves.
- `committed`: the new catalog is in place; only the staging directory and the journal are left to remove.

Durability order (every step is fsynced before the next begins): staged files and directories, then the journal
`swapping`, then each rename together with BOTH of its parent directories, then the marker (`committed` or
`rolled_back`), then the removal of the staging directory, then the removal of the journal. A journal is removed only
after the recovery it describes has completed; if recovery fails it stays, so the next start retries.
On Windows a directory cannot be opened for fsync, so that step is skipped there (NTFS journals metadata itself); file
fsyncs still happen.
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


def _hook(point: str) -> None:
    """Named points in recovery and rollback; tests replace this to kill the process at each of them."""


def fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    """Make a directory's entries (creations, renames, removals) durable. Not possible on Windows: skipped there."""
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(path: Path) -> None:
    """fsync every file and every directory under `path`, and `path`'s own parent entry."""
    for root, _dirs, files in os.walk(path):
        for name in files:
            fsync_file(Path(root) / name)
        fsync_dir(Path(root))
    fsync_dir(Path(path).parent)


def durable_rename(src: Path, dst: Path) -> None:
    """A rename that is on disk when it returns: both parent directories are fsynced (they may be different)."""
    os.rename(src, dst)
    fsync_dir(Path(dst).parent)
    if Path(src).parent != Path(dst).parent:
        fsync_dir(Path(src).parent)


def write_journal(home: Path, data: dict) -> None:
    tmp = home / (JOURNAL + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, home / JOURNAL)
    fsync_dir(home)  # make the rename itself durable


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
    """Reverse the renames, newest first, each durably, skipping any that did not happen or were already reversed."""
    n = 0  # counts the renames actually reversed, for the hook names
    for src, dst in reversed(moves):
        s, d = Path(src), Path(dst)
        if os.path.lexists(d) and not os.path.lexists(s):
            n += 1
            _hook(f"undo-rename-{n}-before")
            durable_rename(d, s)
            _hook(f"undo-rename-{n}-after")


def cleanup(home: Path, journal: dict, keep_replaced: bool) -> None:
    """Remove the staging directory, then the (empty) replaced directory, then the journal, in that order."""
    shutil.rmtree(journal["stage"], ignore_errors=True)
    fsync_dir(home)
    _hook("cleanup-after-stage")
    replaced = Path(journal["replaced"])
    if not keep_replaced:
        try:
            replaced.rmdir()  # only when empty: the old catalog is never deleted
        except OSError:
            pass
        fsync_dir(home)
    _hook("journal-unlink-before")
    try:
        (home / JOURNAL).unlink()
    except FileNotFoundError:
        pass
    fsync_dir(home)


def rollback(home: Path, journal: dict) -> None:
    """Undo a `swapping` journal durably, record `rolled_back`, then clean up. If anything raises, the journal stays."""
    undo(journal["moves"])
    journal = {**journal, "state": "rolled_back"}
    _hook("rolled-back-marker-before")
    write_journal(home, journal)  # after the undo renames are durable; before anything is deleted
    _hook("rolled-back-marker-after")
    cleanup(home, journal, keep_replaced=False)


def recover(home: Path) -> str | None:
    """Finish or undo an interrupted restore. Returns "rolled back", "completed" or None (nothing to do).
    A failure leaves the journal exactly where it was."""
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
            cleanup(home, journal, keep_replaced=True)
            return "completed"
        if journal["state"] == "rolled_back":
            cleanup(home, journal, keep_replaced=False)  # cleanup only: never replay the moves
            return "rolled back"
        rollback(home, journal)
        return "rolled back"
