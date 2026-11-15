# Contributing

Resink Local Catalog is Apache-2.0. Issues and pull requests are welcome.

## Set up

```bash
uv sync
scripts/build_ui.sh                  # needs Node 20+; installs web/node_modules, runs Vitest, builds the UI into the package
uv run pytest                        # offline: no test needs the network
uv run ruff check . && uv run ruff format --check .
(cd web && npx playwright test)      # browser journey; uses the installed Chrome
```

## Rules

- **No test may need the network.** Anything that downloads (DuckDB extensions, real-provider smoke runs) is optional
  and skips itself, except in the release CI, which installs the extensions first and fails on a skip.
- **Licences:** new runtime dependencies must be permissive (MIT, BSD, Apache-2.0, PSF, ISC). Regenerate the list with
  `uv run python scripts/licenses.py --markdown > THIRD_PARTY_LICENSES.md`; a test fails if you forget.
- **The push contract** (`contracts/push-protocol-v0.openapi.yaml`) is changed together with the design document and
  its test (`tests/test_push_contract.py`). Every request object stays closed (`additionalProperties: false`).
- **Data stays local.** The MCP tools never change data; what an AI writes (tags, notes) is stored with author `ai`.
  Anything that makes the server reachable from another machine, or that puts the token in a cookie or a URL query,
  will be declined.
- Real-data tests are opt-in (`RESINK_CATALOG_REAL_DATA`) and the data never goes into the repository.

## Pull requests

Keep them small, with tests. Run `ruff format .` first. Describe what a user sees change.
