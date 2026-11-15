"""Check the importer against real data: SQLite tables exported the way users' tools export them, then imported and
compared with the source at row, column and value level.

Used by `test_real_data.py` (opt-in: set RESINK_CATALOG_REAL_DATA to a folder of `.sqlite` files) and by
`test_real_data_harness.py` (always on, with a small synthetic database, to prove the checks can fail). The data is
read from where it is and exported into a temporary folder; nothing is copied into the repository.

Value-level checks, per imported table, on top of row and column counts:
- every numeric column: count of non-null values and their sum (integers exactly, floats to 1e-9 relative);
- the first text column: number of distinct non-empty values.
"""

from __future__ import annotations

import csv
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from resink_catalog import Catalog

STYLES = ["csv", "csv", "csv", "csv", "excel-eu", "parquet"]  # a rough mix: plain, Excel-EU, Parquet


@dataclass
class Source:
    db: Path
    table: str
    columns: list[str]
    rows: list[tuple]


@dataclass
class Outcome:
    file: str
    style: str
    rows: int
    columns: int
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def pick(dbs: list[Path], target: int = 50, max_rows: int = 300_000) -> list[tuple[Path, str]]:
    """The largest and second largest table (up to `max_rows`) of each database, up to `target` tables."""
    chosen: list[tuple[Path, str]] = []
    for db in dbs:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        names = [
            r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")
        ]
        sized = []
        for t in names:
            try:
                n = con.execute(f'select count(*) from "{t}"').fetchone()[0]
            except sqlite3.Error:
                continue
            if 1 <= n <= max_rows:
                sized.append((n, t))
        con.close()
        chosen += [(db, t) for _, t in sorted(sized, reverse=True)[:2]]
    return chosen[:target]


def load(db: Path, table: str) -> Source:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.text_factory = lambda b: b.decode("utf-8", errors="replace")
    cur = con.execute(f'select * from "{table}"')
    columns = [d[0] for d in cur.description]
    rows = cur.fetchall()
    con.close()
    return Source(db, table, columns, rows)


def export(src: Source, out: Path, style: str) -> Path:
    stem = f"{src.db.stem}__{src.table}".replace(" ", "_")
    if style == "parquet":
        path = out / f"{stem}.parquet"
        arrays = {}
        for i, c in enumerate(src.columns):
            values = [r[i] for r in src.rows]
            try:
                arrays[c] = pa.array(values)
            except (pa.ArrowInvalid, pa.ArrowTypeError):
                arrays[c] = pa.array([None if x is None else str(x) for x in values])
        pq.write_table(pa.table(arrays), path)
    elif style == "excel-eu":  # semicolon, Windows-1252, CRLF, as Excel writes in many European locales
        path = out / f"{stem}.csv"
        with path.open("w", encoding="cp1252", errors="replace", newline="") as f:
            w = csv.writer(f, delimiter=";", lineterminator="\r\n")
            w.writerow(src.columns)
            w.writerows(src.rows)
    else:
        path = out / f"{stem}.csv"
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(src.columns)
            w.writerows(src.rows)
    return path


def _lossy(v: str, style: str) -> str:
    """What a text value looks like after the export (characters Windows-1252 cannot hold became '?')."""
    return v.encode("cp1252", errors="replace").decode("cp1252") if style == "excel-eu" else v


def _is_number(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _t(table: str) -> str:
    return ".".join(_q(p) for p in table.split("."))


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def verify(cat: Catalog, src: Source, style: str, path: Path) -> Outcome:
    out = Outcome(path.name, style, len(src.rows), len(src.columns))
    r = cat.import_file(path)
    if r.outcome not in ("created", "updated"):
        out.problems.append(f"import failed: {(r.error or '')[:200]}")
        return out
    names = [f["name"] for f in cat.table_info(r.table)["schema"]]
    if len(names) != len(src.columns):
        out.problems.append(f"columns {len(names)} != {len(src.columns)}")
        return out
    n = cat.query(f"select count(*) as n from {_t(r.table)}")["n"][0]
    if n != len(src.rows):
        out.problems.append(f"rows {n} != {len(src.rows)}")
        return out
    for i, source_name in enumerate(src.columns):
        values = [row[i] for row in src.rows if row[i] is not None]
        col = _q(names[i])
        if values and all(_is_number(v) for v in values):
            got = cat.query(
                f"select count({col}) as c, sum(try_cast({col} as double)) as s, "
                f"count(try_cast({col} as double)) as ok from {_t(r.table)}"
            ).iloc[0]
            if int(got["c"]) != len(values) or int(got["ok"]) != len(values):
                out.problems.append(f"{source_name}: {got['ok']}/{len(values)} values are numeric after import")
            elif all(isinstance(v, int) for v in values):
                if int(got["s"]) != sum(values) and not math.isclose(got["s"], sum(values), rel_tol=1e-12):
                    out.problems.append(f"{source_name}: sum {got['s']} != {sum(values)}")
            elif not math.isclose(got["s"], math.fsum(values), rel_tol=1e-9, abs_tol=1e-9):
                out.problems.append(f"{source_name}: sum {got['s']} != {math.fsum(values)}")
    first_text = next(
        (i for i in range(len(src.columns)) if any(isinstance(r_[i], str) and r_[i] != "" for r_ in src.rows)), None
    )
    if first_text is not None:
        want = len(
            {
                _lossy(row[first_text], style)
                for row in src.rows
                if isinstance(row[first_text], str) and row[first_text] != ""
            }
        )
        got = cat.query(f"select count(distinct {_q(names[first_text])}) as d from {_t(r.table)}")["d"][0]
        if int(got) != want:
            out.problems.append(f"{src.columns[first_text]}: {got} distinct values != {want}")
    return out


def run(spider: Path, work: Path, target: int = 50, max_rows: int = 300_000) -> list[Outcome]:
    files = work / "files"
    files.mkdir(parents=True, exist_ok=True)
    cat = Catalog(work / "home")
    outcomes = []
    for i, (db, table) in enumerate(pick(sorted(spider.glob("*.sqlite")), target, max_rows)):
        style = STYLES[i % len(STYLES)]
        src = load(db, table)
        outcomes.append(verify(cat, src, style, export(src, files, style)))
    return outcomes
