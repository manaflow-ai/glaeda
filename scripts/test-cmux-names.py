#!/usr/bin/env python3
"""cmux-disk, cmux-procs and cmux-disk-registry are the primary names of glaeda-disk, glaeda-procs and
glaeda-disk-registry (Leo, 2026-11-25: "start migrating off glaeda and into cmux-prefixed naming tooling").

Installed the way glaeda-mini-setup installs them (both files in one bin directory) and run as callers do: by name on
PATH, by path, and as `python3 PATH` (launchd agents and glaeda-update's health check). The old name prints nothing
about the rename and counts its uses in ~/.local/state/cmux-names/OLD.json; the new name counts nothing."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
NAMES = {"glaeda-disk": "cmux-disk", "glaeda-procs": "cmux-procs", "glaeda-disk-registry": "cmux-disk-registry"}


class CmuxNamesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.bin = self.home / ".local/bin"
        self.bin.mkdir(parents=True)
        for old, new in NAMES.items():
            shutil.copy2(SCRIPTS / old, self.bin / old)
            shutil.copy2(SCRIPTS / new, self.bin / new)
        shutil.copy2(SCRIPTS / "intent.py", self.bin / "intent.py") if (SCRIPTS / "intent.py").is_file() else None
        self.env = {"HOME": os.fspath(self.home), "PATH": f"{self.bin}:/usr/bin:/bin"}
        self.state = self.home / ".local/state/cmux-names"

    def run_as(self, name: str, *args: str, how: str = "name") -> subprocess.CompletedProcess:
        argv = {"name": [name], "path": [os.fspath(self.bin / name)],
                "python": [sys.executable, os.fspath(self.bin / name)]}[how]
        return subprocess.run([*argv, *args], capture_output=True, text=True, env=self.env, timeout=120,
                              stdin=subprocess.DEVNULL)

    def test_both_names_run_each_saying_its_own_name(self) -> None:
        for old, new in NAMES.items():
            for how in ("name", "path", "python"):
                with self.subTest(old=old, how=how):
                    a, b = self.run_as(old, "--help", how=how), self.run_as(new, "--help", how=how)
                    self.assertEqual((a.returncode, b.returncode), (0, 0), (a.stderr[-400:], b.stderr[-400:]))
                    self.assertTrue(a.stdout.startswith(f"usage: {old}"), a.stdout[:80])
                    self.assertTrue(b.stdout.startswith(f"usage: {new}"), b.stdout[:80])
                    self.assertEqual(a.stderr, "")  # the old name is silent: parsers of its output see nothing new
                    self.assertEqual(b.stderr, "")

    def test_old_name_counts_its_uses_and_new_name_does_not(self) -> None:
        for old, new in NAMES.items():
            with self.subTest(old=old):
                self.run_as(new, "--help")
                self.assertFalse((self.state / f"{old}.json").exists())
                self.run_as(old, "--help")
                self.run_as(old, "--help", how="python")
                data = json.loads((self.state / f"{old}.json").read_text())
                self.assertEqual((data["old"], data["new"]), (old, new))
                self.assertEqual(sum(data["days"].values()), 2)
                self.assertTrue(data["callers"] and data["last"])

    def test_counter_failing_costs_the_call_nothing(self) -> None:
        (self.home / ".local/state").mkdir(parents=True)
        (self.home / ".local/state/cmux-names").write_text("not a directory")
        result = self.run_as("glaeda-procs", "--help")
        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_new_name_exits_with_the_programs_code(self) -> None:
        a = self.run_as("glaeda-disk-registry", "no-such-command")
        b = self.run_as("cmux-disk-registry", "no-such-command")
        self.assertEqual(a.returncode, b.returncode)
        self.assertNotEqual(b.returncode, 0)
        self.assertIn("usage: cmux-disk-registry", b.stderr)


if __name__ == "__main__":
    unittest.main()
