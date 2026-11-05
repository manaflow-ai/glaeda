#!/usr/bin/env python3
"""Contract tests for scripts/glaeda-nightly-only-admit (the nightly-only dedicated runners' admission) and the
DEDICATED_ONLY member declaration it ships with (glaeda_fleet_labels, glaeda-cmux-runner, glaeda-cmux-runner-fleet).

Runs on Linux CI: the admission is pure given the job's environment and event payload, and the CLI is run as a
subprocess with a fake event file, the way dedicated-admit.sh runs it.
"""

from __future__ import annotations

import importlib.machinery
import io
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ADMIT = ROOT / "scripts" / "glaeda-nightly-only-admit"
HOOK = ROOT / "scripts" / "glaeda-cmux-runner-hook"
REPO = "manaflow-ai/cmux"


def load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, os.fspath(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


admit = load("glaeda_nightly_only_admit", ADMIT)
hook = admit.load_hook(HOOK)
labels = load("glaeda_fleet_labels", ROOT / "scripts" / "glaeda_fleet_labels.py")
fleet = load("glaeda_cmux_runner_fleet_nightly_only", ROOT / "scripts" / "glaeda-cmux-runner-fleet")


def job(job_name: str, workflow: str = "nightly.yml", ref: str = "refs/heads/nightly-next", event_name: str = "push",
        workflow_ref_branch: str | None = None, repository: str = REPO, **payload):
    event = {"repository": {"full_name": repository}, **payload}
    if event_name == "push":
        event.setdefault("ref", ref)
    return dict(event_name=event_name, ref=ref, repository=repository, event=event,
                workflow_ref=f"{repository}/.github/workflows/{workflow}@{workflow_ref_branch or ref}", job=job_name)


class RefusalTest(unittest.TestCase):
    def why(self, **kw):
        return admit.refusal(hook, kw["event_name"], kw["ref"], kw["repository"], kw["event"], kw["workflow_ref"],
                             kw["job"])

    def test_nightly_jobs_on_nightly_next_and_main_are_admitted(self):
        for ref in ("refs/heads/nightly-next", "refs/heads/main"):
            for name in ("decide", "build-nightly-app", "build-nightly-ghostty-cli-helper", "pick-nightly-build-runner",
                         "resolve-nightly-cmux-tui-client", "find-nightly-next-recovery", "build-nightly-inputs"):
                with self.subTest(ref=ref, job=name):
                    self.assertIsNone(self.why(**job(name, ref=ref)))
        self.assertIsNone(self.why(**job("decide", event_name="workflow_dispatch")))
        self.assertIsNone(self.why(**job("decide", ref="refs/heads/main", event_name="schedule")))

    def test_a_called_workflow_runs_under_the_callers_workflow_ref(self):
        # nightly.yml's `uses: ./.github/workflows/nightly-tag-permission-probe.yml`: GITHUB_WORKFLOW_REF stays the
        # caller's, so a required child is admitted with nightly.yml and never on its own
        self.assertIsNone(self.why(**job("probe", ref="refs/heads/nightly-next")))
        self.assertIsNotNone(self.why(**job("probe", workflow="nightly-tag-permission-probe.yml")))

    def test_the_promotion_release_compile_is_admitted_only_on_a_feat_cmux_next_push(self):
        ok = job("release-compile", workflow="cmux-next.yml", ref="refs/heads/feat-cmux-next")
        self.assertIsNone(self.why(**ok))
        self.assertIsNotNone(self.why(**job("cmux-scheme-compile", workflow="cmux-next.yml",
                                            ref="refs/heads/feat-cmux-next")))
        self.assertIsNotNone(self.why(**job("release-compile", workflow="cmux-next.yml", ref="refs/heads/feat-cmux-next",
                                            event_name="workflow_dispatch", inputs={"dev_daily": "true"})))

    def test_dev_daily_canary_and_launch_smoke_are_refused(self):
        for case in (job("sign", workflow="cmux-next-dev-daily.yml", ref="refs/heads/main",
                         event_name="workflow_dispatch"),
                     job("verify", workflow="nightly-next-canary.yml", ref="refs/heads/main", event_name="schedule"),
                     job("smoke", workflow="nightly-next-launch-smoke.yml", ref="refs/heads/main",
                         event_name="schedule")):
            with self.subTest(case=case["workflow_ref"]):
                why = self.why(**case)
                self.assertIsNotNone(why)
                self.assertIn("not the nightly itself", why)

    def test_pr_dev_and_foreign_jobs_are_refused(self):
        cases = {
            "pull request": job("decide", event_name="pull_request", ref="refs/pull/1/merge",
                                pull_request={"number": 1}),
            "a nightly payload with a pull request": job("decide", pull_request={"number": 1}),
            "workflow_run": job("decide", workflow_run={"id": 1}),
            "another branch": job("decide", ref="refs/heads/feature", workflow_ref_branch="refs/heads/feature"),
            "nightly.yml from another ref": job("decide", workflow_ref_branch="refs/heads/feature"),
            "another repository": job("decide", repository="manaflow-ai/other"),
            "a fork's push": dict(job("decide"), event={"repository": {"full_name": "someone/cmux"},
                                                         "ref": "refs/heads/nightly-next"}),
            "a DEV compile": job("cmux-scheme-compile", workflow="cmux-next.yml", ref="refs/heads/feat-cmux-next"),
            "a PR test workflow": job("swift-tests", workflow="ci.yml", ref="refs/heads/main"),
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.assertIsNotNone(self.why(**case))

    def test_signing_jobs_keep_their_exclusive_route(self):
        for workflow, name in hook.SIGNING_JOBS:
            if workflow != "nightly.yml":
                continue
            with self.subTest(job=name):
                why = self.why(**job(name))
                self.assertIsNotNone(why)
                self.assertIn("signing job", why)

    def test_classic_auto_resume_key_free_jobs_are_admitted(self):
        # ONE_MINI_NIGHTLY, AUTO_RESUME: main's auto-resume-nightly-notarization.yml after main's nightly.yml run on
        # main (workflow_run), or its dispatch from main: the two jobs that hold no key (preflight, verify-stapled)
        # run here, light; its key-holding jobs keep the trusted signing runner, and the rest is refused
        source = {"path": ".github/workflows/nightly.yml", "head_branch": "main", "event": "schedule",
                  "pull_requests": [], "head_repository": {"full_name": REPO}, "repository": {"full_name": REPO}}
        repository = {"full_name": REPO, "default_branch": "main"}

        def resume(name, event_name="workflow_run", **changes):
            case = job(name, workflow="auto-resume-nightly-notarization.yml", ref="refs/heads/main",
                       event_name=event_name)
            if event_name == "workflow_run":
                case["event"] = {"action": "completed", "repository": repository,
                                 "workflow_run": {**source, **changes}}
            else:
                case["event"] = {"ref": "refs/heads/main", "repository": repository, "inputs": {}}
            return case
        for name in ("preflight", "verify-stapled"):
            for event_name in ("workflow_run", "workflow_dispatch"):
                with self.subTest(job=name, event_name=event_name):
                    self.assertIsNone(self.why(**resume(name, event_name)))
            self.assertEqual(hook.job_class(name, REPO, hook.DEFAULT_HOME_REPO,
                                            "auto-resume-nightly-notarization.yml")[0], "light")
        for name in hook.AUTO_RESUME_SIGNING_JOBS:
            with self.subTest(signing=name):
                self.assertIn("signing job", self.why(**resume(name)))
        for name in ("poll-outer", "poll-stapled", "publish", "decide"):
            with self.subTest(other=name):
                self.assertIsNotNone(self.why(**resume(name)))
        for changes in ({"head_branch": "nightly-next"}, {"head_branch": "feat-x"},
                        {"path": ".github/workflows/ci.yml"}, {"event": "pull_request"},
                        {"head_repository": {"full_name": "someone/cmux"}}, {"pull_requests": [{"number": 1}]}):
            with self.subTest(source=changes):
                self.assertIsNotNone(self.why(**resume("preflight", **changes)))
        other_branch = resume("preflight")
        other_branch["workflow_ref"] = (f"{REPO}/.github/workflows/auto-resume-nightly-notarization.yml"
                                        "@refs/heads/feat-x")
        self.assertIsNotNone(self.why(**other_branch))

    def test_cli_admits_and_refuses_from_the_job_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            def run(case):
                path = Path(tmp) / "event.json"
                path.write_text(json.dumps(case["event"]))
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GITHUB_EVENT_PATH": os.fspath(path),
                       "GITHUB_EVENT_NAME": case["event_name"], "GITHUB_REF": case["ref"],
                       "GITHUB_REPOSITORY": case["repository"], "GITHUB_WORKFLOW_REF": case["workflow_ref"],
                       "GITHUB_JOB": case["job"]}
                return subprocess.run([sys.executable, os.fspath(ADMIT), "--hook", os.fspath(HOOK)], env=env,
                                      capture_output=True, text=True, timeout=60, check=False)
            got = run(job("build-nightly-app"))
            self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
            self.assertIn("admitted", got.stdout)
            got = run(job("verify", workflow="nightly-next-canary.yml", ref="refs/heads/main", event_name="schedule"))
            self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
            self.assertIn("refused", got.stdout)
            got = subprocess.run([sys.executable, os.fspath(ADMIT), "--hook", os.fspath(Path(tmp) / "missing")],
                                 env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(got.returncode, 1)
            self.assertIn("cannot load the runner hook", got.stdout)


MINE = "41655282358"
NN_WORKFLOW_REF = f"{REPO}/.github/workflows/nightly.yml@refs/heads/nightly-next"


def appcast(*versions: int) -> str:
    items = "".join(f"<item><sparkle:version>{v}</sparkle:version></item>" for v in versions)
    return f'<?xml version="1.0"?><rss><channel>{items}</channel></rss>'


class NewestRunFirstTest(unittest.TestCase):
    """An older nightly-next run's jobs yield (are refused in seconds) only when the feed already has a newer build."""

    def why(self, job_name: str, highest=None, run_id: str = MINE, attempt: str = "1"):
        return admit.yield_refusal(job_name, run_id, attempt, highest)

    def test_scope_is_attempt_one_of_a_nightly_next_push_and_a_pre_publish_job(self):
        wf = NN_WORKFLOW_REF
        self.assertTrue(admit.yield_scope("push", "refs/heads/nightly-next", REPO, wf, "build-nightly-app", "1"))
        self.assertFalse(admit.yield_scope("push", "refs/heads/nightly-next", REPO, wf, "build-nightly-app", "2"))
        self.assertFalse(admit.yield_scope("workflow_dispatch", "refs/heads/nightly-next", REPO, wf,
                                           "build-nightly-app", "1"))
        self.assertFalse(admit.yield_scope("push", "refs/heads/main", REPO,
                                           f"{REPO}/.github/workflows/nightly.yml@refs/heads/main",
                                           "build-nightly-app", "1"))
        for name in ("decide", "legacy-signer-dmg-image", "verify-nightly-identity-legacy-pool",
                     "nightly-identity-legacy-published", "report-nightly-failure", "close-nightly-failure-issue",
                     "publish-nightly", "generate-nightly-deltas"):
            with self.subTest(job=name):
                self.assertFalse(admit.yield_scope("push", "refs/heads/nightly-next", REPO, wf, name, "1"))

    def test_an_overtaken_run_yields_every_pre_publish_job(self):
        build = int(MINE) * 100 + 1
        for name in sorted(admit.BEFORE_PUBLISH_JOBS):
            with self.subTest(job=name):
                why = self.why(name, highest=build + 100)
                self.assertIsNotNone(why)
                self.assertIn("can never publish", why)
        self.assertIsNone(self.why("signer-dmg-image", highest=build))  # its own published build is not newer
        self.assertIsNone(self.why("signer-dmg-image", highest=build - 100))
        self.assertIsNone(self.why("signer-dmg-image", highest=None))  # an unreadable feed admits
        self.assertIsNone(self.why("legacy-signer-dmg-image", highest=build + 100))  # post-publication: never

    def test_a_newer_attempt_of_the_same_run_counts_but_this_runs_own_attempt_does_not(self):
        build = int(MINE) * 100 + 1
        self.assertIsNotNone(self.why("build-nightly-app", highest=build + 1))  # attempt 2 of this run published
        self.assertIsNone(self.why("build-nightly-app", highest=build, attempt="1"))

    def test_a_newer_push_alone_never_makes_a_run_yield(self):
        # cmux #28961: skipping for any newer push let promotions outrun every build. With nothing published above it,
        # the run keeps every slot, whatever newer runs exist or are building.
        self.assertIsNone(self.why("build-nightly-app", highest=int(MINE) * 100 - 500))
        self.assertIsNone(self.why("build-nightly-app", highest=None))

    def test_ids_that_are_not_numbers_admit(self):
        self.assertIsNone(self.why("build-nightly-app", highest=10 ** 17, run_id="abc"))
        self.assertIsNone(self.why("build-nightly-app", highest=10 ** 17, run_id=""))
        self.assertIsNone(self.why("build-nightly-app", highest=10 ** 17, attempt="x"))

    def test_feed_parsing(self):
        self.assertEqual(admit.feed_highest(appcast(4165528235801, 4165709173201, 4165000000001)), 4165709173201)
        self.assertIsNone(admit.feed_highest("<rss></rss>"))

    def test_newest_first_refusal_never_raises(self):
        env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/nightly-next", "GITHUB_REPOSITORY": REPO,
               "GITHUB_WORKFLOW_REF": NN_WORKFLOW_REF, "GITHUB_JOB": "build-nightly-app", "GITHUB_RUN_ATTEMPT": "1",
               "GITHUB_RUN_ID": MINE}
        with mock.patch.object(admit, "read_feed_highest", side_effect=RuntimeError("boom")):
            self.assertIsNone(admit.newest_first_refusal(hook, env))

    def cli(self, tmp: str, feed: Path, job_name: str, run_id: str, ref: str = "refs/heads/nightly-next",
            extra: list[str] | None = None):
        event = Path(tmp) / "event.json"
        event.write_text(json.dumps({"ref": ref, "repository": {"full_name": REPO}}))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp, "GITHUB_EVENT_PATH": os.fspath(event),
               "GITHUB_EVENT_NAME": "push", "GITHUB_REF": ref, "GITHUB_REPOSITORY": REPO,
               "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/nightly.yml@{ref}", "GITHUB_JOB": job_name,
               "GITHUB_RUN_ID": run_id, "GITHUB_RUN_ATTEMPT": "1", admit.FEED_URL_ENV: feed.as_uri()}
        return subprocess.run([sys.executable, os.fspath(ADMIT), "--hook", os.fspath(HOOK), *(extra or [])], env=env,
                              capture_output=True, text=True, timeout=60, check=False)

    def test_cli_yields_an_overtaken_job_and_admits_the_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "appcast-arm64.xml"
            newer = str(int(MINE) + 1000)
            feed.write_text(appcast(int(newer) * 100 + 1))
            got = self.cli(tmp, feed, "verify-final-dmg-nightly", MINE)
            self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
            self.assertIn("yielded to a newer run", got.stdout)
            got = self.cli(tmp, feed, "verify-final-dmg-nightly", newer)  # its own build is the feed's highest
            self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
            got = self.cli(tmp, feed, "build-nightly-app", str(int(newer) + 1))
            self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
            feed.unlink()  # an unreadable feed admits
            got = self.cli(tmp, feed, "build-nightly-app", MINE)
            self.assertEqual(got.returncode, 0, got.stdout + got.stderr)

    def test_the_trusted_path_on_main_never_yields(self):
        # --trusted-ref serves main's nightly: out of scope (nightly-next only), so a high feed changes nothing there
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "appcast-arm64.xml"
            feed.write_text(appcast(10 ** 17))
            with mock.patch.object(admit, "read_feed_highest", side_effect=AssertionError("feed read out of scope")):
                env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": REPO,
                       "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/nightly.yml@refs/heads/main",
                       "GITHUB_JOB": "build-nightly-app", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": MINE}
                self.assertIsNone(admit.newest_first_refusal(hook, env))
            got = self.cli(tmp, feed, "build-nightly-app", MINE, ref="refs/heads/main",
                           extra=["--trusted-ref", "refs/heads/main", "--trusted-repo", REPO])
            self.assertNotIn("yielded to a newer run", got.stdout + got.stderr)


def manifest(runner: dict) -> dict:
    return {"defaults": {"xcode": {"apps": [{"path": "/Applications/Xcode_26.6.app", "version": "26.6"}]}},
            "hosts": {"m": {"class": "std", "availability": "dedicated", "roles": ["ci-runner"], "hardware": "m4pro-48",
                            "overrides": {"runner": runner}}}}


NIGHTLY_ONLY = {"name": "nightly-only-1", "repo": REPO, "labels": ["glaeda-nightly-only-m4pro",
                                                                  "glaeda-runner-m-nightly-only-1"],
                "admit": ["{python}", "~/glaeda-runner/scripts/glaeda-nightly-only-admit",
                          "--hook", "{runnerDir}/glaeda-hooks/glaeda-cmux-runner-hook"]}


class DedicatedOnlyTest(unittest.TestCase):
    def test_member_labels_reports_dedicated_only_and_validates_it(self):
        summary, why = labels.member_labels(manifest({"dedicatedOnly": True, "dedicated": [NIGHTLY_ONLY]}), "m",
                                            lambda app: True)
        self.assertIsNone(why)
        self.assertTrue(summary["dedicatedOnly"])
        self.assertEqual(summary["pools"], [])  # in no pool: declared_pools and capacity totals leave it out
        self.assertEqual(labels.declared_pools(manifest({"dedicatedOnly": True, "dedicated": [NIGHTLY_ONLY]})), {})
        pooled = manifest({"dedicatedOnly": True, "dedicated": [NIGHTLY_ONLY]})
        pooled["defaults"]["runner"] = {"classes": {"std": {"gatewayRunners": 1}}}  # the pools' default
        summary, why = labels.member_labels(pooled, "m", lambda app: True)
        self.assertIsNone(why)
        self.assertEqual(summary["gatewayRunners"], 0)
        # a trusted-only pool is the only server of its trusted labels: never retired by dedicatedOnly
        trusted = manifest({"dedicatedOnly": True, "dedicated": [NIGHTLY_ONLY], "trustedRef": "refs/heads/main",
                            "trustedRepo": REPO})
        summary, why = labels.member_labels(trusted, "m", lambda app: True)
        self.assertIsNone(summary)
        self.assertIn("trusted-only pool", why)
        trusted["hosts"]["m"]["overrides"]["runner"].pop("dedicatedOnly")
        summary, why = labels.member_labels(trusted, "m", lambda app: True)
        self.assertIsNone(why)
        self.assertTrue(summary["pools"])  # it keeps its trusted pool beside the dedicated runners
        summary, _ = labels.member_labels(manifest({}), "m", lambda app: True)
        self.assertFalse(summary["dedicatedOnly"])
        for bad in ({"dedicatedOnly": "yes", "dedicated": [NIGHTLY_ONLY]}, {"dedicatedOnly": True},
                    {"dedicatedOnly": True, "dedicated": [NIGHTLY_ONLY], "nightlySlot": True}):
            with self.subTest(bad=bad):
                summary, why = labels.member_labels(manifest(bad), "m", lambda app: True)
                self.assertIsNone(summary)
                self.assertIn("dedicatedOnly", why)

    def test_a_dedicated_command_may_start_with_the_hook_python(self):
        entries, why = labels.dedicated_runners(manifest({"dedicatedOnly": True, "dedicated": [NIGHTLY_ONLY]}), "m")
        self.assertIsNone(why)
        self.assertEqual(entries[0]["admit"][0], "{python}")
        bad = dict(NIGHTLY_ONLY, admit=["python3", "x"])
        _, why = labels.dedicated_runners(manifest({"dedicated": [bad]}), "m")
        self.assertIn("{python}", why)

    def test_the_runner_expands_the_hook_python_word(self):
        runner = load("glaeda_cmux_runner_nightly_only", ROOT / "scripts" / "glaeda-cmux-runner")
        ctx = SimpleNamespace(python="/opt/homebrew/bin/python3", home=Path("/Users/cmux"),
                              runner_dir=Path("/Users/cmux/actions-runner-glaeda-nightly-only-1"))
        words = [runner.dedicated_word(ctx, w) for w in NIGHTLY_ONLY["admit"]]
        self.assertEqual(words, ["/opt/homebrew/bin/python3", "/Users/cmux/glaeda-runner/scripts/glaeda-nightly-only-admit",
                                 "--hook", "/Users/cmux/actions-runner-glaeda-nightly-only-1/glaeda-hooks/"
                                           "glaeda-cmux-runner-hook"])
        self.assertEqual(runner.dedicated_word(ctx, "x{python}"), "x{python}")  # only a whole word

    def test_the_fleet_stages_the_admission_beside_the_hook(self):
        self.assertIn("glaeda-nightly-only-admit", fleet.STAGED)

    def test_installed_pool_instances_lists_only_pool_receipts(self):
        out = ("receipt receipt.json\nreceipt instance-3/receipt.json\nreceipt instance-12/receipt.json\n"
               "dir actions-runner-glaeda\ndir actions-runner-glaeda-3\ndir actions-runner-glaeda-nightly-next\n"
               "receipt instance-dedicated-nightly-next/receipt.json\nend\n")
        with mock.patch.object(fleet, "run", return_value=(0, out, "")) as run:
            self.assertEqual(fleet.installed_pool_instances("m"), ([0, 3, 12], None))
        self.assertIn("ls -1A", run.call_args[0][0][-1])  # ls, never a glob: an unreadable directory fails
        with mock.patch.object(fleet, "run", return_value=(0, "receipt receipt.json\ndir actions-runner-glaeda\n"
                                                              "dir actions-runner-glaeda-5\nend\n", "")):
            got, why = fleet.installed_pool_instances("m")
        self.assertIsNone(got)
        self.assertIn("no receipt (instances [5])", why)
        with mock.patch.object(fleet, "run", return_value=(255, "", "no route")):
            got, why = fleet.installed_pool_instances("m")
        self.assertIsNone(got)
        self.assertIn("no route", why)
        # review of 5758205: an incomplete or odd listing is unknown state, never "no pool instances"
        for out, needle in (("", "did not complete"), ("receipt receipt.json\n", "did not complete"),
                            ("cd: permission denied\nend\n", "cannot read")):
            with self.subTest(out=out), mock.patch.object(fleet, "run", return_value=(0, out, "")):
                got, why = fleet.installed_pool_instances("m")
            self.assertIsNone(got)
            self.assertIn(needle, why)

    def test_retire_mints_a_remove_token_for_the_receipts_scope_and_never_names_the_manifest(self):
        calls = []

        def fake_run(argv, timeout, stdin=None):
            calls.append((argv[-1], stdin))
            if argv[-1] == fleet.POOL_RECEIPTS:
                return 0, "end\n", ""
            if "--apply" not in argv[-1]:
                return 0, json.dumps({"actions": [{"kind": "deregister", "state": "remove",
                                                   "scope": "manaflow-ai"}]}), ""
            return 0, "{}", ""
        with mock.patch.object(fleet, "run", side_effect=fake_run), \
                mock.patch.object(fleet, "mint", return_value="tok") as mint:
            got = fleet.retire_instance("m", 4)
        self.assertEqual(got["state"], "retired")
        mint.assert_called_once_with("orgs/manaflow-ai", "remove-token")
        self.assertEqual(calls[-1][0], fleet.POOL_RECEIPTS)  # the retirement is confirmed on the mini afterwards
        calls = calls[:-1]
        self.assertTrue(all("--manifest" not in cmd and "--instance 4" in cmd for cmd, _ in calls))
        self.assertEqual(calls[-1][1], "tok\n")

    def test_a_busy_instance_is_left_alone(self):
        def fake_run(argv, timeout, stdin=None):
            if "--apply" not in argv[-1]:
                return 0, json.dumps({"actions": [{"kind": "deregister", "state": "remove",
                                                   "scope": "manaflow-ai/cmux"}]}), ""
            return 1, json.dumps({"actions": [{"kind": "agent", "state": "failed",
                                               "note": "the runner has a job (or a listener runs); nothing removed"}]}), ""
        with mock.patch.object(fleet, "run", side_effect=fake_run), \
                mock.patch.object(fleet, "mint", return_value="tok") as mint:
            got = fleet.retire_instance("m", 0)
        mint.assert_called_once_with("repos/manaflow-ai/cmux", "remove-token")
        self.assertEqual(got["exit"], 1)
        self.assertIn("has a job", got["state"])


class DedicatedOnlyApplyTest(unittest.TestCase):
    def test_the_receipt_listing_survives_an_unmatched_glob(self):
        for files in ([], ["receipt.json"], ["receipt.json", "instance-2/receipt.json"]):
            with self.subTest(files=files), tempfile.TemporaryDirectory() as home:
                (Path(home) / "actions-runner-glaeda-nightly-next").mkdir()
                state = Path(home) / ".local/state/glaeda/cmux-runner"
                state.mkdir(parents=True)
                for name in files:
                    (state / name).parent.mkdir(parents=True, exist_ok=True)
                    (state / name).write_text("{}")
                # the login shell parses the command first (zsh on a mini); sh then expands the glob
                got = subprocess.run(["/bin/bash", "-c", fleet.POOL_RECEIPTS], env={"HOME": home, "PATH": "/usr/bin:/bin"},
                                     capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertEqual(sorted(got.stdout.splitlines()[:-2]), sorted(f"receipt {f}" for f in files))
                self.assertEqual(got.stdout.splitlines()[-2:], ["dir actions-runner-glaeda-nightly-next", "end"])

    def test_an_unreadable_state_directory_fails_the_listing(self):
        with tempfile.TemporaryDirectory() as home:
            state = Path(home) / ".local/state/glaeda/cmux-runner"
            state.mkdir(parents=True)
            state.chmod(0o300)  # enterable but not readable: a glob would list nothing and exit 0 (a1de21e4)
            try:
                got = subprocess.run(["/bin/bash", "-c", fleet.POOL_RECEIPTS],
                                     env={"HOME": home, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
                                     timeout=30, check=False)
            finally:
                state.chmod(0o700)
            if os.geteuid() != 0:  # root enters any directory
                self.assertEqual(got.returncode, 3, got.stdout + got.stderr)
                self.assertNotIn("end", got.stdout)

    def test_an_unregistered_instance_is_retired_without_a_token(self):
        calls = []

        def fake_run(argv, timeout, stdin=None):
            calls.append((argv[-1], stdin))
            if argv[-1] == fleet.POOL_RECEIPTS:
                return 0, "end\n", ""
            if "--apply" not in argv[-1]:
                return 0, json.dumps({"actions": [{"kind": "deregister", "state": "kept"}]}), ""
            return 0, "{}", ""
        with mock.patch.object(fleet, "run", side_effect=fake_run), mock.patch.object(fleet, "mint") as mint:
            got = fleet.retire_instance("m", 2)
        self.assertEqual(got["state"], "retired")
        mint.assert_not_called()
        self.assertNotIn("--token-stdin", calls[-2][0])
        self.assertIsNone(calls[-2][1])

    def test_an_uninstall_that_exits_0_but_leaves_the_runner_is_not_retired(self):
        for listing, needle in (("receipt instance-3/receipt.json\ndir actions-runner-glaeda-3", "still there"),
                                ("dir actions-runner-glaeda-3", "no receipt")):
            def fake_run(argv, timeout, stdin=None, listing=listing):
                if argv[-1] == fleet.POOL_RECEIPTS:
                    return 0, listing + "\nend\n", ""
                if "--apply" not in argv[-1]:
                    return 0, json.dumps({"actions": [{"kind": "deregister", "state": "kept"}]}), ""
                return 0, "{}", ""  # kept a directory it does not own, and exited 0
            with self.subTest(listing=listing), mock.patch.object(fleet, "run", side_effect=fake_run), \
                    mock.patch.object(fleet, "mint"):
                got = fleet.retire_instance("m", 3)
            self.assertEqual(got["exit"], 1)
            self.assertIn("unconfirmed", got["state"])
            self.assertIn(needle, got["state"])

    def test_a_retirement_with_no_deregistration_plan_fails_closed(self):
        with mock.patch.object(fleet, "run", return_value=(0, json.dumps({"actions": [{"kind": "note"}]}), "")), \
                mock.patch.object(fleet, "mint") as mint:
            got = fleet.retire_instance("m", 1)
        mint.assert_not_called()
        self.assertIsNone(got["exit"])
        self.assertIn("no deregistration plan", got["state"])

    def one_member(self, retired_exit, relisted=([], None)):
        summary = {"class": "std", "runners": 4, "gatewayRunners": 0, "capacityUnits": 14, "dedicatedOnly": True}
        args = SimpleNamespace(scope_flags=False, apply=True, include_ineligible=False, allow_downgrade=None,
                               guard={"head": "h", "refusals": []}, skip_launchctl=False, org=None,
                               migrate_from_repo=None)
        man = manifest({"dedicatedOnly": True, "dedicated": [NIGHTLY_ONLY]})
        with mock.patch.object(fleet, "check", return_value=(True, "ok")), \
                mock.patch.object(fleet, "stage", return_value=None), \
                mock.patch.object(fleet, "prepare_headless", return_value=None), \
                mock.patch.object(fleet, "installed_pool_instances", side_effect=[([0, 1], None), relisted]), \
                mock.patch.object(fleet, "retire_instance",
                                  side_effect=lambda m, k: {"instance": k, "exit": retired_exit, "state": "x"}), \
                mock.patch.object(fleet, "apply_dedicated",
                                  return_value={"dedicated": "nightly-only-1", "exit": 0, "state": "ok"}) as dedicated:
            row = fleet.one_member("m", summary, args, man, Path("/nonexistent"))
        return row, dedicated

    def test_dedicated_runners_wait_until_every_pool_instance_is_retired(self):
        row, dedicated = self.one_member(1)
        dedicated.assert_not_called()
        self.assertEqual(row["action"], "failed")
        self.assertIn("held", row["instances"][-1]["state"])
        row, dedicated = self.one_member(0)
        dedicated.assert_called_once()
        self.assertEqual(row["action"], "applied")

    def test_a_pool_instance_installed_after_the_snapshot_holds_the_dedicated_runners(self):
        # review of 8c6009cd: every recorded retirement succeeded, but a concurrent apply added instance 5 meanwhile
        for relisted in (([5], None), (None, "cannot list its runner receipts")):
            with self.subTest(relisted=relisted):
                row, dedicated = self.one_member(0, relisted)
                dedicated.assert_not_called()
                self.assertEqual(row["action"], "failed")
                self.assertIn("still has pool instances", row["instances"][-1]["state"])


TRUSTED = {"name": "trusted-main", "repo": REPO, "trustedRef": "refs/heads/main", "trustedRepo": REPO,
           "labels": ["glaeda-trusted-std-xcode-26.6", "glaeda-runner-t-glaeda"],
           "admit": ["{python}", "~/glaeda-runner/scripts/glaeda-nightly-only-admit", "--hook",
                     "{runnerDir}/glaeda-hooks/glaeda-cmux-runner-hook", "--trusted-ref", "refs/heads/main",
                     "--trusted-repo", REPO]}


def two_hosts(entry: dict, trusted_ref: str = "refs/heads/main") -> dict:
    """A trusted-only member t (its pool serves glaeda-trusted-std-xcode-26.6) and a dedicatedOnly member m."""
    doc = manifest({"dedicatedOnly": True, "dedicated": [NIGHTLY_ONLY, entry]})
    doc["hosts"]["t"] = {"class": "std", "availability": "dedicated", "roles": ["ci-runner"], "hardware": "m4pro-48",
                         "overrides": {"runner": {"trustedRef": trusted_ref, "trustedRepo": REPO}}}
    return doc


class TrustedDedicatedTest(unittest.TestCase):
    """DEDICATED_TRUSTED: a second host serves a trusted pool's labels through a trusted runner.dedicated entry."""

    def test_a_trusted_entry_may_carry_a_trusted_pool_label_of_the_same_trust(self):
        self.assertIn("glaeda-trusted-std-xcode-26.6", labels.declared_pools(two_hosts(TRUSTED)))
        entries, why = labels.dedicated_runners(two_hosts(TRUSTED), "m")
        self.assertIsNone(why)
        entry = next(e for e in entries if e["name"] == "trusted-main")
        self.assertEqual((entry["trustedRef"], entry["trustedRepo"]), ("refs/heads/main", REPO))
        self.assertIsNone(next(e for e in entries if e["name"] == "nightly-only-1")["trustedRef"])

    def test_the_pool_label_stays_refused_without_matching_trust(self):
        untrusted = {k: v for k, v in TRUSTED.items() if k not in ("trustedRef", "trustedRepo")}
        cases = {"no trust": (untrusted, "refs/heads/main"),
                 "another ref than the pool's": (dict(TRUSTED, trustedRef="refs/heads/feat-cmux-next"), "refs/heads/main"),
                 "an untrusted pool label": (dict(TRUSTED, labels=["glaeda-std-xcode-26.6"]), "refs/heads/main")}
        for name, (entry, ref) in cases.items():
            with self.subTest(name):
                doc = two_hosts(entry, ref)
                if name == "an untrusted pool label":
                    doc["hosts"]["u"] = {"class": "std", "availability": "dedicated", "roles": ["ci-runner"],
                                         "hardware": "m4pro-48"}
                _, why = labels.dedicated_runners(doc, "m")
                self.assertIn("pool labels", why or "")

    def test_trust_needs_both_fields_and_a_repo_scope(self):
        for bad in ({k: v for k, v in TRUSTED.items() if k != "trustedRepo"},
                    dict(TRUSTED, trustedRepo="other/repo"),
                    dict({k: v for k, v in TRUSTED.items() if k != "repo"}, org="manaflow-ai"),
                    dict(TRUSTED, trustedRef="main")):
            with self.subTest(bad=bad):
                _, why = labels.dedicated_runners(two_hosts(bad), "m")
                self.assertIn("trusted", why or "")

    def test_the_hook_gets_the_entrys_trust_on_an_untrusted_member(self):
        runner = load("glaeda_cmux_runner_trusted_dedicated", ROOT / "scripts" / "glaeda-cmux-runner")
        home = Path("/Users/cmux")

        def started(entry):
            ctx = mock.Mock(python=sys.executable, home=home, hook_dir=home / "h",
                            runner_dir=home / "actions-runner-glaeda-x",
                            org=None, repo=REPO, min_free_gib=0, capacity_units=0, labels=list(entry["labels"]),
                            session_runner=False, dedicated=entry, instance=None, gateway_runner=False,
                            member={"hardware": "m4pro-48", "xcodeApps": [], "trustedRef": None})
            return b"".join(runner.hook_wrappers(ctx).values()).decode()
        member_ctx = started  # the member summary path below; the real dedicated path leaves ctx.member None

        def dedicated_path(entry):
            ctx = mock.Mock(python=sys.executable, home=home, hook_dir=home / "h",
                            runner_dir=home / "actions-runner-glaeda-x", org=None, repo=REPO, min_free_gib=0,
                            capacity_units=0, labels=list(entry["labels"]), session_runner=False, dedicated=entry,
                            instance=None, gateway_runner=False, member=None)
            return b"".join(runner.hook_wrappers(ctx).values()).decode()
        # as dedicated_context() builds it (review of d24d574e: the trust arguments must not need ctx.member)
        text = dedicated_path(dict(TRUSTED))
        self.assertIn("--trusted-ref refs/heads/main --trusted-repo manaflow-ai/cmux", text)
        self.assertNotIn("--trusted-ref", dedicated_path(dict(NIGHTLY_ONLY, trustedRef=None, trustedRepo=None)))
        text = member_ctx(dict(TRUSTED))
        self.assertNotIn("--test-keychain", text)
        self.assertIn("--trusted-ref refs/heads/main --trusted-repo manaflow-ai/cmux", text)
        self.assertNotIn("--trusted-next-nightly", text)
        self.assertNotIn("--trusted-ref", started(dict(NIGHTLY_ONLY, trustedRef=None, trustedRepo=None)))


class TrustedAdmitTest(unittest.TestCase):
    def why(self, run_attempt="1", **kw):
        return admit.trusted_refusal(hook, kw["event_name"], kw["ref"], kw["repository"], kw["event"],
                                     kw["workflow_ref"], kw["job"], run_attempt, "refs/heads/main", REPO)

    def test_main_push_and_schedule_jobs_are_admitted(self):
        self.assertIsNone(self.why(**job("seed", workflow="seed-derived-data.yml", ref="refs/heads/main")))
        self.assertIsNone(self.why(**job("build-nightly-app", ref="refs/heads/main")))
        sched = job("build-nightly-app", ref="refs/heads/main", event_name="schedule")
        sched["event"]["repository"]["default_branch"] = "main"
        self.assertIsNone(self.why(**sched))

    def test_pr_dispatch_other_refs_and_signing_are_refused(self):
        pr = job("build", workflow="ci.yml", ref="refs/pull/1/merge", event_name="pull_request",
                 pull_request={"head": {"repo": {"full_name": REPO}}})
        for name, case in {"pull request": pr,
                           "dispatch": job("build", workflow="cmux-next-dev-daily.yml", ref="refs/heads/main",
                                           event_name="workflow_dispatch"),
                           "nightly-next": job("build-nightly-app"),
                           "a main push side job": job("build", workflow="ci.yml", ref="refs/heads/main"),
                           "a main schedule side job": dict(job("report", workflow="ci-health-report.yml",
                                                                ref="refs/heads/main", event_name="schedule")),
                           "signing": job("build-sign-notarize-nightly", ref="refs/heads/main")}.items():
            with self.subTest(name):
                self.assertIsNotNone(self.why(**case))

    def test_cli_trusted_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = job("seed", workflow="seed-derived-data.yml", ref="refs/heads/main")
            path = Path(tmp) / "event.json"
            path.write_text(json.dumps(case["event"]))
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GITHUB_EVENT_PATH": os.fspath(path),
                   "GITHUB_EVENT_NAME": "push", "GITHUB_REF": case["ref"], "GITHUB_REPOSITORY": REPO,
                   "GITHUB_WORKFLOW_REF": case["workflow_ref"], "GITHUB_JOB": "seed", "GITHUB_RUN_ATTEMPT": "1"}
            base = [sys.executable, os.fspath(ADMIT), "--hook", os.fspath(HOOK)]
            got = subprocess.run(base + ["--trusted-ref", "refs/heads/main", "--trusted-repo", REPO], env=env,
                                 capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
            got = subprocess.run(base, env=env, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(got.returncode, 1, "the nightly-only mode refuses a seed")
            got = subprocess.run(base + ["--trusted-ref", "refs/heads/main"], env=env, capture_output=True, text=True,
                                 timeout=60, check=False)
            self.assertEqual(got.returncode, 2)

    def test_ota_ships_the_admission_with_the_runner_files(self):
        update = load("glaeda_update_nightly_only", ROOT / "scripts" / "glaeda-update")
        self.assertIn("glaeda-nightly-only-admit", update.RUNNER_FILES)


class RetirementRaceTest(unittest.TestCase):
    """Review of 611e013e: the dedicatedOnly invariant is held on the mini, and a recorded registration with no
    runner directory is never read as unregistered."""

    def test_pool_installs_lists_pool_receipts_and_dirs_only(self):
        runner = load("glaeda_cmux_runner_pool_installs", ROOT / "scripts" / "glaeda-cmux-runner")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state = home / ".local/state/glaeda/cmux-runner"
            (state / "instance-dedicated-nightly-only-1").mkdir(parents=True)
            (state / "instance-dedicated-nightly-only-1/receipt.json").write_text("{}")
            (home / "actions-runner-glaeda-nightly-only-1").mkdir()
            self.assertEqual(runner.pool_installs(home), [])
            (state / "instance-3").mkdir()
            (state / "instance-3/receipt.json").write_text("{}")
            (home / "actions-runner-glaeda").mkdir()
            self.assertEqual(runner.pool_installs(home), ["instance-3/receipt.json", "actions-runner-glaeda"])

    def test_a_dedicated_apply_on_a_dedicated_only_member_refuses_beside_a_pool_instance(self):
        runner = load("glaeda_cmux_runner_dedicated_only_lock", ROOT / "scripts" / "glaeda-cmux-runner")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "actions-runner-glaeda-2").mkdir()
            man = Path(tmp) / "m.json"
            man.write_text(json.dumps(manifest({"dedicatedOnly": True, "dedicated": [NIGHTLY_ONLY]})))
            with mock.patch.object(runner.Path, "home", return_value=home), \
                    mock.patch.object(runner, "DARWIN_REQUIRED", False), \
                    mock.patch.dict(os.environ, {"HOME": tmp}), \
                    mock.patch.object(runner, "install_or_uninstall", return_value=0) as install, \
                    mock.patch.object(runner, "run", return_value=(1, "")), \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                code = runner.main(["--manifest", os.fspath(man), "--member", "m", "--dedicated", "nightly-only-1",
                                    "--apply", "--skip-launchctl"])
            self.assertEqual(code, 2)
            install.assert_not_called()
            self.assertIn("still has pool instances", err.getvalue())
            (home / "actions-runner-glaeda-2").rmdir()  # retired: the same apply now proceeds
            with mock.patch.object(runner.Path, "home", return_value=home), \
                    mock.patch.object(runner, "DARWIN_REQUIRED", False), \
                    mock.patch.dict(os.environ, {"HOME": tmp}), \
                    mock.patch.object(runner, "install_or_uninstall", return_value=0) as install, \
                    mock.patch.object(runner, "run", return_value=(1, "")):
                self.assertEqual(runner.main(["--manifest", os.fspath(man), "--member", "m", "--dedicated",
                                              "nightly-only-1", "--apply", "--skip-launchctl"]), 0)
            install.assert_called_once()

    def test_pool_installs_fails_closed_on_an_unreadable_directory(self):
        runner = load("glaeda_cmux_runner_pool_installs_unreadable", ROOT / "scripts" / "glaeda-cmux-runner")
        for target in ("state", "home"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / "h"
                state = home / ".local/state/glaeda/cmux-runner"
                (state / "instance-2").mkdir(parents=True)
                (state / "instance-2/receipt.json").write_text("{}")
                path = state if target == "state" else home
                path.chmod(0o300)
                try:
                    got = runner.pool_installs(home)
                finally:
                    path.chmod(0o700)
                if os.geteuid() != 0:
                    self.assertTrue(got and got[0].startswith("unreadable"), got)

    def test_a_stale_pool_apply_refuses_once_the_mac_is_dedicated_only(self):
        runner = load("glaeda_cmux_runner_marker", ROOT / "scripts" / "glaeda-cmux-runner")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            def args(**kw):
                return SimpleNamespace(**{"apply": True, "uninstall": False, "clear_dedicated_only": False,
                                          "member": "m", "dedicated": None, **kw})
            pool = SimpleNamespace(home=home, dedicated=None)
            dedicated = SimpleNamespace(home=home, dedicated=NIGHTLY_ONLY, dedicated_only=True)
            self.assertIsNone(runner.dedicated_only_refusal(pool, args()))  # no marker: pool applies as before
            self.assertIsNone(runner.dedicated_only_refusal(dedicated, args(dedicated="nightly-only-1")))
            marker = home / ".local/state/glaeda/cmux-runner" / runner.DEDICATED_ONLY_MARKER
            self.assertTrue(marker.exists())
            # a pool apply that read the old manifest before it waited for the lock is refused under it
            self.assertIn("dedicatedOnly", runner.dedicated_only_refusal(pool, args()))
            self.assertIsNone(runner.dedicated_only_refusal(pool, args(uninstall=True)))  # retiring still works
            self.assertIsNone(runner.dedicated_only_refusal(pool, args(clear_dedicated_only=True)))
            self.assertFalse(marker.exists())

    def test_a_recorded_registration_without_its_directory_is_not_retired(self):
        plan = {"actions": [{"kind": "deregister", "state": "kept", "registered": True, "scope": "manaflow-ai"}]}
        with mock.patch.object(fleet, "run", return_value=(0, json.dumps(plan), "")), \
                mock.patch.object(fleet, "mint") as mint:
            got = fleet.retire_instance("m", 6)
        mint.assert_not_called()
        self.assertIsNone(got["exit"])
        self.assertIn("records a registration", got["state"])


class NoEmDashTest(unittest.TestCase):
    def test_no_em_dash(self):
        for path in (ADMIT, Path(__file__)):
            self.assertNotIn(chr(0x2014), path.read_text(encoding="utf-8"), path.name)


if __name__ == "__main__":
    unittest.main()
