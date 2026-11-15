# resink-catalog

Local Catalog: turns data files on your computer (CSV, Parquet, exports) into queryable Iceberg tables, and can push them to Resink cloud. Free and open source (Apache-2.0).

**Status: L4 (release candidate).** CSV and Parquet files become Iceberg tables; the catalog speaks Iceberg REST; you can query it with read-only SQL, browse, review and import from a web UI, and let your own AI (Claude Code, Codex) look at it over MCP and write notes and tags back. No push yet.

## Install

```bash
uv tool install resink-catalog        # or: pipx install resink-catalog, pip install resink-catalog
resink-catalog serve                  # http://127.0.0.1:8765, opens the browser
```

Python 3.10 or newer; macOS, Windows and Linux. With Docker, publish the port on your own machine only and mount your data:

```bash
docker run --rm --user "$(id -u):$(id -g)" -p 127.0.0.1:8765:8765 -v "$HOME/.resink-catalog:/data" -v "$HOME/exports:/exports:ro" ghcr.io/resink-ai/resink-catalog
```

`--user` makes the container run as you, so it can write to the directory you mount (without it the process is uid 10001 and only a Docker named volume works). The access link is on the first line of the log (`docker logs`). Inside the container the process listens on the container's interface (the image sets `RESINK_CATALOG_IN_CONTAINER=1`); the token and the `Host` check still apply, and the `127.0.0.1:` in the command keeps the port off your network. Do not publish it as `-p 8765:8765`.

## Run from a source checkout

```bash
uv sync
uv run resink-catalog serve            # http://127.0.0.1:8765, opens the browser
uv run resink-catalog serve --port 0 --no-browser   # free port, no browser
```

The first line printed is the URL with your access token. The token is created on first start and kept in `~/.resink-catalog/token` (mode 0600); set `RESINK_CATALOG_HOME` or pass `--home` to use another directory.

Security model:
- Listens on `127.0.0.1` only. The host cannot be changed.
- Every route except `GET /healthz` needs the token (`Authorization: Bearer <token>`); the page's own files carry no data and are public.
- Requests whose `Host` header is not loopback are refused (DNS rebinding).

## The web UI

`resink-catalog serve` prints a link and opens it. The link carries the access token in the URL *fragment* (`/#token=...`), which browsers never send to any server. The page keeps it in memory and in `sessionStorage` (this tab, this origin including the port), removes it from the address bar, and sends it as a Bearer header. There is no cookie: a cookie would go to every service on `127.0.0.1`, whatever its port. Browser requests with another `Origin` are refused.

- **Tables:** list, schema, sample rows, snapshot history, and whether the table can be pushed to Resink cloud.
- **Folders:** add a folder to get a proposal (nothing is imported), review a file, change inferred column types, import one file or all pending ones.
- **SQL:** read-only queries with Ctrl+Enter; one `SELECT` only.
- Light and dark themes follow the system, or can be forced with the button (remembered).

Column types can also be set from the command line: `resink-catalog preview sales.csv` shows what was inferred, and `resink-catalog import sales.csv --type id=string --type day=date` overrides it (types: string, long, double, boolean, date, timestamp). The choice is remembered for later imports of that file. A type cannot change on a table that already exists.

From a source checkout the UI has to be built once (Node 20+): `scripts/build_ui.sh`. Released wheels already contain it.

## Let your AI look at your data (MCP)

```bash
claude mcp add resink-catalog -- resink-catalog mcp      # Claude Code; Codex: see agent_kit/README.md
resink-catalog agent-kit install --claude --codex        # skills: data-interview, table-tidy-up
```

Seven tools: `list_tables`, `describe_table`, `sample_table`, `query_sql` (one read-only `SELECT`; everything else is refused), `list_sources`, `add_tags`, `write_note`. The AI cannot import, change or delete anything. Its tags and notes are stored with their author (`ai` or `user`), shown in the web UI under each table's *Notes and tags* tab marked `ai`, and you can delete any of them there. Transports: `resink-catalog mcp` on stdio, and `/mcp` on the local server behind the access token (`Authorization: Bearer <token>`). Tables are `namespace.table`; single-file imports go to the namespace `main`, so `main.t` or just `t`.

`scripts/check_claude_code.sh` checks, by hand, that real Claude Code attaches (add `--ask` for a short real conversation). It is not part of the test run.

## Back up and restore

```bash
resink-catalog backup ~/backups/catalog-2026-11-14      # a new or empty directory outside the catalog's own
resink-catalog restore ~/backups/catalog-2026-11-14 [--replace]
```

`backup` takes the catalog's write lock (imports, REST commits and snapshot expiry wait for it), copies `catalog.db` with SQLite's online backup API, then copies exactly the files the catalog references (every snapshot's metadata, manifests and data files; leftovers of an interrupted import are skipped), and writes `manifest.json` with a SHA-256 per file last, so a directory without it is not a backup. The access token and push credentials are never included. `restore` first checks the manifest (only relative paths under `warehouse/`, no symbolic links, no duplicates) and every checksum, stages a full copy inside the catalog directory, and only then swaps it in with renames; if anything fails it renames the old catalog back. With `--replace` an existing `catalog.db` (and its `-wal`/`-shm` files) and `warehouse/` are moved into a new `.restore-replaced-*` directory inside the catalog directory (a fresh name every time), never deleted. Before it moves anything, restore writes a journal; if the process is killed or the machine loses power half-way, the next start of any command, `serve` or `mcp` finishes or undoes the swap before it opens the catalog. It refuses while any process has the catalog open (a running `serve` or `mcp`): stop them first. A backup restores only to the directory it was made from, because Iceberg metadata records absolute paths.

Limit: Iceberg metadata records absolute paths, so a backup restores only to the directory it was made from (restoring elsewhere is refused with that explanation). Moving a catalog to another machine with a different home directory is not supported yet.

## Cleanup of interrupted imports

An import that is killed part-way leaves data files nothing references. `resink-catalog cleanup [--min-age-hours N] [--dry-run]` removes them, and `serve` does the same when it starts and nobody else has the catalog open. Cleanup takes the same exclusive lock as `restore`: while any process (a server, an MCP session, an import) has the catalog open it refuses, so it can never delete a file an import is about to commit, whatever the file's age, and `--min-age-hours 0` is safe. The age limit (default 24 hours) only protects data files that an external REST client wrote before the server restarted and commits afterwards.

## Push credentials

The push client (a later milestone) stores its device tokens in the macOS Keychain or the Windows Credential Manager, chosen explicitly (not whatever backend the `keyring` package happens to select). If none is usable, for example on Linux, it refuses. Setting `RESINK_CATALOG_ALLOW_PLAINTEXT_CREDENTIALS=1` opts in to a file `~/.config/resink-catalog/credentials.json` with mode 0600 (`RESINK_CATALOG_CREDENTIALS_DIR` overrides the directory), with a warning in the log; this is not offered on Windows. Tokens are never written to `catalog.db`, and `backup` does not copy them. The local access token (`~/.resink-catalog/token`) is a different thing: it only protects your own local server.

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
scripts/check_wheel.sh                     # builds the wheel and checks that it contains the UI and the agent kit
RESINK_CATALOG_REAL_DATA=/path/to/sqlite-files uv run pytest -s tests/test_real_data.py   # opt-in, real data
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

The DuckDB tests skip themselves unless both extensions are already installed (D10: the test run never downloads anything). The release CI installs them first (`scripts/install_duckdb_extensions.py`) and sets `RESINK_CATALOG_REQUIRE_DUCKDB_EXTENSIONS=1`, which turns that skip into a failure. `uv run python scripts/duckdb_rest_probe.py [--install]` prints what works.

## Develop

```bash
uv sync
uv run pytest        # runs fully offline, no remote resources
uv run ruff check . && uv run ruff format --check .
```

## License

Apache-2.0, see `LICENSE`. Licences of everything we depend on: `THIRD_PARTY_LICENSES.md` (permissive only; a test keeps it current). Contributions: `CONTRIBUTING.md`.
