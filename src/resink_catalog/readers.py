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

        def parse(content: str) -> pa.Table:
            utf8.write_text(content, encoding="utf-8", newline="")
            return duckdb.read_csv(str(utf8), sample_size=-1, ignore_errors=False).to_arrow_table()

        try:
            table = parse(text)
        except duckdb.Error as e:
            # `\r\r\n` is what a CRLF file becomes after one more text-mode newline translation (Windows tools do
            # this), and DuckDB's sniffer fails on it for a file without data rows. Only a file that failed to read
            # is retried that way, so a `\r\r\n` inside a quoted value of a readable file is kept exactly.
            if "\r\r" not in text:
                raise ReadError(f"not a readable CSV: {str(e).splitlines()[0].replace(str(utf8), path.name)}") from e
            try:
                table = parse(re.sub(r"\r{2,}\n", "\r\n", text))
            except duckdb.Error:
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


# Column types a user may choose. They are the Iceberg primitives the catalog writes.
TYPE_NAMES = ("string", "long", "double", "boolean", "date", "timestamp")
_SQL_TYPE = {
    "string": "VARCHAR",
    "long": "BIGINT",
    "double": "DOUBLE",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
}


def type_name(t: pa.DataType) -> str:
    """The user-facing name of an Arrow type."""
    if pa.types.is_integer(t):
        return "long"
    if pa.types.is_floating(t) or pa.types.is_decimal(t):
        return "double"
    if pa.types.is_boolean(t):
        return "boolean"
    if pa.types.is_date(t):
        return "date"
    if pa.types.is_timestamp(t):
        return "timestamp"
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "string"
    return str(t)


def apply_types(table: pa.Table, overrides: dict[str, str]) -> pa.Table:
    """Cast the named columns. A value that cannot be converted fails the whole import with the column named, so a
    wrong choice is seen, not silently turned into nulls."""
    if not overrides:
        return table
    unknown = [c for c in overrides if c not in table.schema.names]
    if unknown:
        raise ReadError(f"no such column: {', '.join(unknown)}")
    bad = [f"{c}: {t}" for c, t in overrides.items() if t not in TYPE_NAMES]
    if bad:
        raise ReadError(f"unknown type ({', '.join(bad)}); choose one of {', '.join(TYPE_NAMES)}")
    con = duckdb.connect()
    con.register("_t", table)
    parts = []
    for name in table.schema.names:
        q = '"' + name.replace('"', '""') + '"'
        want = overrides.get(name)
        if want is None or type_name(table.schema.field(name).type) == want:
            parts.append(q)
        elif want == "double" and pa.types.is_string(table.schema.field(name).type):
            parts.append(f"CAST(replace({q}, ',', '') AS DOUBLE) AS {q}")  # '1,234.50'
        else:
            parts.append(f"CAST({q} AS {_SQL_TYPE[want]}) AS {q}")
    try:
        return con.sql(f"SELECT {', '.join(parts)} FROM _t").to_arrow_table()
    except duckdb.Error as e:
        raise ReadError(f"cannot convert to the chosen type: {str(e).splitlines()[0]}") from e
    finally:
        con.close()
