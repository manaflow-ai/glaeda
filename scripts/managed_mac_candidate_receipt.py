#!/usr/bin/env python3
"""Write a bounded receipt for the managed Mac admission candidate.

The candidate contains the compiled Glaeda library artifact and a synthetic
receipt-backed adapter matrix.  This receipt deliberately records that the
matrix did not open a runner root or enable deletion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable, Mapping


def git_value(*args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _env(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name, "")
    return value or None


def _digest(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except (OSError, ValueError):
        return None


def _toolchain(environ: Mapping[str, str]) -> str | None:
    value = _env(environ, "TOOLCHAIN")
    if value:
        return value
    try:
        return subprocess.check_output(["rustc", "--version"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def build_receipt(
    environ: Mapping[str, str],
    artifact: Path,
    *,
    git_reader: Callable[..., str | None] = git_value,
) -> dict[str, object]:
    checked_out_commit = git_reader("rev-parse", "HEAD^{commit}")
    checked_out_tree = git_reader("rev-parse", "HEAD^{tree}")
    binary_sha256 = _digest(artifact)
    build_outcome = _env(environ, "ADMISSION_BUILD_OUTCOME")
    matrix_outcome = _env(environ, "ADMISSION_MATRIX_OUTCOME")
    receipt: dict[str, object] = {
        "document_type": "glaeda-managed-mac-admission-build-receipt",
        "schema_version": 1,
        "repository": _env(environ, "REPOSITORY"),
        "requested_head": _env(environ, "REQUESTED_HEAD"),
        "source_commit": checked_out_commit,
        "source_tree": checked_out_tree,
        "checked_out_commit": checked_out_commit,
        "checked_out_tree": checked_out_tree,
        "target": _env(environ, "TARGET"),
        "toolchain": _toolchain(environ),
        "binary_name": artifact.name if binary_sha256 else None,
        "binary_sha256": binary_sha256,
        "build_outcome": build_outcome,
        "synthetic_matrix_outcome": matrix_outcome,
        "test_outcome": matrix_outcome,
        "synthetic_receipt_matrix": {
            "outcome": matrix_outcome,
            "log": "managed-admission-matrix.log",
            "live_deletion_enabled": False,
        },
        "synthetic_fixture": {
            "mode": "synthetic_fixture",
            "live_deletion_enabled": False,
            "runner_root": None,
        },
        "run_id": _env(environ, "RUN_ID"),
        "run_attempt": _env(environ, "RUN_ATTEMPT"),
    }
    if build_outcome != "success" or matrix_outcome != "success" or binary_sha256 is None:
        receipt["failure"] = {
            "stage": _env(environ, "FAILURE_STAGE") or "managed_mac_admission",
            "reason": _env(environ, "FAILURE_REASON") or "candidate_artifact_incomplete",
        }
    return receipt


def valid_receipt(receipt: Mapping[str, object]) -> bool:
    """Return whether the receipt proves a synthetic, exact-source candidate."""
    source_commit = receipt.get("source_commit")
    source_tree = receipt.get("source_tree")
    binary_sha256 = receipt.get("binary_sha256")
    fixture = receipt.get("synthetic_fixture")
    return (
        receipt.get("document_type") == "glaeda-managed-mac-admission-build-receipt"
        and receipt.get("schema_version") == 1
        and isinstance(source_commit, str)
        and re.fullmatch(r"[a-f0-9]{40}", source_commit) is not None
        and isinstance(source_tree, str)
        and re.fullmatch(r"[a-f0-9]{40}", source_tree) is not None
        and receipt.get("checked_out_commit") == source_commit
        and receipt.get("checked_out_tree") == source_tree
        and isinstance(receipt.get("target"), str)
        and isinstance(receipt.get("toolchain"), str)
        and isinstance(binary_sha256, str)
        and re.fullmatch(r"[a-f0-9]{64}", binary_sha256) is not None
        and receipt.get("build_outcome") == "success"
        and receipt.get("synthetic_matrix_outcome") == "success"
        and isinstance(fixture, dict)
        and fixture.get("mode") == "synthetic_fixture"
        and fixture.get("live_deletion_enabled") is False
        and fixture.get("runner_root") is None
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = build_receipt(os.environ, args.artifact)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if valid_receipt(receipt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
