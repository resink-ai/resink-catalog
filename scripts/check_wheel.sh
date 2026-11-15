#!/usr/bin/env sh
# Build the wheel and fail unless it contains the built web UI. Run scripts/build_ui.sh first.
set -eu
cd "$(dirname "$0")/.."
rm -rf dist
uv build --wheel >/dev/null
wheel=$(ls dist/*.whl)
for f in resink_catalog/static/index.html resink_catalog/static/favicon.svg \
  resink_catalog/agent_kit/claude/skills/data-interview/SKILL.md resink_catalog/agent_kit/codex/prompts/table-tidy-up.md; do
  unzip -l "$wheel" | grep -q "$f" || { echo "missing from wheel: $f" >&2; exit 1; }
done
unzip -l "$wheel" | grep -q "resink_catalog/static/assets/.*\.js" || { echo "missing JS bundle" >&2; exit 1; }
echo "ok: $wheel contains the web UI and the agent kit"
