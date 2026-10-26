#!/usr/bin/env python3
"""Tests for scripts/glaeda-daemon-heal and its desired state, config/macos-daemons.json."""

from __future__ import annotations

import copy
import gzip
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "macos-daemons.json"


def load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, os.fspath(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    sys.modules[name] = module
    loader.exec_module(module)
    return module


heal = load("glaeda_daemon_heal", ROOT / "scripts" / "glaeda-daemon-heal")
POLICY = heal.load_policy(POLICY_PATH)
FSEV = next(d for d in POLICY["daemons"] if d["name"] == "fseventsd")
WRM = next(d for d in POLICY["daemons"] if d["name"] == "WirelessRadioManagerd")
T0 = 1_790_500_000.0


class FakeHost(heal.Host):
    """Processes from a table; CPU time grows by `rate` cores per second of fake clock."""

    def __init__(self) -> None:
        self.t = T0
        self.procs: dict[int, list] = {}  # pid -> [uid, cpu, exe, rate]
        self.signals: list[tuple[int, int]] = []
        self.respawn: dict[str, int] = {}
        self.survives_term = False
        self.sizes: dict[int, int] = {}  # pid -> footprint bytes
        self.asked: list[list[int]] = []

    def add(self, pid: int, exe: str, rate: float, uid: int = 0) -> None:
        self.procs[pid] = [uid, 0.0, exe, rate]

    def advance(self, seconds: float) -> None:
        self.t += seconds
        for row in self.procs.values():
            row[1] += row[3] * seconds

    def now(self) -> float:
        return self.t

    def processes(self):
        return [(pid, r[0], r[1], r[2]) for pid, r in self.procs.items()]

    def footprints(self, pids):
        self.asked.append(list(pids))
        return {pid: self.sizes[pid] for pid in pids if pid in self.sizes and pid in self.procs}

    def signal(self, pid: int, sig: int) -> bool:
        self.signals.append((pid, sig))
        if pid in self.procs and (sig == signal.SIGKILL or not self.survives_term):
            exe = self.procs.pop(pid)[2]
            if exe in self.respawn:
                self.add(self.respawn.pop(exe), exe, 0.01)
        return True

    def sleep(self, seconds: float) -> None:
        pass


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.state: dict = {}
        self.policy = copy.deepcopy(POLICY)

    def tick(self, seconds: float | None = None, apply: bool = True) -> dict:
        self.host.advance(self.policy["every_s"] if seconds is None else seconds)
        self.state, report = heal.reconcile(self.host, self.policy, self.state, apply)
        return report

    def run_for(self, hours: float, apply: bool = True) -> list[dict]:
        actions: list[dict] = []
        steps = int(hours * 3600 / self.policy["every_s"])
        for _ in range(steps):
            actions += self.tick(apply=apply)["actions"]
        return actions


class Policy(unittest.TestCase):
    def test_shipped_policy_is_valid_and_names_the_seen_daemons(self) -> None:
        self.assertEqual({d["name"] for d in POLICY["daemons"]}, {"fseventsd", "audiomxd", "WirelessRadioManagerd"})
        self.assertTrue(FSEV["wait_idle"], "a fseventsd restart drops live FSEvents streams: only on an idle host")

    def test_only_system_executables_can_be_named(self) -> None:
        for path in ("/opt/homebrew/bin/fseventsd", "/Users/cmux/fseventsd", "/usr/libexec/../../tmp/fseventsd",
                     "/usr/libexec/other"):
            bad = copy.deepcopy(POLICY)
            bad["daemons"] = [{"name": "fseventsd", "path": path, "wait_idle": True}]
            with self.assertRaises(heal.HealError, msg=path):
                heal.validate_policy(bad)

    def test_numbers_are_validated(self) -> None:
        for key, value in (("hot_hours", 0), ("hot_cores", "1"), ("every_s", True), ("busy_hot_hours", 1),
                           ("footprint_gib", 0), ("footprint_gib", None)):
            bad = copy.deepcopy(POLICY)
            bad[key] = value
            with self.assertRaises(heal.HealError, msg=key):
                heal.validate_policy(bad)

    def test_no_limit_is_a_literal_in_the_script(self) -> None:
        """Every limit comes from the policy: the script names each key, never a number for it."""
        text = (ROOT / "scripts" / "glaeda-daemon-heal").read_text()
        for key in heal.POLICY_KEYS:
            self.assertNotRegex(text, rf"\b{key.upper()}\s*=\s*\d", key)
        self.assertNotRegex(text, r"hot_hours\"\]\s*[*]\s*3600\s*[<>]")
        self.assertNotIn("fseventsd\"", text.split("def validate_policy")[1])  # no daemon is special-cased

    def test_cpu_time_parsing(self) -> None:
        self.assertEqual(heal.cpu_seconds("1891:53.94"), 1891 * 60 + 53.94)
        self.assertEqual(heal.cpu_seconds("0:12.59"), 12.59)
        self.assertEqual(heal.cpu_seconds("1-02:03:04"), ((24 + 2) * 60 + 3) * 60 + 4)
        self.assertIsNone(heal.cpu_seconds("-"))

    def test_footprint_parsing(self) -> None:
        self.assertEqual(heal.footprint_bytes("102G"), 102 << 30)
        self.assertEqual(heal.footprint_bytes("1264M"), 1264 << 20)
        self.assertEqual(heal.footprint_bytes("31M+"), 31 << 20)
        self.assertEqual(heal.footprint_bytes("4096K-"), 4096 << 10)
        self.assertEqual(heal.footprint_bytes("12B"), 12)
        self.assertEqual(heal.footprint_bytes("1.5T"), int(1.5 * (1 << 40)))
        for bad in ("", "-", "MEM", "12X", "G"):
            self.assertIsNone(heal.footprint_bytes(bad), bad)

    def test_host_reads_footprints_from_top_for_the_asked_pids_only(self) -> None:
        top = ("Processes: 900 total\nPhysMem: 37G used (3421M wired, 11G compressor), 11G unused.\n\n"
               "PID  MEM  \n427  102G \n1    31M+ \n9    5M\n")
        calls = []

        def fake_sh(argv, timeout=15):
            calls.append(argv)
            return 0, top, ""

        real = heal.sh
        heal.sh = fake_sh
        try:
            self.assertEqual(heal.Host().footprints([1, 427]), {427: 102 << 30, 1: 31 << 20})
            self.assertEqual(heal.Host().footprints([]), {})
        finally:
            heal.sh = real
        self.assertEqual(len(calls), 1, "no pids: top is not run")
        heal.sh = lambda argv, timeout=15: (1, top, "top: failure")
        try:
            self.assertEqual(heal.Host().footprints([427]), {}, "a failed top measures nothing, whatever it printed")
        finally:
            heal.sh = real
        self.assertEqual(calls[0][:2], ["/usr/bin/top", "-l"])
        self.assertEqual([calls[0][i + 1] for i, a in enumerate(calls[0]) if a == "-pid"], ["1", "427"])


class Reconcile(Base):
    def test_hot_daemon_on_an_idle_host_restarts_at_hot_hours_and_not_before(self) -> None:
        self.host.add(421, FSEV["path"], 1.0)
        self.host.respawn[FSEV["path"]] = 9000
        early = self.run_for(self.policy["hot_hours"] - 0.1)
        self.assertEqual(early, [])
        self.assertEqual(self.tick()["hot"][0]["name"], "fseventsd")
        actions = self.run_for(0.2)
        self.assertEqual([a["name"] for a in actions], ["fseventsd"])
        self.assertTrue(actions[0]["ok"])
        self.assertIn("launchd started pid 9000", actions[0]["action"])
        self.assertEqual(self.host.signals, [(421, signal.SIGTERM)])
        self.assertEqual(self.tick()["hot"], [], "the fresh daemon is cool")

    def test_the_limit_follows_the_policy(self) -> None:
        self.policy["hot_hours"] = 0.5
        self.host.add(421, FSEV["path"], 1.0)
        self.assertEqual(len(self.run_for(0.6)), 1)

    def test_a_cool_interval_ends_the_streak(self) -> None:
        self.host.add(421, FSEV["path"], 1.0)
        self.run_for(self.policy["hot_hours"] - 0.2)
        self.host.procs[421][3] = 0.1
        self.tick()
        self.host.procs[421][3] = 1.0
        self.assertEqual(self.run_for(0.5), [], "the streak restarted after the cool interval")

    def test_wait_idle_daemon_waits_while_a_job_runs_until_busy_hot_hours(self) -> None:
        self.host.add(421, FSEV["path"], 1.0)
        self.host.add(77, "/Users/cmux/actions-runner-glaeda/bin/Runner.Worker", 0.0, uid=501)
        self.assertEqual(self.run_for(self.policy["hot_hours"] + 0.5), [])
        waiting = self.tick()["hot"][0]["waiting"]
        self.assertIn("host busy (Runner.Worker)", waiting)
        actions = self.run_for(self.policy["busy_hot_hours"] - self.policy["hot_hours"])
        self.assertEqual([a["name"] for a in actions], ["fseventsd"])

    def test_job_ending_lets_a_due_restart_happen(self) -> None:
        self.host.add(421, FSEV["path"], 1.0)
        self.host.add(77, "/x/bin/Runner.Worker", 0.0, uid=501)
        self.run_for(self.policy["hot_hours"] + 0.1)
        del self.host.procs[77]
        self.assertEqual([a["name"] for a in self.tick()["actions"]], ["fseventsd"])

    def test_daemon_without_wait_idle_restarts_on_a_busy_host(self) -> None:
        self.host.add(283, WRM["path"], 1.1)
        self.host.add(77, "/x/bin/xcodebuild", 0.0, uid=501)
        self.assertEqual([a["name"] for a in self.run_for(self.policy["hot_hours"] + 0.1)], ["WirelessRadioManagerd"])

    def test_restarts_are_capped_per_day(self) -> None:
        self.policy["hot_hours"] = 0.1
        self.policy["max_restarts_per_day"] = 2
        self.host.add(1, FSEV["path"], 1.0)
        pid = 2
        actions = []
        for _ in range(6):
            self.host.respawn[FSEV["path"]] = pid
            pid += 1
            actions += self.run_for(0.2)
            for p in list(self.host.procs):
                self.host.procs[p][3] = 1.0
        self.assertEqual(len(actions), 2)
        self.assertIn("2 restarts in the last day", self.tick()["hot"][0]["waiting"])

    def test_only_the_system_daemon_at_its_exact_path_is_touched(self) -> None:
        self.host.add(500, FSEV["path"], 1.0, uid=501)  # a user process at that path is not the daemon
        self.host.add(501, "/tmp/fseventsd", 1.0)
        self.assertEqual(self.run_for(self.policy["busy_hot_hours"] + 1), [])
        self.assertEqual(self.host.signals, [])

    def test_plan_never_signals(self) -> None:
        self.host.add(421, FSEV["path"], 1.0)
        self.run_for(self.policy["hot_hours"] + 1, apply=False)
        self.assertEqual(self.host.signals, [])

    def test_a_daemon_that_ignores_sigterm_is_killed(self) -> None:
        self.host.survives_term = True
        self.host.add(421, FSEV["path"], 1.0)
        actions = self.run_for(self.policy["hot_hours"] + 0.1)
        self.assertEqual(self.host.signals, [(421, signal.SIGTERM), (421, signal.SIGKILL)])
        self.assertIn("SIGKILL after SIGTERM", actions[0]["action"])

    def test_bloated_daemon_restarts_at_once_even_on_a_busy_host_and_while_cool(self) -> None:
        """cmuxs-mac-mini-6 10-25: fseventsd at 102 GiB (65 GiB of it swap) and 3 GiB of disk free."""
        self.host.add(427, FSEV["path"], 0.0)
        self.host.sizes[427] = 102 << 30
        self.host.add(77, "/Users/cmux/actions-runner-glaeda/bin/Runner.Worker", 0.0, uid=501)
        self.host.respawn[FSEV["path"]] = 9000
        report = self.tick()
        self.assertEqual([a["name"] for a in report["actions"]], ["fseventsd"])
        self.assertEqual(self.host.signals, [(427, signal.SIGTERM)])
        self.assertEqual(report["healed"][0]["footprint_gib"], 102.0)
        self.assertEqual(self.tick()["actions"], [], "the fresh daemon is small and cool")

    def test_a_footprint_under_the_cap_waits_for_the_cpu_rule(self) -> None:
        self.host.add(427, FSEV["path"], 0.0)
        self.host.sizes[427] = int(self.policy["footprint_gib"] * heal.GIB) - 1
        self.assertEqual(self.run_for(self.policy["busy_hot_hours"] + 1), [])
        self.assertEqual(self.tick()["hot"], [])

    def test_the_footprint_cap_follows_the_policy(self) -> None:
        self.policy["footprint_gib"] = 1
        self.host.add(427, FSEV["path"], 0.0)
        self.host.sizes[427] = 1 << 30
        self.assertEqual(len(self.tick()["actions"]), 1)

    def test_a_bloated_daemon_is_held_to_the_daily_restart_cap(self) -> None:
        self.policy["max_restarts_per_day"] = 2
        pid = 100
        self.host.add(pid, FSEV["path"], 0.0)
        actions = []
        for _ in range(5):
            for p in list(self.host.procs):
                self.host.sizes[p] = 50 << 30
            self.host.respawn[FSEV["path"]] = pid = pid + 1
            actions += self.tick()["actions"]
        self.assertEqual(len(actions), 2)
        hot = self.tick()["hot"][0]
        self.assertIn("2 restarts in the last day", hot["waiting"])
        self.assertEqual(hot["footprint_gib"], 50.0)

    def test_plan_reports_a_bloated_daemon_without_signalling(self) -> None:
        self.host.add(427, FSEV["path"], 0.0)
        self.host.sizes[427] = 102 << 30
        hot = self.tick(apply=False)["hot"]
        self.assertEqual(self.host.signals, [])
        self.assertEqual(hot[0]["waiting"], "would restart now (plan)")
        self.assertIn("footprint 102.0 GiB", hot[0]["why"])

    def test_footprints_are_asked_once_per_pass_for_the_policy_daemons_only(self) -> None:
        self.host.add(427, FSEV["path"], 0.0)
        self.host.add(500, FSEV["path"], 0.0, uid=501)  # a user process at that path is not the daemon
        self.host.add(283, WRM["path"], 0.0)
        self.host.sizes[500] = 900 << 30
        self.tick()
        self.assertEqual(self.host.asked, [[283, 427]], "the user process at fseventsd's path is not asked")
        self.assertEqual(self.host.signals, [])

    def test_a_new_pid_starts_over(self) -> None:
        self.host.add(421, FSEV["path"], 1.0)
        self.run_for(self.policy["hot_hours"] - 0.2)
        del self.host.procs[421]
        self.host.add(422, FSEV["path"], 1.0)
        self.assertEqual(self.run_for(0.5), [])


class Guards(Base):
    def test_never_signals_launchd_or_the_kernel(self) -> None:
        ok, what = heal.restart(self.host, self.policy, FSEV, 1)
        self.assertFalse(ok)
        self.assertIn("refusing", what)
        self.assertEqual(self.host.signals, [])

    def test_install_refuses_bytes_that_are_not_the_release(self) -> None:
        with self.assertRaisesRegex(heal.HealError, "script sha256 .* is not the expected"):
            heal.install(POLICY_PATH, True, expect_sha256="0" * 64)
        with self.assertRaisesRegex(heal.HealError, "policy sha256 .* is not the expected"):
            heal.install(POLICY_PATH, True, expect_policy_sha256="0" * 64)
        good = heal.sha256((ROOT / "scripts" / "glaeda-daemon-heal").read_bytes())
        with self.assertRaisesRegex(heal.HealError, "not 64 hex digits"):
            heal.install(POLICY_PATH, True, expect_sha256="nothex")
        with self.assertRaisesRegex(heal.HealError, "policy sha256"):  # shasum's "<hash>  -" is accepted for the script
            heal.install(POLICY_PATH, True, expect_sha256=good + "  -", expect_policy_sha256="0" * 64)


class Install(unittest.TestCase):
    def test_launchdaemon_runs_the_root_snapshot_with_system_python_at_the_policy_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            want = heal.desired_install(POLICY_PATH, Path(tmp))
            doc = plistlib.loads(want["plist"])
            self.assertEqual(doc["ProgramArguments"][:3], ["/usr/bin/python3", "-I", os.fspath(Path(tmp) / "glaeda-daemon-heal")])
            self.assertEqual(doc["StartInterval"], POLICY["every_s"])
            self.assertNotIn("UserName", doc)
            self.assertIn(os.fspath(Path(tmp) / "macos-daemons.json"), doc["ProgramArguments"])

    def test_status_is_current_only_when_script_policy_and_plist_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d, plist = Path(tmp) / "heal", Path(tmp) / "x.plist"
            d.mkdir()
            self.assertFalse(heal.install_status(POLICY_PATH, d, plist)["installed"])
            want = heal.desired_install(POLICY_PATH, d)
            for path, (data, _mode) in want["files"].items():
                path.write_bytes(data)
            plist.write_bytes(want["plist"])
            status = heal.install_status(POLICY_PATH, d, plist)
            self.assertTrue(status["installed"] and status["current"], status)
            self.assertFalse(status["reportFresh"], "no report yet: the daemon has not run a pass")
            (want["state_dir"]).mkdir()
            (want["state_dir"] / "report.json").write_text("{}")
            self.assertTrue(heal.install_status(POLICY_PATH, d, plist)["reportFresh"])
            (d / "macos-daemons.json").write_text("{}")
            self.assertEqual(heal.install_status(POLICY_PATH, d, plist)["stale"], [os.fspath(d / "macos-daemons.json")])

    def test_runs_on_macos_system_python(self) -> None:
        """The root daemon runs /usr/bin/python3 (3.9 on macOS): no syntax newer than that."""
        import ast
        tree = ast.parse((ROOT / "scripts" / "glaeda-daemon-heal").read_text(), feature_version=(3, 9))
        self.assertFalse([n for n in ast.walk(tree) if type(n).__name__ == "Match"])


SCRIPT_BYTES = (ROOT / "scripts" / "glaeda-daemon-heal").read_bytes()
POLICY_BYTES = POLICY_PATH.read_bytes()
ORIGIN, MIRROR = heal.RELEASE_SOURCES


def tar_gz(files: dict) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


class Channel:
    """The ota-channels files and releases on one or both sources, as GitHub serves them; records each download."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.down: set[str] = set()
        self.got: list[str] = []

    def publish(self, n: int, script: bytes = SCRIPT_BYTES, policy: bytes = POLICY_BYTES, sources=(ORIGIN, MIRROR),
                members: dict | None = None, release_sha: str | None = None, paused: bool = False,
                ancestors: range | None = None) -> str:
        tag, commit = f"r-20261026-{n:012x}", f"{n:040x}"
        ancestry = "".join(f"{k:040x}\n" for k in (ancestors if ancestors is not None else range(n, 0, -1)))
        archive = tar_gz(members if members is not None else {
            "glaeda/README.md": b"glaeda\n", heal.MEMBERS[heal.SCRIPT_NAME]: script,
            heal.MEMBERS[heal.POLICY_NAME]: policy, heal.ANCESTRY: ancestry.encode()})
        release = json.dumps({"schema": heal.RELEASE_SCHEMA, "repository": heal.REPOSITORY, "tag": tag,
                              "source": commit, "assets": {heal.ARCHIVE: {
                                  "size": len(archive), "sha256": hashlib.sha256(archive).hexdigest()}}}).encode()
        entry = {"schema": heal.CHANNEL_SCHEMA, "ring": "stable", "tag": tag, "source": commit,
                 "releaseSha256": release_sha or hashlib.sha256(release).hexdigest()}
        for source in sources:
            self.files[heal.release_url(source, "ota-channels", "control.json")] = json.dumps(
                {"schema": heal.CONTROL_SCHEMA, "paused": paused}).encode()
            self.files[heal.release_url(source, "ota-channels", "stable.json")] = json.dumps(entry).encode()
            self.files[heal.release_url(source, tag, "release.json")] = release
            self.files[heal.release_url(source, tag, heal.ARCHIVE)] = archive
        return tag

    def get(self, url: str, limit: int) -> bytes:
        self.got.append(url)
        if any(url.startswith(f"https://github.com/{s}/") for s in self.down) or url not in self.files:
            raise urllib.error.URLError("connection refused")
        if len(self.files[url]) > limit:
            raise heal.HealError("too big")
        return self.files[url]


def edited_policy(**changes) -> bytes:
    doc = json.loads(POLICY_BYTES)
    doc.update(changes)
    return (json.dumps(doc, indent=2) + "\n").encode()


class SelfUpdate(unittest.TestCase):
    """The installed root copy follows the stable release: a release that changes the script or the policy reaches
    the snapshot on its own, through the same hash chain glaeda-update checks."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "daemon-heal"
        self.state_dir = self.dir / "state"
        self.state_dir.mkdir(parents=True)
        for name, data in ((heal.SCRIPT_NAME, SCRIPT_BYTES), (heal.POLICY_NAME, POLICY_BYTES)):
            (self.dir / name).write_bytes(data)  # what a root --install of today's release left
        self.channel = Channel()
        self.host = FakeHost()

    def installed(self, name: str) -> bytes:
        return (self.dir / name).read_bytes()

    def run_pass(self, seconds: float = 0, python: str = sys.executable) -> dict:
        self.host.advance(seconds)
        return heal.run_pass(self.dir / heal.POLICY_NAME, self.state_dir, True, self.host, install_dir=self.dir,
                             get=self.channel.get, python=python)

    def verdict(self, report: dict, after: float = 0) -> dict:
        """The judgment `after` seconds on, of a pass then that holds this release state (passes kept running)."""
        now = heal.parse_iso(report["at"]) + after
        return heal.judge({**report, "at": heal.iso(now)}, now, True)

    def test_a_release_that_changes_the_script_and_the_policy_reaches_the_snapshot_with_no_hand_step(self) -> None:
        script = SCRIPT_BYTES + b"\n# released later\n"
        policy = edited_policy(hot_hours=3)
        tag = self.channel.publish(2, script=script, policy=policy)
        report = self.run_pass()  # the staged pair really runs: a plan pass of the new code with its policy
        self.assertEqual((self.installed(heal.SCRIPT_NAME), self.installed(heal.POLICY_NAME)), (script, policy))
        self.assertEqual((report["release"]["tag"], report["release"]["wants"], report["release"]["error"]),
                         (tag, tag, None))
        self.assertEqual(self.verdict(report)["state"], "current")
        later = edited_policy(hot_hours=4)  # a release that changes only the policy, picked up within every_s
        tag3 = self.channel.publish(3, script=script, policy=later)
        self.run_pass(60)
        self.assertEqual(self.installed(heal.POLICY_NAME), policy, "not due yet")
        report = self.run_pass(POLICY["release"]["every_s"])
        self.assertEqual((self.installed(heal.POLICY_NAME), report["release"]["tag"]), (later, tag3))
        mode = (self.dir / heal.SCRIPT_NAME).stat().st_mode & 0o777
        self.assertEqual(mode, 0o555)

    def test_a_release_that_changes_only_the_script_reaches_the_snapshot(self) -> None:
        script = SCRIPT_BYTES + b"\n# only the script\n"
        tag = self.channel.publish(2, script=script)
        report = self.run_pass()
        self.assertEqual((self.installed(heal.SCRIPT_NAME), self.installed(heal.POLICY_NAME)), (script, POLICY_BYTES))
        self.assertEqual(report["release"]["tag"], tag)
        self.assertFalse((self.dir / "staged" / heal.PAIR_MARK).exists(), "the swap finished")

    def test_a_release_that_does_not_descend_from_the_installed_one_is_refused(self) -> None:
        self.channel.publish(2, script=SCRIPT_BYTES + b"# 2\n")
        self.run_pass()
        self.channel.publish(3, script=SCRIPT_BYTES + b"# 3, from a branch\n", ancestors=range(3, 2, -1))
        report = self.run_pass(POLICY["release"]["every_s"])
        self.assertEqual(self.installed(heal.SCRIPT_NAME), SCRIPT_BYTES + b"# 2\n")
        self.assertIn("does not descend", report["release"]["error"])

    def test_a_crash_before_state_is_saved_still_refuses_a_downgrade(self) -> None:
        self.channel.publish(1, script=SCRIPT_BYTES + b"# 1\n")
        self.run_pass()
        saved = (self.state_dir / "state.json").read_bytes()
        self.channel.publish(2, script=SCRIPT_BYTES + b"# 2\n")
        self.run_pass(POLICY["release"]["every_s"])
        (self.state_dir / "state.json").write_bytes(saved)  # the pass died after the swap, before its state
        self.channel.publish(1, script=SCRIPT_BYTES + b"# 1\n")  # a lagging source names the older release
        report = self.run_pass(POLICY["release"]["every_s"])
        self.assertEqual(self.installed(heal.SCRIPT_NAME), SCRIPT_BYTES + b"# 2\n")
        self.assertIn("does not descend", report["release"]["error"])

    def test_a_truncated_download_is_an_error_not_a_crash(self) -> None:
        import http.client
        self.channel.publish(2, script=b"# new\n")
        real = self.channel.get

        def truncated(url: str, limit: int) -> bytes:
            if url.endswith(heal.ARCHIVE):
                raise http.client.IncompleteRead(b"partial")
            return real(url, limit)
        self.channel.get = truncated
        report = self.run_pass()
        self.assertTrue(report["release"]["error"])
        self.assertEqual(self.installed(heal.SCRIPT_NAME), SCRIPT_BYTES)
        self.assertTrue((self.state_dir / "report.json").exists(), "the pass still saved its report")

    def test_malformed_release_metadata_is_an_error_not_a_crash(self) -> None:
        self.channel.publish(2, script=b"# new\n")
        url = next(u for u in self.channel.files if u.endswith("/release.json") and f"/{ORIGIN}/" in u)
        doc = json.loads(self.channel.files[url])
        doc["assets"] = ["not", "a", "map"]
        self.channel.files[url] = json.dumps(doc).encode()
        stable = heal.release_url(ORIGIN, "ota-channels", "stable.json")
        entry = json.loads(self.channel.files[stable])
        entry["releaseSha256"] = hashlib.sha256(self.channel.files[url]).hexdigest()
        self.channel.files[stable] = json.dumps(entry).encode()
        report = self.run_pass()
        self.assertIn("has no", report["release"]["error"])
        self.assertEqual(self.installed(heal.SCRIPT_NAME), SCRIPT_BYTES)

    def test_a_swap_cut_short_is_finished_before_the_next_policy_load(self) -> None:
        script, policy = SCRIPT_BYTES + b"# next\n", edited_policy(hot_hours=5)
        stage = self.dir / "staged"
        stage.mkdir()
        (stage / heal.SCRIPT_NAME).write_bytes(script)
        (stage / heal.POLICY_NAME).write_bytes(policy)
        hashes = {heal.SCRIPT_NAME: heal.sha256(script), heal.POLICY_NAME: heal.sha256(policy)}
        (stage / heal.PAIR_MARK).write_text(json.dumps({"files": hashes, "release": {"tag": "r-9", "commit": "c9"}}))
        (self.dir / heal.POLICY_NAME).write_bytes(policy)  # died after the policy, before the script
        self.assertTrue(heal.finish_pair(self.dir, root=False))
        self.assertEqual((self.installed(heal.SCRIPT_NAME), self.installed(heal.POLICY_NAME)), (script, policy))
        self.assertEqual(json.loads(self.installed(heal.INSTALLED))["commit"], "c9", "the release is swapped with it")
        self.assertFalse((stage / heal.PAIR_MARK).exists())
        (stage / heal.PAIR_MARK).write_text(json.dumps({"files": {heal.SCRIPT_NAME: "0" * 64,
                                                                  heal.POLICY_NAME: "0" * 64}, "release": None}))
        (stage / heal.SCRIPT_NAME).write_bytes(b"# torn\n")
        self.assertTrue(heal.finish_pair(self.dir, root=False))
        self.assertEqual(self.installed(heal.SCRIPT_NAME), script, "a mark the staged files do not match is dropped")
        self.assertFalse(heal.finish_pair(self.dir, root=False))

    def test_the_staged_run_keeps_bounded_output_and_a_timeout(self) -> None:
        code, out, _err = heal.bounded_run([sys.executable, "-c", "print('x' * 5_000_000)"], 30, limit=1000)
        self.assertEqual((code, len(out)), (0, 1000))
        code, _out, _err = heal.bounded_run([sys.executable, "-c", "import time; time.sleep(30)"], 0.5)
        self.assertEqual(code, 124)
        started = heal.time.time()  # a descendant that keeps the pipes open is killed with it
        code, _out, _err = heal.bounded_run(["/bin/sh", "-c", "sleep 30 & sleep 30"], 0.5)
        self.assertEqual(code, 124)
        self.assertLess(heal.time.time() - started, 4)

    def test_the_same_release_costs_two_small_reads_and_rewrites_nothing(self) -> None:
        tag = self.channel.publish(2)
        self.run_pass()
        before = (self.dir / heal.SCRIPT_NAME).stat().st_mtime_ns
        self.channel.got.clear()
        report = self.run_pass(POLICY["release"]["every_s"])
        self.assertEqual([u.rsplit("/", 1)[1] for u in self.channel.got], ["control.json", "stable.json"])
        self.assertEqual(((self.dir / heal.SCRIPT_NAME).stat().st_mtime_ns, report["release"]["tag"]), (before, tag))
        self.assertIsNone(report["release"]["installedAt"], "the hand install already held these bytes")

    def test_files_that_drift_from_their_release_are_restored_at_the_next_pass(self) -> None:
        self.channel.publish(2)
        self.run_pass()
        (self.dir / heal.SCRIPT_NAME).write_bytes(b"print('hand edit')\n")
        self.run_pass(POLICY["every_s"])  # drift is checked every pass, not every release.every_s
        self.assertEqual(self.installed(heal.SCRIPT_NAME), SCRIPT_BYTES)

    def test_files_that_drift_and_cannot_be_repaired_show_as_stale(self) -> None:
        tag = self.channel.publish(2)
        self.run_pass()
        (self.dir / heal.SCRIPT_NAME).write_bytes(b"print('hand edit')\n")
        url = next(u for u in self.channel.files if u.endswith(heal.ARCHIVE) and f"/{ORIGIN}/" in u)
        del self.channel.files[url]  # the repair cannot download its release
        report = self.run_pass(POLICY["every_s"])
        self.assertEqual((report["release"]["tag"], report["release"]["wants"]), (tag, tag))
        self.assertTrue(report["release"]["drifted"] and report["release"]["error"])
        verdict = self.verdict(report, POLICY["release"]["stale_s"] + 1)
        self.assertEqual(verdict["state"], "stale")
        self.assertIn("not the " + tag, verdict["why"])

    def test_the_mirror_answers_when_the_repository_does_not(self) -> None:
        tag = self.channel.publish(2, script=SCRIPT_BYTES + b"# 2\n")
        self.channel.down.add(ORIGIN)
        report = self.run_pass()
        self.assertEqual((report["release"]["source"], report["release"]["tag"]), (MIRROR, tag))

    def test_a_paused_channel_installs_nothing(self) -> None:
        self.channel.publish(2, script=b"# new\n", paused=True)
        report = self.run_pass()
        self.assertEqual(self.installed(heal.SCRIPT_NAME), SCRIPT_BYTES)
        self.assertEqual(self.channel.got[-1].rsplit("/", 1)[1], "control.json")
        self.assertEqual(self.verdict(report)["state"], "current")

    def refused(self, why: str, **publish) -> dict:
        self.channel.publish(2, **publish)
        report = self.run_pass()
        self.assertEqual((self.installed(heal.SCRIPT_NAME), self.installed(heal.POLICY_NAME)),
                         (SCRIPT_BYTES, POLICY_BYTES), why)
        self.assertRegex(report["release"]["error"], why)
        self.assertIn("hot", report, "the heal pass itself still ran")
        return report

    def test_a_release_json_that_is_not_the_channels_is_refused(self) -> None:
        self.refused("does not match the channel", script=b"# evil\n", release_sha="0" * 64)

    def test_an_archive_that_is_not_release_jsons_is_refused(self) -> None:
        self.channel.publish(2, script=b"# evil\n")
        url = next(u for u in self.channel.files if u.endswith(heal.ARCHIVE) and f"/{ORIGIN}/" in u)
        self.channel.files[url] = tar_gz({heal.MEMBERS[heal.SCRIPT_NAME]: b"# swapped\n"})
        report = self.run_pass()
        self.assertEqual(self.installed(heal.SCRIPT_NAME), SCRIPT_BYTES)
        self.assertIn("does not match release.json", report["release"]["error"])

    def test_a_release_without_the_two_files_is_refused(self) -> None:
        self.refused("ships no", members={heal.MEMBERS[heal.SCRIPT_NAME]: SCRIPT_BYTES})

    def test_a_pair_that_cannot_plan_a_pass_is_never_installed_and_shows_as_stale(self) -> None:
        report = self.refused("its own plan pass failed", script=b"raise SystemExit(3)\n")
        self.assertEqual(self.verdict(report)["state"], "current", "within stale_s it is a lag, not a fault")
        verdict = self.verdict(report, POLICY["release"]["stale_s"] + 1)
        self.assertEqual(verdict["state"], "stale")
        self.assertIn("plan pass failed", verdict["why"])

    def test_a_policy_its_own_code_refuses_is_never_installed(self) -> None:
        self.refused("its own plan pass failed", policy=b'{"schema": "nope"}')

    def test_an_unreachable_channel_is_stale_after_stale_s(self) -> None:
        report = self.run_pass()  # nothing published: every source refuses
        self.assertIn("no release source answered", report["release"]["error"])
        self.assertEqual(self.verdict(report)["state"], "stale")
        self.assertIn("has not read its release channel", self.verdict(report)["why"])

    def test_root_reads_nothing_the_runner_user_writes(self) -> None:
        text = SCRIPT_BYTES.decode()
        for path in ("~/", ".config/glaeda", ".local/state/glaeda", "Path.home", "expanduser", "HOME"):
            self.assertNotIn(path, text.split("# ---------------------------------------------------------------- the "
                                              "release this copy follows")[1].split("def judge")[0], path)
        with self.assertRaises(heal.HealError):
            heal.fetch("http://github.com/x", 10)
        with self.assertRaises(heal.HealError):
            heal.fetch("https://example.com/x", 10)
        for bad in ("http://release-assets.githubusercontent.com/x", "https://evil.example/x",
                    "https://github.com.evil.example/x", "https://release-assets.githubusercontent.com:8443/x"):
            with self.assertRaisesRegex(heal.HealError, "refusing a redirect", msg=bad):
                heal.HttpsOnly().redirect_request(None, None, 302, "Found", {}, bad)
        self.assertTrue(heal.url_ok("https://release-assets.githubusercontent.com/github-production-release-asset/1"))
        for sources in (["someone/glaeda"], [], [ORIGIN, ORIGIN], [{"x": 1}]):
            bad = copy.deepcopy(POLICY)
            bad["release"]["sources"] = sources
            with self.assertRaises(heal.HealError, msg=sources):
                heal.validate_policy(bad)


class Judge(unittest.TestCase):
    def report(self, **release) -> dict:
        rel = {"tag": "r-1", "wants": "r-1", "okAt": heal.iso(T0), "behindSince": None, "error": None,
               "staleAfterS": 3600, **release}
        return {"at": heal.iso(T0), "staleAfterS": 360, "release": rel}

    def test_states(self) -> None:
        self.assertEqual(heal.judge({}, T0, False)["state"], "missing")
        self.assertEqual(heal.judge(None, T0, True)["state"], "stale")
        old = heal.judge({"at": heal.iso(T0), "hot": []}, T0, True)
        self.assertEqual(old["state"], "stale")
        self.assertIn("one root --install", old["why"])
        self.assertEqual(heal.judge(self.report(), T0 + 300, True)["state"], "current")
        self.assertIn("not running", heal.judge(self.report(), T0 + 400, True)["why"])
        behind = self.report(wants="r-2", behindSince=heal.iso(T0 - 4000), error="boom")
        self.assertIn("named r-2", heal.judge(behind, T0, True)["why"])
        self.assertIn("boom", heal.judge(behind, T0, True)["why"])
        self.assertEqual(heal.judge(self.report(okAt=heal.iso(T0 - 4000)), T0, True)["state"], "stale")


class Reload(unittest.TestCase):
    def test_a_plist_that_no_longer_matches_the_policy_is_reloaded_once_from_a_detached_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d, plist = Path(tmp) / "heal", Path(tmp) / "x.plist"
            (d / "state").mkdir(parents=True)
            (d / heal.POLICY_NAME).write_bytes(edited_policy(every_s=300))
            plist.write_bytes(heal.desired_install(POLICY_PATH, d)["plist"])  # loaded at the old interval
            spawn = mock.Mock()
            state: dict = {}
            self.assertTrue(heal.schedule_reload(d, plist, state, T0, 600, spawn))
            argv, kw = spawn.call_args
            self.assertEqual(argv[0][:2], ["/usr/bin/python3", "-I"])
            self.assertIn("--after-pid", argv[0])
            self.assertTrue(kw["start_new_session"])
            self.assertFalse(heal.schedule_reload(d, plist, state, T0 + 60, 600, spawn), "once per every_s")
            plist.write_bytes(heal.desired_install(d / heal.POLICY_NAME, d)["plist"])
            self.assertFalse(heal.schedule_reload(d, plist, {}, T0, 600, spawn))
            self.assertEqual(spawn.call_count, 1)


if __name__ == "__main__":
    unittest.main()
