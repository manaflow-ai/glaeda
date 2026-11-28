#!/usr/bin/env python3
"""Bounded contract tests for glaeda-linux-vm-personal with the base suite's fake limactl; no VM, no network."""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import pathlib
import plistlib
import sys
import tempfile
import unittest


def load(name: str, path: pathlib.Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


ROOT = pathlib.Path(__file__).resolve().parents[1]
fixtures = load("test_glaeda_linux_vm_base", ROOT / "scripts" / "test-glaeda-linux-vm-base.py")
personal = load("glaeda_linux_vm_personal", ROOT / "scripts" / "glaeda-linux-vm-personal")
base = personal.base


class FakeLima(fixtures.FakeLima):
    """The base fake plus `limactl create`, which writes the instance and leaves it Stopped like the real one."""

    def __call__(self, argv, *, env, timeout):
        if argv[1:3] == ["--tty=false", "create"]:
            self.calls.append(list(argv))
            self.envs.append(dict(env))
            name, home = argv[4], pathlib.Path(env["LIMA_HOME"])
            (home / name).mkdir(parents=True)
            (home / name / "lima.yaml").write_bytes(pathlib.Path(argv[-1]).read_bytes())
            self.status[name] = "Stopped"
            return fixtures.ok()
        return super().__call__(argv, env=env, timeout=timeout)


class PersonalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self.tmp.name)
        self.root, self.home = tmp / "linux-vm", tmp / "home"
        self.archive = fixtures.lima_archive()
        self.downloads = 0
        self.fake = FakeLima(self.root)
        self.boots = True
        self.launchctl_calls: list[list[str]] = []
        self._pin = (base.LIMA_BYTES, base.LIMA_SHA256)
        base.LIMA_BYTES, base.LIMA_SHA256 = len(self.archive), hashlib.sha256(self.archive).hexdigest()

    def tearDown(self):
        base.LIMA_BYTES, base.LIMA_SHA256 = self._pin
        self.tmp.cleanup()

    def download(self, url, dest, timeout):
        self.downloads += 1
        dest.write_bytes(self.archive)

    def launchctl(self, argv):
        """launchd: loading the RunAtLoad job or kickstarting it runs `limactl start linux` outside any session."""
        self.launchctl_calls.append(argv)
        if argv[0] in ("bootstrap", "kickstart") and "linux" in self.fake.status and self.boots:
            self.fake.status["linux"] = "Running"
        return fixtures.ok("", 3 if argv[0] == "bootout" else 0)

    def make(self, host=None):
        made = personal.Personal(root=self.root, home=self.home, uid=501, launchctl=self.launchctl, runner=self.fake,
                                 download=self.download, host=host or fixtures.HOST, sleep=lambda s: None)
        made.start_timeout = 0
        return made

    def test_plan_has_no_side_effects(self):
        out = self.make().execute(apply=False)
        self.assertEqual(out["plan"], ["install_lima", "materialize_template", "create", "start", "login_item", "wrapper"])
        self.assertEqual(out["sizing"], {"cpus": 7, "memory_gib": 12, "disk_gib": 100})
        self.assertFalse(self.root.exists())
        self.assertFalse(self.home.exists())
        self.assertEqual(self.downloads, 0)

    def test_sizing_is_capped_and_floored(self):
        self.assertEqual(personal.sizing({"cpus": 28, "memory_gib": 192}), {"cpus": 8, "memory_gib": 16, "disk_gib": 100})
        self.assertEqual(personal.sizing({"cpus": 1, "memory_gib": 4}), {"cpus": 1, "memory_gib": 2, "disk_gib": 100})

    def test_apply_creates_running_vm_login_item_and_wrapper_then_is_idempotent(self):
        out = self.make().execute(apply=True)
        self.assertEqual(out["disposition"], "changed")
        self.assertEqual(out["status"], "Running")
        self.assertEqual(out["guest_arch"], "aarch64")
        create = [c for c in self.fake.calls if c[2:3] == ["create"]]
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0][3:8], ["--name", "linux", "--cpus=7", "--memory=12", "--disk=100"])
        # The VM is booted by launchd, never by a `limactl start` in this process.
        self.assertFalse([c for c in self.fake.calls if c[2:3] == ["start"]])
        self.assertEqual(self.launchctl_calls[-1], ["kickstart", "gui/501/com.glaeda.linux-vm"])
        self.assertEqual(pathlib.Path(create[0][-1]).read_bytes(), personal.TEMPLATE_YAML.read_bytes())
        self.assertTrue(all(env["LIMA_HOME"] == str(self.root / "personal-home") for env in self.fake.envs))
        plist = plistlib.loads((self.home / "Library/LaunchAgents/com.glaeda.linux-vm.plist").read_bytes())
        self.assertEqual(plist["ProgramArguments"], [str(self.root / "lima-2.2.0/bin/limactl"), "--tty=false", "start", "linux"])
        self.assertEqual(plist["EnvironmentVariables"]["LIMA_HOME"], str(self.root / "personal-home"))
        self.assertTrue(plist["RunAtLoad"])
        self.assertNotIn("KeepAlive", plist)
        self.assertIn(["bootstrap", "gui/501", str(self.home / "Library/LaunchAgents/com.glaeda.linux-vm.plist")], self.launchctl_calls)
        wrapper = self.home / ".local/bin/glaeda-linux"
        self.assertEqual(wrapper.stat().st_mode & 0o777, 0o755)
        self.assertIn(f"LIMA_HOME='{self.root / 'personal-home'}'", wrapper.read_text())
        self.assertEqual(json.loads((self.root / "personal.json").read_text())["state"], "created")
        self.fake.calls.clear()
        self.launchctl_calls.clear()
        again = self.make().execute(apply=True)
        self.assertEqual(again["disposition"], "unchanged")
        self.assertEqual(again["plan"], [])
        self.assertEqual(self.downloads, 1)
        self.assertEqual(self.launchctl_calls, [])
        self.assertFalse([c for c in self.fake.calls if c[2:3] in (["start"], ["stop"], ["delete"])])

    def test_stopped_vm_is_started(self):
        self.make().execute(apply=True)
        self.fake.status["linux"] = "Stopped"
        out = self.make().execute(apply=True)
        self.assertEqual(out["plan"], ["start"])
        self.assertEqual(out["status"], "Running")
        self.assertEqual(self.launchctl_calls[-1], ["kickstart", "gui/501/com.glaeda.linux-vm"])

    def test_create_cut_off_before_boot_is_finished_by_a_rerun(self):
        self.boots = False  # the first run's session died: the instance exists, never booted
        with self.assertRaises(base.Failed):
            self.make().execute(apply=True)
        self.assertEqual(self.fake.status["linux"], "Stopped")
        self.boots = True
        out = self.make().execute(apply=True)
        self.assertEqual(out["plan"], ["start"])
        self.assertEqual(out["status"], "Running")
        self.assertEqual(len([c for c in self.fake.calls if c[2:3] == ["create"]]), 1)

    def test_guest_shell_is_retried_while_sshd_comes_up(self):
        answers = iter([fixtures.ok("", 255, "connection refused")] * 3)
        guest = self.fake.guest
        self.fake.guest = lambda argv: next(answers, None) or guest(argv)
        out = self.make().execute(apply=True)
        self.assertEqual(out["guest_arch"], "aarch64")

    def test_foreign_instance_is_left_alone(self):
        self.make().execute(apply=True)
        (self.root / "personal.json").unlink()
        with self.assertRaises(base.Refused):
            self.make().execute(apply=True)

    def test_base_vm_home_is_separate(self):
        self.make().execute(apply=True)
        self.assertFalse((self.root / "lima-home").exists())

    def test_status_is_read_only(self):
        out = self.make().execute(apply=False, status_only=True)
        self.assertEqual(out["status"], "absent")
        self.assertIn("ssh.config lima-linux", out["reach"]["ssh"])
        self.assertFalse(self.root.exists())

    def test_refuses_off_apple_silicon(self):
        with self.assertRaises(base.Refused):
            self.make(host={**fixtures.HOST, "machine": "x86_64"}).execute(apply=False)

    def test_refuses_low_disk_before_create(self):
        with self.assertRaises(base.Refused):
            self.make(host={**fixtures.HOST, "free_gib": 10}).execute(apply=True)
        self.assertEqual(self.downloads, 0)


if __name__ == "__main__":
    unittest.main()
