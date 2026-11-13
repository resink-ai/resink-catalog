# resink-catalog

Local Catalog: turns data files on your computer (CSV, Parquet, exports) into queryable Iceberg tables, and can push them to Resink cloud. Free and open source (Apache-2.0).

**Status: L0 skeleton.** Only the server, access token and CLI exist. No import, Iceberg REST, UI, MCP or push yet.

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

## Develop

```bash
uv sync
uv run pytest        # runs fully offline, no remote resources
uv run ruff check . && uv run ruff format --check .
```

## License

Apache-2.0, see `LICENSE`.
