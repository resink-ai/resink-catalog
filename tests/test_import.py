"""L1 acceptance 1 and 2: one CSV becomes an Iceberg table; re-import is a no-op unless content changed."""

import pandas as pd
import pytest
from pyiceberg.table import StaticTable  # noqa: F401  (documents that tables are plain Iceberg)


def _csv(path, rows, head="id,name,amount"):
    path.write_text(head + "\n" + "\n".join(rows) + "\n")
    return path


def test_csv_becomes_an_iceberg_table(tmp_path, catalog):
    f = _csv(tmp_path / "sales_2026_10.csv", ["1,Ann,10.5", "2,Bo,20.0"])
    r = catalog.import_file(f, "sales.monthly")
    assert (r.outcome, r.rows) == ("created", 2)
    t = catalog.iceberg.load_table(("sales", "monthly"))
    assert [f.name for f in t.schema().fields] == ["id", "name", "amount"]
    assert str(t.schema().find_field("id").field_type) == "long"
    assert str(t.schema().find_field("amount").field_type) == "double"
    assert catalog.read_table("sales.monthly")["name"].tolist() == ["Ann", "Bo"]
    # push compatibility: unpartitioned, Parquet data files, no delete files (push protocol v0)
    assert t.spec().is_unpartitioned()
    files = [task for task in t.scan().plan_files()]
    assert files and all(task.file.file_format.name == "PARQUET" for task in files)
    assert all(not task.delete_files for task in files)


def test_default_table_name_comes_from_the_file(tmp_path, catalog):
    catalog.import_file(_csv(tmp_path / "My Export (2).csv", ["1,a,1"]))
    assert [t["name"] for t in catalog.list_tables()] == ["main.my_export_2"]


def test_reimport_unchanged_creates_no_snapshot(tmp_path, catalog):
    f = _csv(tmp_path / "a.csv", ["1,x,1"])
    catalog.import_file(f, "t")
    before = catalog.table_info("t")["snapshots"]
    r = catalog.import_file(f)
    assert r.outcome == "unchanged"
    assert catalog.table_info("t")["snapshots"] == before


def test_touching_a_file_without_changing_content_is_not_a_change(tmp_path, catalog):
    f = _csv(tmp_path / "a.csv", ["1,x,1"])
    catalog.import_file(f, "t")
    f.write_bytes(f.read_bytes())  # new mtime, same bytes
    assert catalog.import_file(f).outcome == "unchanged"


def test_changed_content_makes_a_new_snapshot_and_history_stays_queryable(tmp_path, catalog):
    f = _csv(tmp_path / "a.csv", ["1,x,1", "2,y,2"])
    catalog.import_file(f, "t")
    first = catalog.iceberg.load_table(("main", "t")).current_snapshot().snapshot_id
    _csv(f, ["1,x,1", "2,y,2", "3,z,3"])
    r = catalog.import_file(f)
    assert (r.outcome, r.rows) == ("updated", 3)
    table = catalog.iceberg.load_table(("main", "t"))
    assert table.current_snapshot().snapshot_id != first
    assert len(table.scan().to_arrow()) == 3
    old = table.scan(snapshot_id=first).to_arrow()  # time travel to the first import
    assert old.num_rows == 2 and sorted(old.column("id").to_pylist()) == [1, 2]
    assert all(s.summary.operation.value in ("append", "delete", "overwrite") for s in table.metadata.snapshots)


def test_changed_file_with_a_new_column_evolves_the_schema(tmp_path, catalog):
    f = _csv(tmp_path / "a.csv", ["1,x"], head="id,name")
    catalog.import_file(f, "t")
    _csv(f, ["1,x,5"], head="id,name,score")
    assert catalog.import_file(f).outcome == "updated"
    df = catalog.read_table("t")
    assert list(df.columns) == ["id", "name", "score"] and df["score"].tolist() == [5]


def test_unreadable_file_is_reported_on_the_source(tmp_path, catalog):
    bad = tmp_path / "bad.csv"
    bad.write_bytes(bytes(range(256)) * 8)
    r = catalog.import_file(bad)
    assert r.outcome == "error" and r.error
    [src] = catalog.sources()
    assert src["status"] == "error" and src["error"]
    assert catalog.list_tables() == []


def test_parquet_schema_is_read_directly(tmp_path, catalog):
    p = tmp_path / "x.parquet"
    pd.DataFrame({"id": [1, 2], "ts": pd.to_datetime(["2026-01-01", "2026-01-02"])}).to_parquet(p)
    assert catalog.import_file(p, "pq").outcome == "created"
    assert catalog.read_table("pq")["id"].tolist() == [1, 2]


def test_unsupported_file_type_is_refused(tmp_path, catalog):
    from resink_catalog.catalog import ImportErrorForUser

    f = tmp_path / "sheet.xlsx"
    f.write_bytes(b"PK")
    with pytest.raises(ImportErrorForUser):
        catalog.import_file(f)


def test_scan_finds_changed_missing_and_new_files(tmp_path, catalog):
    folder = tmp_path / "exports"
    folder.mkdir()
    a, b = _csv(folder / "a.csv", ["1,x,1"]), _csv(folder / "b.csv", ["1,y,1"])
    catalog.add_folder(folder)
    catalog.import_folder(folder)
    _csv(a, ["1,x,1", "2,x,2"])
    b.unlink()
    _csv(folder / "c.csv", ["9,n,9"])
    report = catalog.scan()
    assert [p.rsplit("/", 1)[-1] for p in report["changed"]] == ["a.csv"]
    assert [p.rsplit("/", 1)[-1] for p in report["missing"]] == ["b.csv"]
    assert [p.rsplit("/", 1)[-1] for p in report["new"]] == ["c.csv"]
    applied = catalog.scan(apply=True)
    assert applied["imported"][0]["outcome"] == "updated"
    assert len(catalog.read_table("exports.a")) == 2


def test_repeated_exports_share_a_table_and_reimport_replaces_only_that_file(tmp_path, catalog):
    folder = tmp_path / "d"
    folder.mkdir()
    for day in (1, 2, 3):
        _csv(folder / f"report_2026-10-0{day}.csv", [f"{day}0,x,1", f"{day}1,y,2"])
    catalog.add_folder(folder)
    catalog.import_folder(folder)
    df = catalog.read_table("d.report")
    assert len(df) == 6 and set(df["_source_file"]) == {f"report_2026-10-0{d}.csv" for d in (1, 2, 3)}
    _csv(folder / "report_2026-10-02.csv", ["20,x,1", "21,y,2", "22,z,3"])
    catalog.scan(apply=True)
    df = catalog.read_table("d.report")
    assert len(df) == 7
    assert (df["_source_file"] == "report_2026-10-02.csv").sum() == 3
