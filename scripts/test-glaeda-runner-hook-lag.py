#!/usr/bin/env python3
"""Tests for glaeda-runner-hook-lag with a fake ssh on PATH that runs the probe here, against fake homes."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKOUT_FILES = ("glaeda-runner-hook-lag", "glaeda-cmux-runner-fleet", "glaeda-cmux-runner",
                  "glaeda-cmux-runner-hook", "glaeda_fleet_labels.py", "glaeda_reservation.py", "glaeda-update",
                  "glaeda_release.py")
LONG_AGO = "2026-01-01T00:00:00Z"
MANIFEST = {
    "defaults": {"xcode": {"apps": [{"path": "/Applications/Xcode_26.6.app", "version": "26.6", "build": "17F113"}]}},
    "hosts": {
        "mini-a": {"class": "std", "availability": "dedicated", "roles": ["ci-runner"], "hardware": "m4pro-48"},
        "mini-b": {"class": "std", "availability": "dedicated", "roles": ["ci-runner"], "hardware": "m4pro-48"},
        "cache": {"class": "std", "availability": "dedicated", "roles": ["cache-host"], "hardware": "m4pro-48"},
        "aws-a": {"class": "std", "availability": "dedicated", "roles": ["ci-runner"], "hardware": "m4pro-48"},
        "aws-b": {"class": "std", "availability": "dedicated", "roles": ["ci-runner"], "hardware": "m4pro-48"},
        "stranger": {"class": "std", "availability": "dedicated", "roles": ["ci-runner"], "hardware": "m4pro-48"},
    },
}
# The shape of leo-workspace machines/fleet-registry.json: aws-a is marked no_touch, aws-b's policy says never
# ssh, mini-b is known by an alias, and stranger is not named at all.
REGISTRY = {
    "approval_policies": {"manaflow-fleet": {"auto": ["inspect"], "never": ["passwords: keys only"]},
                          "lawrence-no-touch": {"auto": [], "never": ["ssh", "passwords of any kind"]}},
    "machines": {
        "mini-a": {"approval": "manaflow-fleet", "roles": ["ci-runner"]},
        "mini-b-long-name": {"aliases": ["mini-b"], "approval": "manaflow-fleet", "roles": ["ci-runner"]},
        "aws-a": {"approval": "manaflow-fleet", "no_touch": True},
        "aws-b": {"approval": "lawrence-no-touch"},
    },
}
# Runs the remote command's probe with this python, as the member's user: HOME is FAKE_HOMES/<host>.
FAKE_SSH = """#!{python}
import json, os, shlex, subprocess, sys
args = sys.argv[1:]
while args and args[0] == "-o":
    args = args[2:]
host, remote = args[0], shlex.split(" ".join(args[1:]))
open(os.environ["FAKE_LOG"], "a").write(json.dumps({{"host": host, "remote": remote}}) + "\\n")
if host in os.environ.get("FAKE_DOWN", "").split(","):
    sys.stderr.write("ssh: connect to host " + host + " port 22: Connection refused\\n"); sys.exit(255)
assert remote[0] == "/usr/bin/python3", remote
env = {{**os.environ, "HOME": os.path.join(os.environ["FAKE_HOMES"], host)}}
sys.exit(subprocess.run([sys.executable, *remote[1:]], stdin=sys.stdin, env=env).returncode)
"""
OLD_FIRST_POLL = 'GATE_FIRST_POLL_S = float(os.environ.get("GLAEDA_RUNNER_GATE_FIRST_POLL_S") or 30.0)'


def load_tool():
    path = ROOT / "scripts" / "glaeda-runner-hook-lag"
    loader = importlib.machinery.SourceFileLoader("glaeda_runner_hook_lag", os.fspath(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def old_hook(text: str) -> str:
    """main's hook as it was before glaeda#1572: a 30 s first poll and no restart-free wait."""
    lines = []
    for line in text.splitlines(keepends=True):
        if line.startswith("GATE_FIRST_POLL_S = "):
            line = OLD_FIRST_POLL + "\n"
        elif line.startswith("GATE_RESTART_FREE_S = "):
            continue
        lines.append(line)
    return "".join(lines)


class GateSettingsTest(unittest.TestCase):
    def test_settings_are_every_env_read_with_a_default(self) -> None:
        settings = load_tool().gate_settings((ROOT / "scripts" / "glaeda-cmux-runner-hook").read_text())
        self.assertEqual(settings["GATE_FIRST_POLL_S"], "GLAEDA_RUNNER_GATE_FIRST_POLL_S")
        self.assertEqual(settings["GATE_RESTART_FREE_S"], "GLAEDA_RUNNER_GATE_RESTART_FREE_S")
        self.assertEqual(settings["GATE_CPU_PAUSE"], "GLAEDA_RUNNER_GATE_CPU_PAUSE")
        self.assertEqual(settings["GATE_THERMAL_HOLD"], "GLAEDA_RUNNER_THERMAL_HOLD")  # through _hold
        self.assertEqual(settings["CONSOLE_GATE"], "GLAEDA_RUNNER_CONSOLE_GATE")
        self.assertNotIn("_hold", settings)
        self.assertNotIn("GATE_STOP_WAIT_S", settings)  # a constant with no variable: not a setting
        self.assertNotIn("BROKER_POLL_S", settings)


class HookLagTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        (bin_dir / "ssh").write_text(FAKE_SSH.format(python=sys.executable))
        (bin_dir / "ssh").chmod(0o755)
        self.manifest = self.dir / "mini-fleet.json"
        self.manifest.write_text(json.dumps(MANIFEST))
        self.log = self.dir / "calls.jsonl"
        self.homes = self.dir / "homes"
        self.env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": os.fspath(self.dir), "FAKE_LOG": os.fspath(self.log),
                    "FAKE_HOMES": os.fspath(self.homes)}
        self.repo = self.dir / "glaeda"
        (self.repo / "scripts").mkdir(parents=True)
        for name in CHECKOUT_FILES:
            shutil.copy2(ROOT / "scripts" / name, self.repo / "scripts" / name)
        origin = self.dir / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", os.fspath(origin), cwd=self.dir)
        self.git("init", "-q", "-b", "main")
        # Two hook commits: the pre-#1572 hook, then main's. The second is the change on its way.
        self.hook = (ROOT / "scripts" / "glaeda-cmux-runner-hook").read_text()
        (self.repo / "scripts" / "glaeda-cmux-runner-hook").write_text(old_hook(self.hook))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "one")
        (self.repo / "scripts" / "glaeda-cmux-runner-hook").write_text(self.hook)
        self.git("commit", "-q", "-am", "two")
        self.git("remote", "add", "origin", os.fspath(origin))
        self.git("push", "-q", "origin", "main")
        self.head = self.git("rev-parse", "HEAD")
        self.registry = self.dir / "fleet-registry.json"
        self.registry.write_text(json.dumps(REGISTRY))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                               "-c", "commit.gpgsign=false", "-C", os.fspath(cwd or self.repo), *args],
                              capture_output=True, text=True, check=True, timeout=60,
                              env={"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GIT_CONFIG_NOSYSTEM": "1"},
                              ).stdout.strip()

    def install(self, host: str, runner: str, text: str) -> None:
        hooks = self.homes / host / runner / "glaeda-hooks"
        hooks.mkdir(parents=True)
        (hooks / "glaeda-cmux-runner-hook").write_text(text)

    def stable(self, source: str, promoted: str) -> Path:
        path = self.dir / f"stable-{source[:12]}-{promoted}.json"  # one file per channel state, never overwritten
        path.write_text(json.dumps({"ring": "stable", "source": source, "promoted": promoted}))
        return path

    def lag(self, *args: str, stable: Path | None = None, **env: str) -> subprocess.CompletedProcess:
        stable = stable or self.stable(self.head, LONG_AGO)  # main's hook on stable long ago: a lag is a lag
        return subprocess.run([sys.executable, os.fspath(self.repo / "scripts" / "glaeda-runner-hook-lag"),
                               "--manifest", os.fspath(self.manifest), "--output", "json",
                               "--stable-json", os.fspath(stable), "--registry", os.fspath(self.registry), *args],
                              capture_output=True, text=True, timeout=300, env={**self.env, **env})

    def test_an_old_hook_lags_by_setting_and_a_current_one_does_not(self) -> None:
        self.install("mini-a", "actions-runner-glaeda", self.hook)
        self.install("mini-b", "actions-runner-glaeda", self.hook)
        self.install("mini-b", "actions-runner-glaeda-1", old_hook(self.hook))
        # The probe clears the runner variables, so a caller's override never hides the installed default.
        out = self.lag(GLAEDA_RUNNER_GATE_FIRST_POLL_S="60")
        self.assertEqual(out.returncode, 1, out.stderr)
        report = json.loads(out.stdout)
        members = {m["member"]: m for m in report["members"]}
        self.assertEqual(sorted(members), ["mini-a", "mini-b"])  # the cache host takes no runners
        self.assertEqual(members["mini-a"]["state"], "current")
        self.assertEqual(members["mini-b"]["state"], "lags")
        runners = {r["runner"]: r for r in members["mini-b"]["runners"]}
        self.assertEqual(runners["actions-runner-glaeda"]["state"], "current")
        self.assertEqual(runners["actions-runner-glaeda-1"]["lags"], [
            {"name": "GATE_FIRST_POLL_S", "main": "60.0", "installed": "30.0"},
            {"name": "GATE_RESTART_FREE_S", "main": "60.0", "installed": None}])
        self.assertFalse(list(self.homes.rglob("__pycache__")))  # nothing written on a member

    def test_a_lag_converges_while_the_release_reaches_the_runners(self) -> None:
        self.install("mini-a", "actions-runner-glaeda", old_hook(self.hook))
        self.install("mini-b", "actions-runner-glaeda", self.hook)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cases = {"released": self.stable(self.head, now),  # promoted just now: inside the refresh window
                 "soaking": self.stable("0" * 40, LONG_AGO)}  # stable lacks the (fresh) hook commit
        for state, stable in cases.items():
            with self.subTest(state):
                out = self.lag(stable=stable)
                self.assertEqual(out.returncode, 0, out.stderr)
                report = json.loads(out.stdout)
                self.assertEqual(report["rollout"]["state"], state)
                members = {m["member"]: m["state"] for m in report["members"]}
                self.assertEqual(members, {"mini-a": "converging", "mini-b": "current"})

    def test_a_runner_two_changes_behind_lags_inside_the_window(self) -> None:
        pause = 'GLAEDA_RUNNER_GATE_CPU_PAUSE") or '
        older = old_hook(self.hook).replace(pause + "85.0", pause + "70.0")  # a change released before #1572
        self.assertNotEqual(older, old_hook(self.hook))
        self.install("mini-a", "actions-runner-glaeda", older)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        out = self.lag(stable=self.stable(self.head, now))
        self.assertEqual(out.returncode, 1, out.stderr)
        report = json.loads(out.stdout)
        self.assertIsNotNone(report["rollout"]["until"])  # the window is open, and still this runner lags
        self.assertEqual(report["members"][0]["state"], "lags")
        self.assertIn("GATE_CPU_PAUSE", [lag["name"] for lag in report["members"][0]["runners"][0]["lags"]])

    def test_no_touch_and_unregistered_members_are_never_probed(self) -> None:
        for host in ("mini-a", "mini-b", "aws-a", "aws-b", "stranger"):
            self.install(host, "actions-runner-glaeda", self.hook)
        out = self.lag()
        self.assertEqual(out.returncode, 0, out.stderr)
        report = json.loads(out.stdout)
        self.assertEqual([m["member"] for m in report["members"]], ["mini-a", "mini-b"])
        self.assertIn("no-touch", report["skipped"]["aws-a"])
        self.assertIn("no-touch", report["skipped"]["aws-b"])
        self.assertIn("not in the fleet registry", report["skipped"]["stranger"])
        asked = {json.loads(line)["host"] for line in self.log.read_text().splitlines()}
        self.assertEqual(asked, {"mini-a", "mini-b"})  # not even a read-only probe of the others

    def test_no_registry_checks_nothing(self) -> None:
        self.registry.unlink()
        out = self.lag()
        self.assertEqual(out.returncode, 2)
        self.assertIn("fleet registry", out.stderr)
        self.assertFalse(self.log.exists())

    def test_an_unreadable_channel_gives_no_window(self) -> None:
        self.install("mini-a", "actions-runner-glaeda", old_hook(self.hook))
        out = self.lag(stable=self.dir / "missing.json")
        self.assertEqual(out.returncode, 1, out.stderr)
        report = json.loads(out.stdout)
        self.assertEqual(report["rollout"]["state"], "unknown")
        self.assertEqual(report["members"][0]["state"], "lags")

    def test_other_bytes_with_the_same_settings_differ_but_do_not_lag(self) -> None:
        self.install("mini-a", "actions-runner-glaeda", self.hook + "\n# a comment\n")
        self.install("mini-b", "actions-runner-glaeda", self.hook)
        out = self.lag()
        self.assertEqual(out.returncode, 0, out.stderr)
        members = {m["member"]: m["state"] for m in json.loads(out.stdout)["members"]}
        self.assertEqual(members, {"mini-a": "differs", "mini-b": "current"})

    def test_an_unreachable_member_or_a_broken_hook_fails_the_check(self) -> None:
        future = "from __future__ import annotations\n"  # a hook that exits on import, after its first statement
        self.install("mini-a", "actions-runner-glaeda", self.hook.replace(future, future + "raise SystemExit(3)\n", 1))
        out = self.lag(FAKE_DOWN="mini-b")
        self.assertEqual(out.returncode, 1, out.stderr)
        members = {m["member"]: m for m in json.loads(out.stdout)["members"]}
        self.assertEqual(members["mini-b"]["state"], "unreachable")
        self.assertEqual(members["mini-a"]["runners"][0]["state"], "unreadable")

    def test_a_checkout_that_is_not_origin_main_checks_nothing(self) -> None:
        (self.repo / "two.txt").write_text("two\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "two")
        out = self.lag()
        self.assertEqual(out.returncode, 2)
        self.assertIn("is not origin/main", out.stderr)
        self.assertFalse(self.log.exists())  # no member was asked


if __name__ == "__main__":
    unittest.main()
