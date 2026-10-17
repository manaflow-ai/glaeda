#!/usr/bin/env python3
"""Fixture contracts for canonical worktree creation."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorktreeTest(unittest.TestCase):
    def test_no_build_adds_under_canonical_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            projects = home / "Projects"
            repo = projects / "demo"
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "README").write_text("ok\n")
            subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
            result = subprocess.run([str(ROOT / "scripts/glaeda-worktree"), "add", "--no-build",
                                     str(repo), "feature"],
                                    env={**os.environ, "HOME": str(home), "GIT_DIR": str(home / "foreign.git")},
                                    capture_output=True, text=True, check=True)
            target = Path(result.stdout.strip())
            self.assertEqual(target, projects / "worktrees/demo/feature")
            self.assertTrue((target / "README").is_file())

    def test_build_initializes_ghostty_from_the_canonical_object_store(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            projects = home / "Projects"
            projects.mkdir(parents=True)
            env = {**os.environ, "HOME": str(home), "GIT_ALLOW_PROTOCOL": "file"}

            ghostty = projects / "ghostty-source"
            ghostty.mkdir()
            for args in (("init", "-q", "-b", "main", str(ghostty)),):
                subprocess.run(["git", *args], check=True, env=env)
            for key, value in (("user.email", "test@example.invalid"), ("user.name", "Test")):
                subprocess.run(["git", "-C", str(ghostty), "config", key, value], check=True, env=env)
            (ghostty / "README").write_text("ghostty\n")
            subprocess.run(["git", "-C", str(ghostty), "add", "README"], check=True, env=env)
            subprocess.run(["git", "-C", str(ghostty), "commit", "-qm", "initial"], check=True, env=env)

            repo = projects / "demo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
            for key, value in (("user.email", "test@example.invalid"), ("user.name", "Test")):
                subprocess.run(["git", "-C", str(repo), "config", key, value], check=True, env=env)
            (repo / "README").write_text("demo\n")
            subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, env=env)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True, env=env)
            subprocess.run(["git", "-C", str(repo), "submodule", "add", "-q", str(ghostty), "ghostty"],
                           check=True, env=env)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add ghostty"], check=True, env=env)

            result = subprocess.run([str(ROOT / "scripts/glaeda-worktree"), "add", "--build", str(repo), "feature"],
                                    env=env, capture_output=True, text=True)
            if result.returncode:
                raise AssertionError(result.stderr)
            target = Path(result.stdout.strip())
            self.assertTrue((target / "ghostty/README").is_file())
            gitfile = target / "ghostty/.git"
            gitdir = (gitfile.parent / Path(gitfile.read_text().split(" ", 1)[1].strip())).resolve()
            alternates = (gitdir / "objects/info/alternates").read_text().strip()
            self.assertEqual(Path(alternates), repo / ".git/modules/ghostty/objects")


if __name__ == "__main__":
    unittest.main()
