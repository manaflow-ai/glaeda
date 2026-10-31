#!/usr/bin/env python3
"""Tests for glaeda_never_cancel: the predicate's table, cancel_run, and that nothing else in glaeda cancels a run."""
from __future__ import annotations

import base64
import re
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import glaeda_never_cancel as nc  # noqa: E402

NOW = 1_800_000_000.0
SHA = "a" * 40
WF = """\
name: x
on: push
jobs:
  build:
    runs-on: x
    timeout-minutes: 90
    steps:
      - run: true
        timeout-minutes: 500
  publish:
    needs: build
    timeout-minutes: 20
    runs-on: x
    steps: [{run: true}]
"""


def run(path="nightly.yml", status="in_progress", event="schedule", name="", attempt=1):
    return {"id": 7, "path": f".github/workflows/{path}", "status": status, "event": event, "name": name,
            "display_title": name, "head_sha": SHA, "run_attempt": attempt}


def ts(min_ago):
    return datetime.fromtimestamp(NOW - min_ago * 60, timezone.utc).isoformat().replace("+00:00", "Z")


def job(status="in_progress", started_min_ago=5.0, runner="r1", setup_min=0.0, in_setup=False):
    """A job that started started_min_ago minutes ago and spent setup_min of them in "Set up runner"."""
    if status == "queued":
        return {"id": 1, "name": "build", "status": "queued", "runner_name": None, "started_at": None, "steps": []}
    steps = [{"name": "Set up job", "status": "completed", "started_at": ts(started_min_ago)},
             {"name": "Set up runner", "status": "in_progress" if in_setup else "completed",
              "started_at": ts(started_min_ago)}]
    if not in_setup:
        steps.append({"name": "Compile", "status": "in_progress", "started_at": ts(started_min_ago - setup_min)})
    return {"id": 1, "name": "build", "status": status, "runner_name": runner, "started_at": ts(started_min_ago),
            "steps": steps}


class DeliveryTable(unittest.TestCase):
    def test_which_runs_are_delivery_runs(self):
        cases = [
            (run("nightly.yml"), True),
            (run("nightly-sign.yml"), True),
            (run("nightly-next-train.yml"), True),
            (run("nightly-next-tick.yml"), True),
            (run("nightly-next-canary.yml"), True),
            (run("cmux-tui-nightly.yml"), True),
            (run("cmux-next-dev-daily.yml", event="workflow_dispatch"), True),
            (run("cmux-next-dev-daily-dispatch.yml"), True),
            (run("cloud-vm-image-promote.yml"), True),
            (run("promote.yml"), True),
            (run("release.yml"), True),
            (run("cmux-next.yml", event="push"), True),
            (run("cmux-next.yml", event="workflow_dispatch", name="cmux NEXT DEV daily · abc123"), True),
            (run("cmux-next.yml", event="pull_request"), False),
            (run("ci.yml", event="pull_request"), False),
            (run("ci.yml", event="push"), False),
            ({**run("nightly.yml"), "path": ".github/workflows/nightly.yml@refs/heads/feature"}, True),
            ({**run("x"), "path": ""}, False),
        ]
        for r, want in cases:
            with self.subTest(path=r["path"], event=r["event"], name=r["name"]):
                self.assertEqual(bool(nc.delivery_reason(r)), want)

    def test_any_branch(self):
        for branch in ("main", "feat-cmux-next", "some-fork-branch"):
            r = {**run("nightly.yml"), "head_branch": branch}
            self.assertFalse(nc.may_cancel(r, [job()], 90, NOW)[0], branch)


class PredicateTable(unittest.TestCase):
    def test_table(self):
        # (run, jobs, timeout minutes, allowed)
        cases = [
            ("non-delivery started run", run("ci.yml", event="pull_request"), [job()], None, True),
            ("non-delivery, jobs unreadable", run("ci.yml", event="pull_request"), None, None, True),
            ("delivery, queued, no job started", run(status="queued"), [job("queued")], None, True),
            ("delivery, started, within timeout", run(), [job(started_min_ago=30)], 90, False),
            ("delivery, started, waiting for a runner", run(), [job("queued")], 90, False),
            ("delivery, started, exactly at timeout", run(), [job(started_min_ago=90)], 90, False),
            ("delivery, stuck past timeout", run(), [job(started_min_ago=91)], 90, True),
            ("delivery, completed job past timeout", run(), [job("completed", started_min_ago=500)], 90, False),
            ("delivery, started, timeout unreadable", run(), [job(started_min_ago=1000)], None, False),
            ("delivery, jobs unreadable", run(), None, 90, False),
            ("delivery, run completed", run(status="completed"), [], 90, False),
            ("DEV daily stuck in setup 19 min of 20", run("cmux-next-dev-daily.yml"),
             [job(started_min_ago=19)], 20, False),
            ("promote past timeout", run("cloud-vm-image-promote.yml"), [job(started_min_ago=400)], 360, True),
            # A runner hook's admission wait in "Set up runner" never counts toward the timeout.
            ("still in admission 91 min of 90", run(), [job(started_min_ago=91, in_setup=True)], 90, False),
            ("still in admission for hours", run(), [job(started_min_ago=600, in_setup=True)], 90, False),
            ("left a 90 min admission 1 min ago", run(), [job(started_min_ago=91, setup_min=90)], 90, False),
            ("worked 91 min after a 30 min admission", run(), [job(started_min_ago=121, setup_min=30)], 90, True),
            ("steps not listed", run(), [{**job(started_min_ago=500), "steps": []}], 90, False),
            ("first work step skipped, the next one past timeout", run(),
             [{**job(started_min_ago=200), "steps": [
                 {"name": "Set up runner", "status": "completed", "started_at": ts(200)},
                 {"name": "Maybe", "status": "completed", "conclusion": "skipped", "started_at": None},
                 {"name": "Compile", "status": "in_progress", "started_at": ts(100)}]}], 90, True),
            # Any sign of a start counts, whatever status the run reports.
            ("waiting run, job with a start time", run(status="waiting"),
             [{**job("queued"), "started_at": ts(5)}], 90, False),
            ("waiting run, job with a runner", run(status="waiting"), [{**job("queued"), "runner_name": "m1"}], 90,
             False),
            ("queued run, a completed job", run(status="queued"), [job("completed")], 90, False),
            ("waiting run, nothing started", run(status="waiting"), [job("queued")], 90, True),
        ]
        for label, r, jobs, timeout, want in cases:
            with self.subTest(label):
                ok, why = nc.may_cancel(r, jobs, timeout, NOW)
                self.assertEqual(ok, want, why)
                self.assertTrue(why)

    def test_timeout_reading(self):
        self.assertEqual(nc.job_timeout_minutes(WF), 90)  # job level only; a step's 500 does not count
        self.assertEqual(nc.job_timeout_minutes(WF.replace("    timeout-minutes: 20\n", "")), 360)
        self.assertIsNone(nc.job_timeout_minutes(None))
        self.assertIsNone(nc.job_timeout_minutes("name: x\non: push\n"))
        self.assertIsNone(nc.job_timeout_minutes("jobs:\n  - nope\n"))


class Fake:
    def __init__(self, r, jobs, wf=WF):
        self.r, self.jobs, self.wf, self.posts, self.gets = r, jobs, wf, [], []

    def get(self, path):
        self.gets.append(path)
        if re.search(r"/actions/runs/7/attempts/\d+/jobs", path):
            if self.jobs is None:
                raise OSError("jobs unreadable")
            return {"jobs": self.jobs}
        if "/contents/" in path:
            assert f"ref={SHA}" in path, path
            if self.wf is None:
                raise OSError("404")
            return {"content": base64.b64encode(self.wf.encode()).decode()}
        raise AssertionError(path)

    def post(self, path):
        self.posts.append(path)


class CancelRun(unittest.TestCase):
    def cancel(self, fake, force=False):
        return nc.cancel_run(fake.get, fake.post, "o/r", fake.r, force=force, now=lambda: NOW)

    def test_non_delivery_run_is_cancelled_without_extra_reads(self):
        f = Fake(run("ci.yml", event="pull_request"), [job()])
        self.assertTrue(self.cancel(f)[0])
        self.assertEqual((f.gets, f.posts), ([], ["/repos/o/r/actions/runs/7/cancel"]))
        f = Fake(run("ci.yml", event="pull_request"), [job()])
        self.cancel(f, force=True)
        self.assertEqual(f.posts, ["/repos/o/r/actions/runs/7/force-cancel"])

    def test_started_delivery_run_is_never_cancelled(self):
        for force in (False, True):
            f = Fake(run(), [job(started_min_ago=30)])
            ok, why = self.cancel(f, force)
            self.assertFalse(ok, why)
            self.assertEqual(f.posts, [])

    def test_stuck_delivery_run_past_its_timeout_is_cancelled(self):
        f = Fake(run(), [job(started_min_ago=95)])
        self.assertTrue(self.cancel(f)[0])
        self.assertEqual(f.posts, ["/repos/o/r/actions/runs/7/cancel"])

    def test_malformed_jobs_refuse(self):
        for body in ({"jobs": ["x"]}, {"jobs": None}, {"jobs": {"a": 1}}, ["x"]):
            f = Fake(run(), [])
            f.get = lambda path, body=body: body if "/jobs" in path else Fake.get(f, path)
            self.assertFalse(self.cancel(f)[0], body)
            self.assertEqual(f.posts, [])

    def test_unreadable_reads_refuse(self):
        for f in (Fake(run(), None), Fake(run(), [job(started_min_ago=500)], wf=None)):
            self.assertFalse(self.cancel(f)[0])
            self.assertEqual(f.posts, [])


# Ways to cancel a GitHub Actions run or job: the REST endpoints in any spelling, gh's command in a shell line or an
# argv list, and SDK names. Comment lines are skipped; everything else is matched, f-string pieces included.
CANCEL_CALL = re.compile(
    r"actions/+(?:runs|jobs)/.*?/(?:force-)?cancel(?![\w-])"   # .../actions/runs/{run['id']}/cancel, force-cancel
    r"|[\"'`]/?(?:force-)?cancel[\"'`]"                        # a piece joined on later ("/cancel"), or argv's "cancel"
    r"|\brun\s+cancel\b"                                        # gh run cancel N
    r"|get_workflow_run\b|cancel[_-]?workflow[_-]?run|cancelWorkflowRun",  # PyGithub, octokit and other SDKs
    re.IGNORECASE)
GUARD = "scripts/glaeda_never_cancel.py"
# Each hit outside the guard's one POST, by file and exact line, with why it cancels no GitHub run.
ALLOWED = {
    ("src/main.rs", '"cancel",'): "the local personal-worker queue's `glaeda job cancel` subcommand in a CLI test",
    ("scripts/glaeda-gh", 'GH_CACHE_RUN_WRITES = {"rerun", "cancel"}'):
        "the answer cache reads a caller's `gh run cancel N` to decide what to invalidate; it cancels nothing",
}
GUARD_LINE = "post(f\"/repos/{repo}/actions/runs/{run_id}/{'force-cancel' if force else 'cancel'}\")"


def scanned_files() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
    names = [n for n in out.decode().split("\0") if n]
    # Docs, and tests that fake GitHub's endpoints, cancel nothing.
    return [n for n in names if not (n.endswith(".md") or re.search(r"(^|/)test[-_][^/]*$", n) or n.startswith("tests/"))]


def cancel_hits(name: str, text: str) -> list[str]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        code = line.strip()
        if code.startswith(("#", "//", "*", "/*")) or not CANCEL_CALL.search(code):
            continue
        if (name, code) in ALLOWED or (name == GUARD and code == GUARD_LINE):
            continue
        hits.append(f"{name}:{lineno}: {code}")
    return hits


class NoOtherCancelPath(unittest.TestCase):
    def test_only_the_guard_cancels_runs(self):
        offenders, guard_posts = [], 0
        for name in scanned_files():
            try:
                text = (ROOT / name).read_text()
            except (UnicodeDecodeError, OSError):
                continue
            offenders += cancel_hits(name, text)
            if name == GUARD:
                guard_posts = sum(1 for ln in text.splitlines() if ln.strip() == GUARD_LINE)
        self.assertEqual(offenders, [], "cancel a run only through glaeda_never_cancel.cancel_run, so a started "
                         "nightly, DEV or promote run is never cancelled:\n" + "\n".join(offenders))
        self.assertEqual(guard_posts, 1, "the guard's one cancel POST moved: update GUARD_LINE with it")

    def test_the_scan_catches_each_spelling(self):
        for line in ('api.ok("POST", f"/repos/{repo}/actions/runs/{run_id}/cancel")',
                     """api.ok("POST", f"/repos/{repo}/actions/runs/{run['id']}/cancel")""",
                     'post(f"/repos/{r}/actions/runs/{i}/force-cancel")',
                     'url = base + "/cancel"',
                     "gh run cancel 123 -R o/r",
                     'subprocess.run(["gh", "run", "cancel", str(run_id)])',
                     "subprocess.run(['gh', 'api', '-X', 'POST', 'repos/o/r/actions/runs/1/cancel'])",
                     "repo.get_workflow_run(run_id).cancel()",
                     "octokit.rest.actions.cancelWorkflowRun({owner, repo, run_id})",
                     "curl -X POST https://api.github.com/repos/o/r/actions/runs/$ID/cancel"):
            self.assertTrue(cancel_hits("scripts/x", line), line)
        for line in ("cancel-in-progress: false", "future.cancel()", 'conclusion == "cancelled"',
                     "the rescue cancels a job", "# gh run cancel is refused here", "    // run cancel docs"):
            self.assertFalse(cancel_hits("scripts/x", line), line)
        # The guard file gets no blanket pass: a second cancel there is caught too.
        self.assertTrue(cancel_hits(GUARD, 'api.ok("POST", f"/repos/{repo}/actions/runs/{run_id}/cancel")'))

    def test_glaeda_route_cancels_through_the_guard(self):
        route = (ROOT / "scripts/glaeda-route").read_text()
        self.assertIn("never_cancel.cancel_run(", route)
        self.assertIn("force=True", route)


if __name__ == "__main__":
    unittest.main()
