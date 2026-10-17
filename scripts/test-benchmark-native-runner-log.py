#!/usr/bin/env python3
"""Focused tests for the native runner-log benchmark's fail-closed accounting."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "benchmark_native_runner_log", ROOT / "scripts/benchmark-native-runner-log.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BenchmarkAccountingTest(unittest.TestCase):
    def test_reclaim_not_ready_blocks_comparison(self) -> None:
        status, reasons = MODULE.classify_status(
            [{"implementation": "native", "native_errors": ["reclaim_not_ready"],
              "effect_valid": False}],
            source_matches_checkout=True,
            native_available=True,
        )
        self.assertEqual(status, "blocked_native_effect_comparison")
        self.assertIn("native_apply_not_admitted", reasons)

    def test_missing_adapter_blocks_without_native_metrics(self) -> None:
        status, reasons = MODULE.classify_status(
            [{"implementation": "python", "valid": True}],
            source_matches_checkout=None,
            native_available=False,
        )
        self.assertEqual(status, "blocked_native_effect_comparison")
        self.assertIn("native_adapter_binary_unavailable", reasons)

    def test_native_receipt_summary_counts_actions_bytes_and_ids(self) -> None:
        report = {"reclaimed_count": 1, "errors": [], "actions": [{
            "receipt_id": "0000000000000001-0000000000000002",
            "identity": {"size": 7},
        }]}
        self.assertEqual(MODULE.native_receipt_summary(report), {
            "action_count": 1,
            "bytes": 7,
            "receipt_ids": ["0000000000000001-0000000000000002"],
            "identity_kind": "native_device_inode",
            "reclaimed_count": 1,
            "errors": [],
        })

    def test_python_receipt_summary_is_bounded_and_typed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            logs = home / "Library/Logs"
            logs.mkdir(parents=True)
            (logs / "cruft-sweep.jsonl").write_text(json.dumps({
                "batch_id": "batch-1", "bytes": 7, "removed": True,
                "path": "/private/fixture/path.log",
            }) + "\n")
            summary = MODULE.python_receipt_summary(home)
        self.assertEqual(summary["action_count"], 1)
        self.assertEqual(summary["bytes"], 7)
        self.assertEqual(summary["receipt_ids"], ["batch-1"])
        self.assertEqual(summary["identity_kind"], "python_batch_id")

    def test_diagnostics_redact_private_fixture_paths(self) -> None:
        self.assertNotIn("/private/var/folders", MODULE.redact_output(
            "/private/var/folders/aa/bb/T/glaeda-bench-python/home/log\n"
        ))


if __name__ == "__main__":
    unittest.main()
