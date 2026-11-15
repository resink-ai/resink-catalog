"""The agent kit: skills exist, say true things about the tools, and install where the AI tools look."""

import re

import pytest

from resink_catalog import agent_kit
from resink_catalog.cli import main
from resink_catalog.mcp_server import build_server

KIT = agent_kit.kit_root()


async def real_tools() -> set[str]:
    from mcp import Client

    async with Client(build_server(lambda: None)) as c:  # listing tools never touches the catalog
        return {t.name for t in (await c.list_tools()).tools}


def frontmatter(path) -> dict[str, str]:
    text = path.read_text()
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, f"{path} has no frontmatter"
    return dict(line.split(": ", 1) for line in m.group(1).splitlines())


def all_files():
    return [KIT / "claude" / "skills" / s / "SKILL.md" for s in agent_kit.SKILLS] + [
        KIT / "codex" / "prompts" / f"{s}.md" for s in agent_kit.SKILLS
    ]


@pytest.mark.parametrize("skill", agent_kit.SKILLS)
def test_claude_skill_frontmatter(skill):
    fm = frontmatter(KIT / "claude" / "skills" / skill / "SKILL.md")
    assert fm["name"] == skill and len(fm["description"]) > 40
    assert "use when" in fm["description"].lower()  # says what it does, then when to use it


@pytest.mark.parametrize("skill", agent_kit.SKILLS)
def test_codex_prompt_has_a_description(skill):
    assert frontmatter(KIT / "codex" / "prompts" / f"{skill}.md")["description"]


@pytest.mark.anyio
async def test_skills_name_only_tools_that_exist_and_allow_exactly_those():
    tools = await real_tools()
    for path in all_files():
        body = path.read_text().split("---\n", 2)[-1]
        named = set(re.findall(r"`([a-z]+_[a-z_]+)`", body))  # snake_case words in backticks are tool names
        assert named and named <= tools, (path.name, named - tools)
        assert {"list_tables", "describe_table", "query_sql", "write_note"} <= named, path.name
    for skill in agent_kit.SKILLS:
        allowed = set(frontmatter(KIT / "claude" / "skills" / skill / "SKILL.md")["allowed-tools"].split())
        assert allowed == {f"mcp__resink-catalog__{t}" for t in tools}, (
            "a skill may use the catalog's tools, nothing else"
        )


def test_skills_state_the_limits_and_the_ai_marking():
    for path in all_files():
        text = path.read_text().lower()
        assert "cannot" in text and "import" in text and "delete" in text  # what the AI may not do
        assert "ai" in text and "read-only" in text


def test_install_into_given_folders(tmp_path, capsys):
    assert main(["agent-kit", "install", "--claude", str(tmp_path / "c"), "--codex", str(tmp_path / "x")]) == 0
    for skill in agent_kit.SKILLS:
        assert (tmp_path / "c" / skill / "SKILL.md").read_text() == (
            KIT / "claude/skills" / skill / "SKILL.md"
        ).read_text()
        assert (tmp_path / "x" / f"{skill}.md").exists()
    assert str(tmp_path / "c" / "data-interview" / "SKILL.md") in capsys.readouterr().out


def test_install_defaults_go_to_the_home_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert main(["agent-kit", "install", "--claude", "--codex"]) == 0
    assert (tmp_path / ".claude/skills/table-tidy-up/SKILL.md").exists()
    assert (tmp_path / ".codex/prompts/data-interview.md").exists()


def test_install_needs_a_target_and_config_prints(capsys):
    assert main(["agent-kit", "install"]) == 2
    assert main(["agent-kit", "config"]) == 0
    out = capsys.readouterr().out
    assert "claude mcp add resink-catalog" in out and "[mcp_servers.resink-catalog]" in out


@pytest.mark.parametrize("path", sorted(p.relative_to(KIT) for p in KIT.rglob("*.md") if p.name != "README.md"))
def test_every_skill_and_prompt_says_data_is_not_instructions(path):
    assert "not instructions" in (KIT / path).read_text().lower()
