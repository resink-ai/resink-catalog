# resink-catalog

Local Catalog: turns data files on your computer (CSV, Parquet, exports) into queryable Iceberg tables, and can push them to Resink cloud. Free and open source (Apache-2.0).

**Status: L2 (web UI).** CSV and Parquet files become Iceberg tables; the catalog speaks Iceberg REST; you can query it with read-only SQL, and browse, review and import from a web UI. No MCP or push yet.

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

## The web UI

`resink-catalog serve` prints a link and opens it. The link carries the access token once; the server turns it into an `HttpOnly`, `SameSite=Strict` cookie and the page removes the token from the address bar. Without the link (or the cookie) every page and asset answers 401. Requests that use the cookie and come from another origin are refused.

- **Tables:** list, schema, sample rows, snapshot history, and whether the table can be pushed to Resink cloud.
- **Folders:** add a folder to get a proposal (nothing is imported), review a file, change inferred column types, import one file or all pending ones.
- **SQL:** read-only queries with Ctrl+Enter; one `SELECT` only.
- Light and dark themes follow the system, or can be forced with the button (remembered).

Column types can also be set from the command line: `resink-catalog preview sales.csv` shows what was inferred, and `resink-catalog import sales.csv --type id=string --type day=date` overrides it (types: string, long, double, boolean, date, timestamp). The choice is remembered for later imports of that file. A type cannot change on a table that already exists.

From a source checkout the UI has to be built once (Node 20+): `scripts/build_ui.sh`. Released wheels already contain it.

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

## Reproduce the checks

```bash
uv sync
uv run pytest                              # all tests, offline
uv run pytest -s tests/test_sample_set.py  # prints the import success rate on the 50-file sample
uv run python tests/samples.py /tmp/sample # writes the 50 sample files, to try add-folder by hand
scripts/build_ui.sh                        # web: npm ci, Vitest (18 tests), production build into the package
(cd web && npx playwright test)            # end-to-end smoke test against `serve`, drives the installed Chrome
scripts/check_wheel.sh                     # builds the wheel and checks that it contains the UI
```

The Playwright test uses the Chrome already on the machine (`channel: "chrome"`), so nothing is downloaded; use `E2E_CHANNEL=chromium npx playwright install chromium` for a managed browser. It needs no network after `npm ci` (D10).

### DuckDB

DuckDB can read and write the catalog through its `iceberg` extension. The token goes in a secret, and the authorization type must be `oauth2` (`none` sends no token and is refused with 401, as it should):

```sql
INSTALL httpfs; INSTALL iceberg;            -- once; DuckDB downloads them, so it needs the network that one time
LOAD httpfs; LOAD iceberg;
CREATE SECRET (TYPE iceberg, TOKEN '<your access token>');
ATTACH 'local' AS ice (TYPE iceberg, ENDPOINT 'http://127.0.0.1:8765/iceberg', AUTHORIZATION_TYPE 'oauth2');
SELECT * FROM ice.shop.orders;
INSERT INTO ice.shop.orders VALUES (4, 'd');
```

Caveat: DuckDB `DELETE` and `UPDATE` write Iceberg delete files. They read back correctly everywhere, but push v0 refuses tables that have delete files, and `GET /api/v1/tables/{name}` reports `push_ready: false` with the blocker. Rewrite the table with the Python API or re-import the source to make it pushable again.

The DuckDB tests skip themselves unless both extensions are already installed (D10: the test run never downloads anything). `uv run python scripts/duckdb_rest_probe.py [--install]` prints what works.

## Develop

```bash
uv sync
uv run pytest        # runs fully offline, no remote resources
uv run ruff check . && uv run ruff format --check .
```

## License

Apache-2.0, see `LICENSE`.
