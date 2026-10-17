#!/usr/bin/env python3
"""Fixture contracts for the Projects layout planner."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("glaeda_projects", os.fspath(ROOT / "scripts/glaeda-projects"))
spec = importlib.util.spec_from_loader(loader.name, loader)
gp = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gp
loader.exec_module(gp)


def run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.DEVNULL)


class ProjectsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.projects = self.home / "Projects"
        self.projects.mkdir(parents=True)
        self.old = (gp.HOME, gp.PROJECTS, gp.WORKTREES, gp.SCRATCH)
        gp.HOME, gp.PROJECTS = self.home, self.projects
        gp.WORKTREES, gp.SCRATCH = self.projects / "worktrees", self.projects / "scratch"

    def tearDown(self) -> None:
        gp.HOME, gp.PROJECTS, gp.WORKTREES, gp.SCRATCH = self.old
        self.tmp.cleanup()

    def repo(self, name: str = "demo") -> Path:
        repo = self.projects / name
        repo.mkdir()
        run("init", "-q", cwd=repo)
        run("config", "user.email", "test@example.invalid", cwd=repo)
        run("config", "user.name", "Test", cwd=repo)
        (repo / "README").write_text("ok\n")
        run("add", "README", cwd=repo)
        run("commit", "-qm", "initial", cwd=repo)
        return repo

    def test_dry_run_is_default_and_only_old_linked_worktree_is_planned(self) -> None:
        repo = self.repo()
        remote = Path(self.tmp.name) / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True)
        run("remote", "add", "origin", str(remote), cwd=repo)
        run("push", "-q", "-u", "origin", "HEAD", cwd=repo)
        legacy = self.projects / "demo-worktrees" / "old"
        run("worktree", "add", "-q", str(legacy), "-b", "old", "HEAD", cwd=repo)
        old = time.time() - 48 * 3600
        for item in legacy.rglob("*"):
            os.utime(item, (old, old), follow_symlinks=False)
        os.utime(legacy, (old, old))
        with mock.patch.object(gp, "process_evidence", return_value=([], "")), \
                mock.patch.object(gp, "session_mentions", return_value=False):
            actions = gp.plan()
            self.assertTrue(any(a.kind == "worktree" and a.status == "planned" for a in actions))
            self.assertTrue(all(not (a.kind == "worktree" and a.status == "applied") for a in actions))

    def test_apply_moves_clean_linked_worktree_into_canonical_root(self) -> None:
        repo = self.repo()
        legacy = self.projects / "loose-worktree"
        run("worktree", "add", "-q", str(legacy), "-b", "feature", "HEAD", cwd=repo)
        old = time.time() - 48 * 3600
        for item in legacy.rglob("*"):
            os.utime(item, (old, old), follow_symlinks=False)
        os.utime(legacy, (old, old))
        with mock.patch.object(gp, "process_evidence", return_value=([], "")), \
                mock.patch.object(gp, "session_mentions", return_value=False):
            actions = [a for a in gp.plan() if a.kind == "worktree"]
            gp.apply(actions)
        self.assertFalse(legacy.exists())
        self.assertTrue((self.projects / "worktrees/demo/loose-worktree/.git").is_file())

    def test_dirty_submodule_status_is_a_guard(self) -> None:
        repo = self.repo()
        self.assertEqual(gp.submodule_status(repo), "")
        (repo / "dirty").write_text("keep\n")
        self.assertIn("uncommitted", gp.submodule_status(repo))

    def test_duplicate_clone_keeps_unpublished_local_refs(self) -> None:
        repo = self.repo()
        remote = Path(self.tmp.name) / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True)
        run("remote", "add", "origin", str(remote), cwd=repo)
        run("push", "-q", "-u", "origin", "HEAD", cwd=repo)
        run("checkout", "-qb", "private", cwd=repo)
        (repo / "private").write_text("keep\n")
        run("add", "private", cwd=repo)
        run("commit", "-qm", "private", cwd=repo)
        self.assertIn("not confirmed by a remote", gp.clean_pushed(repo))

    def test_duplicate_clone_with_external_linked_worktree_is_kept(self) -> None:
        repo = self.repo()
        external = Path(self.tmp.name) / "external-worktree"
        run("worktree", "add", "-q", str(external), "-b", "external", "HEAD", cwd=repo)
        self.assertIn("backs linked worktrees", gp.clean_pushed(repo))

    def test_referenced_main_checkout_is_kept(self) -> None:
        repo = self.repo()
        remote = Path(self.tmp.name) / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True)
        run("remote", "add", "origin", str(remote), cwd=repo)
        run("push", "-q", "-u", "origin", "HEAD", cwd=repo)
        clone = self.projects / "terminal-kit"
        subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
        old = time.time() - 48 * 3600
        for item in clone.rglob("*"):
            os.utime(item, (old, old), follow_symlinks=False)
        os.utime(clone, (old, old))
        (self.home / ".zshrc").write_text(f"source {clone}/shell.zsh\n")
        action = next(a for a in gp.plan() if Path(a.source) == clone)
        self.assertEqual((action.kind, action.status), ("skip", "skipped"))
        self.assertIn("referenced", action.reason)

    def test_main_checkout_reference_sources_are_all_checked(self) -> None:
        repo = self.repo()
        sources = [
            (".codex/hooks.json", "hooks"), (".codex/config.toml", "config"),
            (".claude/settings.json", "claude"), (".zshrc", "zsh"),
            (".bashrc", "bash"), (".profile", "profile"),
            ("Library/LaunchAgents/example.plist", "launch"),
            (".config/systemd/user/example.service", "systemd"),
        ]
        for relative, marker in sources:
            config = self.home / relative
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(f"{marker} = {repo}/bin/tool\n")
            self.assertIn("referenced", gp.referenced_main_checkout(repo), relative)
            config.unlink()
        local_bin = self.home / ".local/bin"
        local_bin.mkdir(parents=True)
        (local_bin / "tool").symlink_to(repo / "bin/tool")
        self.assertIn("~/.local/bin", gp.referenced_main_checkout(repo))

    def test_apply_aborts_when_process_evidence_is_unavailable(self) -> None:
        action = gp.Action("scratch", str(self.projects / "stale"), str(self.projects / "scratch/stale"), "old")
        with mock.patch.object(gp, "process_evidence", side_effect=gp.EvidenceUnavailable("cannot list processes")):
            gp.apply([action])
        self.assertEqual((action.status, action.reason), ("skipped", "cannot list processes"))

    def test_legacy_worktree_discovery_resolves_relative_common_dir(self) -> None:
        repo = self.repo()
        legacy = self.projects / "legacy-worktrees" / "old"
        run("worktree", "add", "-q", str(legacy), "-b", "old", "HEAD", cwd=repo)
        old = time.time() - 48 * 3600
        for item in legacy.rglob("*"):
            os.utime(item, (old, old), follow_symlinks=False)
        os.utime(legacy, (old, old))
        with mock.patch.object(gp, "process_evidence", return_value=([], "")), \
                mock.patch.object(gp, "session_mentions", return_value=False):
            actions = gp.plan()
        self.assertTrue(any(a.kind == "worktree" and Path(a.source) == legacy for a in actions))


if __name__ == "__main__":
    unittest.main()
