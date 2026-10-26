#!/usr/bin/env python3
"""Contract tests for scripts/glaeda-disk."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import contextlib
import collections
import fcntl
import hashlib
import io
import json
import errno
import os
import plistlib
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT / "scripts"))
import glaeda_hook_pin  # noqa: E402

# pin a changed runner hook before glaeda-disk loads, so its drain tests see the pin on the first run (CI only checks)
if not glaeda_hook_pin.under_ci():
    glaeda_hook_pin.ensure(check=False)
loader = importlib.machinery.SourceFileLoader("glaeda_disk", os.fspath(ROOT / "scripts" / "glaeda-disk"))
spec = importlib.util.spec_from_loader("glaeda_disk", loader)
gd = importlib.util.module_from_spec(spec)
sys.modules["glaeda_disk"] = gd
loader.exec_module(gd)
# An --apply in a test must never reap the real $TMPDIR of the host running the suite: without a tmp of
# the test's own, the TMPDIR reaper does nothing.
_reap_tmpdir_test_residue = gd.reap_tmpdir_test_residue
gd.reap_tmpdir_test_residue = (lambda receipt, tmp=None, halt=None:
                               0 if tmp is None else _reap_tmpdir_test_residue(receipt, tmp, halt))
# Nor git gc the clones in the real home of the host running the suite: only a test's own home is gc'd.
_gc_home_clones = gd.gc_home_clones
gd.gc_home_clones = lambda receipt, home=None, halt=None: 0 if home is None else _gc_home_clones(receipt, home, halt)


def make(path: Path, mib: int = 1, age_hours: float = 48) -> Path:
    path.mkdir(parents=True)
    (path / "blob").write_bytes(b"\0" * (mib * 1024 * 1024))
    t = time.time() - age_hours * 3600
    for p in (path / "blob", path):
        os.utime(p, (t, t))
    return path


class GlaedaDiskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.fam = gd.Family("xcode-derived-data", self.root, True, "rebuild")
        self.gd_evidence = gd.process_evidence
        gd.process_evidence = lambda: ([], "")
        gd._DISCOVERED.clear()
        self.gd_keep = gd.NESTED_KEEP
        gd.NESTED_KEEP = self.root.parent / f"{self.root.name}-keep.json"

    def tearDown(self) -> None:
        gd.process_evidence = self.gd_evidence
        gd.FLOOR_HOLD = {}  # a main() pressure run in a test sets it
        gd.NESTED_KEEP.unlink(missing_ok=True)
        gd.NESTED_KEEP = self.gd_keep
        self.tmp.cleanup()

    def test_du_walk_timeout_kills_child_and_returns_unsized(self) -> None:
        class HungDu:
            pid = 7319
            returncode = None

            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.timeout = None
                self.killed = False

            def communicate(self, timeout=None):
                self.timeout = timeout
                raise subprocess.TimeoutExpired(self.argv, timeout)

        with mock.patch.object(gd.subprocess, "Popen", HungDu), \
                mock.patch.object(gd.os, "killpg") as killpg, \
                mock.patch.object(gd, "eviction_activity", return_value=None):
            self.assertIsNone(gd.du_bytes(self.root))
            self.assertIsNone(gd.du_children(self.root))
        self.assertEqual(killpg.call_count, 2)
        self.assertTrue(all(call.args[1] == gd.signal.SIGKILL for call in killpg.call_args_list))
        self.assertEqual(gd.EVICTION_STUCK[-1]["id"], "eviction_stuck")
        self.assertIn(str(self.root), gd.EVICTION_STUCK[-1]["path"])

    def test_report_only_du_timeout_stays_out_of_eviction_findings(self) -> None:
        class HungDu:
            pid = 7320
            returncode = None

            def __init__(self, argv, **kwargs):
                self.argv = argv

            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired(self.argv, timeout)

        saved = list(gd.EVICTION_STUCK)
        gd.EVICTION_STUCK.clear()
        self.addCleanup(lambda: (gd.EVICTION_STUCK.clear(), gd.EVICTION_STUCK.extend(saved)))
        with mock.patch.object(gd.subprocess, "Popen", HungDu), \
                mock.patch.object(gd.os, "killpg"):
            self.assertIsNone(gd.du_bytes(self.root, record_timeout=False))
            self.assertIsNone(gd.du_children_with_status(self.root, record_timeout=False))
        self.assertEqual(gd.EVICTION_STUCK, [])

    def test_eviction_timeout_findings_aggregate_by_path(self) -> None:
        saved = list(gd.EVICTION_STUCK)
        gd.EVICTION_STUCK.clear()
        self.addCleanup(lambda: (gd.EVICTION_STUCK.clear(), gd.EVICTION_STUCK.extend(saved)))
        argv = ["du", "-xsk", str(self.root)]
        with mock.patch.object(gd, "eviction_activity", return_value=None):
            gd._record_eviction_stuck(argv)
            gd._record_eviction_stuck(argv)
        self.assertEqual(len(gd.EVICTION_STUCK), 1)
        self.assertEqual(gd.EVICTION_STUCK[0]["count"], 2)
        self.assertEqual(gd.EVICTION_STUCK[0]["path"], str(self.root))

    def test_timed_out_path_is_not_rewalked_during_cooldown(self) -> None:
        saved_state = gd.TIMEOUT_STATE
        saved_paths = dict(gd._TIMED_OUT_PATHS)
        gd.TIMEOUT_STATE = self.root / "timeout-paths.json"
        gd._TIMED_OUT_PATHS.clear()
        self.addCleanup(lambda: (setattr(gd, "TIMEOUT_STATE", saved_state),
                                  gd._TIMED_OUT_PATHS.clear(), gd._TIMED_OUT_PATHS.update(saved_paths)))
        gd._record_eviction_stuck(["du", "-xsk", str(self.root)])
        with mock.patch.object(gd, "_du_output") as du:
            self.assertIsNone(gd.du_bytes(self.root))
        du.assert_not_called()

    def test_eviction_timeout_marks_active_host_contention(self) -> None:
        saved = list(gd.EVICTION_STUCK)
        gd.EVICTION_STUCK.clear()
        self.addCleanup(lambda: (gd.EVICTION_STUCK.clear(), gd.EVICTION_STUCK.extend(saved)))
        argv = ["du", "-xsk", str(self.root)]
        with mock.patch.object(gd, "eviction_activity", return_value={
                "state": "active", "evidence": "host.lock pid 7319 (worker-current)"}), \
                mock.patch.object(gd.time, "time", return_value=1_790_500_000):
            gd._record_eviction_stuck(argv)
        finding = gd.EVICTION_STUCK[0]
        self.assertEqual((finding["state"], finding["severity"], finding["auto_fix"]),
                         ("active", "warn", "deferred"))
        self.assertEqual(finding["observed_at"], 1_790_500_000)
        self.assertIn("worker-current", finding["evidence"])
        self.assertEqual(finding["activity"]["state"], "active")

    def test_eviction_activity_accepts_only_live_marker_pid(self) -> None:
        fleet = self.root / "fleet"
        fleet.mkdir()
        (fleet / "evict-lock-v1").write_text("7319\n")
        with mock.patch.object(gd, "FLEET_DIR", fleet), \
                mock.patch.object(gd, "_pid_alive", return_value=True), \
                mock.patch.object(gd, "_owner_command", return_value="worker-current"):
            self.assertEqual(gd.eviction_activity(), {
                "state": "active", "evidence": "evict-lock-v1 pid 7319 (worker-current)"})
        with mock.patch.object(gd, "FLEET_DIR", fleet), \
                mock.patch.object(gd, "_pid_alive", return_value=False), \
                mock.patch.object(gd.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="")):
            self.assertIsNone(gd.eviction_activity())

    def test_eviction_activity_reads_a_live_host_lock(self) -> None:
        fleet = self.root / "fleet"
        fleet.mkdir()
        lock = fleet / "host.lock"
        with lock.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(gd, "FLEET_DIR", fleet), \
                    mock.patch.object(gd.subprocess, "run") as run:
                activity = gd.eviction_activity()
            run.assert_not_called()
        self.assertEqual(activity, {"state": "active", "evidence": "fleet host.lock is held"})

    def test_du_root_walk_refuses_filesystem_root(self) -> None:
        with mock.patch.object(gd, "_du_output") as run:
            self.assertIsNone(gd.du_children(Path("/")))
        run.assert_not_called()

    def test_fixed_measurement_roots_skip_fileprovider_and_root(self) -> None:
        saved = (gd.HOME, gd.FLEET_ROOT, gd.DARWIN)
        gd.HOME = self.root
        gd.FLEET_ROOT = self.root / "shared"
        gd.DARWIN = True
        try:
            roots = gd.fixed_measurement_roots()
            self.assertTrue(roots)
            self.assertNotIn(Path("/"), roots)
            self.assertFalse(any("FileProvider" in p.parts for p in roots))
            self.assertIn(gd.FLEET_ROOT / "cache", roots)
            self.assertIn(gd.FLEET_ROOT / "ci-ios", roots)
        finally:
            gd.HOME, gd.FLEET_ROOT, gd.DARWIN = saved

    def test_runner_cache_profile_is_allowlisted_and_bounded(self) -> None:
        saved = (gd.HOME, gd.DARWIN, gd.RUNNER_CACHE_PROFILE)
        gd.HOME = self.root
        gd.DARWIN = True
        gd.RUNNER_CACHE_PROFILE = True
        try:
            for relative in (
                "Library/Developer/Xcode/DerivedData",
                ".cache/cmux-job",
                ".cache/cmux/ghosttykit",
                "Library/Application Support/cua-ssh/recordings",
                "Library/Caches/cmux/cmux-cua",
            ):
                (self.root / relative).mkdir(parents=True)
            families = gd.runner_cache_families()
            self.assertEqual(
                {family.id for family in families},
                {"xcode-derived-data", "cmux-job-cache", "cmux-ghosttykit-cache",
                 "cua-recordings", "cua-cache"},
            )
            self.assertTrue(all(family.min_idle_hours == 168.0 for family in families))
            self.assertTrue(gd.runner_profile_roots_safe(families))
            measured = gd.fixed_measurement_roots()
            self.assertEqual(set(measured), {family.root for family in families})
            self.assertNotIn(self.root / ".cache", measured)
            self.assertNotIn(self.root / "Library/Caches", measured)
        finally:
            gd.HOME, gd.DARWIN, gd.RUNNER_CACHE_PROFILE = saved

    def test_runner_cache_profile_keeps_git_index_refresh_out_of_age_clock(self) -> None:
        saved = gd.RUNNER_CACHE_PROFILE
        gd.RUNNER_CACHE_PROFILE = True
        try:
            family = gd.Family("cmux-job-cache", self.root, True, "rebuild", min_idle_hours=168)
            git = self.root / ".git"
            git.mkdir()
            now = time.time()
            old = now - 200 * 3600
            os.utime(self.root, (old, old))
            os.utime(git, (now, now))
            self.assertLess(gd.item_mtime(family, self.root), now - 190 * 3600)
            for mode in (None, "pressure", "sweep"):
                with mock.patch.object(gd, "ACTIVITY_MODE", mode):
                    self.assertFalse(gd.item_due(family, 167, 168)[0], mode)
                    self.assertTrue(gd.item_due(family, 168, 168)[0], mode)
        finally:
            gd.RUNNER_CACHE_PROFILE = saved

    def test_eviction_lock_owner_is_recorded_and_released(self) -> None:
        lock_path, owner_path = gd.EVICT_LOCK, gd.EVICT_OWNER
        gd.EVICT_LOCK = self.root / "evict.lock"
        gd.EVICT_OWNER = self.root / "evict.owner.json"
        try:
            handle = gd.acquire_evict_lock()
            self.assertIsNotNone(handle)
            owner = json.loads(gd.EVICT_OWNER.read_text())
            self.assertEqual(owner["pid"], os.getpid())
            self.assertEqual(owner["owner"], gd.pwd.getpwuid(os.getuid()).pw_name)
            self.assertIn(f"pid={os.getpid()}", gd.eviction_owner_text(gd.eviction_owner_status()))
            gd.release_evict_lock(handle)
            self.assertFalse(gd.EVICT_OWNER.exists())
            with gd.EVICT_LOCK.open("a") as other:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            gd.EVICT_LOCK = lock_path
            gd.EVICT_OWNER = owner_path
            gd._LIVE_REFERENCES.clear()

    def test_paused_runner_work_is_cleared_only_for_held_idle_runner(self) -> None:
        saved_home = gd.HOME
        gd.HOME = self.root
        try:
            runner = self.root / "actions-runner-glaeda-2"
            work = runner / "_work/cmux"
            work.mkdir(parents=True)
            (work / "old-build").write_bytes(b"old")
            (runner / ".runner").write_text("{}")
            (runner / "glaeda-hooks").mkdir()
            held = self.root / gd.RUNNER_HELD_DIR / runner.name
            held.parent.mkdir(parents=True)
            held.write_text("held")
            receipt = self.root / "receipt.jsonl"
            with mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                    mock.patch.object(gd, "du_bytes", return_value=1234):
                self.assertEqual(gd.cleanup_paused_runner_work(receipt), 1234)
            self.assertFalse(work.exists())
            record = json.loads(receipt.read_text())
            self.assertEqual(record["family"], "paused-runner-work")
            # Without the explicit drain marker, the same idle-looking tree remains intact.
            work.mkdir(parents=True)
            (work / "new").write_bytes(b"new")
            held.unlink()
            with mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                    mock.patch.object(gd, "du_bytes", return_value=1234):
                self.assertEqual(gd.cleanup_paused_runner_work(receipt), 0)
            self.assertTrue(work.exists())
        finally:
            gd.HOME = saved_home

    def test_timed_out_bulk_walk_does_not_cache_zero_sizes(self) -> None:
        self.fam = gd.Family("user-tmp", self.root, True, "scratch", bulk_sizes=True)
        make(self.root / "a", mib=2)
        with mock.patch.object(gd, "du_children", return_value=None), \
                mock.patch.object(gd, "du_bytes", return_value=None):
            items = gd.survey([self.fam], 24, 1 << 20)
        self.assertEqual(items, [])

    def test_refresh_budget_does_not_start_walks_after_deadline(self) -> None:
        make(self.root / "a", mib=2)
        with mock.patch.object(gd, "du_bytes") as du:
            items = gd.survey([self.fam], 24, 1 << 20,
                              deadline=time.monotonic() - 1)
        self.assertEqual(items, [])
        du.assert_not_called()
        self.assertTrue(gd._MEASURE_BUDGET_REACHED)

    def verdicts(self, idle: float = 24) -> dict[str, str]:
        return {Path(i.path).name: i.verdict for i in gd.survey([self.fam], idle, 0)}

    def test_idle_is_reclaimable_recent_is_not(self) -> None:
        make(self.root / "old")
        make(self.root / "new", age_hours=1)
        self.assertEqual(self.verdicts(), {"old": "reclaimable", "new": "recent"})

    def test_tmpdir_double_slash_still_names_the_path(self) -> None:
        cmds = gd.command_text("bash /var/folders/px/abc/T//work/build.sh\n")
        self.assertTrue(gd.named_by("/private/var/folders/px/abc/T/work", cmds))
        self.assertFalse(gd.named_by("/private/var/folders/px/abc/T/wor", cmds))

    def test_process_cwd_and_command_line_veto(self) -> None:
        a, b = make(self.root / "a"), make(self.root / "b")
        make(self.root / "a-sibling")
        gd.process_evidence = lambda: ([str(a / "sub")], f"xcodebuild -derivedDataPath {b} build\n")
        v = self.verdicts()
        self.assertEqual(v["a"], "in-use")
        self.assertEqual(v["b"], "in-use")
        # a prefix of another path must not veto it
        self.assertEqual(v["a-sibling"], "reclaimable")

    def test_git_checkout_is_never_reclaimable_outside_derived_data(self) -> None:
        self.fam = gd.Family("tmp", self.root, True, "scratch")
        repo = make(self.root / "repo")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        os.utime(repo, (time.time() - 48 * 3600,) * 2)
        os.utime(repo / ".git", (time.time() - 48 * 3600,) * 2)
        self.assertEqual(gd.survey([self.fam], 24, 0)[0].verdict, "git-checkout")

    def test_referenced_main_checkout_is_kept(self) -> None:
        home = self.root / "home"
        home.mkdir()
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "README").write_text("ok\n")
        subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        remote = self.root / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
        subprocess.run(["git", "-C", str(repo), "push", "-q", "-u", "origin", "HEAD"], check=True)
        (home / ".zshrc").write_text(f"source {repo}/shell.zsh\n")
        old = time.time() - 48 * 3600
        os.utime(repo, (old, old))
        os.utime(repo / ".git", (old, old))
        fam = gd.Family("tmp", self.root, True, "scratch", git_disposable=True)
        with mock.patch.object(gd, "HOME", home):
            item = next(i for i in gd.survey([fam], 24, 0) if i.path == str(repo))
            self.assertEqual(item.verdict, "in-use")
            self.assertTrue(any("referenced" in reason for reason in item.reasons))
            receipt = self.root / "referenced.jsonl"
            item.verdict = "reclaimable"  # exercise the apply-time reference recheck
            gd.apply([item], {fam.id: fam}, receipt, None, 24)
        self.assertTrue(repo.exists())
        self.assertIn("changed:referenced", receipt.read_text())

    def test_tmp_checkout_git_metadata_does_not_reset_idle_clock(self) -> None:
        self.fam = gd.Family("tmp", self.root, True, "scratch", git_disposable=True,
                             max_idle_hours=1.0)
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        old = time.time() - 48 * 3600
        os.utime(repo, (old, old))
        os.utime(repo / ".git", (time.time(), time.time()))
        age = (time.time() - gd.item_mtime(self.fam, repo)) / 3600
        self.assertGreater(age, 47)
        self.assertEqual(gd.idle_window(self.fam, 6), 1.0)

    def test_bulk_sizes_walk_the_root_once_and_skip_prefixes(self) -> None:
        self.fam = gd.Family("user-tmp", self.root, True, "scratch", bulk_sizes=True,
                             skip_prefixes=("com.apple.",))
        for name in ("a", "b", "c"):
            make(self.root / name, mib=2)
        saved_min, gd.BULK_SIZE_MIN = gd.BULK_SIZE_MIN, 2
        make(self.root / "com.apple.imtransferservices")
        calls: list[list[str]] = []
        real_du_output = gd._du_output

        def counting_du_output(args, **kwargs):
            if args and args[0] == "du":
                calls.append(list(args))
            return real_du_output(args, **kwargs)

        gd._du_output = counting_du_output
        try:
            items = gd.survey([self.fam], 24, 1 << 20)
        finally:
            gd._du_output = real_du_output
            gd.BULK_SIZE_MIN = saved_min
        self.assertEqual(sorted(Path(i.path).name for i in items), ["a", "b", "c"])
        self.assertTrue(all(i.bytes >= 2 << 20 and i.verdict == "reclaimable" for i in items))
        self.assertEqual([c[:4] for c in calls], [["du", "-xk", "-d", "1"]])  # one walk, no per-item du
        # a report defers unsized bulk candidates to the background refresh instead of walking
        make(self.root / "d", mib=2)
        known = {i.path: i.bytes for i in items}
        deferred = gd.survey([self.fam], 24, 1 << 20, known, defer_bulk=True)
        self.assertEqual(sorted(Path(i.path).name for i in deferred), ["a", "b", "c"])

    def test_darwin_user_tmp_family(self) -> None:
        saved = (gd.DARWIN, gd.darwin_user_tmp)
        gd.DARWIN, gd.darwin_user_tmp = True, lambda: self.root
        try:
            fams = {f.id: f for f in gd.default_families()}
        finally:
            gd.DARWIN, gd.darwin_user_tmp = saved
        fam = fams["user-tmp"]
        self.assertEqual(fam.root, self.root)
        self.assertTrue(fam.reclaimable and fam.git_disposable and not fam.bulk_sizes)
        self.assertIn("com.apple.", fam.skip_prefixes)

    def _clone_tree(self) -> tuple[Path, Path]:
        user_tmp, clones = self.root / "T", self.root / "X"
        user_tmp.mkdir()
        bundle = clones / "com.cmuxterm.app.debug.ci.media.pr.1.head.code_sign_clone"
        for name, age_h in (("code_sign_clone.Old1", 30), ("code_sign_clone.Old2", 20), ("code_sign_clone.New", 10)):
            deep = bundle / name / "cmux DEV.app.bundle/Contents/Frameworks/X.framework/Versions/A/Resources/r"
            deep.mkdir(parents=True)
            (deep / "blob").write_bytes(b"x" * (2 << 20))
            stamp = time.time() - age_h * 3600
            for d, dirs, files in os.walk(bundle / name):
                for f in files:
                    os.utime(Path(d) / f, (stamp, stamp))
            for d, dirs, files in list(os.walk(bundle / name))[::-1]:
                os.utime(d, (stamp, stamp))
        (bundle / "stray").mkdir()
        (clones / "com.apple.Safari.code_sign_clone" / "code_sign_clone.Q1").mkdir(parents=True)
        return user_tmp, clones

    def _clone_family(self, user_tmp: Path) -> "gd.Family":
        saved = (gd.DARWIN, gd.darwin_user_tmp)
        gd.DARWIN, gd.darwin_user_tmp = True, lambda: user_tmp
        try:
            return next(f for f in gd.default_families() if f.id == "code-sign-clones")
        finally:
            gd.DARWIN, gd.darwin_user_tmp = saved

    def test_code_sign_clones_family(self) -> None:
        user_tmp, clones = self._clone_tree()
        fam = self._clone_family(user_tmp)
        self.assertEqual(fam.root, clones)
        self.assertTrue(fam.reclaimable and fam.app_clones and fam.build_output)
        self.assertGreaterEqual(fam.min_idle_hours, 2.0)
        with mock.patch.object(gd.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "/bin/zsh\n", "")):
            names = sorted(p.name for p in gd.candidates(fam))
        # the newest copy of the bundle stays; Apple bundles and stray names are never candidates
        self.assertEqual(names, ["code_sign_clone.Old1", "code_sign_clone.Old2"])

    def test_code_sign_clones_of_a_running_app_are_kept(self) -> None:
        user_tmp, _ = self._clone_tree()
        fam = self._clone_family(user_tmp)
        level = [p for p in (fam.root / "com.cmuxterm.app.debug.ci.media.pr.1.head.code_sign_clone").iterdir()
                 if p.name.startswith("code_sign_clone.")]
        running = "/Users/cmux/Apps/cmux DEV.app/Contents/MacOS/cmux --flag\n"
        self.assertEqual(gd.keep_live_app_clones(level, running), [])
        self.assertEqual(len(gd.keep_live_app_clones(level, "/bin/zsh\n")), 2)
        with mock.patch.object(gd.subprocess, "run", side_effect=OSError("no ps")):
            self.assertEqual(gd.keep_live_app_clones(level), [])  # no process list: keep everything

    def test_code_sign_clones_deep_bundle_is_reclaimable(self) -> None:
        user_tmp, _ = self._clone_tree()
        fam = self._clone_family(user_tmp)
        with mock.patch.object(gd, "keep_live_app_clones", side_effect=lambda level, commands=None: level):
            items = gd.survey([fam], 6, 0)
        verdicts = {Path(i.path).name: i.verdict for i in items}
        self.assertEqual(verdicts["code_sign_clone.Old1"], "reclaimable", verdicts)
        self.assertEqual(verdicts["code_sign_clone.Old2"], "reclaimable", verdicts)

    def test_no_code_sign_clones_family_without_the_directory(self) -> None:
        user_tmp = self.root / "T"
        user_tmp.mkdir()
        saved = (gd.DARWIN, gd.darwin_user_tmp)
        gd.DARWIN, gd.darwin_user_tmp = True, lambda: user_tmp
        try:
            ids = {f.id for f in gd.default_families()}
        finally:
            gd.DARWIN, gd.darwin_user_tmp = saved
        self.assertNotIn("code-sign-clones", ids)

    def test_default_user_tmp_sizes_children_without_bulk_root_walk(self) -> None:
        saved = (gd.DARWIN, gd.darwin_user_tmp)
        gd.DARWIN, gd.darwin_user_tmp = True, lambda: self.root
        try:
            fam = next(f for f in gd.default_families() if f.id == "user-tmp")
        finally:
            gd.DARWIN, gd.darwin_user_tmp = saved
        for index in range(gd.BULK_SIZE_MIN + 1):
            (self.root / f"scratch-{index}").mkdir()
        with mock.patch.object(gd, "du_children", side_effect=AssertionError("bulk root walk")), \
                mock.patch.object(gd, "du_bytes", return_value=2 * 1024 * 1024):
            items = gd.survey([fam], 24, 1 << 20)
        self.assertEqual(len(items), gd.BULK_SIZE_MIN + 1)

    def test_report_only_family_is_never_deleted(self) -> None:
        self.fam = gd.Family("user-cache", self.root, False, "report")
        make(self.root / "cache")
        items = gd.survey([self.fam], 24, 0)
        self.assertEqual(items[0].verdict, "report-only")
        receipt = self.root.parent / f"{self.root.name}-receipt.jsonl"
        gd.apply(items, {"user-cache": self.fam}, receipt, None, 24)
        self.assertTrue((self.root / "cache").exists())
        self.assertFalse(receipt.exists())

    def test_apply_deletes_and_writes_receipt(self) -> None:
        make(self.root / "old")
        receipt = self.root.parent / f"{self.root.name}-receipt.jsonl"
        try:
            items = gd.survey([self.fam], 24, 0)
            gd.apply(items, {self.fam.id: self.fam}, receipt, None, 24)
            self.assertFalse((self.root / "old").exists())
            self.assertIn('"outcome": "reclaimed"', receipt.read_text())
        finally:
            receipt.unlink(missing_ok=True)

    def test_apply_rechecks_and_skips_item_touched_since_survey(self) -> None:
        d = make(self.root / "old")
        items = gd.survey([self.fam], 24, 0)
        (d / "fresh").write_text("x")
        receipt = self.root.parent / f"{self.root.name}-receipt.jsonl"
        try:
            gd.apply(items, {self.fam.id: self.fam}, receipt, None, 24)
            self.assertTrue(d.exists())
            self.assertEqual(items[0].verdict, "blocked")
            self.assertIn("changed:modified", receipt.read_text())
        finally:
            receipt.unlink(missing_ok=True)

    def test_git_clone_two_levels_down_is_a_checkout(self) -> None:
        self.fam = gd.Family("claude-scratchpad", self.root, True, "scratch")
        session = make(self.root / "session")
        (session / "scratchpad" / "repo" / ".git").mkdir(parents=True)
        for p in (session / "scratchpad" / "repo" / ".git", session / "scratchpad" / "repo",
                  session / "scratchpad", session):
            os.utime(p, (time.time() - 48 * 3600,) * 2)
        self.assertEqual(gd.survey([self.fam], 24, 0)[0].verdict, "git-checkout")

    def test_deep_tree_is_unchecked_not_git_and_build_dirs_are_skipped(self) -> None:
        deep = self.root / "deep"
        (deep / "a/b/c/d/e/f/g").mkdir(parents=True)
        self.assertEqual(gd.git_state(deep), "unchecked")
        mods = self.root / "mods"
        (mods / "node_modules/x/y/z/w/v/u").mkdir(parents=True)
        self.assertEqual(gd.git_state(mods), "none")
        (self.root / "clone/target/.git").mkdir(parents=True)
        self.assertEqual(gd.git_state(self.root / "clone"), "git")
        (self.root / "repo/sub/.git").mkdir(parents=True)
        self.assertEqual(gd.git_state(self.root / "repo"), "git")

    @staticmethod
    def age(*tops: Path, hours: float = 48) -> None:
        old = time.time() - hours * 3600
        for top in tops:
            for dirpath, dirs, files in os.walk(top):
                for n in dirs + files:
                    os.utime(os.path.join(dirpath, n), (old, old), follow_symlinks=False)
            os.utime(top, (old, old))

    @staticmethod
    def hex_store(root: Path, shards: int = 20, files: int = 5) -> Path:
        for i in range(shards):
            shard = root / f"{i + 0xa0:02x}"
            shard.mkdir(parents=True)
            for j in range(files):
                (shard / f"{i:02x}{j:062x}-a").write_bytes(b"x")
        return root

    def test_sealed_caches_are_recognized_by_content_not_name(self) -> None:
        # The Go build cache outgrew git_state's budget and stayed "unchecked" forever. A tree whose
        # layout a tool writes is sealed: only its top two levels are searched, whatever its name.
        go = self.hex_store(self.root / "whatever-name")
        self.assertEqual(gd.cache_layout(go), "hash-sharded-store")
        self.assertEqual(gd.git_state(go, budget=50, sealed=True), "none")
        # a family that deletes disposable checkouts (tmp, scratch) never takes the shortcut
        self.assertEqual(gd.git_state(go, budget=50), "unchecked")
        plain = self.root / "plain"
        for i in range(20):
            (plain / f"d{i}").mkdir(parents=True)
            for j in range(5):
                (plain / f"d{i}" / f"f{j}").write_bytes(b"x")
        self.assertEqual(gd.git_state(plain, budget=50, sealed=True), "unchecked")
        chrome = self.root / "Browser/Default/Cache/Cache_Data"
        (chrome / "index-dir").mkdir(parents=True)
        (chrome / "index-dir/the-real-index").write_bytes(b"x")
        # an index and a data_0 are any dataset's names; Chromium's blockfile index carries its magic
        dataset = self.root / "dataset"
        dataset.mkdir()
        for n in ("index", "data_0", "data_1", "data_2", "data_3"):
            (dataset / n).write_bytes(b"rows")
        self.assertEqual(gd.cache_layout(dataset), "")
        (dataset / "index").write_bytes(gd.CHROMIUM_INDEX_MAGIC + b"rest")
        self.assertEqual(gd.cache_layout(dataset), "chromium-disk-cache")
        for j in range(100):
            (chrome / f"{j:016x}_0").write_bytes(b"x")
        self.assertEqual(gd.cache_layout(chrome), "chromium-disk-cache")
        self.assertEqual(gd.git_state(self.root / "Browser", budget=50, sealed=True), "none")
        tagged = self.root / "tagged"
        (tagged / "a/b/c/d/e/f/g").mkdir(parents=True)
        self.assertEqual(gd.git_state(tagged, sealed=True), "unchecked")
        (tagged / "CACHEDIR.TAG").write_bytes(gd.CACHEDIR_TAG + b"\n")
        self.assertEqual(gd.git_state(tagged, sealed=True), "none")
        # a FIFO carrying the name is never opened (the read would block)
        fifo = self.root / "fifo"
        (fifo / "a/b/c/d/e/f/g").mkdir(parents=True)
        os.mkfifo(fifo / "CACHEDIR.TAG")
        self.assertEqual(gd.cache_layout(fifo), "")
        self.assertEqual(gd.git_state(fifo, sealed=True), "unchecked")
        # ~/.cache/bazel on Linux: an output user root whose bases hold bazel's own fetched clones
        bazel = self.root / "bazel/_bazel_me"
        (bazel / "install/abc").mkdir(parents=True)
        (bazel / ("0" * 31 + "a") / "external/rules_x/.git").mkdir(parents=True)
        self.assertEqual(gd.cache_layout(bazel), "bazel-output-root")
        self.assertEqual(gd.git_state(self.root / "bazel", sealed=True), "none")
        (self.root / "notbazel/install").mkdir(parents=True)
        (self.root / "notbazel" / ("0" * 31 + "a") / "src/repo/.git").mkdir(parents=True)
        self.assertEqual(gd.git_state(self.root / "notbazel", sealed=True), "git")
        # numbered directories (Maven versions) are not hex shards
        maven = self.root / "maven"
        for v in range(10, 30):
            (maven / str(v)).mkdir(parents=True)
            (maven / str(v) / "x.pom").write_bytes(b"x")
        self.assertEqual(gd.cache_layout(maven), "")
        # shards are only sampled to recognize the store; every shard is still read, so a worktree
        # in the last one is found
        mixed = self.hex_store(self.root / "mixed", files=40)
        (mixed / "b3/worktree/.git").mkdir(parents=True)
        self.assertEqual(gd.git_state(mixed, budget=50, sealed=True), "git")
        (mixed / "b3/worktree/.git").rmdir()
        self.assertEqual(gd.git_state(mixed, budget=50, sealed=True), "none")

    def test_a_clone_at_the_top_of_a_sealed_cache_is_still_kept(self) -> None:
        # sealing skips the deep search, not the shallow one: a person's clone at the top of a cache,
        # or one level down, is never deleted without a look; a clone deeper in the blobs is the tool's.
        self.fam = gd.Family("user-cache", self.root, True, "re-download")
        tag = gd.CACHEDIR_TAG + b"\n"
        top = make(self.root / "top")
        (top / "CACHEDIR.TAG").write_bytes(tag)
        (top / ".git").mkdir()
        nested = make(self.root / "nested")
        (nested / "CACHEDIR.TAG").write_bytes(tag)
        (nested / "repo/.git").mkdir(parents=True)
        deep = make(self.root / "deep")
        (deep / "CACHEDIR.TAG").write_bytes(tag)
        (deep / "a/b/.git").mkdir(parents=True)
        inner = make(self.root / "inner")  # a sealed store below an ordinary directory
        self.hex_store(inner / "store")
        (inner / "store/a0/clone/.git").mkdir(parents=True)
        self.age(top, nested, deep, inner)
        self.assertEqual(self.verdicts(), {"top": "git-checkout", "nested": "git-checkout",
                                           "deep": "reclaimable", "inner": "git-checkout"})

    def test_home_caches_are_discovered_by_layout(self) -> None:
        home = self.root / "home"
        tag = gd.CACHEDIR_TAG + b"\n"
        cargo_tag = gd.CACHEDIR_TAG + b"\n# This file is a cache directory tag created by cargo.\n"
        (home / ".npm/_cacache/index-v5").mkdir(parents=True)
        (home / ".npm/_cacache/content-v2").mkdir()
        bun = home / ".bun/install/cache"
        for i in range(10):
            (bun / f"pkg{i}@1.0.{i}@@@1").mkdir(parents=True)
        (home / ".bun/bin").mkdir()
        (home / "go/pkg/mod/cache/download").mkdir(parents=True)
        (home / "go/pkg/mod/cache/lock").write_bytes(b"")
        (home / "go/src/mine").mkdir(parents=True)
        (home / ".cargo/registry").mkdir(parents=True)
        (home / ".cargo/registry/CACHEDIR.TAG").write_bytes(cargo_tag)
        (home / ".cargo/bin").mkdir()
        # never candidates: secrets, source, a checkout's build output, the Chromium trees,
        # a project's node_modules, and a hex-sharded store (a backup repository looks the same)
        for keep in (".ssh/cache", ".config/gh/cache", "Documents/cache", "Projects/x/cache",
                     "Pictures/Wallpapers/cache",
                     "code/repo/target", "cmux-browser-fleet/cache", "app/node_modules/.cache",
                     ".my-tokens/cache", ".tool/untagged-creator"):
            (home / keep).mkdir(parents=True)
            (home / keep / "CACHEDIR.TAG").write_bytes(tag)
        # installed environments uv and direnv tag as caches, even ones naming cargo, are not recreatable
        for env in (".local/share/uv/tools/ruff", ".local/share/uv/python/cpython-3.12", "tools/.direnv",
                    "envs/myenv", ".pipx-like/venvs/x"):
            (home / env).mkdir(parents=True)
            (home / env / "CACHEDIR.TAG").write_bytes(cargo_tag)
        (home / "envs/myenv/pyvenv.cfg").write_text("home = /usr/bin")
        # cargo tags a target directory too: one outside a project (CARGO_TARGET_DIR) is a build
        (home / ".cargo-target/debug").mkdir(parents=True)
        (home / "shared-target").mkdir()
        (home / "shared-target/.rustc_info.json").write_text("{}")
        for build in (".cargo-target", "shared-target"):
            (home / build / "CACHEDIR.TAG").write_bytes(cargo_tag)
        (home / ".pipx-like/venvs/x/pyvenv.cfg").write_text("home = /usr/bin")
        (home / "code/repo/.git").mkdir()
        self.hex_store(home / "backups/restic/data")
        with mock.patch.object(gd, "HOME", home):
            found = sorted(os.path.relpath(p, home) for p in gd.discover_caches(home))
        self.assertEqual(found, [".bun/install/cache", ".cargo/registry", ".npm/_cacache", "go/pkg/mod"])
        # a tree too large for the budget hides its own caches, and only its own
        wide = home / "wide"
        for i in range(50):
            (wide / f"d{i}").mkdir(parents=True)
        (wide / "d0/CACHEDIR.TAG").write_bytes(cargo_tag)
        with mock.patch.object(gd, "HOME", home):
            found = {os.path.relpath(p, home) for p in gd.discover_caches(home, budget=20)}
        self.assertNotIn("wide/d0", found)
        self.assertIn(".cargo/registry", found)

    def test_other_filesystems_and_read_errors_fail_closed(self) -> None:
        tree = self.root / "tree"
        (tree / "mnt/data").mkdir(parents=True)
        (tree / "plain").mkdir()
        self.assertEqual(gd.git_state(tree), "none")
        real = os.DirEntry.stat
        dev = os.lstat(tree).st_dev

        def fake(entry, *a, **k):
            st = real(entry, *a, **k)
            if entry.name == "mnt":
                return os.stat_result((st.st_mode, st.st_ino, dev + 1) + tuple(st)[3:])
            return st
        with mock.patch.object(os.DirEntry, "stat", fake):
            self.assertEqual(gd.git_state(tree), "unchecked")  # a delete would descend into the mount
        real_lstat = os.lstat

        def fake_lstat(p, *a, **k):
            st = real_lstat(p, *a, **k)
            if os.fspath(p).endswith("/mnt"):
                return os.stat_result((st.st_mode, st.st_ino, dev + 1) + tuple(st)[3:])
            return st
        home = self.root / "home"
        (home / "mnt/cache/index-v5").mkdir(parents=True)
        (home / "mnt/cache/content-v2").mkdir()
        with mock.patch.object(os, "lstat", fake_lstat):
            self.assertEqual(gd.discover_caches(home), [])
        self.assertEqual(gd.discover_caches(home), [home / "mnt/cache"])
        # EIO or ESTALE while reading a directory is not evidence that nothing is there
        real_scandir = os.scandir

        def failing(p="."):
            if os.fspath(p).endswith("/plain"):
                raise OSError(5, "Input/output error")
            return real_scandir(p)
        with mock.patch.object(os, "scandir", failing):
            self.assertEqual(gd.git_state(tree), "unchecked")
        with self.assertRaisesRegex(OSError, "mount"), mock.patch.object(os.path, "ismount", return_value=True):
            gd.remove(tree / "plain")
        self.assertTrue((tree / "plain").is_dir())

    def test_apply_refreshes_only_discovered_candidates_sizes(self) -> None:
        home = self.root / "home"
        (home / ".npm/_cacache/index-v5").mkdir(parents=True)
        (home / ".npm/_cacache/content-v2").mkdir()
        (home / "code/checkout").mkdir(parents=True)
        disc = gd.Family("home-caches", home, True, "re-download", discover=True)
        other = gd.Family("xcode-derived-data", self.root / "dd", True, "rebuild")
        known = {str(home / ".npm/_cacache"): 1, str(home / "code/checkout"): 2,
                 str(self.root / "dd/x"): 3, str(self.root / "elsewhere"): 4}
        self.assertEqual(gd.fresh_for_apply(known, [disc, other]),
                         {str(home / "code/checkout"): 2, str(self.root / "elsewhere"): 4})

    def rustup_home(self, home: Path) -> Path:
        rh = home / ".rustup"
        for tc in ("stable-aarch64-apple-darwin", "nightly-aarch64-apple-darwin",
                   "1.88.0-aarch64-apple-darwin", "1.90.0-aarch64-apple-darwin"):
            lib = rh / "toolchains" / tc / "lib/rustlib"
            lib.mkdir(parents=True)
            (lib / "multirust-channel-manifest.toml").write_text("x")
            (lib / "components").write_text("rustc")
            (rh / "toolchains" / tc / "bin").mkdir()
            (rh / "toolchains" / tc / "bin/rustc").write_bytes(b"\0" * 1024 * 1024)
            (rh / "update-hashes").mkdir(exist_ok=True)
            (rh / "update-hashes" / tc).write_text("hash")
        (rh / "settings.toml").write_text('version = "12"\ndefault_toolchain = "stable"\n'
                                          '[overrides]\n"/p/x" = "1.90.0-aarch64-apple-darwin"\n')
        return rh

    def test_rustup_toolchains_other_than_the_default_go_lru(self) -> None:
        home = self.root / "home"
        rh = self.rustup_home(home)
        self.age(rh)
        # read an hour ago, written long ago: where the filesystem records access, LRU order sees it
        now = time.time()
        os.utime(rh / "toolchains/nightly-aarch64-apple-darwin/bin/rustc", (now - 3600, now - 90 * 24 * 3600))
        self.fam = gd.Family("home-caches", home, True, "re-download", discover=True)
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"CARGO_HOME": str(home / ".cargo")}):
            self.assertTrue(all(v == "kept" for v in self.verdicts().values()))  # no rustup to reinstall
            (home / ".cargo/bin").mkdir(parents=True)
            (home / ".cargo/bin/rustup").write_bytes(b"")
            self.assertEqual(self.verdicts(), {"stable-aarch64-apple-darwin": "kept",
                                               "1.90.0-aarch64-apple-darwin": "kept",
                                               "nightly-aarch64-apple-darwin": "recent",
                                               "1.88.0-aarch64-apple-darwin": "reclaimable"})
            items = gd.survey([self.fam], 24, 0)
            receipt = self.root.parent / f"{self.root.name}-r.jsonl"
            with contextlib.redirect_stdout(io.StringIO()):
                gd.apply(items, {self.fam.id: self.fam}, receipt, None, 24)
            receipt.unlink(missing_ok=True)
        self.assertFalse((rh / "toolchains/1.88.0-aarch64-apple-darwin").exists())
        # rustup would skip the reinstall while the old update hash still matches the channel
        self.assertFalse((rh / "update-hashes/1.88.0-aarch64-apple-darwin").exists())
        self.assertTrue((rh / "update-hashes/nightly-aarch64-apple-darwin").exists())
        self.assertTrue((rh / "toolchains/stable-aarch64-apple-darwin").exists())
        self.assertTrue((rh / "settings.toml").exists())

    def test_rustup_toolchains_a_checkout_or_the_environment_pins_stay(self) -> None:
        home = self.root / "home"
        rh = self.rustup_home(home)
        (home / ".cargo/bin").mkdir(parents=True)
        (home / ".cargo/bin/rustup").write_bytes(b"")
        (home / "Projects/app").mkdir(parents=True)
        (home / "Projects/app/rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.88.0"\n')
        (home / "Projects/app-worktrees/b").mkdir(parents=True)
        (home / "Projects/app-worktrees/b/rust-toolchain").write_text("nightly\n")
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"CARGO_HOME": str(home / ".cargo")}):
            os.environ.pop("RUSTUP_TOOLCHAIN", None)
            tcs = rh / "toolchains"
            self.assertTrue(gd.rustup_verdict(tcs / "1.88.0-aarch64-apple-darwin"))
            self.assertTrue(gd.rustup_verdict(tcs / "nightly-aarch64-apple-darwin"))
            (home / "Projects/app-worktrees/b/rust-toolchain").unlink()
            self.assertEqual(gd.rustup_verdict(tcs / "nightly-aarch64-apple-darwin"), "")
            os.environ["RUSTUP_TOOLCHAIN"] = "nightly"
            self.assertTrue(gd.rustup_verdict(tcs / "nightly-aarch64-apple-darwin"))

    def test_apply_rechecks_a_discovered_cache_for_a_new_clone(self) -> None:
        home = self.root / "home"
        cache = home / ".npm/_cacache"
        (cache / "index-v5").mkdir(parents=True)
        (cache / "content-v2").mkdir()
        (cache / "content-v2/blob").write_bytes(b"\0" * 1024 * 1024)
        self.age(home / ".npm")
        self.fam = gd.Family("home-caches", home, True, "re-download", discover=True)
        receipt = self.root.parent / f"{self.root.name}-r.jsonl"
        with mock.patch.object(gd, "HOME", home):
            items = gd.survey([self.fam], 24, 0)
            self.assertEqual([i.verdict for i in items], ["reclaimable"])
            (cache / "mine/.git").mkdir(parents=True)  # someone cloned into it after the survey
            self.age(home / ".npm")
            with contextlib.redirect_stdout(io.StringIO()):
                gd.apply(items, {self.fam.id: self.fam}, receipt, None, 24)
        outcome = json.loads(receipt.read_text().splitlines()[-1])["outcome"]
        receipt.unlink(missing_ok=True)
        self.assertEqual(outcome, "changed:git")
        self.assertTrue((cache / "mine/.git").is_dir())

    def test_discovered_cache_in_use_is_kept(self) -> None:
        home = self.root / "home"
        cache = home / ".npm/_cacache"
        (cache / "index-v5").mkdir(parents=True)
        (cache / "content-v2").mkdir()
        (cache / "content-v2/blob").write_bytes(b"\0" * 1024 * 1024)
        self.age(home / ".npm")
        self.fam = gd.Family("home-caches", home, True, "re-download", discover=True)
        with mock.patch.object(gd, "HOME", home):
            self.assertEqual(self.verdicts(), {"_cacache": "reclaimable"})
            gd.process_evidence = lambda: ([str(cache / "content-v2/blob")], "")
            self.assertEqual(self.verdicts(), {"_cacache": "in-use"})

    def test_bazel_output_bases_go_and_the_repository_cache_stays(self) -> None:
        # /private/var/tmp/_bazel_$USER on macOS: output bases (md5 names) and install/ are rebuilt by
        # bazel, even with fetched clones inside; cache/ is the repository cache and is never a candidate.
        self.fam = gd.bazel_family(self.root)
        base = make(self.root / "0123456789abcdef0123456789abcdef")
        (base / "external/rules_x/.git").mkdir(parents=True)
        (base / "execroot/_main").mkdir(parents=True)
        os.symlink(self.root.parent, base / "execroot/_main/outside")
        (make(self.root / "cache") / "repos/v1").mkdir(parents=True)
        old = time.time() - 48 * 3600
        for top in (base, self.root / "cache"):
            for dirpath, dirs, files in os.walk(top):
                for n in dirs + files:
                    os.utime(os.path.join(dirpath, n), (old, old), follow_symlinks=False)
            os.utime(top, (old, old))
        self.assertEqual(self.verdicts(), {base.name: "reclaimable"})
        items = [i for i in gd.survey([self.fam], 24, 0) if i.verdict == "reclaimable"]
        with contextlib.redirect_stdout(io.StringIO()):
            gd.apply(items, {self.fam.id: self.fam}, self.root.parent / f"{self.root.name}-r.jsonl", None, 24)
        (self.root.parent / f"{self.root.name}-r.jsonl").unlink(missing_ok=True)
        self.assertFalse(base.exists())
        self.assertTrue((self.root / "cache/repos/v1").is_dir() and self.root.parent.is_dir())

    def test_only_home_caches_discovers(self) -> None:
        fams = gd.default_families()
        self.assertEqual([(f.id, f.root) for f in fams if f.discover], [("home-caches", gd.HOME)])
        for n in (".ssh", ".gnupg", ".secrets", "Library", "Projects", "Documents", ".cache"):
            self.assertIn(n, gd.HOME_NEVER)

    def test_tmp_alias_and_comma_lists_count_as_named(self) -> None:
        self.assertTrue(gd.named_by("/private/tmp/foo", "tool --out /tmp/foo/dist\n"))
        self.assertTrue(gd.named_by("/private/tmp/foo", "tool --dirs=/private/tmp/foo,/x\n"))
        self.assertFalse(gd.named_by("/private/tmp/foo", "tool /tmp/foobar\n"))
        self.assertTrue(gd.in_cwd("/private/tmp/foo", ["/tmp/foo/a.log"]))
        index = gd.open_index(["/tmp/foo/a.log", "/tmp/foobar/b"])
        self.assertTrue(gd.in_cwd("/tmp/foo", index))
        self.assertTrue(gd.in_cwd("/tmp/foo/a.log", index))
        self.assertFalse(gd.in_cwd("/tmp/fo", index))
        self.assertFalse(gd.in_cwd("/tmp/foo/a", index))

    def test_missing_process_evidence_fails_closed(self) -> None:
        make(self.root / "old")

        def blind():
            raise gd.NoEvidence("lsof missing")
        gd.process_evidence = blind
        items = gd.survey([self.fam], 24, 0)
        self.assertEqual(items[0].verdict, "in-use")
        forced = [gd.Item(i.family, i.path, i.bytes, i.idle_hours, "reclaimable") for i in items]
        receipt = self.root.parent / f"{self.root.name}-receipt.jsonl"
        gd.apply(forced, {self.fam.id: self.fam}, receipt, None, 24)
        self.assertTrue((self.root / "old").exists())

    def test_remove_never_chmods_through_symlinks(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.write_text("x")
        os.chmod(outside, 0o400)
        try:
            tree = self.root / "tree"
            (tree / "ro").mkdir(parents=True)
            os.symlink(outside, tree / "ro" / "link")
            os.chmod(tree / "ro", 0o500)
            gd.remove(tree)
            self.assertFalse(tree.exists())
            self.assertEqual(outside.stat().st_mode & 0o777, 0o400)
        finally:
            os.chmod(outside, 0o600)
            outside.unlink()

    def test_known_sizes_are_reused_and_new_items_measured(self) -> None:
        old, new = make(self.root / "old"), make(self.root / "new")
        known = {str(old): 7 * 1024**3}
        sizes = {Path(i.path).name: i.bytes for i in gd.survey([self.fam], 24, 0, known)}
        self.assertEqual(sizes["old"], 7 * 1024**3)
        self.assertGreater(sizes["new"], 0)
        self.assertIn(str(new), known)  # measured size is kept for the next snapshot

    def test_saving_new_paths_keeps_the_snapshot_stamp(self) -> None:
        snap = self.root / "keep.json"
        gd.save_snapshot({"/a": 1, "/b": 2}, snap, at=1000.0)
        self.assertEqual(gd.load_snapshot(snap), (1000.0, {"/a": 1, "/b": 2}))
        gd.save_snapshot({"/a": 1}, snap)
        self.assertGreater(gd.load_snapshot(snap)[0], 1000.0)  # a full measurement is now

    def test_snapshot_round_trip_and_corrupt_snapshot_is_empty(self) -> None:
        snap = self.root / "sizes.json"
        gd.save_snapshot({"/a": 1}, snap)
        self.assertEqual(gd.load_snapshot(snap)[1], {"/a": 1})
        snap.write_text("{not json")
        self.assertEqual(gd.load_snapshot(snap), (0.0, {}))

    def test_snapshot_records_per_item_measurement_times(self) -> None:
        snap = self.root / "times.json"
        gd.save_snapshot({"/old": 1, "/new": 2}, snap, at=1000.0,
                         measured_at={"/old": 1000.0, "/new": 2000.0})
        self.assertEqual(gd.load_snapshot_meta(snap), (1000.0, {"/old": 1, "/new": 2},
                                                        {"/old": 1000.0, "/new": 2000.0}))

    def test_refresh_lock_recovers_marker_but_excludes_live_child(self) -> None:
        saved_snapshot = gd.SNAPSHOT
        gd.SNAPSHOT = self.root / "sizes.json"
        child = None
        first = second = third = None
        try:
            marker = gd.refresh_lock_path()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()  # an unlocked marker is recoverable after a killed child
            first = gd.acquire_refresh_lock()
            self.assertIsNotNone(first)
            fd = first.fileno()
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(.3)"],
                                     pass_fds=(fd,))
            first.close()  # the child now owns the same kernel lock
            first = None
            second = gd.acquire_refresh_lock()
            self.assertIsNone(second)
            child.wait(timeout=5)
            third = gd.acquire_refresh_lock()
            self.assertIsNotNone(third)
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait()
            if first is not None:
                gd.release_refresh_lock(first)
            if second is not None:
                gd.release_refresh_lock(second)
            if third is not None:
                gd.release_refresh_lock(third)
            gd.SNAPSHOT = saved_snapshot

    def test_background_refresh_passes_lock_fd_to_child(self) -> None:
        saved_snapshot = gd.SNAPSHOT
        gd.SNAPSHOT = self.root / "sizes.json"
        try:
            with mock.patch.object(gd.subprocess, "Popen") as popen:
                self.assertTrue(gd.launch_background_refresh())
            kwargs = popen.call_args.kwargs
            self.assertEqual(kwargs["pass_fds"], (kwargs["pass_fds"][0],))
            self.assertIn("--refresh-lock-fd", popen.call_args.args[0])
            self.assertEqual(kwargs["pass_fds"][0],
                             int(popen.call_args.args[0][popen.call_args.args[0].index("--refresh-lock-fd") + 1]))
            self.assertIn("--refresh-budget-s", popen.call_args.args[0])
            self.assertEqual(float(popen.call_args.args[0][popen.call_args.args[0].index("--refresh-budget-s") + 1]),
                             gd.REFRESH_BUDGET_S)
            gd.refresh_lock_path().unlink(missing_ok=True)
        finally:
            gd.SNAPSHOT = saved_snapshot

    def test_accounting_reports_df_space_outside_measured_items(self) -> None:
        path = self.root / "known"
        path.write_bytes(b"x")
        fs = gd.Fs(path.stat().st_dev, str(self.root), 2 * gd.GIB, 10 * gd.GIB, 3 * gd.GIB, 4 * gd.GIB)
        item = gd.Item("xcode-derived-data", str(path), 3 * gd.GIB, 2.0, "report-only")
        saved = (gd.DARWIN, gd.ACCOUNTING)
        gd.DARWIN, gd.ACCOUNTING = True, self.root / "accounting.json"
        gd._ACCOUNTING_MEM.clear()
        self.addCleanup(lambda: (setattr(gd, "DARWIN", saved[0]), setattr(gd, "ACCOUNTING", saved[1]),
                                  gd._ACCOUNTING_MEM.clear()))
        with mock.patch.object(gd, "fixed_measurement_roots", return_value=[self.root]), \
                mock.patch.object(gd, "du_bytes", return_value=8 * gd.GIB) as du, \
                mock.patch.object(gd, "accounting_coverage", return_value=[]), \
                mock.patch.object(gd, "apfs_container_capacity", return_value=None):
            got = gd.filesystem_accounting([fs], [item])
        du.assert_called_once_with(self.root, record_timeout=False)
        self.assertEqual(got[0]["measured"], 3 * gd.GIB)
        self.assertEqual(got[0]["unmeasured"], 5 * gd.GIB)
        self.assertEqual(got[0]["top_level"][0]["path"], str(self.root))

    def test_accounting_reconciles_overlapping_shared_runner_coverage(self) -> None:
        fs = gd.Fs(self.root.stat().st_dev, str(self.root), 2 * gd.GIB, 10 * gd.GIB, 3 * gd.GIB, 4 * gd.GIB)
        saved = (gd.DARWIN, gd.ACCOUNTING)
        gd.DARWIN, gd.ACCOUNTING = True, self.root / "accounting-coverage-gap.json"
        gd._ACCOUNTING_MEM.clear()
        gd._ACCOUNTING_COVERAGE_MEM.clear()
        self.addCleanup(lambda: (setattr(gd, "DARWIN", saved[0]), setattr(gd, "ACCOUNTING", saved[1]),
                                  gd._ACCOUNTING_MEM.clear(), gd._ACCOUNTING_COVERAGE_MEM.clear()))
        coverage = [
            {"scope": "data-users-shared", "complete": True,
             "children": [{"category": "cmux-build-fleet", "bytes": 6 * gd.GIB}]},
            {"scope": "shared-fleet", "complete": True,
             "children": [{"category": "ci", "bytes": 6 * gd.GIB}]},
            {"scope": "shared-fleet/ci", "complete": True,
             "children": [{"category": "ci", "bytes": 6 * gd.GIB}]},
        ]
        with mock.patch.object(gd, "fixed_measurement_roots", return_value=[]), \
                mock.patch.object(gd, "accounting_coverage", return_value=coverage), \
                mock.patch.object(gd, "apfs_container_capacity", return_value=None):
            got = gd.filesystem_accounting([fs], [])[0]
        self.assertEqual(got["unclassified"], 8 * gd.GIB)
        self.assertEqual(got["known_coverage"], 6 * gd.GIB)
        self.assertEqual(got["accounting_gap"], 2 * gd.GIB)

    def test_accounting_does_not_double_count_nested_items(self) -> None:
        parent = self.root / "parent"
        child = parent / "child"
        parent.mkdir()
        child.mkdir()
        fs = gd.Fs(parent.stat().st_dev, str(self.root), 2 * gd.GIB, 10 * gd.GIB, 3 * gd.GIB, 4 * gd.GIB)
        items = [gd.Item("projects", str(parent), 7 * gd.GIB, 2.0, "report-only"),
                 gd.Item("checkout-build", str(child), 3 * gd.GIB, 2.0, "reclaimable")]
        saved = (gd.DARWIN, gd.ACCOUNTING)
        gd.DARWIN, gd.ACCOUNTING = False, self.root / "accounting-overlap.json"
        gd._ACCOUNTING_MEM.clear()
        self.addCleanup(lambda: (setattr(gd, "DARWIN", saved[0]), setattr(gd, "ACCOUNTING", saved[1]),
                                  gd._ACCOUNTING_MEM.clear()))
        got = gd.filesystem_accounting([fs], items)
        self.assertEqual(got[0]["measured"], 7 * gd.GIB)
        self.assertEqual(got[0]["reclaimable"], 3 * gd.GIB)

    def test_accounting_prefix_selection_preserves_independent_overlap_sets(self) -> None:
        parent = self.root / "parent"
        child = parent / "child"
        sibling = self.root / "sibling"
        rows = [(child, 3, "reclaimable"), (parent, 7, "report-only"),
                (sibling, 5, "reclaimable"), (sibling, 1, "recent")]
        selected = gd.top_level_catalogued(sorted(rows, key=lambda row: (len(row[0].parts), str(row[0]))))
        self.assertEqual([(path, size, verdict) for path, size, verdict in selected],
                         [(parent, 7, "report-only"), (sibling, 5, "reclaimable")])

    def test_accounting_prefix_selection_scales_with_flat_catalog(self) -> None:
        rows = [(self.root / f"item-{index:05d}", 1, "report-only") for index in range(12_000)]
        started = time.monotonic()
        selected = gd.top_level_catalogued(rows)
        elapsed = time.monotonic() - started
        self.assertEqual(len(selected), len(rows))
        # A flat catalogue should remain bounded by path depth. This catches a
        # regression to scanning every previously selected item.
        self.assertLess(elapsed, 5.0)

    def test_apfs_container_capacity_is_separate_from_volume_free(self) -> None:
        saved = gd.DARWIN
        gd.DARWIN = True
        gd._APFS_CONTAINER_MEM.clear()
        self.addCleanup(lambda: (setattr(gd, "DARWIN", saved), gd._APFS_CONTAINER_MEM.clear()))
        plist = plistlib.dumps({"APFSContainerSize": 494 * gd.GIB,
                                "APFSContainerFree": 50 * gd.GIB})
        with mock.patch.object(gd.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, plist, b"")) as run:
            got = gd.apfs_container_capacity("/System/Volumes/Data", now=100.0)
            again = gd.apfs_container_capacity("/System/Volumes/Data", now=101.0)
        self.assertEqual(got, {"status": "observed", "total": 494 * gd.GIB,
                               "free": 50 * gd.GIB, "used": 444 * gd.GIB, "at": 100.0})
        self.assertEqual(again, got)
        run.assert_called_once()

    def test_accounting_separates_reclaimable_from_unclassified_and_container(self) -> None:
        path = self.root / "rebuildable"
        path.write_bytes(b"x")
        fs = gd.Fs(path.stat().st_dev, str(self.root), 3 * gd.GIB, 10 * gd.GIB, 4 * gd.GIB, 5 * gd.GIB)
        item = gd.Item("xcode-derived-data", str(path), 2 * gd.GIB, 30.0, "reclaimable")
        saved = (gd.DARWIN, gd.ACCOUNTING)
        gd.DARWIN, gd.ACCOUNTING = False, self.root / "accounting-reclaimable.json"
        gd._ACCOUNTING_MEM.clear()
        gd._ACCOUNTING_COVERAGE_MEM.clear()
        self.addCleanup(lambda: (setattr(gd, "DARWIN", saved[0]), setattr(gd, "ACCOUNTING", saved[1]),
                                  gd._ACCOUNTING_MEM.clear(), gd._ACCOUNTING_COVERAGE_MEM.clear()))
        with mock.patch.object(gd, "apfs_container_capacity", return_value={
                "status": "observed", "total": 20 * gd.GIB, "free": 6 * gd.GIB,
                "used": 14 * gd.GIB, "at": 100.0}):
            got = gd.filesystem_accounting([fs], [item])[0]
        self.assertEqual(got["reclaimable"], 2 * gd.GIB)
        self.assertEqual(got["unclassified"], 5 * gd.GIB)
        self.assertEqual(got["container"]["free"], 6 * gd.GIB)

    def test_accounting_surfaces_blocked_and_unchecked_bytes(self) -> None:
        blocked = self.root / "blocked"
        unchecked = self.root / "unchecked"
        reclaimable = self.root / "reclaimable"
        for path in (blocked, unchecked, reclaimable):
            path.mkdir()
        fs = gd.Fs(self.root.stat().st_dev, str(self.root), 3 * gd.GIB, 10 * gd.GIB,
                   4 * gd.GIB, 5 * gd.GIB)
        items = [gd.Item("xcode-derived-data", str(blocked), 4 * gd.GIB, 30.0, "in-use"),
                 gd.Item("xcode-derived-data", str(unchecked), 3 * gd.GIB, 30.0, "unchecked"),
                 gd.Item("xcode-derived-data", str(reclaimable), 2 * gd.GIB, 30.0, "reclaimable")]
        saved = (gd.DARWIN, gd.ACCOUNTING)
        gd.DARWIN, gd.ACCOUNTING = False, self.root / "accounting-blocked.json"
        gd._ACCOUNTING_MEM.clear()
        gd._ACCOUNTING_COVERAGE_MEM.clear()
        self.addCleanup(lambda: (setattr(gd, "DARWIN", saved[0]), setattr(gd, "ACCOUNTING", saved[1]),
                                  gd._ACCOUNTING_MEM.clear(), gd._ACCOUNTING_COVERAGE_MEM.clear()))
        got = gd.filesystem_accounting([fs], items)[0]
        self.assertEqual(got["blocked"], 7 * gd.GIB)
        self.assertEqual(got["unchecked"], 3 * gd.GIB)
        self.assertEqual(got["blocked_by_verdict"], {"in-use": 4 * gd.GIB, "unchecked": 3 * gd.GIB})

    def test_eviction_summary_separates_logical_and_physical_bytes(self) -> None:
        receipt = self.root / "eviction.jsonl"
        before = [{"dev": 1, "mount": "/data", "free": 10, "total": 20,
                   "low": 12, "target": 15, "under": True, "observed": True}]
        after = [{"dev": 1, "mount": "/data", "free": 25, "total": 20,
                  "low": 12, "target": 15, "under": False, "observed": True}]
        accounting = [{"mount": "/data", "blocked": 7, "unchecked": 3, "accounting_gap": 4,
                       "reclaimable": 2}]
        summary = gd.write_eviction_summary(receipt, "pressure", 12,
                                            before, after, accounting, accounting)
        self.assertEqual(summary["logical_released_bytes"], 12)
        self.assertEqual(summary["physical_df_delta_bytes"], 15)
        self.assertEqual(summary["physical_df_reclaimed_bytes"], 15)
        self.assertEqual(summary["blocked_bytes"], 7)
        self.assertEqual(summary["unchecked_bytes"], 3)
        self.assertEqual(json.loads(receipt.read_text())["before"]["filesystems"][0]["free"], 10)

    def test_runner_profile_accounting_is_scoped_and_does_not_walk_roots_again(self) -> None:
        path = self.root / "cua-cache" / "frame.bin"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")
        fs = gd.Fs(path.stat().st_dev, str(self.root), 3 * gd.GIB, 10 * gd.GIB, 4 * gd.GIB, 5 * gd.GIB)
        item = gd.Item("cua-cache", str(path), 2 * gd.GIB, 30.0, "reclaimable")
        saved = (gd.DARWIN, gd.RUNNER_CACHE_PROFILE, gd.ACCOUNTING)
        gd.DARWIN, gd.RUNNER_CACHE_PROFILE = True, True
        gd.ACCOUNTING = self.root / "runner-accounting.json"
        gd._ACCOUNTING_MEM.clear()
        gd._ACCOUNTING_COVERAGE_MEM.clear()
        self.addCleanup(lambda: (setattr(gd, "DARWIN", saved[0]),
                                  setattr(gd, "RUNNER_CACHE_PROFILE", saved[1]),
                                  setattr(gd, "ACCOUNTING", saved[2]),
                                  gd._ACCOUNTING_MEM.clear(), gd._ACCOUNTING_COVERAGE_MEM.clear()))
        with mock.patch.object(gd, "fixed_measurement_roots", side_effect=AssertionError("profile root walk")), \
                mock.patch.object(gd, "accounting_coverage", side_effect=AssertionError("coverage walk")), \
                mock.patch.object(gd, "apfs_container_capacity", return_value=None):
            got = gd.filesystem_accounting([fs], [item])[0]
        self.assertEqual(got["scope"], "runner-cache-profile")
        self.assertEqual(got["top_level"], [{"path": str(path), "bytes": 2 * gd.GIB}])

    def test_accounting_cache_keeps_walk_timestamp_until_walk_is_due(self) -> None:
        path = self.root / "known"
        path.write_bytes(b"x")
        fs = gd.Fs(path.stat().st_dev, str(self.root), 2 * gd.GIB, 10 * gd.GIB, 3 * gd.GIB, 4 * gd.GIB)
        saved = (gd.DARWIN, gd.ACCOUNTING, gd.DARWIN_ACCOUNTING_ROOTS)
        gd.DARWIN, gd.ACCOUNTING = True, self.root / "accounting-cache.json"
        gd.DARWIN_ACCOUNTING_ROOTS = ()
        gd._ACCOUNTING_MEM.clear()
        gd._ACCOUNTING_COVERAGE_MEM.clear()
        self.addCleanup(lambda: (setattr(gd, "DARWIN", saved[0]), setattr(gd, "ACCOUNTING", saved[1]),
                                  setattr(gd, "DARWIN_ACCOUNTING_ROOTS", saved[2]),
                                  gd._ACCOUNTING_MEM.clear(), gd._ACCOUNTING_COVERAGE_MEM.clear()))
        with mock.patch.object(gd, "fixed_measurement_roots", return_value=[self.root]), \
                mock.patch.object(gd, "du_bytes", return_value=8 * gd.GIB), \
                mock.patch.object(gd, "accounting_coverage", return_value=[]), \
                mock.patch.object(gd, "apfs_container_capacity", return_value=None):
            gd.filesystem_accounting([fs], [])
        first = json.loads(gd.ACCOUNTING.read_text())[str(fs.dev)]["at"]
        gd._ACCOUNTING_MEM.clear()
        with mock.patch.object(gd, "fixed_measurement_roots", side_effect=AssertionError("cache should be used")):
            gd.filesystem_accounting([fs], [])
        second = json.loads(gd.ACCOUNTING.read_text())[str(fs.dev)]["at"]
        self.assertEqual(second, first)

    def test_accounting_coverage_is_cached_and_private_names_are_not_emitted(self) -> None:
        path = self.root / "known"
        path.write_bytes(b"x")
        fs = gd.Fs(path.stat().st_dev, str(self.root), 2 * gd.GIB, 10 * gd.GIB, 3 * gd.GIB, 4 * gd.GIB)
        saved = (gd.DARWIN, gd.ACCOUNTING, gd.DARWIN_ACCOUNTING_ROOTS)
        data = self.root / "Data"
        users = data / "Users"
        shared = users / "Shared"
        shared.mkdir(parents=True)
        gd.DARWIN, gd.ACCOUNTING = True, self.root / "accounting-coverage.json"
        gd.DARWIN_ACCOUNTING_ROOTS = (("data-volume", data), ("data-users", users),
                                      ("data-users-shared", shared))
        gd._ACCOUNTING_MEM.clear()
        gd._ACCOUNTING_COVERAGE_MEM.clear()
        self.addCleanup(lambda: (setattr(gd, "DARWIN", saved[0]), setattr(gd, "ACCOUNTING", saved[1]),
                                  setattr(gd, "DARWIN_ACCOUNTING_ROOTS", saved[2]),
                                  gd._ACCOUNTING_MEM.clear(), gd._ACCOUNTING_COVERAGE_MEM.clear()))
        coverage = [
            ({str(data / "Users"): 100, str(data / "Applications"): 20}, False),
            ({str(users / "leo"): 80, str(users / "Shared"): 30}, False),
            ({str(shared / "cmux-build-fleet"): 30}, True),
        ]
        with mock.patch.object(gd, "fixed_measurement_roots", return_value=[self.root]), \
                mock.patch.object(gd, "du_bytes", return_value=8 * gd.GIB), \
                mock.patch.object(gd, "shared_fleet_roots", return_value=[]), \
                mock.patch.object(gd, "du_children_with_status", side_effect=coverage) as covered:
            got = gd.filesystem_accounting([fs], [])
            gd._ACCOUNTING_MEM.clear()
            again = gd.filesystem_accounting([fs], [])
        self.assertEqual(covered.call_count, 3)
        self.assertTrue(all(call.kwargs == {"record_timeout": False}
                            for call in covered.call_args_list))
        self.assertEqual(got[0]["coverage"], again[0]["coverage"])
        encoded = json.dumps(got)
        self.assertNotIn("leo", encoded)
        users_row = next(x for x in got[0]["coverage"] if x["scope"] == "data-users")
        self.assertEqual(users_row["children"], [
            {"category": "private-user-data", "bytes": 80, "private": True,
             "classification": "private"},
            {"category": "shared-users-data", "bytes": 30, "private": False,
             "classification": "shared-runner"},
        ])

    def test_partial_du_coverage_is_a_lower_bound(self) -> None:
        with mock.patch.object(gd, "_du_output",
                               return_value=f"5 {self.root / 'one'}\ndu: protected\n"):
            self.assertEqual(gd.du_children_with_status(self.root),
                             ({str(self.root / "one"): 5 * 1024}, False))

    def test_runner_home_coverage_requires_runner_evidence_and_classifies_builds(self) -> None:
        saved = (gd.HOME, gd.DARWIN, gd.DARWIN_ACCOUNTING_ROOTS)
        gd.HOME, gd.DARWIN, gd.DARWIN_ACCOUNTING_ROOTS = self.root, True, ()
        runner = self.root / "actions-runner-glaeda"
        (runner / "glaeda-hooks").mkdir(parents=True)
        (runner / ".runner").write_text("{}")
        (self.root / "cmux-browser-fleet/build").mkdir(parents=True)
        (self.root / "Library/Caches").mkdir(parents=True)
        self.addCleanup(lambda: (setattr(gd, "HOME", saved[0]), setattr(gd, "DARWIN", saved[1]),
                                  setattr(gd, "DARWIN_ACCOUNTING_ROOTS", saved[2])))
        results = [({str(self.root / "cmux-browser-fleet"): 42}, True),
                   ({str(self.root / "Library/Caches/cmux-next-ci"): 8}, True),
                   ({str(self.root / "cmux-browser-fleet/build/obj"): 40}, True)]
        with mock.patch.object(gd, "shared_fleet_roots", return_value=[]), \
                mock.patch.object(gd, "du_children_with_status", side_effect=results):
            rows = gd.accounting_coverage(self.root.stat().st_dev, 100.0)
        self.assertEqual([r["scope"] for r in rows], ["runner-home", "runner-library-caches",
                                                        "runner-browser-build"])
        self.assertEqual(rows[0]["children"][0]["classification"], "build")
        self.assertEqual(rows[1]["children"][0]["category"], "cache/cmux-next-ci")
        self.assertNotIn("path", json.dumps(rows))

    def test_shared_fleet_coverage_separates_cache_build_ci_and_products(self) -> None:
        saved = (gd.DARWIN, gd.DARWIN_ACCOUNTING_ROOTS)
        root = self.root / "shared-fleet"
        for name in ("cache", "xcode", "ci", "node-products"):
            (root / name).mkdir(parents=True)
        gd.DARWIN, gd.DARWIN_ACCOUNTING_ROOTS = True, ()
        self.addCleanup(lambda: (setattr(gd, "DARWIN", saved[0]),
                                  setattr(gd, "DARWIN_ACCOUNTING_ROOTS", saved[1])))
        roots = [("shared-fleet", root)] + [(f"shared-fleet/{name}", root / name)
                                             for name in ("cache", "xcode", "ci", "node-products")]
        covered = [
            ({str(root / name): size for name, size in (("cache", 54), ("xcode", 53),
                                                        ("ci", 37), ("node-products", 23))}, True),
        ]
        with mock.patch.object(gd, "shared_fleet_roots", return_value=roots), \
                mock.patch.object(gd, "du_children_with_status", side_effect=covered) as measured:
            rows = gd.accounting_coverage(root.stat().st_dev, 100.0)
        self.assertEqual(measured.call_count, 1)
        self.assertEqual([r["scope"] for r in rows], [x[0] for x in roots])
        root_row = rows[0]
        self.assertEqual({x["category"]: x["classification"] for x in root_row["children"]},
                         {"cache": "shared-cache", "xcode": "macos-build", "ci": "macos-ci",
                          "node-products": "shared-node-products"})
        self.assertEqual(rows[1]["children"][0]["classification"], "shared-cache")
        self.assertNotIn("path", json.dumps(rows))

    def test_retired_owner_family_is_reclaimable_when_unloaded(self) -> None:
        make(self.root / "old")
        fam = gd.Family("retired", self.root, True, "rebuild", retired_only=True,
                        owner_job="system/example.retired")
        with mock.patch.object(gd, "owner_retired", return_value=True):
            self.assertEqual(self.verdicts_for([fam])["old"], "reclaimable")

    def verdicts_for(self, families: list[gd.Family]) -> dict[str, str]:
        return {Path(i.path).name: i.verdict for i in gd.survey(families, 24, 0)}

    def receipt(self) -> Path:
        r = self.root.parent / f"{self.root.name}-receipt.jsonl"
        self.addCleanup(r.unlink, missing_ok=True)
        return r

    def test_help_renders(self) -> None:  # argparse %-formats help; a bare % breaks it
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as e:
            gd.main(["--help"])
        self.assertEqual(e.exception.code, 0)
        self.assertIn("--low", out.getvalue())

    def test_thresholds_take_gib_or_percent(self) -> None:
        self.assertEqual(gd.space("60", 10**15), 60 * 1024**3)
        gib = 1024**3
        # one default across a 256 GB laptop, a 1 TB mini and an 8 TB studio
        self.assertEqual(gd.space("15%:40-150", 238 * gib), 40 * gib)
        self.assertEqual(gd.space("15%:40-150", 931 * gib), int(931 * gib * 0.15))
        self.assertEqual(gd.space("15%:40-150", 7450 * gib), 150 * gib)
        # a 30 GiB VM: the 40 GiB floor stops at twice the share instead of exceeding the disk
        self.assertEqual(gd.space("15%:40-150", 30 * gib), int(30 * gib * 0.15) * 2)
        self.assertLess(gd.space("25%:80-300", 30 * gib), 30 * gib)
        for bad in ("60:1-2", "15%:9", "15%:50-40", "15%:a-b"):
            with self.assertRaises(ValueError):
                gd.space(bad, 1000)
        self.assertEqual(gd.space("15%", 1000), 150)
        self.assertEqual(gd.space("2.5", 0), int(2.5 * 1024**3))
        with self.assertRaises(ValueError):
            gd.space("lots", 1000)

    def test_tmp_family_does_not_treat_marker_search_budget_as_job_use(self) -> None:
        fam = gd.Family("tmp", self.root, True, "scratch")
        with mock.patch.object(gd, "cmux_active", return_value=True):
            self.assertFalse(gd.job_busy(fam, self.root))

    def test_cmux_active_marker_holds_a_slot(self) -> None:
        slot = self.root / "work/slot-1"
        (slot / ".cmux-active").mkdir(parents=True)
        self.assertFalse(gd.cmux_active(self.root / "work"))  # marker dir, no pid yet
        (slot / ".cmux-active/pid").write_text(f"{os.getpid()}\n")
        self.assertTrue(gd.cmux_active(self.root / "work"))  # live pid one level down
        dead = subprocess.Popen(["true"])
        dead.wait()
        (slot / ".cmux-active/pid").write_text(str(dead.pid))
        self.assertFalse(gd.cmux_active(self.root / "work"))
        (slot / ".cmux-active/pid").write_text("garbage")
        self.assertTrue(gd.cmux_active(self.root / "work"))  # unreadable: fail closed
        (slot / ".cmux-active/pid").unlink()
        (slot / ".lock").write_text("")
        self.assertTrue(gd.cmux_active(self.root / "work"))  # hq's slot lock also means busy
        (slot / ".lock").unlink()
        (self.root / "work/slot-1/SourcePackages").mkdir()
        (self.root / "work/slot-1/SourcePackages/.lock").write_text("")
        self.assertFalse(gd.cmux_active(self.root / "work"))  # a SwiftPM lock is not a slot lock
        # the marker lives in work/<slot>; the DerivedData beside it is what glaeda-disk deletes
        (slot / ".cmux-active/pid").write_text(str(os.getpid()))
        fam = gd.Family("cmux-job-cache", self.root, True, "x", depth=2)
        (self.root / "DerivedData/slot-1-simulator").mkdir(parents=True)
        self.assertTrue(gd.job_busy(fam, self.root / "DerivedData"))
        self.assertFalse(gd.cmux_active(self.root / "DerivedData"))

    def test_pressure_deletes_cheapest_loss_first(self) -> None:
        dd = gd.Family("xcode-derived-data", self.root, True, "x", rebuild_minutes=15)
        scratch = gd.Family("claude-scratchpad", self.root, True, "x", rebuild_minutes=0.5)
        big = gd.Item("xcode-derived-data", "/a", 20 * gd.GIB, 30.0, "reclaimable")
        small = gd.Item("claude-scratchpad", "/b", 2 * gd.GIB, 7.0, "reclaimable")
        self.assertGreater(gd.value(small, scratch), gd.value(big, dd))
        stale = gd.replace(big, idle_hours=24 * 7)
        self.assertGreater(gd.value(stale, dd), gd.value(big, dd))  # long idle ranks higher

    @unittest.skipUnless(sys.platform == "darwin", "launchd")
    def test_owner_retired_needs_a_named_unloaded_service(self) -> None:
        self.assertFalse(gd.owner_retired(gd.Family("x", self.root, False, "", owner="someone")))
        gone = gd.Family("x", self.root, False, "", owner="someone", owner_job="system/com.example.glaeda-none")
        self.assertTrue(gd.owner_retired(gone))

    def test_a_shared_cache_is_retired_only_when_no_worker_using_it_is_loaded(self) -> None:
        """cmux12s, 2026-10-11: the lent worker builds in the shared cache, which was labelled RETIRED
        because only the dedicated dev-build worker's service was asked."""
        fam = gd.Family("hq-build-fleet-cache", self.root, True, "x", retired_only=True,
                        owner_job=gd.HQ_BUILD_WORKERS)
        self.assertIn("system/ai.manaflow.cmux-lent-worker", gd.HQ_BUILD_WORKERS)
        self.assertIn("system/ai.manaflow.cmux-build-worker", gd.HQ_BUILD_WORKERS)
        loaded: set[str] = set()
        asked: list[str] = []

        def launchctl(argv, **_):
            asked.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0 if argv[-1] in loaded else 113)

        make(self.root / "cmux-workspaces")
        with mock.patch.object(gd, "DARWIN", True), mock.patch.object(gd.subprocess, "run", launchctl):
            self.assertTrue(gd.owner_retired(fam))
            self.assertEqual(asked, list(gd.HQ_BUILD_WORKERS))
            loaded.add("system/ai.manaflow.cmux-lent-worker")  # a lent mini
            self.assertFalse(gd.owner_retired(fam))
            with mock.patch.object(gd, "process_evidence", return_value=([], "")):
                self.assertEqual(self.verdicts_for([fam])["cmux-workspaces"], "report-only")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                gd.report([], [], 0, 24, fams={fam.id: fam})
                gd.report([gd.Item(fam.id, str(self.root / "cmux-workspaces"), 1, 48, "report-only")], [], 0,
                          24, fams={fam.id: fam})
            self.assertNotIn("RETIRED", out.getvalue())
            loaded.clear()
            loaded.add("system/ai.manaflow.cmux-build-worker")  # the dedicated dev-build worker
            self.assertFalse(gd.owner_retired(fam))
        # a single service name still works
        one = gd.Family("x", self.root, False, "", owner_job="system/a")
        with mock.patch.object(gd, "DARWIN", True), \
                mock.patch.object(gd.subprocess, "run", lambda argv, **_: subprocess.CompletedProcess(argv, 113)):
            self.assertTrue(gd.owner_retired(one))

    def test_a_retired_workers_cache_entries_idle_a_week_are_mirrors(self) -> None:
        """cmux14, 2026-10-25: at the 30 GiB floor with 0.0G reclaimable, while its unloaded hq workers'
        cmux-workspaces (idle 190 h), cmux-tui-target (289 h) and swiftpm (310 h) read git-checkout or unchecked."""
        real = gd.hq_cache_family()
        self.assertEqual((real.root, real.reference_root), (gd.FLEET_ROOT / "cache", gd.FLEET_ROOT))
        self.assertEqual(real.mirror_idle_hours, gd.CMUX_JOB_MIRROR_HOURS)
        self.assertTrue(real.retired_only)
        self.assertEqual(real.owner_job, gd.HQ_BUILD_WORKERS)
        cache = self.root / "cache"
        fam = gd.hq_cache_family(cache)
        self.assertEqual(fam.reference_root, self.root)
        week = gd.CMUX_JOB_MIRROR_HOURS
        for name, hours in (("cmux-workspaces", week + 22), ("swiftpm", week + 142), ("fresh-workspaces", 72),
                            ("leased", week + 50), ("cmux-tui-target", week + 121)):
            make(cache / name, age_hours=hours)
        for name in ("cmux-workspaces", "fresh-workspaces", "leased"):
            (cache / name / "key/src/.git").mkdir(parents=True)
            t = time.time() - (72 if name == "fresh-workspaces" else week + 22) * 3600
            for d in (cache / name / "key/src/.git", cache / name / "key/src", cache / name / "key", cache / name):
                os.utime(d, (t, t))
        (cache / "swiftpm/repositories/dep/.git").mkdir(parents=True)
        t = time.time() - (week + 142) * 3600
        for d in (cache / "swiftpm/repositories/dep/.git", cache / "swiftpm/repositories/dep",
                  cache / "swiftpm/repositories", cache / "swiftpm"):
            os.utime(d, (t, t))
        (self.root / "jobs").mkdir()
        (self.root / "jobs/j.json").write_text(json.dumps({"state": "running", "path": os.fspath(cache / "leased")}))
        gd._LIVE_REFERENCES.clear()
        self.addCleanup(gd._LIVE_REFERENCES.clear)
        real_git = gd.family_git_state

        def git_state(f, path):  # a cargo target dir too large to search
            return "unchecked" if path.name == "cmux-tui-target" else real_git(f, path)

        with mock.patch.object(gd, "family_git_state", git_state):
            with mock.patch.object(gd, "owner_retired", return_value=True):
                items = {Path(i.path).name: i for i in gd.survey([fam], 24, 0)}
            with mock.patch.object(gd, "owner_retired", return_value=False):  # a worker builds here
                live = {Path(i.path).name: i.verdict for i in gd.survey([fam], 24, 0)}
        self.assertEqual({k: i.verdict for k, i in items.items()}, {
            "cmux-workspaces": "reclaimable", "swiftpm": "reclaimable", "cmux-tui-target": "reclaimable",
            "fresh-workspaces": "git-checkout",  # idle three days: its clone still has to lose nothing
            "leased": "in-use"})  # a running fleet job names it
        self.assertIn("idle 168h+: its checkouts are mirrors", items["cmux-workspaces"].reasons)
        self.assertIn("live lease or running job", items["leased"].reasons)
        self.assertEqual({k for k, v in live.items() if v != "report-only"}, {"leased"})
        reclaim = [i for i in items.values() if i.verdict == "reclaimable"]
        # between the survey and the delete a fleet job leases swiftpm and a process starts in cmux-tui-target:
        # apply re-reads both (the survey's cached references and process snapshot are stale by then)
        (self.root / "jobs/late.json").write_text(json.dumps({"until": time.time() + 600,
                                                              "path": os.fspath(cache / "swiftpm")}))
        late = ([], f"cargo build --target-dir {cache / 'cmux-tui-target'}\n")
        receipt = self.receipt()
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(gd, "owner_retired", return_value=True), \
                mock.patch.object(gd, "family_git_state", git_state), \
                mock.patch.object(gd, "process_evidence", return_value=late), \
                mock.patch.object(gd, "command_lines", return_value=late[1]):
            gd.apply(reclaim, {fam.id: fam}, receipt, None, 24)
        self.assertEqual(sorted(p.name for p in cache.iterdir()),
                         ["cmux-tui-target", "fresh-workspaces", "leased", "swiftpm"])
        outcomes = {Path(r["path"]).name: r["outcome"] for r in map(json.loads, receipt.read_text().splitlines())}
        self.assertEqual(outcomes, {"cmux-workspaces": "reclaimed", "swiftpm": "changed:in-use",
                                    "cmux-tui-target": "changed:in-use"})

    def test_pressure_waits_for_a_ci_job_until_the_emergency_floor(self) -> None:
        job = "/Users/cmux/actions-runner-glaeda-2/bin/Runner.Worker spawnclient 148 151"
        self.assertTrue(gd.CI_WORKER.search(job))
        self.assertTrue(gd.CI_WORKER.search("/Users/cmux/actions-runner/bin/Runner.Worker"))
        self.assertTrue(gd.CI_WORKER.search("/Users/cmux/actions-runner-cmux-nightly-mini/bin.2.337.0/Runner.Worker spawnclient 1 2"))
        self.assertFalse(gd.CI_WORKER.search("/Users/cmux/actions-runner-glaeda/bin/Runner.Listener run"))
        total = 460 * gd.GIB  # a build mini: emergency 10%:30-60 is 46 GiB
        low = gd.Fs(1, "/", 60 * gd.GIB, total, 69 * gd.GIB, 115 * gd.GIB)
        self.assertIn("deferred", gd.ci_defer({1: low}, [job], "10%:30-60"))
        self.assertEqual(gd.ci_defer({1: low}, [], "10%:30-60"), "")  # no job: go ahead
        critical = gd.replace(low, free=40 * gd.GIB)
        self.assertEqual(gd.ci_defer({1: critical}, [job], "10%:30-60"), "")  # the job would fail anyway
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            gd.main(["--pressure", "--emergency", "lots"])
        # a 50 GiB runner floor lifts the floor to 70 GiB on HOME's volume, so a busy mini reclaims before
        # its gate stops the runners, not only after
        home = gd.replace(low, dev=gd.HOME.stat().st_dev)
        self.assertIn("deferred", gd.ci_defer({1: home}, [job], "10%:30-60", floor_gib=40))
        self.assertEqual(gd.ci_defer({1: home}, [job], "10%:30-60", floor_gib=50), "")
        self.assertIn("deferred", gd.ci_defer({1: gd.replace(home, free=75 * gd.GIB)}, [job], "10%:30-60", 50))

    def test_chromium_trees_are_never_candidates(self) -> None:
        saved = gd.HOME
        gd.HOME = self.root
        try:
            caches = self.root / "Library/Caches"
            for name in ("siso", "cmux-release-support", "go-build", "chromium-cache"):
                make(caches / name)
            make(self.root / "cmux-browser-fleet/build/chromium/src")
            make(self.root / "other")
            fams = [gd.Family("library-caches", caches, True, "rebuild"),
                    gd.Family("home", self.root, True, "rebuild"),
                    gd.Family("deep", self.root / "cmux-browser-fleet", True, "rebuild", depth=2)]
            got = {Path(i.path).name: i.verdict for i in gd.survey(fams, 1, 0)}
            self.assertEqual(got, {"go-build": "reclaimable", "other": "reclaimable"})
            for rel in ("cmux-browser-fleet", "cmux-browser-fleet/build", "Library/Caches/siso",
                        "Library/Caches/cmux-release-support/ghostty", "Library"):  # Library holds some
                self.assertTrue(gd.never_delete(self.root / rel), rel)
            self.assertTrue(gd.never_delete(Path("/private/tmp/chromium-out")))
            self.assertFalse(gd.never_delete(self.root / "Library/Caches/go-build"))
        finally:
            gd.HOME = saved

    def test_fleet_artifacts_use_cutoff_and_live_lease_references(self) -> None:
        fleet = self.root / "fleet"
        cache = fleet / "cache"
        cache.mkdir(parents=True)
        old = make(cache / "old-artifact", age_hours=24)
        fresh = make(cache / "fresh-artifact", age_hours=1)
        gd.ARTIFACT_CUTOFF = time.time() - 12 * 3600
        fam = gd.Family("fleet-artifacts", cache, True, "rebuild", stale_before=gd.ARTIFACT_CUTOFF,
                        allow_stale_protected=True, reference_root=fleet)
        try:
            with mock.patch.object(gd, "live_artifact_references", return_value=frozenset()):
                got = {Path(i.path).name: i.verdict for i in gd.survey([fam], 24, 0)}
            self.assertEqual(got, {"old-artifact": "reclaimable", "fresh-artifact": "recent"})
            with mock.patch.object(gd, "live_artifact_references", return_value=frozenset({str(fresh)})):
                got = {Path(i.path).name: (i.verdict, i.reasons) for i in gd.survey([fam], 0, 0)}
            self.assertEqual(got["fresh-artifact"][0], "in-use")
            self.assertIn("live lease or running job", got["fresh-artifact"][1])
        finally:
            gd.ARTIFACT_CUTOFF = None

    def test_default_families_cover_fleet_and_browser_artifacts(self) -> None:
        saved = (gd.HOME, gd.FLEET_ROOT, gd.DARWIN)
        gd.HOME = self.root
        gd.FLEET_ROOT = self.root / "shared-fleet"
        gd.DARWIN = True
        try:
            (gd.HOME / "cmux-browser-fleet").mkdir()
            (gd.FLEET_ROOT / "cache").mkdir(parents=True)
            (gd.FLEET_ROOT / "ci-ios" / "runner-a" / "derived-data").mkdir(parents=True)
            (gd.HOME / "Library/Caches/cmux-next-ci").mkdir(parents=True)
            fams = {f.id: f for f in gd.default_families()}
            self.assertIn("fleet-artifacts", fams)
            self.assertIn("ci-ios-derived-data", fams)
            self.assertIn("browser-artifacts", fams)
            self.assertIn(gd.CMUX_NEXT_CI_FAMILY, fams)
            self.assertEqual(fams[gd.CMUX_NEXT_CI_FAMILY].min_idle_hours,
                             gd.CMUX_NEXT_CI_MIN_IDLE_HOURS)
            self.assertIn("cmux-next-ci", fams["library-caches"].skip)
            self.assertTrue(fams["fleet-artifacts"].allow_stale_protected)
            self.assertTrue(fams["browser-artifacts"].allow_stale_protected)
        finally:
            gd.HOME, gd.FLEET_ROOT, gd.DARWIN = saved

    def test_default_families_delineate_cua_recordings_and_cache(self) -> None:
        saved = (gd.HOME, gd.FLEET_ROOT, gd.DARWIN)
        gd.HOME = self.root
        gd.FLEET_ROOT = self.root / "shared-fleet"
        gd.DARWIN = True
        try:
            (gd.HOME / "Library/Application Support/cua-ssh/recordings").mkdir(parents=True)
            (gd.HOME / "Library/Caches/cmux/cmux-cua").mkdir(parents=True)
            fams = {f.id: f for f in gd.default_families()}
            self.assertEqual(fams["cua-recordings"].root,
                             gd.HOME / "Library/Application Support/cua-ssh/recordings")
            self.assertEqual(fams["cua-cache"].root, gd.HOME / "Library/Caches/cmux/cmux-cua")
            self.assertTrue(fams["cua-recordings"].reclaimable)
            self.assertEqual(fams["cua-recordings"].min_idle_hours, 24.0)
            self.assertEqual(fams["cua-cache"].owner, "cmux CUA")
        finally:
            gd.HOME, gd.FLEET_ROOT, gd.DARWIN = saved

    def test_ci_ios_derived_data_is_reclaimable_without_deleting_runner_root(self) -> None:
        root = self.root / "ci-ios"
        old = root / "runner-a" / "derived-data"
        make(old, age_hours=48)
        fam = gd.ci_ios_family(root)
        with mock.patch.object(gd, "live_artifact_references", return_value=frozenset()):
            got = gd.survey([fam], 24, 0)
        self.assertEqual([Path(i.path) for i in got], [old])
        self.assertEqual(got[0].verdict, "reclaimable")
        self.assertTrue((root / "runner-a").is_dir())

    def test_cmux_next_ci_only_lists_valid_generated_children(self) -> None:
        root = self.root / "Library/Caches/cmux-next-ci"
        runner = root / "cmux-scheme/runner-a"
        daily = root / "dev-daily-release/abcdef0123456789/Release/26f113aa"
        xcode_daily = root / "dev-daily-release/1234567890abcdef/Release/xcode-2c2299e9015ce0c8"
        release_compile = root / "release-compile/bcc51113400dc347/Release/xcode-2c2299e9015ce0c8"
        for state in (runner, daily, xcode_daily, release_compile):
            state.mkdir(parents=True)
            for name in ("Build", "Index.noindex", "Logs", "Products"):
                make(state / name, age_hours=96)
            for name in gd.CMUX_NEXT_CI_PRESERVED_NAMES:
                (state / name).mkdir()
        invalid = root / "dev-daily-release/not-a-fingerprint/Release/26f113aa"
        (invalid / "Build").mkdir(parents=True)
        linked_release_target = self.root / "linked-release"
        linked_release_target.mkdir(parents=True)
        linked_release = root / "dev-daily-release/2222222222222222/Release"
        linked_release.parent.mkdir(parents=True, exist_ok=True)
        linked_release.symlink_to(linked_release_target, target_is_directory=True)
        (linked_release_target / "xcode-2c2299e9015ce0c8/Build").mkdir(parents=True)
        linked_daily_target = self.root / "linked-daily"
        (linked_daily_target / "abcdef0123456789/Release/xcode-2c2299e9015ce0c8/Build").mkdir(parents=True)
        linked_root = self.root / "symlink-cache"
        linked_root.mkdir()
        (linked_root / "dev-daily-release").symlink_to(linked_daily_target, target_is_directory=True)
        self.assertEqual(gd.cmux_next_ci_state_roots(linked_root), [])
        fam = gd.cmux_next_ci_family(root)
        with mock.patch.object(gd, "process_evidence", return_value=([], "")):
            items = gd.survey([fam], 24, 0)
        paths = {Path(i.path) for i in items}
        expected = {
            state / name for state in (runner, daily, xcode_daily, release_compile)
            for name in ("Build", "Index.noindex", "Logs", "Products")
        }
        self.assertEqual(paths, expected)
        self.assertTrue(all(i.verdict == "reclaimable" for i in items))

    def test_cmux_next_ci_keeps_unknown_state_children(self) -> None:
        root = self.root / "Library/Caches/cmux-next-ci"
        state = root / "cmux-scheme/runner-a"
        build = make(state / "Build", age_hours=96)
        (state / "unknown-owner-state").mkdir(parents=True)
        fam = gd.cmux_next_ci_family(root)
        with mock.patch.object(gd, "process_evidence", return_value=([], "")):
            got = gd.survey([fam], 24, 0)
        self.assertEqual(got[0].path, str(build))
        self.assertEqual(got[0].verdict, "in-use")
        self.assertIn("unknown children", got[0].reasons[0])

    def test_cmux_next_ci_preserves_the_binary_module_guard_digest(self) -> None:
        # cmux's swiftpm-binary-module-guard.sh writes it beside SourcePackages; it is not an unknown owner
        root = self.root / "Library/Caches/cmux-next-ci"
        state = root / "cmux-scheme/runner-a"
        build = make(state / "Build", age_hours=96)
        (state / ".cmux-binary-module-inputs").write_text("0" * 64 + "\n")
        fam = gd.cmux_next_ci_family(root)
        with mock.patch.object(gd, "process_evidence", return_value=([], "")):
            got = gd.survey([fam], 24, 0)
        self.assertEqual(got[0].path, str(build))
        self.assertNotEqual(got[0].verdict, "in-use")

    def test_cmux_next_ci_preserves_compilation_cache_child(self) -> None:
        root = self.root / "Library/Caches/cmux-next-ci"
        state = root / "cmux-scheme/runner-a"
        build = make(state / "Build", age_hours=96)
        (state / "CompilationCache.noindex").mkdir(parents=True)
        fam = gd.cmux_next_ci_family(root)
        with mock.patch.object(gd, "process_evidence", return_value=([], "")):
            got = gd.survey([fam], 24, 0)
        self.assertEqual(got[0].path, str(build))
        self.assertNotEqual(got[0].verdict, "in-use")

    def test_cmux_next_ci_keeps_recent_and_marked_state(self) -> None:
        root = self.root / "Library/Caches/cmux-next-ci"
        state = root / "cmux-scheme/runner-a"
        build = make(state / "Build", age_hours=96)
        recent = make(state / "Logs", age_hours=2)
        (state / ".lock").write_text("")
        fam = gd.cmux_next_ci_family(root)
        with mock.patch.object(gd, "process_evidence", return_value=([], "")):
            got = {Path(i.path): i for i in gd.survey([fam], 24, 0)}
        self.assertEqual(got[build].verdict, "in-use")
        self.assertIn("active or ambiguous marker", got[build].reasons[0])
        self.assertEqual(got[recent].verdict, "in-use")
        self.assertIn("active or ambiguous marker", got[recent].reasons[0])

    def test_cmux_next_ci_idle_floor_survives_pressure_mode(self) -> None:
        root = self.root / "Library/Caches/cmux-next-ci"
        build = make(root / "cmux-scheme/runner-a/Build", age_hours=48)
        fam = gd.cmux_next_ci_family(root)
        with mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), \
                mock.patch.object(gd, "process_evidence", return_value=([], "")):
            got = gd.survey([fam], 0, 0)
        self.assertEqual(got[0].path, str(build))
        self.assertEqual(got[0].verdict, "recent")

    def test_cmux_next_ci_apply_rechecks_process_and_marker(self) -> None:
        root = self.root / "Library/Caches/cmux-next-ci"
        state = root / "cmux-scheme/runner-a"
        build = make(state / "Build", age_hours=96)
        fam = gd.cmux_next_ci_family(root)
        with mock.patch.object(gd, "process_evidence", return_value=([], "")):
            items = gd.survey([fam], 24, 0)
        self.assertEqual(items[0].verdict, "reclaimable")
        (state / ".cmux-active").write_text("")
        receipt = self.root / "receipt.jsonl"
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(gd, "process_evidence", return_value=([], "")):
            gd.apply(items, {fam.id: fam}, receipt, None, 24)
        self.assertTrue(build.exists())
        self.assertIn("changed:in-use", receipt.read_text())

    def test_stale_marker_uses_host_activity_before_wall_clock_cap(self) -> None:
        root = self.root / "Library/Caches/cmux-next-ci"
        state = root / "cmux-scheme/runner-a"
        state.mkdir(parents=True)
        marker = state / ".build.active"
        marker.write_text("pid=gone\n")
        now = time.time()
        old = now - 1.0 * 3600
        os.utime(marker, (old, old))
        receipt = self.root / "receipt.jsonl"
        policy = dict(gd.POLICY)
        policy.update(stale_marker_min_age_hours=0.5, stale_marker_jobs=3, stale_marker_max_hours=48.0)
        starts = [old + 600, old + 1200, old + 1800]
        with mock.patch.object(gd, "POLICY", policy), \
                mock.patch.object(gd, "job_starts", return_value=starts), \
                mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                mock.patch.object(gd, "host_busy", return_value=""), \
                mock.patch.object(gd, "live_artifact_references", return_value=frozenset()), \
                mock.patch.object(gd, "holds_socket", return_value=False):
            self.assertEqual(gd.clear_stale_build_markers(receipt, [root], now=now), len(b"pid=gone\n"))
        self.assertFalse(marker.exists())
        record = json.loads(receipt.read_text())
        self.assertEqual(record["reasons"][0], "host-activity")
        self.assertEqual(record["reasons"][1]["jobs_since_marker"], 3)

    def test_stale_marker_waits_for_activity_then_uses_long_safety_cap(self) -> None:
        root = self.root / "Library/Caches/cmux-next-ci"
        state = root / "cmux-scheme/runner-a"
        state.mkdir(parents=True)
        marker = state / ".build.active"
        marker.write_text("pid=gone\n")
        now = time.time()
        old = now - 2.0 * 3600
        os.utime(marker, (old, old))
        receipt = self.root / "receipt.jsonl"
        policy = dict(gd.POLICY)
        policy.update(stale_marker_min_age_hours=0.5, stale_marker_jobs=3, stale_marker_max_hours=48.0)
        with mock.patch.object(gd, "POLICY", policy), \
                mock.patch.object(gd, "job_starts", return_value=[old + 600]), \
                mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                mock.patch.object(gd, "host_busy", return_value=""), \
                mock.patch.object(gd, "live_artifact_references", return_value=frozenset()), \
                mock.patch.object(gd, "holds_socket", return_value=False):
            self.assertEqual(gd.clear_stale_build_markers(receipt, [root], now=now), 0)
        self.assertTrue(marker.exists())
        cap_now = old + 49.0 * 3600
        with mock.patch.object(gd, "POLICY", policy), \
                mock.patch.object(gd, "job_starts", return_value=None), \
                mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                mock.patch.object(gd, "host_busy", return_value=""), \
                mock.patch.object(gd, "live_artifact_references", return_value=frozenset()), \
                mock.patch.object(gd, "holds_socket", return_value=False):
            self.assertEqual(gd.clear_stale_build_markers(receipt, [root], now=cap_now), len(b"pid=gone\n"))
        self.assertFalse(marker.exists())

    def test_cmux_next_ci_products_dedupe_guard_fails_closed_on_marker(self) -> None:
        root = self.root / "Library/Caches/cmux-next-ci"
        state = root / "cmux-scheme/runner-a"
        products = make(state / "Products", age_hours=96)
        nested_products = make(state / "Build/Products", age_hours=96)
        guard = gd.cmux_next_ci_dedupe_guard(root)
        self.assertTrue(guard(products / "blob"))
        nested_guard = gd.cmux_next_ci_dedupe_guard(root)
        self.assertTrue(nested_guard(nested_products / "blob"))
        (state / ".lock").write_text("")
        self.assertFalse(guard(products / "blob"))
        (state / ".lock").unlink()
        with mock.patch.object(gd, "live_artifact_references", return_value=frozenset({str(state)})):
            self.assertFalse(guard(products / "blob"))
        no_evidence_guard = gd.cmux_next_ci_dedupe_guard(root)
        with mock.patch.object(gd, "process_evidence", side_effect=gd.NoEvidence("unavailable")):
            self.assertFalse(no_evidence_guard(products / "blob"))

    def test_cmux_next_ci_products_dedupe_roots_are_sealed_dirs_only(self) -> None:
        root = self.root / "Library/Caches/cmux-next-ci"
        state = root / "cmux-scheme/runner-a"
        products = make(state / "Products", age_hours=96)
        build = make(state / "Build", age_hours=96)
        nested_products = make(state / "Build/Products", age_hours=96)
        (state / "Logs").mkdir(parents=True)
        (state / "SourcePackages").mkdir()
        (state / "unknown").mkdir()
        self.assertEqual(sorted(gd.cmux_next_ci_dedupe_roots(root)), sorted([products, nested_products]))

    def test_fleet_ci_hot_tier(self) -> None:
        ci = self.root / "ci"
        (ci / "seeds").mkdir(parents=True)
        (ci / "seed-source.json").write_text(json.dumps({"prefix": "p-"}))
        for name, age in (("p-a", 1), ("p-b", 2), ("p-c", 48), ("q-x", 48)):
            make(ci / "seeds" / name, age_hours=age)
        make(ci / "pr-builds/pr-1", age_hours=0.5)
        make(ci / "pr-builds/pr-2", age_hours=10)
        make(ci / "pr-builds/.pr-3.discard-9", age_hours=2)  # cmux's clear() owns it
        make(ci / "pr-builds/other", age_hours=10)
        make(ci / "derived-data", age_hours=10)
        make(ci / ".derived-data.discard-7", age_hours=60)
        make(ci / "source-packages", age_hours=60)
        make(ci / "cmux-ci-2/derived-data", age_hours=60)
        # a root with no seed source keeps its newest two of any prefix; dot entries are never seeds
        for name, age in (("p-y", 48), ("p-z", 49), ("q-z", 50), (".q-w.incoming-3", 50)):
            make(ci / "cmux-ci-2/seeds" / name, age_hours=age)
        make(ci / "cmux-ci-x/derived-data", age_hours=60)  # not a root store
        # SwiftPM checkouts inside DerivedData do not make it a checkout
        (ci / "pr-builds/pr-2/SourcePackages/checkouts/dep/.git").mkdir(parents=True)
        for rel in ("SourcePackages/checkouts/dep/.git", "SourcePackages/checkouts/dep", "SourcePackages/checkouts",
                    "SourcePackages", ""):
            os.utime(ci / "pr-builds/pr-2" / rel, (time.time() - 36000,) * 2)
        self.assertEqual(gd.fleet_stores(ci), [ci, ci / "cmux-ci-2"])
        items = gd.survey(gd.fleet_families(ci), 6, 0)
        got = {str(Path(i.path).relative_to(ci)): i.verdict for i in items}
        self.assertEqual(got, {
            "seeds/p-a": "kept", "seeds/p-b": "kept", "seeds/p-c": "reclaimable", "seeds/q-x": "reclaimable",
            "pr-builds/pr-1": "recent", "pr-builds/pr-2": "recent",
            "derived-data": "recent", ".derived-data.discard-7": "reclaimable",
            "cmux-ci-2/derived-data": "reclaimable", "cmux-ci-2/seeds/p-y": "kept", "cmux-ci-2/seeds/p-z": "kept",
            "cmux-ci-2/seeds/q-z": "reclaimable", "cmux-ci-2/seeds/.q-w.incoming-3": "reclaimable"})
        # apply keeps the hot-tier windows, and re-reads command lines per item: a job that starts cloning a
        # parked build during the run keeps it
        # (apply looks families up by id, as main passes them: every root's family of an id behaves the same)
        fams = {f.id: f for f in gd.fleet_families(ci)}
        with mock.patch.object(gd, "FLEET_CI", ci), \
                mock.patch.object(gd, "command_lines", lambda: f"cp -cR {ci}/pr-builds/pr-2 /tmp/x\n"):
            reclaimable = [i for i in items if i.verdict == "reclaimable"]
            gd.apply(reclaimable, fams, self.root / "r.jsonl", None, 6)
        self.assertTrue((ci / "pr-builds/pr-2").exists())
        self.assertFalse((ci / "seeds/p-c").exists())
        self.assertTrue((ci / "seeds/p-a").exists())
        self.assertFalse((ci / "cmux-ci-2/derived-data").exists())

    def test_spm_scratch_goes_under_its_own_lock(self) -> None:
        """cmux9s, 2026-09-30: 31 GiB of SwiftPM scratch no job held, 0.0 GiB reclaimable, every job refused."""
        ci = self.root / "ci"
        make(ci / "seeds/p-a", age_hours=1)
        scratch = ci / "spm-scratch"
        for name, age in (("held", 1), ("free", 1), ("old", 50), (".trash-gone-7", 0.1)):
            make(scratch / name, age_hours=age)
        for name, age in (("held", 0.5), ("free", 0.5), ("old", 50)):
            lock = scratch / f"{name}.lock"
            lock.touch()
            t = time.time() - age * 3600
            os.utime(lock, (t, t))
        (scratch / "free.size").write_text("1048576")
        make(ci / "cmux-ci-2/spm-scratch/other", age_hours=50)  # only root 1's store holds scratch (cmux mini_store)
        # a job's holder: owned_spm_scratch.py hold keeps a shared flock for the whole job
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time\n"
                                   "f = open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_SH)\n"
                                   "print('held', flush=True); time.sleep(60)\n", os.fspath(scratch / "held.lock")],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        self.assertTrue(gd.spm_scratch_held(scratch / "held"))
        self.assertFalse(gd.spm_scratch_held(scratch / "free"))
        self.assertFalse(gd.spm_scratch_held(scratch / "old"))

        fams = [f for f in gd.fleet_families(ci) if f.id == gd.SPM_SCRATCH]
        self.assertEqual([f.root for f in fams], [scratch])
        self.assertFalse(gd.job_busy(fams[0], scratch / "free"), "its lock, not a slot marker search, decides")

        def verdicts(emergency: bool) -> dict[str, str]:
            devs = frozenset({scratch.stat().st_dev}) if emergency else frozenset()
            with mock.patch.object(gd, "EMERGENCY_DEVS", devs):
                items = gd.survey(fams, 6, 0)
            return {Path(i.path).name: i.verdict for i in items}

        # under pressure a used scratch ages like any cache; a leftover of a killed delete always goes
        self.assertEqual(verdicts(False), {"held": "in-use", "free": "recent", "old": "reclaimable",
                                           ".trash-gone-7": "reclaimable"})
        # below the emergency floor, whatever no job holds goes: it rebuilds in minutes, the runner floor is near
        got = verdicts(True)
        self.assertEqual(got, {"held": "in-use", "free": "reclaimable", "old": "reclaimable",
                               ".trash-gone-7": "reclaimable"})
        with mock.patch.object(gd, "EMERGENCY_DEVS", frozenset({scratch.stat().st_dev})):
            items = [i for i in gd.survey(fams, 6, 0) if i.verdict == "reclaimable"]
            # a job links "old" between the survey and the delete: apply re-checks the lock and keeps it
            late = open(scratch / "old.lock", "a")
            fcntl.flock(late, fcntl.LOCK_SH)
            with mock.patch.object(gd, "process_evidence", return_value=([], "")):
                gd.apply(items, {f.id: f for f in fams}, self.root / "r.jsonl", None, 6)
            late.close()
        self.assertFalse((scratch / "free").exists())
        self.assertFalse((scratch / "free.size").exists())
        self.assertFalse((scratch / ".trash-gone-7").exists())
        self.assertTrue((scratch / "old").exists(), "held at delete time")
        self.assertTrue((scratch / "held").exists())
        self.assertEqual(sorted(p.name for p in scratch.iterdir()),
                         ["free.lock", "held", "held.lock", "old", "old.lock"], "no .trash-* left behind")
        outcomes = {Path(r["path"]).name: r["outcome"]
                    for r in map(json.loads, (self.root / "r.jsonl").read_text().splitlines())}
        self.assertEqual(outcomes, {"free": "reclaimed", ".trash-gone-7": "reclaimed", "old": "changed:in-use"})
        # the delete itself takes the lock: a holder that arrives after every re-check still keeps its directory
        make(scratch / "raced", age_hours=50)
        late = open(scratch / "raced.lock", "a")
        fcntl.flock(late, fcntl.LOCK_SH)
        self.assertFalse(gd.remove_spm_scratch(scratch / "raced"))
        late.close()
        self.assertTrue(gd.remove_spm_scratch(scratch / "raced"))
        self.assertFalse((scratch / "raced").exists())

    def test_canonical_root_admission_goes_under_its_root_token(self) -> None:
        """cmux7s, cmux12s, cmux13s, 2026-10-21: 8-9 GiB of admission DerivedData per idle canonical root stayed,
        because the tmp family judged the whole root and its src/ checkout holds untracked build inputs."""
        tmp, capacity = self.root / "tmp", self.root / "capacity"
        capacity.mkdir()
        for k in (1, 2):
            (capacity / f"root-{k}.token").touch()
        one, two = tmp / "cmux-ci", tmp / "cmux-ci-2"
        make(one / "derived-data-compile-admission", age_hours=50)  # root 1: a job holds it
        make(two / "derived-data-compile-admission", age_hours=50)
        make(two / "compile-admission-cas", age_hours=50)
        make(two / "derived-data-rerun-resolve", age_hours=2)
        make(two / "src", age_hours=50)  # the root's checkout stays the tmp family's to judge
        make(tmp / "cmux-ci-next" / "derived-data-x", age_hours=50)  # not a canonical root
        for odd in ("cmux-ci-\u00b2", "cmux-ci-0", "cmux-ci-02", "cmux-ci-100"):  # not roots the runner hook takes
            make(tmp / odd / "derived-data-x", age_hours=50)
        job = open(capacity / "root-1.token")
        self.addCleanup(job.close)
        fcntl.flock(job, fcntl.LOCK_EX)  # glaeda-cmux-runner-hook take-root holds it for the whole job
        with mock.patch.object(gd, "CANONICAL_CAPACITY", capacity):
            fams = gd.canonical_admission_families(tmp)
            self.assertEqual([f.root for f in fams], [one, two])
            self.assertFalse(gd.job_busy(fams[1], two / "derived-data-compile-admission"),
                             "the root's token, not a slot marker search, decides")
            items = gd.survey(fams, 6, 0)
            self.assertEqual({os.path.relpath(i.path, tmp): i.verdict for i in items},
                             {"cmux-ci/derived-data-compile-admission": "in-use",
                              "cmux-ci-2/derived-data-compile-admission": "reclaimable",
                              "cmux-ci-2/compile-admission-cas": "reclaimable",
                              "cmux-ci-2/derived-data-rerun-resolve": "recent"})
            reclaim = [i for i in items if i.verdict == "reclaimable"]
            families = {f.id: f for f in fams}
            # a job takes root 2 between the survey and the delete: apply re-checks the token and keeps it all
            late = open(capacity / "root-2.token")
            fcntl.flock(late, fcntl.LOCK_EX)
            gd.apply(reclaim, families, self.root / "r1.jsonl", None, 6)
            late.close()
            self.assertTrue((two / "derived-data-compile-admission").exists())
            # apply marks what it refused blocked, so the next pass starts from a fresh survey
            reclaim = [i for i in gd.survey(fams, 6, 0) if i.verdict == "reclaimable"]
            self.assertEqual(len(reclaim), 2)
            gd.apply(reclaim, families, self.root / "r2.jsonl", None, 6)
            # the delete itself takes the token: a job that arrives after every re-check still keeps its root
            make(two / "derived-data-raced", age_hours=50)
            late = open(capacity / "root-2.token")
            fcntl.flock(late, fcntl.LOCK_SH)
            self.assertFalse(gd.remove_canonical_admission(two / "derived-data-raced"))
            late.close()
            self.assertTrue(gd.remove_canonical_admission(two / "derived-data-raced"))
        self.assertEqual(sorted(p.name for p in two.iterdir()), ["derived-data-rerun-resolve", "src"],
                         "nothing renamed aside is left behind")
        self.assertTrue((one / "derived-data-compile-admission").exists())
        for receipt, want in (("r1.jsonl", "changed:in-use"), ("r2.jsonl", "reclaimed")):
            outcomes = {r["outcome"] for r in map(json.loads, (self.root / receipt).read_text().splitlines())}
            self.assertEqual(outcomes, {want}, receipt)
        # no token yet: no job ever took the root; a path outside a canonical root is never this family's to delete
        with mock.patch.object(gd, "CANONICAL_CAPACITY", self.root / "none"):
            self.assertFalse(gd.canonical_root_held(two / "src"))
        self.assertTrue(gd.canonical_root_held(tmp / "cmux-ci-next" / "derived-data-x"))
        self.assertFalse(gd.remove_canonical_admission(tmp / "cmux-ci-next" / "derived-data-x"))

    def test_canonical_admission_gives_way_under_the_runner_floor_only_while_no_job_holds_its_root(self) -> None:
        """cmux14, 2026-10-25: at the 30 GiB floor with 9.1 and 8.7 GiB of admission DerivedData idle 9 and 21 h
        ("recent" under the 24 h window) on two roots no job held, and 0.0G reclaimable."""
        tmp, capacity = self.root / "tmp", self.root / "capacity"
        capacity.mkdir()
        for k in (1, 2, 3):
            (capacity / f"root-{k}.token").touch()
        roots = {k: tmp / ("cmux-ci" if k == 1 else f"cmux-ci-{k}") for k in (1, 2, 3)}
        for k, hours in ((1, 9.3), (2, 20.9), (3, 0.5)):
            make(roots[k] / "derived-data-compile-admission", age_hours=hours)
            make(roots[k] / "src", age_hours=hours)
        job = open(capacity / "root-3.token")
        self.addCleanup(job.close)
        fcntl.flock(job, fcntl.LOCK_EX)  # a compile (or a product consumer) holds root 3
        dev = tmp.stat().st_dev

        def verdicts(hold):
            with mock.patch.object(gd, "CANONICAL_CAPACITY", capacity), \
                    mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "FLOOR_HOLD", hold):
                fams = gd.canonical_admission_families(tmp)
                return fams, {os.path.relpath(i.path, tmp): i.verdict for i in gd.survey(fams, 6, 0)}
        _, above = verdicts({})
        self.assertEqual(set(above.values()), {"recent", "in-use"})  # above the floor the 24 h window stands
        fams, under = verdicts({dev: 50 * gd.GIB})
        self.assertEqual(under, {"cmux-ci/derived-data-compile-admission": "reclaimable",
                                 "cmux-ci-2/derived-data-compile-admission": "reclaimable",
                                 "cmux-ci-3/derived-data-compile-admission": "in-use"})
        # least recently used first, and only down to the floor plus target_margin_gib: 35 -> 45 -> stop at 50
        patches, removed = self._floor_disk(35.0)
        with patches[0], patches[1], mock.patch.object(gd, "CANONICAL_CAPACITY", capacity), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), \
                mock.patch.object(gd, "FLOOR_HOLD", {dev: 50 * gd.GIB}), contextlib.redirect_stdout(io.StringIO()):
            items = gd.survey(fams, 6, 0)
            gd.apply(items, {f.id: f for f in fams}, self.receipt(), None, 6, all_families=fams)
        self.assertEqual(len(removed), 2)
        self.assertFalse((roots[2] / "derived-data-compile-admission").exists(), "the older root goes first")
        self.assertFalse((roots[1] / "derived-data-compile-admission").exists())
        self.assertTrue((roots[3] / "derived-data-compile-admission").exists(), "a held root keeps its build")
        self.assertTrue(all((roots[k] / "src").exists() for k in roots), "src stays the tmp family's to judge")
        # a root without a token file is outside the runner hook's protocol: its 24 h window stands
        (capacity / "root-1.token").unlink()
        make(roots[1] / "derived-data-compile-admission", age_hours=9.3)
        _, under = verdicts({dev: 50 * gd.GIB})
        self.assertEqual(under["cmux-ci/derived-data-compile-admission"], "recent")

    def test_the_tmp_family_deletes_a_whole_canonical_root_only_under_its_root_token(self) -> None:
        """The tmp family judges /private/tmp/cmux-ci and cmux-ci-<k> as one item each and may delete a whole one
        after an hour idle. A compile job holds its root's token (glaeda-cmux-runner-hook take-root) for the whole
        job, and a job's tree can sit an hour unwritten (a long test phase), so only the token tells a root in use:
        it is in use while held, re-checked at apply, and the delete takes it exclusive as cmux-ci-admission does."""
        tmp, capacity = self.root / "tmp", self.root / "capacity"
        capacity.mkdir()
        for k in (1, 2):
            (capacity / f"root-{k}.token").touch()
        one, two = make(tmp / "cmux-ci", 8, age_hours=50), make(tmp / "cmux-ci-2", 8, age_hours=50)
        other = make(tmp / "cmux-ci-next", 8, age_hours=50)  # not a canonical root: the tmp family's as before
        fam = replace(next(f for f in gd.default_families() if f.id == "tmp"), root=tmp, skip=(), min_bytes=None)
        job = open(capacity / "root-1.token")
        self.addCleanup(job.close)
        fcntl.flock(job, fcntl.LOCK_EX)  # a job holds root 1 for its whole run
        with mock.patch.object(gd, "CANONICAL_CAPACITY", capacity):
            items = gd.survey([fam], 6, 0)
            self.assertEqual({os.path.relpath(i.path, tmp): i.verdict for i in items},
                             {"cmux-ci": "in-use", "cmux-ci-2": "reclaimable", "cmux-ci-next": "reclaimable"})
            self.assertIn("a job holds this canonical root (root token)",
                          next(i for i in items if i.path == os.fspath(one)).reasons)
            reclaim = [i for i in items if i.verdict == "reclaimable"]
            # a job takes root 2 between the survey and the delete: apply re-checks the token and keeps the root
            late = open(capacity / "root-2.token")
            fcntl.flock(late, fcntl.LOCK_EX)
            gd.apply(reclaim, {fam.id: fam}, self.root / "r1.jsonl", None, 6)
            late.close()
            self.assertTrue(two.exists())
            self.assertFalse(other.exists())
            outcomes = {Path(r["path"]).name: r["outcome"]
                        for r in map(json.loads, (self.root / "r1.jsonl").read_text().splitlines())}
            self.assertEqual(outcomes, {"cmux-ci-2": "changed:in-use", "cmux-ci-next": "reclaimed"})
            # the delete itself takes the token: a job that arrives after every re-check still keeps its root
            late = open(capacity / "root-2.token")
            fcntl.flock(late, fcntl.LOCK_SH)
            self.assertFalse(gd.remove_canonical_root(two))
            late.close()
            self.assertTrue(two.exists())
            reclaim = [i for i in gd.survey([fam], 6, 0) if i.verdict == "reclaimable"]
            self.assertEqual([Path(i.path).name for i in reclaim], ["cmux-ci-2"])
            gd.apply(reclaim, {fam.id: fam}, self.root / "r2.jsonl", None, 6)
        self.assertFalse(two.exists())
        self.assertTrue(one.exists())
        self.assertEqual(sorted(p.name for p in tmp.iterdir()), ["cmux-ci"], "nothing renamed aside is left behind")

    def test_the_emergency_floor_names_its_filesystems(self) -> None:
        total = 460 * gd.GIB  # emergency 10%:30-60 is 46 GiB; a 30 GiB runner floor lifts it to 50 on HOME's volume
        home = gd.HOME.stat().st_dev
        fss = {1: gd.Fs(1, "/a", 40 * gd.GIB, total, 115 * gd.GIB, 161 * gd.GIB),
               2: gd.Fs(2, "/b", 90 * gd.GIB, total, 115 * gd.GIB, 161 * gd.GIB),
               home: gd.Fs(home, "/", 48 * gd.GIB, total, 115 * gd.GIB, 161 * gd.GIB)}
        self.assertEqual(gd.emergency_devs(fss, "10%:30-60"), {1})
        self.assertEqual(gd.emergency_devs(fss, "10%:30-60", floor_gib=30), {1, home})

    def test_a_seed_the_archive_holds_goes_without_waiting(self) -> None:
        ci = self.root / "ci"
        (ci / "seeds").mkdir(parents=True)
        (ci / "seed-source.json").write_text(json.dumps({"prefix": "p-"}))
        for name, age in (("p-a", 0.2), ("p-b", 0.3), ("p-c", 1), ("p-d", 2), ("p-e", 3)):
            make(ci / "seeds" / name, age_hours=age)
        log = self.root / "serve.jsonl"
        archive, mini = "172.20.21.158", "172.20.21.9"

        def rec(client: str, outcome: str, **kw) -> str:
            return json.dumps({"client": client, "outcome": outcome, "role": "seed", **kw})

        lst = rec(archive, "list", count=5)
        (log.with_name("serve.jsonl.1")).write_text("\n".join([
            lst, rec(archive, "served", status=0, key="p-c"),
            rec(archive, "served", status=0, key="p-b"), lst, lst]) + "\n")
        log.write_text("\n".join([
            "not json", json.dumps({"client": ["x"], "outcome": "list"}), "[" * 5000,
            lst, rec(archive, "served", status=0, key="p-a"),
            rec(archive, "failed", status=1, key="p-d"),
            # a client that never listed is not the archive
            rec(mini, "served", status=0, key="p-e"),
            lst, lst,
            # served but not yet seen held by two later lists: the archive may still reject it
            rec(archive, "served", status=0, key="p-f"), lst,
            # the archive asked for p-b again (its check failed there): only a key's newest request counts
            rec(archive, "served", status=0, key="p-b")]) + "\n")
        self.assertEqual(gd.archived_seeds(log), {"p-a", "p-c"})
        self.assertEqual(gd.archived_seeds(self.root / "missing.jsonl"), frozenset())
        # a real DerivedData's module cache is wider than cmux_active's search budget, which reads as busy:
        # that must not keep cmux CI's warm state, which no hq reload slot marker ever covers
        with mock.patch.object(gd, "SEED_SERVE_LOG", log), mock.patch.object(gd, "cmux_active", lambda *a, **k: True):
            items = gd.survey(gd.fleet_families(ci), 24, 0)
            got = {Path(i.path).name: (i.verdict, i.reasons) for i in items}
            # the newest two stay even when archived; an archived older one goes; the rest wait out the window
            self.assertEqual(got["p-a"][0], "kept")
            self.assertEqual(got["p-b"][0], "kept")
            self.assertEqual(got["p-c"], ("reclaimable", ["the seed archive holds it"]))
            self.assertEqual(got["p-d"][0], "recent")
            self.assertEqual(got["p-e"][0], "recent")
            fams = {f.id: f for f in gd.fleet_families(ci)}
            with mock.patch.object(gd, "FLEET_CI", ci):
                gd.apply([i for i in items if i.verdict == "reclaimable"], fams, self.root / "r.jsonl", None, 24)
        self.assertFalse((ci / "seeds/p-c").exists())
        for name in ("p-a", "p-b", "p-d", "p-e"):
            self.assertTrue((ci / "seeds" / name).exists(), name)

    def test_pressure_targets_are_per_filesystem(self) -> None:
        make(self.root / "old")
        items = gd.survey([self.fam], 24, 0)
        fams = {self.fam.id: self.fam}
        dev = self.root.stat().st_dev
        # this filesystem is not under pressure: nothing on it may go
        gd.apply(items, fams, self.receipt(), {dev + 1: 1 << 62}, 24)
        self.assertTrue((self.root / "old").exists())
        # under pressure but already at its free target: stop before deleting
        gd.apply(items, fams, self.receipt(), {dev: 0}, 24)
        self.assertTrue((self.root / "old").exists())
        gd.apply(items, fams, self.receipt(), {dev: 1 << 62}, 24)
        self.assertFalse((self.root / "old").exists())

    def test_a_runner_disk_floor_lifts_the_pressure_thresholds(self) -> None:
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        self.assertEqual(gd.runner_floor_gib(home), 0.0, "no runner: no floor")
        for name, text in (("actions-runner-glaeda", "exec x job-started --min-free-gib 100 --min-free-gib 150\n"),
                           ("actions-runner-glaeda-1", "exec x job-started --min-free-gib 1e+02\n"),
                           ("actions-runner-glaeda-2", "exec x job-started --min-free-gib nope\n"),
                           ("actions-runner-glaeda-3", "exec x job-started --min-free-gib 1e999\n")):
            hook = home / name / "glaeda-hooks/job-started.sh"
            hook.parent.mkdir(parents=True)
            hook.write_text(text)
        self.assertEqual(gd.runner_floor_gib(home), 150.0, "the highest, each hook's last")
        free, total = gd.free_bytes(self.root)
        with mock.patch.object(gd, "HOME", self.root):  # the floor lifts only HOME's volume
            plain = gd.filesystems([self.fam], "0", "0")
            lifted = gd.filesystems([self.fam], "0", "0", floor_gib=1)
            (fs,), (up,) = plain.values(), lifted.values()
            self.assertEqual((fs.low, fs.target), (0, 0))
            self.assertEqual(up.low, min(int(5 * gd.GIB), total // 2), "pressure starts before the resume mark")
            self.assertEqual(up.target, min(int(21 * gd.GIB), total // 2))
            huge = next(iter(gd.filesystems([self.fam], "0", "0", floor_gib=1e6).values()))
            self.assertEqual((huge.low, huge.target), (total // 2, total // 2), "never past half the disk")
            high = gd.filesystems([self.fam], "100%", "100%", floor_gib=150)
            self.assertEqual(next(iter(high.values())).low, total, "never lowers a higher threshold")
        with mock.patch.object(gd, "HOME", Path("/dev")):  # another volume: no lift
            other = next(iter(gd.filesystems([self.fam], "0", "0", floor_gib=1).values()))
            self.assertEqual(other.low, 0)

    def test_floor_gib_adds_the_callers_floor(self) -> None:
        seen: list[float] = []

        def fss(*args: object) -> dict:
            seen.append(args[-1])
            return {}

        with mock.patch.object(gd, "runner_floor_gib", return_value=50.0), \
                mock.patch.object(gd, "filesystems", side_effect=fss), contextlib.redirect_stdout(io.StringIO()):
            gd.main(["--pressure", "--floor-gib", "150", "--no-snapshot"])
            gd.main(["--pressure", "--floor-gib", "10", "--no-snapshot"])
            gd.main(["--pressure", "--floor-gib", "inf", "--no-snapshot"])
        self.assertEqual(seen, [150.0, 50.0, 50.0], "the higher of the hooks' floors and the caller's")

    def test_one_eviction_at_a_time(self) -> None:
        lock = self.root / "evict.lock"
        receipt = ["--receipt", os.fspath(self.receipt())]
        with mock.patch.object(gd, "EVICT_LOCK", lock), \
                mock.patch.object(gd, "EVICT_OWNER", self.root / "evict.owner.json"), \
                mock.patch.object(gd, "HEALTH_SUMMARY", self.root / "summary.json"), \
                mock.patch.object(gd, "filesystems", return_value={}), \
                mock.patch.object(gd, "survey", return_value=[]) as survey, \
                mock.patch.object(gd, "apply", return_value=0):
            with lock.open("a") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                gd.EVICT_OWNER.write_text(json.dumps({"pid": os.getpid(), "owner": "test-owner",
                                                       "started_at": time.time(), "command": "glaeda-disk --apply"}))
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(gd.main(["--apply", "--no-snapshot", "--top", "0", *receipt]), 0)
                self.assertIn("another eviction running", out.getvalue())
                self.assertIn(f"pid={os.getpid()} owner=test-owner", out.getvalue())
                skipped = json.loads(self.receipt().read_text().splitlines()[-1])
                self.assertEqual((skipped["outcome"], skipped["reason"], skipped["owner"]["pid"]),
                                 ("skipped", "another eviction running", os.getpid()))
                summary = json.loads(gd.HEALTH_SUMMARY.read_text())
                self.assertEqual(summary["eviction_contention"]["owner"]["pid"], os.getpid())
                survey.assert_not_called()
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(gd.main(["--apply", "--json", "--no-snapshot", *receipt]), 3)
                report = json.loads(out.getvalue())
                self.assertEqual((report["status"], report["reason"], report["complete"]),
                                 ("deferred", "eviction_busy", False))
                self.assertNotIn("items", report)
                survey.assert_not_called()
            with contextlib.redirect_stdout(io.StringIO()):
                gd.main(["--apply", "--no-snapshot", "--top", "0", *receipt])
            survey.assert_called_once()

    def test_dead_eviction_owner_does_not_keep_contention_health_stale(self) -> None:
        receipt = self.receipt()
        lock = self.root / "evict.lock"
        receipt.write_text(json.dumps({"at": "now", "at_epoch": time.time(), "family": "eviction",
                                       "outcome": "skipped", "reason": "another eviction running",
                                       "owner": {"pid": 12345, "owner": "cmux", "state": "live"}}) + "\n")
        with mock.patch.object(gd, "EVICT_LOCK", lock), \
                mock.patch.object(gd, "_pid_alive", return_value=False):
            self.assertIsNone(gd.read_eviction_contention(receipt))

    def test_live_eviction_owner_with_held_lock_keeps_contention_health(self) -> None:
        receipt = self.receipt()
        lock = self.root / "evict.lock"
        receipt.write_text(json.dumps({"at": "now", "at_epoch": time.time(), "family": "eviction",
                                       "outcome": "skipped", "reason": "another eviction running",
                                       "owner": {"pid": os.getpid(), "owner": "cmux", "state": "live"}}) + "\n")
        with mock.patch.object(gd, "EVICT_LOCK", lock), lock.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            contention = gd.read_eviction_contention(receipt)
        self.assertIsNotNone(contention)
        self.assertEqual(contention["owner"]["pid"], os.getpid())

    def test_live_pid_without_held_lock_does_not_keep_contention_health_stale(self) -> None:
        receipt = self.receipt()
        lock = self.root / "evict.lock"
        receipt.write_text(json.dumps({"at": "now", "at_epoch": time.time(), "family": "eviction",
                                       "outcome": "skipped", "reason": "another eviction running",
                                       "owner": {"pid": os.getpid(), "owner": "cmux", "state": "live"}}) + "\n")
        with mock.patch.object(gd, "EVICT_LOCK", lock):
            self.assertIsNone(gd.read_eviction_contention(receipt))

    def test_measurement_busy_json_is_not_a_completed_empty_report(self) -> None:
        lock = self.root / "measurement.lock"
        with mock.patch.object(gd, "MEASURE_LOCK", lock), \
                mock.patch.object(gd, "default_families", return_value=[self.fam]), \
                mock.patch.object(gd, "filesystems", return_value={}), \
                mock.patch.object(gd, "survey") as survey:
            with lock.open("a") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    status = gd.main(["--json", "--refresh", "--no-snapshot"])
                self.assertEqual(status, 3)
                self.assertEqual(json.loads(out.getvalue()), {
                    "status": "deferred", "complete": False, "reason": "measurement_busy",
                    "message": "another measurement running; skipped",
                })
                survey.assert_not_called()

    def test_a_pressure_apply_below_the_emergency_floor_does_not_wait_for_a_measurement(self) -> None:
        lock = self.root / "measurement.lock"
        dev = gd.HOME.stat().st_dev
        total = 460 * gd.GIB
        # main() sets these module globals for the run; later tests read them.
        for name in ("EMERGENCY_DEVS", "ACTIVITY_MODE", "FLOOR_HOLD", "ROLE", "RUNNER_CACHE_PROFILE",
                     "_REFRESH_DEADLINE_ACTIVE", "_MEASURE_BUDGET_REACHED"):
            self.addCleanup(setattr, gd, name, getattr(gd, name))

        def run(free_gib: float) -> tuple[int, str, mock.MagicMock]:
            fss = {dev: gd.Fs(dev, "/", int(free_gib * gd.GIB), total, 128 * gd.GIB, 170 * gd.GIB)}
            out = io.StringIO()
            with mock.patch.object(gd, "MEASURE_LOCK", lock), \
                    mock.patch.object(gd, "EVICT_LOCK", self.root / "evict.lock"), \
                    mock.patch.object(gd, "EVICT_OWNER", self.root / "evict.owner.json"), \
                    mock.patch.object(gd, "HEALTH_SUMMARY", self.root / "summary.json"), \
                    mock.patch.object(gd, "default_families", return_value=[]), \
                    mock.patch.object(gd, "runner_floor_gib", return_value=0.0), \
                    mock.patch.object(gd, "filesystems", return_value=fss), \
                    mock.patch.object(gd, "ci_jobs", return_value=[]), \
                    mock.patch.object(gd, "survey", return_value=[]) as survey, \
                    mock.patch.object(gd, "apply", return_value=0), \
                    mock.patch.object(gd, "cleanup_paused_runner_work", return_value=0), \
                    mock.patch.object(gd, "evict_idle_runner_checkouts", return_value=0), \
                    mock.patch.object(gd, "notify_disk"), \
                    contextlib.redirect_stdout(out):
                with lock.open("a") as held:
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    status = gd.main(["--json", "--pressure", "--apply", "--no-snapshot", "--top", "0",
                                      "--receipt", os.fspath(self.receipt())])
            return status, out.getvalue(), survey

        # Under pressure but above the emergency floor (10% of 460 GiB): it still defers.
        status, out, survey = run(100)
        self.assertEqual(status, 3)
        self.assertIn("measurement_busy", out)
        survey.assert_not_called()
        # A deferred run lets go of the eviction lock it took, not only at exit.
        self.assertIsNone(gd._EVICT_HANDLE)
        # Nearly empty: it goes on.
        status, out, survey = run(0.9)
        self.assertNotIn("measurement_busy", out)
        survey.assert_called()

    def test_report_only_refresh_replaces_stale_timeout_summary_without_apply(self) -> None:
        receipt = self.receipt()
        summary_path = self.root / "summary.json"
        saved_findings = list(gd.EVICTION_STUCK)
        gd.EVICTION_STUCK.clear()
        self.addCleanup(lambda: (gd.EVICTION_STUCK.clear(), gd.EVICTION_STUCK.extend(saved_findings)))
        summary_path.write_text(json.dumps({
            "schema": "glaeda-disk/v1", "at": 1_790_500_000,
            "findings": [{"id": "eviction_stuck", "state": "stale", "count": 1}],
        }))
        with mock.patch.object(gd, "HEALTH_SUMMARY", summary_path), \
                mock.patch.object(gd, "default_families", return_value=[self.fam]), \
                mock.patch.object(gd, "filesystems", return_value={1: gd.Fs(1, "/", 2, 3, 0, 0)}), \
                mock.patch.object(gd, "survey", return_value=[]) as survey, \
                mock.patch.object(gd, "apply") as apply, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gd.main(["--refresh", "--json", "--no-snapshot", "--top", "0",
                                      "--receipt", os.fspath(receipt)]), 0)
        self.assertEqual(json.loads(summary_path.read_text())["findings"], [])
        survey.assert_called_once()
        apply.assert_not_called()

    def test_background_refresh_wall_clock_guard_restores_signal_state(self) -> None:
        previous = signal.getsignal(signal.SIGALRM)
        with mock.patch.object(gd, "survey", side_effect=gd.RefreshDeadlineExceeded):
            self.assertEqual(gd.bounded_refresh_survey([self.fam], 24, 0, {}, False, 1), [])
        self.assertIs(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertTrue(gd._MEASURE_BUDGET_REACHED)

    def test_background_refresh_main_guard_catches_timeout_and_cleans_up(self) -> None:
        previous = signal.getsignal(signal.SIGALRM)
        with mock.patch.object(gd, "_main_impl", side_effect=gd.RefreshDeadlineExceeded):
            self.assertEqual(
                gd.main(["--refresh", "--refresh-lock-fd", "3", "--refresh-budget-s", "1"]),
                0,
            )
        self.assertIs(signal.getsignal(signal.SIGALRM), previous)
        self.assertFalse(gd._REFRESH_DEADLINE_ACTIVE)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_survey_captures_process_evidence_before_measurement_walks(self) -> None:
        item = make(self.root / "measure-me", age_hours=48)
        events = []
        fam = gd.Family("tmp", self.root, True, "rebuild")
        with mock.patch.object(gd, "candidates", return_value=[item]), \
                mock.patch.object(gd, "process_evidence", side_effect=lambda: (events.append("evidence") or ([], ""))), \
                mock.patch.object(gd, "du_bytes", side_effect=lambda _path: (events.append("du") or 1 << 20)):
            gd.survey([fam], 24, 0, known={})
        self.assertEqual(events[:2], ["evidence", "du"])

    def test_survey_keeps_no_evidence_as_a_typed_blind_verdict(self) -> None:
        item = make(self.root / "blind", age_hours=48)
        fam = gd.Family("tmp", self.root, True, "rebuild")
        with mock.patch.object(gd, "candidates", return_value=[item]), \
                mock.patch.object(gd, "process_evidence", side_effect=gd.NoEvidence("ps failed")), \
                mock.patch.object(gd, "du_bytes", return_value=1 << 20):
            result = gd.survey([fam], 24, 0, known={})
        self.assertEqual(result[0].verdict, "in-use")
        self.assertTrue(any("no process evidence" in reason for reason in result[0].reasons))

    def test_refresh_timeout_reaps_owned_du_processes(self) -> None:
        class Proc:
            pid = 1234

            def communicate(self, timeout=None):
                return "", ""

        proc = Proc()
        gd._ACTIVE_DU_PROCS[proc.pid] = proc
        with mock.patch.object(gd.os, "killpg") as killpg:
            gd._terminate_active_children()
        killpg.assert_called_once_with(proc.pid, gd.signal.SIGKILL)
        gd._ACTIVE_DU_PROCS.pop(proc.pid, None)  # worker owns the reap/removal path

    def test_filesystems_group_roots_and_apply_thresholds(self) -> None:
        other = gd.Family("tmp", self.root, True, "scratch")
        fss = gd.filesystems([self.fam, other], "0", "100%")
        self.assertEqual(len(fss), 1)
        fs = next(iter(fss.values()))
        self.assertFalse(fs.under)
        self.assertEqual(fs.target, fs.total)
        self.assertEqual(gd.filesystems([self.fam], "100%", "100%")[fs.dev].low, fs.total)

    def test_directory_holding_a_socket_is_in_use(self) -> None:
        d = make(self.root / "tmux-1000")
        make(self.root / "plain")
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(d / "default"))
        # a long-lived server's socket is as old as its directory; only an old enough item pays for the socket walk
        for p in (d / "default", d):
            os.utime(p, (time.time() - 48 * 3600,) * 2)
        v = self.verdicts()
        self.assertEqual(v["tmux-1000"], "in-use")
        self.assertEqual(v["plain"], "reclaimable")
        forced = [gd.Item("xcode-derived-data", str(d), 1, 48, "reclaimable")]
        receipt = self.receipt()
        gd.apply(forced, {self.fam.id: self.fam}, receipt, None, 24)
        self.assertTrue(d.exists())
        self.assertIn("changed:in-use", receipt.read_text())

    def _age(self, root: Path, hours: float = 48) -> None:
        t = time.time() - hours * 3600
        for dirpath, dirnames, filenames in os.walk(root):
            for n in dirnames + filenames:
                os.utime(os.path.join(dirpath, n), (t, t), follow_symlinks=False)
        os.utime(root, (t, t))

    def _git(self, *args: str) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        # no detached auto-maintenance: it writes into .git after a test has aged the tree
        return subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args],
                              check=True, capture_output=True, text=True, env=env).stdout

    def _tmp_repos(self) -> tuple[Path, Path]:
        """A tmp-family root plus an origin repository with one commit outside it."""
        self.fam = gd.Family("tmp", self.root / "tmp", True, "scratch", files=True,
                             min_bytes=0, git_disposable=True)
        self.fam.root.mkdir()
        origin = self.root / "origin"
        # these stand in for a network server; any other local remote vouches for nothing
        gd.TRUSTED_LOCAL_REMOTES = (str(origin), str(self.root / "subsrc"))
        self.addCleanup(setattr, gd, "TRUSTED_LOCAL_REMOTES", ())
        self._git("init", "-q", "-b", "main", str(origin))
        (origin / "f").write_text("x")
        self._git("-C", str(origin), "add", "f")
        self._git("-C", str(origin), "commit", "-qm", "c")
        return self.fam.root, origin

    def test_tmpfs_is_judged_by_its_own_thresholds(self) -> None:
        mounts = self.root / "mounts"
        mounts.write_text(f"tmpfs {self.root} tmpfs rw 0 0\n")
        saved = gd.mount_point
        gd.mount_point = lambda p: self.root
        try:
            self.assertEqual(gd.fs_type(self.root, mounts), "tmpfs")
            mounts.write_text(f"/dev/x {self.root} ext4 rw 0 0\n")
            self.assertEqual(gd.fs_type(self.root, mounts), "ext4")
            self.assertEqual(gd.fs_type(self.root, self.root / "missing"), "")
        finally:
            gd.mount_point = saved
        saved_type, saved_darwin = gd.fs_type, gd.DARWIN
        gd.fs_type, gd.DARWIN = (lambda p, *_: "tmpfs"), False
        try:
            fs = next(iter(gd.filesystems([self.fam], "0", "0", "100%", "100%").values()))
        finally:
            gd.fs_type, gd.DARWIN = saved_type, saved_darwin
        self.assertTrue(fs.tmpfs)
        self.assertTrue(fs.under)  # free space alone would say no pressure with low "0"
        self.assertEqual(fs.target, fs.total)

    def test_tmp_files_are_candidates_and_removed(self) -> None:
        root, _ = self._tmp_repos()
        (root / "leaked.so").write_bytes(b"\0" * 4096)
        (root / "fresh.log").write_bytes(b"\0" * 4096)
        self._age(root / "leaked.so")
        v = {Path(i.path).name: i.verdict for i in gd.survey([self.fam], 24, 0)}
        self.assertEqual(v, {"leaked.so": "reclaimable", "fresh.log": "recent"})
        plain = gd.Family("tmp", root, True, "scratch")
        self.assertEqual(gd.survey([plain], 24, 0), [])  # files only when the family opts in
        gd.apply(gd.survey([self.fam], 24, 0), {"tmp": self.fam}, self.receipt(), None, 24)
        self.assertFalse((root / "leaked.so").exists())
        self.assertTrue((root / "fresh.log").exists())

    def test_nested_checkouts_in_idle_scratch_sessions(self) -> None:
        root, origin = self._tmp_repos()
        self.fam = gd.replace(self.fam, files=False, nested_git=True)
        for session in ("clean", "unpushed", "dirty"):
            (root / session / "scratchpad").mkdir(parents=True)
            self._git("clone", "-q", str(origin), str(root / session / "scratchpad/a"))
            self._git("clone", "-q", str(origin), str(root / session / "scratchpad/b"))
            (root / session / "scratchpad/notes.txt").write_text("n")
        (root / "unpushed/scratchpad/b/g").write_text("y")
        self._git("-C", str(root / "unpushed/scratchpad/b"), "add", "g")
        self._git("-C", str(root / "unpushed/scratchpad/b"), "commit", "-qm", "local")
        (root / "dirty/scratchpad/a/untracked").write_text("z")
        # a bare seed with a linked worktree: the branch lives only in the seed, inside the item
        (root / "seeded/scratchpad").mkdir(parents=True)
        self._git("clone", "-q", "--bare", str(origin), str(root / "seeded/scratchpad/seed.git"))
        self._git("-C", str(root / "seeded/scratchpad/seed.git"), "worktree", "add", "-q", "-b", "feat",
                  str(root / "seeded/scratchpad/wt"))
        (root / "seeded/scratchpad/wt/g").write_text("y")
        self._git("-C", str(root / "seeded/scratchpad/wt"), "add", "g")
        self._git("-C", str(root / "seeded/scratchpad/wt"), "commit", "-qm", "only in the seed")
        # a bare copy whose commits a remote confirms loses nothing; one with a local commit does
        (root / "bare/scratchpad").mkdir(parents=True)
        self._git("clone", "-q", "--bare", str(origin), str(root / "bare/scratchpad/copy.git"))
        (root / "barelocal/scratchpad").mkdir(parents=True)
        seed = root / "barelocal/scratchpad/seed"
        self._git("clone", "-q", "--bare", str(origin), str(seed))
        tree = self._git("--git-dir", str(seed), "rev-parse", "HEAD^{tree}").strip()
        local = self._git("--git-dir", str(seed), "commit-tree", tree, "-p", "HEAD", "-m", "local").strip()
        self._git("--git-dir", str(seed), "update-ref", "refs/heads/main", local)
        # a clone kept in an ignored directory of a clean checkout is judged too
        (root / "vendored/scratchpad").mkdir(parents=True)
        outer = root / "vendored/scratchpad/outer"
        self._git("clone", "-q", str(origin), str(outer))
        (outer / ".git/info/exclude").write_text("vendor/\n")
        (outer / "vendor").mkdir()
        self._git("clone", "-q", str(origin), str(outer / "vendor/lib"))
        (outer / "vendor/lib/g").write_text("y")
        self._git("-C", str(outer / "vendor/lib"), "add", "g")
        self._git("-C", str(outer / "vendor/lib"), "commit", "-qm", "vendored and unpushed")
        # checkouts nested deeper than git_state's search are still found and judged
        deep = root / "deep/scratchpad/a/b/c/d/e/f"
        deep.mkdir(parents=True)
        self._git("clone", "-q", str(origin), str(deep / "clone"))
        (deep / "clone/g").write_text("y")
        self._git("-C", str(deep / "clone"), "add", "g")
        self._git("-C", str(deep / "clone"), "commit", "-qm", "deep and unpushed")
        for d in root.iterdir():
            self._age(d)
        items = gd.survey([self.fam], 24, 0)
        v = {Path(i.path).name: i.verdict for i in items}
        self.assertEqual(v, {"clean": "reclaimable", "unpushed": "git-checkout", "dirty": "git-checkout",
                             "seeded": "git-checkout", "bare": "reclaimable", "barelocal": "git-checkout",
                             "deep": "git-checkout", "vendored": "git-checkout"})
        why = {Path(i.path).name: i.reasons for i in items}
        self.assertEqual(why["unpushed"], ["scratchpad/b: commits no remote confirms"])
        self.assertEqual(why["barelocal"], ["scratchpad/seed: commits no remote confirms"])
        self.assertEqual(why["deep"], ["scratchpad/a/b/c/d/e/f/clone: commits no remote confirms"])
        self.assertEqual(why["vendored"], ["scratchpad/outer: vendor/lib: commits no remote confirms"])
        # kept verdicts are cached, so a dead session with unpushed work is not re-walked every run
        self.assertIn(str(root / "deep"), json.loads(gd.NESTED_KEEP.read_text()))
        # a checkout rooted below the item stays protected without the opt-in
        plain = gd.replace(self.fam, nested_git=False)
        self.assertLessEqual({i.verdict for i in gd.survey([plain], 24, 0)}, {"git-checkout", "unchecked"})
        gd.apply(items, {"tmp": self.fam}, self.receipt(), None, 24)
        self.assertEqual(sorted(p.name for p in root.iterdir()),
                         ["barelocal", "deep", "dirty", "seeded", "unpushed", "vendored"])

    def test_family_size_floor_overrides_min_mib(self) -> None:
        root, _ = self._tmp_repos()
        (root / "small").write_bytes(b"\0" * 4096)
        self._age(root / "small")
        self.assertEqual(len(gd.survey([self.fam], 24, 1 << 30)), 1)
        self.fam = gd.replace(self.fam, min_bytes=None)
        self.assertEqual(gd.survey([self.fam], 24, 1 << 30), [])

    def test_disposable_checkouts_in_tmp(self) -> None:
        root, origin = self._tmp_repos()
        self._git("clone", "-q", str(origin), str(root / "pushed"))
        self._git("clone", "-q", str(origin), str(root / "unpushed"))
        (root / "unpushed/g").write_text("y")
        self._git("-C", str(root / "unpushed"), "add", "g")
        self._git("-C", str(root / "unpushed"), "commit", "-qm", "local")
        self._git("clone", "-q", str(origin), str(root / "dirty"))
        (root / "dirty/untracked").write_text("z")
        self._git("clone", "-q", str(origin), str(root / "stashed"))
        (root / "stashed/f").write_text("changed")
        self._git("-C", str(root / "stashed"), "stash", "-q")
        (root / "nested").mkdir()
        self._git("clone", "-q", str(origin), str(root / "nested/inner"))
        self._git("-C", str(origin), "worktree", "add", "-q", "-b", "wt", str(root / "worktree"))
        self._git("-C", str(origin), "worktree", "add", "-q", "--detach", str(root / "detached"))
        (root / "detached/h").write_text("w")
        self._git("-C", str(root / "detached"), "add", "h")
        self._git("-C", str(root / "detached"), "commit", "-qm", "orphan")
        self._git("clone", "-q", str(origin), str(root / "young"))
        for d in root.iterdir():
            if d.name != "young":
                self._age(d)
        items = gd.survey([self.fam], 24, 0)
        v = {Path(i.path).name: i.verdict for i in items}
        self.assertEqual(v, {"pushed": "reclaimable", "worktree": "reclaimable",
                             "unpushed": "git-checkout", "dirty": "git-checkout",
                             "stashed": "git-checkout", "nested": "git-checkout",
                             "detached": "git-checkout", "young": "git-checkout"})
        why = {Path(i.path).name: i.reasons for i in items}
        self.assertEqual(why["unpushed"], ["commits no remote confirms"])
        self.assertEqual(why["detached"], ["HEAD on no ref"])
        # without the opt-in every checkout stays protected
        plain = gd.replace(self.fam, git_disposable=False)
        self.assertEqual({i.verdict for i in gd.survey([plain], 24, 0)}, {"git-checkout"})
        gd.apply(items, {"tmp": self.fam}, self.receipt(), None, 24)
        self.assertEqual(sorted(p.name for p in root.iterdir()),
                         ["detached", "dirty", "nested", "stashed", "unpushed", "young"])
        listed = self._git("-C", str(origin), "worktree", "list", "--porcelain")
        self.assertNotIn(str(root / "worktree"), listed)  # pruned from its repository
        self.assertIn("refs/heads/wt", self._git("-C", str(origin), "for-each-ref"))

    def test_checkouts_with_hidden_git_state_are_kept(self) -> None:
        root, origin = self._tmp_repos()
        # a clean, pushed clone that backs a worktree with an unpushed detached commit
        self._git("clone", "-q", str(origin), str(root / "main"))
        self._git("-C", str(root / "main"), "worktree", "add", "-q", "--detach",
                  str(self.root / "elsewhere"))
        # a pushed superproject whose submodule commit exists only locally
        sub = self.root / "subsrc"
        self._git("clone", "-q", str(origin), str(sub))
        self._git("clone", "-q", str(origin), str(root / "super"))
        self._git("-C", str(root / "super"), "-c", "protocol.file.allow=always", "submodule",
                  "add", "-q", str(sub), "sm")
        (root / "super/sm/local").write_text("only here")
        self._git("-C", str(root / "super/sm"), "add", "local")
        self._git("-C", str(root / "super/sm"), "commit", "-qm", "local")
        self._git("-C", str(root / "super"), "add", "sm")
        self._git("-C", str(root / "super"), "commit", "-qm", "sm")
        self._git("-C", str(root / "super"), "push", "-q", "origin", "HEAD:refs/heads/super")
        self._git("-C", str(root / "super"), "fetch", "-q")
        # an edit hidden from status by skip-worktree
        self._git("clone", "-q", str(origin), str(root / "skipped"))
        self._git("-C", str(root / "skipped"), "update-index", "--skip-worktree", "f")
        (root / "skipped/f").write_text("edited")
        # a worktree whose commit only a worktree-local ref holds, and a locked one
        self._git("-C", str(origin), "worktree", "add", "-q", "--detach", str(root / "bisect"))
        (root / "bisect/b").write_text("b")
        self._git("-C", str(root / "bisect"), "add", "b")
        self._git("-C", str(root / "bisect"), "commit", "-qm", "b")
        self._git("-C", str(root / "bisect"), "update-ref", "refs/bisect/bad", "HEAD")
        self._git("-C", str(origin), "worktree", "add", "-q", "-b", "lk", str(root / "locked"))
        self._git("-C", str(origin), "worktree", "lock", str(root / "locked"))
        for d in root.iterdir():
            self._age(d)
        why = {Path(i.path).name: (i.verdict, i.reasons) for i in gd.survey([self.fam], 24, 0)}
        self.assertEqual(why, {
            "main": ("git-checkout", ["other worktrees use this repository"]),
            "super": ("git-checkout", ["submodule sm: commits no remote confirms"]),
            "skipped": ("git-checkout", ["files hidden from status"]),
            "bisect": ("git-checkout", ["HEAD on no ref"]),
            "locked": ("git-checkout", ["locked worktree"])})

    def test_submodules_are_judged_not_vetoed(self) -> None:
        root, origin = self._tmp_repos()
        sub = self.root / "subsrc"
        self._git("clone", "-q", str(origin), str(sub))
        add = ("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sm")

        def superproject(name: str, worktree: bool = False) -> Path:
            d = root / name
            if worktree:
                self._git("-C", str(origin), "worktree", "add", "-q", "-b", name, str(d))
            else:
                self._git("clone", "-q", str(origin), str(d))
            self._git("-C", str(d), *add)
            self._git("-C", str(d), "commit", "-qm", "sm")
            if not worktree:
                self._git("-C", str(d), "push", "-q", "origin", f"HEAD:refs/heads/{name}")
                self._git("-C", str(d), "fetch", "-q")
            return d

        superproject("pushed")  # submodule at a commit its remote has
        # pinned at a commit fetched by id: on the remote, but no remote-tracking ref has it
        extra = self.root / "extra"
        self._git("clone", "-q", str(sub), str(extra))
        (extra / "e").write_text("e")
        self._git("-C", str(extra), "add", "e")
        self._git("-C", str(extra), "commit", "-qm", "e")
        pinned = self._git("-C", str(extra), "rev-parse", "HEAD").strip()
        self._git("-C", str(extra), "push", "-q", "origin", "HEAD:refs/pinned/e")  # no branch
        d = superproject("fetched")
        self._git("-C", str(d / "sm"), "-c", "uploadpack.allowAnySHA1InWant=true", "fetch", "-q",
                  "origin", pinned)
        self._git("-C", str(d / "sm"), "checkout", "-q", pinned)
        self.assertTrue(self._git("-C", str(d / "sm"), "rev-list", "HEAD", "--not", "--remotes"))
        self._git("-C", str(d), "add", "sm")
        self._git("-C", str(d), "commit", "-qm", "pin")
        self._git("-C", str(d), "push", "-q", "origin", "HEAD:refs/heads/fetched")
        self._git("-C", str(d), "fetch", "-q")
        superproject("wt-pushed", worktree=True)
        d = superproject("dirty")
        (d / "sm/untracked").write_text("u")
        d = superproject("stashed")
        (d / "sm/f").write_text("changed")
        self._git("-C", str(d / "sm"), "stash", "-q")
        d = superproject("wt-local", worktree=True)  # modules live in the worktree's git dir
        (d / "sm/g").write_text("g")
        self._git("-C", str(d / "sm"), "add", "g")
        self._git("-C", str(d / "sm"), "commit", "-qm", "g")
        self._git("-C", str(d), "add", "sm")
        self._git("-C", str(d), "commit", "-qm", "bump")
        for c in root.iterdir():
            self._age(c)
        why = {Path(i.path).name: (i.verdict, i.reasons) for i in gd.survey([self.fam], 24, 0)}
        self.assertEqual(why, {
            "pushed": ("reclaimable", []),
            "fetched": ("reclaimable", []),
            "wt-pushed": ("reclaimable", []),
            "dirty": ("git-checkout", ["uncommitted or untracked changes"]),
            "stashed": ("git-checkout", ["submodule sm: stash entries"]),
            "wt-local": ("git-checkout", ["submodule sm: commits no remote confirms"])})

    def test_submodule_work_the_remote_lacks_is_kept(self) -> None:
        """The #1149 review repros: local submodule commits with no telltale reflog entry."""
        root, origin = self._tmp_repos()
        sub = self.root / "subsrc"
        self._git("clone", "-q", str(origin), str(sub))
        file_ok = ("-c", "protocol.file.allow=always")

        def superproject(name: str) -> Path:
            d = root / name
            self._git("clone", "-q", str(origin), str(d))
            self._git("-C", str(d), *file_ok, "submodule", "add", "-q", str(sub), "sm")
            self._git("-C", str(d), "commit", "-qm", "sm")
            self._git("-C", str(d), "push", "-q", "origin", f"HEAD:refs/heads/{name}")
            self._git("-C", str(d), "fetch", "-q")
            return d

        def commit(repo: Path, msg: str) -> str:
            self._git("-C", str(repo), "commit", "-q", "--allow-empty", "-m", msg)
            return self._git("-C", str(repo), "rev-parse", "HEAD").strip()

        d = superproject("orphaned")  # detached commit, then `submodule update` moves away
        commit(d / "sm", "local")
        self._git("-C", str(d), *file_ok, "submodule", "update", "-q")
        d = superproject("commit-tree")  # no commit-shaped reflog entry, only "branch: Created"
        c = self._git("-C", str(d / "sm"), "commit-tree", "HEAD^{tree}", "-p", "HEAD",
                      "-m", "ct").strip()
        self._git("-C", str(d / "sm"), "branch", "keep", c)
        d = superproject("expired")  # reflog expired
        self._git("-C", str(d / "sm"), "checkout", "-q", "-b", "feat")
        commit(d / "sm", "local")
        self._git("-C", str(d / "sm"), "checkout", "-q", "--detach", "HEAD~1")
        self._git("-C", str(d / "sm"), "reflog", "expire", "--expire=now", "--all")
        d = superproject("embedded")  # the submodule's .git is a directory in the tree
        self._git("-C", str(d), "submodule", "deinit", "-q", "-f", "sm")
        gitdir = self._git("-C", str(d), "rev-parse", "--path-format=absolute", "--git-dir")
        shutil.rmtree(Path(gitdir.strip()) / "modules/sm")
        shutil.rmtree(d / "sm")
        self._git("clone", "-q", str(sub), str(d / "sm"))
        self._git("-C", str(d / "sm"), "checkout", "-q", "-b", "feat")
        commit(d / "sm", "local")
        self._git("-C", str(d / "sm"), "checkout", "-q", "--detach", "origin/HEAD")
        d = superproject("sub-worktree")  # the submodule backs a worktree elsewhere
        self._git("-C", str(d / "sm"), "worktree", "add", "-q", "--detach",
                  str(self.root / "sub-wt"))
        d = superproject("local-fetch")  # commits fetched from another scratch repo
        other = self.root / "other"
        self._git("clone", "-q", str(sub), str(other))
        commit(other, "other")
        self._git("-C", str(d / "sm"), "fetch", "-q", str(other), "HEAD:refs/heads/fromother")
        for c in root.iterdir():
            self._age(c)
        why = {Path(i.path).name: (i.verdict, i.reasons) for i in gd.survey([self.fam], 24, 0)}
        confirm = ["submodule sm: commits no remote confirms"]
        self.assertEqual(why, {
            "orphaned": ("git-checkout", confirm),
            "commit-tree": ("git-checkout", confirm),
            "expired": ("git-checkout", confirm),
            "embedded": ("git-checkout", confirm),
            "sub-worktree": ("git-checkout", ["submodule sm: has worktrees of its own"]),
            "local-fetch": ("git-checkout", confirm)})

    def test_local_remotes_never_vouch_for_commits(self) -> None:
        root, origin = self._tmp_repos()
        a, b = root / "a", root / "b"
        self._git("clone", "-q", str(origin), str(a))
        self._git("-C", str(a), "commit", "-q", "--allow-empty", "-m", "only here")
        self._git("clone", "-q", str(a), str(b))  # b's origin is a
        self._git("-C", str(a), "remote", "add", "b", str(b))
        self._git("-C", str(a), "fetch", "-q", "b")
        for c in (a, b):
            self._age(c)
        v = {Path(i.path).name: (i.verdict, i.reasons) for i in gd.survey([self.fam], 24, 0)}
        self.assertEqual(v, {"a": ("git-checkout", ["commits no remote confirms"]),
                             "b": ("git-checkout", ["commits no remote confirms"])})
        self.assertEqual(gd.network_remotes(a / ".git"), ["origin"])  # not the sibling b

    def test_network_remote_urls(self) -> None:
        for url in ("https://github.com/o/r.git", "ssh://git@h/o/r", "git@github.com:o/r.git",
                    "big-red:Projects/x", "git://h/r"):
            self.assertTrue(gd.NETWORK_URL.match(url), url)
        for url in ("/tmp/x", "../b", "./b", "file:///tmp/x", "b", "/c/x"):
            self.assertFalse(gd.NETWORK_URL.match(url) and not url.startswith("file:"), url)

    def test_worktree_reflog_only_commit_is_kept(self) -> None:
        root, origin = self._tmp_repos()
        wt = root / "wt"
        self._git("-C", str(origin), "worktree", "add", "-q", "-b", "wt", str(wt))
        self._git("-C", str(wt), "checkout", "-q", "--detach")
        self._git("-C", str(wt), "commit", "-q", "--allow-empty", "-m", "made here")
        self._git("-C", str(wt), "checkout", "-q", "wt")
        self._age(wt)
        item = gd.survey([self.fam], 24, 0)[0]
        self.assertEqual((item.verdict, item.reasons),
                         ("git-checkout", ["commits only this worktree's reflog holds"]))

    def test_apply_rechecks_a_checkout_that_gained_work(self) -> None:
        root, origin = self._tmp_repos()
        self._git("clone", "-q", str(origin), str(root / "c"))
        self._age(root / "c")
        items = gd.survey([self.fam], 24, 0)
        self.assertEqual(items[0].verdict, "reclaimable")
        (root / "c/new").write_text("n")
        self._age(root / "c")
        receipt = self.receipt()
        gd.apply(items, {"tmp": self.fam}, receipt, None, 24)
        self.assertTrue((root / "c").exists())
        self.assertIn("changed:git", receipt.read_text())

    def test_cargo_target_search_is_report_only(self) -> None:
        fam = gd.Family("cargo-target", self.root, False, "cargo clean", depth=4,
                        match="target", marker="CACHEDIR.TAG")
        for rel in ("glaeda/target", "wts/branch/target", "mono/crates/cli/target"):
            make(self.root / rel)
            (self.root / rel / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55")
        make(self.root / "site/target")  # not Cargo: no CACHEDIR.TAG
        (self.root / "web/node_modules/pkg/target").mkdir(parents=True)
        (self.root / "web/node_modules/pkg/target/CACHEDIR.TAG").write_text("x")
        (self.root / "glaeda/target/debug/target").mkdir(parents=True)  # never searched inside a match
        (self.root / "glaeda/target/debug/target/CACHEDIR.TAG").write_text("x")
        found = sorted(str(p.relative_to(self.root)) for p in gd.candidates(fam))
        self.assertEqual(found, ["glaeda/target", "mono/crates/cli/target", "wts/branch/target"])
        items = gd.survey([fam], 24, 0)
        self.assertEqual({i.verdict for i in items}, {"report-only"})
        receipt = self.receipt()
        gd.apply(items, {fam.id: fam}, receipt, None, 24)
        self.assertTrue((self.root / "glaeda/target").exists())
        self.assertFalse(receipt.exists())

    def _checkout_builds(self) -> tuple[Path, gd.Family]:
        """projects/app (tracked root and nested packages) with a linked worktree in projects/wts and one
        outside projects; every .build is tagged unless the test says otherwise."""
        projects = self.root / "projects"
        app = projects / "app"
        self._git("init", "-q", "-b", "main", str(app))
        for rel in ("Package.swift", "Packages/macOS/Core/Package.swift", "Sources/x.swift"):
            (app / rel).parent.mkdir(parents=True, exist_ok=True)
            (app / rel).write_text("x")
        self._git("-C", str(app), "add", ".")
        self._git("-C", str(app), "commit", "-qm", "c")
        self._git("-C", str(app), "worktree", "add", "-q", str(projects / "wts/feature"), "-b", "feature")
        self._git("-C", str(app), "worktree", "add", "-q", str(self.root / "scratch/wt"), "-b", "scratch")
        tag = "Signature: 8a477f597d28d172789f06886806bc55"
        for co in (app, projects / "wts/feature", self.root / "scratch/wt"):
            for pkg in ("", "Packages/macOS/Core"):
                make(co / pkg / ".build")
                (co / pkg / ".build/CACHEDIR.TAG").write_text(tag)
        make(app / "Sources/.build")  # no Package.swift there: not SwiftPM's
        (app / "Sources/.build/CACHEDIR.TAG").write_text(tag)
        make(app / "Packages/macOS/Core/Tests/.build")  # untracked location, not a package
        make(app / ".glaeda/apple-build/cache/k1")
        (app / ".glaeda/apple-build/receipt.json").write_text("{}")
        for co in (app, projects / "wts/feature"):
            self._age(co)
        return projects, gd.checkout_build_family(projects)

    def test_checkout_builds_are_package_builds_and_apple_generations(self) -> None:
        projects, fam = self._checkout_builds()
        found = sorted(str(p.relative_to(projects)) for p in gd.candidates(fam))
        self.assertEqual(found, ["app/.build", "app/.glaeda/apple-build/cache/k1",
                                 "app/Packages/macOS/Core/.build", "wts/feature/.build",
                                 "wts/feature/Packages/macOS/Core/.build"])

    def test_checkout_build_goes_after_half_a_day_and_the_checkout_stays(self) -> None:
        projects, fam = self._checkout_builds()
        self._age(projects / "wts/feature/.build", hours=12)
        gd.process_evidence = lambda: ([], f"swift-frontend {projects}/app/Packages/macOS/Core/.build/x.o\n")
        items = gd.survey([fam], 6, 0)
        v = {str(Path(i.path).relative_to(projects)): i.verdict for i in items}
        self.assertEqual(v["app/.build"], "reclaimable")
        self.assertEqual(v["app/Packages/macOS/Core/.build"], "in-use")
        self.assertEqual(v["wts/feature/.build"], "reclaimable")  # 12 h retention for disposable output
        receipt = self.receipt()
        gd.apply(items, {fam.id: fam}, receipt, None, 6)
        self.assertFalse((projects / "app/.build").exists())
        self.assertFalse((projects / "app/.glaeda/apple-build/cache/k1").exists())
        self.assertTrue((projects / "app/.glaeda/apple-build/receipt.json").exists())
        self.assertTrue((projects / "app/Packages/macOS/Core/.build").exists())
        self.assertFalse((projects / "wts/feature/.build").exists())
        self.assertEqual(self._git("-C", str(projects / "app"), "status", "--porcelain", "-uno"), "")

    def test_apple_generation_stays_while_a_run_holds_the_lock(self) -> None:
        projects, fam = self._checkout_builds()
        state = projects / "app/.glaeda/apple-build"
        fd = os.open(state / "build.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        v = {str(Path(i.path).relative_to(projects)): i.verdict for i in gd.survey([fam], 6, 0)}
        self.assertEqual(v["app/.glaeda/apple-build/cache/k1"], "kept")
        fcntl.flock(fd, fcntl.LOCK_UN)
        (state / "inflight.json").write_text("{}")
        v = {str(Path(i.path).relative_to(projects)): i.verdict for i in gd.survey([fam], 6, 0)}
        self.assertEqual(v["app/.glaeda/apple-build/cache/k1"], "kept")
        (state / "inflight.json").unlink()
        v = {str(Path(i.path).relative_to(projects)): i.verdict for i in gd.survey([fam], 6, 0)}
        self.assertEqual(v["app/.glaeda/apple-build/cache/k1"], "reclaimable")

    def test_checkout_builds_ignore_the_callers_git_dir(self) -> None:
        projects, fam = self._checkout_builds()
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.root / "nowhere/.git")}):
            found = {str(p.relative_to(projects)) for p in gd.checkout_builds(projects)}
        self.assertIn("wts/feature/Packages/macOS/Core/.build", found)

    def test_apply_refreshes_only_checkout_build_sizes(self) -> None:
        projects, fam = self._checkout_builds()
        known = {str(projects / "app/.build"): 1, str(projects / "app"): 2, str(projects / "wts/feature"): 3}
        self.assertEqual(gd.fresh_for_apply(known, [fam]),
                         {str(projects / "app"): 2, str(projects / "wts/feature"): 3})


    def _runner_checkouts(self) -> tuple[Path, list[gd.Family]]:
        """Two enrolled runners, each with a cmux checkout holding SwiftPM and cargo output and git temp packs."""
        home = self.root / "home"
        for name in ("actions-runner-glaeda", "actions-runner-glaeda-2"):
            runner = home / name
            (runner / "glaeda-hooks").mkdir(parents=True)
            (runner / ".runner").write_text("{}")
            co = runner / "_work/cmux/cmux"
            (co / ".git/objects/pack").mkdir(parents=True)
            (co / ".git/objects/ab").mkdir()
            (co / "Sources").mkdir()
            make(co / "Packages/macOS/CmuxNext/.build")
            make(co / "Packages/Shared/.build")
            make(co / "cmux-tui/target")
            (co / "cmux-tui/target/CACHEDIR.TAG").write_text(
                "Signature: 8a477f597d28d172789f06886806bc55\n"
                "# This file is a cache directory tag created by cargo.\n")
            make(co / "Sources/target")  # no cargo tag: someone's source directory, not cargo's
            for rel in (".git/objects/pack/tmp_pack_Ab12Cd", ".git/objects/ab/tmp_obj_Xy98Zw",
                        ".git/objects/pack/pack-1234.pack"):
                (co / rel).write_bytes(b"\0" * (2 * 1024 * 1024))
            make(runner / "_work/_temp/cmux-derived-data-tests-1")  # the runner's own scratch is not a checkout
            # $RUNNER_WORKSPACE/.glaeda-state: its DerivedData is build output; SourcePackages and cas are kept
            for name in ("ios-e2e-mac-derived-data", "SourcePackages", "cas"):
                make(runner / "_work/cmux/.glaeda-state" / name)
            make(runner / "_work/_temp/.glaeda-state/DerivedData")  # not a workspace
            self._age(runner / "_work", hours=3)
        unenrolled = home / "actions-runner-old/_work/cmux/cmux"
        (unenrolled / ".git").mkdir(parents=True)
        make(unenrolled / "Packages/macOS/X/.build")
        return home, gd.runner_checkout_families(home)

    def test_runner_checkout_families_find_build_output_and_git_garbage(self) -> None:
        home, fams = self._runner_checkouts()
        self.assertEqual(sorted({(f.id, str(f.root.relative_to(home))) for f in fams}), [
            ("runner-checkout-build", "actions-runner-glaeda-2/_work"),
            ("runner-checkout-build", "actions-runner-glaeda/_work"),
            ("runner-git-garbage", "actions-runner-glaeda-2/_work"),
            ("runner-git-garbage", "actions-runner-glaeda/_work")])
        work = home / "actions-runner-glaeda/_work"
        build = next(f for f in fams if f.id == gd.RUNNER_BUILD_FAMILY and f.root == work)
        garbage = next(f for f in fams if f.id == gd.RUNNER_GIT_GARBAGE_FAMILY and f.root == work)
        self.assertEqual(sorted(str(p.relative_to(work)) for p in gd.candidates(build)), [
            "cmux/.glaeda-state/ios-e2e-mac-derived-data",
            "cmux/cmux/Packages/Shared/.build", "cmux/cmux/Packages/macOS/CmuxNext/.build",
            "cmux/cmux/cmux-tui/target"])
        self.assertEqual(sorted(str(p.relative_to(work)) for p in gd.candidates(garbage)), [
            "cmux/cmux/.git/objects/ab/tmp_obj_Xy98Zw", "cmux/cmux/.git/objects/pack/tmp_pack_Ab12Cd"])

    def test_runner_build_output_goes_under_pressure_only_while_its_runner_has_no_job(self) -> None:
        home, fams = self._runner_checkouts()
        busy = home / "actions-runner-glaeda-2"
        gd.process_evidence = lambda: ([], f"{busy}/bin/Runner.Worker spawnclient 151 154\n"
                                           f"{home}/actions-runner-glaeda/bin/Runner.Listener run\n")
        with mock.patch.object(gd, "ACTIVITY_MODE", "pressure"):
            items = gd.survey(fams, 6, 0)
        v = {str(Path(i.path).relative_to(home)): (i.verdict, i.reasons) for i in items}
        idle = "actions-runner-glaeda/_work/cmux/cmux/"
        live = "actions-runner-glaeda-2/_work/cmux/cmux/"
        for rel in ("Packages/macOS/CmuxNext/.build", "Packages/Shared/.build", "cmux-tui/target"):
            self.assertEqual(v[idle + rel][0], "reclaimable", rel)  # three hours idle, no Runner.Worker
            self.assertEqual(v[live + rel], ("in-use", ["actions-runner-glaeda-2 is running a job (Runner.Worker)"]))
        state = "actions-runner-glaeda/_work/cmux/.glaeda-state/"
        self.assertEqual(v[state + "ios-e2e-mac-derived-data"][0], "reclaimable")
        self.assertEqual(v["actions-runner-glaeda-2/_work/cmux/.glaeda-state/ios-e2e-mac-derived-data"],
                         ("in-use", ["actions-runner-glaeda-2 is running a job (Runner.Worker)"]))
        self.assertNotIn(state + "SourcePackages", v)
        self.assertNotIn(state + "cas", v)
        # an hour-old temp pack is garbage even while that runner has a job
        self.assertEqual(v[live + ".git/objects/pack/tmp_pack_Ab12Cd"][0], "reclaimable")
        self.assertNotIn(live + ".git/objects/pack/pack-1234.pack", v)
        self.assertNotIn("actions-runner-old/_work/cmux/cmux/Packages/macOS/X/.build", v)
        # outside a pressure run (the idle sweep, a report) build output is kept; temp packs still go
        with mock.patch.object(gd, "ACTIVITY_MODE", "sweep"):
            v = {str(Path(i.path).relative_to(home)): (i.verdict, i.reasons) for i in gd.survey(fams, 24, 0)}
        self.assertEqual(v[idle + "cmux-tui/target"], ("kept", ["runner build output: pressure-only policy"]))
        self.assertEqual(v[idle + ".git/objects/pack/tmp_pack_Ab12Cd"][0], "reclaimable")

    def test_runner_build_output_goes_at_once_under_the_runner_floor(self) -> None:
        home, fams = self._runner_checkouts()
        fams = [f for f in fams if f.id == gd.RUNNER_BUILD_FAMILY]
        for name in ("actions-runner-glaeda", "actions-runner-glaeda-2"):
            self._age(home / name / "_work", hours=0.2)  # built minutes ago, then the disk went under the floor
        worker = f"{home}/actions-runner-glaeda-2/bin/Runner.Worker spawnclient 1 2\n"
        gd.process_evidence = lambda: ([], worker)
        dev = home.stat().st_dev
        idle = "actions-runner-glaeda/_work/cmux/"
        live = "actions-runner-glaeda-2/_work/cmux/cmux/"

        def verdicts(hold):
            with mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "FLOOR_HOLD", hold):
                return {str(Path(i.path).relative_to(home)): i.verdict for i in gd.survey(fams, 6, 0)}
        self.assertEqual(verdicts({})[idle + "cmux/cmux-tui/target"], "recent")  # above the floor: the hour stands
        v = verdicts({dev: 50 * gd.GIB})
        for rel in ("cmux/Packages/macOS/CmuxNext/.build", "cmux/Packages/Shared/.build", "cmux/cmux-tui/target",
                    ".glaeda-state/ios-e2e-mac-derived-data"):
            self.assertEqual(v[idle + rel], "reclaimable", rel)
        self.assertEqual(v[live + "cmux-tui/target"], "in-use")  # a runner with a job keeps its output
        # apply stops at the floor plus target_margin_gib (50 GiB here): 25 -> 35 -> 45 -> 55, one item stays
        patches, removed = self._floor_disk(25.0)
        with patches[0], patches[1], mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), \
                mock.patch.object(gd, "FLOOR_HOLD", {dev: 50 * gd.GIB}), \
                mock.patch.object(gd, "command_lines", return_value=worker), \
                contextlib.redirect_stdout(io.StringIO()):
            items = gd.survey(fams, 6, 0)
            gd.apply(items, {f.id: f for f in fams}, self.receipt(), None, 6, all_families=fams)
        self.assertEqual(len(removed), 3)
        self.assertTrue(all(r.startswith(str(home / idle)) for r in removed))
        self.assertTrue((home / live / "cmux-tui/target").exists())

    def test_code_sign_clones_go_at_once_under_the_runner_floor(self) -> None:
        # cmux7s, 2026-10-21: 21 GiB free under a 30 GiB floor with 40 GiB of media CI's cmux DEV copies
        # "recent" (min_idle_hours 2): every launch copies the bundle again, so two hours of jobs filled it
        user_tmp, clones = self._clone_tree()
        bundle = clones / "com.cmuxterm.app.debug.ci.media.pr.1.head.code_sign_clone"
        for name, age_h in (("code_sign_clone.Old1", 1.5), ("code_sign_clone.Old2", 1.0), ("code_sign_clone.New", 0.5)):
            self._age(bundle / name, hours=age_h)
        fam = self._clone_family(user_tmp)
        dev = clones.stat().st_dev

        def not_newest(level, commands=None):
            return [p for p in level if p.name != "code_sign_clone.New"]

        def verdicts(hold):
            with mock.patch.object(gd, "keep_live_app_clones", side_effect=not_newest), \
                    mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "FLOOR_HOLD", hold):
                return {Path(i.path).name: i.verdict for i in gd.survey([fam], 6, 0)}
        self.assertEqual(verdicts({}), {"code_sign_clone.Old1": "recent", "code_sign_clone.Old2": "recent"})
        self.assertEqual(verdicts({dev: 50 * gd.GIB}),
                         {"code_sign_clone.Old1": "reclaimable", "code_sign_clone.Old2": "reclaimable"})
        # apply: an app that started from a copy since the survey keeps it; else they go, down to the hold's stop
        patches, removed = self._floor_disk(25.0)
        running = "/x/cmux DEV.app/Contents/MacOS/cmux\n"
        with patches[0], patches[1], mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), \
                mock.patch.object(gd, "FLOOR_HOLD", {dev: 50 * gd.GIB}), \
                mock.patch.object(gd, "keep_live_app_clones", side_effect=not_newest), \
                contextlib.redirect_stdout(io.StringIO()):
            items = gd.survey([fam], 6, 0)
            with mock.patch.object(gd, "command_lines", return_value=running), \
                    mock.patch.object(gd, "process_evidence", return_value=([], running)):
                gd.apply(items, {fam.id: fam}, self.receipt(), None, 6, all_families=[fam])
            self.assertEqual(removed, [])
            items = gd.survey([fam], 6, 0)  # apply marks what it held back: the next pass surveys again
            with mock.patch.object(gd, "command_lines", return_value="/bin/zsh\n"):
                gd.apply(items, {fam.id: fam}, self.receipt(), None, 6, all_families=[fam])
        self.assertEqual(sorted(Path(r).name for r in removed), ["code_sign_clone.Old1", "code_sign_clone.Old2"])
        self.assertTrue((bundle / "code_sign_clone.New").is_dir())

    @staticmethod
    def _registered_long_ago(runner_file: Path) -> None:
        """A .runner written a day ago: no worker under an older name can still be running (enrolled_runner_roster)."""
        old = time.time() - 24 * 3600
        os.utime(runner_file, (old, old))

    def _cmux_next_runners(self) -> tuple[Path, "gd.Family"]:
        """Two enrolled runners named mini-glaeda and mini-glaeda-2, each with its cmux-next scheme state, and
        a state no enrolled runner owns, all built an hour ago."""
        home = self.root / "home"
        for runner, name in (("actions-runner-glaeda", "mini-glaeda"), ("actions-runner-glaeda-2", "mini-glaeda-2")):
            (home / runner / "glaeda-hooks").mkdir(parents=True)
            # the runner writes .runner as UTF-8 with a byte order mark
            (home / runner / ".runner").write_text(json.dumps({"agentName": name}), encoding="utf-8-sig")
            self._registered_long_ago(home / runner / ".runner")
        root = home / "Library/Caches/cmux-next-ci"
        for name in ("mini-glaeda", "mini-glaeda-2", "gone-runner"):
            make(root / "cmux-scheme" / name / "Build", age_hours=1)
            (root / "cmux-scheme" / name / "SourcePackages").mkdir()
        return home, gd.cmux_next_ci_family(root)

    def test_idle_runners_cmux_next_build_output_goes_at_once_under_the_runner_floor(self) -> None:
        # cmux7s, 2026-10-21: 19.9 GiB of per-runner cmux-next Build "recent" (72 h window) under the floor
        home, fam = self._cmux_next_runners()
        worker = f"{home}/actions-runner-glaeda-2/bin/Runner.Worker spawnclient 1 2\n"
        dev = home.stat().st_dev

        def verdicts(hold):
            gd._DISCOVERED.clear()
            with mock.patch.object(gd, "HOME", home), \
                    mock.patch.object(gd, "process_evidence", return_value=([], worker)), \
                    mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "FLOOR_HOLD", hold):
                return {Path(i.path).parent.name: (i.verdict, i.reasons) for i in gd.survey([fam], 6, 0)
                        if Path(i.path).name == "Build"}
        v = verdicts({})
        self.assertEqual(v["mini-glaeda"][0], "recent")
        self.assertEqual(v["mini-glaeda-2"], ("in-use", ["actions-runner-glaeda-2 is running a job (Runner.Worker)"]))
        v = verdicts({dev: 50 * gd.GIB})
        self.assertEqual(v["mini-glaeda"][0], "reclaimable")
        self.assertEqual(v["mini-glaeda-2"][0], "in-use")  # a runner with a job keeps its output
        self.assertEqual(v["gone-runner"][0], "recent")  # no enrolled runner to ask: its window stands
        patches, removed = self._floor_disk(25.0, freed_gib=30.0)
        gd._DISCOVERED.clear()
        with patches[0], patches[1], mock.patch.object(gd, "HOME", home), \
                mock.patch.object(gd, "process_evidence", return_value=([], worker)), \
                mock.patch.object(gd, "command_lines", return_value=worker), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), \
                mock.patch.object(gd, "FLOOR_HOLD", {dev: 50 * gd.GIB}), contextlib.redirect_stdout(io.StringIO()):
            items = gd.survey([fam], 6, 0)
            gd.apply(items, {fam.id: fam}, self.receipt(), None, 6, all_families=[fam])
        # the Build alone brought free (25 + 30) past the hold's stop (50): the slot's inputs stay
        self.assertEqual(removed, [str(fam.root / "cmux-scheme/mini-glaeda/Build")])
        self.assertTrue((fam.root / "cmux-scheme/mini-glaeda/SourcePackages").is_dir())

    def test_the_cmux_next_build_log_is_generated_not_an_unknown_owners_state(self) -> None:
        # cmux14, 2026-10-25: every idle state's Build "in-use: cmux-next state has unknown children:
        # cmux-next-build.log", so the pressure runs freed 0.0 GiB at 10.8 GiB free under a 30 GiB floor
        root = self.root / "Library/Caches/cmux-next-ci"
        state = root / "cmux-scheme/runner-a"
        make(state / "Build", age_hours=96)
        (state / "cmux-next-build.log").write_text("** BUILD SUCCEEDED **\n")
        self._age(state / "cmux-next-build.log", hours=96)
        (state / "SourcePackages").mkdir()
        fam = gd.cmux_next_ci_family(root)
        with mock.patch.object(gd, "process_evidence", return_value=([], "")):
            got = {Path(i.path).name: i.verdict for i in gd.survey([fam], 24, 0)}
        self.assertEqual(got, {"Build": "reclaimable", "cmux-next-build.log": "reclaimable"})

    def test_under_the_runner_floor_idle_runners_whole_slots_go_least_recently_used_after_their_output(self) -> None:
        """cmux14, 2026-10-25: 115 GiB of cmux-next-ci, most of it the slots' inputs, at 10.8 GiB free under a
        30 GiB floor. Under the floor hold an idle enrolled runner's whole slot may go, after every generated
        child, least recently used first, until free reaches the hold's stop; a runner with a job, a process in a
        slot and a slot no enrolled runner owns keep theirs."""
        home, fam, st = self._cmux_next_cap_tree()
        worker = f"{home}/actions-runner-glaeda-2/bin/Runner.Worker spawnclient 1 2\n"
        dev = home.stat().st_dev

        def survey(hold):
            gd._DISCOVERED.clear()
            with mock.patch.object(gd, "HOME", home), \
                    mock.patch.object(gd, "process_evidence", return_value=([], worker)), \
                    mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "FLOOR_HOLD", hold):
                return gd.survey([fam], 6, 0)
        # no hold: no whole slot is listed at all
        self.assertFalse({Path(i.path) for i in survey({})} & set(st.values()))
        v = {Path(i.path): (i.verdict, i.reasons) for i in survey({dev: 50 * gd.GIB})}
        # enrolled runners' slots are listed whole; a slot no enrolled runner owns is not (the cap's 6 h rule)
        for key in ("mini-glaeda", "daily", "mini-glaeda-2", "release-new"):
            self.assertIn(st[key], v, key)
        for key in ("gone-runner", "release-old"):
            self.assertNotIn(st[key], v, key)
        self.assertEqual(v[st["mini-glaeda"]][0], "reclaimable")
        self.assertIn("whole cmux-next slot", " ".join(v[st["mini-glaeda"]][1]))
        for key in ("mini-glaeda-2", "release-new"):  # runner 2 has a job
            self.assertEqual(v[st[key]][0], "in-use", key)
        # apply: every idle Build first, then the slots least recently used first, until free reaches 50 GiB
        patches, removed = self._floor_disk(20.0, freed_gib=4.0)
        receipt = self.receipt()
        gd._DISCOVERED.clear()
        with patches[0], patches[1], mock.patch.object(gd, "HOME", home), \
                mock.patch.object(gd, "process_evidence", return_value=([], worker)), \
                mock.patch.object(gd, "command_lines", return_value=worker), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), \
                mock.patch.object(gd, "FLOOR_HOLD", {dev: 50 * gd.GIB}), contextlib.redirect_stdout(io.StringIO()):
            items = gd.survey([fam], 6, 0)
            gd.apply(items, {fam.id: fam}, receipt, None, 6, all_families=[fam])
        self.assertEqual([Path(r) for r in removed],
                         [st["mini-glaeda"] / "Build", st["daily"] / "Build",  # generated output first (5 h, 3 h)
                          st["mini-glaeda"], st["daily"]])  # then the idle slots, least recently used first
        for key in ("mini-glaeda-2", "release-new"):
            self.assertTrue((st[key] / "SourcePackages").is_dir(), key)
        # the stop: with free already at the hold's stop, no slot goes
        for key in ("mini-glaeda", "daily"):
            (st[key] / "SourcePackages").mkdir(parents=True)
            (st[key] / "info.plist").write_text("{}")
        patches, removed = self._floor_disk(50.0)
        gd._DISCOVERED.clear()
        with patches[0], patches[1], mock.patch.object(gd, "HOME", home), \
                mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                mock.patch.object(gd, "command_lines", return_value=""), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), \
                mock.patch.object(gd, "FLOOR_HOLD", {dev: 50 * gd.GIB}), contextlib.redirect_stdout(io.StringIO()):
            items = gd.survey([fam], 6, 0)
            gd.apply(items, {fam.id: fam}, self.receipt(), None, 6, all_families=[fam])
        self.assertFalse(set(map(Path, removed)) & set(st.values()))

    def test_a_slot_a_job_takes_after_the_survey_is_kept(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        dev = home.stat().st_dev
        patches, removed = self._floor_disk(20.0, freed_gib=1.0)
        named = f"xcodebuild -derivedDataPath {st['mini-glaeda']}\n"
        gd._DISCOVERED.clear()
        with patches[0], patches[1], mock.patch.object(gd, "HOME", home), \
                mock.patch.object(gd, "command_lines", return_value=""), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), \
                mock.patch.object(gd, "FLOOR_HOLD", {dev: 50 * gd.GIB}), contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(gd, "process_evidence", return_value=([], "")):
                items = [i for i in gd.survey([fam], 6, 0) if Path(i.path) == st["mini-glaeda"]]
            self.assertEqual([i.verdict for i in items], ["reclaimable"])
            with mock.patch.object(gd, "process_evidence", return_value=([], named)):
                gd.apply(items, {fam.id: fam}, self.receipt(), None, 6, all_families=[fam])
        self.assertEqual(removed, [])
        self.assertTrue((st["mini-glaeda"] / "SourcePackages").is_dir())

    def _floor_apply(self, home, fam, free_gib, freed_gib, evidence=([], ""), over=0, slots_only=False, during=None):
        """One pressure apply under a 50 GiB floor hold stop. Returns (released, removed, items)."""
        dev = home.stat().st_dev
        patches, removed = self._floor_disk(free_gib, freed_gib=freed_gib)
        gd._DISCOVERED.clear()
        with patches[0], patches[1], mock.patch.object(gd, "HOME", home), \
                mock.patch.object(gd, "command_lines", return_value=evidence[1]), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "CMUX_NEXT_CI_OVER", over), \
                mock.patch.object(gd, "FLOOR_HOLD", {dev: 50 * gd.GIB}), contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(gd, "process_evidence", return_value=([], "")):
                items = gd.survey([fam], 6, 0)
            if slots_only:
                items = [i for i in items if Path(i.path).name not in gd.CMUX_NEXT_CI_GENERATED_NAMES]
            with mock.patch.object(gd, "process_evidence", side_effect=during or (lambda: evidence)):
                released = gd.apply(items, {fam.id: fam}, self.receipt(), None, 6, all_families=[fam])
        return released, [Path(r) for r in removed], items

    def test_a_floor_slot_is_not_stopped_by_the_spent_cap_budget(self) -> None:
        # over the cap by one byte: the first Build spends the cap's budget, and the floor hold still wants 50 GiB
        home, fam, st = self._cmux_next_cap_tree()
        _, removed, _ = self._floor_apply(home, fam, 20.0, 4.0, over=1)
        for key in ("mini-glaeda", "daily"):
            self.assertIn(st[key], removed, key)

    def test_a_slots_bytes_count_once_with_its_children(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        worker = f"{home}/actions-runner-glaeda-2/bin/Runner.Worker spawnclient 1 2\n"  # runner 2 has a job
        released, removed, items = self._floor_apply(home, fam, 20.0, 4.0, evidence=([], worker))
        size = {Path(i.path): i.bytes for i in items}
        self.assertEqual(removed, [st["mini-glaeda"] / "Build", st["daily"] / "Build", st["mini-glaeda"],
                                       st["daily"]])
        # each slot's measured size includes its Build: the pass counts what the slots held, not more
        self.assertEqual(released, size[st["mini-glaeda"]] + size[st["daily"]])

    def test_the_floor_stop_is_read_again_right_before_a_slot_goes(self) -> None:
        # free reaches the stop while the fresh evidence is taken: the slot stays
        home, fam, st = self._cmux_next_cap_tree()
        dev = home.stat().st_dev
        state = {"free": 20 * gd.GIB}

        calls = {"n": 0}

        def evidence():  # apply's first read leaves free under the stop; the per-item fresh read sees it met
            calls["n"] += 1
            if calls["n"] >= 2:
                state["free"] = 60 * gd.GIB  # another job's cleanup finished meanwhile
            return [], ""
        gd._DISCOVERED.clear()
        with mock.patch.object(gd, "free_bytes", side_effect=lambda _p: (state["free"], 460 * gd.GIB)), \
                mock.patch.object(gd, "remove") as remove, mock.patch.object(gd, "HOME", home), \
                mock.patch.object(gd, "command_lines", return_value=""), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), \
                mock.patch.object(gd, "FLOOR_HOLD", {dev: 50 * gd.GIB}), contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(gd, "process_evidence", return_value=([], "")):
                items = [i for i in gd.survey([fam], 6, 0) if Path(i.path) == st["mini-glaeda"]]
            self.assertEqual([i.verdict for i in items], ["reclaimable"])
            with mock.patch.object(gd, "process_evidence", side_effect=evidence):
                gd.apply(items, {fam.id: fam}, self.receipt(), None, 6, all_families=[fam])
        self.assertGreaterEqual(calls["n"], 2)  # it got past the pre-item stop check to the fresh evidence
        remove.assert_not_called()

    def test_a_slot_whose_runner_takes_a_job_during_the_rename_is_put_back(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        worker = f"{home}/actions-runner-glaeda/bin/Runner.Worker spawnclient 1 2\n"
        calls = {"n": 0}

        def during():  # idle at the last check before the rename, then runner 1's Runner.Worker appears
            calls["n"] += 1
            return ([], "") if calls["n"] <= 2 else ([], worker)  # before the loop, before the rename, after it
        released, removed, items = self._floor_apply(home, fam, 20.0, 4.0, slots_only=True, during=during)
        self.assertNotIn(st["mini-glaeda"], removed)
        self.assertTrue((st["mini-glaeda"] / "SourcePackages").is_dir())  # back at its path
        self.assertFalse([p for p in st["mini-glaeda"].parent.iterdir() if p.name.startswith(gd.DELETING_PREFIX)])
        self.assertEqual(next(i for i in items if Path(i.path) == st["mini-glaeda"]).verdict, "blocked")

    def test_a_slot_with_inputs_wider_than_the_marker_search_still_goes(self) -> None:
        # a real slot's module cache holds thousands of entries: more than cmux_active's 5000-entry budget
        home, fam, st = self._cmux_next_cap_tree()
        wide = st["mini-glaeda"] / "ModuleCache.noindex"
        wide.mkdir()
        for n in range(5001):
            (wide / f"m{n}.pcm").touch()
        self._age(st["mini-glaeda"], hours=5)
        _, removed, items = self._floor_apply(home, fam, 20.0, 4.0, slots_only=True)
        self.assertEqual(next(i for i in items if Path(i.path) == st["mini-glaeda"]).verdict, "reclaimable")
        self.assertIn(st["mini-glaeda"], removed)

    def test_a_slot_in_use_is_kept_aside_when_a_new_slot_took_its_path(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        worker = f"{home}/actions-runner-glaeda/bin/Runner.Worker spawnclient 1 2\n"
        calls = {"n": 0}

        def during():  # after the rename, runner 1's job is live and has already made a new slot at the path
            calls["n"] += 1
            if calls["n"] <= 2:
                return [], ""
            if not st["mini-glaeda"].exists():
                make(st["mini-glaeda"] / "Build")
            return [], worker
        _, removed, items = self._floor_apply(home, fam, 20.0, 4.0, slots_only=True, during=during)
        self.assertNotIn(st["mini-glaeda"], removed)
        aside = [p for p in st["mini-glaeda"].parent.iterdir() if p.name.startswith(gd.DELETING_PREFIX)]
        self.assertEqual(len(aside), 1)  # the tree the job may still use is not deleted
        self.assertTrue((aside[0] / "SourcePackages").is_dir())
        self.assertTrue((st["mini-glaeda"] / "Build").exists())  # nor the job's new slot
        self.assertEqual(next(i for i in items if Path(i.path) == st["mini-glaeda"]).verdict, "blocked")

    def test_a_slot_put_back_never_replaces_an_empty_slot_a_job_made(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        worker = f"{home}/actions-runner-glaeda/bin/Runner.Worker spawnclient 1 2\n"
        calls = {"n": 0}

        def during():  # after the rename, runner 1's job is live and has made an empty slot (its cwd)
            calls["n"] += 1
            if calls["n"] <= 2:
                return [], ""
            if not st["mini-glaeda"].exists():
                st["mini-glaeda"].mkdir()
                made.append(st["mini-glaeda"].stat().st_ino)
            return [], worker
        made: list[int] = []
        _, removed, _ = self._floor_apply(home, fam, 20.0, 4.0, slots_only=True, during=during)
        self.assertNotIn(st["mini-glaeda"], removed)
        self.assertEqual(st["mini-glaeda"].stat().st_ino, made[0])  # the job's directory is still the one there
        self.assertEqual(list(st["mini-glaeda"].iterdir()), [])
        aside = [p for p in st["mini-glaeda"].parent.iterdir() if p.name.startswith(gd.DELETING_PREFIX)]
        self.assertEqual(len(aside), 1)
        self.assertTrue((aside[0] / "SourcePackages").is_dir())

    def test_rename_noreplace_refuses_an_empty_directory(self) -> None:
        src, dst = self.root / "src", self.root / "dst"
        src.mkdir()
        dst.mkdir()
        with self.assertRaises(OSError) as raised:
            gd.rename_noreplace(src, dst)
        self.assertIn(raised.exception.errno, (errno.EEXIST, errno.ENOTSUP))
        self.assertTrue(src.is_dir() and dst.is_dir())
        if raised.exception.errno == errno.EEXIST:
            dst.rmdir()
            gd.rename_noreplace(src, dst)
            self.assertTrue(dst.is_dir() and not src.exists())

    def test_an_unknown_owner_vetoes_a_state_and_its_children(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "FLOOR_HOLD", {home.stat().st_dev: 50 * gd.GIB}):
            (home / "actions-runner-glaeda-2/.runner").write_text("{not json")
            # mini-glaeda-2's own slot and children: no runner matches by name, and one .runner cannot be read
            self.assertEqual(gd.cmux_next_ci_owner(st["mini-glaeda-2"], fam.root), gd.OWNER_UNKNOWN)
            for path in (st["mini-glaeda-2"], st["mini-glaeda-2"] / "Build", st["gone-runner"]):
                self.assertIn("unknown", gd.cmux_next_ci_busy(fam, path, [], ""), path)
            # a runner it can read still owns its own
            self.assertEqual(gd.cmux_next_ci_owner(st["mini-glaeda"], fam.root), home / "actions-runner-glaeda")

    def test_a_release_owner_comes_from_one_read_of_its_runner(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        runner = home / "actions-runner-glaeda"
        good = (runner / ".runner").read_text(encoding="utf-8-sig")
        reads = {"n": 0}
        real = gd.runner_config

        def config(r):  # the first read of runner 1 succeeds, any later one fails
            if r == runner:
                reads["n"] += 1
                if reads["n"] > 1:
                    return {}
            return real(r)
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "runner_config", side_effect=config):
            self.assertEqual(gd.cmux_next_ci_owner(st["daily"], fam.root), runner)
        self.assertEqual(reads["n"], 1)
        self.assertTrue(good)

    def test_a_stale_root_the_cap_takes_is_put_back_when_its_tree_turns_busy(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        vetted = mock.patch.dict(gd.CMUX_NEXT_CI_VETTED_OWNER, {st["gone-runner"]: None})
        vetted.start()
        self.addCleanup(vetted.stop)
        doomed = st["gone-runner"].with_name(f"{gd.DELETING_PREFIX}{st['gone-runner'].name}-{os.getpid()}")
        recheck = gd.cmux_next_ci_slot_recheck(fam, st["gone-runner"], floor=False)
        os.rename(st["gone-runner"], doomed)
        (doomed / "unknown-child").mkdir()  # something new arrived in the detached tree
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "process_evidence", return_value=([], "")):
            with self.assertRaises(gd.SlotTaken):
                recheck()
        self.assertTrue((st["gone-runner"] / "SourcePackages").is_dir())
        (st["gone-runner"] / "unknown-child").rmdir()
        recheck = gd.cmux_next_ci_slot_recheck(fam, st["gone-runner"], floor=False)
        os.rename(st["gone-runner"], doomed)
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                mock.patch.object(gd, "free_bytes", return_value=(400 * gd.GIB, 460 * gd.GIB)):
            recheck()  # idle, and no floor stop applies to the cap's stale root: remove() deletes it
        self.assertTrue(doomed.is_dir())

    def test_a_state_a_renamed_runner_left_stays_unknown_for_a_jobs_timeout(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        runner = home / "actions-runner-glaeda"
        # runner 1 re-registered as another name just now: its old Runner.Worker may still use cmux-scheme/mini-glaeda
        (runner / ".runner").write_text(json.dumps({"agentName": "renamed"}), encoding="utf-8-sig")
        with mock.patch.object(gd, "HOME", home), \
                mock.patch.object(gd, "FLOOR_HOLD", {fam.root.stat().st_dev: 50 * gd.GIB}):
            self.assertEqual(gd.cmux_next_ci_owner(st["mini-glaeda"], fam.root), gd.OWNER_UNKNOWN)
            self.assertIn("unknown", gd.cmux_next_ci_busy(fam, st["mini-glaeda"], [], ""))
            self.assertNotIn(st["mini-glaeda"], gd.cmux_next_ci_stale_roots(fam.root))  # on every later pass too
            self._registered_long_ago(runner / ".runner")
            self.assertIsNone(gd.cmux_next_ci_owner(st["mini-glaeda"], fam.root))  # a job's timeout later: gone

    def test_a_runner_whose_config_went_missing_just_now_is_not_known_gone(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        runner = home / "actions-runner-glaeda-2"
        (runner / ".runner").unlink()  # mid re-registration: its old Runner.Worker may still use its state
        with mock.patch.object(gd, "HOME", home):
            self.assertEqual(gd.cmux_next_ci_owner(st["mini-glaeda-2"], fam.root), gd.OWNER_UNKNOWN)
            self.assertNotIn(st["mini-glaeda-2"], gd.cmux_next_ci_stale_roots(fam.root))
            self._registered_long_ago(runner)  # removed a job's timeout ago: gone
            self.assertIsNone(gd.cmux_next_ci_owner(st["mini-glaeda-2"], fam.root))

    def test_an_aside_slot_finished_counts_against_the_cap(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        aside = st["gone-runner"].with_name(f"{gd.DELETING_PREFIX}gone-runner-999999")
        os.rename(st["gone-runner"], aside)
        self._age(aside, hours=30)
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), \
                mock.patch.object(gd, "CMUX_NEXT_CI_OVER", 1), mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                mock.patch.object(gd, "command_lines", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            gd._DISCOVERED.clear()
            fams = [fam] + [f for f in gd.interrupted_families([fam]) if f.root == aside.parent]
            items = gd.survey(fams, 6, 0)
            by_path = {Path(i.path): i for i in items}
            self.assertIn(aside, by_path)
            for i in items:  # the aside first, then the cap's young output
                i.verdict = "reclaimable" if Path(i.path) in (aside, st["mini-glaeda"] / "Build") else "blocked"
            items.sort(key=lambda i: Path(i.path) != aside)
            gd.apply(items, {f.id: f for f in fams}, self.receipt(), None, 6, all_families=fams)
        self.assertFalse(aside.exists())
        self.assertTrue((st["mini-glaeda"] / "Build").exists())  # the aside's bytes already met the cap

    def test_a_drain_lists_no_whole_slot(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        with mock.patch.object(gd, "HOME", home), \
                mock.patch.object(gd, "FLOOR_HOLD", {fam.root.stat().st_dev: 50 * gd.GIB}):
            self.assertIn(st["mini-glaeda"], gd.cmux_next_ci_floor_slots(fam.root))
            with mock.patch.object(gd, "CMUX_NEXT_CI_DRAIN", True), mock.patch.dict(gd._DISCOVERED, {}, clear=True):
                self.assertEqual(gd.cmux_next_ci_floor_slots(fam.root), [])
                listed = gd.candidates(fam)
                self.assertFalse(set(listed) & set(gd.cmux_next_ci_state_roots(fam.root)))
                self.assertIn(st["mini-glaeda"] / "Build", listed)

    def test_an_unreadable_directory_in_a_slot_counts_as_holding_a_socket(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root reads any directory")
        home, fam, st = self._cmux_next_cap_tree()
        build = st["mini-glaeda"] / "Build"
        build.chmod(0)
        try:
            self.assertTrue(gd.cmux_next_ci_holds_socket(st["mini-glaeda"]))
        finally:
            build.chmod(0o755)
        self.assertFalse(gd.cmux_next_ci_holds_socket(st["mini-glaeda"]))

    def test_a_blank_agent_name_leaves_the_roster_incomplete(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        (home / "actions-runner-glaeda-2/.runner").write_text(json.dumps({"agentName": " "}), encoding="utf-8-sig")
        with mock.patch.object(gd, "HOME", home):
            names, complete, _ = gd.enrolled_runner_roster()
            self.assertNotIn(" ", names)
            self.assertFalse(complete)
            self.assertEqual(gd.cmux_next_ci_owner(st["mini-glaeda-2"], fam.root), gd.OWNER_UNKNOWN)

    def test_a_slot_is_not_deleted_beside_a_new_one_when_free_space_cannot_be_read(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        slot, runner = st["mini-glaeda"], home / "actions-runner-glaeda"
        doomed = slot.with_name(f"{gd.DELETING_PREFIX}{slot.name}-{os.getpid()}")
        with mock.patch.object(gd, "HOME", home), \
                mock.patch.object(gd, "FLOOR_HOLD", {slot.parent.stat().st_dev: 50 * gd.GIB}), \
                mock.patch.dict(gd.CMUX_NEXT_CI_VETTED_OWNER, {slot: runner}), \
                mock.patch.object(gd, "process_evidence", return_value=([], "")):
            recheck = gd.cmux_next_ci_slot_recheck(fam, slot, floor=True)
            os.rename(slot, doomed)
            slot.mkdir()  # a new slot stands at the path
            with mock.patch.object(gd, "free_bytes", side_effect=OSError(errno.EIO, "unreadable")), \
                    self.assertRaises(gd.SlotNotNeeded):
                recheck()
        self.assertTrue((doomed / "SourcePackages").is_dir())  # left aside, not handed to remove()

    def test_a_slot_whose_runner_is_renamed_after_its_veto_is_put_back(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        slot, runner = st["mini-glaeda"], home / "actions-runner-glaeda"
        doomed = slot.with_name(f"{gd.DELETING_PREFIX}{slot.name}-{os.getpid()}")
        worker = f"{runner}/bin/Runner.Worker spawnclient 1 2\n"
        with mock.patch.object(gd, "HOME", home), \
                mock.patch.object(gd, "FLOOR_HOLD", {slot.parent.stat().st_dev: 50 * gd.GIB}), \
                mock.patch.object(gd, "free_bytes", return_value=(10 * gd.GIB, 460 * gd.GIB)), \
                mock.patch.dict(gd.CMUX_NEXT_CI_VETTED_OWNER, {}):
            self.assertEqual(gd.cmux_next_ci_busy(fam, slot, [], ""), "")  # vetted under runner 1
            recheck = gd.cmux_next_ci_slot_recheck(fam, slot, floor=True)
            os.rename(slot, doomed)
            # runner 1 is re-registered under another name while its old Runner.Worker still runs
            (runner / ".runner").write_text(json.dumps({"agentName": "renamed"}), encoding="utf-8-sig")
            self._registered_long_ago(runner / ".runner")  # past the roster's own window: the vetted owner holds it
            self.assertIsNone(gd.cmux_next_ci_owner(slot, fam.root))
            with mock.patch.object(gd, "process_evidence", return_value=([], worker)), \
                    self.assertRaises(gd.SlotTaken):
                recheck()
        self.assertTrue((slot / "SourcePackages").is_dir())  # put back

    def test_no_state_parent_is_found_through_a_symlink(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        outside = self.root / "outside"
        (outside / ("ab" * 8) / "Release").mkdir(parents=True)
        os.symlink(outside, fam.root / "dev-daily-release-link")
        shutil.rmtree(fam.root / "release-compile")
        os.symlink(outside, fam.root / "release-compile")
        parents = gd.cmux_next_ci_state_parents(fam.root)
        self.assertFalse([d for d in parents if d.resolve().is_relative_to(outside.resolve())], parents)
        self.assertIn(fam.root / "cmux-scheme", parents)
        linked_root = self.root / "linked-cmux-next-ci"
        os.symlink(fam.root, linked_root)
        self.assertEqual(gd.cmux_next_ci_state_parents(linked_root), [])

    def test_an_aside_slot_keeps_its_vetoes_on_a_later_pass(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        scheme = st["mini-glaeda"].parent
        aside = scheme / f"{gd.DELETING_PREFIX}mini-glaeda-999999"
        os.rename(st["mini-glaeda"], aside)
        wide = aside / "ModuleCache.noindex"
        wide.mkdir()
        for n in range(3001):
            (wide / f"m{n}.pcm").touch()
        sock_dir = Path(tempfile.mkdtemp(dir="/tmp"))
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(os.fspath(sock_dir / "s"))
            os.rename(sock_dir / "s", aside / "Build" / "s")
            # the generic search gives up past its budget (where it meets the wide cache first depends on the
            # filesystem's listing order, so that is simulated): the aside's own vetoes still see the socket
            with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "holds_socket", return_value=False), \
                    contextlib.redirect_stdout(io.StringIO()):
                fams = [f for f in gd.interrupted_families([fam]) if f.root == scheme]
                self.assertEqual(len(fams), 1)
                self.assertIn("socket", gd.cmux_next_ci_aside_busy(aside, [], ""))
                worker = f"{home}/actions-runner-glaeda/bin/Runner.Worker spawnclient 1 2\n"
                os.unlink(aside / "Build" / "s")
                self.assertEqual(gd.cmux_next_ci_aside_busy(aside, [], ""), "")
                self.assertIn("Runner.Worker", gd.cmux_next_ci_aside_busy(aside, [], worker))
                with mock.patch.object(gd, "process_evidence", return_value=([], worker)), \
                        mock.patch.object(gd, "command_lines", return_value=worker):
                    items = [i for i in gd.survey(fams, 6, 0) if Path(i.path) == aside]
                    self.assertEqual(len(items), 1)
                    items[0].verdict = "reclaimable"
                    gd.apply(items, {f.id: f for f in fams}, self.receipt(), None, 6, all_families=fams)
            self.assertTrue((aside / "SourcePackages").is_dir())  # its runner's job keeps it on the second pass
        finally:
            server.close()
            shutil.rmtree(sock_dir, ignore_errors=True)

    def test_no_cmux_next_state_root_is_busy_for_its_width_alone(self) -> None:
        # an unowned stale root the cap takes whole, and an enrolled runner's slot, each wider than cmux_active's budget
        home, fam, st = self._cmux_next_cap_tree()
        for key in ("gone-runner", "mini-glaeda"):
            wide = st[key] / "ModuleCache.noindex"
            wide.mkdir()
            for n in range(5001):
                (wide / f"m{n}.pcm").touch()
            self.assertTrue(gd.cmux_active(st[key]), key)  # the generic search gives up
            self.assertFalse(gd.job_busy(fam, st[key]), key)

    def test_a_socket_past_a_wide_cache_still_keeps_a_slot(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        wide = st["mini-glaeda"] / "ModuleCache.noindex"
        wide.mkdir()
        for n in range(3001):
            (wide / f"m{n}.pcm").touch()
        self.assertFalse(gd.cmux_next_ci_tree_busy(st["mini-glaeda"]))  # the cache alone is not activity
        sock_dir = Path(tempfile.mkdtemp(dir="/tmp"))  # AF_UNIX paths are short: bind short, then move in
        try:
            server = socket.socket(socket.AF_UNIX)
            server.bind(os.fspath(sock_dir / "s"))
            os.rename(sock_dir / "s", st["mini-glaeda"] / "Build" / "s")
            self.assertIn("socket", gd.cmux_next_ci_tree_busy(st["mini-glaeda"]))
        finally:
            server.close()
            shutil.rmtree(sock_dir, ignore_errors=True)
        crowded = st["daily"] / "Build"
        for n in range(3001):
            (crowded / f"f{n}").touch()
        self.assertIn("socket", gd.cmux_next_ci_tree_busy(st["daily"]))  # a search that cannot finish fails closed

    def test_an_unreadable_runner_config_makes_no_state_unowned(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        old = time.time() - 30 * 86400
        for key in ("gone-runner", "release-old"):
            for d in (st[key], *st[key].rglob("*")):
                os.utime(d, (old, old))
        with mock.patch.object(gd, "HOME", home):
            self.assertIn(st["gone-runner"], gd.cmux_next_ci_stale_roots(fam.root))
            (home / "actions-runner-glaeda-2/.runner").write_text("{not json")
            self.assertEqual(gd.cmux_next_ci_stale_roots(fam.root), [])

    def test_a_slot_left_aside_is_found_when_no_state_is_left_beside_it(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        scheme = st["mini-glaeda"].parent
        for child in list(scheme.iterdir()):
            shutil.rmtree(child)
        aside = scheme / f"{gd.DELETING_PREFIX}mini-glaeda-999999"
        make(aside / "Build")
        release = st["daily"].parent
        shutil.rmtree(st["daily"])
        aside_release = release / f"{gd.DELETING_PREFIX}{st['daily'].name}-999999"
        make(aside_release / "Build")
        with mock.patch.object(gd, "HOME", home):
            roots = {f.root for f in gd.interrupted_families([fam])}
        self.assertIn(scheme, roots)
        self.assertIn(release, roots)

    def test_a_socket_that_moves_with_the_slot_keeps_it(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        calls = {"n": 0}
        real_tree_busy = gd.cmux_next_ci_tree_busy

        def tree_busy(state):  # a socket appears in the tree between the last check and the rename
            return "cmux-next state holds a socket" if gd.DELETING_PREFIX in state.name else real_tree_busy(state)
        with mock.patch.object(gd, "cmux_next_ci_tree_busy", side_effect=tree_busy):
            _, removed, items = self._floor_apply(home, fam, 20.0, 4.0, slots_only=True)
        self.assertNotIn(st["mini-glaeda"], removed)
        self.assertTrue((st["mini-glaeda"] / "SourcePackages").is_dir())  # put back
        self.assertEqual(next(i for i in items if Path(i.path) == st["mini-glaeda"]).verdict, "blocked")

    def test_a_slot_that_cannot_be_put_back_for_another_reason_stays_aside(self) -> None:
        # the floor is met after the rename, and the rename back fails without a new slot at the path
        home, fam, st = self._cmux_next_cap_tree()
        dev = home.stat().st_dev
        state = {"free": 20 * gd.GIB, "n": 0}

        def evidence():
            state["n"] += 1
            if state["n"] > 2:
                state["free"] = 60 * gd.GIB
            return [], ""

        def rename(src, dst):  # the put-back alone fails
            raise PermissionError(errno.EACCES, "Permission denied")
        gd._DISCOVERED.clear()
        with mock.patch.object(gd, "free_bytes", side_effect=lambda _p: (state["free"], 460 * gd.GIB)), \
                mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "command_lines", return_value=""), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "rename_noreplace", rename), \
                mock.patch.object(gd, "FLOOR_HOLD", {dev: 50 * gd.GIB}), contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(gd, "process_evidence", return_value=([], "")):
                items = [i for i in gd.survey([fam], 6, 0) if Path(i.path) == st["mini-glaeda"]]
            with mock.patch.object(gd, "process_evidence", side_effect=evidence):
                gd.apply(items, {fam.id: fam}, self.receipt(), None, 6, all_families=[fam])
        aside = [p for p in st["mini-glaeda"].parent.iterdir() if p.name.startswith(gd.DELETING_PREFIX)]
        self.assertEqual(len(aside), 1)
        self.assertTrue((aside[0] / "SourcePackages").is_dir())  # not deleted

    def test_a_slot_no_longer_needed_after_the_rename_is_put_back(self) -> None:
        # free reaches the stop while the post-rename evidence is taken: the slot goes back to its path
        home, fam, st = self._cmux_next_cap_tree()
        dev = home.stat().st_dev
        state = {"free": 20 * gd.GIB, "n": 0}

        def evidence():
            state["n"] += 1
            if state["n"] > 2:  # before the loop, before the rename, after it
                state["free"] = 60 * gd.GIB
            return [], ""
        gd._DISCOVERED.clear()
        with mock.patch.object(gd, "free_bytes", side_effect=lambda _p: (state["free"], 460 * gd.GIB)), \
                mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "command_lines", return_value=""), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), \
                mock.patch.object(gd, "FLOOR_HOLD", {dev: 50 * gd.GIB}), contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(gd, "process_evidence", return_value=([], "")):
                items = [i for i in gd.survey([fam], 6, 0) if Path(i.path) == st["mini-glaeda"]]
            with mock.patch.object(gd, "process_evidence", side_effect=evidence):
                released = gd.apply(items, {fam.id: fam}, self.receipt(), None, 6, all_families=[fam])
        self.assertEqual(released, 0)
        self.assertTrue((st["mini-glaeda"] / "SourcePackages").is_dir())
        self.assertFalse([p for p in st["mini-glaeda"].parent.iterdir() if p.name.startswith(gd.DELETING_PREFIX)])

    def _cmux_next_host_tree(self, hours: float = 2.0):
        """_cmux_next_cap_tree plus cmux#25147's per-host DEV build root: dev-daily-release/host with its source
        clone (.git included), its Release DerivedData and the lock its builds flock."""
        home, fam, st = self._cmux_next_cap_tree()
        host = fam.root / "dev-daily-release/host"
        make(host / "src/.git", age_hours=hours)
        xcode = host / "Release/xcode-2c2299e9015ce0c8"
        for name in ("CompilationCache.noindex", "SourcePackages", "Build"):
            make(xcode / name, age_hours=hours)
        (host / "lock").write_text('{"pid": 1, "runner": "mini-glaeda"}')
        self._age(host, hours=hours)
        return home, fam, st, host

    def test_the_per_host_cmux_next_root_goes_last_and_only_under_the_floor(self) -> None:
        """cmux#25147's host tree (14-16 GiB, 424 of 502 compile jobs hit warm on cmux14, 2026-10-26) is never the
        cap's: under the runner floor hold everything else goes first, the per-instance dev-daily state (its
        fallback) before all, and the host tree only while free is still below the floor itself."""
        home, fam, st, host = self._cmux_next_host_tree()
        dev = home.stat().st_dev
        everything_else = [st["daily"] / "Build", st["daily"],  # the per-instance fallback state first, then whole
                           st["release-old"], st["gone-runner"],  # then least recently used: 8 h, 7 h (whole)
                           st["mini-glaeda"] / "Build", st["mini-glaeda-2"] / "Build", st["release-new"] / "Build",
                           st["mini-glaeda"], st["mini-glaeda-2"], st["release-new"]]  # then the other slots
        # over the cap with no floor hold: the host tree is listed, but its 48 h window holds
        gd._DISCOVERED.clear()
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "CMUX_NEXT_CI_OVER", gd.GIB):
            v = {Path(i.path): i.verdict for i in gd.survey([fam], 6, 0)}
        self.assertEqual(v[host], "recent")
        self.assertNotIn(host / "Release/xcode-2c2299e9015ce0c8", v)  # never a state root of its own
        # 35 GiB free: under the hold's 50 GiB stop, above the 30 GiB floor. Everything else goes; the host stays
        with mock.patch.object(gd, "CMUX_NEXT_HOST_FLOOR", {dev: 30 * gd.GIB}):
            _, removed, items = self._floor_apply(home, fam, 35.0, 0.0, over=100 * gd.GIB)
        self.assertIn("per-host cmux-next build root", " ".join(
            next(i for i in items if Path(i.path) == host).reasons))
        self.assertEqual(removed, everything_else)
        self.assertTrue((host / "Release/xcode-2c2299e9015ce0c8/CompilationCache.noindex").is_dir())
        # over the cap, the per-instance fallback state idle past a job's timeout goes whole, inputs included; an
        # enrolled runner's other state as idle keeps its inputs
        shutil.rmtree(home)
        home, fam, st, host = self._cmux_next_host_tree()
        for key in ("daily", "mini-glaeda"):
            self._age(st[key], hours=7)
        with mock.patch.object(gd, "HOME", home):
            stale = gd.cmux_next_ci_stale_roots(fam.root)
        self.assertIn(st["daily"], stale)
        self.assertNotIn(st["mini-glaeda"], stale)
        self.assertNotIn(host, stale)
        # 10 GiB free: still under the floor after everything else, so the host tree goes, last
        shutil.rmtree(home)
        home, fam, st, host = self._cmux_next_host_tree()
        with mock.patch.object(gd, "CMUX_NEXT_HOST_FLOOR", {dev: 30 * gd.GIB}):
            _, removed, _ = self._floor_apply(home, fam, 10.0, 0.0, over=100 * gd.GIB)
        self.assertEqual(removed[:len(everything_else)], everything_else)
        self.assertEqual(sorted(removed[len(everything_else):]), [host / "Release", host / "src"])  # last
        # emptied, its lock left in place (the same inode a build may already have open), and no aside left
        self.assertEqual([p.name for p in host.iterdir()], ["lock"])

    def test_the_per_host_cmux_next_root_is_kept_while_its_lock_is_held_and_goes_once_idle(self) -> None:
        home, fam, st, host = self._cmux_next_host_tree()
        dev = home.stat().st_dev
        with open(host / "lock", "a") as build:  # cmux host_build_lock.py: flock(LOCK_EX | LOCK_NB), held by xcodebuild
            fcntl.flock(build, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(gd, "CMUX_NEXT_HOST_FLOOR", {dev: 30 * gd.GIB}):
                _, removed, items = self._floor_apply(home, fam, 10.0, 0.0, over=100 * gd.GIB)
            self.assertNotIn(host, removed)
            self.assertEqual(next(i for i in items if Path(i.path) == host).verdict, "in-use")
            # a build that takes the lock between the survey and the rename keeps it too
            fcntl.flock(build, fcntl.LOCK_UN)
            self.assertEqual(gd.cmux_next_ci_host_lock_held(host), "")
            fcntl.flock(build, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(gd.remove_cmux_next_host(host))
        self.assertTrue((host / "src/.git").is_dir())
        # idle 48 h: an idle sweep (no pressure, no floor) takes it, git clone and all; younger, it stays
        receipt = self.receipt()
        for hours, gone in ((47.0, False), (49.0, True)):
            self._age(host, hours=hours)
            gd._DISCOVERED.clear()
            with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                    mock.patch.object(gd, "command_lines", return_value=""), \
                    mock.patch.object(gd, "ACTIVITY_MODE", "sweep"), contextlib.redirect_stdout(io.StringIO()):
                items = [i for i in gd.survey([fam], 6, 0) if Path(i.path) == host]
                gd.apply(items, {fam.id: fam}, receipt, None, 6, all_families=[fam])
            self.assertEqual(not (host / "src").exists(), gone, hours)
        # an interrupted removal's aside tree is finished like any other, unless a build holds its lock
        aside = host / f"{gd.DELETING_PREFIX}src-999999"
        make(aside / "x")
        with mock.patch.object(gd, "HOME", home):
            self.assertIn(host, gd.cmux_next_ci_host_parents(fam.root))
            self.assertEqual(gd.cmux_next_ci_aside_busy(aside, [], ""), "")
            with open(host / "lock", "a") as build:
                fcntl.flock(build, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIn("holds the host root", gd.cmux_next_ci_aside_busy(aside, [], ""))
        # [] in disk-policy.json turns the rule off: the tree is then nobody's to judge, and kept
        with mock.patch.dict(gd.POLICY, {"cmux_next_ci_host_roots": []}):
            self.assertEqual(gd.cmux_next_ci_host_roots(fam.root), [])
        self.assertEqual(gd.POLICY_DEFAULTS["cmux_next_ci_host_roots"], ["dev-daily-release/host"])
        self.assertEqual(gd.POLICY_DEFAULTS["cmux_next_ci_host_idle_hours"], 48.0)

    def test_the_host_lock_keeps_its_inode_so_a_build_that_opened_it_still_excludes_others(self) -> None:
        # a build opened host/lock (fd 9) before the removal and flocks it after: a new lock file at the path would
        # let a second build lock another inode and share host/src with it
        home, fam, st, host = self._cmux_next_host_tree()
        inode = (host / "lock").stat().st_ino
        with open(host / "lock", "a") as early:
            self.assertTrue(gd.remove_cmux_next_host(host))
            fcntl.flock(early, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual((host / "lock").stat().st_ino, inode)
            with open(host / "lock", "a") as late, self.assertRaises(BlockingIOError):
                fcntl.flock(late, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_over_the_cap_an_idle_owned_fallback_state_goes_whole_without_a_floor_hold(self) -> None:
        home, fam, st, host = self._cmux_next_host_tree()
        for key in st:
            self._age(st[key], hours=3)
        self._age(st["daily"], hours=7)  # the owned per-instance fallback, idle past a job's timeout
        receipt = self.receipt()
        gd._DISCOVERED.clear()
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                mock.patch.object(gd, "command_lines", return_value=""), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "CMUX_NEXT_CI_OVER", 1), \
                contextlib.redirect_stdout(io.StringIO()):
            items = gd.survey([fam], 6, 0)
            self.assertFalse(gd.cmux_next_ci_floor_slot(fam, st["daily"]))
            gd.apply(items, {fam.id: fam}, receipt, None, 6, all_families=[fam])
        records = [json.loads(line) for line in receipt.read_text().splitlines()]
        self.assertEqual([(Path(r["path"]), r["outcome"]) for r in records], [(st["daily"], "reclaimed")])
        self.assertFalse(st["daily"].exists())
        self.assertTrue((host / "src").is_dir())

    def test_a_finished_host_aside_counts_against_the_cap(self) -> None:
        home, fam, st, host = self._cmux_next_host_tree()
        aside = host / f"{gd.DELETING_PREFIX}src-999999"
        make(aside / "x", mib=3)
        inter = gd.Family(gd.INTERRUPTED, host, True, "finish", build_output=True, max_idle_hours=0.0,
                          rebuild_minutes=0.1)
        items = [gd.Item(gd.INTERRUPTED, os.fspath(aside), 3 * 1024 * 1024, 48, "reclaimable", []),
                 gd.Item(fam.id, os.fspath(st["mini-glaeda"] / "Build"), 1024 * 1024, 5, "reclaimable", [])]
        receipt = self.receipt()
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                mock.patch.object(gd, "command_lines", return_value=""), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), \
                mock.patch.object(gd, "CMUX_NEXT_CI_OVER", 2 * 1024 * 1024), contextlib.redirect_stdout(io.StringIO()):
            gd.apply(items, {fam.id: fam, inter.id: inter}, receipt, None, 6, all_families=[fam, inter])
        self.assertFalse(aside.exists())
        self.assertTrue((st["mini-glaeda"] / "Build").is_dir())  # the aside alone brought the cache under its cap
        # the host root measured with that aside in it: a later pass sharing the tally counts only the rest
        taken = {host: 3 * 1024 * 1024}
        self._age(host, hours=49)
        item = gd.Item(fam.id, os.fspath(host), 5 * 1024 * 1024, 49, "reclaimable", [])
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                mock.patch.object(gd, "command_lines", return_value=""), \
                mock.patch.object(gd, "ACTIVITY_MODE", "sweep"), contextlib.redirect_stdout(io.StringIO()):
            released = gd.apply([item], {fam.id: fam}, receipt, None, 6, all_families=[fam], taken=taken)
        self.assertEqual(released, 2 * 1024 * 1024)
        self.assertEqual([p.name for p in host.iterdir()], ["lock"])
        # for the floor alone, free is read again under the lock: met by then, nothing goes
        make(host / "src")
        with self.assertRaises(gd.SlotNotNeeded):
            gd.remove_cmux_next_host(host, needed=lambda: False)
        self.assertTrue((host / "src").is_dir())
        # a host root that is a mount point is refused whole
        with mock.patch.object(gd.os.path, "ismount", return_value=True), self.assertRaises(OSError):
            gd.remove_cmux_next_host(host)

    def test_main_takes_the_per_host_cmux_next_root_after_idle_runners_checkouts(self) -> None:
        home, fam, st, host = self._cmux_next_host_tree()
        other = gd.Item(fam.id, os.fspath(st["mini-glaeda"] / "Build"), gd.GIB, 5.0, "reclaimable", [])
        last = gd.Item(fam.id, os.fspath(host), 14 * gd.GIB, 2.0, "reclaimable", [])
        with mock.patch.object(gd, "HOME", home):
            _, _, _, apply = self._main_run(["--pressure", "--apply"], items=[last, other], families=[fam])
        self.assertEqual([[i.path for i in call.args[0]] for call in apply.call_args_list],
                         [[other.path], [last.path]])  # its own pass, after the runner checkouts' last resort

    @staticmethod
    def _release_key(name: str, runner: Path) -> str:
        """cmux scripts/cmux-next/release-derived-data-path.sh: sha256("$RUNNER_NAME\n$GITHUB_WORKSPACE\n")[:16]."""
        return hashlib.sha256(f"{name}\n{runner}/_work/cmux/cmux\n".encode()).hexdigest()[:16]

    def _cmux_next_cap_tree(self, runners: bool = True) -> tuple[Path, "gd.Family", dict[str, Path]]:
        """cmux11s's layout, 2026-10-21: two enrolled runners' scheme states and a scheme state no enrolled
        runner owns; release-compile states keyed by runner 2 and by a runner no longer enrolled, and a dev-daily
        state keyed by runner 1; all younger than the family's 72 h window. Each state keeps its SourcePackages
        input beside a 1 MiB Build."""
        home = self.root / "home"
        if runners:
            for runner, name in (("actions-runner-glaeda", "mini-glaeda"),
                                 ("actions-runner-glaeda-2", "mini-glaeda-2")):
                (home / runner / "glaeda-hooks").mkdir(parents=True)
                (home / runner / ".runner").write_text(json.dumps({"agentName": name}), encoding="utf-8-sig")
                self._registered_long_ago(home / runner / ".runner")
        root = home / "Library/Caches/cmux-next-ci"
        xcode = "Release/xcode-2c2299e9015ce0c8"
        key1 = self._release_key("mini-glaeda", home / "actions-runner-glaeda")
        key2 = self._release_key("mini-glaeda-2", home / "actions-runner-glaeda-2")
        states = {
            "mini-glaeda": (root / "cmux-scheme/mini-glaeda", 5),
            "mini-glaeda-2": (root / "cmux-scheme/mini-glaeda-2", 4),
            "gone-runner": (root / "cmux-scheme/gone-runner", 7),
            "release-old": (root / "release-compile/aaaaaaaaaaaaaaaa" / xcode, 8),  # no enrolled runner's key
            "release-new": (root / "release-compile" / key2 / xcode, 1),
            "daily": (root / "dev-daily-release" / key1 / xcode, 3),
        }
        for state, hours in states.values():
            make(state / "Build", age_hours=hours)
            (state / "SourcePackages").mkdir()
            (state / "info.plist").write_text("{}")
            self._age(state, hours=hours)
        return home, gd.cmux_next_ci_family(root), {k: s for k, (s, _) in states.items()}

    def test_cmux_next_ci_cap_policy_default_and_override(self) -> None:
        self.assertEqual(gd.POLICY_DEFAULTS["cmux_next_ci_cap_gib"], 40.0)
        policy = self.root / "disk-policy.json"
        policy.write_text('{"cmux_next_ci_cap_gib": 25}')
        self.assertEqual(gd.disk_policy(policy)["cmux_next_ci_cap_gib"], 25.0)

    def test_cmux_next_ci_cap_state_measures_the_whole_cache(self) -> None:
        home, fam, _ = self._cmux_next_cap_tree()
        other = gd.Family("tmp", self.root, True, "x")
        record = self.root / "cmux-next-cap.json"
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "CMUX_NEXT_CAP_RECORD", record):
            # a report reads the last pressure run's measurement and never walks the cache itself
            with mock.patch.object(gd, "du_bytes", return_value=78 * gd.GIB) as du:
                self.assertIsNone(gd.cmux_next_ci_cap_state([other, fam], 40.0, measure=False))
                du.assert_not_called()
                got = gd.cmux_next_ci_cap_state([other, fam], 40.0, measure=True)
                self.assertEqual({k: got[k] for k in ("used", "cap", "over")},
                                 {"used": 78 * gd.GIB, "cap": 40 * gd.GIB, "over": 38 * gd.GIB})
                # a size probe, not an eviction measurement: no eviction_stuck or cooldown from its timeout
                du.assert_called_once_with(fam.root, record_timeout=False)
                du.reset_mock()
                seen = gd.cmux_next_ci_cap_state([other, fam], 25.0, measure=False)
                du.assert_not_called()
                self.assertEqual((seen["used"], seen["over"]), (78 * gd.GIB, 53 * gd.GIB))
                self.assertLessEqual(time.time() - seen["at"], 60)
                du.reset_mock()
                self.assertIsNone(gd.cmux_next_ci_cap_state([other, fam], 0.0, measure=True))  # 0 turns the cap off
                self.assertIsNone(gd.cmux_next_ci_cap_state([other], 40.0, measure=True))  # family not in this run
                du.assert_not_called()
                self.assertIsNone(gd.cmux_next_ci_cap_state([other, fam], 0.0))  # 0 turns the cap off
                self.assertIsNone(gd.cmux_next_ci_cap_state([other], 40.0))  # the family is not in this run
            with mock.patch.object(gd, "du_bytes", return_value=30 * gd.GIB):
                self.assertEqual(gd.cmux_next_ci_cap_state([fam], 40.0, measure=True)["over"], 0)
            with mock.patch.object(gd, "du_bytes", return_value=None):  # an unsized cache keeps the 72 h window
                self.assertIsNone(gd.cmux_next_ci_cap_state([fam], 40.0, measure=True))
        # a host with no enrolled glaeda runner is no CI host: the cap never applies there
        with mock.patch.object(gd, "HOME", self.root / "bare"), mock.patch.object(gd, "CMUX_NEXT_CAP_RECORD", record), \
                mock.patch.object(gd, "du_bytes", return_value=78 * gd.GIB) as du:
            self.assertIsNone(gd.cmux_next_ci_cap_state([fam], 40.0, measure=True))
            self.assertIsNone(gd.cmux_next_ci_cap_state([fam], 40.0, measure=False))
            du.assert_not_called()

    def test_cmux_next_ci_unowned_states_go_whole_only_past_the_job_timeout(self) -> None:
        # a state no enrolled runner owns but used within a job's timeout may still be a job's (a runner renamed
        # or re-enrolled mid-job): it keeps its inputs until it has been idle for GitHub's default job timeout
        self.assertEqual(gd.POLICY_DEFAULTS["cmux_next_ci_unowned_idle_hours"], 6.0)
        home, fam, st = self._cmux_next_cap_tree()
        with mock.patch.object(gd, "HOME", home):
            self.assertEqual(sorted(gd.cmux_next_ci_stale_roots(fam.root)),
                             sorted([st["gone-runner"], st["release-old"]]))  # 7 h and 8 h idle
            self._age(st["gone-runner"], hours=2)
            self.assertEqual(gd.cmux_next_ci_stale_roots(fam.root), [st["release-old"]])
            with mock.patch.dict(gd.POLICY, {"cmux_next_ci_unowned_idle_hours": 1.0}):
                self.assertIn(st["gone-runner"], gd.cmux_next_ci_stale_roots(fam.root))

    def test_cmux_next_ci_release_key_uses_the_runner_work_folder(self) -> None:
        # .runner's workFolder: relative to the runner directory, or absolute
        home, fam, _ = self._cmux_next_cap_tree()
        runner = home / "actions-runner-glaeda"
        elsewhere = self.root / "work-elsewhere"
        (runner / ".runner").write_text(json.dumps({"agentName": "mini-glaeda", "workFolder": os.fspath(elsewhere)}),
                                        encoding="utf-8-sig")
        (home / "actions-runner-glaeda-2/.runner").write_text(
            json.dumps({"agentName": "mini-glaeda-2", "workFolder": "w"}), encoding="utf-8-sig")
        with mock.patch.object(gd, "HOME", home):
            keys = gd.cmux_next_ci_release_keys()
        self.assertEqual(keys[gd.cmux_next_ci_release_key("mini-glaeda", f"{elsewhere}/cmux/cmux")], runner)
        self.assertEqual(keys[gd.cmux_next_ci_release_key("mini-glaeda-2", f"{home}/actions-runner-glaeda-2/w/cmux/cmux")],
                         home / "actions-runner-glaeda-2")
        self.assertNotIn(self._release_key("mini-glaeda", runner), keys)

    def test_cmux_next_ci_release_states_are_keyed_by_runner(self) -> None:
        # cmux11s, 2026-10-21: release-compile/3c965dbc3ce04dc2 is cmux11s-mac-mini-glaeda-1's
        self.assertEqual(gd.cmux_next_ci_release_key("cmux11s-mac-mini-glaeda-1",
                                                     "/Users/cmux/actions-runner-glaeda-1/_work/cmux/cmux"),
                         "3c965dbc3ce04dc2")
        home, fam, st = self._cmux_next_cap_tree()
        with mock.patch.object(gd, "HOME", home):
            self.assertEqual(gd.cmux_next_ci_runner(st["release-new"] / "Build", fam.root),
                             home / "actions-runner-glaeda-2")
            self.assertEqual(gd.cmux_next_ci_runner(st["daily"] / "Build", fam.root), home / "actions-runner-glaeda")
            self.assertIsNone(gd.cmux_next_ci_runner(st["release-old"] / "Build", fam.root))
            # so the runner floor hold covers a live runner's Release output too
            self.assertTrue(gd.floor_hold_family(fam, st["release-new"] / "Build"))
            self.assertEqual(sorted(gd.cmux_next_ci_stale_roots(fam.root)), sorted([st["gone-runner"], st["release-old"]]))
        # an aside tree a deletion left is never a state root
        aside = fam.root / "cmux-scheme" / f"{gd.DELETING_PREFIX}gone-runner-123"
        aside.mkdir()
        self.assertNotIn(aside, gd.cmux_next_ci_state_roots(fam.root))

    def test_cmux_next_ci_over_its_cap_lists_stale_roots_whole_and_drops_the_idle_window(self) -> None:
        # cmux11s, 2026-10-21: 78 GiB of cmux-next-ci, "reclaimable 0.0G", jobs refused at 21 GiB under a 40 GiB floor
        home, fam, st = self._cmux_next_cap_tree()
        worker = f"{home}/actions-runner-glaeda-2/bin/Runner.Worker spawnclient 1 2\n"

        def verdicts(over, mode="pressure"):
            gd._DISCOVERED.clear()
            with mock.patch.object(gd, "HOME", home), \
                    mock.patch.object(gd, "process_evidence", return_value=([], worker)), \
                    mock.patch.object(gd, "ACTIVITY_MODE", mode), mock.patch.object(gd, "CMUX_NEXT_CI_OVER", over):
                return {Path(i.path): (i.verdict, i.reasons) for i in gd.survey([fam], 6, 0)}
        # within the cap: the generated children alone, each waiting out the 72 h window
        v = verdicts(0)
        self.assertEqual(set(v), {s / "Build" for s in st.values()})
        self.assertEqual(v[st["mini-glaeda"] / "Build"][0], "recent")
        self.assertEqual(v[st["release-old"] / "Build"][0], "recent")
        # over the cap: states of runners no longer enrolled (scheme by name, Release by key) go whole; every
        # enrolled runner's states keep their inputs, and a runner with a job keeps its output too
        v = verdicts(5 * gd.GIB)
        self.assertEqual(set(v), {st["release-old"], st["gone-runner"], st["mini-glaeda"] / "Build",
                                  st["mini-glaeda-2"] / "Build", st["release-new"] / "Build",
                                  st["daily"] / "Build"})
        for key in ("release-old", "gone-runner"):
            self.assertEqual(v[st[key]][0], "reclaimable", key)
        for key in ("mini-glaeda", "daily"):
            self.assertEqual(v[st[key] / "Build"][0], "reclaimable", key)
        self.assertTrue(any("cap" in r for r in v[st["release-old"]][1]))
        for key in ("mini-glaeda-2", "release-new"):
            self.assertEqual(v[st[key] / "Build"],
                             ("in-use", ["actions-runner-glaeda-2 is running a job (Runner.Worker)"]), key)
        # the cap is a pressure rule: an idle sweep or a report keeps the window
        v = verdicts(5 * gd.GIB, mode="sweep")
        self.assertEqual(set(v), {s / "Build" for s in st.values()})
        self.assertTrue(all(verdict != "reclaimable" for verdict, _ in v.values()))

    def test_cmux_next_ci_cap_keeps_every_in_use_guard(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        (st["release-old"] / ".build.active").write_text("pid=1\n")  # an active marker in an unowned state
        self._age(st["release-old"], hours=8)
        (st["gone-runner"] / "unknown-owner-state").mkdir()  # an unknown child: someone else's state
        self._age(st["gone-runner"], hours=7)  # still past the job timeout
        xcodebuild = f"xcodebuild -derivedDataPath {st['daily']} build\n"  # a process naming a root
        gd._DISCOVERED.clear()
        with mock.patch.object(gd, "HOME", home), \
                mock.patch.object(gd, "process_evidence", return_value=([os.fspath(st["release-new"])], xcodebuild)), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "CMUX_NEXT_CI_OVER", gd.GIB):
            v = {Path(i.path): (i.verdict, i.reasons) for i in gd.survey([fam], 6, 0)}
        self.assertEqual(v[st["release-old"]][0], "in-use")
        self.assertIn("active or ambiguous marker", v[st["release-old"]][1][0])
        self.assertEqual(v[st["gone-runner"]][0], "in-use")
        self.assertIn("unknown children", v[st["gone-runner"]][1][0])
        self.assertEqual(v[st["daily"] / "Build"][0], "in-use")  # named by a running command
        self.assertEqual(v[st["release-new"] / "Build"][0], "in-use")  # a process's cwd
        self.assertEqual(v[st["mini-glaeda"] / "Build"][0], "reclaimable")

    def test_cmux_next_ci_states_stay_partial_without_enrolled_runners(self) -> None:
        # with no enrolled runner to ask, no state (scheme or release) can be called a gone runner's
        home, fam, st = self._cmux_next_cap_tree(runners=False)
        gd._DISCOVERED.clear()
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "CMUX_NEXT_CI_OVER", gd.GIB):
            paths = {Path(i.path) for i in gd.survey([fam], 6, 0)}
        for key in ("gone-runner", "release-old"):
            self.assertIn(st[key] / "Build", paths)
            self.assertNotIn(st[key], paths)

    def test_cmux_next_ci_cap_evicts_least_recently_used_until_under_the_cap(self) -> None:
        home, fam, st = self._cmux_next_cap_tree()
        over = int(2.5 * 1024 * 1024)  # three 1 MiB entries bring it under
        receipt = self.receipt()
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                mock.patch.object(gd, "command_lines", return_value=""), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "CMUX_NEXT_CI_OVER", over), \
                contextlib.redirect_stdout(io.StringIO()):
            gd._DISCOVERED.clear()
            items = gd.survey([fam], 6, 0)
            # a job starts on the unowned release state after the survey: the fresh recheck keeps it
            named = f"xcodebuild -derivedDataPath {st['release-old']}\n"
            with mock.patch.object(gd, "process_evidence", return_value=([], named)):
                gd.apply(items, {fam.id: fam}, receipt, None, 6, all_families=[fam])
        records = [json.loads(line) for line in receipt.read_text().splitlines()]
        self.assertEqual([(Path(r["path"]), r["outcome"]) for r in records],
                         [(st["release-old"], "changed:in-use"),  # 8 h, the oldest, but in use now
                          (st["gone-runner"], "reclaimed"),  # 6 h
                          (st["mini-glaeda"] / "Build", "reclaimed"),  # 5 h
                          (st["mini-glaeda-2"] / "Build", "reclaimed")])  # 4 h: now under the cap
        self.assertTrue(st["release-old"].is_dir())
        self.assertFalse(st["gone-runner"].exists())
        # with nothing naming it, the unowned Release state goes whole, leaving no empty <key>/Release behind
        with mock.patch.object(gd, "HOME", home), mock.patch.object(gd, "process_evidence", return_value=([], "")), \
                mock.patch.object(gd, "command_lines", return_value=""), \
                mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "CMUX_NEXT_CI_OVER", over), \
                contextlib.redirect_stdout(io.StringIO()):
            gd._DISCOVERED.clear()
            items = [i for i in gd.survey([fam], 6, 0) if Path(i.path) == st["release-old"]]
            gd.apply(items, {fam.id: fam}, receipt, None, 6, all_families=[fam])
        self.assertFalse((fam.root / "release-compile/aaaaaaaaaaaaaaaa").exists())
        self.assertTrue((fam.root / "release-compile").is_dir())
        self.assertTrue((st["mini-glaeda"] / "SourcePackages").is_dir())  # an enrolled runner's inputs stay
        for key in ("daily", "release-new"):
            self.assertTrue((st[key] / "Build").is_dir(), key)

    def test_main_shows_the_cmux_next_cap_and_records_it_in_the_run_line(self) -> None:
        self.addCleanup(setattr, gd, "CMUX_NEXT_CI_OVER", gd.CMUX_NEXT_CI_OVER)
        state = {"used": 47 * gd.GIB, "cap": 25 * gd.GIB, "over": 22 * gd.GIB, "at": time.time() - 600}
        with mock.patch.object(gd, "cmux_next_ci_cap_state", return_value=state) as cap:
            _, rows, _, _ = self._main_run(["--pressure", "--apply", "--cmux-next-cap-gib", "25"])
            self.assertEqual(cap.call_args[0][1], 25.0)
            self.assertIs(cap.call_args[1]["measure"], True)  # only a pressure run walks the cache
            self.assertEqual(gd.CMUX_NEXT_CI_OVER, 22 * gd.GIB)
            self.assertEqual(rows[-1]["cmux_next_ci_over_gib"], 22.0)
            # a report and a keepup without --apply show the last measurement; only a pressure run acts on it
            for argv in (["report"], ["keepup"]):
                cap.reset_mock()
                printed = io.StringIO()
                with mock.patch.object(gd, "print", create=True,
                                          side_effect=lambda *x, **k: printed.write(" ".join(map(str, x)) + "\n")):
                    self._main_run(argv)
                cap.assert_called_once()
                self.assertIs(cap.call_args[1]["measure"], False, argv)
                self.assertRegex(printed.getvalue(),
                                 r"cmux-next-ci: 47\.0G of its 25 GiB cap \(22\.0G over\), measured 1\d min ago", argv)
                self.assertEqual(gd.cmux_next_ci_over(), 0)  # no pressure run: the 72 h window stands
            # glaeda-fleet-status's `--json --top 0 --max-age 1000000` on every mini neither measures nor prints it
            cap.reset_mock()
            self._main_run(["--json", "--max-age", "1000000"])
            cap.assert_not_called()
        with mock.patch.object(gd, "cmux_next_ci_cap_state", return_value=None) as cap:
            _, rows, _, _ = self._main_run(["--pressure", "--apply"])
            self.assertEqual(cap.call_args[0][1], gd.POLICY["cmux_next_ci_cap_gib"])
            self.assertEqual(gd.CMUX_NEXT_CI_OVER, 0)
            self.assertIsNone(rows[-1]["cmux_next_ci_over_gib"])

    def _main_run(self, argv: list[str], free_gib: float = 20.0, **over):
        """One main() run on a fake 460 GiB HOME volume; returns (status, run log rows, survey and apply mocks)."""
        log = self.root / "runs.jsonl"
        dev = gd.HOME.stat().st_dev
        total = 460 * gd.GIB
        for name in ("EMERGENCY_DEVS", "ACTIVITY_MODE", "FLOOR_HOLD", "ROLE", "RUNNER_CACHE_PROFILE",
                     "_REFRESH_DEADLINE_ACTIVE", "_MEASURE_BUDGET_REACHED"):
            self.addCleanup(setattr, gd, name, getattr(gd, name))
        fss = {dev: gd.Fs(dev, "/", int(free_gib * gd.GIB), total, 120 * gd.GIB, 170 * gd.GIB)}
        items = over.pop("items", [])
        with mock.patch.object(gd, "RUN_LOG", log), \
                mock.patch.object(gd, "DISK_OFF", over.pop("off", self.root / "disk-off")), \
                mock.patch.dict(gd.POLICY, over.pop("policy", {})), \
                mock.patch.object(gd, "MEASURE_LOCK", self.root / "measure.lock"), \
                mock.patch.object(gd, "EVICT_LOCK", self.root / "evict.lock"), \
                mock.patch.object(gd, "EVICT_OWNER", self.root / "evict.owner.json"), \
                mock.patch.object(gd, "HEALTH_SUMMARY", self.root / "summary.json"), \
                mock.patch.object(gd, "default_families", return_value=over.pop("families", [])), \
                mock.patch.object(gd, "runner_floor_gib", return_value=30.0), \
                mock.patch.object(gd, "filesystems", return_value=fss), \
                mock.patch.object(gd, "free_bytes", return_value=(int(free_gib * gd.GIB), total)), \
                mock.patch.object(gd, "ci_jobs", return_value=[]), \
                mock.patch.object(gd, "survey", return_value=items) as survey, \
                mock.patch.object(gd, "apply", return_value=over.pop("released", 0)) as apply, \
                mock.patch.object(gd, "cleanup_paused_runner_work", return_value=0), \
                mock.patch.object(gd, "evict_idle_runner_checkouts", return_value=0), \
                mock.patch.object(gd, "clear_stale_build_markers", return_value=0), \
                mock.patch.object(gd, "repack_runner_checkouts", return_value=0), \
                mock.patch.object(gd, "notify_disk"), contextlib.redirect_stdout(io.StringIO()):
            status = gd.main(argv + ["--no-snapshot", "--top", "0", "--receipt", os.fspath(self.receipt())])
        rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        return status, rows, survey, apply

    def test_every_applying_run_writes_one_run_line(self) -> None:
        blocked = gd.Item("code-sign-clones", "/x/c", 12 * gd.GIB, 1.0, "recent", [])
        status, rows, survey, apply = self._main_run(["--pressure", "--apply"], items=[blocked], released=3 * gd.GIB)
        self.assertEqual(status, 0)
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        for key in ("at", "host", "mode", "off", "free_gib_before", "free_gib_after", "target_gib", "floor_gib",
                    "reclaimed_gib", "reached", "blocked", "duration_s", "skipped"):
            self.assertIn(key, row)
        self.assertEqual((row["mode"], row["off"], row["skipped"]), ("pressure", False, None))
        self.assertEqual((row["free_gib_before"], row["target_gib"], row["floor_gib"]), (20.0, 170.0, 30.0))
        self.assertEqual(row["target_bytes"], 170 * gd.GIB)
        self.assertEqual((row["targets"], row["default_targets"]), ({"/": 170 * gd.GIB}, True))  # the hook's skip
        self.assertEqual(row["config_sha256"], gd.config_sha256())
        self.assertEqual(row["reclaimed_gib"], 3.0)
        self.assertFalse(row["reached"])
        self.assertEqual(row["blocked"], {"code-sign-clones": 12.0})
        # no pressure: still one line, nothing reclaimed, the target already met
        status, rows, survey, _ = self._main_run(["--pressure", "--apply"], free_gib=200.0)
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[1]["skipped"], rows[1]["reached"]), ("no pressure", True))
        # a report writes none
        self._main_run(["report"])
        self.assertEqual(len((self.root / "runs.jsonl").read_text().splitlines()), 2)

    def test_the_run_line_says_when_its_targets_are_not_the_defaults(self) -> None:
        """The runner hook skips its pass on a run line's targets only when they are a plain --pressure --apply's
        under the same glaeda-disk and disk-policy.json (config_sha256)."""
        _, rows, _, _ = self._main_run(["--pressure", "--apply", "--target", "100"])
        self.assertFalse(rows[-1]["default_targets"], "a custom target must never let the runner hook skip")
        policy = self.root / "policy.json"
        before = gd.config_sha256(policy=policy)
        policy.write_text("{}")
        self.assertNotEqual(before, gd.config_sha256(policy=policy))

    def _queue(self):
        """This test's runner-hook queue files, in place of the home's."""
        lock, pending = self.root / "hook-pass.lock", self.root / "hook-pass.pending"
        lock.touch()
        return lock, pending, mock.patch.object(gd, "HOOK_PASS_LOCK", lock), \
            mock.patch.object(gd, "HOOK_PASS_PENDING", pending)

    def test_a_pressure_run_joins_a_running_hook_pass_instead_of_skipping_as_busy(self) -> None:
        """The LaunchAgent's 15-minute pass raced the runner hook's queued pass and skipped as eviction_busy."""
        lock, pending, p_lock, p_pending = self._queue()
        with p_lock, p_pending, lock.open("a") as held, \
                mock.patch.dict(os.environ, {gd.CALLER_ENV: "disk-pressure LaunchAgent"}), \
                mock.patch.object(gd, "cmux_next_ci_cap_state", return_value=None) as cap:
            os.environ.pop(gd.HOOK_PASS_ENV, None)
            fcntl.flock(held, fcntl.LOCK_EX)
            status, rows, survey, _ = self._main_run(["--pressure", "--idle", "--apply"], free_gib=100.0)
        self.assertEqual(status, 0)
        self.assertEqual((rows[-1]["skipped"], rows[-1]["caller"]), (gd.COALESCED_INTO_HOOK, "disk-pressure LaunchAgent"))
        self.assertTrue(pending.exists(), "the running hook pass serves this trigger with its follow-up")
        survey.assert_not_called()
        cap.assert_not_called()

    def test_a_run_that_takes_the_hook_lock_holds_it_and_serves_the_waiting_triggers(self) -> None:
        """A run that finds the hook's lock free takes it for its whole run, so a hook pass started meanwhile only
        queues behind it, and it clears the triggers it served (and only when it did its pass)."""
        lock, pending, p_lock, p_pending = self._queue()
        claimed = Path(f"{pending}.claimed")
        seen = []

        def working(*_a, **_k) -> None:
            seen.append(hook_held())

        def hook_held() -> bool:
            with lock.open("a") as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                return False

        pending.touch()
        with p_lock, p_pending, mock.patch.object(gd, "HOOK_PASS_CLAIMED", claimed), \
                mock.patch.object(gd, "cmux_next_ci_cap_state", return_value=None):
            os.environ.pop(gd.HOOK_PASS_ENV, None)
            with mock.patch.object(gd, "write_health_summary", side_effect=working):
                status, rows, _, _ = self._main_run(["--pressure", "--apply"], free_gib=100.0)
        self.assertEqual((status, rows[-1]["skipped"]), (0, None))
        self.assertTrue(seen and all(seen), "the hook lock is held while the run works")
        self.assertFalse(pending.exists() or claimed.exists(), "the triggers it served are cleared")
        self.assertFalse(hook_held(), "and the lock is let go")

    def test_a_run_deferred_for_ci_still_leaves_or_keeps_its_trigger(self) -> None:
        lock, pending, p_lock, p_pending = self._queue()
        claimed = Path(f"{pending}.claimed")
        with p_lock, p_pending, mock.patch.object(gd, "HOOK_PASS_CLAIMED", claimed), \
                mock.patch.object(gd, "cmux_next_ci_cap_state", return_value=None), \
                mock.patch.object(gd, "ci_defer", return_value="deferred: a CI job is running (x)"):
            os.environ.pop(gd.HOOK_PASS_ENV, None)
            with lock.open("a") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                _, rows, _, _ = self._main_run(["--pressure", "--apply"], free_gib=100.0,
                                               policy={"defer_during_ci": True})
                self.assertEqual(rows[-1]["skipped"], gd.COALESCED_INTO_HOOK)
                self.assertTrue(pending.exists(), "joined before the deferral")
            _, rows, _, _ = self._main_run(["--pressure", "--apply"], free_gib=100.0, policy={"defer_during_ci": True})
        self.assertEqual(rows[-1]["skipped"], "defer_during_ci")
        self.assertTrue(claimed.exists(), "a deferred run did no pass, so its claimed trigger stays")

    def test_a_lock_taking_run_that_does_no_pass_leaves_its_own_trigger(self) -> None:
        lock, pending, p_lock, p_pending = self._queue()
        claimed = Path(f"{pending}.claimed")
        self.assertFalse(pending.exists())
        with p_lock, p_pending, mock.patch.object(gd, "HOOK_PASS_CLAIMED", claimed), \
                mock.patch.object(gd, "cmux_next_ci_cap_state", return_value=None), \
                mock.patch.object(gd, "ci_defer", return_value="deferred: a CI job is running (x)"):
            os.environ.pop(gd.HOOK_PASS_ENV, None)
            _, rows, _, _ = self._main_run(["--pressure", "--apply"], free_gib=100.0, policy={"defer_during_ci": True})
        self.assertEqual(rows[-1]["skipped"], "defer_during_ci")
        self.assertTrue(claimed.exists())

    def test_a_trigger_that_cannot_be_left_never_overlaps_the_running_pass(self) -> None:
        lock, _, p_lock, _ = self._queue()
        with p_lock, mock.patch.object(gd, "HOOK_PASS_PENDING", self.root / "missing" / "hook-pass.pending"), \
                lock.open("a") as held, mock.patch.object(gd, "cmux_next_ci_cap_state", return_value=None):
            os.environ.pop(gd.HOOK_PASS_ENV, None)
            fcntl.flock(held, fcntl.LOCK_EX)
            _, rows, survey, _ = self._main_run(["--pressure", "--apply"], free_gib=100.0)
        self.assertEqual(rows[-1]["skipped"], gd.COALESCED_INTO_HOOK)
        survey.assert_not_called()

    def test_a_json_run_that_joins_the_queue_reports_deferred(self) -> None:
        lock, _, p_lock, p_pending = self._queue()
        with p_lock, p_pending, lock.open("a") as held, mock.patch.object(gd, "cmux_next_ci_cap_state", return_value=None):
            os.environ.pop(gd.HOOK_PASS_ENV, None)
            fcntl.flock(held, fcntl.LOCK_EX)
            status, _, _, _ = self._main_run(["--pressure", "--apply", "--json"], free_gib=100.0)
        self.assertEqual(status, 3)

    def test_an_emergency_run_joins_the_queue_too(self) -> None:
        """Below the emergency mark the hook's pass deletes the same way, so the LaunchAgent's run still joins it."""
        lock, pending, p_lock, p_pending = self._queue()
        with p_lock, p_pending, lock.open("a") as held, mock.patch.object(gd, "cmux_next_ci_cap_state", return_value=None):
            os.environ.pop(gd.HOOK_PASS_ENV, None)
            fcntl.flock(held, fcntl.LOCK_EX)
            _, rows, _, _ = self._main_run(["--pressure", "--apply"], free_gib=20.0)
        self.assertEqual(rows[-1]["skipped"], gd.COALESCED_INTO_HOOK)

    def test_the_hook_pass_itself_and_floor_evictions_are_never_queued(self) -> None:
        lock, pending, p_lock, p_pending = self._queue()
        with p_lock, p_pending, lock.open("a") as held, mock.patch.dict(os.environ, {gd.HOOK_PASS_ENV: "1"}), \
                mock.patch.object(gd, "cmux_next_ci_cap_state", return_value=None):
            fcntl.flock(held, fcntl.LOCK_EX)
            status, rows, survey, _ = self._main_run(["--pressure", "--apply"], free_gib=100.0)
        self.assertEqual((status, rows[-1]["skipped"]), (0, None))
        self.assertFalse(pending.exists())
        survey.assert_called()

    def test_a_held_evict_lock_skips_before_the_cap_measurement(self) -> None:
        """A busy run spent 10-20 s in the cmux-next cap's du before it found evict.lock taken; it now stops first,
        and its run line names its caller and the holder with when it took the lock."""
        _, _, p_lock, p_pending = self._queue()
        lock = self.root / "evict.lock"
        with p_lock, p_pending, lock.open("a") as held, mock.patch.dict(os.environ, {gd.CALLER_ENV: "test"}), \
                mock.patch.object(gd, "cmux_next_ci_cap_state", return_value=None) as cap:
            fcntl.flock(held, fcntl.LOCK_EX)
            (self.root / "evict.owner.json").write_text(json.dumps({
                "pid": os.getpid(), "owner": "cmux", "started_at": time.time() - 60,
                "command": "glaeda-disk --pressure --apply --top 0"}))
            status, rows, survey, _ = self._main_run(["--pressure", "--apply"], free_gib=100.0)
        self.assertEqual(status, 0)
        row = rows[-1]
        self.assertEqual((row["skipped"], row["caller"]), ("eviction_busy", "test"))
        self.assertEqual((row["evict_owner"]["pid"], row["evict_owner"]["command"]),
                         (os.getpid(), "glaeda-disk --pressure --apply --top 0"))
        self.assertIsNotNone(row["evict_owner"]["since"])
        cap.assert_not_called()
        survey.assert_not_called()

    def test_the_run_line_names_its_caller_without_the_variable(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(gd.CALLER_ENV, None)
            self.assertTrue(gd.run_caller())

    def test_a_deferred_run_writes_its_reason(self) -> None:
        lock = self.root / "measure.lock"
        with lock.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            status, rows, survey, _ = self._main_run(["--pressure", "--apply"], free_gib=100.0)
        survey.assert_not_called()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["skipped"], "measurement_busy")

    def test_the_kill_switch_deletes_nothing_and_says_so(self) -> None:
        off = self.root / "disk-off"
        off.write_text("")
        for over in ({"off": off}, {"policy": {"enabled": False}}):
            with self.subTest(over=list(over)):
                (self.root / "runs.jsonl").unlink(missing_ok=True)
                status, rows, survey, apply = self._main_run(["--pressure", "--apply"], **over)
                self.assertEqual(status, 0)
                survey.assert_not_called()
                apply.assert_not_called()
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0]["off"])
                self.assertEqual(rows[0]["reclaimed_gib"], 0.0)
        self.assertIn("disk-off", gd.__doc__)

    def test_floor_hold_only_below_the_gates_resume_mark(self) -> None:
        dev = self.root.stat().st_dev

        def fss(free_gib, tmpfs=False):
            return {dev: gd.Fs(dev, "/", int(free_gib * gd.GIB), 460 * gd.GIB, 0, 0, tmpfs)}
        self.assertEqual(gd.floor_hold(fss(31.9), 30.0, self.root), {dev: 50 * gd.GIB})  # floor 30 + margin 20
        self.assertEqual(gd.floor_hold(fss(32.0), 30.0, self.root), {})  # the gate has let go
        self.assertEqual(gd.floor_hold(fss(10.0), 0.0, self.root), {})  # no runner floor
        self.assertEqual(gd.floor_hold(fss(10.0, tmpfs=True), 30.0, self.root), {})

    def test_runner_git_garbage_younger_than_an_hour_or_open_stays(self) -> None:
        home, fams = self._runner_checkouts()
        co = home / "actions-runner-glaeda/_work/cmux/cmux"
        os.utime(co / ".git/objects/pack/tmp_pack_Ab12Cd")  # a fetch is writing it now
        held = home / "actions-runner-glaeda-2/_work/cmux/cmux/.git/objects/pack/tmp_pack_Ab12Cd"
        gd.process_evidence = lambda: ([str(held)], "git index-pack --stdin\n")
        garbage = [f for f in fams if f.id == gd.RUNNER_GIT_GARBAGE_FAMILY]
        v = {str(Path(i.path).relative_to(home)): i.verdict for i in gd.survey(garbage, 6, 0)}
        self.assertEqual(v["actions-runner-glaeda/_work/cmux/cmux/.git/objects/pack/tmp_pack_Ab12Cd"], "recent")
        self.assertEqual(v["actions-runner-glaeda/_work/cmux/cmux/.git/objects/ab/tmp_obj_Xy98Zw"], "reclaimable")
        self.assertEqual(v["actions-runner-glaeda-2/_work/cmux/cmux/.git/objects/pack/tmp_pack_Ab12Cd"], "in-use")

    def test_apply_evicts_idle_runner_output_oldest_first_and_rechecks_the_runner(self) -> None:
        home, fams = self._runner_checkouts()
        idle = home / "actions-runner-glaeda/_work/cmux/cmux"
        other = home / "actions-runner-glaeda-2/_work/cmux/cmux"
        self._age(idle / "cmux-tui/target", hours=30)
        self._age(other / "Packages/Shared/.build", hours=10)
        gd.process_evidence = lambda: ([], "")
        with mock.patch.object(gd, "ACTIVITY_MODE", "pressure"):
            items = gd.survey(fams, 6, 0)
            receipt = self.receipt()
            # a job starts on runner 2 after the survey: the fresh ps before each deletion shows its Worker
            worker = f"{home}/actions-runner-glaeda-2/bin.2.330.0/Runner.Worker spawnclient 1 2\n"
            with mock.patch.object(gd, "command_lines", return_value=worker):
                gd.apply(items, {f.id: f for f in fams}, receipt, None, 6, all_families=fams)
        recs = [json.loads(line) for line in receipt.read_text().splitlines()]
        build = [r for r in recs if r["family"] == gd.RUNNER_BUILD_FAMILY]
        # git garbage goes before any build output that costs a rebuild
        self.assertEqual({r["family"] for r in recs[:4]}, {gd.RUNNER_GIT_GARBAGE_FAMILY})
        self.assertEqual(build[0]["path"], str(idle / "cmux-tui/target"))  # least recently modified first
        self.assertEqual(build[1]["path"], str(other / "Packages/Shared/.build"))
        self.assertEqual({r["outcome"] for r in build if r["path"].startswith(str(other) + "/")},
                         {"changed:in-use"})
        self.assertEqual({r["outcome"] for r in build if r["path"].startswith(str(idle) + "/")}, {"reclaimed"})
        self.assertFalse((idle / "cmux-tui/target").exists())
        self.assertFalse((idle / "Packages/macOS/CmuxNext/.build").exists())
        self.assertTrue((other / "cmux-tui/target").exists())
        self.assertFalse((other / ".git/objects/pack/tmp_pack_Ab12Cd").exists())
        self.assertTrue((other / ".git/objects/pack/pack-1234.pack").exists())
        self.assertTrue((idle / "Sources/target").exists())
        self.assertTrue((idle / ".git").is_dir())
        self.assertFalse((idle.parent / ".glaeda-state/ios-e2e-mac-derived-data").exists())
        self.assertTrue((idle.parent / ".glaeda-state/SourcePackages").exists())
        self.assertTrue((idle.parent / ".glaeda-state/cas").exists())
        self.assertTrue((other.parent / ".glaeda-state/ios-e2e-mac-derived-data").exists())

    def test_runner_git_garbage_stays_inside_the_runners_work(self) -> None:
        home, _ = self._runner_checkouts()
        outside = self.root / "main-clone/.git"
        (outside / "objects/pack").mkdir(parents=True)
        (outside / "objects/pack/tmp_pack_Zz12Yy").write_bytes(b"\0" * 1024)
        co = home / "actions-runner-glaeda/_work/cmux/cmux"
        shutil.rmtree(co / ".git")
        (co / ".git").symlink_to(outside)  # a .git that points at a clone outside _work
        other = home / "actions-runner-glaeda-2/_work/cmux/cmux/.git/objects"
        shutil.rmtree(other / "pack")
        (other / "pack").symlink_to(outside / "objects/pack")
        found = [str(p) for w in ("actions-runner-glaeda", "actions-runner-glaeda-2")
                 for p in gd.runner_git_garbage(home / w / "_work")]
        self.assertFalse([f for f in found if "Zz12Yy" in f or f.startswith(str(co))], found)
        self.assertIn(str(other / "ab/tmp_obj_Xy98Zw"), found)

    def test_runner_job_live_matches_either_spelling_of_the_runner_path(self) -> None:
        real = self.root / "real-home/actions-runner-glaeda"
        real.mkdir(parents=True)
        (self.root / "home-link").symlink_to(self.root / "real-home")
        runner = self.root / "home-link/actions-runner-glaeda"
        self.assertIn("running a job", gd.runner_job_live(runner, f"{real.resolve()}/bin/Runner.Worker spawnclient 1 2\n"))
        self.assertIn("running a job", gd.runner_job_live(runner, f"{runner}/bin/Runner.Worker spawnclient 1 2\n"))
        self.assertEqual(gd.runner_job_live(runner, f"{runner}-2/bin/Runner.Worker spawnclient 1 2\n"), "")

    def _floor_disk(self, free_gib: float, freed_gib: float = 10.0):
        """A fake HOME volume at free_gib; each removal frees freed_gib. Returns (patches, removed paths)."""
        state = {"free": int(free_gib * gd.GIB)}
        removed: list[str] = []
        real_remove = gd.remove

        def remove(path, after_rename=None):
            real_remove(path, after_rename)
            removed.append(str(path))
            state["free"] += int(freed_gib * gd.GIB)
        return (mock.patch.object(gd, "free_bytes", side_effect=lambda _p: (state["free"], 460 * gd.GIB)),
                mock.patch.object(gd, "remove", side_effect=remove)), removed

    def _idle_checkouts(self, hours: dict[str, float]) -> tuple[Path, dict[str, Path]]:
        home, _ = self._runner_checkouts()
        cos = {}
        for name, age in hours.items():
            runner = home / name
            if not runner.exists():
                (runner / "glaeda-hooks").mkdir(parents=True)
                (runner / ".runner").write_text("{}")
                (runner / "_work/cmux/cmux/.git").mkdir(parents=True)
            (runner / "_diag").mkdir(exist_ok=True)
            (runner / "_diag/Worker_20261021-000000-utc.log").write_text("job")
            self._age(runner, hours=age)
            cos[name] = runner / "_work/cmux/cmux"
        return home, cos

    def test_idle_runner_checkouts_stay_while_above_the_floor(self) -> None:
        home, cos = self._idle_checkouts({"actions-runner-glaeda": 5, "actions-runner-glaeda-2": 5})
        patches, removed = self._floor_disk(32.5)  # over the gate's resume mark (floor + GATE_DISK_RESUME_GIB)
        with patches[0], patches[1]:
            self.assertEqual(gd.evict_idle_runner_checkouts(self.receipt(), 30.0, home), 0)
            self.assertEqual(gd.evict_idle_runner_checkouts(self.receipt(), 0.0, home), 0)  # no runner floor
        self.assertEqual(removed, [])
        self.assertTrue(all(c.is_dir() for c in cos.values()))

    def test_idle_runner_checkouts_go_between_the_floor_and_the_gates_resume_mark(self) -> None:
        # the gate holds until floor + 2 GiB; at 31 GiB it still holds, so the last resort still acts
        home, cos = self._idle_checkouts({"actions-runner-glaeda": 5, "actions-runner-glaeda-2": 5})
        patches, removed = self._floor_disk(31.0)
        with patches[0], patches[1], contextlib.redirect_stdout(io.StringIO()):
            self.assertGreater(gd.evict_idle_runner_checkouts(self.receipt(), 30.0, home), 0)
        self.assertTrue(removed)

    def test_idle_runner_checkouts_go_least_recently_used_first_until_the_target(self) -> None:
        home, cos = self._idle_checkouts({"actions-runner-glaeda": 3, "actions-runner-glaeda-2": 9,
                                          "actions-runner-glaeda-3": 6})
        patches, removed = self._floor_disk(25.0, freed_gib=15.0)  # floor 30 + target_margin_gib 20 = 50
        receipt = self.receipt()
        with patches[0], patches[1], contextlib.redirect_stdout(io.StringIO()):
            released = gd.evict_idle_runner_checkouts(receipt, 30.0, home)
        # 25 -> 40 -> 55: two checkouts, oldest use first; the most recently used one stays
        self.assertEqual(removed, [str(cos["actions-runner-glaeda-2"]), str(cos["actions-runner-glaeda-3"])])
        self.assertEqual(released, 30 * gd.GIB)
        self.assertTrue(cos["actions-runner-glaeda"].is_dir())
        recs = [json.loads(line) for line in receipt.read_text().splitlines()]
        self.assertEqual({(r["family"], r["outcome"]) for r in recs}, {(gd.RUNNER_IDLE_CHECKOUT_FAMILY, "reclaimed")})
        # only the checkout goes: the workspace's .glaeda-state caches and the runner's _tool/_temp stay
        work = home / "actions-runner-glaeda-2/_work"
        self.assertTrue((work / "cmux/.glaeda-state/SourcePackages").is_dir())
        self.assertTrue((work / "_temp/cmux-derived-data-tests-1").is_dir())

    def test_idle_runner_checkouts_of_busy_runners_go_between_jobs(self) -> None:
        """cmuxs-mac-mini-6, 2026-10-21: light jobs kept every runner busy at least hourly, so under the floor
        no checkout was ever an hour quiet; runners with no job, last used 23-32 minutes before, held 20 GiB."""
        home, cos = self._idle_checkouts({"actions-runner-glaeda": 0.54, "actions-runner-glaeda-2": 0.39,
                                          "actions-runner-glaeda-3": 0.06})
        gd.process_evidence = lambda: ([], "")
        patches, removed = self._floor_disk(10.0, freed_gib=6.5)
        receipt = self.receipt()
        with patches[0], patches[1], contextlib.redirect_stdout(io.StringIO()):
            self.assertGreater(gd.evict_idle_runner_checkouts(receipt, 30.0, home), 0)
        self.assertEqual(removed, [str(cos["actions-runner-glaeda"]), str(cos["actions-runner-glaeda-2"])])
        self.assertTrue(cos["actions-runner-glaeda-3"].is_dir(), "used minutes ago: its job may be finishing")

    def test_idle_runner_checkout_vetoes(self) -> None:
        home, cos = self._idle_checkouts({"actions-runner-glaeda": 5, "actions-runner-glaeda-2": 5,
                                          "actions-runner-glaeda-3": 5, "actions-runner-glaeda-4": 0.1})
        live = home / "actions-runner-glaeda"
        opened = cos["actions-runner-glaeda-2"] / "Sources/x.swift"
        named = cos["actions-runner-glaeda-3"]
        cmds = (f"{live.resolve()}/bin/Runner.Worker spawnclient 1 2\n"  # a job, by the resolved spelling
                f"/usr/bin/xcodebuild -project {named}/cmux.xcodeproj\n")
        gd.process_evidence = lambda: ([str(opened)], cmds)
        patches, removed = self._floor_disk(10.0)
        receipt = self.receipt()
        with patches[0], patches[1]:
            self.assertEqual(gd.evict_idle_runner_checkouts(receipt, 30.0, home), 0)
        self.assertEqual(removed, [])  # the runner used within the quiet window is not even a candidate
        why = {Path(r["path"]).parts[-4]: r["reasons"][0] for r in map(json.loads, receipt.read_text().splitlines())}
        self.assertIn("running a job", why["actions-runner-glaeda"])
        self.assertIn("open or a working directory", why["actions-runner-glaeda-2"])
        self.assertIn("named by a running command", why["actions-runner-glaeda-3"])
        self.assertNotIn("actions-runner-glaeda-4", why)
        self.assertTrue(all(c.is_dir() for c in cos.values()))

    def test_idle_runner_checkout_rechecks_before_each_delete_and_refuses_without_evidence(self) -> None:
        home, cos = self._idle_checkouts({"actions-runner-glaeda": 5, "actions-runner-glaeda-2": 4})
        calls = []

        def evidence():
            calls.append(1)
            if len(calls) == 1:
                return [], ""
            # a job started on runner 2 after the first deletion
            return [], f"{home}/actions-runner-glaeda-2/bin/Runner.Worker spawnclient 1 2\n"
        gd.process_evidence = evidence
        patches, removed = self._floor_disk(10.0, freed_gib=5.0)
        with patches[0], patches[1], contextlib.redirect_stdout(io.StringIO()):
            gd.evict_idle_runner_checkouts(self.receipt(), 30.0, home)
        self.assertEqual(removed, [str(cos["actions-runner-glaeda"])])
        self.assertTrue(cos["actions-runner-glaeda-2"].is_dir())

        def no_evidence():
            raise gd.NoEvidence("lsof failed")
        gd.process_evidence = no_evidence
        patches, removed = self._floor_disk(10.0)
        with patches[0], patches[1], contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gd.evict_idle_runner_checkouts(self.receipt(), 30.0, home), 0)
        self.assertEqual(removed, [])

    def test_idle_runner_checkout_never_follows_symlinks_or_leaves_the_checkout(self) -> None:
        home, cos = self._idle_checkouts({"actions-runner-glaeda": 5, "actions-runner-glaeda-2": 5,
                                          "actions-runner-glaeda-3": 5})
        outside = self.root / "main-clone"
        (outside / ".git").mkdir(parents=True)
        linked = cos["actions-runner-glaeda-2"]
        shutil.rmtree(linked)
        linked.symlink_to(outside)  # a checkout that is a symlink to a clone outside _work
        work3 = home / "actions-runner-glaeda-3/_work"
        shutil.rmtree(work3)
        work3.symlink_to(self.root / "main-clone")  # a symlinked _work
        other = home / "actions-runner-glaeda/_work/cmux/other"  # a checkout outside the default layout
        (other / ".git").mkdir(parents=True)
        for keep in ("_tool/node", "_actions/actions/checkout"):
            make(home / "actions-runner-glaeda/_work" / keep)
        self._age(home / "actions-runner-glaeda", hours=5)
        patches, removed = self._floor_disk(10.0)
        with patches[0], patches[1], contextlib.redirect_stdout(io.StringIO()):
            gd.evict_idle_runner_checkouts(self.receipt(), 30.0, home)
        self.assertEqual(removed, [str(cos["actions-runner-glaeda"])])
        self.assertTrue((outside / ".git").is_dir())
        self.assertTrue(other.is_dir())
        self.assertTrue((home / "actions-runner-glaeda/_work/_tool/node").is_dir())
        self.assertTrue((home / "actions-runner-glaeda/_work/_actions/actions/checkout").is_dir())

    def test_idle_runner_checkout_used_since_the_survey_stays(self) -> None:
        home, cos = self._idle_checkouts({"actions-runner-glaeda": 5, "actions-runner-glaeda-2": 5})

        def evidence():  # a job's checkout step rewrites the index between the survey and the delete
            for co in cos.values():
                os.utime(co / ".git")
            return [], ""
        gd.process_evidence = evidence
        patches, removed = self._floor_disk(10.0)
        receipt = self.receipt()
        with patches[0], patches[1]:
            gd.evict_idle_runner_checkouts(receipt, 30.0, home)
        self.assertEqual(removed, [])
        self.assertEqual({json.loads(line)["outcome"] for line in receipt.read_text().splitlines()},
                         {"changed:modified"})

    def test_idle_runner_checkout_pass_runs_only_in_a_pressure_apply(self) -> None:
        dev = gd.HOME.stat().st_dev
        under = {dev: gd.Fs(dev, "/", 10 * gd.GIB, 460 * gd.GIB, 34 * gd.GIB, 50 * gd.GIB)}
        receipt = ["--receipt", os.fspath(self.receipt())]
        with mock.patch.object(gd, "EVICT_LOCK", self.root / "evict.lock"), \
                mock.patch.object(gd, "EVICT_OWNER", self.root / "evict.owner.json"), \
                mock.patch.object(gd, "MEASURE_LOCK", self.root / "measure.lock"), \
                mock.patch.object(gd, "HEALTH_SUMMARY", self.root / "summary.json"), \
                mock.patch.object(gd, "default_families", return_value=[]), \
                mock.patch.object(gd, "runner_floor_gib", return_value=30.0), \
                mock.patch.object(gd, "filesystems", return_value=under), \
                mock.patch.object(gd, "survey", return_value=[]), \
                mock.patch.object(gd, "apply", return_value=0), \
                mock.patch.object(gd, "cleanup_paused_runner_work", return_value=0), \
                mock.patch.object(gd, "notify_disk"), \
                mock.patch.object(gd, "evict_idle_runner_checkouts", return_value=0) as last, \
                contextlib.redirect_stdout(io.StringIO()):
            gd.main(["--apply", "--no-snapshot", "--top", "0", *receipt])
            last.assert_not_called()
            gd.main(["--pressure", "--no-snapshot", "--top", "0", *receipt])  # a report
            last.assert_not_called()
            gd.main(["--pressure", "--apply", "--no-snapshot", "--top", "0", *receipt])
            last.assert_called_once()
            self.assertEqual(last.call_args.args[1], 30.0)

    def _tmp_residue(self) -> tuple[Path, dict[str, Path]]:
        """A fake $TMPDIR: named test scratch, old and new, beside entries the reaper must never take."""
        tmp = self.root / "T"
        e: dict[str, Path] = {}
        e["socket"] = tmp / "cmux-cli-socket-00D670AE-52D1-4CCE-88A3-08D9A0C1851D"
        (e["socket"] / "cmux DEV.app/Contents/MacOS").mkdir(parents=True)
        (e["socket"] / "cmux DEV.app/Contents/MacOS/cmux").write_bytes(b"\0" * 4096)
        e["git"] = tmp / "git-pr-003CE1E9-4204-4B78-8FB7-87EFC23D81A3"
        (e["git"] / "repo/.git").mkdir(parents=True)
        e["foundation"] = tmp / "TemporaryDirectory.000X0T"
        e["foundation"].mkdir()
        (e["foundation"] / ".keep-directory").write_text("")
        e["file"] = tmp / "cmux-shortcut-routing-0008E778-433F-46C4-89E2-78F8075D2C08.json"
        e["file"].write_text("{}")
        e["recent"] = tmp / "cmux-cli-socket-0171FEFD-85E4-43A4-B516-55787B935041"
        e["recent"].mkdir()
        e["inner-recent"] = tmp / "git-stage-00560C4B-4E10-4993-BE64-00845DEB616F"
        (e["inner-recent"] / "a/b").mkdir(parents=True)
        e["unknown"] = tmp / "notes-from-an-agent"
        e["unknown"].mkdir()
        e["apple"] = tmp / "com.apple.WebKit.Networking"
        e["apple"].mkdir()
        e["dash"] = tmp / "--git-pr-0358FC19-C6D4-4553"
        e["dash"].mkdir()
        outside = self.root / "outside-clone"
        (outside / ".git").mkdir(parents=True)
        e["link"] = tmp / "git-hunk-04049B5D-F71E-44B9-B596-0356BB38F98B"
        e["link"].symlink_to(outside)
        for name, path in e.items():
            if name != "recent":
                self._age(path if not path.is_symlink() else outside, hours=72)
        (e["inner-recent"] / "a/b/fresh").write_text("still in use")  # an old top with a fresh file inside
        os.utime(e["inner-recent"], (time.time() - 72 * 3600,) * 2)
        os.utime(e["recent"], None)
        os.utime(e["link"], (time.time() - 72 * 3600,) * 2, follow_symlinks=False)
        return tmp, e

    def test_tmpdir_test_residue_reaps_only_old_named_test_scratch(self) -> None:
        tmp, e = self._tmp_residue()
        receipt = self.receipt()
        with contextlib.redirect_stdout(io.StringIO()):
            released = gd.reap_tmpdir_test_residue(receipt, tmp)
        gone = {n for n, p in e.items() if not os.path.lexists(p)}
        self.assertEqual(gone, {"socket", "git", "foundation", "file"})
        self.assertGreater(released, 0)
        self.assertTrue((self.root / "outside-clone/.git").is_dir())  # a symlinked entry is never followed
        self.assertTrue(tmp.is_dir())
        self.assertEqual(sorted(p.name for p in tmp.iterdir() if p.name.startswith(gd.DELETING_PREFIX)), [])
        rec = json.loads(receipt.read_text())
        self.assertEqual((rec["family"], rec["outcome"], rec["items"]), (gd.TMP_TEST_RESIDUE_FAMILY, "reclaimed", 4))
        self.assertEqual(rec["by_prefix"], {"cmux-cli-socket": 1, "git-pr": 1, "TemporaryDirectory": 1,
                                            "cmux-shortcut-routing": 1})
        self.assertEqual(rec["kept"], {"a symlink": 1, "changed within 48h": 1})  # recent tops are never walked

    def test_tmpdir_test_residue_vetoes_open_and_named_entries(self) -> None:
        tmp, e = self._tmp_residue()
        opened = e["socket"] / "cmux DEV.app/Contents/MacOS/cmux"
        gd.process_evidence = lambda: ([str(opened)], f"/usr/bin/git -C {e['git']}/repo status\n")
        receipt = self.receipt()
        with contextlib.redirect_stdout(io.StringIO()):
            gd.reap_tmpdir_test_residue(receipt, tmp)
        self.assertTrue(e["socket"].is_dir())
        self.assertTrue(e["git"].is_dir())
        self.assertFalse(e["foundation"].exists())
        kept = json.loads(receipt.read_text())["kept"]
        self.assertEqual(kept["open or a working directory of a process"], 1)
        self.assertEqual(kept["named by a running command"], 1)

    def test_tmpdir_test_residue_fails_closed(self) -> None:
        tmp, e = self._tmp_residue()

        def no_evidence():
            raise gd.NoEvidence("lsof failed (exit 1)")
        gd.process_evidence = no_evidence
        receipt = self.receipt()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gd.reap_tmpdir_test_residue(receipt, tmp), 0)
        self.assertTrue(all(os.path.lexists(p) for p in e.values()))
        self.assertIn("no process evidence", json.loads(receipt.read_text())["stopped"])
        # a tree too big to walk within the budget is not proven idle
        gd.process_evidence = lambda: ([], "")
        with mock.patch.object(gd, "TMP_TEST_RESIDUE_WALK_BUDGET", 1), contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(gd.tree_newest(e["socket"], 1))
            gd.reap_tmpdir_test_residue(self.receipt(), tmp)
        self.assertTrue(e["socket"].is_dir())  # 4 entries below it, budget 1
        self.assertFalse(e["file"].exists())  # a plain file needs no walk
        # a busy host halts the sweep before anything goes
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gd.reap_tmpdir_test_residue(self.receipt(), tmp, halt=lambda: "a CI job runs"), 0)
        self.assertTrue(e["socket"].is_dir())

    def test_tmpdir_test_residue_rereads_evidence_during_a_pass(self) -> None:
        tmp, e = self._tmp_residue()
        calls = []

        def evidence():
            calls.append(1)  # after the first deletion, a test starts in the git scratch
            return ([], "") if len(calls) == 1 else ([str(e["git"] / "repo")], "")
        gd.process_evidence = evidence
        with mock.patch.object(gd, "TMP_TEST_RESIDUE_EVIDENCE_S", -1.0), contextlib.redirect_stdout(io.StringIO()):
            gd.reap_tmpdir_test_residue(self.receipt(), tmp)
        self.assertGreater(len(calls), 1)
        self.assertTrue(e["git"].is_dir())
        self.assertFalse(e["socket"].exists())  # sorted first: TemporaryDirectory, cmux-cli-socket, ...

    def test_tmpdir_test_residue_runs_on_every_apply_but_not_a_report(self) -> None:
        dev = gd.HOME.stat().st_dev
        fss = {dev: gd.Fs(dev, "/", 300 * gd.GIB, 460 * gd.GIB, 34 * gd.GIB, 50 * gd.GIB)}
        receipt = ["--receipt", os.fspath(self.receipt())]
        with mock.patch.object(gd, "EVICT_LOCK", self.root / "evict.lock"), \
                mock.patch.object(gd, "EVICT_OWNER", self.root / "evict.owner.json"), \
                mock.patch.object(gd, "HEALTH_SUMMARY", self.root / "summary.json"), \
                mock.patch.object(gd, "default_families", return_value=[]), \
                mock.patch.object(gd, "filesystems", return_value=fss), \
                mock.patch.object(gd, "survey", return_value=[]), \
                mock.patch.object(gd, "apply", return_value=0), \
                mock.patch.object(gd, "cleanup_paused_runner_work", return_value=0), \
                mock.patch.object(gd, "notify_disk"), \
                mock.patch.object(gd, "reap_tmpdir_test_residue", return_value=0) as reap, \
                contextlib.redirect_stdout(io.StringIO()):
            gd.main(["--no-snapshot", "--top", "0", *receipt])  # a report
            reap.assert_not_called()
            gd.main(["--apply", "--no-snapshot", "--top", "0", "--family", "user-tmp", *receipt])
            reap.assert_not_called()
            gd.main(["--apply", "--no-snapshot", "--top", "0", *receipt])  # not under pressure: still runs
            reap.assert_called_once()

    def test_tmpdir_test_residue_walk_stops_at_the_deadline_and_at_mounts(self) -> None:
        tmp, e = self._tmp_residue()
        self.assertIsNotNone(gd.tree_newest(e["socket"]))
        self.assertIsNone(gd.tree_newest(e["socket"], deadline=time.time() - 1))
        real_scandir = os.scandir

        class Other:  # an entry below the scratch that reports another device
            def __init__(self, entry):
                self.entry, self.name, self.path = entry, entry.name, entry.path

            def stat(self, follow_symlinks=True):
                st = self.entry.stat(follow_symlinks=follow_symlinks)
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, *tuple(st)[3:]))

        @contextlib.contextmanager
        def scandir(path):
            with real_scandir(path) as it:
                yield [Other(x) for x in it]
        with mock.patch.object(gd.os, "scandir", scandir):
            self.assertIsNone(gd.tree_newest(e["socket"]))

    def test_tmpdir_test_residue_main_apply_reaps_the_user_tmp(self) -> None:
        tmp, e = self._tmp_residue()
        dev = gd.HOME.stat().st_dev
        fss = {dev: gd.Fs(dev, "/", 300 * gd.GIB, 460 * gd.GIB, 34 * gd.GIB, 50 * gd.GIB)}
        with mock.patch.object(gd, "EVICT_LOCK", self.root / "evict.lock"), \
                mock.patch.object(gd, "EVICT_OWNER", self.root / "evict.owner.json"), \
                mock.patch.object(gd, "HEALTH_SUMMARY", self.root / "summary.json"), \
                mock.patch.object(gd, "default_families", return_value=[]), \
                mock.patch.object(gd, "filesystems", return_value=fss), \
                mock.patch.object(gd, "survey", return_value=[]), \
                mock.patch.object(gd, "apply", return_value=0), \
                mock.patch.object(gd, "cleanup_paused_runner_work", return_value=0), \
                mock.patch.object(gd, "notify_disk"), \
                mock.patch.object(gd, "DARWIN", True), \
                mock.patch.object(gd, "darwin_user_tmp", return_value=tmp), \
                mock.patch.object(gd, "reap_tmpdir_test_residue", _reap_tmpdir_test_residue), \
                contextlib.redirect_stdout(io.StringIO()):
            gd.main(["--apply", "--no-snapshot", "--top", "0", "--receipt", os.fspath(self.receipt())])
        self.assertFalse(e["socket"].exists())
        self.assertTrue(e["recent"].is_dir())

    def test_job_busy_ignores_wide_runner_build_trees(self) -> None:
        home, fams = self._runner_checkouts()
        target = home / "actions-runner-glaeda/_work/cmux/cmux/cmux-tui/target"
        build = next(f for f in fams if f.id == gd.RUNNER_BUILD_FAMILY)
        with mock.patch.object(gd, "cmux_active", return_value=True):
            self.assertFalse(gd.job_busy(build, target))

    def _home_clone(self, packs: int = 3) -> tuple[Path, Path, Path]:
        """A fake runner home with ~/cmux (packs packs) and a linked work tree ~/cmux-wt beside it."""
        home = self.root / "home"
        clone = home / "cmux"
        clone.mkdir(parents=True)
        self._git("init", "-q", "-b", "main", os.fspath(clone))
        for n in range(packs):  # each repack without -a writes the new loose objects to a pack of their own
            (clone / f"f{n}").write_text(str(n) * 100)
            self._git("-C", os.fspath(clone), "add", f"f{n}")
            self._git("-C", os.fspath(clone), "commit", "-q", "-m", f"c{n}")
            self._git("-C", os.fspath(clone), "repack", "-q")
        wt = home / "cmux-wt"
        self._git("-C", os.fspath(clone), "worktree", "add", "-q", "-b", "wt", os.fspath(wt))
        runner = home / "actions-runner-glaeda"  # a CI runner account's home
        (runner / "glaeda-hooks").mkdir(parents=True, exist_ok=True)
        (runner / ".runner").write_text("{}")
        self._age(home, hours=5)
        self.assertEqual(gd.git_pack_count(clone / ".git"), packs)
        return home, clone, wt

    @contextlib.contextmanager
    def _gc_log(self):
        with mock.patch.object(gd, "HOME_CLONE_GC_LOG", self.root / "gc.log"), \
                mock.patch.object(gd, "HOME_CLONE_GC_FAILED", self.root / "gc-failed.json"):
            yield

    def _runner_checkout_packs(self, packs: int = 3) -> tuple[Path, Path, Path]:
        """A fake runner home whose actions-runner-glaeda-2 has _work/cmux/cmux with packs packs."""
        home = self.root / "home"
        runner = home / "actions-runner-glaeda-2"
        (runner / "glaeda-hooks").mkdir(parents=True)
        (runner / ".runner").write_text("{}")
        tree = runner / "_work/cmux/cmux"
        tree.mkdir(parents=True)
        self._git("init", "-q", "-b", "main", os.fspath(tree))
        self._git("-C", os.fspath(tree), "config", "gc.auto", "0")  # as cmux's nightly.yml leaves it
        for n in range(packs):
            (tree / f"f{n}").write_text(str(n) * 100)
            self._git("-C", os.fspath(tree), "add", f"f{n}")
            self._git("-C", os.fspath(tree), "commit", "-q", "-m", f"c{n}")
            self._git("-C", os.fspath(tree), "repack", "-q")
        self._age(home, hours=5)
        self.assertEqual(gd.git_pack_count(tree / ".git"), packs)
        return home, runner, tree

    def test_runner_checkout_repack_folds_a_checkout_with_many_packs_and_loses_nothing(self) -> None:
        # 2026-10-21: gc.auto=0 from nightly.yml left 3935 packs in one runner's checkout; no pass repacked it
        home, _, tree = self._runner_checkout_packs(3)
        heads = self._git("-C", os.fspath(tree), "rev-list", "--all").split()
        # a fetch's objects are unreachable until it moves its refs: a packed object no ref names must stay
        (self.root / "in-flight").write_text("in flight\n")
        loose = self._git("-C", os.fspath(tree), "hash-object", "-w", os.fspath(self.root / "in-flight")).strip()
        subprocess.run(["git", "-C", os.fspath(tree), "pack-objects", "-q", ".git/objects/pack/pack"],
                       input=loose + "\n", text=True, check=True, capture_output=True)
        (tree / ".git/objects" / loose[:2] / loose[2:]).unlink()  # only in that pack now, as a fetch leaves it
        self._age(home, hours=5)
        receipt = self.receipt()
        gd.process_evidence = lambda: ([], "")
        with mock.patch.object(gd, "RUNNER_CHECKOUT_REPACK_PACKS", 4), self._gc_log(), \
                contextlib.redirect_stdout(io.StringIO()):
            gd.repack_runner_checkouts(receipt, home)
        self.assertEqual(gd.git_pack_count(tree / ".git"), 1)
        rec = json.loads(receipt.read_text())
        self.assertEqual((rec["family"], rec["outcome"], rec["packs"], rec["packs_after"]),
                         (gd.RUNNER_CHECKOUT_REPACK_FAMILY, "reclaimed", 4, 1))
        self.assertEqual(self._git("-C", os.fspath(tree), "cat-file", "-t", loose).strip(), "blob")
        self.assertEqual(self._git("-C", os.fspath(tree), "rev-list", "--all").split(), heads)
        self._git("-C", os.fspath(tree), "fsck", "--no-progress")

    def test_runner_checkout_repack_waits_for_the_runners_job_and_the_threshold(self) -> None:
        home, runner, tree = self._runner_checkout_packs(3)
        receipt = self.receipt()
        gd.process_evidence = lambda: ([], f"{runner}/bin/Runner.Worker spawnclient 107 110\n")
        with mock.patch.object(gd, "RUNNER_CHECKOUT_REPACK_PACKS", 3), self._gc_log():
            self.assertEqual(gd.repack_runner_checkouts(receipt, home), 0)
        self.assertEqual(gd.git_pack_count(tree / ".git"), 3)
        self.assertIn("is running a job", json.loads(receipt.read_text())["reasons"][0])
        receipt.unlink()
        gd.process_evidence = lambda: ([], "")
        with mock.patch.object(gd, "RUNNER_CHECKOUT_REPACK_PACKS", 4), self._gc_log():
            self.assertEqual(gd.repack_runner_checkouts(receipt, home), 0)
        self.assertEqual(gd.git_pack_count(tree / ".git"), 3)
        self.assertFalse(receipt.exists())

    def test_home_clones_are_clones_directly_under_home(self) -> None:
        home, clone, wt = self._home_clone(1)
        (home / "plain").mkdir()
        (home / "link-to-clone").symlink_to(clone)
        runner = home / "actions-runner-glaeda/_work/cmux/cmux"
        runner.mkdir(parents=True)
        self._git("init", "-q", os.fspath(runner))
        self._git("-C", os.fspath(runner), "commit", "-q", "--allow-empty", "-m", "x")
        self._git("-C", os.fspath(runner), "worktree", "add", "-q", "-b", "o", os.fspath(home / "runner-wt"))
        # ~/cmux once, also through its linked work tree; never the symlink or a runner checkout's tree
        self.assertEqual(gd.home_clones(home), [(clone, clone / ".git")])
        self.assertEqual(gd.linked_work_trees(clone / ".git"), [wt])

    def test_home_clone_gc_repacks_the_clone_with_many_packs(self) -> None:
        home, clone, wt = self._home_clone(3)
        receipt = self.receipt()
        with mock.patch.object(gd, "HOME_CLONE_GC_PACKS", 3), self._gc_log(), contextlib.redirect_stdout(io.StringIO()):
            gd.gc_home_clones(receipt, home)
        self.assertEqual(gd.git_pack_count(clone / ".git"), 1)
        rec = json.loads(receipt.read_text())
        self.assertEqual((rec["family"], rec["outcome"], rec["verdict"], rec["packs"], rec["packs_after"]),
                         (gd.HOME_CLONE_GC_FAMILY, "reclaimed", "reclaimable", 3, 1))
        self.assertEqual(self._git("-C", os.fspath(wt), "rev-parse", "--abbrev-ref", "HEAD").strip(), "wt")
        self._git("-C", os.fspath(clone), "fsck", "--no-progress")  # nothing reachable was lost

    def test_home_clone_gc_below_the_pack_threshold_or_without_runners_does_nothing(self) -> None:
        home, clone, _ = self._home_clone(2)
        receipt = self.receipt()
        with mock.patch.object(gd, "HOME_CLONE_GC_PACKS", 3), self._gc_log():
            self.assertEqual(gd.gc_home_clones(receipt, home), 0)
        self.assertEqual(gd.git_pack_count(clone / ".git"), 2)
        self.assertFalse(receipt.exists())
        shutil.rmtree(home / "actions-runner-glaeda")  # a person's or a dev box's home: never
        with mock.patch.object(gd, "HOME_CLONE_GC_PACKS", 2), self._gc_log():
            self.assertEqual(gd.gc_home_clones(receipt, home), 0)
        self.assertEqual(gd.git_pack_count(clone / ".git"), 2)
        self.assertFalse(receipt.exists())

    def test_home_clone_gc_vetoes(self) -> None:
        cases = {
            "a git operation holds gc.pid": lambda c, w: (c / ".git/gc.pid").write_text("1 host"),
            "a git operation holds index.lock": lambda c, w: (c / ".git/worktrees/cmux-wt/index.lock").write_text(""),
            "git activity within 1h": lambda c, w: (c / ".git/FETCH_HEAD").write_text("x"),
            "holds refs/heads/feat/x.lock": lambda c, w: (
                (c / ".git/refs/heads/feat").mkdir(parents=True), (c / ".git/refs/heads/feat/x.lock").write_text(""),
                os.utime(c / ".git/refs/heads/feat/x.lock", (time.time() - 7200,) * 2),
                os.utime(c / ".git/refs/heads/feat", (time.time() - 7200,) * 2)),
            "writing a pack": lambda c, w: (c / ".git/objects/pack/tmp_pack_abc123").write_text(""),
            "a linked work tree is missing": lambda c, w: shutil.rmtree(w),
            "holds MERGE_HEAD": lambda c, w: (c / ".git/MERGE_HEAD").write_text("0" * 40),
            "holds rebase-merge": lambda c, w: (c / ".git/worktrees/cmux-wt/rebase-merge").mkdir(),
            "open or a working directory": lambda c, w: setattr(
                gd, "process_evidence", lambda: ([os.fspath(w / "f0")], "")),
            "named by a running command": lambda c, w: setattr(
                gd, "process_evidence", lambda: ([], f"git -C {c} fetch origin\n")),
        }
        for reason, make_busy in cases.items():
            with self.subTest(reason):
                shutil.rmtree(self.root / "home", ignore_errors=True)
                gd.process_evidence = lambda: ([], "")
                home, clone, wt = self._home_clone(3)
                make_busy(clone, wt)
                receipt = self.receipt()
                receipt.unlink(missing_ok=True)
                with mock.patch.object(gd, "HOME_CLONE_GC_PACKS", 3), self._gc_log():
                    self.assertEqual(gd.gc_home_clones(receipt, home), 0)
                self.assertEqual(gd.git_pack_count(clone / ".git"), 3)
                self.assertIn(reason, json.loads(receipt.read_text())["reasons"][0])

    def test_home_clone_gc_fails_closed_and_never_kills_a_slow_gc(self) -> None:
        home, clone, _ = self._home_clone(3)

        def no_evidence():
            raise gd.NoEvidence("ps failed (exit 1)")
        gd.process_evidence = no_evidence
        with mock.patch.object(gd, "HOME_CLONE_GC_PACKS", 3), self._gc_log(), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gd.gc_home_clones(self.receipt(), home), 0)
            gd.process_evidence = lambda: ([], "")
            self.assertEqual(gd.gc_home_clones(self.receipt(), home, halt=lambda: "a CI job runs"), 0)
        self.assertEqual(gd.git_pack_count(clone / ".git"), 3)
        # a gc.pid git itself would ignore (12 h old) does not veto
        (clone / ".git/gc.pid").write_text("1 elsewhere")
        os.utime(clone / ".git/gc.pid", (time.time() - 13 * 3600,) * 2)
        self.assertEqual(gd.home_clone_gc_veto(clone, clone / ".git", frozenset(), "", time.time()), "")

        class SlowGc:
            pid = 4242
            killed = False

            def __init__(self, argv, **kw):
                SlowGc.argv, self.kw = argv, kw

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("git gc", timeout)

            def kill(self):
                SlowGc.killed = True
            terminate = kill
        receipt = self.receipt()
        with mock.patch.object(gd, "HOME_CLONE_GC_PACKS", 3), self._gc_log(), \
                mock.patch.object(gd, "git_object_bytes", return_value=0), \
                mock.patch.object(gd.subprocess, "Popen", SlowGc), mock.patch.object(gd.os, "killpg") as killpg:
            gd.gc_home_clones(receipt, home)
        self.assertFalse(SlowGc.killed)
        killpg.assert_not_called()
        rec = json.loads(receipt.read_text())
        self.assertEqual((rec["outcome"], rec["verdict"], rec["pid"]), ("running", "running", 4242))
        self.assertIn("--prune=1.day.ago", SlowGc.argv)
        self.assertIn("gc.worktreePruneExpire=never", SlowGc.argv)

    def test_home_clone_gc_failure_backs_off_for_a_day(self) -> None:
        home, clone, _ = self._home_clone(3)

        class FailingGc:
            pid = 99
            calls = 0

            def __init__(self, argv, **kw):
                FailingGc.calls += 1

            def wait(self, timeout=None):
                return 128
        receipt = self.receipt()
        with mock.patch.object(gd, "HOME_CLONE_GC_PACKS", 3), self._gc_log(), \
                mock.patch.object(gd, "git_object_bytes", return_value=0), \
                mock.patch.object(gd.subprocess, "Popen", FailingGc):
            gd.gc_home_clones(receipt, home)
            gd.gc_home_clones(receipt, home)
        self.assertEqual(FailingGc.calls, 1)
        first, second = (json.loads(line) for line in receipt.read_text().splitlines())
        self.assertEqual((first["outcome"], first["verdict"], first["reasons"][-1]),
                         ("failed:exit-128", "failed", "gc failed:exit-128"))
        self.assertIn("its last gc failed within 24h", second["reasons"][0])

    def test_home_clone_gc_runs_on_pressure_and_sweeps_of_fleet_minis_only(self) -> None:
        dev = gd.HOME.stat().st_dev
        under = {dev: gd.Fs(dev, "/", 10 * gd.GIB, 460 * gd.GIB, 34 * gd.GIB, 50 * gd.GIB)}
        receipt = ["--receipt", os.fspath(self.receipt())]
        order: list[str] = []
        real_release = gd.release_evict_lock

        def release(handle):
            if handle is not None:
                order.append("release")
            real_release(handle)
        with mock.patch.object(gd, "EVICT_LOCK", self.root / "evict.lock"), \
                mock.patch.object(gd, "EVICT_OWNER", self.root / "evict.owner.json"), \
                mock.patch.object(gd, "MEASURE_LOCK", self.root / "measure.lock"), \
                mock.patch.object(gd, "HEALTH_SUMMARY", self.root / "summary.json"), \
                mock.patch.object(gd, "default_families", return_value=[]), \
                mock.patch.object(gd, "filesystems", return_value=under), \
                mock.patch.object(gd, "survey", return_value=[]), \
                mock.patch.object(gd, "apply", return_value=0), \
                mock.patch.object(gd, "cleanup_paused_runner_work", return_value=0), \
                mock.patch.object(gd, "evict_idle_runner_checkouts", return_value=0), \
                mock.patch.object(gd, "notify_disk"), \
                mock.patch.object(gd, "release_evict_lock", side_effect=release), \
                mock.patch.object(gd, "gc_home_clones", side_effect=lambda *a, **k: order.append("gc") or 0) as gc, \
                mock.patch.object(gd, "repack_runner_checkouts", return_value=0) as repack, \
                contextlib.redirect_stdout(io.StringIO()):
            gd.main(["--no-snapshot", "--top", "0", *receipt])  # a report
            gd.main(["--apply", "--no-snapshot", "--top", "0", *receipt])  # neither a sweep nor pressure
            gd.main(["--pressure", "--apply", "--role", "devbox", "--no-snapshot", "--top", "0", *receipt])
            gc.assert_not_called()
            repack.assert_not_called()
            gd.main(["--pressure", "--apply", "--no-snapshot", "--top", "0", *receipt])
            gc.assert_called_once()
            repack.assert_called_once()
        self.assertEqual(order[-2:], ["release", "gc"])  # a long gc never holds the eviction lock

    def test_idle_sweep_windows_follow_reuse(self) -> None:
        ci = self.root / "ci"
        (ci / "seeds").mkdir(parents=True)
        (ci / "seed-source.json").write_text(json.dumps({"prefix": "p-"}))
        for name, age in (("p-a", 1), ("p-b", 2), ("p-c", 12), ("p-d", 30)):
            make(ci / "seeds" / name, age_hours=age)
        make(ci / "pr-builds/pr-1", age_hours=3)
        make(ci / "pr-builds/pr-2", age_hours=7)
        make(ci / "derived-data", age_hours=30)
        fams = gd.idle_families(gd.fleet_families(ci))
        got = {str(Path(i.path).relative_to(ci)): i.verdict for i in gd.survey(fams, gd.IDLE_SWEEP_HOURS, 0)}
        self.assertEqual(got, {"seeds/p-a": "kept", "seeds/p-b": "kept", "seeds/p-c": "reclaimable",
                               "seeds/p-d": "reclaimable", "pr-builds/pr-1": "recent",
                               "pr-builds/pr-2": "recent", "derived-data": "reclaimable"})
        # DerivedData and caches use the 12 h idle window for disposable output.
        make(self.root / "dd/old", age_hours=50)
        make(self.root / "dd/day", age_hours=30)
        fam = gd.Family("xcode-derived-data", self.root / "dd", True, "rebuild")
        got = {Path(i.path).name: i.verdict for i in gd.survey(gd.idle_families([fam]), gd.IDLE_SWEEP_HOURS, 0)}
        self.assertEqual(got, {"old": "reclaimable", "day": "reclaimable"})

    def test_host_busy_sees_jobs_builds_locks_and_reservations(self) -> None:
        fleet = self.root / "fleet"
        fleet.mkdir()
        ps = lambda out: mock.patch.object(gd.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, out, ""))
        with ps("/sbin/launchd\n/usr/bin/python3 x\n"):
            self.assertEqual(gd.host_busy(fleet), "")
            (fleet / "host.lock").touch()
            self.assertEqual(gd.host_busy(fleet), "")
            with (fleet / "host.lock").open() as held:
                fcntl.flock(held, fcntl.LOCK_SH)  # a PR job holds it shared: Runner.Worker says busy
                self.assertEqual(gd.host_busy(fleet), "")
            with (fleet / "host.lock").open() as held:
                fcntl.flock(held, fcntl.LOCK_EX)  # a fleet build
                self.assertIn("host lock", gd.host_busy(fleet))
            marker = {"schema": "glaeda-reservation/v1", "until": int(time.time()) + 60}
            (fleet / "reservation.json").write_text(json.dumps(marker))
            self.assertIn("reserved", gd.host_busy(fleet))
            (fleet / "reservation.json").write_text(json.dumps({**marker, "until": int(time.time()) - 60}))
            self.assertEqual(gd.host_busy(fleet), "")
            (fleet / "reservation.json").write_text(json.dumps({"until": int(time.time()) - 60}))
            self.assertIn("reserved", gd.host_busy(fleet))  # another schema is invalid, so active
            (fleet / "reservation.json").write_text("{")
            self.assertIn("unreadable", gd.host_busy(fleet))
            (fleet / "reservation.json").unlink()
            (fleet / "jobs").mkdir()
            (fleet / "jobs/.DS_Store").touch()
            self.assertEqual(gd.host_busy(fleet), "")
            (fleet / "jobs/abc").mkdir()
            self.assertIn("dev-build worker", gd.host_busy(fleet))
            (fleet / "jobs/abc").rmdir()
        with ps("/Users/cmux/actions-runner-glaeda/bin/Runner.Worker spawnclient 1 2\n"):
            self.assertIn("CI job", gd.host_busy(fleet))
        with ps("/Applications/Xcode 26.app/Contents/Developer/usr/bin/xcodebuild build\n"):
            self.assertIn("build", gd.host_busy(fleet))
        with ps(""):
            self.assertIn("cannot list", gd.host_busy(fleet))

    def test_host_busy_marks_its_no_answer_reasons(self) -> None:
        """glaeda-mini-health tells host_busy's could-not-read reasons (no restart is coming) from a busy host (a short
        wait) by host_busy_unreadable, not by matching text of its own."""
        fleet = self.root / "fleet"
        fleet.mkdir()
        ps = lambda out: mock.patch.object(gd.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, out, ""))
        unreadable, busy = [], []
        with ps(""):
            unreadable.append(gd.host_busy(fleet))
        with ps("/Users/cmux/actions-runner-glaeda/bin/Runner.Worker spawnclient 1 2\n"):
            busy.append(gd.host_busy(fleet))
        with ps("/sbin/launchd\n"):
            (fleet / "jobs").write_text("")  # not a directory: scandir fails
            unreadable.append(gd.host_busy(fleet))
            (fleet / "jobs").unlink()
            (fleet / "jobs").mkdir()
            (fleet / "jobs/abc").mkdir()
            busy.append(gd.host_busy(fleet))
            (fleet / "jobs/abc").rmdir()
            (fleet / "reservation.json").write_text("{")
            unreadable.append(gd.host_busy(fleet))
            (fleet / "reservation.json").write_text(json.dumps({"until": 1}))
            busy.append(gd.host_busy(fleet))
            (fleet / "reservation.json").unlink()
            os.symlink(fleet / "elsewhere", fleet / "host.lock")  # O_NOFOLLOW refuses it
            unreadable.append(gd.host_busy(fleet))
            (fleet / "host.lock").unlink()
            (fleet / "host.lock").touch()
            with (fleet / "host.lock").open() as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                busy.append(gd.host_busy(fleet))
        self.assertEqual(len(set(unreadable)), 4, unreadable)
        for reason in unreadable:
            self.assertTrue(reason and gd.host_busy_unreadable(reason), reason)
        for reason in busy:
            self.assertTrue(reason and not gd.host_busy_unreadable(reason), reason)
        self.assertFalse(gd.host_busy_unreadable(""))

    def test_the_repo_runner_hook_is_allowlisted_for_drain(self) -> None:
        """Every change to scripts/glaeda-cmux-runner-hook must pin its sha256 in CMUX_NEXT_DRAIN_HOOK_SHA256, or
        drain refuses every runner that installed it ("runner hook enrollment is not proven"). #1561 missed it
        and the three drain tests below went red on main with a message that did not say why (#1553 before it).
        On a checkout this pins it (scripts/glaeda_hook_pin.py); under CI it only checks."""
        self.assertIsNone(glaeda_hook_pin.ensure(check=glaeda_hook_pin.under_ci()))
        self.assertIn(glaeda_hook_pin.hook_sha(), gd.CMUX_NEXT_DRAIN_HOOK_SHA256)

    def test_cmux_next_drain_requires_enrollment_and_holds_host_lock(self) -> None:
        fleet = self.root / "fleet"
        fleet.mkdir()
        (fleet / "host.lock").touch()
        (fleet / "reservation.json").write_text(json.dumps({
            "schema": "glaeda-reservation/v1", "owner": "test", "purpose": "drain",
            "since": int(time.time()) - 1, "until": int(time.time()) + 60,
        }))
        runner = self.root / "actions-runner-glaeda"
        (runner / "glaeda-hooks").mkdir(parents=True)
        (runner / ".runner").write_text("{}")
        (runner / "glaeda-hooks/job-started.sh").write_text(
            "exec /usr/bin/python3 glaeda-cmux-runner-hook job-started --allowed-owner manaflow-ai\n"
        )
        (runner / "glaeda-hooks/glaeda-cmux-runner-hook").write_bytes(
            (ROOT / "scripts/glaeda-cmux-runner-hook").read_bytes()
        )
        plist = self.root / "Library/LaunchAgents"
        plist.mkdir(parents=True)
        (plist / "runner.plist").write_bytes(plistlib.dumps({
            "EnvironmentVariables": {"ACTIONS_RUNNER_HOOK_JOB_STARTED": os.fspath(runner / "glaeda-hooks/job-started.sh")}
        }))
        saved = (gd.HOME, gd.FLEET_DIR)
        gd.HOME, gd.FLEET_DIR = self.root, fleet
        try:
            ps = mock.patch.object(
                gd.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, "/sbin/launchd\n", ""),
            )
            with mock.patch.object(
                gd.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, "/sbin/launchd\n", ""),
            ):
                handle, reason = gd.cmux_next_drain_preflight(expected_owner="test")
            self.assertIsNotNone(handle, reason)
            self.assertEqual(reason, "")
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
            with mock.patch.object(
                gd.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, "/sbin/launchd\n", ""),
            ):
                handle, reason = gd.cmux_next_drain_preflight(expected_owner="another-owner")
            self.assertIsNone(handle)
            self.assertIn("owner", reason)
            (runner / "glaeda-hooks/glaeda-cmux-runner-hook").write_text(
                "# def take_host_lock(x): pass; def release_host_lock(x): pass\n"
            )
            with ps:
                handle, reason = gd.cmux_next_drain_preflight(expected_owner="test")
            self.assertIsNone(handle)
            self.assertIn("enrollment", reason)
        finally:
            gd.HOME, gd.FLEET_DIR = saved

    def test_cmux_next_drain_defers_when_host_lock_is_held(self) -> None:
        fleet = self.root / "fleet"
        fleet.mkdir()
        lock = fleet / "host.lock"
        lock.touch()
        (fleet / "reservation.json").write_text(json.dumps({
            "schema": "glaeda-reservation/v1", "owner": "test", "purpose": "drain",
            "since": int(time.time()) - 1, "until": int(time.time()) + 60,
        }))
        runner = self.root / "actions-runner-glaeda"
        (runner / "glaeda-hooks").mkdir(parents=True)
        (runner / ".runner").write_text("{}")
        (runner / "glaeda-hooks/job-started.sh").write_text("glaeda-cmux-runner-hook job-started\n")
        (runner / "glaeda-hooks/glaeda-cmux-runner-hook").write_bytes(
            (ROOT / "scripts/glaeda-cmux-runner-hook").read_bytes()
        )
        plist = self.root / "Library/LaunchAgents"
        plist.mkdir(parents=True)
        (plist / "runner.plist").write_bytes(plistlib.dumps({
            "EnvironmentVariables": {"ACTIONS_RUNNER_HOOK_JOB_STARTED": os.fspath(runner / "glaeda-hooks/job-started.sh")}
        }))
        saved = (gd.HOME, gd.FLEET_DIR)
        gd.HOME, gd.FLEET_DIR = self.root, fleet
        with lock.open("a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            try:
                with mock.patch.object(
                    gd.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], 0, "/sbin/launchd\n", ""),
                ):
                    handle, reason = gd.cmux_next_drain_preflight(expected_owner="test")
                self.assertIsNone(handle)
                self.assertIn("host.lock", reason)
            finally:
                gd.HOME, gd.FLEET_DIR = saved

    def test_cmux_next_drain_defers_for_a_live_runner_worker(self) -> None:
        fleet = self.root / "fleet"
        fleet.mkdir()
        (fleet / "host.lock").touch()
        (fleet / "reservation.json").write_text(json.dumps({
            "schema": "glaeda-reservation/v1", "owner": "test", "purpose": "drain",
            "since": int(time.time()) - 1, "until": int(time.time()) + 60,
        }))
        runner = self.root / "actions-runner-glaeda"
        (runner / "glaeda-hooks").mkdir(parents=True)
        (runner / ".runner").write_text("{}")
        (runner / "glaeda-hooks/job-started.sh").write_text("glaeda-cmux-runner-hook job-started\n")
        (runner / "glaeda-hooks/glaeda-cmux-runner-hook").write_bytes(
            (ROOT / "scripts/glaeda-cmux-runner-hook").read_bytes()
        )
        plist = self.root / "Library/LaunchAgents"
        plist.mkdir(parents=True)
        (plist / "runner.plist").write_bytes(plistlib.dumps({
            "EnvironmentVariables": {"ACTIONS_RUNNER_HOOK_JOB_STARTED": os.fspath(runner / "glaeda-hooks/job-started.sh")}
        }))
        saved = (gd.HOME, gd.FLEET_DIR)
        gd.HOME, gd.FLEET_DIR = self.root, fleet
        try:
            with mock.patch.object(
                gd.subprocess, "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, "/Users/actions-runner-glaeda/bin/Runner.Worker --job\n", ""
                ),
            ):
                handle, reason = gd.cmux_next_drain_preflight(expected_owner="test")
            self.assertIsNone(handle)
            self.assertIn("live job/build", reason)
        finally:
            gd.HOME, gd.FLEET_DIR = saved

    def test_idle_apply_stops_once_the_host_turns_busy(self) -> None:
        for name in ("a", "b", "c"):
            make(self.root / name)
        items = gd.survey([self.fam], 24, 0)
        calls = iter(["", "a CI job is running (x)"])
        with contextlib.redirect_stdout(io.StringIO()):
            gd.apply(items, {self.fam.id: self.fam}, self.receipt(), None, 24, halt=lambda: next(calls))
        self.assertEqual(sum((self.root / n).exists() for n in ("a", "b", "c")), 2)
        recs = [json.loads(l) for l in self.receipt().read_text().splitlines()]
        self.assertEqual([(r["outcome"], r["sweep"]) for r in recs], [("reclaimed", "idle")])

    def test_idle_sweep_in_main(self) -> None:
        make(self.root / "old", age_hours=50)
        make(self.root / "day", age_hours=30)
        stamp = self.root.parent / f"{self.root.name}-idle.last"
        self.addCleanup(stamp.unlink, missing_ok=True)
        args = ["--pressure", "--idle", "--apply", "--no-snapshot", "--top", "0", "--min-mib", "0", "--low", "1", "--target", "2",
                "--receipt", os.fspath(self.receipt())]
        with mock.patch.object(gd, "default_families", return_value=[self.fam]), \
                mock.patch.object(gd, "free_bytes", return_value=(100 * gd.GIB, 400 * gd.GIB)), \
                mock.patch.object(gd, "IDLE_STAMP", stamp), mock.patch.object(gd, "EVICT_LOCK", self.root.parent / f"{self.root.name}.lock"), \
                mock.patch.object(gd, "simulator_runtimes", return_value=[]):
            with mock.patch.object(gd, "host_busy", return_value="a CI job is running (x)"), \
                    mock.patch.dict(gd.POLICY, defer_during_ci=True), contextlib.redirect_stdout(io.StringIO()) as out:
                gd.main(args)
            self.assertIn("host busy", out.getvalue())
            self.assertTrue((self.root / "old").exists(), "defer_during_ci keeps the old hold")
            # a busy host sweeps too by default: only what a job uses is held
            with mock.patch.object(gd, "host_busy", return_value="a CI job is running (x)"), \
                    contextlib.redirect_stdout(io.StringIO()):
                gd.main(args)
            self.assertFalse((self.root / "old").exists())
            self.assertFalse((self.root / "day").exists())
            self.assertTrue(stamp.exists())
            make(self.root / "old", age_hours=50)
            with mock.patch.object(gd, "host_busy", return_value=""), contextlib.redirect_stdout(io.StringIO()) as out:
                gd.main(args)  # within the hour: no second sweep
            self.assertIn("within", out.getvalue())
            self.assertTrue((self.root / "old").exists())

    def test_simulator_runtimes_go_only_when_nothing_uses_them(self) -> None:
        now = time.time()
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 30 * 86400))
        new = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 86400))

        def rt(version, used, deletable=True):
            return {"runtimeIdentifier": f"ios-{version}", "platformIdentifier": "iphonesimulator",
                    "version": version, "deletable": deletable, "lastUsedAt": used, "sizeBytes": 8 * gd.GIB}
        runtimes = {"A": rt("26.5", old), "B": rt("26.2", old), "C": rt("26.1", new), "D": rt("18.0", old),
                    "E": rt("17.0", old, deletable=False)}
        devices = {"devices": {"ios-18.0": [{"state": "Shutdown"}]}}
        got = {i.path: i.verdict for i in gd.runtime_items(runtimes, devices, now)}
        self.assertEqual(got, {"A": "kept", "B": "reclaimable", "C": "recent", "D": "kept", "E": "kept"})
        devices["devices"]["ios-18.0"][0]["state"] = "Booted"
        self.assertNotIn("reclaimable", {i.verdict for i in gd.runtime_items(runtimes, devices, now)})
        self.assertEqual(gd.runtime_items({"A": rt("1", old)}, None, now), [])

    def test_xcode_copies_are_report_only_with_their_pins(self) -> None:
        apps = self.root / "Applications"
        for name in ("Xcode.app", "Xcode_26.6.app"):
            (apps / name / "Contents").mkdir(parents=True)
        pins = {os.path.realpath(apps / "Xcode_26.6.app"): "a runner hook's --toolchain-xcode"}
        items = {Path(i.path).name: i for i in gd.xcode_items(apps, pins)}
        self.assertEqual({i.verdict for i in items.values()}, {"report-only"})
        self.assertIn("no pin", items["Xcode.app"].reasons[1])
        self.assertIn("runner hook", items["Xcode_26.6.app"].reasons[1])
        home = self.root / "home"
        (home / "actions-runner-glaeda/glaeda-hooks").mkdir(parents=True)
        (home / "actions-runner-glaeda/glaeda-hooks/job-started.sh").write_text(
            "exec x job-started --min-free-gib 50 --toolchain-xcode /Applications/Xcode_26.6.app --instance 0\n")
        self.assertIn(os.path.realpath("/Applications/Xcode_26.6.app"), gd.xcode_pins(home))


class LinuxLayoutTest(unittest.TestCase):
    """The Linux layout, checked on every platform by pointing HOME and DARWIN at a fake host."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.saved = (gd.HOME, gd.DARWIN)
        gd.HOME, gd.DARWIN = self.home, False

    def tearDown(self) -> None:
        gd.HOME, gd.DARWIN = self.saved
        gd.UNREADABLE.clear()
        self.tmp.cleanup()

    def test_cache_families_skip_state_that_is_not_a_cache(self) -> None:
        fams = {f.id: f for f in gd.default_families() if f.id in ("user-cache", "library-caches")}
        for fam in fams.values():
            fam.root.mkdir(parents=True, exist_ok=True)
        keep = {"user-cache": ["glaeda", "glaeda-disk", "glaeda-fullapp", "glaeda-fleet-cas", "cmux-job",
                               "huggingface", "codex-runtimes"],
                "library-caches": ["glaeda", "cmux-next-ci", "PassKit", "tidy-branches", "CloudKit", "com.apple.Safari"]}
        for fid, names in keep.items():
            fam = fams.get(fid)
            if fam is None:
                continue
            for n in names:
                self.assertTrue(n in fam.skip or n.startswith(fam.skip_prefixes), f"{fid}/{n}")
            self.assertTrue(fam.reclaimable)

    def test_linux_families(self) -> None:
        for rel in ("Projects/glaeda", "Projects/glaeda-worktrees/a", "Projects/botany-sim-worktrees/b",
                    ".cache/pip"):
            (self.home / rel).mkdir(parents=True)
        fams = gd.default_families()
        by_id: dict[str, list[Path]] = {}
        for f in fams:
            by_id.setdefault(f.id, []).append(f.root)
        self.assertEqual(by_id["tmp"], [Path("/tmp")])
        self.assertNotIn("library-caches", by_id)
        self.assertNotIn("xcode-derived-data", by_id)
        self.assertEqual(sorted(p.name for p in by_id["worktrees"]),
                         ["botany-sim-worktrees", "glaeda-worktrees"])
        reclaimable = {f.id for f in fams if f.reclaimable}
        self.assertTrue(reclaimable <= {"tmp", "claude-scratchpad", "user-cache", "home-caches", "checkout-build"})
        self.assertEqual(next(f for f in fams if f.id == "tmp").platform, "linux")
        self.assertEqual(next(f for f in fams if f.id == "user-cache").platform, "linux")
        self.assertEqual(next(f for f in fams if f.id == "home-caches").platform, "linux")
        projects = next(f for f in fams if f.id == "projects")
        self.assertIn("botany-sim-worktrees", projects.skip)
        tmp = next(f for f in fams if f.id == "tmp")
        self.assertIn(f"claude-{os.getuid()}", tmp.skip)
        # scratch clones and leaked files are judged on any filesystem, not only a tmpfs
        self.assertTrue(tmp.files and tmp.git_disposable)

    def test_cmux_job_units_go_but_unpushed_checkouts_stay(self) -> None:
        job = self.home / ".cache/cmux-job"
        # as on cmux13s: SwiftPM clones inside DerivedData, and each slot's clone one level below cmux-base
        (job / "reload-cloud-ios/DerivedData/slot-1/Build").mkdir(parents=True)
        (job / "reload-cloud-ios/DerivedData/slot-1/Build/x.o").write_bytes(b"\0" * 4096)
        (job / "reload-cloud-ios/DerivedData/slot-1/SourcePackages/checkouts/dep/.git").mkdir(parents=True)
        base = job / "reload-cloud/cmux-base/slot-1"
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init", "-q", str(base)], check=True, env=env)
        (base / "f").write_text("x")
        subprocess.run(["git", "-C", str(base), "add", "f"], check=True, env=env)
        subprocess.run(["git", "-C", str(base), "commit", "-qm", "local only"], check=True, env=env)
        old = time.time() - 48 * 3600
        for dirpath, dirnames, filenames in os.walk(job):
            for n in dirnames + filenames:
                os.utime(os.path.join(dirpath, n), (old, old), follow_symlinks=False)
        saved = gd.process_evidence
        gd.process_evidence = lambda: ([], "")
        try:
            fams = [f for f in gd.default_families() if f.id in ("cmux-job-cache", "user-cache")]
            self.assertEqual(next(f for f in fams if f.id == "cmux-job-cache").min_idle_hours, 12.0)
            items = gd.survey(fams, 24, 0)
        finally:
            gd.process_evidence = saved
        verdicts = {Path(i.path).relative_to(self.home).as_posix(): (i.family, i.verdict) for i in items}
        self.assertEqual(verdicts[".cache/cmux-job/reload-cloud-ios/DerivedData"], ("cmux-job-cache", "reclaimable"))
        # a commit no remote holds keeps its checkout
        self.assertEqual(verdicts[".cache/cmux-job/reload-cloud/cmux-base"], ("cmux-job-cache", "git-checkout"))
        self.assertNotIn(".cache/cmux-job", verdicts)  # not listed again as a report-only tool cache
        receipt = self.home / "receipt.jsonl"
        fams_by_id = {f.id: f for f in fams}
        with contextlib.redirect_stdout(io.StringIO()):
            saved = gd.process_evidence
            gd.process_evidence = lambda: ([], "")
            try:
                gd.apply(items, fams_by_id, receipt, None, 24)
            finally:
                gd.process_evidence = saved
        self.assertFalse((job / "reload-cloud-ios/DerivedData").exists())
        self.assertEqual([c.name for c in (job / "reload-cloud-ios").iterdir()], [])  # no half-deleted leftover
        self.assertTrue((base / ".git").is_dir())
        # a delete interrupted after its rename leaves a tree nothing else will ever clean up
        dead = subprocess.Popen(["true"])
        dead.wait()
        left = job / f"reload-cloud-ios/.glaeda-disk-deleting-DerivedData-{dead.pid}"
        (left / "slot-1/SourcePackages/checkouts/dep/.git").mkdir(parents=True)
        (left / "slot-1/x.o").write_bytes(b"\0" * 4096)
        for dirpath, dirnames, filenames in os.walk(left):
            for n in dirnames + filenames:
                os.utime(os.path.join(dirpath, n), (old, old), follow_symlinks=False)
        os.utime(left, (old, old))
        saved = gd.process_evidence
        gd.process_evidence = lambda: ([], "")
        try:
            again = {i.path: (i.family, i.verdict)
                     for i in gd.survey(fams + gd.interrupted_families(fams), 24, 0)}
        finally:
            gd.process_evidence = saved
        self.assertEqual(again[str(left)], ("interrupted-deletions", "reclaimable"))

    def test_idle_cmux_job_mirrors_go_after_a_week_unless_a_job_holds_them(self) -> None:
        """2026-10-11: 107 GiB of reload-cloud trees idle since Sep 28-Oct 2 on eight minis, all kept as
        "git-checkout (checkout below the top level)", so glaeda-disk reported 0.0 G reclaimable."""
        job = self.home / ".cache/cmux-job"
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        units = {"reload-cloud/cmux-base": "slot-1", "reload-cloud/work": "slot-1", "slots": "host.1"}
        for unit, slot in units.items():
            repo = job / unit / slot
            subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
            (repo / "f").write_text("x")
            subprocess.run(["git", "-C", str(repo), "add", "f"], check=True, env=env)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "local only"], check=True, env=env)
        # SwiftPM's clones: reproducible like DerivedData, so they go at the family's usual age
        (job / "SourcePackages/host.1/checkouts/dep/.git").mkdir(parents=True)
        (job / "reload-cloud-ios/work/slot-2").mkdir(parents=True)
        (job / "reload-cloud-ios/work/slot-2/.lock").write_text("")  # hq's slot lock: a reload runs
        (job / "reload-cloud-ios/cmux-base/slot-2/.git").mkdir(parents=True)

        def age(rel: str, hours: float) -> None:
            t = time.time() - hours * 3600
            for dirpath, dirnames, filenames in os.walk(job / rel):
                for n in dirnames + filenames:
                    os.utime(os.path.join(dirpath, n), (t, t), follow_symlinks=False)
            os.utime(job / rel, (t, t))

        age("", 8 * 24)
        age("slots", 3 * 24)  # idle, but not a week: its local commit still keeps it
        fams = [f for f in gd.default_families() if f.id == "cmux-job-cache"]
        self.assertEqual(fams[0].mirror_idle_hours, 168.0)
        with mock.patch.object(gd, "process_evidence",
                               return_value=([], f"rsync -a {job}/reload-cloud/work/slot-1/ x\n")):
            items = {Path(i.path).relative_to(job).as_posix(): i for i in gd.survey(fams, 24, 0)}
        verdicts = {k: i.verdict for k, i in items.items()}
        self.assertEqual(verdicts, {
            "reload-cloud/cmux-base": "reclaimable",
            "reload-cloud/work": "in-use",  # a running command names a slot in it
            "reload-cloud-ios/cmux-base": "in-use",  # its unit's work/<slot>/.lock
            "reload-cloud-ios/work": "in-use",
            "slots/host.1": "git-checkout",
            "SourcePackages/host.1": "reclaimable"})
        self.assertIn("idle 168h+: its checkouts are mirrors", items["reload-cloud/cmux-base"].reasons)
        receipt = self.home / "receipt.jsonl"
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(gd, "process_evidence", return_value=([], "")):
            gd.apply(list(items.values()), {f.id: f for f in fams}, receipt, None, 24)
        self.assertFalse((job / "reload-cloud/cmux-base").exists())
        self.assertFalse((job / "SourcePackages/host.1").exists())
        self.assertTrue((job / "reload-cloud/work/slot-1/.git").is_dir())
        self.assertTrue((job / "slots/host.1/.git").is_dir())
        self.assertTrue((job / "reload-cloud-ios/cmux-base/slot-2/.git").is_dir())

    def test_interrupted_deletions_finish_once_their_deleter_is_gone(self) -> None:
        """cmux8s and cmuxs-mac-mini-3, 2026-10-11: 9.3 and 7.8 GiB renamed aside by a killed glaeda-disk sat
        in the fleet CI store, where cmux-kept-build lists only its own names."""
        store = self.home / "ci"
        kept = gd.Family("cmux-kept-build", store, True, "x", include=("derived-data",), build_output=True)
        make(store / "derived-data", age_hours=1)
        dead = subprocess.Popen(["true"])
        dead.wait()
        stranger = subprocess.Popen(["sleep", "60"])  # a reused pid running something else
        self.addCleanup(stranger.wait)
        self.addCleanup(stranger.kill)
        gone = make(store / f".glaeda-disk-deleting-derived-data-{dead.pid}", age_hours=0)
        (gone / "SourcePackages/checkouts/dep/.git").mkdir(parents=True)
        (gone / "ro").mkdir()
        (gone / "ro/f").write_text("x")
        os.chmod(gone / "ro", 0o500)
        reused = make(store / f".glaeda-disk-deleting-derived-data-{stranger.pid}", age_hours=0)
        live = make(store / f".glaeda-disk-deleting-derived-data-{os.getpid()}", age_hours=0)  # this test: glaeda-disk
        self.assertEqual(gd.candidates(kept), [store / "derived-data"])
        extra = gd.interrupted_families([kept])
        self.assertEqual([(f.id, f.root) for f in extra], [("interrupted-deletions", store)])
        with mock.patch.object(gd, "process_evidence", return_value=([], "")):
            items = gd.survey([kept, *extra], 24, 0)
        got = {Path(i.path).name: (i.family, i.verdict) for i in items}
        self.assertEqual(got, {
            "derived-data": ("cmux-kept-build", "recent"),
            gone.name: ("interrupted-deletions", "reclaimable"),
            reused.name: ("interrupted-deletions", "reclaimable"),
            live.name: ("interrupted-deletions", "kept")})
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(gd, "process_evidence", return_value=([], "")):
            gd.apply(items, {f.id: f for f in (kept, *extra)}, self.home / "r.jsonl", None, 24,
                     all_families=[kept, *extra])
        self.assertEqual(sorted(p.name for p in store.iterdir()), sorted(["derived-data", live.name]))
        # finished in place, not renamed a second time
        self.assertFalse(any(".glaeda-disk-deleting-.glaeda-disk-deleting-" in p.name for p in store.iterdir()))
        receipts = [json.loads(line) for line in (self.home / "r.jsonl").read_text().splitlines()]
        self.assertEqual(sorted(r["outcome"] for r in receipts), ["reclaimed", "reclaimed"])
        # a match family whose only candidate went aside, and a depth-2 family never lists inside an aside tree
        ios = self.home / "ci-ios"
        match = gd.Family("ci-ios-derived-data", ios, True, "x", depth=2, match="derived-data", build_output=True)
        make(ios / "runner-1" / f".glaeda-disk-deleting-derived-data-{dead.pid}" / "derived-data")
        self.assertEqual(gd.candidates(match), [])
        self.assertEqual([f.root for f in gd.interrupted_families([match])], [ios / "runner-1"])
        two = gd.Family("cmux-job-cache", self.home / "job", True, "x", depth=2)
        make(self.home / "job" / f".glaeda-disk-deleting-reload-cloud-{dead.pid}" / "work")
        make(self.home / "job/reload-cloud/work")
        self.assertEqual(gd.candidates(two), [self.home / "job/reload-cloud/work"])

    def test_claude_session_seen_in_alternate_config_dir(self) -> None:
        t = self.home / ".claude-outlook/projects/-home-leo-Projects/abc-123.jsonl"
        t.parent.mkdir(parents=True)
        t.write_text("{}")
        self.assertTrue(gd.claude_session_active("abc-123", 3600))
        self.assertFalse(gd.claude_session_active("other", 3600))

    def test_unreadable_message_names_no_full_disk_access_on_linux(self) -> None:
        gd.UNREADABLE.add("/tmp/locked")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            gd.report([], [], 5, 24)
        self.assertIn("cannot read /tmp/locked", err.getvalue())
        self.assertNotIn("Full Disk Access", err.getvalue())

    def test_linux_paths_have_one_spelling(self) -> None:
        self.assertEqual(gd.spellings("/tmp/claude-1000/x"), ["/tmp/claude-1000/x"])
        self.assertTrue(gd.named_by("/tmp/claude-1000/x", "cargo build --target-dir /tmp/claude-1000/x/t\n"))


@unittest.skipUnless(sys.platform.startswith("linux"), "reads /proc")
class ProcEvidenceTest(unittest.TestCase):
    def test_proc_evidence_sees_own_cwd(self) -> None:
        opened, cmds = gd.process_evidence()
        self.assertIn(os.getcwd(), opened)
        self.assertTrue(cmds.strip())


@unittest.skipUnless(sys.platform == "darwin", "reads macOS process evidence")
class DarwinEvidenceTest(unittest.TestCase):
    def test_owned_query_returns_text_and_status(self) -> None:
        with mock.patch.object(gd, "_owned_query", side_effect=[
            (0, "p123\nn/Users/cmux/work\n"),
            (0, "launchd\n"),
        ]):
            opened, commands = gd.process_evidence()
        self.assertEqual(opened, ["/Users/cmux/work"])
        self.assertEqual(commands, "launchd\n")

    def test_owned_query_parses_cache_path(self) -> None:
        with mock.patch.object(gd, "_owned_query", side_effect=[
            (0, "p123\nn/Users/cmux/cache\n"),
            (0, "launchd\n"),
        ]):
            opened, commands = gd.process_evidence()
        self.assertEqual(opened, ["/Users/cmux/cache"])
        self.assertEqual(commands, "launchd\n")


@unittest.skipUnless(sys.platform == "darwin", "APFS clones are macOS only")
class DedupeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=os.path.expanduser("~"))
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def blob(self, rel: str, data: bytes, mode: int = 0o644, mtime: float = 1_000_000) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.chmod(p, mode)
        os.utime(p, (mtime, mtime))
        return p

    def test_identical_files_are_cloned_keeping_mode_and_mtime(self) -> None:
        data = os.urandom(2 * 1024 * 1024)
        a = self.blob("a/SourcePackages/x.bin", data)
        b = self.blob("b/SourcePackages/x.bin", data, mode=0o755, mtime=2_000_000)
        c = self.blob("c/SourcePackages/x.bin", os.urandom(len(data)))
        ino_b = b.stat().st_ino
        r = gd.dedupe([self.root / "a", self.root / "b", self.root / "c"], self.state)
        self.assertEqual(r["cloned_bytes"], len(data))
        self.assertEqual(b.read_bytes(), data)
        self.assertNotEqual(b.stat().st_ino, ino_b)
        self.assertEqual(oct(b.stat().st_mode & 0o777), oct(0o755))
        self.assertEqual(int(b.stat().st_mtime), 2_000_000)
        self.assertNotEqual(c.read_bytes(), data)
        self.assertEqual(a.read_bytes(), data)
        again = gd.dedupe([self.root / "a", self.root / "b", self.root / "c"], self.state)
        self.assertEqual((again["hashed_bytes"], again["cloned_bytes"]), (0, 0))
        self.assertEqual([p.name for p in self.root.rglob("*glaeda-clone*")], [])

    def test_cmux_next_ci_runner_copies_are_deduped(self) -> None:
        """cmux10s, 2026-10-11: --dedupe found 0 files while three runners each kept the same 3.9 GiB of
        SourcePackages under ~/Library/Caches/cmux-next-ci/<scheme>/<runner>."""
        data = os.urandom(2 * 1024 * 1024)
        base = "Library/Caches/cmux-next-ci/cmux-scheme"
        copies = [self.blob(f"{base}/host-glaeda-{n}/SourcePackages/artifacts/x.bin", data) for n in (2, 3, 4)]
        module = self.blob(f"{base}/host-glaeda-2/ModuleCache.noindex/m.pcm", data)
        build = self.blob(f"{base}/host-glaeda-3/Build/out.o", data)  # build products are not deduped
        self.assertEqual(gd.cmux_next_ci_dirs(self.root), [self.root / base])
        self.assertEqual(gd.cmux_next_ci_dirs(self.root / "nowhere"), [])
        roots = gd.dedupe_roots(self.root / base)
        self.assertEqual(sorted(r.relative_to(self.root / base).as_posix() for r in roots),
                         ["host-glaeda-2/ModuleCache.noindex", "host-glaeda-2/SourcePackages",
                          "host-glaeda-3/SourcePackages", "host-glaeda-4/SourcePackages"])
        build_ino = build.stat().st_ino
        r = gd.dedupe(roots, self.state)
        self.assertEqual(r["cloned_bytes"], 3 * len(data))  # four identical files, one canonical
        self.assertTrue(all(p.read_bytes() == data for p in (*copies, module)))
        self.assertEqual(build.stat().st_ino, build_ino)

    def test_cmux_next_ci_products_are_deduped_only_with_opt_in_guard(self) -> None:
        data = os.urandom(2 * 1024 * 1024)
        root = self.root / "Library/Caches/cmux-next-ci"
        first = self.blob("Library/Caches/cmux-next-ci/cmux-scheme/runner-a/Products/a.o", data)
        second = self.blob("Library/Caches/cmux-next-ci/cmux-scheme/runner-b/Products/a.o", data)
        before = second.stat().st_ino
        # The ordinary input-only roots intentionally do not include Products.
        self.assertEqual(gd.dedupe(gd.dedupe_roots(root / "cmux-scheme"), self.state)["cloned_bytes"], 0)
        self.assertEqual(second.stat().st_ino, before)
        # An explicit inactive-Products pass can clone it after its guard has rechecked
        # the state roots.  This test uses a constant guard because APFS clone support
        # is already the class-level Darwin requirement.
        r = gd.dedupe(gd.cmux_next_ci_dedupe_roots(root), self.state, guard=lambda _: True)
        self.assertEqual(r["cloned_bytes"], len(data))
        self.assertEqual(second.read_bytes(), data)
        self.assertNotEqual(second.stat().st_ino, before)
        self.assertEqual(first.read_bytes(), data)

    def test_cmux_next_ci_nested_build_products_are_deduped(self) -> None:
        data = os.urandom(2 * 1024 * 1024)
        root = self.root / "Library/Caches/cmux-next-ci"
        first = self.blob("Library/Caches/cmux-next-ci/cmux-scheme/runner-a/Build/Products/a.o", data)
        second = self.blob("Library/Caches/cmux-next-ci/cmux-scheme/runner-b/Build/Products/a.o", data)
        before = second.stat().st_ino
        # The ordinary input-only roots still exclude the Build tree.
        self.assertEqual(gd.dedupe(gd.dedupe_roots(root / "cmux-scheme"), self.state)["cloned_bytes"], 0)
        self.assertEqual(second.stat().st_ino, before)
        r = gd.dedupe(gd.cmux_next_ci_dedupe_roots(root), self.state, guard=lambda _: True)
        self.assertEqual(r["cloned_bytes"], len(data))
        self.assertEqual(second.read_bytes(), data)
        self.assertNotEqual(second.stat().st_ino, before)
        self.assertEqual(first.read_bytes(), data)

    @unittest.skipUnless(sys.platform == "darwin", "APFS clones")
    def test_dedupe_main_scans_cmux_next_ci(self) -> None:
        data = os.urandom(2 * 1024 * 1024)
        base = "Library/Caches/cmux-next-ci/cmux-scheme"
        for n in (2, 3):
            self.blob(f"{base}/host-glaeda-{n}/SourcePackages/x.bin", data)
        out = io.StringIO()
        with mock.patch.object(gd, "HOME", self.root), mock.patch.object(gd, "DEDUPE_STATE", self.state), \
                mock.patch.object(gd, "default_families", return_value=[]), contextlib.redirect_stdout(out):
            self.assertEqual(gd.main(["--dedupe"]), 0)
        self.assertIn("dedupe: 2 files >= 1 MiB", out.getvalue())
        self.assertIn("cloned 0.0G", out.getvalue())  # 2 MiB rounds to 0.0G; the state records the clone
        state = json.loads(self.state.read_text())
        self.assertTrue(all(v["cloned"] for v in state.values()))

    def test_target_changed_after_hash_is_refused(self) -> None:
        data = os.urandom(2 * 1024 * 1024)
        a = self.blob("a/x.bin", data)
        b = self.blob("b/x.bin", data)
        key = gd._file_key(os.lstat(b))
        os.utime(b, (3_000_000, 3_000_000))
        self.assertFalse(gd.clone_over(str(a), str(b), key))
        self.assertEqual(int(b.stat().st_mtime), 3_000_000)

    def test_canonical_changed_after_hash_is_refused(self) -> None:
        data = os.urandom(2 * 1024 * 1024)
        a = self.blob("a/x.bin", data)
        b = self.blob("b/x.bin", data)
        akey, bkey = gd._file_key(os.lstat(a)), gd._file_key(os.lstat(b))
        a.write_bytes(os.urandom(len(data)))
        self.assertFalse(gd.clone_over(str(a), str(b), bkey, akey))
        self.assertEqual(b.read_bytes(), data)

    def test_recently_written_files_wait_for_a_later_pass(self) -> None:
        data = os.urandom(2 * 1024 * 1024)
        self.blob("a/x.bin", data)
        self.blob("b/x.bin", data, mtime=time.time())
        r = gd.dedupe([self.root / "a", self.root / "b"], self.state, settle_s=600)
        self.assertEqual((r["young"], r["cloned_bytes"]), (1, 0))

    def test_small_and_unique_files_are_ignored(self) -> None:
        self.blob("a/small", b"x" * 10)
        self.blob("b/small", b"x" * 10)
        r = gd.dedupe([self.root / "a", self.root / "b"], self.state)
        self.assertEqual((r["files"], r["cloned_bytes"]), (0, 0))


class _Stat:
    """An lstat result with some fields replaced (st_dev, st_uid), for paths a test pretends are elsewhere."""

    def __init__(self, st: os.stat_result, **over: int) -> None:
        self._st, self._over = st, over

    def __getattr__(self, name: str):
        return self._over[name] if name in self._over else getattr(self._st, name)


apfs = unittest.skipUnless(sys.platform == "darwin", "APFS clones are macOS only")


class CaptureDedupeTest(unittest.TestCase):
    """cmux-mac-mini, 2026-10-18..21: every pressure pass freed 0 GiB while ~/messageslab-26 held 11.2 GiB of
    byte-identical PNG frames (deputy-2 cloned them by hand at a 16 KiB floor). Dedupe looked only at Xcode's
    folders with a 1 MiB floor. Capture and evidence trees are now policy roots with their own smaller floor."""

    KIB = 1024

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=os.path.expanduser("~"))
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.policy = dict(gd.POLICY_DEFAULTS)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def blob(self, rel: str, data: bytes, mode: int = 0o644, mtime: float = 1_000_000) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.chmod(p, mode)
        os.utime(p, (mtime, mtime))
        return p

    def dedupe(self, roots: list[Path], **kw):
        kw.setdefault("min_bytes", 16 * self.KIB)
        kw.setdefault("strict", True)
        return gd.dedupe(roots, self.state, **kw)

    def files(self) -> set[str]:
        return {p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                if p.is_file() and p != self.state and not p.name.startswith("dedupe")}

    def run_main(self, policy: dict | None = None, writers: frozenset = frozenset()) -> str:
        dd = self.root / "Library/Developer/Xcode/DerivedData"
        dd.mkdir(parents=True, exist_ok=True)
        out = io.StringIO()
        with mock.patch.object(gd, "HOME", self.root), \
                mock.patch.object(gd, "DEDUPE_STATE", self.root / ".cache/glaeda-disk/dedupe.json"), \
                mock.patch.object(gd, "POLICY", policy or self.policy), \
                mock.patch.object(gd, "default_families",
                                  return_value=[gd.Family("xcode-derived-data", dd, True, "x")]), \
                mock.patch.object(gd, "open_for_writing", return_value=writers), \
                contextlib.redirect_stdout(out):
            self.assertEqual(gd.main(["--dedupe"]), 0)
        return out.getvalue()

    # -- the policy declares the roots and the floor --------------------------------------------------

    def test_policy_declares_capture_roots_and_a_16_kib_floor(self) -> None:
        roots = gd.POLICY_DEFAULTS["dedupe_capture_roots"]
        for want in ("~/messageslab-*", "~/Projects/captures", "~/Applications/cx-ui-captures",
                     "/Users/Shared/cmux-capture", "/Users/Shared/cmux-captures"):
            self.assertIn(want, roots)
        self.assertEqual(gd.POLICY_DEFAULTS["dedupe_capture_min_kib"], 16)
        self.assertGreater(gd.POLICY_DEFAULTS["dedupe_capture_settle_minutes"], 0)
        self.assertGreater(gd.POLICY_DEFAULTS["dedupe_capture_budget_minutes"], 0)
        self.assertEqual(gd.DEDUPE_MIN_BYTES, 1024 * 1024)  # Xcode's folders keep their 1 MiB floor

    def test_a_host_policy_file_replaces_the_roots_and_bad_values_keep_the_default(self) -> None:
        path = self.root / "disk-policy.json"
        path.write_text(json.dumps({"dedupe_capture_roots": ["~/evidence", "/data/caps-*"],
                                    "dedupe_capture_min_kib": 64}))
        got = gd.disk_policy(path)
        self.assertEqual(got["dedupe_capture_roots"], ["~/evidence", "/data/caps-*"])
        self.assertEqual(got["dedupe_capture_min_kib"], 64)
        for bad in ("~/evidence", ["~/ok", 3], [""]):
            path.write_text(json.dumps({"dedupe_capture_roots": bad}))
            self.assertEqual(gd.disk_policy(path)["dedupe_capture_roots"],
                             gd.POLICY_DEFAULTS["dedupe_capture_roots"])
        path.write_text(json.dumps({"dedupe_capture_roots": []}))  # an empty list turns the pass off
        self.assertEqual(gd.disk_policy(path)["dedupe_capture_roots"], [])

    def test_roots_expand_home_and_globs_and_skip_missing_files_and_symlinks(self) -> None:
        for d in ("messageslab-26", "messageslab-27", "Projects/captures"):
            (self.root / d).mkdir(parents=True)
        (self.root / "messageslab-notes").write_text("a file, not a tree")
        (self.root / "messageslab-link").symlink_to(self.root / "Projects")
        got = gd.capture_dedupe_roots(["~/messageslab-*", "~/Projects/captures", "~/missing",
                                       os.fspath(self.root / "Projects/captures")], home=self.root)
        self.assertEqual(got, [self.root / "messageslab-26", self.root / "messageslab-27",
                               self.root / "Projects/captures"])

    # -- the pass itself ----------------------------------------------------------------------------

    @apfs
    def test_main_dedupe_clones_capture_frames_down_to_16_kib_and_deletes_nothing(self) -> None:
        frame = os.urandom(32 * self.KIB)
        a = self.blob("messageslab-26/sendw/take1/frames/0001.png", frame)
        b = self.blob("messageslab-26/sendw/take2/frames/0001.png", frame)
        c = self.blob("Projects/captures/run/0001.png", frame)
        tiny = os.urandom(8 * self.KIB)
        t1 = self.blob("messageslab-26/a/tiny.png", tiny)
        t2 = self.blob("messageslab-26/b/tiny.png", tiny)
        # Xcode's folders keep their 1 MiB floor: a 64 KiB duplicate there stays as it is.
        pkg = os.urandom(64 * self.KIB)
        x1 = self.blob("Library/Developer/Xcode/DerivedData/p1/SourcePackages/x.bin", pkg)
        x2 = self.blob("Library/Developer/Xcode/DerivedData/p2/SourcePackages/x.bin", pkg)
        self.policy["dedupe_capture_roots"] = ["~/messageslab-*", "~/Projects/captures"]
        inodes = {p: p.stat().st_ino for p in (a, b, c, t1, t2, x1, x2)}
        before = self.files()
        out = self.run_main()
        self.assertIn("dedupe captures: 3 files >= 16 KiB in 2 roots", out)
        self.assertEqual(self.files(), before)  # never deletes
        self.assertTrue(all(p.read_bytes() == frame for p in (a, b, c)))
        self.assertEqual(sum(p.stat().st_ino != inodes[p] for p in (a, b, c)), 2)
        for p in (t1, t2, x1, x2):
            self.assertEqual(p.stat().st_ino, inodes[p])
        state = json.loads((self.root / ".cache/glaeda-disk/dedupe-captures.json").read_text())
        self.assertTrue(all(state[os.fspath(p)]["cloned"] for p in (a, b, c)))

    @apfs
    def test_no_capture_roots_means_no_capture_pass(self) -> None:
        self.policy["dedupe_capture_roots"] = []
        self.assertNotIn("dedupe captures", self.run_main())

    @apfs
    def test_young_capture_files_wait_for_a_later_pass(self) -> None:
        frame = os.urandom(32 * self.KIB)
        a = self.blob("messageslab-26/a.png", frame)
        b = self.blob("messageslab-26/b.png", frame, mtime=time.time() - 60)
        self.policy["dedupe_capture_roots"] = ["~/messageslab-*"]
        ino = b.stat().st_ino
        out = self.run_main()
        self.assertIn("still settling 1", out)
        self.assertEqual(b.stat().st_ino, ino)
        self.assertEqual(a.read_bytes(), frame)

    @apfs
    def test_files_open_for_writing_are_left_alone(self) -> None:
        frame = os.urandom(32 * self.KIB)
        self.blob("caps/a.png", frame)
        b = self.blob("caps/b.png", frame)
        ino = b.stat().st_ino
        with open(b, "r+b") as held:  # a recorder still has it open for writing (real lsof)
            r = self.dedupe([self.root / "caps"], guard=gd.capture_dedupe_guard())
            self.assertEqual((r["cloned_bytes"], r["refused"]), (0, 1))
            self.assertEqual(b.stat().st_ino, ino)
            held.seek(0)
        r = self.dedupe([self.root / "caps"], guard=gd.capture_dedupe_guard())
        self.assertEqual(r["cloned_bytes"], len(frame))
        self.assertEqual(b.read_bytes(), frame)

    def test_writers_are_read_from_lsof_access_modes(self) -> None:
        out = ("p10\nfcwd\na \nn/Users/x\nf3\nar\nn/caps/read.png\nf4\naw\nn/caps/write.png\n"
               "p11\nf5\nau\nn/caps/rw.png\nf6\na \nn/caps/none.png\n")
        self.assertEqual(gd.lsof_writers(out), frozenset({"/caps/write.png", "/caps/rw.png"}))

    @apfs
    def test_without_lsof_evidence_the_capture_pass_is_skipped(self) -> None:
        frame = os.urandom(32 * self.KIB)
        self.blob("messageslab-26/a.png", frame)
        b = self.blob("messageslab-26/b.png", frame)
        self.policy["dedupe_capture_roots"] = ["~/messageslab-*"]
        ino = b.stat().st_ino
        with mock.patch.object(gd, "open_for_writing", side_effect=gd.NoEvidence("lsof: boom")):
            out = io.StringIO()
            with mock.patch.object(gd, "HOME", self.root), mock.patch.object(gd, "POLICY", self.policy), \
                    mock.patch.object(gd, "DEDUPE_STATE", self.root / ".cache/dedupe.json"), \
                    mock.patch.object(gd, "default_families", return_value=[]), contextlib.redirect_stdout(out):
                self.assertEqual(gd.main(["--dedupe"]), 0)
        self.assertIn("dedupe captures: skipped (lsof: boom)", out.getvalue())
        self.assertEqual(b.stat().st_ino, ino)

    @apfs
    def test_files_on_different_volumes_are_never_paired(self) -> None:
        frame = os.urandom(32 * self.KIB)
        a = self.blob("caps/a.png", frame)
        b = self.blob("caps/b.png", frame)
        real = os.lstat

        def lstat(p, *args, **kw):
            st = real(p, *args, **kw)
            return _Stat(st, st_dev=st.st_dev + 1) if os.fspath(p) == os.fspath(b) else st

        ino = b.stat().st_ino
        with mock.patch.object(gd.os, "lstat", side_effect=lstat):
            r = self.dedupe([self.root / "caps"])
            self.assertEqual((r["cloned_bytes"], r["hashed_bytes"]), (0, 0))
            key = gd._file_key(os.lstat(b))
            self.assertFalse(gd.clone_over(str(a), str(b), key, gd._file_key(os.lstat(a))))
        self.assertEqual(b.stat().st_ino, ino)

    # -- safety of each swap ------------------------------------------------------------------------

    @apfs
    def test_a_strict_clone_keeps_mode_times_birthtime_xattrs_and_group(self) -> None:
        frame = os.urandom(32 * self.KIB)
        a = self.blob("caps/a.png", frame)
        b = self.blob("caps/b.png", frame, mode=0o600)
        os.utime(b, (500_000, 500_000))  # moves b's birthtime back to 500000 (APFS keeps birth <= mtime)
        os.utime(b, (2_000_000, 2_000_000))
        for p in (a, b):
            subprocess.run(["xattr", "-w", "com.example.take", "send-typed", os.fspath(p)], check=True)
        before = os.lstat(b)
        r = self.dedupe([self.root / "caps"])
        self.assertEqual(r["cloned_bytes"], len(frame))
        after = os.lstat(b)
        self.assertNotEqual(after.st_ino, before.st_ino)
        self.assertEqual(b.read_bytes(), frame)
        self.assertEqual((oct(after.st_mode), after.st_uid, after.st_gid, after.st_mtime_ns, after.st_flags),
                         (oct(before.st_mode), before.st_uid, before.st_gid, before.st_mtime_ns, before.st_flags))
        self.assertEqual(int(after.st_birthtime), 500_000)
        self.assertEqual(gd.file_xattrs(os.fspath(b)), {"com.example.take": b"send-typed"})
        self.assertEqual([p.name for p in self.root.rglob("*glaeda-clone*")], [])

    @apfs
    def test_a_strict_clone_refuses_differing_xattrs_acls_or_flags(self) -> None:
        frame = os.urandom(32 * self.KIB)

        def pair(name: str) -> Path:
            self.blob(f"{name}/a.png", frame)
            return self.blob(f"{name}/b.png", frame)

        xattr_b, acl_b, flag_b = pair("xattr"), pair("acl"), pair("flag")
        subprocess.run(["xattr", "-w", "com.apple.quarantine", "0081;x;y;", os.fspath(xattr_b)], check=True)
        subprocess.run(["chmod", "+a", "everyone deny delete", os.fspath(acl_b)], check=True)
        os.chflags(flag_b, stat.UF_HIDDEN)
        inodes = {p: p.stat().st_ino for p in (xattr_b, acl_b, flag_b)}
        for p in (xattr_b, acl_b, flag_b):
            r = gd.dedupe([p.parent], self.root / f"{p.parent.name}.json", min_bytes=16 * self.KIB, strict=True)
            self.assertEqual((r["cloned_bytes"], r["refused"]), (0, 1), p.parent.name)
            self.assertEqual(p.stat().st_ino, inodes[p], p.parent.name)
        os.chflags(flag_b, 0)
        subprocess.run(["chmod", "-N", os.fspath(acl_b)], check=True)

    @apfs
    def test_a_strict_clone_refuses_a_file_someone_else_owns(self) -> None:
        frame = os.urandom(32 * self.KIB)
        a = self.blob("caps/a.png", frame)
        b = self.blob("caps/b.png", frame)
        real = os.lstat

        def lstat(p, *args, **kw):
            st = real(p, *args, **kw)
            return _Stat(st, st_uid=st.st_uid + 1) if os.fspath(p) == os.fspath(b) else st

        key, akey = gd._file_key(os.lstat(b)), gd._file_key(os.lstat(a))
        ino = b.stat().st_ino
        with mock.patch.object(gd.os, "lstat", side_effect=lstat):
            self.assertFalse(gd.clone_over(str(a), str(b), key, akey, strict=True))
        self.assertEqual(b.stat().st_ino, ino)

    @apfs
    def test_bytes_are_compared_so_a_hash_collision_is_never_cloned(self) -> None:
        a = self.blob("caps/a.png", os.urandom(32 * self.KIB))
        b = self.blob("caps/b.png", os.urandom(32 * self.KIB))
        want = b.read_bytes()
        with mock.patch.object(gd, "_sha256", return_value="same"):
            r = self.dedupe([self.root / "caps"])
        self.assertEqual((r["cloned_bytes"], r["refused"]), (0, 1))
        self.assertEqual(b.read_bytes(), want)
        self.assertNotEqual(a.read_bytes(), want)

    @apfs
    def test_files_already_sharing_a_clone_are_left_as_they_are(self) -> None:
        frame = os.urandom(32 * self.KIB)
        a = self.blob("caps/a.png", frame)
        b = self.root / "caps/b.png"
        subprocess.run(["/bin/cp", "-c", os.fspath(a), os.fspath(b)], check=True)  # deputy-2's hand dedupe
        os.utime(b, (1_000_000, 1_000_000))
        ino = b.stat().st_ino
        r = self.dedupe([self.root / "caps"])
        self.assertEqual((r["cloned_bytes"], r["shared"], r["refused"]), (0, 1, 0))
        self.assertEqual(b.stat().st_ino, ino)

    @apfs
    def test_git_dirs_inside_a_capture_root_are_not_walked(self) -> None:
        data = os.urandom(32 * self.KIB)
        self.blob("caps/.git/objects/pack/a.pack", data)
        self.blob("caps/.git/objects/pack/b.pack", data)
        self.assertEqual(self.dedupe([self.root / "caps"])["files"], 0)

    # -- a bounded pass -----------------------------------------------------------------------------

    @apfs
    def test_a_zero_budget_does_nothing_and_says_it_is_incomplete(self) -> None:
        frame = os.urandom(32 * self.KIB)
        self.blob("caps/a.png", frame)
        b = self.blob("caps/b.png", frame)
        ino = b.stat().st_ino
        r = self.dedupe([self.root / "caps"], budget_s=0)
        self.assertEqual((r["complete"], r["cloned_bytes"]), (False, 0))
        self.assertEqual(b.stat().st_ino, ino)

    @apfs
    def test_bounded_passes_each_stop_on_time_and_together_finish_every_root(self) -> None:
        """A pass stops at its budget (each clock read is one tick here) and says it is incomplete; later passes
        resume from the hashes it saved and start at the root it stopped in, so roots bigger than one pass
        cannot starve the ones after them."""
        roots, frames = [], {}
        for name in ("r1", "r2", "r3", "r4"):
            data = os.urandom(32 * self.KIB)
            for n in range(3):
                frames[self.blob(f"{name}/f{n}.png", data)] = data
            roots.append(self.root / name)
        inodes = {p: p.stat().st_ino for p in frames}
        ticks = iter(range(10**6))

        def changed() -> collections.Counter:
            return collections.Counter(p.parent.name for p in frames if p.stat().st_ino != inodes[p])

        passes = 0
        while sum(changed().values()) < 8:
            passes += 1
            self.assertLess(passes, 40)
            r = self.dedupe(roots, budget_s=10, clock=lambda: float(next(ticks)))
            self.assertFalse(r["complete"])  # 12 files never fit one 10-tick pass
            self.assertGreater(r["hashed_bytes"] + r["cloned_bytes"], 0)  # yet every pass gets somewhere
        self.assertGreater(passes, 2)
        self.assertEqual(changed(), {"r1": 2, "r2": 2, "r3": 2, "r4": 2})
        self.assertTrue(all(p.read_bytes() == d for p, d in frames.items()))


class ActivityClockTest(unittest.TestCase):
    """The hot tier ages in host CI jobs since last use, not hours; thresholds come from disk-policy.json."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.fam = gd.Family("cmux-pr-builds", self.dir, True, "x")
        self.now = time.time()

    def starts(self, *hours_ago: float) -> list[float]:
        return sorted(self.now - h * 3600 for h in hours_ago)

    def due(self, mode: str, idle: float, starts: list[float] | None, window: float = 1.0) -> bool:
        with mock.patch.object(gd, "ACTIVITY_MODE", mode), mock.patch.object(gd, "job_starts", return_value=starts), \
                mock.patch.object(gd, "running_job_hours", return_value=0.0):
            return gd.item_due(self.fam, idle, window)[0]

    def test_running_job_hours_reads_the_oldest_runner_worker(self) -> None:
        out = (" 1-02:03:04 /Users/cmux/actions-runner-glaeda-2/bin/Runner.Worker spawnclient 1 2\n"
               "   05:06 /Users/cmux/actions-runner-glaeda/bin/Runner.Worker spawnclient\n 99:00:00 /bin/zsh\n")
        with mock.patch.object(gd, "_RUNNING_JOB_HOURS", None), \
                mock.patch.object(gd.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, out, "")):
            self.assertAlmostEqual(gd.running_job_hours(), 26 + 3 / 60 + 4 / 3600, places=4)
        with mock.patch.object(gd, "_RUNNING_JOB_HOURS", None), mock.patch.object(gd.subprocess, "run", side_effect=OSError):
            self.assertEqual(gd.running_job_hours(), 24.0)

    def test_pressure_does_not_use_running_job_age_as_a_retention_window(self) -> None:
        busy = self.starts(*[i / 100 for i in range(100)])
        with mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "job_starts", return_value=busy), \
                mock.patch.object(gd, "running_job_hours", return_value=2.0):
            self.assertTrue(gd.item_due(self.fam, 1.5, 1.0)[0])  # process evidence, not job age, protects it
            self.assertTrue(gd.item_due(self.fam, 2.5, 1.0)[0])  # older than every running job

    def test_a_quiet_fleet_keeps_its_cache(self) -> None:
        # idle 30 h, but only 2 jobs ran since: not stale, nothing had a chance to use it
        self.assertTrue(self.due("pressure", 30, self.starts(1, 2)))
        self.assertFalse(self.due("sweep", 30, self.starts(1, 2)))

    def test_many_jobs_past_it_make_it_a_candidate(self) -> None:
        busy = self.starts(*[i / 100 for i in range(100)])  # 100 jobs in the last hour
        self.assertTrue(self.due("pressure", 1.5, busy))    # past pressure_jobs (80)
        self.assertFalse(self.due("sweep", 1.5, busy))      # the sweep waits for sweep_jobs (190)
        self.assertTrue(self.due("sweep", 30, busy[:90] + self.starts(29)))  # soft 24 h: idle a day, 80+ jobs

    def test_window_needs_some_activity(self) -> None:
        self.assertTrue(self.due("pressure", 2, self.starts(*[0.1] * 6)))  # past its window, min_jobs since
        self.assertTrue(self.due("pressure", 2, self.starts(0.1, 0.2)))

    def test_no_job_log_uses_bounded_idle_sweep_fallback(self) -> None:
        self.assertTrue(self.due("pressure", 2, None))
        self.assertTrue(self.due(None, 2, self.starts(0.1)))
        # Pressure may evict disposable hot state once the emergency policy allows
        # it, but an idle sweep needs a bounded wall-clock fallback when the host
        # never produced a job log.  The seven-day window matches mirror retention.
        self.assertFalse(self.due("sweep", gd.PARKED_NO_LOG_IDLE_HOURS - 1, None))
        self.assertTrue(self.due("sweep", gd.PARKED_NO_LOG_IDLE_HOURS, None))
        self.assertFalse(self.due(None, gd.PARKED_NO_LOG_IDLE_HOURS * 2, None))
        other = gd.Family("tmp", self.dir, True, "x")
        with mock.patch.object(gd, "ACTIVITY_MODE", "pressure"), mock.patch.object(gd, "job_starts", return_value=[]):
            self.assertEqual(gd.item_due(other, 2, 1.0), (True, None))

    def test_job_log_and_policy_file(self) -> None:
        log = self.dir / "jobs.jsonl"
        log.write_text('{"event":"started","at":5}\n{"event":"completed","at":6}\nnot json\n{"event":"started","at":3}\n')
        self.assertEqual(gd.job_starts(log), [3.0, 5.0])
        self.assertIsNone(gd.job_starts(self.dir / "missing"))
        policy = self.dir / "disk-policy.json"
        policy.write_text('{"pressure_jobs": 40, "defer_during_ci": true, "min_jobs": "x", "sweep_jobs": -1}')
        got = gd.disk_policy(policy)
        self.assertEqual((got["pressure_jobs"], got["defer_during_ci"], got["min_jobs"], got["sweep_jobs"]),
                         (40, True, gd.POLICY_DEFAULTS["min_jobs"], gd.POLICY_DEFAULTS["sweep_jobs"]))
        policy.write_text("[")
        self.assertEqual(gd.disk_policy(policy), gd.POLICY_DEFAULTS)

    def test_fleet_disk_thresholds_override_defaults_and_need_margins(self) -> None:
        policy = self.dir / "disk-policy.json"
        policy.write_text('{"low":"54","target":"70","emergency":"50",'
                          '"low_margin_gib":5,"target_margin_gib":21}')
        settings = gd.disk_policy(policy)
        self.assertEqual((settings["low"], settings["target"], settings["emergency"]),
                         ("54", "70", "50"))
        seen: list[tuple[str, str, float]] = []

        def fss(*args: object) -> dict:
            seen.append((args[1], args[2], args[-1]))
            return {}

        with mock.patch.object(gd, "POLICY", settings), \
                mock.patch.object(gd, "runner_floor_gib", return_value=30.0), \
                mock.patch.object(gd, "filesystems", side_effect=fss), \
                contextlib.redirect_stdout(io.StringIO()):
            gd.main(["--pressure", "--no-snapshot"])
            gd.main(["--pressure", "--need", "50", "--no-snapshot"])
        self.assertEqual(seen, [("54", "70", 30.0), ("55", "71", 50.0)])

if __name__ == "__main__":
    unittest.main()
