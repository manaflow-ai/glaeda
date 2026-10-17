#!/usr/bin/env python3
"""Compare the Python runner-log baseline with an admitted native adapter.

The fixture is deliberately end to end: stale, recent, held-open, busy, and warm
entries are created in a private temporary home. Native timings are *never*
comparable unless the adapter reports a real apply with no errors and the fixture
shows the expected deletion and survivors. In particular, the phase-one observer's
``reclaim_not_ready`` result is a blocked measurement, not a speed result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def time_metrics(stderr: str) -> dict:
    # Darwin /usr/bin/time -l prints "0.01 real 0.00 user 0.00 sys"
    # and "123456 maximum resident set size" (bytes).
    cpu = re.search(r"([\d.]+)\s+real\s+([\d.]+)\s+user\s+([\d.]+)\s+sys", stderr)
    rss = re.search(r"(\d+)\s+maximum resident set size", stderr)
    return {
        "time_real_seconds": float(cpu[1]) if cpu else None,
        "user_seconds": float(cpu[2]) if cpu else None,
        "system_seconds": float(cpu[3]) if cpu else None,
        "max_rss_bytes": int(rss[1]) if rss else None,
    }


def redact_output(value: str) -> str:
    """Keep bounded diagnostics without publishing fixture paths."""
    return re.sub(r"/(?:private/)?(?:var/folders|tmp|Users|home)/[^\s)]+", "<private-path>", value)[-8192:]


def python_receipt_summary(home: Path) -> dict[str, Any]:
    path = home / "Library/Logs/cruft-sweep.jsonl"
    rows: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("removed") is True:
                rows.append(row)
    return {
        "action_count": len(rows),
        "bytes": sum(int(row.get("bytes", 0)) for row in rows if isinstance(row.get("bytes"), int)),
        # Python's receipt has no native identity. Batch IDs are retained as
        # diagnostic receipt identities and are not treated as native parity.
        "receipt_ids": sorted(str(row["batch_id"]) for row in rows if isinstance(row.get("batch_id"), str)),
        "identity_kind": "python_batch_id",
    }


def native_receipt_summary(report: dict[str, Any]) -> dict[str, Any]:
    actions = report.get("actions", [])
    if not isinstance(actions, list):
        actions = []
    valid_actions = [row for row in actions if isinstance(row, dict)]
    return {
        "action_count": len(valid_actions),
        "bytes": sum(int(row.get("identity", {}).get("size", 0))
                      for row in valid_actions if isinstance(row.get("identity"), dict)),
        "receipt_ids": sorted(str(row["receipt_id"]) for row in valid_actions
                               if isinstance(row.get("receipt_id"), str)),
        "identity_kind": "native_device_inode",
        "reclaimed_count": report.get("reclaimed_count"),
        "errors": report.get("errors", []),
    }


def classify_status(measurements: list[dict[str, Any]], *, source_matches_checkout: bool | None,
                    native_available: bool) -> tuple[str, list[str]]:
    """Return a conservative status and machine-readable blocking reasons."""
    reasons: list[str] = []
    if not native_available:
        reasons.append("native_adapter_binary_unavailable")
    native_rows = [row for row in measurements if row.get("implementation") == "native"]
    if native_available and not native_rows:
        reasons.append("native_measurement_missing")
    if source_matches_checkout is False:
        reasons.append("native_source_receipt_mismatch")
    if any("reclaim_not_ready" in row.get("native_errors", []) for row in native_rows):
        reasons.append("native_apply_not_admitted")
    if any(row.get("implementation") == "native" and not row.get("effect_valid") for row in native_rows):
        reasons.append("native_effect_not_verified")
    if reasons:
        return ("blocked_native_effect_comparison", sorted(set(reasons)))
    if not measurements or not all(row.get("valid") for row in measurements):
        return ("invalid_comparison", ["fixture_effects_or_metrics_invalid"])
    return ("matched_fixture_effects", [])


class Fixture:
    def __init__(self, base: Path, count: int):
        self.home = base / "home"
        self.root = self.home / "actions-runner-idle"
        self.busy_root = self.home / "actions-runner-busy"
        self.receipts = base / "receipts"
        self.receipts.mkdir()
        self.stale: list[Path] = []
        self.preserved: list[Path] = []
        old = time.time() - 8 * 86400
        for root in (self.root, self.busy_root):
            (root / "_diag").mkdir(parents=True)
            (root / ".runner").write_text("{}")
            (root / ".glaeda-cmux-runner").write_text(root.name + "\n")
            (self.receipts / (root.name + ".json")).write_text(json.dumps({
                "runnerDirOwned": True, "runnerDir": str(root), "installId": root.name,
            }))
        for index in range(count):
            path = self.root / "_diag" / f"stale-{index:04}.log"
            path.write_bytes(b"fixture\n")
            os.utime(path, (old, old))
            self.stale.append(path)
        for filename, root, expired in (("recent.log", self.root, False),
                                         ("open.log", self.root, True),
                                         ("busy.log", self.busy_root, True)):
            path = root / "_diag" / filename
            path.write_bytes(b"preserve\n")
            if expired:
                os.utime(path, (old, old))
            self.preserved.append(path)
        warm = self.home / "Library/Developer/Xcode/DerivedData/cmux-cloud-cache"
        warm.mkdir(parents=True)
        marker = warm / "warm.marker"
        marker.write_bytes(b"warm\n")
        os.utime(marker, (old, old))
        os.utime(warm, (old, old))
        self.preserved.append(marker)
        (self.home / "Library/Logs").mkdir(parents=True)
        self.held = self.preserved[1].open("rb")
        (self.busy_root / "bin").mkdir()
        worker = self.busy_root / "bin/Runner.Worker"
        # Copy executable bytes/mode, not SIP-protected system file flags.
        shutil.copyfile("/bin/sleep", worker)
        worker.chmod(0o700)
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", str(worker)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True, timeout=15,
        )
        self.worker = subprocess.Popen(
            [str(worker), "300"], cwd=self.busy_root, env={},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def close(self):
        self.held.close()
        self.worker.terminate()
        self.worker.wait(timeout=10)

    def outcomes(self) -> dict:
        names = ("recent", "open", "busy", "warm")
        return {
            "stale_count": len(self.stale),
            "stale_deleted": sum(not path.exists() for path in self.stale),
            "survived": {name: path.exists() for name, path in zip(names, self.preserved)},
        }


def timed(argv: list[str], env: dict) -> dict:
    started = time.monotonic()
    result = subprocess.run(
        ["/usr/bin/time", "-l", *argv], env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120,
    )
    return {"exit": result.returncode, "wall_seconds": time.monotonic() - started,
            **time_metrics(result.stderr), "stdout": redact_output(result.stdout),
            "stderr": redact_output(result.stderr)}


def measure(args, implementation: str, trial: int) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"glaeda-bench-{implementation}-") as temporary:
        # /var is a symlink on macOS; the provider intentionally requires canonical paths.
        base = Path(temporary).resolve()
        fixture = Fixture(base, args.stale_count)
        try:
            if fixture.worker.poll() is not None:
                raise RuntimeError("real Runner.Worker fixture exited before measurement")
            runs = []
            if implementation == "native":
                for root in (fixture.root, fixture.busy_root):
                    run = timed([
                        str(args.native_binary), "--runner-root", str(root),
                        "--enrollment-receipt", str(fixture.receipts / (root.name + ".json")),
                        "--minimum-idle-seconds", str(7 * 86400), "--batch-size", "64",
                        "--state-root", str(base / (root.name + "-state")),
                        "--output", "json", "--apply",
                    ], {})
                    try:
                        run["report"] = json.loads(run.pop("stdout"))
                    except ValueError:
                        run["report"] = {"parse_error": True}
                    runs.append(run)
            else:
                # Redirect *all* configurable global candidate roots; no fleet or shared paths.
                runs.append(timed([
                    str(args.python_executable), str(args.python_source), "--apply", "--batch",
                ], {"HOME": str(fixture.home), "PATH": "/usr/bin:/bin",
                    "CRUFT_SWEEP_CI_TMP": str(base / "ci-tmp"),
                    "CRUFT_SWEEP_CAPTURE_ROOT": str(base / "capture"),
                    "CRUFT_SWEEP_APPLY_BUDGET_S": "100"}))
            outcomes = fixture.outcomes()
            worker_alive = fixture.worker.poll() is None
            metrics_present = all(all(run[key] is not None for key in
                                  ("user_seconds", "system_seconds", "max_rss_bytes")) for run in runs)
            effects_match = worker_alive and outcomes["stale_deleted"] == args.stale_count and all(outcomes["survived"].values())
            # A busy-root refusal may return 1, but must contain a typed busy reason and no effects.
            exits_valid = runs[0]["exit"] == 0
            native_errors: list[str] = []
            effect_valid = implementation == "python" and effects_match and exits_valid
            if implementation == "native":
                busy_report = runs[1].get("report", {})
                busy_typed = "runner_process_busy" in json.dumps(busy_report)
                exits_valid = exits_valid and runs[1]["exit"] in (0, 1) and busy_typed
                exits_valid = exits_valid and all(not run["report"].get("parse_error") for run in runs)
                for run in runs:
                    report = run.get("report", {})
                    native_errors.extend(str(error) for error in report.get("errors", [])
                                         if isinstance(error, str))
                # Apply must have been admitted and must have produced the
                # expected effect. A plan or reclaim_not_ready is not a result.
                idle_report = runs[0].get("report", {})
                effect_valid = (effects_match and exits_valid and not native_errors
                                and idle_report.get("operation") == "apply"
                                and idle_report.get("reclaimed_count") == args.stale_count)
            if implementation == "python":
                receipts = python_receipt_summary(fixture.home)
            else:
                receipts = native_receipt_summary(runs[0].get("report", {}))
            return {"implementation": implementation, "trial": trial, "outcomes": outcomes,
                    "worker_alive_after_measurement": worker_alive,
                    "wall_seconds": sum(run["wall_seconds"] for run in runs),
                    "user_seconds": sum(run["user_seconds"] or 0 for run in runs),
                    "system_seconds": sum(run["system_seconds"] or 0 for run in runs),
                    "peak_child_rss_bytes": max(run["max_rss_bytes"] or 0 for run in runs),
                    "metrics_present": metrics_present, "effect_valid": effect_valid,
                    "native_errors": sorted(set(native_errors)), "receipts": receipts,
                    # This row is eligible for comparison only after effect
                    # validation; callers must not infer speed from a blocked row.
                    "valid": metrics_present and effect_valid, "runs": runs}
        finally:
            fixture.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-binary", type=Path,
                        help="verified hosted adapter; omit to emit a blocked baseline-only receipt")
    parser.add_argument("--native-sha256")
    parser.add_argument("--native-receipt", type=Path,
                        help="hosted build receipt for --native-binary")
    parser.add_argument("--source-commit",
                        help="full source commit recorded by the hosted build receipt")
    parser.add_argument("--checkout-commit",
                        help="current benchmark checkout; mismatch blocks native comparison")
    parser.add_argument("--python-source", type=Path, required=True)
    parser.add_argument("--python-source-sha256", required=True)
    parser.add_argument("--python-executable", type=Path, default=Path(sys.executable))
    parser.add_argument("--stale-counts", default="1,16,64",
                        help="comma-separated synthetic matrix sizes, each 1..64")
    parser.add_argument("--trials", type=int, choices=range(1, 6), default=1, metavar="1..5")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.uname().sysname != "Darwin":
        parser.error("requires Darwin and the real native process provider")
    for path in (args.python_source, args.python_executable):
        if not path.is_absolute() or not path.is_file():
            parser.error("executables and source must be existing absolute paths")
    try:
        stale_counts = [int(value) for value in args.stale_counts.split(",")]
    except ValueError:
        parser.error("--stale-counts must be comma-separated integers")
    if not stale_counts or any(value not in range(1, 65) for value in stale_counts):
        parser.error("--stale-counts values must be in 1..64")
    native_available = args.native_binary is not None
    source_matches_checkout: bool | None = None
    native_receipt: dict[str, Any] | None = None
    if native_available:
        if not args.native_sha256 or not args.source_commit:
            parser.error("--native-binary requires --native-sha256 and --source-commit")
        if not args.native_binary.is_absolute() or not args.native_binary.is_file() or not os.access(args.native_binary, os.X_OK):
            parser.error("native binary must be an existing executable absolute path")
        if digest(args.native_binary) != args.native_sha256:
            parser.error("native binary hash mismatch")
        if not re.fullmatch(r"[a-f0-9]{40}", args.source_commit):
            parser.error("source commit must be the full verified build receipt SHA")
        if args.native_receipt:
            try:
                native_receipt = json.loads(args.native_receipt.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                parser.error(f"native receipt invalid: {error}")
            if (native_receipt.get("binary_sha256") != args.native_sha256
                    or native_receipt.get("checked_out_commit") != args.source_commit
                    or native_receipt.get("test_outcome") != "success"):
                parser.error("native receipt does not bind source, binary, and successful tests")
        if args.checkout_commit:
            source_matches_checkout = args.source_commit == args.checkout_commit
    if digest(args.python_source) != args.python_source_sha256:
        parser.error("optimized Python source hash mismatch")
    measurements = []
    for trial in range(args.trials):
        # Alternate ordering to reduce cache/order bias; fixtures remain freshly created.
        order = ("native", "python") if trial % 2 == 0 else ("python", "native")
        for stale_count in stale_counts:
            args.stale_count = stale_count
            for implementation in order:
                if implementation == "native" and not native_available:
                    continue
                measurements.append(measure(args, implementation, trial))
    status, blocking_reasons = classify_status(measurements,
                                               source_matches_checkout=source_matches_checkout,
                                               native_available=native_available)
    output = {"schema": "glaeda-native-runner-log-benchmark/v3",
              "native_sha256": args.native_sha256,
              "native_receipt": native_receipt,
              "source_commit": args.source_commit,
              "checkout_commit": args.checkout_commit,
              "python_source_sha256": args.python_source_sha256,
              "comparison_status": status,
              "blocking_reasons": blocking_reasons,
              "scope": "isolated end-to-end CLI comparison; native handles two roots in two invocations; Python surveys one fixture home",
              "comparability": {
                  "speedup_claim_allowed": status == "matched_fixture_effects",
                  "native_effect_required": True,
                  "receipt_identity_parity": False,
                  "limits": [
                      "Python batch_id and native device/inode receipt identities are different contracts",
                      "native timing is diagnostic only until apply has no errors and expected effects are observed",
                      "fresh temporary fixtures and one controller host do not establish fleet throughput",
                  ],
              },
              "measurements": measurements}
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"comparison_status": output["comparison_status"], "artifact": str(args.output),
                      "blocking_reasons": blocking_reasons, "measurements": len(measurements)}, sort_keys=True))
    return 0 if status == "matched_fixture_effects" else 2 if status.startswith("blocked_") else 1


if __name__ == "__main__":
    raise SystemExit(main())
