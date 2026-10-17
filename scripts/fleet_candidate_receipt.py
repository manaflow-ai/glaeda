#!/usr/bin/env python3
"""Write bounded workflow evidence for a fleet candidate job."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
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


def _candidate(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def build_receipt(
    environ: Mapping[str, str],
    candidate_path: Path,
    *,
    git_reader: Callable[..., str | None] = git_value,
) -> dict[str, object]:
    event = _env(environ, "EVENT_NAME")
    receipt: dict[str, object] = {
        "document_type": "glaeda-fleet-candidate-workflow-receipt",
        "schema_version": 1,
        "repository": _env(environ, "REPOSITORY"),
        "event": event,
        "source_mode": "pull_request_merge" if event == "pull_request" else "workflow_sha",
        "requested_head": _env(environ, "REQUESTED_HEAD"),
        "workflow_sha": _env(environ, "WORKFLOW_SHA"),
        "run_id": _env(environ, "RUN_ID"),
        "run_attempt": _env(environ, "RUN_ATTEMPT"),
        "target": _env(environ, "TARGET"),
        "checked_out_commit": git_reader("rev-parse", "HEAD^{commit}"),
        "checked_out_tree": git_reader("rev-parse", "HEAD^{tree}"),
        "runner": {
            "name": _env(environ, "RUNNER_NAME"),
            "os": _env(environ, "RUNNER_OS"),
            "arch": _env(environ, "RUNNER_ARCH"),
        },
        "job_status": _env(environ, "JOB_STATUS"),
        "steps": {
            "bundle_test": _env(environ, "BUNDLE_TEST_OUTCOME"),
            "candidate_build": _env(environ, "CANDIDATE_BUILD_OUTCOME"),
            "native_build": _env(environ, "NATIVE_BUILD_OUTCOME"),
            "native_test": _env(environ, "NATIVE_TEST_OUTCOME"),
        },
    }
    manifest = _candidate(candidate_path)
    if manifest is not None:
        source = manifest.get("source")
        if isinstance(source, dict):
            receipt["candidate_source"] = source
            receipt["candidate_source_matches_checkout"] = (
                source.get("commit") == receipt["checked_out_commit"]
            )
        receipt["candidate_archive"] = manifest.get("archive")
        receipt["candidate_sha256"] = manifest.get("sha256")
    return receipt


def exact_source_matches(receipt: Mapping[str, object]) -> bool:
    """Whether the candidate manifest binds to the checkout this job observed."""
    return receipt.get("candidate_source_matches_checkout") is True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-receipt", type=Path, default=Path("target/fleet-bundles/receipt.json"))
    parser.add_argument("--require-exact-source", action="store_true")
    args = parser.parse_args()
    receipt = build_receipt(os.environ, args.candidate_receipt)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    if args.require_exact_source and not exact_source_matches(receipt):
        print("candidate receipt source does not match checked-out commit", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
