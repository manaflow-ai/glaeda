#!/usr/bin/env python3
"""Bounded contract tests for the elastic macOS VM controller."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import dataclasses
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader("glaeda_macos_vm", str(ROOT / "scripts" / "glaeda-macos-vm"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC
vm = importlib.util.module_from_spec(SPEC)
sys.modules[LOADER.name] = vm
LOADER.exec_module(vm)
FAKE_LUME = Path(tempfile.gettempdir()) / "glaeda-test-lume"
FAKE_LUME.write_text("#!/bin/sh\n")
FAKE_LUME.chmod(0o700)


class FakeRunner:
    def __init__(self, state="stopped", workers=False):
        self.state = state
        self.workers = workers
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, home, timeout):
        self.calls.append(list(argv))
        if argv[0] == str(FAKE_LUME) and argv[1] == "ls":
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"name": "glaeda-desktop", "id": "vm-1", "state": self.state,
                "os": "macOS", "cpuCount": 8, "memorySize": 16 * 1024 ** 3,
                "diskSize": {"total": 80 * 1024 ** 3}, "locationName": "default"}]), stderr="")
        if argv[0] == str(FAKE_LUME) and argv[1] == "ssh":
            return SimpleNamespace(returncode=0 if self.workers else 1, stdout="123\n" if self.workers else "", stderr="")
        if argv[0] == "/bin/df":
            return SimpleNamespace(returncode=0, stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/disk 200000000 1 150000000 1% /Users/Shared\n", stderr="")
        if argv[0] == "/usr/bin/top":
            return SimpleNamespace(returncode=0, stdout="CPU usage: 20.00% user, 10.00% sys, 70.00% idle\n", stderr="")
        if argv[0] == "/usr/sbin/sysctl":
            return SimpleNamespace(returncode=0, stdout="1\n", stderr="")
        if argv[0] == str(FAKE_LUME) and argv[1] in ("run", "stop"):
            self.state = "running" if argv[1] == "run" else "stopped"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)


def policy() -> vm.VmPolicy:
    return vm.VmPolicy(provider_path=str(FAKE_LUME), provider_id="vm-1", min_free_gib=1)


class ControllerTests(unittest.TestCase):
    def test_observation_uses_exact_vm_identity_and_parses_host_evidence(self):
        fake = FakeRunner()
        observed = vm.observe(policy(), home="/tmp/home", gui_queue_depth=0, run=fake)
        self.assertEqual((observed.vm_state, observed.job_activity), ("stopped", "unknown"))
        self.assertEqual(observed.host_cpu_busy_pct, 30)
        self.assertEqual(observed.memory_pressure, 1)
        self.assertGreater(observed.host_free_bytes, 1 << 30)

    def test_pressure_stops_only_after_idle_barrier(self):
        fake = FakeRunner(state="running", workers=True)
        observed = vm.observe(policy(), home="/tmp/home", gui_queue_depth=0, run=fake)
        observed = dataclasses.replace(observed, host_cpu_busy_pct=90)
        decision = vm.plan(policy(), observed)
        self.assertEqual(decision["disposition"], "blocked")
        self.assertEqual(decision["actions"], [])
        self.assertIn("active", " ".join(decision["blockers"]))

    def test_headroom_and_waiting_gui_job_starts_stopped_vm(self):
        fake = FakeRunner()
        observed = vm.observe(policy(), home="/tmp/home", gui_queue_depth=1, run=fake)
        decision = vm.plan(policy(), observed)
        self.assertEqual((decision["disposition"], decision["actions"]), ("ready", ["start"]))

    def test_apply_is_disabled_until_ownership_and_drain_are_proven(self):
        fake = FakeRunner()
        observed = vm.observe(policy(), home="/tmp/home", gui_queue_depth=1, run=fake)
        decision = vm.plan(policy(), observed)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with self.assertRaisesRegex(vm.VmError, "apply is disabled"):
                vm.apply_plan(policy(), decision, home="/tmp/home", state_path=path, run=fake)
            self.assertFalse(path.exists())
            self.assertFalse(any(call[0] == str(FAKE_LUME) and call[1] == "run" for call in fake.calls))

    def test_unknown_lume_identity_refuses(self):
        fake = FakeRunner()
        original = fake.__call__
        def bad(argv, *, home, timeout):
            result = original(argv, home=home, timeout=timeout)
            if argv[:2] == [str(FAKE_LUME), "ls"]:
                result.stdout = json.dumps([{"name": "other", "state": "stopped", "os": "macOS",
                    "cpuCount": 8, "memorySize": 16 * 1024 ** 3,
                    "diskSize": {"total": 80 * 1024 ** 3}, "locationName": "default"}])
            return result
        with self.assertRaises(vm.VmError):
            vm.observe(policy(), home="/tmp/home", gui_queue_depth=0, run=bad)


if __name__ == "__main__":
    unittest.main()
