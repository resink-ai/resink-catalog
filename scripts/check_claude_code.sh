#!/usr/bin/env sh
# Manual check, NOT for CI: can real Claude Code attach to `resink-catalog mcp`?
#
#   scripts/check_claude_code.sh           # attach check only: no model call, no cost
#   scripts/check_claude_code.sh --ask     # also asks Claude to use the tools (one short model call)
#
# It builds a throw-away catalog and a throw-away project folder with a `.mcp.json`, so your own Claude Code
# configuration and your own catalog are untouched. Needs the `claude` CLI on PATH and `uv`.
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

(cd "$repo" && uv run python - "$work/home" <<'PY'
import sys, pandas as pd
from resink_catalog import Catalog
Catalog(sys.argv[1]).write_table("shop.orders", pd.DataFrame({"id": [1, 2, 3], "amount": [9.5, 20.0, 3.25]}))
PY
)

server_cmd="uv run --project $repo resink-catalog mcp --home $work/home"

# 1. Attach check, in a throw-away Claude Code configuration folder so your own settings are untouched.
export CLAUDE_CONFIG_DIR="$work/claude-config"
mkdir -p "$CLAUDE_CONFIG_DIR"
# shellcheck disable=SC2086
claude mcp add --scope user resink-catalog -- $server_cmd >/dev/null
echo "== claude mcp list (connects to every configured server) =="
out=$(claude mcp list 2>&1) || true
echo "$out"
line=$(echo "$out" | grep "^resink-catalog" || true)
[ -n "$line" ] || { echo "FAIL: resink-catalog is not listed" >&2; exit 1; }
echo "$line" | grep -q "Connected" || { echo "FAIL: resink-catalog did not connect: $line" >&2; exit 1; }
echo "OK: Claude Code attached to resink-catalog over stdio"

# 2. Optional: a real conversation, with your own Claude Code login and the server passed on the command line.
if [ "${1:-}" = "--ask" ]; then
  unset CLAUDE_CONFIG_DIR
  cat > "$work/mcp.json" <<JSON
{"mcpServers": {"resink-catalog": {"command": "uv", "args": ["run", "--project", "$repo", "resink-catalog", "mcp", "--home", "$work/home"]}}}
JSON
  echo "== asking Claude to use the tools =="
  claude -p "Use the resink-catalog MCP server: list the tables, run a read-only query for the total of shop.orders.amount, then try 'drop table shop.orders' with query_sql and tell me what happened. Answer in three short lines." \
    --mcp-config "$work/mcp.json" --strict-mcp-config \
    --allowedTools "mcp__resink-catalog__list_tables mcp__resink-catalog__query_sql" 2>&1 | tail -20
fi
