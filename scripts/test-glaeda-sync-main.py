#!/usr/bin/env python3
"""Contract tests for scripts/glaeda-sync-main, against scratch bare repositories.

Each test makes two bare repositories standing in for teamleaderleo/glaeda (upstream) and
manaflow-ai/glaeda (mirror). Both refuse non-fast-forward updates and deletions, as a server that
must never see a force push would, and every git argv the sync runs is recorded.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "glaeda-sync-main"
loader = importlib.machinery.SourceFileLoader("glaeda_sync_main", os.fspath(SCRIPT))
spec = importlib.util.spec_from_loader("glaeda_sync_main", loader)
gs = importlib.util.module_from_spec(spec)
sys.modules["glaeda_sync_main"] = gs
loader.exec_module(gs)

# Hermetic git: no user or system config, a fixed identity for the fixtures' own commits.
GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}
FORCE_MARKERS = ("--force", "-f", "--force-with-lease", "--force-if-includes", "--mirror", "--delete", "-d", "--prune")


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True)
    return done.stdout.strip()


class Fixture:
    """Two bare remotes sharing one base commit, plus a clone of each to commit from."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[list[str]] = []
        self.on_push = None  # called with the push argv before it runs, to simulate a race
        self.remotes = {}
        self.clones = {}
        for side in ("upstream", "mirror"):
            bare = root / f"{side}.git"
            git(root, "init", "-q", "--bare", "-b", "main", str(bare))
            git(bare, "config", "receive.denyNonFastForwards", "true")
            git(bare, "config", "receive.denyDeletes", "true")
            self.remotes[side] = bare
        seed = root / "seed"
        git(root, "init", "-q", "-b", "main", str(seed))
        (seed / "README").write_text("base\n")
        (seed / "shared.txt").write_text("one\ntwo\nthree\n")
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "base")
        for side, bare in self.remotes.items():
            git(seed, "push", "-q", str(bare), "main:refs/heads/main")
            clone = root / f"{side}-clone"
            git(root, "clone", "-q", str(bare), str(clone))
            self.clones[side] = clone
        self.work = root / "work"

    def head(self, side: str) -> str:
        return git(self.remotes[side], "rev-parse", "refs/heads/main")

    def commit(self, side: str, path: str, content: str, message: str | None = None) -> str:
        clone = self.clones[side]
        git(clone, "pull", "-q", "--ff-only", "origin", "main")
        (clone / path).write_text(content)
        git(clone, "add", path)
        git(clone, "commit", "-q", "-m", message or f"{side}: {path}")
        git(clone, "push", "-q", "origin", "HEAD:refs/heads/main")
        return git(clone, "rev-parse", "HEAD")

    def runner(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.on_push is not None and argv[1:2] == ["push"]:
            hook, self.on_push = self.on_push, None
            hook(argv)
        env = {**GIT_ENV, **{k: v for k, v in (kwargs.pop("env", None) or {}).items() if k.startswith("GIT_")}}
        return subprocess.run(argv, env=env, **kwargs)

    def sync(self, **kwargs):
        return gs.sync(
            upstream=str(self.remotes["upstream"]),
            mirror=str(self.remotes["mirror"]),
            work=self.work,
            runner=self.runner,
            **kwargs,
        )

    def pushes(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv[1:2] == ["push"]]


class SyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self.tmp.name))

    def tearDown(self) -> None:
        for argv in self.fx.pushes():
            self.assert_not_force(argv)
        self.tmp.cleanup()

    def assert_not_force(self, argv: list[str]) -> None:
        for arg in argv[2:]:
            self.assertNotIn(arg, FORCE_MARKERS, argv)
            self.assertFalse(arg.startswith("+"), argv)
            self.assertFalse(arg.startswith("--force"), argv)
            self.assertFalse(arg.startswith(":"), argv)  # a delete refspec

    def test_equal_mains_are_a_no_op(self) -> None:
        before = self.fx.head("upstream")
        result = self.fx.sync()
        self.assertEqual(result.outcome, "in-sync")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.fx.pushes(), [])
        self.assertEqual(self.fx.head("upstream"), before)
        self.assertEqual(self.fx.head("mirror"), before)

    def test_mirror_behind_fast_forwards_the_mirror(self) -> None:
        new = self.fx.commit("upstream", "a.txt", "a\n")
        result = self.fx.sync()
        self.assertEqual(result.outcome, "fast-forward-mirror")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.fx.head("mirror"), new)
        self.assertEqual(self.fx.head("upstream"), new)
        self.assertEqual(len(self.fx.pushes()), 1)
        self.assertEqual(self.fx.pushes()[0][-2:], [str(self.fx.remotes["mirror"]), f"{new}:refs/heads/main"])

    def test_upstream_behind_fast_forwards_upstream(self) -> None:
        new = self.fx.commit("mirror", "b.txt", "b\n")
        result = self.fx.sync()
        self.assertEqual(result.outcome, "fast-forward-upstream")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.fx.head("upstream"), new)
        self.assertEqual(self.fx.head("mirror"), new)
        self.assertEqual(len(self.fx.pushes()), 1)

    def test_diverged_clean_merge_fast_forwards_both_to_one_merge(self) -> None:
        up = self.fx.commit("upstream", "a.txt", "a\n")
        mf = self.fx.commit("mirror", "b.txt", "b\n")
        result = self.fx.sync()
        self.assertEqual(result.outcome, "merged")
        self.assertEqual(result.exit_code, 0)
        merge = result.merge
        self.assertEqual(self.fx.head("upstream"), merge)
        self.assertEqual(self.fx.head("mirror"), merge)
        remote = self.fx.remotes["upstream"]
        self.assertEqual(git(remote, "rev-parse", f"{merge}^1"), up)
        self.assertEqual(git(remote, "rev-parse", f"{merge}^2"), mf)
        message = git(remote, "log", "-1", "--format=%B", merge)
        self.assertIn(up, message)
        self.assertIn(mf, message)
        identity = git(remote, "log", "-1", "--format=%an <%ae>|%cn <%ce>", merge)
        self.assertEqual(identity, f"{gs.IDENTITY_NAME} <{gs.IDENTITY_EMAIL}>|{gs.IDENTITY_NAME} <{gs.IDENTITY_EMAIL}>")
        self.assertEqual(git(remote, "show", f"{merge}:a.txt"), "a")
        self.assertEqual(git(remote, "show", f"{merge}:b.txt"), "b")
        self.assertEqual(len(self.fx.pushes()), 2)

    def test_the_merge_commit_is_deterministic(self) -> None:
        self.fx.commit("upstream", "a.txt", "a\n")
        self.fx.commit("mirror", "b.txt", "b\n")
        first = self.fx.sync(dry_run=True)
        self.fx.work = Path(self.tmp.name) / "work-2"
        second = self.fx.sync(dry_run=True)
        self.assertEqual(first.outcome, "merged")
        self.assertEqual(first.merge, second.merge)

    def test_conflict_changes_neither_side(self) -> None:
        up = self.fx.commit("upstream", "shared.txt", "one\nUP\nthree\n")
        mf = self.fx.commit("mirror", "shared.txt", "one\nMF\nthree\n")
        self.fx.commit("upstream", "only-up.txt", "x\n")
        up = self.fx.head("upstream")
        result = self.fx.sync()
        self.assertEqual(result.outcome, "conflict")
        self.assertEqual(result.exit_code, gs.EXIT_CONFLICT)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.conflicts, ["shared.txt"])
        self.assertEqual(self.fx.pushes(), [])
        self.assertEqual(self.fx.head("upstream"), up)
        self.assertEqual(self.fx.head("mirror"), mf)

    def test_race_on_fast_forward_leaves_the_new_commit_for_the_next_run(self) -> None:
        self.fx.commit("upstream", "a.txt", "a\n")
        raced = {}
        self.fx.on_push = lambda argv: raced.setdefault("sha", self.fx.commit("mirror", "late.txt", "late\n"))
        result = self.fx.sync()
        self.assertEqual(result.outcome, "raced")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.fx.head("mirror"), raced["sha"])  # the rejected push changed nothing
        # The next tick sees a divergence and merges it.
        self.fx.work = Path(self.tmp.name) / "work-2"
        again = self.fx.sync()
        self.assertEqual(again.outcome, "merged")
        self.assertEqual(self.fx.head("mirror"), self.fx.head("upstream"))

    def test_race_on_merge_push_converges_next_run(self) -> None:
        self.fx.commit("upstream", "a.txt", "a\n")
        self.fx.commit("mirror", "b.txt", "b\n")
        self.fx.on_push = lambda argv: self.fx.commit("upstream", "late.txt", "late\n")
        result = self.fx.sync()
        self.assertEqual(result.outcome, "raced")
        self.assertEqual(result.exit_code, 0)
        self.fx.work = Path(self.tmp.name) / "work-2"
        again = self.fx.sync()
        self.assertIn(again.outcome, ("merged", "fast-forward-mirror"))
        self.assertEqual(self.fx.head("mirror"), self.fx.head("upstream"))
        for name in ("a.txt", "b.txt", "late.txt"):
            git(self.fx.remotes["mirror"], "cat-file", "-e", f"main:{name}")

    def test_a_refused_push_with_an_unmoved_remote_fails(self) -> None:
        self.fx.commit("upstream", "a.txt", "a\n")
        hook = self.fx.remotes["mirror"] / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho refused >&2\nexit 1\n")
        hook.chmod(0o755)
        before = self.fx.head("mirror")
        result = self.fx.sync()
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.exit_code, gs.EXIT_FAILED)
        self.assertEqual(self.fx.head("mirror"), before)

    def test_a_refused_merge_push_to_an_unmoved_upstream_leaves_the_mirror_alone(self) -> None:
        # Pushing the merge to the mirror anyway would leave it ahead of upstream, and every later
        # run would try (and fail) the same upstream push.
        up = self.fx.commit("upstream", "a.txt", "a\n")
        mf = self.fx.commit("mirror", "b.txt", "b\n")
        hook = self.fx.remotes["upstream"] / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho refused >&2\nexit 1\n")
        hook.chmod(0o755)
        result = self.fx.sync()
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.exit_code, gs.EXIT_FAILED)
        self.assertEqual(self.fx.head("upstream"), up)
        self.assertEqual(self.fx.head("mirror"), mf)
        self.assertEqual(len(self.fx.pushes()), 1)

    def test_dry_run_pushes_nothing(self) -> None:
        self.fx.commit("upstream", "a.txt", "a\n")
        result = self.fx.sync(dry_run=True)
        self.assertEqual(result.outcome, "fast-forward-mirror")
        self.assertEqual(self.fx.pushes(), [])

    def test_push_refuses_any_force_form(self) -> None:
        for argv in (
            ["git", "push", "--force", "r", "a:refs/heads/main"],
            ["git", "push", "-f", "r", "a:refs/heads/main"],
            ["git", "push", "--force-with-lease", "r", "a:refs/heads/main"],
            ["git", "push", "--force-with-lease=main:b", "r", "a:refs/heads/main"],
            ["git", "push", "r", "+a:refs/heads/main"],
            ["git", "push", "r", ":refs/heads/main"],
            ["git", "push", "--mirror", "r"],
            ["git", "push", "--delete", "r", "main"],
        ):
            with self.assertRaises(gs.ForcePushRefused, msg=argv):
                gs.assert_no_force(argv)
        gs.assert_no_force(["git", "push", "--porcelain", "r", "a" * 40 + ":refs/heads/main"])

    def test_cli_reports_json_and_exit_code(self) -> None:
        self.fx.commit("upstream", "shared.txt", "one\nUP\nthree\n")
        self.fx.commit("mirror", "shared.txt", "one\nMF\nthree\n")
        report = Path(self.tmp.name) / "report.json"
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "--upstream", str(self.fx.remotes["upstream"]),
             "--mirror", str(self.fx.remotes["mirror"]), "--work", str(self.fx.work), "--report", str(report)],
            env=GIT_ENV, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(done.returncode, gs.EXIT_CONFLICT, done.stderr)
        data = json.loads(report.read_text())
        self.assertEqual(data["outcome"], "conflict")
        self.assertEqual(data["conflicts"], ["shared.txt"])
        self.assertIn(gs.REPAIR_POINTER, done.stdout + done.stderr)


class FakeGh:
    def __init__(self, issues: list[dict] | None = None) -> None:
        self.issues = issues or []
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], stdin: str | None = None) -> str:
        self.calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            return json.dumps([i for i in self.issues if i["state"] == "OPEN"])
        if args[:2] == ["issue", "create"]:
            number = 100 + len(self.issues)
            self.issues.append({"number": number, "title": args[args.index("--title") + 1], "body": stdin, "state": "OPEN"})
            return f"https://github.com/o/r/issues/{number}\n"
        if args[:2] == ["issue", "edit"]:
            issue = next(i for i in self.issues if i["number"] == int(args[2]))
            issue["body"] = stdin
            return ""
        if args[:2] in (["issue", "close"], ["issue", "comment"]):
            if args[1] == "close":
                next(i for i in self.issues if i["number"] == int(args[2]))["state"] = "CLOSED"
            return ""
        raise AssertionError(args)


def conflict_result(up: str = "1" * 40, mf: str = "2" * 40, paths=("shared.txt",)):
    return gs.SyncResult(outcome="conflict", upstream=up, mirror=mf, conflicts=list(paths))


class TrackingIssueTest(unittest.TestCase):
    def test_conflict_opens_one_issue_naming_both_shas_and_paths(self) -> None:
        gh = FakeGh()
        action = gs.update_tracking_issue(conflict_result(), repo="o/r", gh=gh)
        self.assertEqual(action, "opened")
        self.assertEqual(len(gh.issues), 1)
        body = gh.issues[0]["body"]
        for text in ("1" * 40, "2" * 40, "shared.txt", gs.REPAIR_POINTER):
            self.assertIn(text, body)
        self.assertEqual(gh.issues[0]["title"], gs.ISSUE_TITLE)

    def test_a_repeated_conflict_updates_the_same_issue_only_when_it_changed(self) -> None:
        gh = FakeGh()
        gs.update_tracking_issue(conflict_result(), repo="o/r", gh=gh)
        self.assertEqual(gs.update_tracking_issue(conflict_result(), repo="o/r", gh=gh), "unchanged")
        self.assertEqual(gs.update_tracking_issue(conflict_result(mf="3" * 40), repo="o/r", gh=gh), "updated")
        self.assertEqual(len(gh.issues), 1)
        self.assertIn("3" * 40, gh.issues[0]["body"])

    def test_an_unrelated_issue_with_another_title_is_left_alone(self) -> None:
        gh = FakeGh([{"number": 7, "title": "something else", "body": "", "state": "OPEN"}])
        self.assertEqual(gs.update_tracking_issue(conflict_result(), repo="o/r", gh=gh), "opened")
        self.assertEqual(len(gh.issues), 2)

    def test_a_later_successful_sync_closes_the_issue(self) -> None:
        gh = FakeGh()
        gs.update_tracking_issue(conflict_result(), repo="o/r", gh=gh)
        merged = gs.SyncResult(outcome="merged", upstream="1" * 40, mirror="2" * 40, merge="4" * 40)
        self.assertEqual(gs.update_tracking_issue(merged, repo="o/r", gh=gh), "closed")
        self.assertEqual(gh.issues[0]["state"], "CLOSED")
        self.assertEqual(gs.update_tracking_issue(merged, repo="o/r", gh=gh), "none")

    def test_a_race_or_failure_leaves_the_issue_open(self) -> None:
        gh = FakeGh()
        gs.update_tracking_issue(conflict_result(), repo="o/r", gh=gh)
        for outcome in ("raced", "failed"):
            result = gs.SyncResult(outcome=outcome, upstream="1" * 40, mirror="2" * 40)
            self.assertEqual(gs.update_tracking_issue(result, repo="o/r", gh=gh), "none")
        self.assertEqual(gh.issues[0]["state"], "OPEN")


if __name__ == "__main__":
    unittest.main()
