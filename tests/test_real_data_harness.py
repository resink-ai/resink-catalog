"""The real-data checks, proven on a small synthetic SQLite database: they pass on a faithful import and fail when
a value is wrong, so a passing real-data run means something."""

import sqlite3

import pytest
from real_data import STYLES, export, load, pick, verify

from resink_catalog import Catalog


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "shop.sqlite"
    con = sqlite3.connect(path)
    con.execute("create table orders (id integer, customer text, amount real, note text, qty integer)")
    con.executemany(
        "insert into orders values (?, ?, ?, ?, ?)",
        [
            (i, f"Zoë {i % 7}" if i % 2 else f"Müller {i % 5}", i * 1.37, None if i % 3 else "x", i * 1000)
            for i in range(1, 201)
        ],
    )
    con.execute("create table tiny (a integer)")
    con.execute("insert into tiny values (1)")
    con.commit()
    con.close()
    return path


@pytest.mark.parametrize("style", sorted(set(STYLES)))
def test_a_faithful_import_passes_every_check(tmp_path, db, style):
    src = load(db, "orders")
    out = verify(
        Catalog(tmp_path / "home"),
        src,
        style,
        export(src, tmp_path / "files", style) if (tmp_path / "files").mkdir() is None else None,
    )
    assert out.ok, out.problems


def test_a_wrong_number_is_caught(tmp_path, db):
    src = load(db, "orders")
    (tmp_path / "files").mkdir()
    path = export(src, tmp_path / "files", "csv")
    text = path.read_text().replace(",137.0,", ",138.0,", 1) if ",137.0," in path.read_text() else path.read_text()
    lines = text.splitlines()
    lines[5] = lines[5].rsplit(",", 1)[0] + ",999999"  # corrupt the last column of one row
    path.write_text("\n".join(lines) + "\n")
    out = verify(Catalog(tmp_path / "home"), src, "csv", path)
    assert not out.ok and any("sum" in p for p in out.problems), out.problems


def test_a_lost_distinct_value_is_caught(tmp_path, db):
    src = load(db, "orders")
    (tmp_path / "files").mkdir()
    path = export(src, tmp_path / "files", "csv")
    path.write_text(path.read_text().replace("Zoë 3", "Zoë 1"))  # two customers merge into one
    out = verify(Catalog(tmp_path / "home"), src, "csv", path)
    assert not out.ok and any("distinct" in p for p in out.problems), out.problems


def test_a_missing_row_is_caught(tmp_path, db):
    src = load(db, "orders")
    (tmp_path / "files").mkdir()
    path = export(src, tmp_path / "files", "csv")
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:-1]))
    out = verify(Catalog(tmp_path / "home"), src, "csv", path)
    assert not out.ok and any("rows" in p for p in out.problems)


def test_pick_takes_the_two_largest_tables(db):
    assert [t for _, t in pick([db])] == ["orders", "tiny"]
