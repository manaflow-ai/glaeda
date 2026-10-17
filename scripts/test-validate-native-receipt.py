#!/usr/bin/env python3
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "validate_native_receipt", Path(__file__).with_name("validate-native-receipt.py")
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def receipt(binary: bytes) -> dict[str, object]:
    return {
        "document_type": MODULE.RECEIPT_TYPE,
        "schema_version": MODULE.SCHEMA_VERSION,
        "requested_pr_head": "a" * 40,
        "checked_out_commit": "b" * 40,
        "checked_out_tree": "c" * 40,
        "target": "aarch64-apple-darwin",
        "toolchain": "rustc 1.97.1 (fixture)",
        "binary_sha256": hashlib.sha256(binary).hexdigest(),
        "test_outcome": "success",
        "test_targets": list(MODULE.TEST_TARGETS),
    }


class NativeReceiptTests(unittest.TestCase):
    def test_validates_exact_identity_and_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "observer"
            binary.write_bytes(b"native fixture")
            path = root / "receipt.json"
            path.write_text(json.dumps(receipt(binary.read_bytes())), encoding="utf-8")
            value = MODULE.validate(
                path,
                binary_path=binary,
                expected_commit="b" * 40,
                expected_tree="c" * 40,
                requested_head="a" * 40,
            )
            self.assertEqual(value["target"], "aarch64-apple-darwin")

    def test_rejects_identity_or_binary_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "observer"
            binary.write_bytes(b"native fixture")
            path = root / "receipt.json"
            path.write_text(json.dumps(receipt(binary.read_bytes())), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ReceiptError, "expected commit"):
                MODULE.validate(path, expected_commit="d" * 40)
            binary.write_bytes(b"tampered")
            with self.assertRaisesRegex(MODULE.ReceiptError, "binary digest"):
                MODULE.validate(path, binary_path=binary)

    def test_rejects_unknown_fields_incomplete_tests_and_failed_success_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            value = receipt(b"fixture")
            value["unexpected"] = True
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ReceiptError, "fields"):
                MODULE.validate(path)
            value = receipt(b"fixture")
            value["test_targets"] = ["--test runner_log_macos"]
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ReceiptError, "test targets"):
                MODULE.validate(path)
            value = receipt(b"fixture")
            value["test_outcome"] = "failure"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ReceiptError, "did not succeed"):
                MODULE.validate(path)
            MODULE.validate(path, require_success=False)

            value = receipt(b"fixture")
            value["target"] = []
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ReceiptError, "target"):
                MODULE.validate(path)
            value = receipt(b"fixture")
            value["test_outcome"] = []
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.ReceiptError, "outcome"):
                MODULE.validate(path)

    def test_rejects_receipt_over_size_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_bytes(b"{" + b"x" * MODULE.MAX_RECEIPT_BYTES + b"}")
            with self.assertRaisesRegex(MODULE.ReceiptError, "size bound"):
                MODULE.read_receipt(path)

    def test_rejects_symlinked_receipt_and_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "observer"
            binary.write_bytes(b"native fixture")
            receipt_path = root / "receipt.json"
            receipt_path.write_text(json.dumps(receipt(binary.read_bytes())), encoding="utf-8")
            receipt_link = root / "receipt-link.json"
            receipt_link.symlink_to(receipt_path)
            with self.assertRaisesRegex(MODULE.ReceiptError, "receipt is unavailable"):
                MODULE.read_receipt(receipt_link)
            binary_link = root / "observer-link"
            binary_link.symlink_to(binary)
            with self.assertRaisesRegex(MODULE.ReceiptError, "native binary is unavailable"):
                MODULE.validate(receipt_path, binary_path=binary_link)


if __name__ == "__main__":
    unittest.main()
