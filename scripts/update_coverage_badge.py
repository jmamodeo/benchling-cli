#!/usr/bin/env python3
"""Refresh the static coverage badge in README.md.

Runs ``coverage json`` (after ``pytest --cov``) unless ``--json`` points at an
existing report, then rewrites the shields.io coverage badge line. Idempotent.

    uv run pytest --cov
    uv run python scripts/update_coverage_badge.py
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BADGE = re.compile(
    r"^\[!\[Coverage\]\(https://img\.shields\.io/badge/coverage-[^)]*\)\]\([^)]*\)$",
    re.MULTILINE,
)
ANCHOR = re.compile(r"^\[!\[Python 3\.11\+\].*$", re.MULTILINE)


def color_for(percent: int) -> str:
    """Map a coverage percentage to a shields.io color name."""
    if percent >= 90:
        return "brightgreen"
    if percent >= 80:
        return "green"
    if percent >= 70:
        return "yellowgreen"
    if percent >= 60:
        return "yellow"
    return "orange"


def read_percent(json_path: Path | None) -> float:
    """Return total percent covered from a coverage JSON report, generating one if none is given."""
    if json_path is None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "coverage.json"
            subprocess.run(
                [sys.executable, "-m", "coverage", "json", "-q", "-o", str(target)],
                check=True,
                cwd=ROOT,
            )
            data = json.loads(target.read_text("utf-8"))
    else:
        data = json.loads(json_path.read_text("utf-8"))
    return float(data["totals"]["percent_covered"])


def badge_line(percent: int) -> str:
    """Render the README markdown for the coverage badge."""
    return (
        f"[![Coverage](https://img.shields.io/badge/coverage-{percent}%25-{color_for(percent)})]"
        "(https://github.com/jmamodeo/benchlingtk/actions/workflows/ci.yml)"
    )


def update_readme(readme: Path, percent: int) -> bool:
    """Replace or insert the badge line in the README; return False if nothing changed.

    Exits if there is no existing badge or anchor line to extend.
    """
    text = readme.read_text("utf-8")
    line = badge_line(percent)
    if BADGE.search(text):
        new = BADGE.sub(line, text, count=1)
    else:
        anchor = ANCHOR.search(text)
        if anchor is None:
            raise SystemExit("README.md has no badge block to extend")
        new = text[: anchor.end()] + "\n" + line + text[anchor.end() :]
    if new == text:
        return False
    readme.write_text(new, "utf-8")
    return True


def main() -> int:
    """Update the README badge from coverage data; returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", type=Path, help="existing coverage JSON report")
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    args = parser.parse_args()
    percent = round(read_percent(args.json))
    changed = update_readme(args.readme, percent)
    print(f"coverage {percent}%: README.md {'updated' if changed else 'already current'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
