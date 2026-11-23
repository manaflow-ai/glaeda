#!/usr/bin/env python3
"""A hanging script suite is killed and named, and the suites behind it still run.

Verify's `scripts` job runs about 97 suites serially in one step under a 15 min job ceiling. A suite
with no bound of its own spends the whole remaining ceiling: in run 45089631085
`scripts/test-glaeda-gh.py` hung for 729 s (healthy: 45 s), the ceiling killed the job, 48 suites
never ran, and the job reported no verdict about the code at all.

These tests lift the shipped `suite()` helper out of `.github/workflows/ci.yml` rather than copying
it, so they cannot drift from what CI runs.
"""
from __future__ import annotations

import re
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

BOUND = "suite_seconds=${GLAEDA_SUITE_TIMEOUT_SECONDS:-300}"


def helper() -> str:
    """The workflow's own bound line and `suite()` body, dedented into a runnable fragment."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == BOUND]
    if len(starts) != 1:
        raise AssertionError(
            f"{WORKFLOW.name} carries {len(starts)} copies of the per-suite bound {BOUND!r}, want 1: "
            "a suite with no bound hangs to the job ceiling with no verdict")
    start = starts[0]
    opener = next(i for i in range(start, len(lines)) if lines[i].strip() == "suite() {")
    indent = len(lines[opener]) - len(lines[opener].lstrip())
    close = next(i for i in range(opener + 1, len(lines)) if lines[i] == " " * indent + "}")
    return textwrap.dedent("\n".join(lines[start:close + 1]))


def run_suites(body: str, *, bound: str = "1") -> subprocess.CompletedProcess[str]:
    script = "\n".join([
        "set -e",
        "failed=()",
        "timings=()",
        helper(),
        body,
        'printf "FAILED=%s\\n" "${#failed[@]}"',
        'for f in "${failed[@]}"; do printf "ROW=%s\\n" "$f"; done',
    ])
    return subprocess.run(
        ["/bin/bash", "-c", script],
        env={"PATH": "/usr/bin:/bin", "GLAEDA_SUITE_TIMEOUT_SECONDS": bound},
        capture_output=True, text=True, timeout=120, check=False)


class SuiteBound(unittest.TestCase):
    def test_a_hanging_suite_is_killed_and_the_suites_behind_it_still_run(self) -> None:
        done = run_suites('suite sleep 600\nsuite true\necho "REACHED_END=1"')
        self.assertIn("REACHED_END=1", done.stdout, done.stderr)
        self.assertIn("FAILED=1", done.stdout, done.stdout)
        self.assertIn("ROW=sleep 600 (hit its 1s bound)", done.stdout, done.stdout)
        self.assertIn("::error title=Suite hit its 1s bound::sleep 600", done.stdout, done.stdout)

    def test_the_kill_is_bounded_in_wall_clock_not_by_the_job_ceiling(self) -> None:
        done = run_suites("suite sleep 600")
        took = re.search(r"suite took (\d+)s of its 1s bound: sleep 600", done.stdout)
        self.assertIsNotNone(took, done.stdout)
        # 1 s bound plus the 10 s SIGKILL grace: the old behaviour ran to the job's 15 min ceiling.
        self.assertLessEqual(int(took.group(1)), 12, done.stdout)

    def test_a_suite_that_fails_on_its_own_keeps_its_exit_code(self) -> None:
        done = run_suites("suite sh -c 'exit 3'", bound="60")
        self.assertIn("::error title=Suite failed (exit 3)::sh -c exit 3", done.stdout, done.stdout)
        self.assertIn("FAILED=1", done.stdout, done.stdout)

    def test_a_passing_suite_is_timed_and_not_counted_failed(self) -> None:
        done = run_suites("suite true", bound="60")
        self.assertIn("suite took 0s of its 60s bound: true", done.stdout, done.stdout)
        self.assertIn("FAILED=0", done.stdout, done.stdout)

    def test_the_workflow_still_bounds_every_suite_through_one_helper(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("timeout --kill-after=10s \"$suite_seconds\" \"$@\"", text)
        # Every suite goes through the helper, so no call site can skip the bound.
        self.assertEqual(text.count("suite() {"), 1, "more than one suite helper: one of them is unbounded")
        self.assertGreater(len(re.findall(r"^ +suite (?:env |python3 |bash )", text, re.M)), 50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
