#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fleet_candidate_receipt", ROOT / "scripts/fleet_candidate_receipt.py")
assert SPEC and SPEC.loader
receipt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receipt)


class FleetCandidateReceiptTests(unittest.TestCase):
    ENV = {
        "EVENT_NAME": "pull_request",
        "REPOSITORY": "teamleaderleo/glaeda",
        "REQUESTED_HEAD": "a" * 40,
        "WORKFLOW_SHA": "b" * 40,
        "RUN_ID": "123",
        "RUN_ATTEMPT": "2",
        "TARGET": "aarch64-apple-darwin",
        "RUNNER_NAME": "GitHub Actions 42",
        "RUNNER_OS": "macOS",
        "RUNNER_ARCH": "ARM64",
        "JOB_STATUS": "failure",
        "BUNDLE_TEST_OUTCOME": "success",
        "CANDIDATE_BUILD_OUTCOME": "success",
        "NATIVE_BUILD_OUTCOME": "success",
        "NATIVE_TEST_OUTCOME": "failure",
    }

    def test_receipt_binds_candidate_to_checked_out_commit_and_tree(self):
        source = {"repository": "teamleaderleo/glaeda", "commit": "c" * 40, "tree": "d" * 40}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps({"archive": "candidate.tar.gz", "sha256": "e" * 64, "source": source}))
            values = {"rev-parse HEAD^{commit}": source["commit"], "rev-parse HEAD^{tree}": source["tree"]}
            result = receipt.build_receipt(self.ENV, path, git_reader=lambda *args: values[" ".join(args)])
        self.assertEqual(result["checked_out_commit"], source["commit"])
        self.assertEqual(result["checked_out_tree"], source["tree"])
        self.assertTrue(result["candidate_source_matches_checkout"])
        self.assertEqual(result["source_mode"], "pull_request_merge")
        self.assertEqual(result["steps"]["native_test"], "failure")
        self.assertTrue(receipt.exact_source_matches(result))

    def test_receipt_is_still_written_when_candidate_receipt_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            result = receipt.build_receipt(
                {**self.ENV, "EVENT_NAME": "workflow_dispatch"},
                Path(directory) / "missing.json",
                git_reader=lambda *args: None,
            )
        self.assertEqual(result["source_mode"], "workflow_sha")
        self.assertIsNone(result["checked_out_commit"])
        self.assertNotIn("candidate_source", result)
        self.assertFalse(receipt.exact_source_matches(result))

    def test_exact_source_guard_rejects_a_different_checkout_commit(self):
        source = {"repository": "teamleaderleo/glaeda", "commit": "c" * 40, "tree": "d" * 40}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps({"source": source}))
            result = receipt.build_receipt(
                self.ENV,
                path,
                git_reader=lambda *args: "f" * 40 if args[-1] == "HEAD^{commit}" else "e" * 40,
            )
        self.assertFalse(receipt.exact_source_matches(result))


if __name__ == "__main__":
    unittest.main()
