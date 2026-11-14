"""Turn a data file into an Arrow table. CSV goes through DuckDB's sniffer; Parquet is read directly."""

from __future__ import annotations

import codecs
import hashlib
import re
import tempfile
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

CSV_SUFFIXES = {".csv", ".tsv", ".txt"}
PARQUET_SUFFIXES = {".parquet"}
THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
GENERATED = re.compile(r"^column\d+$")


class ReadError(Exception):
    """The file could not be turned into a table; the message is shown to the user."""


def detect_format(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in CSV_SUFFIXES:
        return "csv"
    if suffix in PARQUET_SUFFIXES:
        return "parquet"
    return None


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _decode(raw: bytes) -> str:
    """Text of a file in an unknown encoding: BOMs first, then UTF-8, else Windows-1252 (a Latin-1 superset)."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8) :].decode("utf-8")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("cp1252")
        except UnicodeDecodeError:
            return raw.decode("latin-1")


def _clean_names(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, n in enumerate(names):
        n = n.strip() or f"column_{i + 1}"
        if n in seen:
            seen[n] += 1
            n = f"{n}_{seen[n]}"
        else:
            seen[n] = 1
        out.append(n)
    return out


def read_csv(path: Path) -> pa.Table:
    try:
        text = _decode(path.read_bytes())
    except OSError as e:
        raise ReadError(f"cannot read file: {e}") from e
    if not text.strip():
        raise ReadError("the file is empty")
    with tempfile.TemporaryDirectory() as tmp:
        utf8 = Path(tmp) / "data.csv"  # DuckDB reads UTF-8 only without extensions; transcode once
        utf8.write_text(text, encoding="utf-8", newline="")
        try:
            table = duckdb.read_csv(str(utf8), sample_size=-1, ignore_errors=False).to_arrow_table()
        except duckdb.Error as e:
            raise ReadError(f"not a readable CSV: {str(e).splitlines()[0].replace(str(utf8), path.name)}") from e
    if table.num_columns == 0:
        raise ReadError("the file is empty")
    if table.num_columns == 1 and re.search(r"[,;\t|]", table.schema.names[0]):
        # The sniffer gave up (for example rows with different numbers of fields) and read each line as one value.
        raise ReadError("could not detect the delimiter; check that every row has the same number of fields")
    table = _drop_empty_generated_columns(table)
    table = _fix_thousands(table)
    names = _clean_names(table.schema.names)
    return table.rename_columns(names)


def _drop_empty_generated_columns(table: pa.Table) -> pa.Table:
    """Exports often end every row with a delimiter. DuckDB calls that column `columnN`; drop it if it is empty."""
    keep = []
    for i, name in enumerate(table.schema.names):
        col = table.column(i)
        if GENERATED.match(name) and col.null_count == len(col):
            continue
        keep.append(i)
    return table.select(keep) if len(keep) != table.num_columns else table


def _fix_thousands(table: pa.Table) -> pa.Table:
    """'1,234.50' arrives as text because of the comma. Make it a number when every value looks like one."""
    for i, field in enumerate(table.schema):
        if not pa.types.is_string(field.type):
            continue
        values = [v for v in table.column(i).to_pylist() if v not in (None, "")]
        if values and all(THOUSANDS.match(v) for v in values):
            cleaned = [None if v in (None, "") else v.replace(",", "") for v in table.column(i).to_pylist()]
            numbers = pa.array(cleaned, pa.string()).cast(pa.float64())
            table = table.set_column(i, field.name, numbers)
    return table


def read_parquet(path: Path) -> pa.Table:
    try:
        table = pq.read_table(path)
    except Exception as e:  # pyarrow raises several types for corrupt files
        raise ReadError(f"not a readable Parquet file: {e}") from e
    fields = []
    for f in table.schema:  # Iceberg v2 has microsecond timestamps at most
        if pa.types.is_timestamp(f.type) and f.type.unit == "ns":
            f = f.with_type(pa.timestamp("us", tz=f.type.tz))
        elif pa.types.is_unsigned_integer(f.type):
            f = f.with_type(pa.int64())
        fields.append(f)
    return table.cast(pa.schema(fields))


def read_file(path: Path, fmt: str) -> pa.Table:
    if fmt == "csv":
        return read_csv(path)
    if fmt == "parquet":
        return read_parquet(path)
    raise ReadError(f"unsupported format {fmt!r}")
