#!/usr/bin/env python3
"""scripts/ci-job-inputs.json lists the files runner-tests and glaeda-gh-tests read; a pull request that changes none
of them (and nothing outside scripts/ and config/) skips that suite. This keeps the list whole as the suites grow:
the test file's own scripts/ and config/ paths and every listed module's imports must be listed too."""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "scripts" / "ci-job-inputs.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
# scripts/X or config/X in text, or ROOT / "scripts" / "X" as the tests build paths.
PATH_RE = re.compile(r'''\b(scripts|config)/([A-Za-z0-9_.-]+)|["'](scripts|config)["']\s*/\s*["']([A-Za-z0-9_.-]+)["']''')
IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z0-9_]+)", re.M)


def tracked() -> set[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "scripts", "config"], capture_output=True, text=True,
                         check=True).stdout
    return set(out.split())


def named_paths(text: str, files: set[str]) -> set[str]:
    found = set()
    for a, b, c, d in PATH_RE.findall(text):
        path = f"{a}/{b}" if a else f"{c}/{d}"
        if path in files:
            found.add(path)
    return found


def imports(text: str, files: set[str]) -> set[str]:
    return {f"scripts/{m}.py" for m in IMPORT_RE.findall(text) if f"scripts/{m}.py" in files}


class CiJobInputsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.jobs = {k: v for k, v in json.loads(INPUTS.read_text()).items() if not k.startswith("_")}
        self.files = tracked()
        self.ci = CI.read_text()

    def test_lists_both_sharded_suites(self) -> None:
        self.assertEqual(sorted(self.jobs), ["glaeda-gh-tests", "runner-tests"])

    def test_every_input_is_a_tracked_file(self) -> None:
        for job, spec in self.jobs.items():
            for path in spec["inputs"]:
                self.assertIn(path, self.files, f"{job}: {path} is not a tracked file under scripts/ or config/")
            self.assertEqual(spec["inputs"], sorted(set(spec["inputs"])), f"{job}: keep the inputs sorted, once each")

    def test_job_runs_the_listed_test_and_gates_on_its_output(self) -> None:
        for job, output in (("runner-tests", "runner"), ("glaeda-gh-tests", "gh")):
            spec = self.jobs[job]
            self.assertIn(spec["test"], spec["inputs"])
            self.assertIn("scripts/unittest-shard", spec["inputs"])
            block = self.ci.split(f"\n  {job}:\n", 1)[1].split("\n  # ", 1)[0]
            self.assertIn(f"needs.changes.outputs.{output} == 'true'", block)
            self.assertIn(f"scripts/unittest-shard {spec['test']}", block)
            self.assertIn(f"'.\"{job}\".inputs[]' scripts/ci-job-inputs.json", self.ci)

    def test_test_file_paths_and_module_imports_are_listed(self) -> None:
        for job, spec in self.jobs.items():
            listed = set(spec["inputs"])
            want = named_paths((ROOT / spec["test"]).read_text(errors="replace"), self.files)
            for path in listed:
                want |= imports((ROOT / path).read_text(errors="replace"), self.files)
            self.assertEqual(sorted(want - listed), [],
                             f"{job} reads these but scripts/ci-job-inputs.json does not list them")


if __name__ == "__main__":
    unittest.main()
