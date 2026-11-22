#!/usr/bin/env python3
"""Deterministic contract tests for scripts/verify."""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / "scripts" / "verify"


def read_plan(profile: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(VERIFY), profile, "--plan-json"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


class VerifyPlanTests(unittest.TestCase):
    def test_focused_plan_is_fixed_and_credentialless_compatible(self) -> None:
        plan = read_plan("focused")
        self.assertEqual(plan["profile"], "focused")
        self.assertEqual(plan["authority"], "developer_feedback_only")
        self.assertEqual(
            [phase["name"] for phase in plan["phases"]],
            [
                "fleet-rule-lints",
                "gh-auth-token-lint",
                "compile-all-targets",
                "format",
                "repo-query-integration",
            ],
        )
        self.assertEqual(
            plan["phases"][0]["argv"],
            ["python3", "-I", "scripts/test-fleet-rule-lints.py"],
        )
        self.assertEqual(
            plan["phases"][1]["argv"],
            ["python3", "-I", "scripts/gh_auth_token_lint.py"],
        )
        self.assertEqual(
            plan["phases"][2]["argv"],
            ["cargo", "check", "--locked", "--all-targets", "--all-features"],
        )
        self.assertEqual(
            plan["phases"][4]["argv"],
            ["cargo", "test", "--locked", "--test", "repo_query_cli"],
        )

    def test_the_script_lints_run_before_any_cargo_phase_of_the_focused_plan(self) -> None:
        """`run` breaks at the first failing phase, so the sub-second lints must not sit behind a
        cargo check. Both lints also exist in CI's `scripts` job; this profile is where a change
        under `scripts/` learns their verdict without a CI cycle."""
        names = [phase["name"] for phase in read_plan("focused")["phases"]]
        argvs = {phase["name"]: phase["argv"] for phase in read_plan("focused")["phases"]}
        lints = ["fleet-rule-lints", "gh-auth-token-lint"]
        first_cargo = min(i for i, n in enumerate(names) if argvs[n][0] == "cargo")
        for lint in lints:
            self.assertLess(names.index(lint), first_cargo, names)
        for lint in lints:
            # -I so a planted module beside the script cannot be imported by it.
            self.assertEqual(argvs[lint][:2], ["python3", "-I"], argvs[lint])

    def test_required_profile_is_the_exact_eight_step_agents_sequence(self) -> None:
        plan = read_plan("required")
        self.assertEqual(plan["authority"], "repository_required_checks")
        self.assertEqual(
            [phase["argv"] for phase in plan["phases"]],
            [
                ["scripts/bootstrap", "--output", "json"],
                ["cargo", "fmt", "--all", "--", "--check"],
                [
                    "cargo",
                    "clippy",
                    "--locked",
                    "--all-targets",
                    "--all-features",
                    "--",
                    "-D",
                    "warnings",
                ],
                ["cargo", "test", "--locked", "--all-targets", "--all-features"],
                [
                    "cargo",
                    "run",
                    "--locked",
                    "--quiet",
                    "--",
                    "--output",
                    "json",
                    "doctor",
                ],
                [
                    "cargo",
                    "run",
                    "--locked",
                    "--quiet",
                    "--",
                    "plan",
                    "--file",
                    "examples/quarry.yml",
                ],
                [
                    "cargo",
                    "run",
                    "--locked",
                    "--quiet",
                    "--",
                    "--output",
                    "json",
                    "plan",
                    "--file",
                    "examples/glossless.yml",
                ],
                [
                    "cargo",
                    "run",
                    "--locked",
                    "--quiet",
                    "--",
                    "--output",
                    "json",
                    "host",
                    "plan",
                    "--file",
                    "examples/quarry.yml",
                ],
            ],
        )

    def test_fast_profile_is_explicitly_non_authoritative_and_uses_one_cli_build(self) -> None:
        plan = read_plan("fast")
        self.assertEqual(plan["authority"], "developer_feedback_only")
        phases = plan["phases"]
        self.assertEqual(
            [phase["name"] for phase in phases],
            [
                "unit-and-binary-tests",
                "format",
                "build-cli",
                "doctor",
                "plan-quarry",
                "plan-glossless",
                "host-plan-quarry",
            ],
        )
        self.assertEqual(
            phases[0]["argv"], ["cargo", "test", "--locked", "--lib", "--bins"]
        )
        self.assertEqual(phases[1]["argv"], ["scripts/verify-changed-rustfmt"])
        self.assertEqual(
            sum(phase["argv"][:2] == ["cargo", "build"] for phase in phases), 1
        )
        self.assertTrue(all("sh" not in phase["argv"][:1] for phase in phases))

    def test_full_tests_profile_is_explicit_and_does_not_change_required_checks(self) -> None:
        plan = read_plan("full-tests")
        self.assertEqual(plan["authority"], "developer_feedback_only")
        self.assertEqual(
            plan["phases"],
            [
                {
                    "name": "full-tests",
                    "argv": [
                        "cargo-nextest",
                        "--color",
                        "never",
                        "--no-pager",
                        "--user-config-file",
                        "none",
                        "nextest",
                        "run",
                        "--profile",
                        "default",
                        "--locked",
                        "--all-targets",
                        "--all-features",
                        "--ignore-default-filter",
                        "--test-threads",
                        "num-cpus",
                        "--retries",
                        "0",
                        "--no-fail-fast",
                        "--failure-output",
                        "immediate-final",
                        "--success-output",
                        "never",
                        "--status-level",
                        "fail",
                        "--final-status-level",
                        "fail",
                    ],
                }
            ],
        )

    def test_plans_are_path_free_and_retain_no_logs(self) -> None:
        for profile in ("focused", "fast", "full-tests", "required"):
            plan = read_plan(profile)
            encoded = json.dumps(plan, sort_keys=True)
            self.assertNotIn(str(ROOT), encoded)
            self.assertFalse(plan["retained_logs"])
            self.assertTrue(plan["source_must_remain_unchanged"])
            self.assertEqual(
                plan["execution_environment"]["file_creation_umask"], "0022"
            )

    def test_source_state_detects_content_changes_with_the_same_git_status(self) -> None:
        source_state = runpy.run_path(str(VERIFY), run_name="glaeda_verify_test")[
            "source_state"
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
            tracked = root / "tracked.txt"
            tracked.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Glaeda test",
                    "-c",
                    "user.email=glaeda-test@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ],
                cwd=root,
                check=True,
            )

            tracked.write_text("first dirty value\n", encoding="utf-8")
            first_tracked = source_state(root)
            tracked.write_text("second dirty value\n", encoding="utf-8")
            self.assertNotEqual(first_tracked, source_state(root))

            untracked = root / "untracked.txt"
            untracked.write_text("first untracked value\n", encoding="utf-8")
            first_untracked = source_state(root)
            untracked.write_text("second untracked value\n", encoding="utf-8")
            self.assertNotEqual(first_untracked, source_state(root))

    def test_receipt_records_exact_plan_source_phase_and_resource_observations(self) -> None:
        module = runpy.run_path(str(VERIFY), run_name="glaeda_verify_test")
        child_resources = module["ChildResources"]
        phase_observation = module["PhaseObservation"]
        receipt = module["receipt_document"](
            profile="fast",
            source_commit="1" * 40,
            source_tree="2" * 40,
            source_before=b"\xaa" * 32,
            source_after=b"\xaa" * 32,
            environment={
                "logical_cpu_count": 16,
                "cargo_build_jobs": {"source": "configured", "value": 4},
                "cargo_target": {
                    "source": "configured",
                    "identity_digest": f"sha256:{'3' * 64}",
                },
                "cargo_home": {
                    "source": "user_default",
                    "identity_digest": f"sha256:{'4' * 64}",
                },
            },
            observations=[
                phase_observation(
                    name="format",
                    argv=["cargo", "fmt", "--all", "--", "--check"],
                    exit_code=0,
                    elapsed_seconds=1.2345678,
                    child_resources=child_resources(2.5, 0.75, 123_456),
                )
            ],
            exit_code=0,
            elapsed_seconds=2.3456789,
            started_at_unix_millis=1_000,
            completed_at_unix_millis=3_346,
            resources=child_resources(3.0, 1.0, 234_567),
        )

        self.assertEqual(receipt["document_type"], "glaeda-local-verification-receipt")
        self.assertEqual(receipt["authority"], "performance_observation_only")
        self.assertTrue(receipt["source"]["unchanged"])
        self.assertEqual(receipt["source"]["commit"], "1" * 40)
        self.assertEqual(receipt["environment"]["cargo_build_jobs"]["value"], 4)
        self.assertEqual(receipt["plan"]["executed_phase_count"], 1)
        self.assertEqual(receipt["phases"][0]["elapsed_seconds"], 1.234568)
        self.assertEqual(
            receipt["phases"][0]["process_lifetime_child_max_rss_kib"],
            123_456,
        )
        self.assertEqual(receipt["result"]["elapsed_seconds"], 2.345679)
        self.assertEqual(receipt["result"]["exit_code"], 0)
        self.assertEqual(
            receipt["result"]["max_rss_semantics"],
            "maximum_waited_child_not_concurrent_sum",
        )
        self.assertNotIn(str(ROOT), json.dumps(receipt, sort_keys=True))

    def test_environment_records_cache_identity_without_exposing_paths(self) -> None:
        execution_environment = runpy.run_path(
            str(VERIFY), run_name="glaeda_verify_test"
        )["execution_environment"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = os.environ.copy()
            os.environ["CARGO_TARGET_DIR"] = str(root / "private-target")
            os.environ["CARGO_HOME"] = str(root / "private-cargo-home")
            os.environ["CARGO_BUILD_JOBS"] = "4"
            try:
                observation = execution_environment(ROOT)
            finally:
                os.environ.clear()
                os.environ.update(environment)

            encoded = json.dumps(observation, sort_keys=True)
            self.assertEqual(observation["cargo_build_jobs"]["value"], 4)
            self.assertEqual(observation["file_creation_umask"], "0022")
            self.assertEqual(observation["cargo_target"]["source"], "configured")
            self.assertNotIn(directory, encoded)
            self.assertRegex(
                observation["cargo_target"]["identity_digest"],
                r"^sha256:[0-9a-f]{64}$",
            )

    def test_receipt_writer_atomically_replaces_one_file_without_residue(self) -> None:
        write_receipt = runpy.run_path(str(VERIFY), run_name="glaeda_verify_test")[
            "write_receipt"
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            output.write_text("old\n", encoding="utf-8")
            write_receipt(output, {"schema_version": 1, "result": "passed"})

            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {"schema_version": 1, "result": "passed"},
            )
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_receipt_destination_refuses_the_source_worktree(self) -> None:
        receipt_destination = runpy.run_path(
            str(VERIFY), run_name="glaeda_verify_test"
        )["receipt_destination"]
        with self.assertRaisesRegex(RuntimeError, "outside the source worktree"):
            receipt_destination(ROOT, ROOT / "verification-receipt.json")
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "verification-receipt.json"
            canonical = expected.parent.resolve(strict=True) / expected.name
            self.assertEqual(receipt_destination(ROOT, expected), canonical)

    def test_summary_mode_counts_and_hashes_combined_phase_output(self) -> None:
        execute_phase = runpy.run_path(str(VERIFY), run_name="glaeda_verify_test")[
            "execute_phase"
        ]
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            program = fixture / "producer"
            program.write_text(
                "#!/usr/bin/python3\n"
                "import os\n"
                "os.write(1, b'alpha\\n')\n"
                "os.write(2, b'beta')\n",
                encoding="utf-8",
            )
            program.chmod(0o755)

            returncode, summary, raw_output = execute_phase(
                program, (), fixture, "summary"
            )
            expected = b"alpha\nbeta"
            self.assertEqual(returncode, 0)
            self.assertIsNotNone(summary)
            self.assertEqual(summary.byte_count, len(expected))
            self.assertEqual(summary.line_count, 2)
            self.assertEqual(
                summary.digest,
                f"sha256:{hashlib.sha256(expected).hexdigest()}",
            )
            self.assertEqual(summary.tail, expected)
            self.assertIsNone(raw_output)

    def test_phase_child_uses_profile_umask_instead_of_ambient_umask(self) -> None:
        execute_phase = runpy.run_path(str(VERIFY), run_name="glaeda_verify_test")[
            "execute_phase"
        ]
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            program = fixture / "producer"
            program.write_text(
                "#!/usr/bin/python3\n"
                "from pathlib import Path\n"
                "Path('created').mkdir()\n",
                encoding="utf-8",
            )
            program.chmod(0o755)
            previous = os.umask(0o002)
            try:
                for mode in ("summary", "stream"):
                    phase_root = fixture / mode
                    phase_root.mkdir()
                    returncode, summary, raw_output = execute_phase(
                        program, (), phase_root, mode
                    )
                    self.assertEqual(returncode, 0)
                    if mode == "summary":
                        self.assertIsNotNone(summary)
                    else:
                        self.assertIsNone(summary)
                    self.assertIsNone(raw_output)
                    self.assertEqual(
                        (phase_root / "created").stat().st_mode & 0o777, 0o755
                    )
                ambient_after = os.umask(0o002)
            finally:
                os.umask(previous)

            self.assertEqual(ambient_after, 0o002)

    def test_failed_phase_still_writes_a_terminal_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            fake_cargo = fixture / "cargo"
            fake_cargo.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            fake_cargo.chmod(0o755)
            receipt_path = fixture / "failed.json"
            environment = os.environ.copy()
            environment["PATH"] = f"{fixture}:/usr/bin:/bin"

            result = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY),
                    "fast",
                    "--receipt",
                    str(receipt_path),
                ],
                cwd=ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

            self.assertEqual(result.returncode, 7)
            self.assertEqual(receipt["result"]["exit_code"], 7)
            self.assertEqual(receipt["plan"]["executed_phase_count"], 1)
            self.assertEqual(receipt["phases"][0]["name"], "unit-and-binary-tests")
            self.assertEqual(receipt["phases"][0]["exit_code"], 7)
            self.assertTrue(receipt["source"]["unchanged"])

    def test_summary_mode_preserves_both_edges_and_raw_large_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            fake_cargo = fixture / "cargo"
            fake_cargo.write_text(
                "#!/usr/bin/python3\n"
                "import sys\n"
                "sys.stdout.write('EARLY_DIAGNOSTIC\\n' + 'x' * 20000 + "
                "'\\nFINAL_DIAGNOSTIC\\n')\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            fake_cargo.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fixture}:/usr/bin:/bin"

            result = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY),
                    "fast",
                ],
                cwd=ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 7)
            self.assertIn("EARLY_DIAGNOSTIC", result.stderr)
            self.assertIn("FINAL_DIAGNOSTIC", result.stderr)
            self.assertIn("head_bytes=8192 tail_bytes=8192", result.stderr)
            self.assertRegex(result.stderr, r"omitted_bytes=[1-9][0-9]*")
            self.assertRegex(result.stderr, r"output_bytes=2003[0-9]")
            self.assertRegex(result.stderr, r"output_lines=3")
            self.assertRegex(result.stderr, r"output_digest=sha256:[0-9a-f]{64}")
            match = re.search(r"raw_output=(.+)", result.stderr)
            self.assertIsNotNone(match)
            raw_output = Path(match.group(1).strip())
            try:
                self.assertEqual(raw_output.stat().st_mode & 0o777, 0o600)
                raw = raw_output.read_text(encoding="utf-8")
                self.assertIn("EARLY_DIAGNOSTIC", raw)
                self.assertIn("FINAL_DIAGNOSTIC", raw)
                self.assertEqual(len(raw), 20035)
            finally:
                raw_output.unlink(missing_ok=True)

    def test_summary_mode_prints_a_small_failure_in_full(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            fake_cargo = fixture / "cargo"
            fake_cargo.write_text(
                "#!/bin/sh\nprintf 'FIRST_DIAGNOSTIC\\nSECOND_DIAGNOSTIC\\n'\nexit 7\n",
                encoding="utf-8",
            )
            fake_cargo.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fixture}:/usr/bin:/bin"

            result = subprocess.run(
                [sys.executable, str(VERIFY), "fast"],
                cwd=ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 7)
            self.assertIn("head_bytes=35 tail_bytes=0 omitted_bytes=0", result.stderr)
            self.assertIn("FIRST_DIAGNOSTIC\nSECOND_DIAGNOSTIC\n", result.stderr)
            match = re.search(r"raw_output=(.+)", result.stderr)
            self.assertIsNotNone(match)
            raw_output = Path(match.group(1).strip())
            try:
                self.assertEqual(raw_output.stat().st_mode & 0o777, 0o600)
                self.assertEqual(
                    raw_output.read_text(encoding="utf-8"),
                    "FIRST_DIAGNOSTIC\nSECOND_DIAGNOSTIC\n",
                )
            finally:
                raw_output.unlink(missing_ok=True)

    def test_plan_only_mode_rejects_a_receipt_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt_path = Path(directory) / "receipt.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY),
                    "fast",
                    "--plan-json",
                    "--receipt",
                    str(receipt_path),
                ],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("cannot be combined", result.stderr)
            self.assertFalse(receipt_path.exists())


class HostPolicyRefusalTests(unittest.TestCase):
    """A phase the host refused to run is not a verdict on the phases after it.

    2026-11-22, measured on a shared workstation: `./scripts/verify focused` exited 77 at phase 3 of
    5 because `tools/heavy-guard` refuses cargo there, and the two phases after it -- format and the
    repo-query integration test, the ones a change under `scripts/` needs -- printed no verdict at
    all. The refusal is now reported, the rest of the plan still runs, and the run is still not green.
    """

    def phase(self, module, name: str, script: str):
        return module["Phase"](name, "capability", "sh", ("-c", script))

    def run_phases(self, module, phases) -> tuple[int, str]:
        run = module["run"]
        run.__globals__["phases_for"] = lambda profile: tuple(phases)
        with tempfile.TemporaryDirectory() as directory:
            captured = Path(directory) / "stderr.log"
            with captured.open("w+b") as sink:
                sys.stderr.flush()
                saved = os.dup(2)
                try:
                    os.dup2(sink.fileno(), 2)
                    code = run("fast")
                finally:
                    sys.stderr.flush()
                    os.dup2(saved, 2)
                    os.close(saved)
            return code, captured.read_text(encoding="utf-8", errors="replace")

    def test_a_refused_phase_reports_itself_and_the_rest_of_the_plan_still_runs(self) -> None:
        module = runpy.run_path(str(VERIFY), run_name="glaeda_verify_test")
        refusal = "heavy-guard: refused: [routing-order] 'cargo check' on this host"
        code, said = self.run_phases(
            module,
            [
                self.phase(module, "compile-all-targets", f"echo \"{refusal}\" >&2; exit 77"),
                self.phase(module, "format", "exit 0"),
                self.phase(module, "repo-query-integration", "exit 0"),
            ],
        )

        self.assertEqual(code, 77, said)
        self.assertIn("compile-all-targets: refused by host policy", said)
        self.assertIn("CI owns its verdict", said)
        self.assertIn("[2/3] format: exit=0", said)
        self.assertIn("[3/3] repo-query-integration: exit=0", said)
        self.assertIn("host policy refused 1 phase(s): compile-all-targets", said)

    def test_a_refusal_never_reports_green(self) -> None:
        module = runpy.run_path(str(VERIFY), run_name="glaeda_verify_test")
        code, said = self.run_phases(
            module,
            [self.phase(module, "compile-all-targets", "echo refused: policy >&2; exit 77")],
        )

        self.assertNotEqual(code, 0, said)
        self.assertIn("exit=77", said)

    def test_a_real_failure_after_a_refusal_still_decides_the_exit_code(self) -> None:
        module = runpy.run_path(str(VERIFY), run_name="glaeda_verify_test")
        code, said = self.run_phases(
            module,
            [
                self.phase(module, "compile-all-targets", "echo refused: policy >&2; exit 77"),
                self.phase(module, "repo-query-integration", "echo boom >&2; exit 3"),
            ],
        )

        self.assertEqual(code, 3, said)
        self.assertIn("host policy refused 1 phase(s)", said)

    def test_our_own_tool_exiting_77_is_a_failure_and_still_breaks_the_run(self) -> None:
        """77 alone is not a refusal: the captured text has to say so, or a coincidence passes."""
        module = runpy.run_path(str(VERIFY), run_name="glaeda_verify_test")
        code, said = self.run_phases(
            module,
            [
                self.phase(module, "repo-query-integration", "echo 77 cases failed >&2; exit 77"),
                self.phase(module, "format", "exit 0"),
            ],
        )

        self.assertEqual(code, 77, said)
        self.assertNotIn("refused by host policy", said)
        self.assertNotIn("[2/2] format", said)


if __name__ == "__main__":
    unittest.main()
