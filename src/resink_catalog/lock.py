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

    def downgrade(self) -> None:
        """Exclusive to shared without a gap in which another process could take the lock (POSIX); on Windows the
        exclusive lock is dropped and a shared one taken."""
        if not self.exclusive:
            return
        if os.name == "posix" and self._fd is not None:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_SH)
            self.exclusive = False
            return
        self.release()  # pragma: no cover
        self.exclusive = False  # pragma: no cover
        self._acquire()  # pragma: no cover

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


class blocking_lock:  # noqa: N801 - used as a context manager
    """A blocking exclusive lock on a file, between processes."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._fd: int | None = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.name == "posix":
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows runner only
            import msvcrt

            msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class recovery_guard(blocking_lock):  # noqa: N801 - used as a context manager
    """Serialises crash recovery between processes starting at the same moment."""

    def __init__(self, home: Path):
        super().__init__(Path(home) / ".restore-recovery.lock")
