#!/usr/bin/env python3
"""cmux-gh is glaeda-gh's primary name and glaeda-gh still works: both run the same program, each saying its own name.

Installed the way glaeda-mini-setup installs them (both files in one bin directory), run as a caller would."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class CmuxGhAliasTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.bin = self.home / ".local/bin"
        self.bin.mkdir(parents=True)
        for name in ("glaeda-gh", "cmux-gh"):
            shutil.copy2(SCRIPTS / name, self.bin / name)
        self.env = {"HOME": os.fspath(self.home), "PATH": f"{self.bin}:/usr/bin:/bin"}

    def run_name(self, name: str, *args: str, by_path: bool = False) -> subprocess.CompletedProcess:
        command = os.fspath(self.bin / name) if by_path else name
        return subprocess.run([command, *args], capture_output=True, text=True, env=self.env, timeout=60)

    def test_both_names_run_the_same_help_each_in_its_own_name(self) -> None:
        for by_path in (True, False):
            old = self.run_name("glaeda-gh", "--help", by_path=by_path)
            new = self.run_name("cmux-gh", "--help", by_path=by_path)
            self.assertEqual((old.returncode, new.returncode), (0, 0), (old.stderr, new.stderr))
            self.assertTrue(old.stdout.startswith("usage: glaeda-gh"), old.stdout[:80])
            self.assertTrue(new.stdout.startswith("usage: cmux-gh"), new.stdout[:80])
            self.assertIn("cmux-gh (formerly glaeda-gh", new.stdout)
            self.assertNotIn("formerly", old.stdout)  # the old name says nothing about the rename

    def test_a_mistyped_call_names_the_form_that_runs_under_the_name_typed(self) -> None:
        old = self.run_name("glaeda-gh", "wait", "bogus")
        new = self.run_name("cmux-gh", "wait", "bogus")
        self.assertNotEqual(old.returncode, 0)
        self.assertNotEqual(new.returncode, 0)
        self.assertIn("next: glaeda-gh wait --help", old.stderr)
        self.assertIn("next: cmux-gh wait --help", new.stderr)

    def test_a_local_wait_runs_under_both_names(self) -> None:
        # `wait file` needs no daemon and no GitHub: it proves each name runs a real command to its end
        target = self.home / "ready"
        target.write_text("x")
        for name in ("glaeda-gh", "cmux-gh"):
            result = self.run_name(name, "wait", "file", os.fspath(target), "--exists", "--timeout", "20")
            self.assertEqual(result.returncode, 0, (name, result.stdout, result.stderr))

    def test_the_shim_finds_glaeda_gh_beside_it_not_on_the_path(self) -> None:
        other = self.home / "elsewhere"
        other.mkdir()
        shutil.copy2(SCRIPTS / "cmux-gh", other / "cmux-gh")
        result = subprocess.run([os.fspath(other / "cmux-gh"), "--help"], capture_output=True, text=True,
                                env=self.env, timeout=60)
        self.assertNotEqual(result.returncode, 0)  # no glaeda-gh beside this copy: it never falls back to a stray one


if __name__ == "__main__":
    unittest.main()
