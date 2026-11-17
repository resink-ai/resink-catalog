"""Generate a 50-file sample of export quirks, with the ground truth for each file.

Run `uv run python tests/samples.py <dir>` to write the files. These are synthetic: they imitate the quirks the
design lists (encodings, delimiters, quoting, dates, number formats, trailing delimiters, repeated exports). Real
exports are messier, so a success rate on this set is an upper bound for real files, not an estimate of them.
"""

from __future__ import annotations

import json
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

NAMES = ["Café Noir", "Müller & Söhne", "Zoë", "Ångström", "Plain Name", "Señor Pérez", "Crème Brûlée", "Åsa Lindqvist"]
L = "abcdefghij"  # file names differ by letters, so only the daily reports look like one source
ASCII_NAMES = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]


@dataclass
class Expected:
    """Ground truth for one generated file."""

    file: str
    kind: str
    columns: list[str]
    rows: int
    ids: list[int] = field(default_factory=list)
    names: list[str] = field(default_factory=list)  # value of the `name` column, in row order
    group: str | None = None  # files of one repeated export share a group; they must land in one table


def _rows(rng: random.Random, n: int, names: list[str], start: int = 1) -> list[tuple[int, str, float, str]]:
    return [
        (start + i, rng.choice(names), round(rng.uniform(1, 9999), 2), f"2026-10-{(i % 28) + 1:02d}") for i in range(n)
    ]


def _fmt(v, number: str) -> str:
    return f"{v:.2f}".replace(".", ",") if number == "comma-decimal" else str(v)


def _write_csv(
    path: Path, rows, *, sep=",", encoding="utf-8", bom=False, trailing=False, quote_names=False, header=None
) -> None:
    head = header or ["id", "name", "amount", "date"]
    lines = [sep.join(head) + (sep if trailing else "")]
    for i, n, a, d in rows:
        name = f'"{n}"' if quote_names else n
        lines.append(sep.join([str(i), name, str(a), d]) + (sep if trailing else ""))
    text = "\r\n".join(lines) + "\r\n"
    raw = text.encode(encoding)
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + raw)


def generate(out: Path, seed: int = 7) -> list[Expected]:
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    plan: list[Expected] = []
    head = ["id", "name", "amount", "date"]

    def add(file: str, kind: str, rows, columns=head, group=None) -> Expected:
        e = Expected(file, kind, columns, len(rows), [r[0] for r in rows], [r[1] for r in rows], group)
        plan.append(e)
        return e

    for i in range(6):  # clean UTF-8, comma
        rows = _rows(rng, rng.randint(5, 40), ASCII_NAMES)
        _write_csv(out / f"clean_{L[i]}.csv", rows)
        add(f"clean_{L[i]}.csv", "clean", rows)
    for i in range(5):  # UTF-8 with byte order mark
        rows = _rows(rng, 10, NAMES)
        _write_csv(out / f"bom_{L[i]}.csv", rows, bom=True)
        add(f"bom_{L[i]}.csv", "utf8-bom", rows)
    for i in range(5):  # Latin-1 / Windows-1252, as older systems export
        rows = _rows(rng, 12, NAMES)
        _write_csv(out / f"latin1_{L[i]}.csv", rows, encoding="cp1252")
        add(f"latin1_{L[i]}.csv", "latin1", rows)
    for i in range(6):  # semicolon, the European Excel default
        rows = _rows(rng, 15, ASCII_NAMES)
        _write_csv(out / f"semicolon_{L[i]}.csv", rows, sep=";")
        add(f"semicolon_{L[i]}.csv", "semicolon", rows)
    for i in range(4):  # tab separated
        rows = _rows(rng, 9, ASCII_NAMES)
        _write_csv(out / f"tabbed_{L[i]}.tsv", rows, sep="\t")
        add(f"tabbed_{L[i]}.tsv", "tab", rows)
    for i in range(3):  # a quoted field containing a newline and a comma
        rows = _rows(rng, 6, ASCII_NAMES)
        rows[2] = (rows[2][0], "line one\nline two, with comma", rows[2][2], rows[2][3])
        _write_csv(out / f"newline_{L[i]}.csv", rows, quote_names=True)
        add(f"newline_{L[i]}.csv", "quoted-newline", rows)
    for i in range(3):  # header only, no data rows
        (out / f"headeronly_{L[i]}.csv").write_bytes((",".join(head) + "\r\n").encode())  # CRLF on every OS
        add(f"headeronly_{L[i]}.csv", "header-only", [])
    for i in range(3):  # three date formats in one column
        rows = _rows(rng, 9, ASCII_NAMES)
        fmts = ["2026-10-0{d}", "0{d}/10/2026", "{d} Oct 2026"]
        rows = [(r[0], r[1], r[2], fmts[k % 3].format(d=(k % 9) + 1)) for k, r in enumerate(rows)]
        _write_csv(out / f"mixeddates_{L[i]}.csv", rows)
        add(f"mixeddates_{L[i]}.csv", "mixed-dates", rows)
    for i in range(3):  # thousands separators inside quoted numbers
        rows = _rows(rng, 8, ASCII_NAMES)
        lines = ["id,name,amount,date"] + [f'{r[0]},{r[1]},"{r[2] * 1000:,.2f}",{r[3]}' for r in rows]
        (out / f"thousands_{L[i]}.csv").write_text("\n".join(lines) + "\n")
        add(f"thousands_{L[i]}.csv", "thousands", rows)
    for i in range(3):  # every line ends with a delimiter
        rows = _rows(rng, 7, ASCII_NAMES)
        _write_csv(out / f"trailing_{L[i]}.csv", rows, trailing=True)
        add(f"trailing_{L[i]}.csv", "trailing-delimiter", rows)
    for i in range(4):  # Parquet
        rows = _rows(rng, 20, ASCII_NAMES)
        t = pa.table(
            {
                "id": [r[0] for r in rows],
                "name": [r[1] for r in rows],
                "amount": [r[2] for r in rows],
                "date": [r[3] for r in rows],
            }
        )
        pq.write_table(t, out / f"data_{L[i]}.parquet")
        add(f"data_{L[i]}.parquet", "parquet", rows)
    for i in range(5):  # the same report exported on different days
        rows = _rows(rng, 8, ASCII_NAMES, start=1000 * (i + 1))
        _write_csv(out / f"daily_report_2026-10-{i + 1:02d}.csv", rows)
        add(f"daily_report_2026-10-{i + 1:02d}.csv", "repeated-export", rows, group="daily_report")
    assert len(plan) == 50
    (out / "_expected.json").write_text(json.dumps([e.__dict__ for e in plan], indent=1))
    return plan


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "sample-exports")
    print(f"wrote {len(generate(target))} files to {target}")


def generate_hard(out: Path, seed: int = 11) -> list[Expected]:
    """Ten harder files that real exports contain. Reported separately from the 50-file set: they show the gap
    between a tidy synthetic set and real files. Each has the ground truth a human would expect."""
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    rows = _rows(rng, 10, ASCII_NAMES)
    head = ["id", "name", "amount", "date"]
    plan: list[Expected] = []

    def add(file: str, kind: str, n: int = 10, columns=head) -> None:
        plan.append(Expected(file, kind, columns, n, [r[0] for r in rows[:n]], [r[1] for r in rows[:n]]))

    body = [",".join(map(str, r)) for r in rows]
    (out / "hard_preamble.csv").write_text(
        "Sales report\nGenerated 2026-10-31\n\n" + ",".join(head) + "\n" + "\n".join(body)
    )
    add("hard_preamble.csv", "title lines before the header")
    (out / "hard_ragged.csv").write_text(",".join(head) + "\n" + "\n".join(body[:5] + [body[5] + ",extra"] + body[6:]))
    add("hard_ragged.csv", "one row has an extra field")
    (out / "hard_utf16.csv").write_bytes(("﻿" + ",".join(head) + "\n" + "\n".join(body) + "\n").encode("utf-16"))
    add("hard_utf16.csv", "UTF-16 with BOM")
    eu = [f"{r[0]};{r[1]};{str(r[2]).replace('.', ',')};{r[3]}" for r in rows]
    (out / "hard_decimal_comma.csv").write_text(";".join(head) + "\n" + "\n".join(eu))
    add("hard_decimal_comma.csv", "decimal comma, semicolon")
    (out / "hard_dupheaders.csv").write_text(
        "id,name,name,date\n" + "\n".join(f"{r[0]},{r[1]},{r[1]},{r[3]}" for r in rows)
    )
    add("hard_dupheaders.csv", "duplicate column names", columns=["id", "name", "name_1", "date"])
    (out / "hard_empty.csv").write_text("")
    plan.append(Expected("hard_empty.csv", "empty file", [], 0))
    (out / "hard_binary.csv").write_bytes(bytes(rng.randrange(256) for _ in range(2000)))
    plan.append(Expected("hard_binary.csv", "not text at all", [], 0))
    (out / "hard_noheader.csv").write_text("\n".join(body))
    add("hard_noheader.csv", "no header row", columns=["column0", "column1", "column2", "column3"])
    (out / "hard_semicolon_in_text.csv").write_text(
        "id,name,amount,date\n" + "\n".join(f'{r[0]},"{r[1]}; inc",{r[2]},{r[3]}' for r in rows)
    )
    add("hard_semicolon_in_text.csv", "semicolons inside quoted text, comma delimited")
    plan[-1].names = [f"{n}; inc" for n in plan[-1].names]
    (out / "hard_single_column.csv").write_text("name\n" + "\n".join(r[1] for r in rows))
    plan.append(Expected("hard_single_column.csv", "single column", ["name"], 10, [], [r[1] for r in rows]))
    return plan
