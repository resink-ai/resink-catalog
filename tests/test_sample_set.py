"""L1 acceptance 5: a folder of 50 export files is detected, proposed, and imported after confirmation.

Prints the import success rate. The set is synthetic (see samples.py), so the rate is an upper bound for real files.
"""

import json

import pytest
from samples import generate

from resink_catalog import Catalog

# Per-file result classes recorded for the report.
EXPECTED_FAILURES: dict[str, str] = {}


@pytest.fixture
def folder(tmp_path):
    out = tmp_path / "exports"
    return out, generate(out)


def _check(cat: Catalog, folder, plan):
    """Compare what landed in the catalog with the ground truth. Returns {file: failure reason or None}."""
    sources = {s["path"]: s for s in cat.sources()}
    results: dict[str, str | None] = {}
    frames: dict[str, object] = {}
    for e in plan:
        src = sources.get(str((folder / e.file).resolve()))
        if src is None:
            results[e.file] = "file not detected"
            continue
        if src["status"] != "imported":
            results[e.file] = f"status {src['status']}: {src['error']}"
            continue
        table = src["target_table"]
        df = frames.setdefault(table, cat.read_table(table))
        if e.group:
            df = df[df["_source_file"] == e.file]
            cols = [c for c in df.columns if c != "_source_file"]
        else:
            cols = list(df.columns)
        if cols != e.columns:
            results[e.file] = f"columns {cols} != {e.columns}"
        elif len(df) != e.rows:
            results[e.file] = f"rows {len(df)} != {e.rows}"
        elif e.ids and "id" in df.columns and sorted(df["id"].astype(int).tolist()) != sorted(e.ids):
            results[e.file] = "ids differ"
        elif e.names and "name" in df.columns and sorted(df["name"].astype(str).tolist()) != sorted(e.names):
            results[e.file] = "name column differs (wrong encoding or quoting?)"
        else:
            results[e.file] = None
    return results


def test_add_folder_proposes_without_importing(folder, catalog):
    out, plan = folder
    proposed = catalog.add_folder(out)
    assert len(proposed) == 50
    assert {s["status"] for s in proposed} == {"proposed"}
    assert catalog.list_tables() == []  # nothing imported yet
    # five daily exports share one table, appended file by file; everything else is its own table
    daily = [s for s in proposed if "daily_report" in s["path"]]
    assert {s["target_table"] for s in daily} == {"exports.daily_report"}
    assert {s["mode"] for s in daily} == {"append"}
    assert len({s["target_table"] for s in proposed}) == 46


def test_import_requires_prior_add_folder(folder, catalog):
    from resink_catalog.catalog import ImportErrorForUser

    out, _ = folder
    with pytest.raises(ImportErrorForUser, match="add-folder"):
        catalog.import_folder(out)


def test_fifty_files_import_and_success_rate(folder, catalog, capsys):
    out, plan = folder
    catalog.add_folder(out)
    results = catalog.import_folder(out)
    assert len(results) == 50
    checked = _check(catalog, out, plan)
    failures = {f: why for f, why in checked.items() if why}
    ok = len(checked) - len(failures)
    rate = ok / len(checked)
    with capsys.disabled():
        print(f"\nIMPORT SUCCESS RATE: {ok}/{len(checked)} = {rate:.0%}")
        for f, why in sorted(failures.items()):
            print(f"  FAILED {f}: {why}")
    (out.parent / "result.json").write_text(json.dumps({"ok": ok, "total": len(checked), "failures": failures}))
    assert rate >= 0.9, failures
    assert not failures, failures  # on this synthetic set everything is expected to work; regressions fail loudly


# Files that real exports contain and a tidy synthetic set does not. A failure must be reported, never silent.
HARD_EXPECTED_ERRORS = {"hard_empty.csv", "hard_binary.csv", "hard_ragged.csv"}


def test_hard_cases_import_or_fail_loudly(tmp_path, catalog, capsys):
    from samples import generate_hard

    out = tmp_path / "hard"
    plan = generate_hard(out)
    catalog.add_folder(out)
    catalog.import_folder(out)
    checked = _check(catalog, out, plan)
    failed = {f for f, why in checked.items() if why}
    with capsys.disabled():
        print(f"\nHARD CASES: {len(plan) - len(failed)}/{len(plan)} imported correctly")
        for f in sorted(failed):
            print(f"  not imported: {f}: {checked[f]}")
    assert failed == HARD_EXPECTED_ERRORS, checked
    for s in catalog.sources():
        if s["path"].rsplit("/", 1)[-1] in HARD_EXPECTED_ERRORS:
            assert s["status"] == "error" and s["error"], s  # reported with a reason, nothing half-imported
