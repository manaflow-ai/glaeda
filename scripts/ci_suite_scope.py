#!/usr/bin/env python3
"""Which script suites a pull request's change cannot reach.

    python3 scripts/ci_suite_scope.py CHANGED_FILE    one suite test path per line, the suites the scripts job skips

scripts/ci-suite-inputs.json lists the repository files the slowest suites read. A listed suite is skipped only when
no changed path is one of its inputs and every changed path is one no listed suite can read without listing it: a
file under scripts/ or config/, Markdown, docs/, .claude/, skills/, or a workflow other than ci.yml. A change to
ci.yml, the list or this file, or to any other path (src/, tools/, Cargo.toml), skips nothing. Unlisted suites
always run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUTS = "scripts/ci-suite-inputs.json"
SELF = "scripts/ci_suite_scope.py"
ALWAYS = {".github/workflows/ci.yml", INPUTS, SELF}


def load(root: Path = ROOT) -> dict[str, list[str]]:
    doc = json.loads((root / INPUTS).read_text())
    return {name: spec["inputs"] for name, spec in doc["suites"].items()}


def reachable_only_by_listing(path: str) -> bool:
    """A path a suite reads only by naming it: its list would hold it."""
    if path in ALWAYS:
        return False
    if path.startswith(("scripts/", "config/", "docs/", ".claude/", "skills/")) or path.endswith(".md"):
        return True
    return path.startswith(".github/")


def skipped(changed: list[str], suites: dict[str, list[str]]) -> list[str]:
    if not all(reachable_only_by_listing(p) for p in changed):
        return []
    touched = set(changed)
    return sorted(name for name, inputs in suites.items() if not touched & set(inputs))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    changed = [line for line in Path(argv[1]).read_text().splitlines() if line]
    for name in skipped(changed, load()):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
