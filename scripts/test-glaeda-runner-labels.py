#!/usr/bin/env python3
"""Tests for glaeda_runner_labels: what it refuses and accepts, and that this repository's workflows pass."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import glaeda_runner_labels as rl  # noqa: E402

HEAD = "name: x\non: push\njobs:\n"


def check(body: str, allowed: dict[tuple[str, str], str] | None = None) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "w.yml"
        path.write_text(HEAD + body, encoding="utf-8")
        return rl.violations([path], allowed or {})


class Refuses(unittest.TestCase):
    def test_hosted_labels_in_every_spelling(self) -> None:
        for runs_on in (
            "    runs-on: ubuntu-latest\n",
            "    runs-on: ubuntu-24.04\n",
            "    runs-on: ubuntu-22.04\n",
            "    runs-on: [ubuntu-24.04-arm]\n",
            "    runs-on: ${{ vars.LINUX_RUNNER || 'ubuntu-24.04' }}\n",
            "    runs-on: macos-15\n",
            "    runs-on: macos-latest\n",
            "    runs-on: windows-2025\n",
            "    runs-on: ubuntu-slim\n",
        ):
            with self.subTest(runs_on=runs_on):
                self.assertTrue(check("  a:\n" + runs_on + "    steps: [{run: true}]\n"))

    def test_a_runner_the_text_cannot_show_is_refused(self) -> None:
        for runs_on in (
            "    runs-on: ${{ vars.LINUX_RUNNER }}\n",
            "    runs-on: ${{ vars.LINUX_RUNNER || 'blacksmith-4vcpu-ubuntu-2404' }}\n",
            "    runs-on: ${{ inputs.runner }}\n",
            "    runs-on: ${{ fromJSON(needs.pick.outputs.labels) }}\n",
            "    strategy:\n      matrix: ${{ fromJSON(needs.pick.outputs.matrix) }}\n",
            "    runs-on: ${{ vars['LINUX_RUNNER'] }}\n",
            "    runs-on: ${{ VARS.LINUX_RUNNER }}\n",
            "    runs-on: >-\n      ${{ vars.LINUX_RUNNER\n      || 'blacksmith-4vcpu-ubuntu-2404' }}\n",
            "    runs-on:\n      - ${{ inputs.runner }}\n",
            "    strategy:\n      matrix:\n        os: ${{ fromJSON(inputs.oses) }}\n    runs-on: ${{ matrix.os }}\n",
            "    strategy:\n      matrix:\n        include:\n          - os: ${{ vars.OS }}\n    runs-on: ${{ matrix.os }}\n",
            "    strategy:\n      matrix:\n        name: [ubuntu-latest]\n    runs-on: ${{ matrix.name }}\n",
            "    strategy:\n      matrix:\n        include:\n          - run: ubuntu-24.04\n    runs-on: ${{ matrix.run }}\n",
            "    runs-on: >-\n      ${{ vars\n      ['LINUX_RUNNER'] }}\n",
            "    runs-on: ${{ format('ubuntu-{0}', 'latest') }}\n",
            "    runs-on: ${{ github.event.client_payload.runner }}\n",
            "    runs-on: ${{ matrix.os || 'ubuntu-latest' }}\n",
            "    runs-on: [self-hosted, ubuntu-24.04]\n",
            "    strategy:\n      matrix:\n        version: ['22.04', '24.04']\n    runs-on: ubuntu-${{ matrix.version }}\n",
            "    runs-on: [\"${{ matrix.os }}-arm\"]\n",
        ):
            with self.subTest(runs_on=runs_on):
                self.assertTrue(check("  a:\n" + runs_on + "    steps: [{run: true}]\n"))

    def test_a_picker_that_emits_a_hosted_label_is_refused(self) -> None:
        body = ("  pick:\n    runs-on: blacksmith-4vcpu-ubuntu-2404\n    outputs:\n"
                "      runner: ${{ steps.pick.outputs.runner }}\n    steps:\n      - id: pick\n        run: |\n"
                "          echo runner=ubuntu-latest >> \"$GITHUB_OUTPUT\"\n"
                "  test:\n    needs: pick\n    runs-on: ${{ needs.pick.outputs.runner }}\n")
        problems = check(body)
        self.assertTrue(any("job test" in p for p in problems), problems)

    def test_the_owner_form_must_send_the_fork_to_blacksmith(self) -> None:
        for runs_on in (
            "    runs-on: ${{ github.repository_owner != 'manaflow-ai' && 'blacksmith-4vcpu-ubuntu-2404' || 'ubuntu-24.04' }}\n",
            "    runs-on: ${{ github.repository_owner == 'someone' && 'blacksmith-4vcpu-ubuntu-2404' || 'ubuntu-24.04' }}\n",
            "    runs-on: ${{ github.repository_owner == 'manaflow-ai' && 'ubuntu-latest' || 'ubuntu-24.04' }}\n",
            "    runs-on: ${{ github.event_name == 'push' && github.repository_owner == 'manaflow-ai'"
            " && 'blacksmith-4vcpu-ubuntu-2404' || 'ubuntu-24.04' }}\n",
        ):
            with self.subTest(runs_on=runs_on):
                self.assertTrue(check("  a:\n" + runs_on))

    def test_matrix_row_counts_for_its_job(self) -> None:
        body = ("  a:\n    strategy:\n      matrix:\n        include:\n          - runner: ubuntu-24.04\n"
                "    runs-on: ${{ matrix.runner }}\n    steps: [{run: true}]\n")
        self.assertIn("job a", check(body)[0])

    def test_allowlist_is_per_job_and_needs_a_reason(self) -> None:
        body = "  a:\n    runs-on: ubuntu-24.04\n  b:\n    runs-on: ubuntu-24.04\n"
        problems = check(body, {("w.yml", "a"): "a reason"})
        self.assertEqual(len(problems), 1)
        self.assertIn("job b", problems[0])
        self.assertTrue(check("  a:\n    runs-on: ubuntu-24.04\n", {("w.yml", "a"): " "}))

    def test_stale_allowlist_entry(self) -> None:
        problems = check("  a:\n    runs-on: blacksmith-4vcpu-ubuntu-2404\n", {("w.yml", "a"): "a reason"})
        self.assertEqual(len(problems), 1)
        self.assertIn("names no GitHub-hosted label", problems[0])


class Accepts(unittest.TestCase):
    def test_blacksmith_owned_comments_and_shell(self) -> None:
        body = (
            "  a:\n"
            "    runs-on: blacksmith-4vcpu-ubuntu-2404\n"
            "  b:\n"
            "    runs-on: blacksmith-16vcpu-ubuntu-2404-arm  # was ubuntu-24.04\n"
            "  d:\n"
            "    runs-on: ${{ github.repository_owner == 'manaflow-ai' && 'blacksmith-16vcpu-ubuntu-2404' || 'ubuntu-24.04' }}\n"
            "  e:\n"
            "    strategy:\n      matrix:\n        include:\n"
            "          - runner: \"${{ github.repository_owner == 'manaflow-ai' && 'blacksmith-4vcpu-ubuntu-2404-arm' || 'ubuntu-24.04-arm' }}\"\n"
            "    runs-on: ${{ matrix.runner }}\n"
            "  c:\n"
            "    name: compare with ubuntu-24.04\n"
            "    runs-on: glaeda-owned-linux\n"
            "    steps:\n"
            "      - run: echo ubuntu-latest\n"
            "      - run: |\n"
            "          echo macos-15\n"
            "          echo ubuntu-24.04\n"
            "      # runs-on: ubuntu-latest was the old label\n"
        )
        self.assertEqual(check(body), [])


class Repository(unittest.TestCase):
    def test_workflows_pass(self) -> None:
        self.assertEqual(rl.violations(rl.workflow_files([])), [])

    def test_every_allowlist_entry_has_a_reason(self) -> None:
        for key, reason in rl.HOSTED_ALLOWED.items():
            self.assertTrue(reason.strip(), key)

    def test_jobs_in_the_fleet_fork_run_on_blacksmith(self) -> None:
        # manaflow-ai/glaeda is an organization repository with Blacksmith installed, so a job that runs only
        # there never takes a hosted image, and every allowlisted job is one the fork never runs or a macOS row.
        for path in rl.workflow_files([]):
            text = path.read_text(encoding="utf-8")
            if "github.repository == 'manaflow-ai/glaeda'" in text:
                self.assertEqual(rl.hosted_jobs(path), {}, path.name)

    def test_an_allowlisted_job_never_runs_in_the_fleet_fork(self) -> None:
        # The fork has Blacksmith, so an entry is either gated to this repository or measures the hosted image.
        for (name, job), reason in rl.HOSTED_ALLOWED.items():
            with self.subTest(job=f"{name}:{job}"):
                text = (rl.workflow_files([])[0].parent / name).read_text(encoding="utf-8")
                gated = "github.repository == 'teamleaderleo/glaeda'" in text
                self.assertTrue(gated or reason in (rl.HOSTED_ON_PURPOSE, rl.HOSTED_PROBE), reason)


if __name__ == "__main__":
    unittest.main()
