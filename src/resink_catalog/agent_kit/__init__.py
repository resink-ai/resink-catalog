"""Skill files for Claude Code and Codex. `resink-catalog agent-kit install` copies them where the tools look."""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

SKILLS = ("data-interview", "table-tidy-up")

MCP_CONFIG = """\
# Claude Code
claude mcp add resink-catalog -- resink-catalog mcp

# Codex: ~/.codex/config.toml
[mcp_servers.resink-catalog]
command = "resink-catalog"
args = ["mcp"]
"""


def kit_root() -> Path:
    return Path(str(resources.files(__package__)))


def install_claude(target: Path | None = None) -> list[Path]:
    """Copy the skills to <target>/<skill>/SKILL.md (default ~/.claude/skills)."""
    target = target or Path.home() / ".claude" / "skills"
    written = []
    for skill in SKILLS:
        dest = target / skill
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(kit_root() / "claude" / "skills" / skill / "SKILL.md", dest / "SKILL.md")
        written.append(dest / "SKILL.md")
    return written


def install_codex(target: Path | None = None) -> list[Path]:
    """Copy the prompts to <target>/<name>.md (default ~/.codex/prompts)."""
    target = target or Path.home() / ".codex" / "prompts"
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for skill in SKILLS:
        shutil.copyfile(kit_root() / "codex" / "prompts" / f"{skill}.md", target / f"{skill}.md")
        written.append(target / f"{skill}.md")
    return written
