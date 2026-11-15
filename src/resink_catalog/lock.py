"""One lock per catalog directory, shared by every running catalog process.

Every process that opens a catalog (serve, mcp, the CLI commands, backup) holds the lock *shared* while it has the
catalog open. `restore` takes it *exclusive* and refuses if any other process holds it, so a catalog is never
replaced under a running server.

POSIX: flock on `<home>/.catalog.lock`. Windows: msvcrt byte-range locks, where each sharer locks its own byte in
1..64 and the exclusive holder needs byte 0 plus all of them (untested on Windows until CI runs there).
"""

from __future__ import annotations

import os
from pathlib import Path

LOCK_FILE = ".catalog.lock"
SLOTS = 64


class CatalogInUse(Exception):
    """Another process has this catalog open."""


class CatalogLock:
    def __init__(self, home: Path, exclusive: bool = False):
        self.path = Path(home) / LOCK_FILE
        self.exclusive = exclusive
        self._fd: int | None = None
        self._slot: int | None = None
        self._acquire()

    def _acquire(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "posix":
                import fcntl

                try:
                    fcntl.flock(fd, (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                except OSError as e:
                    raise CatalogInUse(str(self.path)) from e
            else:  # pragma: no cover - exercised on the Windows CI runner only
                import msvcrt

                def lock(byte: int) -> bool:
                    os.lseek(fd, byte, os.SEEK_SET)
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        return True
                    except OSError:
                        return False

                if self.exclusive:
                    got = [b for b in range(0, SLOTS + 1) if lock(b)]
                    if len(got) != SLOTS + 1:
                        for b in got:
                            os.lseek(fd, b, os.SEEK_SET)
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                        raise CatalogInUse(str(self.path))
                else:
                    for b in range(1, SLOTS + 1):
                        if lock(b):
                            self._slot = b
                            break
                    else:
                        raise CatalogInUse(str(self.path))
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing drops the flock / byte-range locks
            self._fd = None

    def __enter__(self) -> CatalogLock:
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass
