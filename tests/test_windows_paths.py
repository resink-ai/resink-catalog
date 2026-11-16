"""Windows behaviour that can be checked anywhere. The real thing runs in CI (windows-latest)."""

import os
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from resink_catalog.readers import ReadError, read_csv
from resink_catalog.store import warehouse_location


def test_the_warehouse_is_a_plain_drive_path_on_windows_and_a_file_uri_elsewhere():
    # PyIceberg reads `file:///C:/x` back as `/C:/x` (WinError 123); a plain `C:\x` it understands.
    assert (
        warehouse_location(PureWindowsPath("C:/Users/me/home/warehouse"), windows=True)
        == "C:\\Users\\me\\home\\warehouse"
    )  # type: ignore[arg-type]
    assert (
        warehouse_location(PurePosixPath("/home/me/warehouse"), windows=False)  # type: ignore[arg-type]
        == "file:///home/me/warehouse"
    )


def test_pyiceberg_understands_what_the_windows_branch_hands_it(monkeypatch):
    """PyIceberg takes a drive path for a local file (its check uses `os.path.splitdrive`, so it is simulated with
    ntpath here) and a `file:///C:/x` URI for a URL, which is what failed on Windows."""
    import ntpath

    from pyiceberg.io import _is_local_path

    monkeypatch.setattr(os.path, "splitdrive", ntpath.splitdrive)
    assert _is_local_path("C:\\Users\\me\\home\\warehouse")
    assert not _is_local_path("file:///C:/Users/me/home/warehouse")


@pytest.mark.parametrize("eol", [b"\r\n", b"\n", b"\r\r\n", b"\r\r\r\n"])
@pytest.mark.parametrize("rows", [b"", b"1,a,2.5" + b"\n"])
def test_a_csv_reads_the_same_whatever_the_line_endings(tmp_path, eol, rows):
    """Header-only files written through a text-mode newline translation on Windows end in `\\r\\r\\n`; the sniffer
    failed on them there. Every ending now gives the same columns and rows on every OS."""
    f = tmp_path / "t.csv"
    f.write_bytes(b"id,name,amount" + eol + (rows.replace(b"\n", eol) if rows else b""))
    t = read_csv(f)
    assert t.schema.names == ["id", "name", "amount"]
    assert t.num_rows == (1 if rows else 0)


def test_an_empty_or_blank_csv_is_still_refused(tmp_path):
    f = tmp_path / "e.csv"
    f.write_bytes(b"\r\r\n")
    with pytest.raises(ReadError):
        read_csv(f)


# ------------------------------------------------------------------------- durability calls on Windows (simulated)


def _fake_io(rj, monkeypatch):
    """Replace descriptor opening, closing and flushing, so the simulated platform never touches a real file or
    directory (a real Windows runner refuses to open a directory)."""
    log = []
    monkeypatch.setattr(rj.os, "open", lambda path, flags, *a, **k: (log.append(("open", str(path), flags)), 99)[1])
    monkeypatch.setattr(rj.os, "fsync", lambda fd: log.append(("fsync", fd)))
    monkeypatch.setattr(rj.os, "close", lambda fd: log.append(("close", fd)))
    return log


def test_windows_opens_files_for_writing_and_never_opens_a_directory(tmp_path, monkeypatch):
    from resink_catalog import restore_journal as rj

    log = _fake_io(rj, monkeypatch)
    monkeypatch.setattr(rj, "_POSIX", False)
    rj.fsync_file(tmp_path / "f")
    rj.fsync_dir(tmp_path)
    assert [e[0] for e in log] == ["open", "fsync", "close"]  # nothing for the directory
    assert log[0][2] & rj.os.O_RDWR


def test_posix_opens_files_read_only_and_flushes_directories(tmp_path, monkeypatch):
    from resink_catalog import restore_journal as rj

    log = _fake_io(rj, monkeypatch)
    monkeypatch.setattr(rj, "_POSIX", True)
    rj.fsync_file(tmp_path / "f")
    rj.fsync_dir(tmp_path)
    opens = [e for e in log if e[0] == "open"]
    assert [e[1] for e in opens] == [str(tmp_path / "f"), str(tmp_path)]
    assert not opens[0][2] & rj.os.O_RDWR
    assert [e[0] for e in log].count("fsync") == 2


def test_a_file_that_cannot_be_opened_for_flushing_is_an_error_not_a_skip(tmp_path, monkeypatch):
    from resink_catalog import restore_journal as rj

    log = _fake_io(rj, monkeypatch)

    def denied(path, flags, *a, **k):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(rj.os, "open", denied)
    monkeypatch.setattr(rj, "_POSIX", False)
    with pytest.raises(PermissionError):
        rj.fsync_file(tmp_path / "ro.bin")
    assert log == []  # nothing was flushed, and the failure was not hidden


def test_a_crlf_inside_a_quoted_value_is_kept_exactly(tmp_path):
    """The `\\r\\r\\n` repair applies only to a file that could not be read; a readable file keeps its values."""
    f = tmp_path / "q.csv"
    f.write_bytes(b'id,note\r\n1,"line one\r\r\nline two"\r\n2,plain\r\n')
    t = read_csv(f)
    assert t.column("note").to_pylist() == ["line one\r\r\nline two", "plain"]
