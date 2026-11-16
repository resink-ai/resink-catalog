"""Windows behaviour that can be checked anywhere. The real thing runs in CI (windows-latest)."""

from pathlib import Path, PureWindowsPath

import pytest

from resink_catalog.readers import ReadError, read_csv
from resink_catalog.store import warehouse_location


def test_the_warehouse_is_a_plain_drive_path_on_windows_and_a_file_uri_elsewhere():
    # PyIceberg reads `file:///C:/x` back as `/C:/x` (WinError 123); a plain `C:\x` it understands.
    assert (
        warehouse_location(PureWindowsPath("C:/Users/me/home/warehouse"), windows=True)
        == "C:\\Users\\me\\home\\warehouse"
    )  # type: ignore[arg-type]
    assert warehouse_location(Path("/home/me/warehouse"), windows=False) == "file:///home/me/warehouse"


def test_pyiceberg_understands_what_the_windows_branch_hands_it(monkeypatch):
    """PyIceberg takes a drive path for a local file (its check uses `os.path.splitdrive`, so it is simulated with
    ntpath here) and a `file:///C:/x` URI for a URL, which is what failed on Windows."""
    import ntpath
    import os

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
