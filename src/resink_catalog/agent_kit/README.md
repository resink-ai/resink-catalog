# Agent kit

Skills for AI coding tools that talk to the Resink Local Catalog over MCP.

| Skill | What it does |
| --- | --- |
| `data-interview` | Looks at a table, asks the user a few questions, then writes notes and tags (marked `ai` in the web UI). |
| `table-tidy-up` | Finds tables that belong together, duplicates and unclear names; writes a plan as notes. Changes no data. |

## Install

```bash
resink-catalog agent-kit install --claude     # copies the skills to ~/.claude/skills/
resink-catalog agent-kit install --codex      # copies the prompts to ~/.codex/prompts/
resink-catalog agent-kit config               # prints the MCP configuration for both tools
```

## Connect the MCP server

Claude Code (in your project folder, or with `--scope user`):

```bash
claude mcp add resink-catalog -- resink-catalog mcp
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.resink-catalog]
command = "resink-catalog"
args = ["mcp"]
```

Add `"--home", "<dir>"` to the arguments to use a catalog other than `~/.resink-catalog`. The MCP server over HTTP is
also available at `http://127.0.0.1:<port>/mcp` while `resink-catalog serve` runs; send the access token as
`Authorization: Bearer <token>`.

## What the AI can and cannot do

It can list tables, read schemas and samples, run read-only SQL (one `SELECT`), and add tags and notes. It cannot import
files, change or delete data, or run anything but a single `SELECT`. Notes and tags are marked `ai` so you can tell
them from your own, and you can delete any of them in the web UI.
