"""Opt-in: the importer against real data.

    RESINK_CATALOG_REAL_DATA=/path/to/folder-of-sqlite-files uv run pytest tests/test_real_data.py -s

Each of up to 50 tables (the two largest per database, up to 300k rows) is exported as plain CSV, Excel-style
European CSV (semicolon, Windows-1252, CRLF) or Parquet, imported, and compared with the source: rows, columns, the
sum of every numeric column and the distinct count of the first text column. The folder is read, never copied. The
data may not be ours to redistribute, which is why this test is skipped unless you point it at a folder.
Variables: RESINK_CATALOG_REAL_DATA_TABLES (default 50), RESINK_CATALOG_REAL_DATA_MAX_ROWS (default 300000).
"""

import os
import time
from pathlib import Path

import pytest
from real_data import run

FOLDER = os.environ.get("RESINK_CATALOG_REAL_DATA")

pytestmark = pytest.mark.skipif(
    not FOLDER or not list(Path(FOLDER or ".").glob("*.sqlite")),
    reason="set RESINK_CATALOG_REAL_DATA to a folder of .sqlite files to run this",
)


def test_real_tables_import_with_the_same_values(tmp_path, capsys):
    started = time.time()
    outcomes = run(
        Path(FOLDER),
        tmp_path,
        int(os.environ.get("RESINK_CATALOG_REAL_DATA_TABLES", "50")),
        int(os.environ.get("RESINK_CATALOG_REAL_DATA_MAX_ROWS", "300000")),
    )
    ok = [o for o in outcomes if o.ok]
    with capsys.disabled():
        by_style: dict[str, list[bool]] = {}
        for o in outcomes:
            by_style.setdefault(o.style, []).append(o.ok)
        print(
            f"\nREAL DATA: {len(ok)}/{len(outcomes)} tables identical to the source, "
            f"{sum(o.rows for o in outcomes):,} rows, {time.time() - started:.0f} s"
        )
        for style, results in sorted(by_style.items()):
            print(f"  {style:9} {sum(results)}/{len(results)}")
        for o in outcomes:
            if not o.ok:
                print(f"  FAILED {o.file} ({o.style}): {'; '.join(o.problems)}")
    assert outcomes, "no tables found"
    assert len(ok) == len(outcomes), [(o.file, o.problems) for o in outcomes if not o.ok]
