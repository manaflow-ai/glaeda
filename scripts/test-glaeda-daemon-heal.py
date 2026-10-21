#!/usr/bin/env python3
"""Tests for scripts/glaeda-daemon-heal and its desired state, config/macos-daemons.json."""

from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import json
import os
import plistlib
import re
import signal
import sys
import tempfile
import unittest
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
        for key, value in (("hot_hours", 0), ("hot_cores", "1"), ("every_s", True), ("busy_hot_hours", 1)):
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


if __name__ == "__main__":
    unittest.main()
