#!/usr/bin/env python3
"""Focused tests for the report-only runner-log handoff adapter."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    "glaeda_runner_log_handoff", os.fspath(ROOT / "scripts" / "glaeda-runner-log-handoff")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec and spec.loader
handoff = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = handoff
spec.loader.exec_module(handoff)


class RunnerLogHandoffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.runner = self.root / "runner"
        (self.runner / "_diag").mkdir(parents=True)
        os.chmod(self.runner, 0o700)
        (self.runner / ".glaeda-cmux-runner").write_text("install-1\n")
        self.receipt = self.root / "enrollment.json"
        self.receipt.write_text(json.dumps({
            "runnerDirOwned": True,
            "runnerDir": os.fspath(self.runner),
            "installId": "install-1",
        }))
        self.observer_args = self.root / "observer-args"
        self.observer = self.root / "observer"
        self.observer.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" > {self.observer_args}\n"
            "printf '%s\\n' '{\"document_type\":\"glaeda-runner-log-reclaim\",\"operation\":\"plan\",\"errors\":[],\"actions\":[{\"relative_path\":\"_diag/old.log\",\"decision\":\"expired_runner_diagnostic_log\",\"receipt_id\":\"r-1\"}],\"observation\":{\"complete\":true,\"eligible_count\":1}}'\n"
        )
        self.observer.chmod(self.observer.stat().st_mode | stat.S_IXUSR)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def args(self, state_root: Path | None = None) -> object:
        parser = __import__("argparse").Namespace(
            runner_root=self.runner,
            enrollment_receipt=self.receipt,
            state_root=state_root,
            observer=os.fspath(self.observer),
            minimum_idle_seconds=3_600,
            batch_size=16,
        )
        return parser

    def _journal_row(self, phase: str, tx_id: str = "tx-1") -> dict[str, object]:
        root = self.runner.stat()
        return {
            "tx_id": tx_id,
            "phase": phase,
            "install_id": "install-1",
            "root_device": int(root.st_dev) & 0xFFFF_FFFF,
            "root_inode": int(root.st_ino),
            "root_uid": int(root.st_uid),
            "relative_path": "_diag/old.log",
            "stage_name": "_diag/.glaeda-reclaiming-tx-1",
            "identity": {
                "device": 1,
                "inode": 2,
                "links": 1,
                "size": 1,
                "modified_seconds": 1,
                "changed_seconds": 1,
                "modified_nanos": 0,
                "changed_nanos": 0,
            },
        }

    def test_plan_invocation_never_passes_apply_and_reports_pending_recovery(self) -> None:
        state = self.root / "state"
        state.mkdir(mode=0o700)
        (state / "journal.jsonl").write_text(json.dumps(self._journal_row("staged")) + "\n")
        code, result = handoff.run(self.args(state))
        self.assertEqual(code, 0)
        self.assertEqual(result["recovery"]["recovery_disposition"], "pending_recovery")
        self.assertEqual(result["recovery"]["pending_count"], 1)
        self.assertEqual(result["actions"], [{"decision": "expired_runner_diagnostic_log", "receipt_id": "r-1", "relative_path": "_diag/old.log"}])
        self.assertNotIn("--apply", self.observer_args.read_text())

    def test_prepared_row_is_reported_as_recovery_blocked(self) -> None:
        state = self.root / "state"
        state.mkdir(mode=0o700)
        (state / "journal.jsonl").write_text(json.dumps(self._journal_row("prepared")) + "\n")
        code, result = handoff.run(self.args(state))
        self.assertEqual(code, 0)
        self.assertEqual(result["recovery"]["recovery_disposition"], "recovery_blocked_prepared")
        self.assertEqual(result["recovery"]["pending_phases"], ["prepared"])

    def test_malformed_journal_fails_closed_without_mutation(self) -> None:
        state = self.root / "state"
        state.mkdir(mode=0o700)
        journal = state / "journal.jsonl"
        journal.write_text("not-json\n")
        before = journal.read_bytes()
        code, result = handoff.run(self.args(state))
        self.assertEqual(code, 1)
        self.assertEqual(result["errors"], ["journal_invalid"])
        self.assertEqual(journal.read_bytes(), before)

    def test_missing_state_is_clean_and_report_only(self) -> None:
        code, result = handoff.run(self.args(self.root / "missing-state"))
        self.assertEqual(code, 0)
        self.assertEqual(result["recovery"]["recovery_disposition"], "clean")
        self.assertFalse(result["recovery"]["state_root_present"])


if __name__ == "__main__":
    unittest.main()
