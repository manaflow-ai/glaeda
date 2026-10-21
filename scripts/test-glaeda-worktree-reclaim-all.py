#!/usr/bin/env python3
"""Discovery regressions for the hourly worktree and local-branch sweep."""

import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().with_name("glaeda-worktree-reclaim-all")


class ReclaimDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.projects = self.root / "Projects with spaces"
        self.projects.mkdir()
        self.capture = self.root / "argv"
        self.reclaim = self.root / "reclaim"
        self.reclaim.write_text('#!/bin/bash\nprintf "%s\\0" "$@" > "$CAPTURE"\n')
        self.reclaim.chmod(0o755)
        self.env = {
            "HOME": str(self.root), "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GLAEDA_PROJECTS_ROOT": str(self.projects),
            "GLAEDA_WORKTREE_RECLAIM": str(self.reclaim),
            "CAPTURE": str(self.capture),
            # The real disk's fullness must not change the argv the tests compare.
            "GLAEDA_RECLAIM_PRESSURE_PERCENT": "101",
        }

    def git(self, repo, *args):
        return subprocess.run(
            ["/usr/bin/git", "-C", str(repo), *args], env=self.env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            check=True, timeout=10,
        ).stdout

    def repository(self, name="demo", branch="main"):
        repo = self.projects / name
        repo.mkdir()
        self.git(repo, "init", "-q", "-b", branch)
        self.git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "initial", "--allow-empty")
        return repo

    def sweep(self, *args):
        subprocess.run(
            ["/bin/bash", str(SCRIPT), *args], env=self.env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            check=True, timeout=20,
        )
        return self.capture.read_bytes().decode().rstrip("\0").split("\0") if self.capture.exists() else []

    def test_last_worktree_removal_still_discovers_leftover_branch(self):
        repo = self.repository()
        checkout = self.root / "linked"
        self.git(repo, "worktree", "add", "-qb", "finished", str(checkout))
        self.git(repo, "worktree", "remove", str(checkout))
        before = self.git(repo, "show-ref")
        self.assertEqual(self.sweep("--output", "json"),
                         ["--branches", "--output", "json", "--repository", str(repo)])
        self.assertEqual(self.git(repo, "show-ref"), before)
        self.assertFalse(checkout.exists())

    def test_dirty_repository_is_only_discovered_never_cleaned_by_wrapper(self):
        repo = self.repository("dirty")
        self.git(repo, "branch", "unfinished")
        dirty = repo / "keep.txt"
        dirty.write_text("uncommitted work\n")
        self.assertIn(str(repo), self.sweep())
        self.assertEqual(dirty.read_text(), "uncommitted work\n")
        self.assertEqual(self.git(repo, "status", "--porcelain"), "?? keep.txt\n")

    def test_linked_worktree_still_selects_repository(self):
        repo = self.repository()
        self.git(repo, "worktree", "add", "--detach", str(self.root / "linked"))
        self.assertEqual(self.sweep(), ["--branches", "--repository", str(repo)])

    def test_main_and_master_only_do_not_consume_repository_budget(self):
        repo = self.repository()
        self.git(repo, "branch", "master")
        self.assertEqual(self.sweep(), [])

    def test_third_sorted_branch_is_discovered_after_main_and_master(self):
        repo = self.repository()
        self.git(repo, "branch", "master")
        self.git(repo, "branch", "zzz-finished")
        self.assertIn(str(repo), self.sweep("--apply"))


    def test_a_checkout_one_folder_down_is_discovered(self):
        nested = self.projects / "personal-build"
        nested.mkdir()
        repo = nested / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q", "-b", "main")
        self.git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "initial", "--allow-empty")
        self.git(repo, "worktree", "add", "-qb", "lane", str(nested / "lane"))
        (self.projects / "alias").symlink_to(nested)
        argv = self.sweep()
        self.assertEqual(argv.count(str(repo)), 1)
        self.assertNotIn(str(nested / "lane"), argv)

    def test_disk_pressure_shortens_the_unfinished_window(self):
        repo = self.repository()
        self.git(repo, "worktree", "add", "-qb", "lane", str(self.root / "lane"))
        self.env["GLAEDA_RECLAIM_PRESSURE_PERCENT"] = "0"
        argv = self.sweep()
        self.assertEqual(argv[argv.index("--minimum-idle-seconds") + 1], "43200")
        # A window the caller passed wins; no pressure, no change.
        argv = self.sweep("--minimum-idle-seconds", "7200")
        self.assertEqual(argv.count("--minimum-idle-seconds"), 1)
        self.env["GLAEDA_RECLAIM_PRESSURE_PERCENT"] = "101"
        self.assertNotIn("--minimum-idle-seconds", self.sweep())


if __name__ == "__main__":
    unittest.main()
