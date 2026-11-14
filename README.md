# resink-catalog

Local Catalog: turns data files on your computer (CSV, Parquet, exports) into queryable Iceberg tables, and can push them to Resink cloud. Free and open source (Apache-2.0).

**Status: L1 (import and Iceberg REST).** CSV and Parquet files become Iceberg tables; the catalog speaks Iceberg REST; you can query it with read-only SQL. No Web UI, MCP or push yet.

## Run

```bash
uv sync
uv run resink-catalog serve            # http://127.0.0.1:8765, opens the browser
uv run resink-catalog serve --port 0 --no-browser   # free port, no browser
```

The first line printed is the URL with your access token. The token is created on first start and kept in `~/.resink-catalog/token` (mode 0600); set `RESINK_CATALOG_HOME` or pass `--home` to use another directory.

Security model:
- Listens on `127.0.0.1` only. The host cannot be changed.
- Every route except `GET /healthz` needs the token (`Authorization: Bearer <token>`, or `?token=` for the link the browser opens). Unknown routes also answer 401.
- Requests whose `Host` header is not loopback are refused (DNS rebinding).

## Import your files

```bash
uv run resink-catalog import sales.csv --table sales.monthly   # one file -> one Iceberg table
uv run resink-catalog add-folder ~/exports                      # propose: finds files, groups repeated exports, imports nothing
uv run resink-catalog import ~/exports                          # apply the proposal after you looked at it
uv run resink-catalog scan [--apply]                            # re-check sources; --apply re-imports changed ones
```

- Re-importing a file whose content (SHA-256) did not change does nothing. Changed content creates new snapshots (a replace is a delete plus an append, so two snapshots) and old snapshots stay queryable.
- Files whose names differ only by digits or dates and whose columns match (`daily_2026-10-01.csv`, `daily_2026-10-02.csv`) go into one table with a `_source_file` column; re-importing one file replaces only its rows.
- CSV: encoding (UTF-8, BOM, UTF-16, Windows-1252), delimiter, quoting and header are detected; thousands separators become numbers; an empty trailing column is dropped. Unreadable files are reported with a reason and nothing is half-imported.
- Tables are unpartitioned, Parquet, with no delete files, so they can be pushed to Resink cloud (push protocol v0).

## Use it from Python

```python
from resink_catalog import Catalog

cat = Catalog()  # ~/.resink-catalog, or Catalog("/some/dir")
cat.write_table("shop.orders", dataframe)  # mode="append" to add rows
cat.read_table("shop.orders")  # pandas DataFrame
cat.query("select count(*) from shop.orders")  # read-only SQL over all tables (DuckDB)
```

## HTTP APIs (all need the token)

| Route | Purpose |
| --- | --- |
| `/iceberg/v1/...` | Iceberg REST catalog: config, namespaces, tables (list, create, load, commit, drop, rename). No views, no multi-table transactions, no scan planning. Point PyIceberg at `http://127.0.0.1:8765/iceberg` with `token=<token>`. |
| `GET /api/v1/tables`, `GET /api/v1/tables/{name}` | tables, schema, snapshots |
| `POST /api/v1/query` `{"sql", "limit"}` | read-only SQL; anything but one `SELECT` is refused with 400 |
| `GET /api/v1/sources`, `POST /api/v1/roots`, `POST /api/v1/sources/import`, `POST /api/v1/sources/scan` | the same as the CLI |

## Reproduce the L1 checks

```bash
uv sync
uv run pytest                              # all tests, offline
uv run pytest -s tests/test_sample_set.py  # prints the import success rate on the 50-file sample
uv run python tests/samples.py /tmp/sample # writes the 50 sample files, to try add-folder by hand
```

The DuckDB-over-REST tests are skipped unless DuckDB's `httpfs` and `iceberg` extensions are installed (a one-time download, so not part of the test run): `uv run python scripts/duckdb_rest_probe.py --install`.

## Develop

```bash
uv sync
uv run pytest        # runs fully offline, no remote resources
uv run ruff check . && uv run ruff format --check .
```

## License

Apache-2.0, see `LICENSE`.
