#!/usr/bin/env python3
"""Validate an exact-source native runner-log build receipt.

The validator is intentionally read-only. It checks the receipt's bounded schema,
optionally compares its checked-out identity with a Git worktree, and optionally
hashes the staged binary. It emits no receipt paths or raw tool output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


RECEIPT_TYPE = "glaeda-runner-log-reclaim-build-receipt"
SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 64 * 1024
MAX_BINARY_BYTES = 512 * 1024 * 1024
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TARGETS = frozenset({"aarch64-apple-darwin"})
TEST_TARGETS = ("--lib runner_log_reclaim::", "--test runner_log_macos")
RECEIPT_KEYS = frozenset(
    {
        "document_type",
        "schema_version",
        "requested_pr_head",
        "checked_out_commit",
        "checked_out_tree",
        "target",
        "toolchain",
        "binary_sha256",
        "test_outcome",
        "test_targets",
    }
)


class ReceiptError(ValueError):
    """A bounded, user-facing receipt validation failure."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReceiptError(message)


def _hex(value: Any, pattern: re.Pattern[str], label: str) -> str:
    _require(isinstance(value, str) and pattern.fullmatch(value) is not None, f"{label} is invalid")
    return value


def _require_regular(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise ReceiptError(f"{label} is unavailable") from error
    _require(stat.S_ISREG(mode), f"{label} is unavailable")


def read_receipt(path: Path) -> dict[str, Any]:
    _require_regular(path, "receipt")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ReceiptError("receipt is unavailable") from error
    _require(size <= MAX_RECEIPT_BYTES, "receipt exceeds the size bound")
    try:
        raw = path.read_bytes()
        _require(len(raw) <= MAX_RECEIPT_BYTES, "receipt exceeds the size bound")
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ReceiptError("receipt is not valid JSON") from error
    _require(isinstance(value, dict), "receipt must be an object")
    return value


def validate_receipt(value: dict[str, Any], *, require_success: bool = True) -> dict[str, Any]:
    _require(set(value) == RECEIPT_KEYS, "receipt fields are unsupported")
    _require(value["document_type"] == RECEIPT_TYPE, "receipt type is unsupported")
    _require(value["schema_version"] == SCHEMA_VERSION, "receipt schema is unsupported")
    _hex(value["requested_pr_head"], HEX40, "requested PR head")
    _hex(value["checked_out_commit"], HEX40, "checked-out commit")
    _hex(value["checked_out_tree"], HEX40, "checked-out tree")
    _require(
        isinstance(value["target"], str) and value["target"] in TARGETS,
        "receipt target is unsupported",
    )
    _require(
        isinstance(value["toolchain"], str) and 1 <= len(value["toolchain"]) <= 256,
        "receipt toolchain is invalid",
    )
    _hex(value["binary_sha256"], HEX64, "binary digest")
    _require(
        isinstance(value["test_outcome"], str)
        and value["test_outcome"] in {"success", "failure", "cancelled", "skipped"},
        "receipt test outcome is invalid",
    )
    targets = value["test_targets"]
    _require(
        isinstance(targets, list) and all(isinstance(item, str) for item in targets),
        "receipt test targets are invalid",
    )
    _require(tuple(targets) == TEST_TARGETS, "receipt test targets are incomplete")
    if require_success:
        _require(value["test_outcome"] == "success", "native tests did not succeed")
    return value


def git_identity(root: Path) -> tuple[str, str]:
    git = shutil.which("git")
    if git is None:
        raise ReceiptError("Git identity is unavailable")
    try:
        result = subprocess.run(
            [os.path.abspath(git), "-C", str(root), "rev-parse", "HEAD", "HEAD^{tree}"],
            env={},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReceiptError("Git identity is unavailable") from error
    values = result.stdout.splitlines()
    _require(result.returncode == 0 and len(values) == 2, "Git identity is unavailable")
    return _hex(values[0], HEX40, "current commit"), _hex(values[1], HEX40, "current tree")


def binary_digest(path: Path) -> str:
    _require_regular(path, "native binary")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ReceiptError("native binary is unavailable") from error
    _require(size <= MAX_BINARY_BYTES, "native binary exceeds the size bound")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ReceiptError("native binary is unreadable") from error
    return digest.hexdigest()


def validate(
    receipt_path: Path,
    *,
    binary_path: Path | None = None,
    git_root: Path | None = None,
    expected_commit: str | None = None,
    expected_tree: str | None = None,
    requested_head: str | None = None,
    require_success: bool = True,
) -> dict[str, Any]:
    receipt = validate_receipt(read_receipt(receipt_path), require_success=require_success)
    if expected_commit is not None:
        _require(
            _hex(expected_commit, HEX40, "expected commit") == receipt["checked_out_commit"],
            "checked-out commit does not match the expected commit",
        )
    if expected_tree is not None:
        _require(
            _hex(expected_tree, HEX40, "expected tree") == receipt["checked_out_tree"],
            "checked-out tree does not match the expected tree",
        )
    if requested_head is not None:
        _require(
            _hex(requested_head, HEX40, "expected requested head") == receipt["requested_pr_head"],
            "requested PR head does not match the expected head",
        )
    if git_root is not None:
        commit, tree = git_identity(git_root)
        _require(
            commit == receipt["checked_out_commit"],
            "checked-out commit does not match Git HEAD",
        )
        _require(
            tree == receipt["checked_out_tree"],
            "checked-out tree does not match Git HEAD",
        )
    if binary_path is not None:
        _require(
            binary_digest(binary_path) == receipt["binary_sha256"],
            "native binary digest does not match the receipt",
        )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path, help="native runner-log receipt JSON")
    parser.add_argument("--binary", type=Path, help="staged native binary to hash")
    parser.add_argument(
        "--git-root", type=Path, help="compare checked-out commit/tree with Git HEAD"
    )
    parser.add_argument("--expected-commit", help="expected checked-out commit")
    parser.add_argument("--expected-tree", help="expected checked-out tree")
    parser.add_argument("--requested-head", help="expected requested PR head")
    parser.add_argument(
        "--allow-failed-tests",
        action="store_true",
        help="validate a diagnostic receipt even when its native tests failed",
    )
    args = parser.parse_args()
    try:
        receipt = validate(
            args.receipt,
            binary_path=args.binary,
            git_root=args.git_root,
            expected_commit=args.expected_commit,
            expected_tree=args.expected_tree,
            requested_head=args.requested_head,
            require_success=not args.allow_failed_tests,
        )
    except ReceiptError as error:
        print(f"native receipt invalid: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "document_type": receipt["document_type"],
                "checked_out_commit": receipt["checked_out_commit"],
                "checked_out_tree": receipt["checked_out_tree"],
                "target": receipt["target"],
                "test_outcome": receipt["test_outcome"],
                "binary_verified": args.binary is not None,
                "source_verified": args.git_root is not None
                or args.expected_commit is not None
                or args.expected_tree is not None,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
