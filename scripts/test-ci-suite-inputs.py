#!/usr/bin/env python3
"""scripts/ci-suite-inputs.json lists the files the slowest script suites read, so a pull request that touches none of
them skips the suite. This keeps the list closed as suites grow (the test file's own repository paths and every
listed module's imports are listed) and pins scripts/ci_suite_scope.py's rule."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
# scripts/X, config/X or docs/X in text, or ROOT / "scripts" / "X" as the tests build paths.
PATH_RE = re.compile(r'''\b(scripts|config|docs)/([A-Za-z0-9_.-]+)|["'](scripts|config|docs)["']\s*/\s*["']([A-Za-z0-9_.-]+)["']''')
IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z0-9_]+)", re.M)

_spec = importlib.util.spec_from_file_location("ci_suite_scope", ROOT / "scripts" / "ci_suite_scope.py")
scope = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scope)


def tracked() -> set[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True).stdout
    return set(out.split())


class SuiteInputsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.suites = scope.load()
        self.files = tracked()
        self.ci = CI.read_text()

    def test_every_listed_suite_runs_in_the_scripts_job(self) -> None:
        self.assertTrue(self.suites)
        for name in self.suites:
            self.assertRegex(self.ci, rf"\n\s+suite (env \S+ )*python3 {re.escape(name)}\n", name)

    def test_inputs_are_tracked_sorted_and_hold_the_test(self) -> None:
        for name, inputs in self.suites.items():
            self.assertIn(name, inputs)
            self.assertEqual(inputs, sorted(set(inputs)), f"{name}: keep the inputs sorted, once each")
            for path in inputs:
                self.assertIn(path, self.files, f"{name}: {path} is not a tracked file")

    def test_test_file_paths_and_module_imports_are_listed(self) -> None:
        for name, inputs in self.suites.items():
            listed = set(inputs)
            want = set()
            for a, b, c, d in PATH_RE.findall((ROOT / name).read_text(errors="replace")):
                path = f"{a}/{b}" if a else f"{c}/{d}"
                if path in self.files:
                    want.add(path)
            for path in listed:
                for module in IMPORT_RE.findall((ROOT / path).read_text(errors="replace")):
                    if f"scripts/{module}.py" in self.files:
                        want.add(f"scripts/{module}.py")
            self.assertEqual(sorted(want - listed), [],
                             f"{name} reads these but scripts/ci-suite-inputs.json does not list them")

    def test_rule(self) -> None:
        suites = {"scripts/test-a.py": ["scripts/a", "scripts/test-a.py"],
                  "scripts/test-b.py": ["config/b.json", "scripts/test-b.py"]}
        self.assertEqual(scope.skipped(["scripts/a"], suites), ["scripts/test-b.py"])
        self.assertEqual(scope.skipped(["scripts/other", "docs/x.md", "README.md"], suites),
                         ["scripts/test-a.py", "scripts/test-b.py"])
        self.assertEqual(scope.skipped(["config/b.json", "scripts/test-a.py"], suites), [])
        for anything in ("src/main.rs", "Cargo.toml", "tools/x/y.go", ".github/workflows/ci.yml",
                         "scripts/ci-suite-inputs.json", "scripts/ci_suite_scope.py"):
            self.assertEqual(scope.skipped(["scripts/other", anything], suites), [], anything)
        self.assertEqual(scope.skipped([".github/workflows/release.yml"], suites), sorted(suites))


if __name__ == "__main__":
    unittest.main()
