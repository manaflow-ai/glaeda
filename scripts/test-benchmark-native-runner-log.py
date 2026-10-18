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
    def _complete_native_report(self) -> dict:
        return {
            "recovery_receipt": {
                "pending_at_start": 0,
                "pending_after": 0,
                "outcome": "clean",
            },
            "item_receipts": [{
                "receipt_id": "item-1",
                "identity": {"device": 1, "inode": 2, "size": 7},
                "outcome": "reclaimed",
            }],
            "post_action_receipt": {"pending_count": 0, "observed": True},
        }

    def test_native_matrix_requires_recovery_item_and_post_action_receipts(self) -> None:
        report = self._complete_native_report()
        self.assertTrue(MODULE.native_receipts_complete(report, expected_items=1))
        for field in ("recovery_receipt", "item_receipts", "post_action_receipt"):
            incomplete = dict(report)
            incomplete.pop(field)
            self.assertFalse(
                MODULE.native_receipts_complete(incomplete, expected_items=1), field
            )

    def test_native_matrix_rejects_pending_recovery_or_unobserved_post_action(self) -> None:
        report = self._complete_native_report()
        report["recovery_receipt"]["pending_after"] = 1
        self.assertFalse(MODULE.native_receipts_complete(report, expected_items=1))
        report = self._complete_native_report()
        report["recovery_receipt"]["pending_at_start"] = -1
        self.assertFalse(MODULE.native_receipts_complete(report, expected_items=1))
        report = self._complete_native_report()
        report["post_action_receipt"]["pending_count"] = False
        self.assertFalse(MODULE.native_receipts_complete(report, expected_items=1))
        report = self._complete_native_report()
        report["post_action_receipt"]["observed"] = False
        self.assertFalse(MODULE.native_receipts_complete(report, expected_items=1))

    def test_matrix_status_blocks_missing_receipts_or_warm_state(self) -> None:
        status, reasons = MODULE.classify_status(
            [{"implementation": "native", "native_errors": [], "effect_valid": True,
              "native_receipts_complete": False, "warm_state_preserved": False}],
            source_matches_checkout=True,
            native_available=True,
        )
        self.assertEqual(status, "blocked_native_effect_comparison")
        self.assertIn("native_authority_receipts_incomplete", reasons)
        self.assertIn("warm_state_not_preserved", reasons)

    def test_matrix_status_requires_exact_source_tree_match(self) -> None:
        status, reasons = MODULE.classify_status(
            [{"implementation": "native", "native_errors": [], "effect_valid": True,
              "native_receipts_complete": True, "warm_state_preserved": True}],
            source_matches_checkout=False,
            native_available=True,
        )
        self.assertEqual(status, "blocked_native_effect_comparison")
        self.assertIn("native_source_receipt_mismatch", reasons)

    def test_managed_adapter_receipt_must_bind_exact_source_and_disable_live_apply(self) -> None:
        receipt = {
            "document_type": "glaeda-managed-mac-admission-build-receipt",
            "schema_version": 1,
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "checked_out_commit": "a" * 40,
            "checked_out_tree": "b" * 40,
            "build_outcome": "success",
            "synthetic_matrix_outcome": "success",
            "binary_sha256": "c" * 64,
            "synthetic_fixture": {
                "mode": "synthetic_fixture",
                "live_deletion_enabled": False,
                "runner_root": None,
            },
        }
        MODULE.validate_managed_adapter_receipt(
            receipt, source_commit="a" * 40, source_tree="b" * 40
        )
        for field, value in (("source_tree", "d" * 40), ("synthetic_matrix_outcome", "failure")):
            invalid = dict(receipt)
            invalid[field] = value
            with self.assertRaises(ValueError):
                MODULE.validate_managed_adapter_receipt(
                    invalid, source_commit="a" * 40, source_tree="b" * 40
                )

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
