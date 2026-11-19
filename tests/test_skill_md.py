"""SKILL.md stays in the Agent Skills shape and only documents real CLI commands."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import yaml
from typer.testing import CliRunner

from benchling_toolkit import cli

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")
_URL = re.compile(r"https://[^\s)`]+")
_ALLOWED_HOSTS = {"github.com"}


def _frontmatter() -> dict[str, object]:
    assert SKILL.startswith("---\n")
    end = SKILL.index("\n---\n", 4)
    loaded = yaml.safe_load(SKILL[4:end])
    assert isinstance(loaded, dict)
    return loaded


def _console_commands() -> list[str]:
    commands: list[str] = []
    in_console = False
    pending = ""
    for line in SKILL.splitlines():
        if line.startswith("```"):
            if in_console and pending:
                commands.append(pending.strip())
                pending = ""
            in_console = not in_console and line[3:].strip() == "console"
            continue
        if not in_console:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending = f"{pending} {stripped[:-1].strip()}".strip()
            continue
        commands.append(f"{pending} {stripped}".strip() if pending else stripped)
        pending = ""
    return commands


def test_frontmatter_matches_the_skill_and_listing_limits() -> None:
    meta = _frontmatter()
    assert meta["name"] == "benchling-toolkit"
    assert meta["name"] == ROOT.name
    description = meta["description"]
    assert isinstance(description, str)
    assert 1 <= len(description) <= 1024
    assert "Use when" in description
    assert "benchling auth login --env NAME" in description
    assert "--allow-writes ENV" in description
    summary = meta["summary"]
    assert isinstance(summary, str) and 1 <= len(summary) <= 300
    assert meta["license"] == "MIT"
    assert isinstance(meta["compatibility"], str) and len(meta["compatibility"]) <= 500
    assert meta["version"] == "0.3.0"
    assert meta["docs_url"] == "https://github.com/jmamodeo/benchling-toolkit"
    tags = meta["tags"]
    keywords = meta["keywords"]
    use_cases = meta["use_cases"]
    assert isinstance(tags, list) and 1 <= len(tags) <= 10
    assert isinstance(keywords, list) and 1 <= len(keywords) <= 10
    assert isinstance(use_cases, list) and 1 <= len(use_cases) <= 8


def test_body_covers_the_read_and_preview_workflows() -> None:
    body = SKILL.split("\n---\n", 1)[1]
    for phrase in (
        "read-only by default",
        "--allow-writes ENV",
        "OS keychain",
        "benchling auth login --env NAME",
        "entry list",
        "entry inspect",
        "table get",
        "results list",
        "results rows",
        "results tables",
        "export --kind",
        "changes plan",
        "changes apply",
    ):
        assert phrase in body
    assert "client secret" in body.casefold()
    assert "Do not run `auth login`" in body


def test_skill_urls_are_the_repository_only() -> None:
    hosts = set()
    for url in _URL.findall(SKILL):
        hosts.add(url.split("//", 1)[1].split("/", 1)[0])
    assert hosts == _ALLOWED_HOSTS


def test_every_documented_command_is_accepted_by_the_cli() -> None:
    commands = _console_commands()
    assert len(commands) >= 15
    runner = CliRunner()
    for command in commands:
        argv = shlex.split(command)
        assert argv[0] == "benchling", command
        if "--help" not in argv:
            argv.append("--help")
        result = runner.invoke(cli.app, argv[1:])
        assert result.exit_code == 0, (command, result.output)
        assert "Usage:" in result.output
