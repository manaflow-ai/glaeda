#!/usr/bin/env python3
"""Tests for scripts/glaeda-fleet-cas-prune."""

from __future__ import annotations

import contextlib
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("glaeda_fleet_cas_prune",
                                              os.fspath(ROOT / "scripts" / "glaeda-fleet-cas-prune"))
spec = importlib.util.spec_from_loader("glaeda_fleet_cas_prune", loader)
fp = importlib.util.module_from_spec(spec)
sys.modules["glaeda_fleet_cas_prune"] = fp
loader.exec_module(fp)


class PruneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "xcode"
        self.root.mkdir()
        self.lock = Path(self.tmp.name) / "host.lock"
        self.lock.write_text("")
        self.saved = (fp.ROOT, fp.HOST_LOCK, fp.running_commands, fp.reserved_above_floor, fp.toolchain_overlap,
                      fp.free_fraction, getattr(fp, "build_starts", None), fp.DISK_ERRORS_DIR)
        fp.ROOT, fp.HOST_LOCK = self.root, self.lock
        fp.DISK_ERRORS_DIR = Path(self.tmp.name) / "disk-errors"  # never the test host's own state
        fp.build_starts = lambda: []  # never the test host's own processes
        fp.free_fraction = lambda path: 0.5  # the test host's own free disk must not decide the gc schedule
        fp.running_commands = lambda: ["/usr/libexec/something", "zsh"]
        fp.reserved_above_floor = lambda: ""
        fp.toolchain_overlap = lambda paths: ""

    def tearDown(self) -> None:
        (fp.ROOT, fp.HOST_LOCK, fp.running_commands, fp.reserved_above_floor, fp.toolchain_overlap,
         fp.free_fraction, fp.build_starts, fp.DISK_ERRORS_DIR) = self.saved
        self.tmp.cleanup()

    def env(self, sign_key: str = "") -> None:
        (self.root / "fleet-cas.env").write_text(
            f"FLEET_CAS_STORE=100.89.140.13:7450\nFLEET_CAS_TRUSTED_KEYS=ab\nFLEET_CAS_SIGN_KEY={sign_key}\n")

    def node(self, n: int, size: int = 4096) -> list[Path]:
        paths = []
        for i in range(n):
            sub = "kv" if i % 3 == 0 else "cas"
            p = self.root / "node-store" / sub / f"{i:02x}"[:2] / f"obj{i}"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\0" * size)
            t = time.time() - (n - i) * 3600  # obj0 is the oldest
            os.utime(p, (t, t))
            paths.append(p)
        (self.root / "node-store/stats.json").write_text("{}")
        return paths

    def run_main(self, *args: str) -> dict:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(fp.main(list(args)), 0)
        return json.loads(out.getvalue())

    def test_busy_matches_builds_not_listeners(self) -> None:
        self.assertTrue(fp.busy(["/Applications/Xcode_26.6.app/Contents/Developer/usr/bin/xcodebuild -scheme cmux"]))
        self.assertTrue(fp.busy(["/Users/cmux/actions-runner-glaeda-2/bin/Runner.Worker spawnclient 1 2"]))
        self.assertTrue(fp.busy(["/Users/cmux/actions-runner-x/bin.2.337.0/Runner.Worker spawnclient 1 2"]))
        self.assertTrue(fp.busy(["/Applications/Xcode_26.6.app/Contents/SharedFrameworks/SwiftBuild.framework/"
                                 "Versions/A/PlugIns/SWBBuildService.bundle/Contents/MacOS/SWBBuildService"]))
        self.assertFalse(fp.busy(["/Users/cmux/actions-runner-glaeda/bin/Runner.Listener run",
                                  "python3 xcodebuild-log-parser.py"]))

    def test_no_fleet_cas_is_a_quiet_no_op(self) -> None:
        self.assertEqual(self.run_main("--apply")["result"], "no fleet-cas on this host")

    def test_reader_evicts_oldest_node_files_to_ninety_percent(self) -> None:
        self.env()
        paths = self.node(10)
        per = paths[0].stat().st_blocks * 512
        rec = self.run_main("--apply", "--node-gib", str(6 * per / fp.GIB))
        self.assertEqual(rec["role"], "reader")
        self.assertEqual(rec["result"], "ok")
        left = [p for p in paths if p.exists()]
        # budget 6 files, goal 90% of that: the 5 newest stay, the 5 oldest go
        self.assertEqual(left, paths[5:])
        self.assertTrue((self.root / "node-store/stats.json").exists())

    def test_under_budget_touches_nothing(self) -> None:
        self.env()
        paths = self.node(4)
        self.run_main("--apply", "--node-gib", "1")
        self.assertTrue(all(p.exists() for p in paths))

    def test_plan_only_deletes_nothing(self) -> None:
        self.env()
        paths = self.node(10)
        rec = self.run_main("--node-gib", "0.000001", "--local-cas-gib", "0")
        self.assertIn("would remove", rec["node_store"])
        self.assertTrue(all(p.exists() for p in paths))

    def test_writer_prunes_its_objects_and_keeps_its_index_entries_and_local_cas(self) -> None:
        """The writer's objects are content-addressed and written through, so they go like a reader's (cmux15's store
        reached 73 GiB, 2026-10-26). Its kv/ entries stay: the fleet store keeps the first value for a key, so a writer
        that recomputed a lost entry would serve a value the fleet does not hold. Its local CAS stays too."""
        self.env("/Users/cmux/.config/fleet-cas/sign.key")
        paths = self.node(10)  # kv: obj0, obj3, obj6, obj9; the rest cas
        (self.root / "cas/v1").mkdir(parents=True)
        (self.root / "cas/v1/blob").write_bytes(b"\0" * 8192)
        per = paths[0].stat().st_blocks * 512
        rec = self.run_main("--apply", "--node-gib", str(6 * per / fp.GIB), "--local-cas-gib", "0")
        self.assertEqual(rec["role"], "writer")
        kv = {paths[i] for i in (0, 3, 6, 9)}
        self.assertTrue(all(p.exists() for p in kv), "index entries stay")
        # 10 files, budget 6, goal 5.4: the 5 oldest objects go (obj1, obj2, obj4, obj5, obj7)
        self.assertEqual([p.name for p in paths if not p.exists()], ["obj1", "obj2", "obj4", "obj5", "obj7"])
        self.assertTrue((self.root / "cas/v1/blob").exists())

    def test_reader_local_cas_goes_whole_over_budget(self) -> None:
        self.env()
        (self.root / "cas/v1").mkdir(parents=True)
        (self.root / "cas/v1/blob").write_bytes(b"\0" * 8192)
        self.assertEqual(self.run_main("--apply", "--local-cas-gib", "0")["local_cas"], "removed")
        self.assertFalse((self.root / "cas").exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".cas")], [])

    def crash_report(self, name: str, when: float, in_plugin: bool = True) -> Path:
        reports = Path(self.tmp.name) / "DiagnosticReports"
        reports.mkdir(exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S.00 %z", time.localtime(when))
        images = [{"name": "libsystem_platform.dylib"}, {"name": fp.CAS_PLUGIN if in_plugin else "libswift.dylib"}]
        body = {"faultingThread": 1, "usedImages": images,
                "threads": [{"frames": [{"imageIndex": 0}]}, {"frames": [{"imageIndex": 0}, {"imageIndex": 1}]}]}
        path = reports / name
        path.write_text(json.dumps({"app_name": name.split("-")[0], "timestamp": stamp}) + "\n" + json.dumps(body))
        return reports

    def corrupt_cas(self) -> None:
        """A small, just-used local CAS, with a build running since before it was last touched: under budget and
        not idle, so before this change it was kept, and every build crashed on it (cmux7s, 2026-11-10 to 11-17)."""
        self.env()
        (self.root / "cas/plugin/v1.1").mkdir(parents=True)
        (self.root / "cas/plugin/lock").write_text("")
        (self.root / "cas/plugin/v1.1/v8.index").write_bytes(b"\0" * 8192)
        fp.running_commands = lambda: ["/Users/cmux/actions-runner-glaeda/bin/Runner.Worker spawnclient 1 2"]
        fp.build_starts = lambda: [time.time() - 3 * 3600]

    def test_corrupt_local_cas_goes_under_budget_while_builds_run(self) -> None:
        self.corrupt_cas()
        reports = self.crash_report("SWBBuildService-2026-11-17-010834.ips", time.time() + 5)
        with mock.patch.object(fp, "CRASH_REPORTS", reports):
            rec = self.run_main("--apply")
        self.assertTrue(rec["local_cas"].startswith("removed (corrupt: SWBBuildService-2026-11-17"), rec)
        self.assertEqual(rec["result"], "ok")
        self.assertFalse((self.root / "cas").exists())

    def test_crash_replaying_a_cached_compile_counts_as_corrupt(self) -> None:
        """ultra1, 2026-11-27: SWBBuildService aborted in the Swift scanner while replaying cached diagnostics; no
        frame of the faulting thread was in the CAS plugin, so the CAS was kept and three nightly builds crashed."""
        self.corrupt_cas()
        reports = self.crash_report("SWBBuildService-2026-11-27-074246.ips", time.time() + 5, in_plugin=False)
        path = reports / "SWBBuildService-2026-11-27-074246.ips"
        head, _, body = path.read_text().partition("\n")
        data = json.loads(body)
        data["threads"][1]["frames"].append({"imageIndex": 1, "symbol": "swiftscan_cache_replay_compilation"})
        path.write_text(head + "\n" + json.dumps(data))
        with mock.patch.object(fp, "CRASH_REPORTS", reports):
            rec = self.run_main("--apply")
        self.assertTrue(rec["local_cas"].startswith("removed (corrupt: SWBBuildService-2026-11-27"), rec)
        self.assertFalse((self.root / "cas").exists())

    def test_corrupt_local_cas_waits_for_its_lock(self) -> None:
        self.corrupt_cas()
        reports = self.crash_report("SWBBuildService-2026-11-17-010834.ips", time.time() + 5)
        fd = os.open(self.root / "cas/plugin/lock", os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            with mock.patch.object(fp, "CRASH_REPORTS", reports):
                rec = self.run_main("--apply")
        finally:
            os.close(fd)
        self.assertIn("holds the CAS lock", rec["result"])
        self.assertTrue((self.root / "cas/plugin/v1.1/v8.index").exists())

    def test_crash_before_the_cas_or_outside_the_plugin_keeps_it(self) -> None:
        self.corrupt_cas()
        reports = self.crash_report("SWBBuildService-old.ips", time.time() - 3600)
        self.crash_report("swift-frontend-new.ips", time.time() + 5, in_plugin=False)
        self.crash_report("bsdtar-new.ips", time.time() + 5)  # not a CAS client
        with mock.patch.object(fp, "CRASH_REPORTS", reports):
            rec = self.run_main("--apply")
        self.assertEqual(rec["local_cas"], "within budget")
        self.assertTrue((self.root / "cas/plugin/v1.1/v8.index").exists())

    def test_malformed_crash_report_is_unreadable_not_fatal(self) -> None:
        self.corrupt_cas()
        reports = self.crash_report("SWBBuildService-good.ips", time.time() - 3600)  # before the CAS: not counted
        stamp = time.strftime("%Y-%m-%d %H:%M:%S.00 %z", time.localtime(time.time() + 5))
        (reports / "SWBBuildService-partial.ips").write_text(
            json.dumps({"timestamp": stamp}) + "\n" + json.dumps({"threads": ["x"], "usedImages": ["y"]}))
        self.assertEqual(fp.corrupt_local_cas(self.root / "cas", reports), "")

    def test_lookback_reads_the_report_time_not_its_mtime(self) -> None:
        """macOS rewrites old reports when it submits them, so a fresh mtime says nothing about when Xcode crashed."""
        self.corrupt_cas()
        reports = self.crash_report("SWBBuildService-resubmitted.ips", time.time() + 5)
        later = time.time() + 30 * 86400
        os.utime(reports / "SWBBuildService-resubmitted.ips", (later, later))
        self.assertEqual(fp.corrupt_local_cas(self.root / "cas", reports, now=later), "")
        self.assertTrue(fp.corrupt_local_cas(self.root / "cas", reports).startswith("corrupt: "))

    def test_leftover_renamed_cas_is_swept(self) -> None:
        self.env()
        left = self.root / ".cas.pruning-4242/v1"
        left.mkdir(parents=True)
        (left / "blob").write_bytes(b"\0" * 8192)
        rec = self.run_main("--apply")
        self.assertGreater(rec["swept_bytes"], 0)
        self.assertFalse((self.root / ".cas.pruning-4242").exists())

    def test_nothing_to_delete_never_takes_the_lock(self) -> None:
        self.env()
        self.node(3)
        held = os.open(self.lock, os.O_RDONLY)
        try:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.run_main("--apply", "--node-gib", "1")["result"], "ok")
        finally:
            os.close(held)

    def eio_on_lstat(self, bad: set[str], unlink_too: bool = False):
        """Patch the module's os.lstat (and optionally os.unlink) so each path in BAD raises EIO, as a failing drive
        does (cmux-lawrence's X10 Pro, 2026-11-27)."""
        real_lstat, real_unlink = fp.os.lstat, fp.os.unlink

        def lstat(path, *a, **k):
            if os.fspath(path) in bad:
                raise OSError(5, "Input/output error", os.fspath(path))
            return real_lstat(path, *a, **k)

        def unlink(path, *a, **k):
            if unlink_too and os.fspath(path) in bad:
                raise OSError(5, "Input/output error", os.fspath(path))
            return real_unlink(path, *a, **k)

        fp.os.lstat, fp.os.unlink = lstat, unlink
        self.addCleanup(setattr, fp.os, "lstat", real_lstat)
        self.addCleanup(setattr, fp.os, "unlink", real_unlink)

    def disk_errors(self, service: str = "fleet-cas-prune") -> dict:
        return json.loads((fp.DISK_ERRORS_DIR / f"{service}.json").read_text())

    def test_a_node_file_whose_stat_reads_eio_is_skipped_counted_and_reported(self) -> None:
        self.env()
        paths = self.node(10)
        per = paths[0].stat().st_blocks * 512
        self.eio_on_lstat({os.fspath(paths[0]), os.fspath(paths[7])})
        rec = self.run_main("--apply", "--node-gib", str(6 * per / fp.GIB))
        self.assertEqual(rec["result"], "ok")
        self.assertEqual(rec["unreadable_entries"], 2)
        self.assertTrue(paths[0].exists(), "an entry that could not be read is never judged, so never removed")
        self.assertFalse(paths[1].exists(), "the readable oldest still go")
        report = self.disk_errors()
        self.assertEqual((report["schema"], report["service"], report["unreadable"]),
                         ("glaeda-disk-errors/v1", "fleet-cas-prune", 2))
        self.assertIn("errno 5", report["examples"][0])

    def test_an_unlink_that_reads_eio_does_not_end_the_cap(self) -> None:
        self.env()
        paths = self.node(10)
        per = paths[0].stat().st_blocks * 512
        real_unlink = fp.os.unlink

        def unlink(path, *a, **k):
            if os.fspath(path) == os.fspath(paths[0]):
                raise OSError(5, "Input/output error", os.fspath(path))
            return real_unlink(path, *a, **k)

        fp.os.unlink = unlink
        self.addCleanup(setattr, fp.os, "unlink", real_unlink)
        rec = self.run_main("--apply", "--node-gib", str(6 * per / fp.GIB))
        self.assertEqual((rec["result"], rec["unreadable_entries"]), ("ok", 1))
        self.assertEqual([p for p in paths[:5] if p.exists()], [paths[0]])  # the other four oldest went

    def test_a_clean_run_reports_zero_and_a_deferred_run_keeps_the_last_count(self) -> None:
        self.env()
        self.node(3)
        self.run_main("--apply")
        self.assertEqual(self.disk_errors()["unreadable"], 0)
        (fp.DISK_ERRORS_DIR / "fleet-cas-prune.json").write_text(json.dumps({"unreadable": 4}))
        fp.reserved_above_floor = lambda: "reserved"  # deferred before any walk
        self.assertTrue(self.run_main("--apply")["result"].startswith("deferred"))
        self.assertEqual(self.disk_errors()["unreadable"], 4)

    def test_a_vanished_file_is_a_race_not_a_disk_error(self) -> None:
        fp.DISK_ERRORS.update(count=0, examples=[], walked=False)
        fp.disk_error(FileNotFoundError(2, "No such file or directory", "x"))
        self.assertEqual(fp.DISK_ERRORS["count"], 0)
        fp.disk_error(OSError(5, "Input/output error", "x"))
        self.assertEqual(fp.DISK_ERRORS["count"], 1)

    def test_store_gc_counts_the_entries_gc_skipped_as_unreadable(self) -> None:
        log = self.fake_store(0)
        binary = self.root / "bin/fleet-cas"
        binary.write_text(binary.read_text().replace(
            'object: 0\"', 'object: 0; unreadable entries skipped: 3\"'))
        fp.DISK_ERRORS.update(count=0, examples=[], walked=False)
        rec = fp.store_gc(self.root, 14)
        self.assertEqual(rec["gc"], "ran")
        self.assertEqual(fp.DISK_ERRORS["count"], 3)
        self.assertTrue(fp.DISK_ERRORS["walked"])
        self.assertEqual(len(log.read_text().splitlines()), 2)

    def fake_store(self, no_roots: int) -> Path:
        for sub in ("kv", "cas"):
            (self.root / "fleet-store" / sub).mkdir(parents=True)
        (self.root / "bin").mkdir()
        log = self.root / "gc.calls"
        binary = self.root / "bin/fleet-cas"
        binary.write_text("#!/bin/sh\n"
                          f"echo \"$*\" >> {log}\n"
                          "case \"$*\" in *--dry-run*) p='dry run: ';; *) p='';; esac\n"
                          f"echo \"${{p}}kv kept 5 deleted 1; cas kept 9 deleted 2 (4096 bytes); "
                          f"kept entries naming no stored object: {no_roots}\"\n")
        binary.chmod(0o755)
        return log

    def test_store_gc_dry_runs_first_and_runs_daily(self) -> None:
        log = self.fake_store(0)
        rec = fp.store_gc(self.root, 14)
        self.assertEqual(rec["gc"], "ran")
        calls = log.read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertIn("--dry-run", calls[0])
        self.assertNotIn("--dry-run", calls[1])
        self.assertIn("--keep-days 14", calls[1])
        self.assertEqual(fp.store_gc(self.root, 14), {"gc": "not due"})
        self.assertEqual(fp.store_gc(self.root, 14, now=time.time() + fp.GC_EVERY + 1)["gc"], "ran")

    def test_store_gc_refuses_when_entries_name_no_object(self) -> None:
        log = self.fake_store(3)
        rec = fp.store_gc(self.root, 14)
        self.assertTrue(rec["gc"].startswith("skipped: kept entries name no stored object"))
        self.assertEqual(len(log.read_text().splitlines()), 1)  # only the dry run

    def test_store_gc_under_disk_pressure_keeps_fewer_days_more_often(self) -> None:
        log = self.fake_store(0)
        rec = fp.store_gc(self.root, 14, free=0.10)
        self.assertEqual((rec["gc"], rec["gc_keep_days"]), ("ran", 7))
        self.assertIn("--keep-days 7", log.read_text().splitlines()[-1])
        later = time.time() + fp.GC_EVERY_PRESSED + 1
        self.assertEqual(fp.store_gc(self.root, 14, now=later, free=0.5), {"gc": "not due"})  # no pressure: daily
        self.assertEqual(fp.store_gc(self.root, 4, now=later, free=0.10)["gc_keep_days"], fp.GC_MIN_KEEP_DAYS)

    def test_store_gc_only_runs_gc_without_the_mini_guards(self) -> None:
        """The store host's agent: gc only, no env file needed, and neither glaeda-disk guard is consulted."""
        log = self.fake_store(0)
        fp.reserved_above_floor = mock.Mock(side_effect=AssertionError("mini guard consulted"))
        fp.toolchain_overlap = mock.Mock(side_effect=AssertionError("mini guard consulted"))
        fp.ROOT = Path("/Volumes/glaeda-fleet-cas")
        with mock.patch.object(fp, "store_gc", return_value={"gc": "ran"}) as gc:
            rec = self.run_main("--apply", "--store-gc-only")
        gc.assert_called_once_with(fp.ROOT, 14)
        self.assertEqual((rec["gc"], rec["result"]), ("ran", "ok"))
        self.assertNotIn("node_store", rec)
        self.assertEqual(log.read_text() if log.exists() else "", "")

    def test_store_gc_only_refuses_a_root_outside_volumes(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(fp.main(["--apply", "--store-gc-only"]), 2)
        self.assertTrue(json.loads(out.getvalue())["result"].startswith("refused"))

    def test_no_store_no_gc(self) -> None:
        self.assertEqual(fp.store_gc(self.root, 14), {})

    def local_cas(self) -> Path:
        blob = self.root / "cas/v1/blob"
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(b"\0" * 8192)
        return blob

    def test_the_node_cap_is_hard_while_a_build_runs(self) -> None:
        """A busy mini always has a build running, so the node store is pruned anyway, oldest first, down to 90% of
        the cap whatever the files' age (a file a build still wants is fetched again). The local CAS, which a build has
        open, waits for no build."""
        self.env()
        paths = self.node(10, size=4096)
        for p in paths[5:]:  # used just now
            os.utime(p, None)
        blob = self.local_cas()
        per = paths[0].stat().st_blocks * 512
        fp.running_commands = lambda: ["/Users/cmux/actions-runner-glaeda/bin/Runner.Worker spawnclient 1 2"]
        rec = self.run_main("--apply", "--node-gib", str(2 * per / fp.GIB), "--local-cas-gib", "0")
        self.assertTrue(rec["result"].startswith("deferred: a build is running"), rec)
        self.assertTrue(rec["local_cas"].startswith("deferred: a build is running"))
        left = [p for p in paths if p.exists()]
        self.assertLessEqual(len(left) * per, 2 * per * 0.9 + per, "under the cap, recent files or not")
        self.assertIn("removed", rec["node_store"])
        self.assertTrue(blob.exists())

    def test_the_local_cas_defers_while_ps_fails_or_a_build_holds_the_lock(self) -> None:
        self.env()
        blob = self.local_cas()
        fp.running_commands = lambda: None
        self.assertTrue(self.run_main("--apply", "--local-cas-gib", "0")["result"].startswith("deferred: ps failed"))
        fp.running_commands = lambda: []
        held = os.open(self.lock, os.O_RDWR)
        try:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.run_main("--apply", "--local-cas-gib", "0")["result"],
                             "deferred: a build holds the host lock")
        finally:
            os.close(held)
        self.assertTrue(blob.exists())

    RUNNER = ["/Users/cmux/actions-runner-glaeda-8/bin.2.338.2/Runner.Worker spawnclient 151 154"]

    def plugin_cas(self, age_s: float) -> Path:
        """An Xcode plugin CAS like cmux9s's (its lock, index, data pool, a standalone leaf), last written and read
        age_s ago."""
        plugin = self.root / "cas/plugin"
        (plugin / "v1.1").mkdir(parents=True)
        files = [plugin / "lock", plugin / "v1.1/v8.index", plugin / "v1.1/v8.data", plugin / "v1.1/v8.4096.leaf"]
        for f in files:
            f.write_bytes(b"\0" * 8192)
        t = time.time() - age_s
        for p in [*files, plugin / "v1.1", plugin, self.root / "cas"]:
            os.utime(p, (t, t))
        return plugin

    def assert_cas_kept(self, rec: dict, why: str) -> None:
        self.assertTrue(rec["local_cas"].startswith(why), rec)
        self.assertEqual(rec["result"], rec["local_cas"])
        self.assertTrue((self.root / "cas/plugin/v1.1/v8.index").exists())

    def test_an_idle_local_cas_goes_while_unrelated_builds_run(self) -> None:
        """hq#11385: cmux9s always runs runner jobs, so its local CAS (29 GiB, last used a day and a half before,
        cap 20 GiB) was never pruned and the host sat under its disk floor. A CAS that no process has open and that
        was last used before the oldest running build started, outside the quiet window, goes while they run. Each
        runner job holds the host lock shared for its whole run (cmux9s after #1998: "deferred: a build holds the
        host lock"), so that hold must not keep it."""
        self.env()
        self.plugin_cas(2 * 86400)
        fp.running_commands = lambda: self.RUNNER
        fp.build_starts = lambda: [time.time() - 3 * 3600, time.time() - 60]
        job = os.open(self.lock, os.O_RDONLY)
        try:
            fcntl.flock(job, fcntl.LOCK_SH)  # the runner hook's job-long hold
            rec = self.run_main("--apply", "--local-cas-gib", "0")
        finally:
            os.close(job)
        self.assertEqual((rec["local_cas"], rec["result"]), ("removed", "ok"), rec)
        self.assertFalse((self.root / "cas").exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".cas")], [])

    def test_a_fleet_build_holding_the_host_lock_keeps_an_idle_local_cas(self) -> None:
        """A fleet build (with-host-lock) holds the host lock exclusive: the CAS waits for it even when idle."""
        self.env()
        self.plugin_cas(2 * 86400)
        fp.running_commands = lambda: self.RUNNER
        fp.build_starts = lambda: [time.time() - 60]
        build = os.open(self.lock, os.O_RDONLY)
        try:
            fcntl.flock(build, fcntl.LOCK_EX)
            self.assert_cas_kept(self.run_main("--apply", "--local-cas-gib", "0"),
                                 "deferred: a build holds the host lock")
        finally:
            os.close(build)

    def test_a_local_cas_used_since_the_oldest_build_started_stays(self) -> None:
        """Used 2 h ago, outside the 60 min quiet window, but by a build that may still be running (started 3 h
        ago): it stays."""
        self.env()
        self.plugin_cas(2 * 3600)
        fp.running_commands = lambda: self.RUNNER
        fp.build_starts = lambda: [time.time() - 3 * 3600]
        self.assert_cas_kept(self.run_main("--apply", "--local-cas-gib", "0"), "deferred: a build is running")

    def test_a_local_cas_used_in_the_quiet_window_stays(self) -> None:
        """Read 10 min ago, before the only build (1 min old) started: still inside the quiet window, so it stays."""
        self.env()
        plugin = self.plugin_cas(2 * 86400)
        t = time.time() - 600
        os.utime(plugin / "v1.1/v8.4096.leaf", (t, time.time() - 2 * 86400))  # a read moves only the atime
        fp.running_commands = lambda: self.RUNNER
        fp.build_starts = lambda: [time.time() - 60]
        self.assert_cas_kept(self.run_main("--apply", "--local-cas-gib", "0"), "deferred: a build is running")
        # a 5 min window: the read is older than both the window and the build, so it goes
        rec = self.run_main("--apply", "--local-cas-gib", "0", "--local-cas-quiet-min", "5")
        self.assertEqual(rec["local_cas"], "removed", rec)

    def test_an_open_local_cas_stays_build_or_not(self) -> None:
        """A process holding the CAS's own lock (LLVM holds it shared while the CAS is open) keeps it, even with no
        build process seen and nothing touched for days."""
        self.env()
        plugin = self.plugin_cas(2 * 86400)
        held = os.open(plugin / "lock", os.O_RDONLY)
        try:
            fcntl.flock(held, fcntl.LOCK_SH)
            for commands in ([], self.RUNNER):
                fp.running_commands = lambda commands=commands: commands
                fp.build_starts = lambda: [time.time() - 60]
                self.assert_cas_kept(self.run_main("--apply", "--local-cas-gib", "0"),
                                     "deferred: a process has the local CAS open")
        finally:
            os.close(held)
        self.assertEqual(self.run_main("--apply", "--local-cas-gib", "0")["local_cas"], "removed")

    def test_unreadable_build_starts_keep_the_local_cas(self) -> None:
        self.env()
        self.plugin_cas(2 * 86400)
        fp.running_commands = lambda: self.RUNNER
        fp.build_starts = lambda: None
        self.assert_cas_kept(self.run_main("--apply", "--local-cas-gib", "0"), "deferred: a build is running")

    def test_parse_etime(self) -> None:
        self.assertEqual(fp.parse_etime("04:59"), 299)
        self.assertEqual(fp.parse_etime("1:02:03"), 3723)
        self.assertEqual(fp.parse_etime("2-01:00:00"), 2 * 86400 + 3600)
        self.assertIsNone(fp.parse_etime("soon"))
        self.assertIsNone(fp.parse_etime("x-01:00"))

    def test_node_only_caps_the_node_store_and_leaves_the_rest(self) -> None:
        self.env()
        paths = self.node(10)
        blob = self.local_cas()
        left = self.root / ".cas.pruning-4242/v1"
        left.mkdir(parents=True)
        rec = self.run_main("--apply", "--node-only", "--node-gib", "0", "--local-cas-gib", "0")
        self.assertEqual(rec["result"], "ok")
        self.assertEqual(rec["local_cas"], "not judged (--node-only)")
        self.assertEqual([p for p in paths if p.exists()], [])
        self.assertTrue(blob.exists())
        self.assertTrue(left.exists(), "no sweep")
        self.assertNotIn("gc", rec)


    def test_a_reserved_host_above_its_floor_is_left_alone(self) -> None:
        self.env()
        paths = self.node(10)
        fp.reserved_above_floor = lambda: "reserved for someone until 2026-10-28T23:59:59Z (r1); 80 GiB free, floor 30"
        rec = self.run_main("--apply", "--node-gib", "0")
        self.assertTrue(rec["result"].startswith("deferred: reserved for someone"), rec)
        self.assertTrue(all(p.exists() for p in paths))
        self.assertIn("would remove", self.run_main("--node-gib", "0")["node_store"], "a plan still reports")

    def test_a_node_store_aliasing_the_job_toolchain_is_never_pruned(self) -> None:
        """A node-store/cas that leads into ~/.rustup (a symlink) is the job toolchain: nothing is unlinked, even over
        the hard cap, and a glaeda-disk that cannot be asked prunes nothing either."""
        real = self.saved[4]
        home = Path(self.tmp.name) / "home"
        toolchain = home / ".rustup/toolchains/1.98.1-aarch64-apple-darwin/bin"
        toolchain.mkdir(parents=True)
        (toolchain / "rustc").write_bytes(b"\0" * 4096)
        (self.root / "node-store").mkdir()
        (self.root / "node-store/cas").symlink_to(home / ".rustup")
        self.env()
        gd = fp.load_glaeda_disk(ROOT / "scripts/glaeda-disk")
        gd.HOME = home
        fp.toolchain_overlap = lambda paths: real(paths, gd)
        rec = self.run_main("--apply", "--node-gib", "0", "--node-only")
        self.assertIn("overlaps the job toolchain", rec["result"])
        self.assertTrue((toolchain / "rustc").exists())
        (self.root / "node-store/cas").unlink()
        self.assertEqual(real([self.root, self.root / "node-store/cas"], gd), "")
        with mock.patch.object(fp.importlib.machinery, "SourceFileLoader", side_effect=OSError("gone")):
            self.assertIn("nothing pruned", real([self.root]))

    def test_reserved_above_floor_reads_glaeda_disk_and_fails_closed(self) -> None:
        real = self.saved[3]
        with mock.patch.object(fp.importlib.machinery, "SourceFileLoader", side_effect=OSError("gone")):
            self.assertIn("kept as reserved", real())
        # the real glaeda-disk loads (its dataclasses need the module in sys.modules) and answers from a made-up list
        gd = fp.load_glaeda_disk(ROOT / "scripts/glaeda-disk")
        listing = Path(self.tmp.name) / "fleet-reservations.json"
        listing.write_text(json.dumps({"expires": "2999-01-01T00:00:00Z", "reservations": [{"id": "r9", "owner": "someone", "hosts": ["cmux30"],
                                                         "from": "2000-01-01T00:00:00Z",
                                                         "until": "2999-01-01T00:00:00Z"}]}))
        gd.FLEET_RESERVATIONS = listing
        gd.host_names = lambda: {"cmux30"}
        gd.runner_floor_gib = lambda: 0.0
        self.assertIn("reserved for someone", real(gd))
        gd.host_names = lambda: {"cmux31"}
        gd.HOST_RESERVATION_LOCAL = Path(self.tmp.name) / "no-reservation.json"
        self.assertEqual(real(gd), "")
        listing.unlink()
        self.assertIn("kept as reserved", real(gd))

    def test_a_reserved_host_never_runs_store_gc(self) -> None:
        fp.reserved_above_floor = lambda: "reserved for someone until 2999-01-01T00:00:00Z (r9); 80 GiB free, floor 30"
        with mock.patch.object(fp, "store_gc", side_effect=AssertionError("gc ran on a reserved host")):
            rec = self.run_main("--apply")
        self.assertTrue(rec["result"].startswith("deferred: reserved"), rec)


    def test_in_flight_daemon_temporaries_are_never_removed(self) -> None:
        self.env()
        paths = self.node(6)
        shard = paths[1].parent
        tmp = shard / f"{paths[1].name}.tmp4242.7"  # fleet-cas write_atomic: name.tmp<pid>.<seq>, renamed when done
        tmp.write_bytes(b"\0" * (1 << 20))
        recent = time.time() - 60  # a write in progress: its file was touched a minute ago
        os.utime(tmp, (recent, recent))
        self.assertNotIn(str(tmp), [p for _, _, p in fp.node_files(self.root / "node-store")])
        self.run_main("--apply", "--node-gib", "0")
        self.assertTrue(tmp.exists(), "an in-flight write was unlinked")
        self.assertFalse(any(p.exists() for p in paths if "/cas/" in str(p)), "the finished objects still go")

    def test_an_abandoned_temporary_counts_and_goes_first(self) -> None:
        self.env()
        paths = self.node(3)
        tmp = paths[1].parent / f"{paths[1].name}.tmp99.1"
        tmp.write_bytes(b"\0" * 4096)
        old = time.time() - fp.TMP_STALE_S - 60
        os.utime(tmp, (old, old))
        listed = fp.node_files(self.root / "node-store")
        self.assertEqual(min(listed)[2], str(tmp), "oldest of all")
        self.run_main("--apply", "--node-gib", "0")
        self.assertFalse(tmp.exists())

    def test_node_only_caps_even_when_ps_fails(self) -> None:
        self.env()
        paths = self.node(6)
        fp.running_commands = lambda: None
        rec = self.run_main("--apply", "--node-only", "--node-gib", "0")
        self.assertIn("removed", rec["node_store"], rec)
        self.assertFalse(any(p.exists() for p in paths if "/cas/" in str(p)))
        rec = self.run_main("--apply", "--node-gib", "0")
        self.assertTrue(rec["result"].startswith("deferred: ps failed"), rec)

    def test_cap_only_caps_both_stores_with_no_sweep_or_gc(self) -> None:
        self.env()
        paths = self.node(6)
        blob = self.local_cas()
        left = self.root / ".cas.pruning-4242/v1"
        left.mkdir(parents=True)
        rec = self.run_main("--apply", "--cap-only", "--node-gib", "0", "--local-cas-gib", "0")
        self.assertEqual((rec["result"], rec["local_cas"]), ("ok", "removed"), rec)
        self.assertEqual(rec["local_cas_bytes"], 8192)
        self.assertEqual(rec["node_store_freed_bytes"], rec["node_store_bytes"])
        self.assertFalse(any(p.exists() for p in paths) or blob.exists())
        self.assertTrue(left.exists(), "no sweep")
        self.assertNotIn("gc", rec)
        self.assertNotIn("swept_bytes", rec)

    def test_cap_only_still_caps_the_node_store_when_ps_fails_and_keeps_the_local_cas(self) -> None:
        self.env()
        paths = self.node(6)
        blob = self.local_cas()
        fp.running_commands = lambda: None
        rec = self.run_main("--apply", "--cap-only", "--node-gib", "0", "--local-cas-gib", "0")
        self.assertIn("removed", rec["node_store"], rec)
        self.assertFalse(any(p.exists() for p in paths))
        self.assertTrue(rec["local_cas"].startswith("deferred: ps failed"), rec)
        self.assertEqual(rec["result"], rec["local_cas"])
        self.assertTrue(blob.exists())

    def test_node_only_reports_the_local_cas_size_without_judging_it(self) -> None:
        self.env()
        blob = self.local_cas()
        rec = self.run_main("--apply", "--node-only", "--local-cas-gib", "0")
        self.assertEqual((rec["local_cas"], rec["local_cas_bytes"]), ("not judged (--node-only)", 8192))
        self.assertTrue(blob.exists())


    def test_a_reserved_host_above_its_floor_is_left_alone(self) -> None:
        self.env()
        paths = self.node(10)
        fp.reserved_above_floor = lambda: "reserved for someone until 2026-10-28T23:59:59Z (r1); 80 GiB free, floor 30"
        rec = self.run_main("--apply", "--node-gib", "0")
        self.assertTrue(rec["result"].startswith("deferred: reserved for someone"), rec)
        self.assertTrue(all(p.exists() for p in paths))
        self.assertIn("would remove", self.run_main("--node-gib", "0")["node_store"], "a plan still reports")

    def test_reserved_above_floor_reads_glaeda_disk_and_fails_closed(self) -> None:
        real = self.saved[3]
        with mock.patch.object(fp.importlib.machinery, "SourceFileLoader", side_effect=OSError("gone")):
            self.assertIn("kept as reserved", real())
        # the real glaeda-disk loads (its dataclasses need the module in sys.modules) and answers from a made-up list
        gd = fp.load_glaeda_disk(ROOT / "scripts/glaeda-disk")
        listing = Path(self.tmp.name) / "fleet-reservations.json"
        listing.write_text(json.dumps({"expires": "2999-01-01T00:00:00Z", "reservations": [{"id": "r9", "owner": "someone", "hosts": ["cmux30"],
                                                         "from": "2000-01-01T00:00:00Z",
                                                         "until": "2999-01-01T00:00:00Z"}]}))
        gd.FLEET_RESERVATIONS = listing
        gd.host_names = lambda: {"cmux30"}
        gd.runner_floor_gib = lambda: 0.0
        self.assertIn("reserved for someone", real(gd))
        gd.host_names = lambda: {"cmux31"}
        gd.HOST_RESERVATION_LOCAL = Path(self.tmp.name) / "no-reservation.json"
        self.assertEqual(real(gd), "")
        listing.unlink()
        self.assertIn("kept as reserved", real(gd))

    def test_a_reserved_host_never_runs_store_gc(self) -> None:
        fp.reserved_above_floor = lambda: "reserved for someone until 2999-01-01T00:00:00Z (r9); 80 GiB free, floor 30"
        with mock.patch.object(fp, "store_gc", side_effect=AssertionError("gc ran on a reserved host")):
            rec = self.run_main("--apply")
        self.assertTrue(rec["result"].startswith("deferred: reserved"), rec)


if __name__ == "__main__":
    unittest.main()
