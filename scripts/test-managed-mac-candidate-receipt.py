#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "managed_mac_candidate_receipt", ROOT / "scripts/managed_mac_candidate_receipt.py"
)
assert SPEC and SPEC.loader
receipt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receipt)


class ManagedMacCandidateReceiptTests(unittest.TestCase):
    ENV = {
        "REPOSITORY": "teamleaderleo/glaeda",
        "TARGET": "aarch64-apple-darwin",
        "TOOLCHAIN": "rustc 1.97.1 (fixture)",
        "ADMISSION_BUILD_OUTCOME": "success",
        "ADMISSION_MATRIX_OUTCOME": "success",
        "RUN_ID": "123",
        "RUN_ATTEMPT": "2",
    }

    def test_receipt_binds_artifact_source_and_synthetic_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "glaeda-managed-admission.rlib"
            artifact.write_bytes(b"compiled fixture")
            values = {
                "rev-parse HEAD^{commit}": "a" * 40,
                "rev-parse HEAD^{tree}": "b" * 40,
            }
            result = receipt.build_receipt(
                self.ENV,
                artifact,
                git_reader=lambda *args: values[" ".join(args)],
            )
        self.assertEqual(result["source_commit"], "a" * 40)
        self.assertEqual(result["source_tree"], "b" * 40)
        self.assertEqual(result["checked_out_commit"], "a" * 40)
        self.assertEqual(result["checked_out_tree"], "b" * 40)
        self.assertEqual(result["test_outcome"], "success")
        self.assertEqual(result["binary_name"], "glaeda-managed-admission.rlib")
        self.assertTrue(receipt.valid_receipt(result))
        self.assertEqual(result["synthetic_receipt_matrix"]["log"], "managed-admission-matrix.log")
        self.assertFalse(result["synthetic_receipt_matrix"]["live_deletion_enabled"])
        fixture = result["synthetic_fixture"]
        self.assertEqual(fixture["mode"], "synthetic_fixture")
        self.assertFalse(fixture["live_deletion_enabled"])
        self.assertIsNone(fixture["runner_root"])

    def test_failure_receipt_is_stable_when_build_or_artifact_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            result = receipt.build_receipt(
                {
                    **self.ENV,
                    "ADMISSION_BUILD_OUTCOME": "failure",
                    "ADMISSION_MATRIX_OUTCOME": "skipped",
                    "FAILURE_STAGE": "managed_mac_admission_build",
                    "FAILURE_REASON": "compile_failed",
                },
                Path(directory) / "missing.rlib",
                git_reader=lambda *args: "c" * 40,
            )
        self.assertFalse(receipt.valid_receipt(result))
        self.assertIsNone(result["binary_sha256"])
        self.assertEqual(result["failure"], {
            "stage": "managed_mac_admission_build",
            "reason": "compile_failed",
        })
        self.assertEqual(result["synthetic_fixture"]["runner_root"], None)

    def test_workflow_builds_library_and_runs_synthetic_matrix_without_root(self):
        workflow = (ROOT / ".github/workflows/fleet-candidate.yml").read_text(encoding="utf-8")
        for snippet in (
            "Build managed Mac admission adapter",
            "glaeda-managed-admission.rlib",
            "production_recovery_adapter::",
            "ADMISSION_MATRIX_OUTCOME",
            "managed_mac_candidate_receipt.py",
            "if: always()",
            "target/fleet-bundles/managed-admission-receipt.json",
        ):
            self.assertIn(snippet, workflow)
        self.assertNotIn("--runner-root", workflow)
        self.assertNotIn("--apply", workflow)


if __name__ == "__main__":
    unittest.main()
