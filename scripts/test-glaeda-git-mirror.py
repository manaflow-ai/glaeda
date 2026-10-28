#!/usr/bin/env python3
"""glaeda-git-mirror: a full local cmux mirror, seeded from an R2 bundle, refreshed by bounded fetches."""

from __future__ import annotations

import fcntl
import importlib.machinery
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load():
    loader = importlib.machinery.SourceFileLoader("glaeda_git_mirror", os.fspath(ROOT / "scripts/glaeda-git-mirror"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


gm = load()


def sh(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


class Mirror(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home = root / "home"
        (self.home / "actions-runner-glaeda").mkdir(parents=True)
        (self.home / "actions-runner-glaeda/.runner").write_text("{}")
        (self.home / gm.SOURCE_CONFIG).parent.mkdir(parents=True)
        (self.home / gm.SOURCE_CONFIG).write_text('{"schema": 1, "source": "direct"}')
        # The "GitHub" repository: three branches.
        self.upstream = root / "upstream"
        self.upstream.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=self.upstream)
        self.commit("one")
        for branch in ("feat-cmux-next", "nightly-next"):
            sh("git", "branch", branch, cwd=self.upstream)
        sh("git", "branch", "pr-only", cwd=self.upstream)
        # The bucket: a bundle of upstream, archived as r2-cache.sh saves a directory.
        self.bucket = root / "bucket"
        self.key = "git-mirror-cmux-2026102709." + sh("git", "rev-parse", "HEAD", cwd=self.upstream)[:12]
        staged = root / "staged"
        staged.mkdir()
        sh("git", "bundle", "create", os.fspath(staged / gm.BUNDLE_FILE), "--branches", cwd=self.upstream)
        (self.bucket / "v1/any/objects").mkdir(parents=True)
        (self.bucket / "v1/any/latest").mkdir(parents=True)
        with tarfile.open(self.bucket / f"v1/any/objects/{self.key}.tar.gz", "w:gz") as archive:
            archive.add(staged / gm.BUNDLE_FILE, arcname=f"./{gm.BUNDLE_FILE}")
        (self.bucket / f"v1/any/latest/{gm.BUNDLE_PREFIX}").write_text(self.key + "\n")
        self.downloads: list[str] = []

        git_dir = root / "fleet/git"
        patches = {"GIT_DIR": git_dir, "MIRROR": git_dir / "cmux.git", "RECORD": git_dir / "cmux.json",
                   "REPO_URL": self.upstream.as_uri(), "BUCKET_URL": "https://bucket.test", "SEED_MIN_FREE": 0,
                   "download": self.fake_download, "zstd": lambda: None, "BACKOFF": (0, 0)}
        for name, value in patches.items():
            old = getattr(gm, name)
            setattr(gm, name, value)
            self.addCleanup(setattr, gm, name, old)
        # seed()'s default argument captured the real download at definition time.
        real_seed = gm.seed
        gm.seed = lambda mirror, from_github, fetch_url=self.fake_download: real_seed(mirror, from_github, fetch_url)
        self.addCleanup(setattr, gm, "seed", real_seed)
        self.mirror = git_dir / "cmux.git"
        git_dir.mkdir(parents=True)
        (git_dir / gm.SEED_AFTER).write_text("0\n")  # this host's first-seed slot opened long ago

    def commit(self, message: str) -> str:
        (self.upstream / "f").write_text(message)
        sh("git", "add", "f", cwd=self.upstream)
        sh("git", "commit", "-q", "-m", message, cwd=self.upstream)
        return sh("git", "rev-parse", "HEAD", cwd=self.upstream)

    def fake_download(self, url: str, target: Path, timeout: float) -> None:
        self.downloads.append(url)
        source = self.bucket / url.removeprefix("https://bucket.test/")
        if not source.is_file():
            raise subprocess.CalledProcessError(22, "curl")
        target.write_bytes(source.read_bytes())

    def test_no_runner_is_a_no_op(self) -> None:
        empty = Path(self.temp.name) / "nobody"
        empty.mkdir()
        self.assertEqual(gm.run(True, home=empty)["state"], "skipped")
        self.assertFalse(self.mirror.exists())

    def test_seeds_from_the_bundle_then_fetches_new_commits(self) -> None:
        head = self.commit("two")  # newer than the bundle: the fetch after the seed brings it
        result = gm.run(True, home=self.home)
        self.assertEqual(result["state"], "applied", result)
        self.assertIn(f"bundle {self.key}", result["seeded"]["source"])
        self.assertEqual(result["heads"]["main"], head)
        self.assertEqual(set(result["heads"]), set(gm.BRANCHES))  # pr-only is not mirrored
        self.assertEqual(gm.healthy(self.mirror), "")
        self.assertEqual(sh("git", "-C", os.fspath(self.mirror), "config", "uploadpack.allowAnySHA1InWant"), "true")
        self.assertTrue(gm.RECORD.is_file())

    def test_a_job_fetches_any_commit_from_the_mirror(self) -> None:
        old = sh("git", "rev-parse", "HEAD", cwd=self.upstream)
        self.commit("two")
        gm.run(True, home=self.home)
        work = Path(self.temp.name) / "work"
        sh("git", "init", "-q", os.fspath(work))
        sh("git", "-C", os.fspath(work), "fetch", "-q", os.fspath(self.mirror), old)
        self.assertEqual(sh("git", "-C", os.fspath(work), "rev-parse", "FETCH_HEAD"), old)

    def test_the_mirror_refuses_to_become_shallow(self) -> None:
        gm.run(True, home=self.home)
        with self.assertRaises(subprocess.CalledProcessError):
            sh("git", "-C", os.fspath(self.mirror), "fetch", "-q", "--depth=1", self.upstream.as_uri(), "main")
        self.assertEqual(gm.healthy(self.mirror), "")

    def test_a_second_pass_only_fetches(self) -> None:
        gm.run(True, home=self.home)
        self.downloads.clear()
        head = self.commit("three")
        result = gm.run(True, home=self.home)
        self.assertNotIn("seeded", result)
        self.assertEqual(self.downloads, [])
        self.assertEqual(result["heads"]["main"], head)

    def test_a_broken_mirror_is_repaired_in_place_and_keeps_every_object(self) -> None:
        sh("git", "checkout", "-q", "-b", "scratch", cwd=self.upstream)
        forced_away = self.commit("only the old mirror has this")
        sh("git", "branch", "-f", "nightly-next", forced_away, cwd=self.upstream)
        gm.run(True, home=self.home)
        sh("git", "branch", "-f", "nightly-next", "main", cwd=self.upstream)
        sh("git", "checkout", "-q", "main", cwd=self.upstream)
        inode = self.mirror.stat().st_ino
        (self.mirror / "HEAD").unlink()
        self.assertEqual(gm.healthy(self.mirror), "missing")
        result = gm.run(True, home=self.home)
        self.assertEqual(result["state"], "applied", result)
        self.assertEqual(result["seeded"]["mode"], "repaired in place")
        self.assertIn("bundle", result["seeded"]["source"])
        self.assertEqual(self.mirror.stat().st_ino, inode)  # never replaced
        sh("git", "-C", os.fspath(self.mirror), "cat-file", "-e", f"{forced_away}^{{commit}}")

    def test_a_shallow_mirror_is_unshallowed_in_place(self) -> None:
        self.commit("two")
        gm.run(True, home=self.home)
        guard = self.mirror / "shallow.lock"
        guard.rmdir()
        tip = sh("git", "-C", os.fspath(self.mirror), "rev-parse", "main")
        (self.mirror / "shallow").write_text(tip + "\n")
        self.assertEqual(gm.healthy(self.mirror), "shallow")
        result = gm.run(True, home=self.home)
        self.assertEqual(result["state"], "applied", result)
        self.assertEqual(gm.healthy(self.mirror), "")
        self.assertTrue(guard.is_dir())

    def test_nothing_happens_without_a_configured_source(self) -> None:
        (self.home / gm.SOURCE_CONFIG).unlink()
        result = gm.run(True, home=self.home)
        self.assertEqual(result["state"], "skipped")
        self.assertFalse(self.mirror.exists())
        self.assertEqual(self.downloads, [])

    def test_every_partial_marker_and_every_branch_counts(self) -> None:
        gm.run(True, home=self.home)
        self.assertEqual(gm.healthy(self.mirror), "")
        sh("git", "-C", os.fspath(self.mirror), "config", "extensions.partialclone", "origin")
        self.assertEqual(gm.healthy(self.mirror), "partial")
        sh("git", "-C", os.fspath(self.mirror), "config", "--unset", "extensions.partialclone")
        sh("git", "-C", os.fspath(self.mirror), "update-ref", "-d", "refs/heads/nightly-next")
        self.assertEqual(gm.healthy(self.mirror), "no nightly-next")

    def test_git_s_own_shallow_lock_file_never_blocks_the_guard(self) -> None:
        gm.run(True, home=self.home)
        guard = self.mirror / "shallow.lock"
        guard.rmdir()
        guard.write_text("")  # what a fetch killed mid-unshallow leaves
        self.assertEqual(gm.run(True, home=self.home)["state"], "applied")
        self.assertTrue(guard.is_dir())

    def test_the_github_fallback_always_refetches(self) -> None:
        calls = []
        real = gm.fetch
        gm.fetch = lambda repo, url, **kw: calls.append(kw.get("extra")) or {"ok": True, "tries": 1, "seconds": 0}
        try:
            with tempfile.TemporaryDirectory() as temp:
                repo = Path(temp) / "r.git"
                sh("git", "init", "-q", "--bare", os.fspath(repo))
                (self.bucket / f"v1/any/latest/{gm.BUNDLE_PREFIX}").unlink()
                gm.fill(repo, Path(temp), True, gm.download)
        finally:
            gm.fetch = real
        self.assertEqual(calls, [["--refetch"]])

    def test_a_failed_repair_restores_the_shallow_file_exactly(self) -> None:
        gm.run(True, home=self.home)
        (self.bucket / f"v1/any/latest/{gm.BUNDLE_PREFIX}").unlink()
        sh("git", "-C", os.fspath(self.mirror), "update-ref", "-d", "refs/heads/main")
        real = gm.fill

        def writes_shallow(repo, *args):
            (repo / "shallow").write_text("0" * 40 + "\n")
            raise RuntimeError("fill failed")

        gm.fill = writes_shallow
        try:
            self.assertEqual(gm.run(True, home=self.home)["state"], "error")
        finally:
            gm.fill = real
        self.assertFalse((self.mirror / "shallow").exists())

    def test_a_truly_shallow_mirror_is_deepened_from_the_bundle(self) -> None:
        self.commit("two")
        self.commit("three")
        (self.bucket / f"v1/any/objects/{self.key}.tar.gz").unlink()
        staged = Path(self.temp.name) / "staged2"
        staged.mkdir()
        sh("git", "bundle", "create", os.fspath(staged / gm.BUNDLE_FILE), "--branches", cwd=self.upstream)
        with tarfile.open(self.bucket / f"v1/any/objects/{self.key}.tar.gz", "w:gz") as archive:
            archive.add(staged / gm.BUNDLE_FILE, arcname=f"./{gm.BUNDLE_FILE}")
        self.mirror.parent.mkdir(parents=True, exist_ok=True)
        sh("git", "clone", "-q", "--bare", "--depth=1", self.upstream.as_uri(), os.fspath(self.mirror))
        self.assertEqual(gm.healthy(self.mirror), "shallow")
        result = gm.run(True, home=self.home)
        self.assertEqual(result["state"], "applied", result)
        self.assertEqual(result["seeded"]["mode"], "repaired in place")
        self.assertEqual(sh("git", "-C", os.fspath(self.mirror), "rev-list", "--count", "main"), "3")

    def test_a_killed_fetch_s_ref_locks_never_wedge_the_mirror(self) -> None:
        gm.run(True, home=self.home)
        self.commit("two")
        (self.mirror / "refs/heads/main.lock").write_text("")
        (self.mirror / "packed-refs.lock").write_text("")
        (self.mirror / "config.lock").write_text("")
        (self.mirror / "HEAD.lock").write_text("")
        abandoned = self.mirror.parent / ".seed-killed"
        (abandoned / "cmux.git").mkdir(parents=True)
        (self.mirror / "shallow").write_text(sh("git", "-C", os.fspath(self.mirror), "rev-parse", "main") + "\n")
        result = gm.run(True, home=self.home)  # a repair, then the fetch
        self.assertEqual(result["state"], "applied", result)
        self.assertEqual(result["fetch"]["tries"], 1)
        self.assertFalse((self.mirror / "refs/heads/main.lock").exists())
        self.assertFalse((self.mirror / "config.lock").exists())
        self.assertFalse((self.mirror / "HEAD.lock").exists())
        self.assertFalse(abandoned.exists())

    def test_a_repair_killed_part_way_still_reads_as_broken(self) -> None:
        gm.run(True, home=self.home)
        sh("git", "-C", os.fspath(self.mirror), "config", "remote.origin.promisor", "true")
        real = gm.fill

        def killed(repo, *args):
            raise KeyboardInterrupt  # stands in for SIGKILL: no restore runs past the marker
        gm.fill = killed
        try:
            with self.assertRaises(KeyboardInterrupt):
                gm.seed(self.mirror, False, self.fake_download)
        finally:
            gm.fill = real
        sh("git", "-C", os.fspath(self.mirror), "config", "--unset-all", "remote.origin.promisor")  # as if lost
        self.assertEqual(gm.healthy(self.mirror), "repair pending")
        result = gm.run(True, home=self.home)
        self.assertEqual(result["state"], "applied", result)
        self.assertEqual(result["seeded"]["because"], "repair pending")
        self.assertFalse((self.mirror / gm.REPAIR_PENDING).exists())

    def test_a_read_that_times_out_fetches_instead_of_reseeding(self) -> None:
        gm.run(True, home=self.home)
        head = self.commit("two")
        real = gm.config_values

        def slow(repo, key):
            raise subprocess.TimeoutExpired(["git", "config"], 30)
        gm.config_values = slow
        try:
            self.assertEqual(gm.healthy(self.mirror), gm.SLOW)
            self.downloads.clear()
            result = gm.run(True, home=self.home)
        finally:
            gm.config_values = real
        self.assertEqual(result["state"], "applied", result)
        self.assertNotIn("seeded", result)  # a loaded host is not a broken mirror
        self.assertEqual(self.downloads, [])
        self.assertIn("slow", result)
        self.assertEqual(result["heads"]["feat-cmux-next"], sh("git", "rev-parse", "feat-cmux-next", cwd=self.upstream))
        self.assertEqual(result["heads"]["main"], head)
        self.assertGreaterEqual(result["fetched_at"], result["time"])  # when the fetch was written, not the start

    def test_a_connectivity_walk_that_times_out_is_not_missing_objects(self) -> None:
        gm.run(True, home=self.home)
        (self.mirror / gm.VERIFIED).unlink()
        real = gm.git

        def walk(repo, *args, **kw):
            if args[:1] == ("fsck",):
                raise subprocess.TimeoutExpired(["git", "fsck"], 1)
            return real(repo, *args, **kw)
        gm.git = walk
        try:
            self.assertIsNone(gm.connected(self.mirror))
            result = gm.run(True, home=self.home)
        finally:
            gm.git = real
        self.assertEqual(result["state"], "applied", result)
        self.assertNotIn("seeded", result)
        self.assertFalse((self.mirror / gm.VERIFIED).exists(), "judged on a later pass")

    def test_a_repair_fetches_the_new_commits_first_and_records_them(self) -> None:
        gm.run(True, home=self.home)
        head = self.commit("two")
        (self.mirror / gm.REPAIR_PENDING).write_text("x\n")
        seen = {}
        real = gm.seed

        def watching(mirror, from_github, fetch_url=self.fake_download):
            seen["record"] = json.loads(gm.RECORD.read_text())  # what health reads while the repair runs
            seen["main"] = sh("git", "-C", os.fspath(mirror), "rev-parse", "main")
            return real(mirror, from_github, fetch_url)
        gm.seed = watching
        try:
            result = gm.run(True, home=self.home)
        finally:
            gm.seed = real
        self.assertEqual(result["state"], "applied", result)
        self.assertEqual(result["seeded"]["because"], "repair pending")
        self.assertEqual(seen["main"], head)
        self.assertEqual(seen["record"]["state"], "repairing")
        self.assertTrue(seen["record"]["fetch"]["ok"])
        self.assertGreaterEqual(seen["record"]["fetched_at"], seen["record"]["time"])

    def test_a_repair_whose_last_fetch_fails_keeps_the_early_fetch_s_tips(self) -> None:
        gm.run(True, home=self.home)
        head = self.commit("newer than the bundle")
        (self.mirror / gm.REPAIR_PENDING).write_text("x\n")
        real_seed, real_fetch = gm.seed, gm.fetch
        calls = []

        def fetching(mirror, url, **kw):
            calls.append(url)
            return real_fetch(mirror, url, **kw) if len(calls) == 1 else {"ok": False, "tries": 1, "errors": ["x"]}
        gm.fetch = fetching
        try:
            result = gm.run(True, home=self.home)
        finally:
            gm.fetch = real_fetch
        self.assertEqual(result["state"], "fetch-failed", result)
        self.assertEqual(result["seeded"]["because"], "repair pending")
        self.assertEqual(sh("git", "-C", os.fspath(self.mirror), "rev-parse", "main"), head)  # not the bundle's

    def test_a_repair_that_fails_keeps_the_early_fetch_s_tips(self) -> None:
        gm.run(True, home=self.home)
        head = self.commit("newer than the bundle")
        (self.mirror / gm.REPAIR_PENDING).write_text("x\n")
        real = gm.seed

        def failing(mirror, from_github, fetch_url=self.fake_download):
            sh("git", "-C", os.fspath(mirror), "update-ref", "refs/heads/main", "main~1")  # as the bundle's tips do
            raise RuntimeError("bundle unreadable")
        gm.seed = failing
        try:
            result = gm.run(True, home=self.home)
        finally:
            gm.seed = real
        self.assertEqual(result["state"], "error", result)
        self.assertEqual(sh("git", "-C", os.fspath(self.mirror), "rev-parse", "main"), head)

    def test_a_record_from_before_fetched_at_carries_its_fetch_time(self) -> None:
        gm.RECORD.write_text(json.dumps({"time": "2026-10-27T20:13:00Z", "fetch": {"ok": True}}) + "\n")
        written = gm.write_record({"time": "2026-10-27T20:20:00Z", "fetch": {"ok": False}}, gm.last_record())
        self.assertEqual(written["fetched_at"], "2026-10-27T20:13:00Z")

    def test_a_shallow_or_partial_repair_does_not_fetch_first(self) -> None:
        gm.run(True, home=self.home)
        self.assertTrue(gm.early_fetch("repair pending"))
        self.assertTrue(gm.early_fetch("no nightly-next"))
        self.assertFalse(gm.early_fetch("shallow"))
        self.assertFalse(gm.early_fetch("missing"))
        sh("git", "-C", os.fspath(self.mirror), "config", "remote.origin.promisor", "true")
        self.assertFalse(gm.early_fetch("repair pending"))

    def test_a_failed_fetch_keeps_the_last_good_fetch_time(self) -> None:
        first = gm.run(True, home=self.home)
        gm.REPO_URL = (Path(self.temp.name) / "gone").as_uri()  # restored by setUp's cleanup
        failed = gm.run(True, home=self.home)
        self.assertEqual(failed["state"], "fetch-failed", failed)
        self.assertEqual(failed["fetched_at"], first["fetched_at"])
        self.assertEqual(json.loads(gm.RECORD.read_text())["fetched_at"], first["fetched_at"])

    def test_a_failed_repair_keeps_the_mirror_marked_broken(self) -> None:
        self.commit("two")
        gm.run(True, home=self.home)
        (self.mirror / "shallow.lock").rmdir()
        tip = sh("git", "-C", os.fspath(self.mirror), "rev-parse", "main")
        (self.mirror / "shallow").write_text(tip + "\n")
        sh("git", "-C", os.fspath(self.mirror), "config", "remote.origin.promisor", "true")
        # No bundle and no GitHub fallback: the repair fails, and the markers stay.
        (self.bucket / f"v1/any/latest/{gm.BUNDLE_PREFIX}").unlink()
        self.assertEqual(gm.run(True, home=self.home)["state"], "error")
        self.assertEqual((self.mirror / "shallow").read_text(), tip + "\n")
        self.assertEqual(gm.config_values(self.mirror, "remote.origin.promisor"), ["true"])
        self.assertNotEqual(gm.healthy(self.mirror), "")

    def test_the_github_fallback_unshallows_an_existing_mirror(self) -> None:
        self.commit("two")
        self.commit("three")
        gm.run(True, home=self.home)
        # Make it truly shallow: a fresh depth-1 copy in place of the full one.
        sh("rm", "-rf", os.fspath(self.mirror))
        sh("git", "clone", "-q", "--bare", "--depth=1", self.upstream.as_uri(), os.fspath(self.mirror))
        self.assertEqual(gm.healthy(self.mirror), "shallow")
        (self.bucket / f"v1/any/latest/{gm.BUNDLE_PREFIX}").unlink()
        result = gm.run(True, home=self.home, from_github=True)
        self.assertEqual(result["state"], "applied", result)
        self.assertEqual(result["seeded"]["mode"], "repaired in place")
        self.assertEqual(gm.healthy(self.mirror), "")
        self.assertEqual(sh("git", "-C", os.fspath(self.mirror), "rev-list", "--count", "main"), "3")

    def test_no_bundle_never_clones_from_github_unless_asked(self) -> None:
        (self.bucket / f"v1/any/latest/{gm.BUNDLE_PREFIX}").unlink()
        result = gm.run(True, home=self.home)
        self.assertEqual(result["state"], "error", result)
        self.assertIn("R2 bundle", result["reason"])
        self.assertFalse(self.mirror.exists())
        # The operator's fallback fills the staging repository before it takes the path.
        result = gm.run(True, home=self.home, from_github=True)
        self.assertEqual(result["state"], "applied", result)
        self.assertIn("github", result["seeded"]["source"])
        self.assertEqual(result["seeded"]["mode"], "new")
        self.assertEqual(gm.healthy(self.mirror), "")

    def test_a_failed_seed_leaves_no_half_mirror(self) -> None:
        (self.bucket / f"v1/any/objects/{self.key}.tar.gz").write_bytes(b"not an archive")
        result = gm.run(True, home=self.home)
        self.assertEqual(result["state"], "error", result)
        self.assertFalse(self.mirror.exists())
        self.assertEqual([p.name for p in self.mirror.parent.iterdir() if p.name.startswith(".seed-")], [])

    def test_a_corrupt_archive_falls_through_to_the_other_one(self) -> None:
        (self.bucket / f"v1/any/objects/{self.key}.tar.zst").write_bytes(b"not zstd")
        with mock.patch.object(gm, "zstd", lambda: "/usr/bin/false"):
            result = gm.run(True, home=self.home)
        self.assertEqual(result["state"], "applied", result)
        self.assertTrue(self.downloads[-1].endswith(".tar.gz"), self.downloads)

    def test_a_killed_fetch_s_keep_files_never_pile_up(self) -> None:
        gm.run(True, home=self.home)
        keep = self.mirror / "objects/pack/pack-dead.keep"
        keep.write_text("fetch-pack 1 on host\n")
        self.assertEqual(gm.run(True, home=self.home)["state"], "applied")
        self.assertFalse(keep.exists())

    def test_a_dead_pass_s_writers_are_stopped_before_the_next_pass_writes(self) -> None:
        gm.run(True, home=self.home)
        fd = os.open(gm.GIT_DIR / ".lock", os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        writer = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True, pass_fds=(fd,))
        self.addCleanup(lambda: (writer.poll() is None and writer.kill(), writer.wait()))
        os.close(fd)  # only the orphan holds the flock now
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        (gm.GIT_DIR / gm.PASS_FILE).write_text(json.dumps({"pid": dead.pid, "groups": [writer.pid]}))
        with mock.patch.object(gm, "ORPHAN_NAMES", {"sleep"}):
            result = gm.run(True, home=self.home)
        self.assertEqual(result["state"], "applied", result)
        self.assertIsNotNone(writer.wait(timeout=10))
        self.assertFalse((gm.GIT_DIR / gm.PASS_FILE).exists())

    def test_a_live_pass_is_never_stopped(self) -> None:
        gm.run(True, home=self.home)
        fd = os.open(gm.GIT_DIR / ".lock", os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        writer = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (writer.kill(), writer.wait()))
        (gm.GIT_DIR / gm.PASS_FILE).write_text(json.dumps({"pid": os.getpid(), "groups": [writer.pid]}))
        with mock.patch.object(gm, "ORPHAN_NAMES", {"sleep"}):
            self.assertEqual(gm.run(True, home=self.home)["state"], "busy")
        self.assertIsNone(writer.poll())

    def test_a_lost_object_is_found_by_the_daily_walk_and_repaired(self) -> None:
        gm.run(True, home=self.home)
        self.assertTrue((self.mirror / gm.VERIFIED).exists())
        self.assertEqual(gm.run(True, home=self.home).get("seeded"), None)  # walked today: not again
        os.utime(self.mirror / gm.VERIFIED, (0, 0))
        walks = iter([False])
        with mock.patch.object(gm, "connected", lambda repo: next(walks)):
            result = gm.run(True, home=self.home)
        self.assertEqual(result["state"], "applied", result)
        self.assertEqual(result["seeded"]["because"], "missing objects")
        self.assertTrue(gm.verified_recently(self.mirror))

    def test_a_bad_pointer_is_refused(self) -> None:
        (self.bucket / f"v1/any/latest/{gm.BUNDLE_PREFIX}").write_text("../../etc/passwd")
        result = gm.run(True, home=self.home)
        self.assertEqual(result["state"], "error")
        self.assertFalse(self.mirror.exists())

    def test_many_packs_are_folded_and_gc_never_runs_inside_a_fetch(self) -> None:
        gm.run(True, home=self.home)
        self.assertEqual(sh("git", "-C", os.fspath(self.mirror), "config", "gc.auto"), "0")
        self.assertEqual(sh("git", "-C", os.fspath(self.mirror), "config", "gc.pruneExpire"), "never")
        # A commit only a since-forced nightly-next held survives the folds (borrowers may hold it).
        sh("git", "checkout", "-q", "-b", "scratch", cwd=self.upstream)
        orphan = self.commit("forced-away")
        sh("git", "branch", "-f", "nightly-next", orphan, cwd=self.upstream)
        gm.run(True, home=self.home)
        sh("git", "branch", "-f", "nightly-next", "main", cwd=self.upstream)
        sh("git", "checkout", "-q", "main", cwd=self.upstream)
        old = gm.PACK_LIMIT
        gm.PACK_LIMIT = 2
        self.addCleanup(setattr, gm, "PACK_LIMIT", old)
        folds = []
        for n in range(4):
            self.commit(f"c{n}")
            folds.append(gm.run(True, home=self.home))
        packs = list((self.mirror / "objects/pack").glob("*.pack"))
        self.assertLessEqual(len(packs), 3)
        self.assertTrue(any(r.get("repack", {}).get("status") == 0 for r in folds), folds)
        loose = [p for p in (self.mirror / "objects").iterdir() if len(p.name) == 2 and any(p.iterdir())]
        self.assertEqual(loose, [])  # a small fetch keeps its pack (fetch.unpackLimit 1), so the folds see it
        sh("git", "-C", os.fspath(self.mirror), "cat-file", "-e", f"{orphan}^{{commit}}")
        self.assertEqual(gm.healthy(self.mirror), "")

    def test_only_a_session_leader_records_its_own_group(self) -> None:
        """The first command of a shell pipeline leads a group its siblings share, so its group is never recorded
        (stop_orphans would kill a sibling); a session leader's group holds only the pass and its children."""
        probe = (f"import importlib.machinery, importlib.util, os, sys\n"
                 f"loader = importlib.machinery.SourceFileLoader('gm', {os.fspath(ROOT / 'scripts/glaeda-git-mirror')!r})\n"
                 f"spec = importlib.util.spec_from_loader('gm', loader); gm = importlib.util.module_from_spec(spec)\n"
                 f"loader.exec_module(gm)\n"
                 f"if sys.argv[1] == 'group': os.setpgid(0, 0)\n"
                 f"print(gm.own_group() == [os.getpid()])\n")
        group = subprocess.run([sys.executable, "-c", probe, "group"], capture_output=True, text=True, check=True)
        self.assertEqual(group.stdout.strip(), "False")
        session = subprocess.run([sys.executable, "-c", probe, "session"], capture_output=True, text=True, check=True,
                                 start_new_session=True)
        self.assertEqual(session.stdout.strip(), "True")

    def test_an_apply_pass_runs_in_a_session_of_its_own(self) -> None:
        mark = Path(self.temp.name) / "leader"

        def call() -> int:
            mark.write_text(str(os.getsid(0) == os.getpid() and gm.own_group() == [os.getpid()]))
            return 3
        self.assertEqual(gm.in_own_session(call), 3)
        self.assertEqual(mark.read_text(), "True")

    def test_a_first_seed_waits_for_the_host_s_slot_and_a_repair_never_does(self) -> None:
        after = gm.GIT_DIR / gm.SEED_AFTER
        longest = gm.SEED_SLOTS * gm.SEED_SLOT_S
        waits = set()
        for host in ("mini-a", "mini-b", "mini-c", "mini-d", "mini-e", "mini-f"):
            after.unlink()
            with mock.patch.object(gm.platform, "node", lambda host=host: host):
                wait = gm.seed_wait(self.home, 1000.0)
                self.assertEqual(gm.seed_wait(self.home, 1000.0 + wait / 2), wait / 2)  # one deadline, kept
            self.assertLess(wait, longest)
            self.assertEqual(wait % gm.SEED_SLOT_S, 0)
            waits.add(wait)
        self.assertGreater(len(waits), 1)  # spread, not all at once
        # touching the source config never moves it; a deadline from a clock that was ahead is pulled back
        os.utime(self.home / gm.SOURCE_CONFIG, None)
        after.write_text(f"{1000 + 86400}\n")
        self.assertLess(gm.seed_wait(self.home, 1000.0), longest)
        self.assertLess(gm.seed_wait(self.home, 1000.0 + longest), 1)
        after.unlink()
        self.assertEqual(gm.seed_wait(self.home, 1000.0, keep=False) >= 0, True)
        self.assertFalse(after.exists())  # plan mode writes nothing
        with mock.patch.object(gm, "seed_wait", lambda home=None, now=None, keep=True: 300.0):
            result = gm.run(True, home=self.home)
            self.assertEqual(result["state"], "skipped", result)
            self.assertIn("slot", result["reason"])
            self.assertFalse(self.mirror.exists())
        after.write_text("0\n")  # the slot has opened
        self.assertEqual(gm.run(True, home=self.home)["state"], "applied")
        (self.mirror / "shallow").write_text("0" * 40 + "\n")  # broken, not missing: repaired at once
        with mock.patch.object(gm, "seed_wait", lambda home=None, now=None, keep=True: 300.0):
            self.assertEqual(gm.run(True, home=self.home)["state"], "applied")
        shutil.rmtree(self.mirror)  # a seeded mirror that disappears: its kept deadline has passed
        self.assertEqual(gm.run(True, home=self.home)["state"], "applied")

    def test_plan_changes_nothing(self) -> None:
        result = gm.run(False, home=self.home)
        self.assertEqual((result["state"], result["health"]), ("plan", "missing"))
        self.assertFalse(self.mirror.exists())


class Fetch(unittest.TestCase):
    def test_every_try_is_bounded_and_retried_with_the_low_speed_abort(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            mirror = Path(temp)
            (mirror / "objects/pack").mkdir(parents=True)
            calls, sleeps = [], []

            def runner(command, timeout):
                calls.append((list(command), timeout))
                (mirror / "objects/pack/tmp_pack_x").write_text("partial")
                return (124, "timed out") if len(calls) < 3 else (0, "")

            result = gm.fetch(mirror, "https://example.test/r.git", runner=runner, sleep=sleeps.append)
            self.assertTrue(result["ok"])
            self.assertEqual(result["tries"], 3)
            self.assertEqual(sleeps, list(gm.BACKOFF))
            for command, timeout in calls:
                self.assertEqual(timeout, gm.FETCH_TIMEOUT)
                self.assertEqual(command[1:5], list(gm.LOW_SPEED))
                self.assertNotIn("--depth", " ".join(command))

    def test_a_download_cut_off_part_way_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target, calls = Path(temp) / "bundle", []

            def curl(command, **_):
                calls.append(command)
                if len(calls) == 1:
                    target.write_bytes(b"half")
                    return subprocess.CompletedProcess(command, 28)
                with target.open("ab") as out:
                    out.write(b" and the rest")
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(gm.subprocess, "run", curl), mock.patch.object(gm.time, "sleep"):
                gm.download("https://bucket.test/x", target, 60)
            self.assertEqual(target.read_bytes(), b"half and the rest")
            self.assertEqual(len(calls), 2)
            self.assertIn("--continue-at", calls[1])

    def test_a_missing_object_is_asked_for_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            with mock.patch.object(gm.subprocess, "run",
                                   lambda command, **_: calls.append(command) or subprocess.CompletedProcess(command, 22)):
                with self.assertRaises(subprocess.CalledProcessError):
                    gm.download("https://bucket.test/x", Path(temp) / "x", 60)
            self.assertEqual(len(calls), 1)

    def test_a_child_does_no_work_until_its_group_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "worked"
            script_path = os.fspath(ROOT / "scripts/glaeda-git-mirror")
            # The pass is SIGKILLed between starting the child and recording its group.
            script = ("import importlib.machinery, importlib.util, os, signal, sys\n"
                      f"loader = importlib.machinery.SourceFileLoader('gm', {script_path!r})\n"
                      "spec = importlib.util.spec_from_loader('gm', loader); gm = importlib.util.module_from_spec(spec)\n"
                      "loader.exec_module(gm)\n"
                      "gm.note_groups = lambda groups: os.kill(os.getpid(), signal.SIGKILL)\n"
                      f"gm.run_group(['/usr/bin/touch', {os.fspath(marker)!r}], 30)\n")
            proc = subprocess.run([sys.executable, "-c", script], timeout=30)
            self.assertEqual(proc.returncode, -signal.SIGKILL)
            time.sleep(1)  # the orphaned child reads EOF and exits instead of running the command
            self.assertFalse(marker.exists())
            self.assertEqual(gm.run_group(["/usr/bin/touch", os.fspath(marker)], 30)[0], 0)
            self.assertTrue(marker.exists())

    def test_an_interrupted_pass_stops_its_child(self) -> None:
        def interrupt(signum, frame):
            raise KeyboardInterrupt
        old = signal.signal(signal.SIGALRM, interrupt)
        self.addCleanup(signal.signal, signal.SIGALRM, old)
        started = []
        real = gm.subprocess.Popen

        def popen(*args, **kwargs):
            started.append(real(*args, **kwargs))
            return started[-1]

        signal.alarm(1)
        with mock.patch.object(gm.subprocess, "Popen", popen), self.assertRaises(KeyboardInterrupt):
            gm.run_group(["/bin/sleep", "30"], timeout=60)
        self.assertIsNotNone(started[0].poll())
        with self.assertRaises(ProcessLookupError):
            os.killpg(started[0].pid, 0)

    def test_a_git_timeout_ends_its_children(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            child = Path(temp) / "child.pid"
            fake = Path(temp) / "git"  # stands in for git: a child (index-pack) that outlives it
            fake.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {child}\nwait\n")
            fake.chmod(0o755)
            with mock.patch.object(gm, "GIT", os.fspath(fake)):
                with self.assertRaises(subprocess.TimeoutExpired):
                    gm.git(Path(temp), "bundle", "unbundle", "x", timeout=1)
            pid = int(child.read_text())
            time.sleep(0.2)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_run_group_kills_a_hung_command(self) -> None:
        status, why = gm.run_group(["sh", "-c", "sleep 30 & sleep 30"], timeout=1)
        self.assertEqual(status, 124)
        self.assertIn("timed out", why)


if __name__ == "__main__":
    unittest.main()
