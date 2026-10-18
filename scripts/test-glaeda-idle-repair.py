#!/usr/bin/env python3
"""Focused contract tests for the idle repair queue."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("idle_repair", str(ROOT / "scripts/glaeda-idle-repair"))
spec = importlib.util.spec_from_loader("idle_repair", loader)
idle = importlib.util.module_from_spec(spec)
loader.exec_module(idle)


class IdleRepairTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        idle.HOME = home
        idle.STATE_DIR = home / ".local/state/glaeda/idle-repair"
        idle.STATE = idle.STATE_DIR / "queue.json"
        idle.RECEIPT = idle.STATE_DIR / "receipts.jsonl"
        idle.LOCK = idle.STATE_DIR / "run.lock"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_busy_host_is_queued_without_running_repairs(self) -> None:
        disk = mock.Mock()
        disk.host_busy.return_value = "a build is running"
        with mock.patch.object(idle, "load_disk_module", return_value=disk), \
             mock.patch.object(idle, "run") as run:
            self.assertEqual(idle.main(["--apply"]), idle.QUEUED_EXIT)
        run.assert_not_called()
        state = json.loads(idle.STATE.read_text())
        self.assertEqual(state["state"], "queued")
        self.assertEqual(state["reason"], "a build is running")

    def test_idle_host_runs_disk_then_health_and_records_receipt(self) -> None:
        disk = mock.Mock()
        disk.host_busy.return_value = ""
        with mock.patch.dict(idle.os.environ, {"GLAEDA_ROLE": "devbox"}), \
             mock.patch.object(idle, "load_disk_module", return_value=disk), \
             mock.patch.object(idle, "run", side_effect=[(0, "disk ok", ""), (0, "health ok", "")]) as run:
            self.assertEqual(idle.main(["--apply"]), 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][2:4], ["--role", "devbox"])
        state = json.loads(idle.STATE.read_text())
        self.assertEqual(state["state"], "idle")
        self.assertEqual([a["exit"] for a in state["actions"]], [0, 0])
        self.assertEqual([call.args[1] for call in run.call_args_list], [660, 180])

    def test_successful_repair_waits_until_next_attempt(self) -> None:
        disk = mock.Mock()
        disk.host_busy.return_value = ""
        with mock.patch.object(idle, "load_disk_module", return_value=disk), \
             mock.patch.object(idle, "run", side_effect=[(0, "disk ok", ""), (0, "health ok", "")]) as run, \
             mock.patch.object(idle.time, "time", side_effect=[1000, 1001]):
            self.assertEqual(idle.main(["--apply"]), 0)
            self.assertEqual(idle.main(["--apply"]), 0)
        self.assertEqual(run.call_count, 2)

    def test_status_is_read_only(self) -> None:
        idle.write_state({"state": "queued", "reason": "reservation"})
        with mock.patch.object(idle, "load_disk_module") as load:
            self.assertEqual(idle.main(["--status"]), 0)
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
