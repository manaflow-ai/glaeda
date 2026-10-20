#!/usr/bin/env python3
"""Contract tests for scripts/glaeda-gh, with a fake GitHub transport."""

from __future__ import annotations

import contextlib
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import socket
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("glaeda_gh", os.fspath(ROOT / "scripts" / "glaeda-gh"))
spec = importlib.util.spec_from_loader("glaeda_gh", loader)
gg = importlib.util.module_from_spec(spec)
sys.modules["glaeda_gh"] = gg
loader.exec_module(gg)

TOKEN = "ghp_faketoken_must_never_touch_disk"
NOW = float(int(time.time()))  # watch files carry real mtimes


def pr_node(number: int, state: str = "OPEN", rollup: str | None = "PENDING", checks: list | None = None) -> dict:
    return {
        "number": number, "url": f"https://github.com/o/r/pull/{number}", "title": "t", "state": state,
        "isDraft": False, "merged": state == "MERGED", "mergedAt": None, "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED", "headRefOid": "a" * 40, "reviewDecision": None, "updatedAt": "x",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": None if rollup is None else {
            "state": rollup,
            "contexts": {"totalCount": len(checks or []), "pageInfo": {"hasNextPage": False}, "nodes": checks or []},
        }}}]},
    }


def check_run(name: str, status: str = "COMPLETED", conclusion: str | None = "SUCCESS") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "detailsUrl": f"https://ci/{name}"}


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def job(name: str, conclusion: str = "failure", run_attempt: int = 1, runner: str = "cmuxs-mac-mini-3-glaeda-1",
        labels: tuple = ("glaeda-root-std-xcode-26.6",), failed_step: str = "Run selected tests",
        seconds: int = 300, end: float | None = None) -> dict:
    """A REST job; a failed one fails at `failed_step`, `seconds` after it started."""
    end = time.time() if end is None else end
    steps = [{"name": "Set up job", "conclusion": "success"}]
    if conclusion == "failure":
        steps.append({"name": failed_step, "conclusion": "failure"})
    return {"id": abs(hash(name)) % 1000, "name": name, "status": "completed", "conclusion": conclusion,
            "html_url": f"https://ci/job/{name}", "run_attempt": run_attempt, "runner_name": runner,
            "labels": list(labels), "started_at": iso(end - seconds), "completed_at": iso(end), "steps": steps}


def cached(*jobs: dict) -> dict:
    """Jobs as the daemon caches them."""
    return {"jobs": gg.slim_jobs({"jobs": list(jobs)})}


def refused(name: str = "build", **kw) -> dict:
    """What glaeda's runner hook leaves when it refuses a job: Set up runner failed within seconds."""
    return job(name, failed_step="Set up runner", seconds=7, **kw)


class FakeGitHub:
    """Answers GraphQL and REST like api.github.com; records every request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.prs: dict[tuple[str, str, int], dict] = {}
        self.runs: dict[int, dict] = {}
        self.jobs: dict[int, list] = {}
        self.issues: dict[tuple[str, str, int], dict] = {}
        self.steps: dict[str, list] = {}  # check run node id -> steps
        self.step_queries: list[list[str]] = []
        self.rest_remaining = 4000
        self.graphql_remaining = 4000
        self.rules: list[dict] = []  # GET .../rules/branches/B
        self.protected: list[str] = []  # the classic protection's required contexts
        self.job_steps: dict[int, list] = {}  # job (check run) database id -> steps, for GET .../actions/jobs/ID
        self.base_gone = False  # the base branch was deleted: its rules and protection answer 404
        self.heads: dict[tuple[str, str, str], list] = {}  # (owner, repo, sha) -> that commit's check contexts

    def rate(self, resource: str, remaining: int) -> dict:
        return {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": str(remaining),
                "x-ratelimit-reset": str(int(NOW + 1800)), "x-ratelimit-resource": resource}

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
        self.calls.append((method, url, dict(headers), body))
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        if url.endswith("/graphql"):
            doc = json.loads(body)
            v, data, errors = doc["variables"], {}, []
            self.graphql_remaining -= 1
            data["rateLimit"] = {"limit": 5000, "remaining": self.graphql_remaining, "resetAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 1800)), "cost": 1}
            if "nodes(ids:" in doc["query"]:
                self.step_queries.append(list(v["ids"]))
                data["nodes"] = [{"id": i, "steps": {"nodes": self.steps[i]}} if i in self.steps else None
                                 for i in v["ids"]]
            i = 0
            while f"u{i}" in v:  # runs by URL
                run = self.runs.get(int(v[f"u{i}"].rsplit("/", 1)[1]))
                data[f"w{i}"] = None if run is None else {
                    "__typename": "WorkflowRun", "databaseId": run["id"], "runAttempt": run.get("run_attempt"),
                    "event": run.get("event"), "createdAt": run.get("created_at"), "updatedAt": run.get("updated_at"),
                    "url": run.get("html_url"), "displayTitle": run.get("display_title"),
                    "workflow": {"name": run.get("name")},
                    "checkSuite": {"status": (run.get("status") or "").upper() or None,
                                   "conclusion": (run.get("conclusion") or "").upper() or None,
                                   "commit": {"oid": run.get("head_sha")},
                                   "branch": {"name": run["head_branch"]} if run.get("head_branch") else None}}
                i += 1
            if "object(oid:" in doc["query"]:
                for m in re.finditer(r'(?:h(\d+): )?repository\(owner:"([^"]+)",name:"([^"]+)"\)\{object\(oid:"([0-9a-f]+)"\)'
                                     r'\{\.\.\. on Commit\{statusCheckRollup\{contexts\(first:100(?:, after:"(\d+)")?\)', doc["query"]):
                    alias, owner, repo, sha, after = m.groups()
                    nodes = self.heads.get((owner, repo, sha))
                    start = int(after or 0)
                    node = None if nodes is None else {"statusCheckRollup": {"contexts": {
                        "pageInfo": {"hasNextPage": start + 100 < len(nodes), "endCursor": str(start + 100)},
                        "nodes": nodes[start:start + 100]}}}
                    data["repository" if alias is None else f"h{alias}"] = {"object": node} if alias is not None else {
                        "object": node}
                out = {"data": data}
                return 200, self.rate("graphql", self.graphql_remaining), json.dumps(out).encode()
            comments = "issueOrPullRequest" in doc["query"]
            alias, field, table = ("c", "issueOrPullRequest", self.issues) if comments else ("p", "pullRequest", self.prs)
            i = 0
            while f"o{i}" in v:
                node = table.get((v[f"o{i}"], v[f"r{i}"], v[f"n{i}"]))
                data[f"{alias}{i}"] = {field: node}
                if node is None:
                    errors.append({"type": "NOT_FOUND", "path": [f"{alias}{i}", field], "message": "Could not resolve"})
                i += 1
            out = {"data": data, **({"errors": errors} if errors else {})}
            return 200, self.rate("graphql", self.graphql_remaining), json.dumps(out).encode()
        path = url.split("api.github.com", 1)[1]
        parts = path.split("?")[0].split("/")
        if parts[4] == "actions" and parts[5] == "jobs":
            steps = self.job_steps.get(int(parts[6]))
            payload = None if steps is None else {"id": int(parts[6]), "steps": steps}
            if payload is None:
                self.rest_remaining -= 1
                return 404, self.rate("core", self.rest_remaining), b'{"message": "Not Found"}'
        elif parts[4] == "actions" and len(parts) > 6:
            run_id = int(parts[6])
            payload = {"jobs": self.jobs.get(run_id, [])} if parts[-1] == "jobs" else self.runs[run_id]
        else:
            payload = self.rest_pr(parts, path)
            if payload is None:
                self.rest_remaining -= 1
                return 404, self.rate("core", self.rest_remaining), b'{"message": "Not Found"}'
        etag = '"' + str(abs(hash(json.dumps(payload, sort_keys=True)))) + '"'
        if headers.get("If-None-Match") == etag:
            return 304, self.rate("core", self.rest_remaining), b""
        self.rest_remaining -= 1
        return 200, {**self.rate("core", self.rest_remaining), "etag": etag}, json.dumps(payload).encode()

    def rest_pr(self, parts: list[str], path: str) -> Any:
        """The REST view of the GraphQL PR nodes in self.prs: pulls, check runs, statuses, a head's
        workflow runs, and the base branch's rules and protection."""
        owner, repo, what = parts[2], parts[3], parts[4]
        if what == "pulls":
            node = self.prs.get((owner, repo, int(parts[5])))
            if node is None:
                return None
            state = "open" if node["state"] == "OPEN" else "closed"
            return {"number": node["number"], "html_url": node["url"], "title": node["title"], "state": state,
                    "draft": node["isDraft"], "merged": node["merged"], "merged_at": node["mergedAt"],
                    "mergeable": {"MERGEABLE": True, "CONFLICTING": False}.get(node["mergeable"]),
                    "mergeable_state": node["mergeStateStatus"].lower(), "updated_at": node["updatedAt"],
                    "head": {"sha": node["headRefOid"], "ref": node.get("headRefName")}, "base": {"ref": "main"}}
        if what in {"rules", "branches"} and self.base_gone:
            return None
        if what == "rules":
            return self.rules
        if what == "actions" and parts[5] == "workflows":
            return {"workflows": [{"id": 77, "name": "CI"}, {"id": 78, "name": "Lint"}]}
        if what == "branches":
            return {"name": parts[5], "protection": {"required_status_checks": {"contexts": self.protected}}}
        sha = parts[5] if what == "commits" else path.split("head_sha=", 1)[1].split("&")[0]
        contexts = []
        for node in self.prs.values():
            if node["headRefOid"] == sha:
                contexts = ((node["commits"]["nodes"][0]["commit"]["statusCheckRollup"] or {}).get("contexts") or {}).get("nodes") or []
        runs = {c["checkSuite"]["workflowRun"]["databaseId"]: c for c in contexts
                if ((c.get("checkSuite") or {}).get("workflowRun") or {}).get("databaseId")}
        if what == "actions":
            return {"workflow_runs": [{"id": rid, "name": "run-name " + str(rid),  # REST: the run-name
                                       "workflow_id": {"CI": 77, "Lint": 78}.get(c["checkSuite"]["workflowRun"]["workflow"]["name"]),
                                       "check_suite_id": rid, "conclusion": (c["checkSuite"].get("conclusion") or "").lower() or None}
                                      for rid, c in runs.items()]}
        if parts[-1] == "status":
            return {"statuses": [{"context": c["context"], "state": c["state"].lower(), "target_url": c.get("targetUrl"),
                                  "created_at": c.get("createdAt")} for c in contexts if c["__typename"] == "StatusContext"]}
        checks = [c for c in contexts if c["__typename"] == "CheckRun"]
        page = int(path.split("page=")[-1]) if "&page=" in path else 1
        checks_page = checks[100 * (page - 1):100 * page]
        return {"total_count": len(checks), "check_runs": [
            {"id": c.get("databaseId"), "node_id": c.get("id"), "name": c["name"], "status": c["status"].lower(),
             "conclusion": (c.get("conclusion") or "").lower() or None, "details_url": c.get("detailsUrl"),
             "started_at": c.get("startedAt"), "completed_at": c.get("completedAt"),
             "check_suite": {"id": ((c.get("checkSuite") or {}).get("workflowRun") or {}).get("databaseId")}}
            for c in checks_page]}

    def rest_calls(self) -> list:
        return [c for c in self.calls if not c[1].endswith("/graphql")]

    def gql_calls(self) -> list:
        return [c for c in self.calls if c[1].endswith("/graphql")]

    def run_calls(self) -> list:
        return [c for c in self.rest_calls() if "/jobs" not in c[1]]

    def job_calls(self) -> list:
        return [c for c in self.rest_calls() if "/jobs" in c[1]]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        # watch files carry real mtimes, so the fake clock starts at each test, not at import:
        # a test reached 20 s into the suite saw its registrations as newer than NOW + 10 ticks
        global NOW
        NOW = float(int(time.time()))
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (gg.BASE, gg.WAIT_POLL, gg.DOWN_GRACE, gg.DOWN_PATIENCE)
        gg.BASE = Path(self.tmp.name) / "gh"
        gg.WAIT_POLL = 0.05
        gg.DOWN_GRACE = 0.3
        gg.DOWN_PATIENCE = 0.6
        self.gh = FakeGitHub()
        self.daemon = gg.Daemon(transport=self.gh, token=lambda: TOKEN, controller=None)

    def tearDown(self) -> None:
        gg.BASE, gg.WAIT_POLL, gg.DOWN_GRACE, gg.DOWN_PATIENCE = self.saved
        self.tmp.cleanup()

    def age_watch(self, key: str, seconds: float) -> None:
        path = gg.watch_dir() / gg.file_name(key)
        os.utime(path, (NOW - seconds, NOW - seconds))


class FakeController:
    """Stands in for the cmux build controller: /v1/github/runs/<id> and the change feed. A change
    to `runs` shows up in the feed, as a webhook delivery would; suites and comments are queued."""

    def __init__(self) -> None:
        self.runs: dict[int, dict] = {}
        self.suites: list[dict] = []
        self.comments: list[dict] = []
        self.calls: list[int] = []  # run() reads
        self.feeds: list[int | None] = []  # events() reads, by cursor
        self.down = False
        self.token = "t"
        self.seq = 0
        self.sent: dict[int, dict] = {}

    def fail(self) -> None:
        if self.down:
            raise self.down if isinstance(self.down, gg.WatchError) else gg.WatchError("controller: URLError")

    def run(self, run_id: int) -> dict | None:
        self.calls.append(run_id)
        self.fail()
        return self.runs.get(run_id)

    def events(self, since: int | None) -> dict:
        self.feeds.append(since)
        self.fail()
        changed = [dict(r) for i, r in self.runs.items() if self.sent.get(i) != r]
        self.sent = {i: dict(r) for i, r in self.runs.items()}
        page = {"cursor": self.seq, "more": False, "runs": [], "suites": [], "comments": [],
                "receiver_last_delivery": "2026-09-25T10:00:00Z"}
        if since is None:  # the start: only the cursor
            self.suites, self.comments = [], []
            return page
        self.seq += len(changed) + len(self.suites) + len(self.comments)
        page.update(cursor=self.seq, runs=changed, suites=self.suites, comments=self.comments)
        self.suites, self.comments = [], []
        return page


def ctl_run(run_id: int, status: str, conclusion: str = "", updated: str = "2026-09-25T10:00:00Z", repo: str = "o/r") -> dict:
    return {"id": run_id, "repo": repo, "name": "seed", "event": "workflow_dispatch", "head_sha": "a" * 40,
            "attempt": 1, "status": status, "conclusion": conclusion, "url": f"https://x/{run_id}", "updated_at": updated}


class ControllerTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.ctl = FakeController()
        feed_every, gg.CONTROLLER_FEED_EVERY = gg.CONTROLLER_FEED_EVERY, 0  # these tests read the feed each tick
        self.addCleanup(setattr, gg, "CONTROLLER_FEED_EVERY", feed_every)
        self.daemon = gg.Daemon(transport=self.gh, token=lambda: TOKEN, controller=self.ctl)

    def test_tracked_run_costs_no_quota(self) -> None:
        self.ctl.runs[21] = ctl_run(21, "in_progress")
        key = gg.parse_key("run", "o/r/21")
        gg.register(key)
        for i in range(5):
            self.daemon.tick(NOW + i * gg.CYCLE)
        self.assertEqual(self.gh.rest_calls(), [])
        self.ctl.runs[21] = ctl_run(21, "completed", "failure", "2026-09-25T10:05:00Z")
        self.gh.runs[21] = {"id": 21, "status": "completed", "conclusion": "failure", "run_attempt": 1,
                            "updated_at": "2026-09-25T10:05:00Z"}
        self.gh.jobs[21] = [job("build", end=NOW)]
        self.daemon.tick(NOW + 5 * gg.CYCLE + gg.TICK)
        entry = gg.read_cache(key)
        self.assertEqual((entry["source"], entry["data"]["conclusion"]), ("controller", "failure"))
        self.assertEqual(gg.verdict("run", entry["data"], jobs=entry["jobs"]), gg.EXIT_FAIL)
        # the failed attempt's job list is read once, for its failed steps; nothing after that
        self.assertEqual((len(self.gh.run_calls()), len(self.gh.job_calls())), (1, 1))
        self.assertEqual(entry["jobs"][0]["failedSteps"], ["Run selected tests"])
        for i in range(1, 4):
            self.daemon.tick(NOW + 5 * gg.CYCLE + gg.TICK + i * gg.CYCLE)
        self.assertEqual(len(self.gh.rest_calls()), 2)

    def test_refusal_reported_by_the_controller_is_held_until_the_rescue_attempt(self) -> None:
        now = time.time()
        self.ctl.runs[31] = ctl_run(31, "completed", "failure", iso(now))
        self.gh.runs[31] = {"id": 31, "status": "completed", "conclusion": "failure", "run_attempt": 1,
                            "updated_at": iso(now)}
        self.gh.jobs[31] = [refused(end=now)]
        key = "run:o/r/31"
        gg.register(key)
        self.age_watch(key, 1)  # the reader came before the first fetch; a later one would get a REST confirmation
        self.daemon.tick(NOW)
        entry = gg.read_cache(key)
        self.assertIsNone(gg.verdict("run", entry["data"], jobs=entry["jobs"]))  # held for the rescue
        self.ctl.runs[31] = {**ctl_run(31, "in_progress", "", iso(now + 50)), "attempt": 2}
        self.daemon.tick(NOW + gg.TICK)
        entry = gg.read_cache(key)
        self.assertEqual((entry["data"]["run_attempt"], entry["data"]["status"]), (2, "in_progress"))
        self.ctl.runs[31] = {**ctl_run(31, "completed", "success", iso(now + 600)), "attempt": 2}
        self.daemon.tick(NOW + 2 * gg.TICK)
        entry = gg.read_cache(key)
        self.assertEqual(gg.verdict("run", entry["data"], jobs=entry.get("jobs")), gg.EXIT_OK)
        self.assertEqual(len(self.gh.rest_calls()), 2)  # only attempt 1's run and job list

    def test_safety_net_reads_rest_every_15_minutes(self) -> None:
        self.ctl.runs[22] = ctl_run(22, "in_progress")
        self.gh.runs[22] = {"id": 22, "status": "in_progress", "updated_at": "2026-09-25T10:00:00Z"}
        gg.register("run:o/r/22")
        self.daemon.tick(NOW)
        self.daemon.tick(NOW + gg.REST_SAFETY - gg.CYCLE)
        self.assertEqual(len(self.gh.rest_calls()), 0)
        # the waiting reader keeps touching its watch; without that it expires at IDLE_EXPIRY,
        # which equals REST_SAFETY (the test passed only while NOW trailed the registration)
        self.age_watch(gg.parse_key("run", "o/r/22"), -(gg.REST_SAFETY - gg.CYCLE))
        self.daemon.tick(NOW + gg.REST_SAFETY + 1)
        self.assertEqual(len(self.gh.rest_calls()), 1)

    def test_lost_delivery_is_not_undone(self) -> None:
        self.ctl.runs[23] = ctl_run(23, "in_progress")
        self.gh.runs[23] = {"id": 23, "status": "completed", "conclusion": "success", "updated_at": "2026-09-25T10:09:00Z"}
        key = gg.parse_key("run", "o/r/23")
        gg.register(key)
        self.daemon.tick(NOW)
        self.age_watch(key, -(gg.REST_SAFETY - gg.CYCLE))  # a reader is still waiting (see above)
        self.daemon.tick(NOW + gg.REST_SAFETY + 1)
        self.daemon.tick(NOW + gg.REST_SAFETY + 1 + gg.TICK)
        self.assertEqual(gg.read_cache(key)["data"]["conclusion"], "success")

    def test_unknown_run_uses_rest(self) -> None:
        self.gh.runs[24] = {"id": 24, "status": "completed", "conclusion": "success"}
        gg.register("run:o/r/24")
        self.daemon.tick(NOW)
        self.assertEqual(len(self.gh.rest_calls()), 1)
        self.assertEqual(gg.read_cache("run:o/r/24")["source"], "rest")

    def test_daemon_follows_a_moved_controller(self) -> None:
        """The client config names the controller. A daemon started before a move kept reading
        the old, drained controller's feed and reported webhooks hours stale (cmuxterm-hq, 2026-09-28)."""
        old, new = FakeController(), FakeController()
        old.base, new.base = "http://old:18765", "http://new:18765"
        current = [old]
        self.daemon = gg.Daemon(transport=self.gh, token=lambda: TOKEN, controller=lambda: current[0])
        self.daemon.tick(NOW)
        self.daemon.tick(NOW + gg.TICK)
        self.assertEqual(old.feeds, [None, 0])
        current[0] = new
        self.daemon.tick(NOW + 2 * gg.TICK)
        self.assertIs(self.daemon.controller, new)
        self.assertEqual(new.feeds, [None])  # a fresh cursor: the old controller's means nothing here
        self.assertEqual(old.feeds, [None, 0])

    def test_controller_source_that_raises_does_not_stop_the_tick(self) -> None:
        """A source that raises while no controller is set reached controller_down with None."""
        def broken():
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        self.daemon = gg.Daemon(transport=self.gh, token=lambda: TOKEN, controller=lambda: None)
        self.daemon.controller_source = broken
        self.daemon.tick(NOW)
        self.assertEqual(self.daemon.controller_status["state"], "down")

    def test_token_file_that_is_not_utf8_reads_as_no_token(self) -> None:
        path = Path(self.tmp.name) / "token"
        path.write_bytes(b"\xff\xfe")
        saved = dict(os.environ)
        os.environ.update(CMUX_CI_CONTROLLER="http://c:18765", CMUX_CI_TOKEN_FILE=str(path))
        try:
            self.assertIsNone(gg.Controller.from_env().token)
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_controller_down_backs_off(self) -> None:
        self.ctl.down = True
        self.gh.runs[25] = {"id": 25, "status": "in_progress"}
        gg.register("run:o/r/25")
        self.daemon.tick(NOW)
        self.daemon.tick(NOW + gg.TICK)
        self.assertEqual(len(self.ctl.feeds), 1)
        self.assertEqual(len(self.gh.rest_calls()), 1)
        self.assertEqual(self.daemon.controller_status["state"], "down")
        self.daemon.tick(NOW + gg.CONTROLLER_RETRY + 1)  # still down: the wait doubles
        self.assertEqual(len(self.ctl.feeds), 2)
        self.daemon.tick(NOW + 2 * gg.CONTROLLER_RETRY + 2)
        self.assertEqual(len(self.ctl.feeds), 2)
        self.ctl.down = False
        self.daemon.tick(NOW + 3 * gg.CONTROLLER_RETRY + 2)
        self.assertEqual(len(self.ctl.feeds), 3)
        self.assertEqual(self.daemon.controller_status["state"], "ok")
        self.assertEqual(self.daemon.controller_failures, 0)

    def test_controller_fault_does_not_stop_the_tick(self) -> None:
        """A bug in the optional controller path (glaeda#1305: an AttributeError on every
        unreachable-controller read) aborted each tick, so GitHub was never polled either."""
        def broken(since: int | None) -> dict:
            raise AttributeError("'function' object has no attribute 'client'")
        self.ctl.events = broken
        self.gh.runs[28] = {"id": 28, "status": "in_progress"}
        gg.register("run:o/r/28")
        status = self.daemon.tick(NOW)
        self.assertEqual(gg.read_cache("run:o/r/28")["data"]["status"], "in_progress")  # REST still served it
        self.assertEqual(self.daemon.controller_status["state"], "down")
        # a bug, not an expected WatchError: it names where it was raised, like a tick's error
        self.assertRegex(self.daemon.controller_status["error"], r"^AttributeError at test-glaeda-gh\.py:\d+ broken$")
        self.assertIn(self.daemon.controller_status["error"], status["errors"])
        self.assertEqual(gg.read_json(gg.BASE / "daemon.json")["at"], NOW)  # and the heartbeat was written

    def test_unreachable_real_controller_does_not_stop_the_tick(self) -> None:
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]  # closed once the with ends: connections are refused
        self.daemon = gg.Daemon(transport=self.gh, token=lambda: TOKEN,
                                controller=gg.Controller(f"http://127.0.0.1:{port}", None))
        self.daemon.tick(NOW)  # raised AttributeError here before glaeda#1305
        self.assertEqual(self.daemon.controller_status["state"], "down")
        self.assertNotIn(" at ", self.daemon.controller_status["error"])  # an expected WatchError: no site

    def test_job_lists_and_other_repos_stay_on_rest(self) -> None:
        self.ctl.runs[26] = ctl_run(26, "in_progress", repo="other/repo")
        self.gh.runs[26] = {"id": 26, "status": "in_progress"}
        gg.register("run:o/r/26")
        self.gh.runs[27] = {"id": 27, "status": "in_progress"}
        self.ctl.runs[27] = ctl_run(27, "in_progress")
        gg.register("run:o/r/27", jobs=True)
        self.daemon.tick(NOW)
        self.assertEqual(self.ctl.calls, [26])
        self.assertEqual(gg.read_cache("run:o/r/26")["source"], "rest")
        self.assertEqual(gg.read_cache("run:o/r/27")["source"], "rest")

    def test_new_reader_of_a_finished_run_gets_one_rest_confirmation(self) -> None:
        self.ctl.runs[28] = ctl_run(28, "completed", "success")
        self.gh.runs[28] = {"id": 28, "status": "completed", "conclusion": "success", "updated_at": "2026-09-25T10:00:00Z"}
        key = "run:o/r/28"
        gg.register(key)
        self.age_watch(key, 200)
        self.daemon.tick(NOW - 100)
        self.assertEqual(self.gh.rest_calls(), [])
        stale = gg.read_cache(key)["dataAt"]
        self.daemon.tick(NOW - 98)
        self.assertEqual(gg.read_cache(key)["dataAt"], stale, "unchanged controller data keeps its time")
        self.age_watch(key, 0)  # a new reader
        self.daemon.tick(NOW)
        self.assertEqual(len(self.gh.rest_calls()), 1)
        self.assertEqual(gg.read_cache(key)["dataAt"], NOW)
        self.daemon.tick(NOW + gg.CYCLE)
        self.assertEqual(len(self.gh.rest_calls()), 1, "confirmed once, not every cycle")

    def test_rerun_before_its_webhook_is_not_answered_with_the_old_attempt(self) -> None:
        self.ctl.runs[29] = ctl_run(29, "completed", "failure")
        self.gh.runs[29] = {"id": 29, "status": "queued", "conclusion": None, "run_attempt": 2,
                            "updated_at": "2026-09-25T10:30:00Z"}
        key = "run:o/r/29"
        gg.register(key)
        self.age_watch(key, 200)
        self.daemon.tick(NOW - 100)
        self.age_watch(key, 0)
        self.daemon.tick(NOW)
        entry = gg.read_cache(key)
        self.assertEqual((entry["source"], entry["data"]["status"]), ("rest", "queued"))
        self.daemon.tick(NOW + gg.TICK)  # the controller has not heard of the rerun yet
        self.assertEqual(gg.read_cache(key)["data"]["status"], "queued")
        self.ctl.runs[29] = {**ctl_run(29, "completed", "success", "2026-09-25T10:40:00Z"), "attempt": 2}
        self.daemon.tick(NOW + 2 * gg.TICK)
        entry = gg.read_cache(key)
        self.assertEqual((entry["source"], entry["data"]["conclusion"], entry["data"]["run_attempt"]), ("controller", "success", 2))

    def test_controller_down_after_tracking_resumes_rest(self) -> None:
        self.ctl.runs[30] = ctl_run(30, "in_progress")
        self.gh.runs[30] = {"id": 30, "status": "in_progress"}
        gg.register("run:o/r/30")
        self.daemon.tick(NOW)
        self.assertEqual(self.gh.rest_calls(), [])
        self.ctl.down = True
        self.daemon.tick(NOW + gg.CYCLE)
        self.assertEqual(len(self.gh.rest_calls()), 1, "no 15-minute blackout while the controller is down")

    def test_controller_without_the_receiver_is_quiet(self) -> None:
        self.ctl.down = gg.NoReceiver("controller HTTP 404")
        self.gh.runs[32] = {"id": 32, "status": "in_progress"}
        gg.register("run:o/r/32")
        status = self.daemon.tick(NOW)
        self.assertEqual(status["errors"], [])
        self.assertEqual(len(self.gh.rest_calls()), 1)
        self.daemon.tick(NOW + gg.CONTROLLER_RETRY + 1)
        self.assertEqual(len(self.ctl.feeds), 1)  # not asked again for 15 minutes
        self.daemon.tick(NOW + gg.CONTROLLER_ABSENT + 1)
        self.assertEqual(len(self.ctl.feeds), 2)

    def test_one_feed_read_a_tick_after_the_baseline(self) -> None:
        for i in (40, 41, 42):
            self.ctl.runs[i] = ctl_run(i, "in_progress")
            gg.register(f"run:o/r/{i}")
        for t in range(10):
            self.daemon.tick(NOW + t * gg.TICK)
        self.assertEqual(sorted(self.ctl.calls), [40, 41, 42])  # one baseline each, then only the feed
        self.assertEqual(len(self.ctl.feeds), 10)
        self.ctl.runs[41] = ctl_run(41, "completed", "success", "2026-09-25T10:03:00Z")
        self.daemon.tick(NOW + 10 * gg.TICK)
        self.assertEqual(gg.read_cache("run:o/r/41")["data"]["conclusion"], "success")
        self.assertEqual(len(self.ctl.calls), 3)
        self.assertEqual(self.gh.rest_calls(), [])
        self.assertEqual(self.daemon.tick(NOW + 11 * gg.TICK)["controller"]["runs"], 3)

    def test_feed_is_read_at_most_every_feed_interval(self) -> None:
        gg.CONTROLLER_FEED_EVERY = 20  # one read per tick from 18 hosts overloaded the controller's whois (10-07)
        self.ctl.runs[44] = ctl_run(44, "in_progress")
        gg.register("run:o/r/44")
        for t in range(20):
            self.daemon.tick(NOW + t * gg.TICK)
        self.assertEqual(len(self.ctl.feeds), 2)  # at 0 s and 20 s
        self.assertEqual(self.ctl.calls, [44])
        self.assertEqual(self.gh.rest_calls(), [])  # the run stays controller-filled between reads
        self.ctl.runs[45] = ctl_run(45, "in_progress")
        gg.register("run:o/r/45")
        self.daemon.tick(NOW + 20 * gg.TICK)
        self.assertEqual(self.ctl.calls, [44, 45])  # a new watch gets its baseline without waiting for the feed
        self.assertEqual(self.gh.rest_calls(), [])

    def test_feed_reset_takes_a_new_baseline(self) -> None:
        self.ctl.runs[43] = ctl_run(43, "in_progress")
        gg.register("run:o/r/43")
        self.daemon.tick(NOW)
        self.daemon.tick(NOW + gg.TICK)
        self.assertEqual(self.ctl.calls, [43])
        real = self.ctl.events
        self.ctl.events = lambda since: {**real(since), "reset": True, "cursor": 0}
        self.daemon.tick(NOW + 2 * gg.TICK)  # the controller lost its state
        self.ctl.events = real
        self.daemon.tick(NOW + 3 * gg.TICK)
        self.assertEqual(self.ctl.calls, [43, 43])

    def test_check_events_nudge_a_watched_pr(self) -> None:
        node = pr_node(1)
        node["headRefName"] = "feat/x"
        self.gh.prs[("o", "r", 1)] = node
        gg.register("pr:o/r#1")
        self.daemon.tick(NOW)
        self.assertEqual(len(self.gh.gql_calls()), 1)
        self.daemon.tick(NOW + gg.NUDGE_MIN)  # nothing happened: no read before the cycle
        self.assertEqual(len(self.gh.gql_calls()), 1)
        self.ctl.suites.append({"id": 1, "repo": "o/r", "head_sha": "a" * 40, "status": "completed"})
        self.daemon.tick(NOW + gg.NUDGE_MIN + gg.TICK)
        self.assertEqual(len(self.gh.gql_calls()), 2)  # re-read at once
        # a push: runs for a new head on the PR's branch nudge it too, but not more than every NUDGE_MIN
        self.ctl.runs[50] = {**ctl_run(50, "queued"), "head_sha": "c" * 40, "head_branch": "feat/x"}
        self.daemon.tick(NOW + gg.NUDGE_MIN + 2 * gg.TICK)
        self.assertEqual(len(self.gh.gql_calls()), 2)
        self.daemon.tick(NOW + 2 * gg.NUDGE_MIN + gg.TICK)
        self.assertEqual(len(self.gh.gql_calls()), 3)
        self.ctl.suites.append({"id": 2, "repo": "o/other", "head_sha": "a" * 40, "status": "completed"})
        self.daemon.tick(NOW + 2 * gg.NUDGE_MIN + 3 * gg.TICK)  # still inside the 60 s cycle
        self.assertEqual(len(self.gh.gql_calls()), 3)  # another repository's commit

    def test_comments_from_the_feed_reach_a_comment_wait(self) -> None:
        self.gh.issues[("t", "s", 454)] = {"__typename": "Issue", "url": "u", "state": "OPEN",
                                           "comments": {"totalCount": 0, "nodes": []}}
        gg.register("comment:t/s#454")
        self.daemon.tick(NOW)
        self.ctl.comments.append({"id": 77, "repo": "t/s", "number": 454, "author": "github-actions[bot]",
                                  "body": "callsign-receipt/v0\nstatus: accepted", "url": "u77",
                                  "created_at": iso(NOW + 1)})
        self.ctl.comments.append({"id": 78, "repo": "t/other", "number": 454, "author": "x", "body": "y"})
        self.daemon.tick(NOW + gg.TICK)  # before the next GraphQL read
        self.assertEqual(len(self.gh.gql_calls()), 1)
        comments = gg.read_cache("comment:t/s#454")["data"]["comments"]
        self.assertEqual([c["id"] for c in comments], [77])
        self.assertTrue(gg.comment_matches(comments[0], "github-actions", re.compile("accepted"), None, NOW))
        self.ctl.comments.append({"id": 77, "repo": "t/s", "number": 454, "deleted": True})
        self.daemon.tick(NOW + 2 * gg.TICK)
        self.assertEqual(gg.read_cache("comment:t/s#454")["data"]["comments"], [])

    def test_budget_names_the_controller_state(self) -> None:
        self.assertIn("webhook events ok, 2 watched runs", gg.controller_line(
            {"state": "ok", "runs": 2, "lastDelivery": iso(time.time() - 5), "token": True}))
        self.assertIn("public repos only", gg.controller_line({"state": "ok", "token": False}))
        self.assertIn("unreachable (controller: URLError); polling GitHub", gg.controller_line(
            {"state": "down", "error": "controller: URLError", "retryAt": time.time() + 60}))
        self.assertIn("no run-events receiver", gg.controller_line({"state": "absent", "retryAt": 0}))
        self.assertIn("none configured", gg.controller_line({"state": "off"}))

    def test_controller_works_without_a_token(self) -> None:
        saved = {k: os.environ.get(k) for k in ("CMUX_CI_TOKEN_FILE", "CMUX_CI_CONTROLLER")}
        os.environ["CMUX_CI_TOKEN_FILE"] = "/nonexistent/token"
        os.environ["CMUX_CI_CONTROLLER"] = "http://controller.test:18765/"
        try:
            ctl = gg.Controller.from_env()
            self.assertEqual((ctl.base, ctl.token), ("http://controller.test:18765", None))  # public repos only
            os.environ["CMUX_CI_CONTROLLER"] = "ftp://nope"
            self.assertIsNone(gg.Controller.from_env())
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class TransportErrorTest(unittest.TestCase):
    """glaeda-gh's http() helper shadows the `http` package. An except clause naming
    http.client raised AttributeError on the first connection failure and aborted every tick."""

    @staticmethod
    def closed_port() -> int:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            return server.getsockname()[1]

    def test_unreachable_controller_is_a_watch_error(self) -> None:
        ctl = gg.Controller(f"http://127.0.0.1:{self.closed_port()}", None)
        with self.assertRaises(gg.WatchError):
            ctl.events(None)

    def test_unreachable_github_is_a_watch_error(self) -> None:
        api = f"http://127.0.0.1:{self.closed_port()}"
        with mock.patch.object(gg, "API", api), self.assertRaises(gg.WatchError):
            gg.http("GET", api + "/rate_limit", {})


class DaemonTest(Base):
    def test_run_etag_304_reuses_cached_body(self) -> None:
        self.gh.runs[7] = {"id": 7, "name": "CI", "status": "in_progress", "conclusion": None, "html_url": "u"}
        key = gg.parse_key("run", "O/R/7")
        gg.register(key)
        self.daemon.tick(NOW)
        first = gg.read_cache(key)
        self.assertEqual(first["data"]["status"], "in_progress")
        self.assertTrue(first["etag"])
        self.daemon.tick(NOW + gg.CYCLE)
        calls = self.gh.rest_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][2]["If-None-Match"], first["etag"])
        second = gg.read_cache(key)
        self.assertEqual(second["data"], first["data"])
        self.assertEqual(second["etag"], first["etag"])
        self.assertEqual(self.gh.rest_remaining, 3999)  # the 304 cost nothing
        self.assertEqual(self.daemon.budget["rest"]["remaining"], 3999)
        # a change comes through as fresh data
        self.gh.runs[7] = {**self.gh.runs[7], "status": "completed", "conclusion": "success"}
        self.daemon.tick(NOW + 2 * gg.CYCLE)
        self.assertEqual(gg.read_cache(key)["data"]["conclusion"], "success")

    def test_jobs_only_when_asked(self) -> None:
        self.gh.runs[8] = {"id": 8, "status": "in_progress"}
        self.gh.jobs[8] = [{"id": 1, "name": "build", "status": "completed", "conclusion": "failure", "html_url": "j"}]
        key = gg.parse_key("run", "https://github.com/o/r/actions/runs/8/job/99")
        gg.register(key)
        self.daemon.tick(NOW)
        self.assertFalse(any("/jobs" in c[1] for c in self.gh.calls))
        gg.register(key, jobs=True)
        gg.register(key)  # another reader without --jobs does not turn it off
        self.daemon.tick(NOW + gg.CYCLE)
        self.assertEqual(gg.read_cache(key)["jobs"][0]["name"], "build")

    def test_batched_graphql_query_and_parse(self) -> None:
        keys = [gg.parse_key("pr", "o/r#1"), gg.parse_key("pr", "https://github.com/O/R/pull/2"), gg.parse_key("pr", "o/r#3")]
        query, variables = gg.pr_query(keys)
        self.assertIn("p0: repository(owner: $o0, name: $r0)", query)
        self.assertIn("p2: repository(owner: $o2", query)
        self.assertIn("rateLimit", query)
        self.assertEqual(variables, {"o0": "o", "r0": "r", "n0": 1, "o1": "o", "r1": "r", "n1": 2,
                                     "o2": "o", "r2": "r", "n2": 3})
        status_ctx = {"__typename": "StatusContext", "context": "legacy", "state": "ERROR", "targetUrl": "s"}
        self.gh.prs[("o", "r", 1)] = pr_node(1, rollup="FAILURE", checks=[check_run("a"), check_run("b", conclusion="FAILURE"), status_ctx])
        self.gh.prs[("o", "r", 2)] = pr_node(2, state="MERGED", rollup="SUCCESS", checks=[check_run("a")])
        for key in keys:
            gg.register(key)
        self.daemon.tick(NOW)
        self.assertEqual(len(self.gh.gql_calls()), 1)  # one query for all three
        one = gg.read_cache(keys[0])["data"]
        self.assertEqual([c["name"] for c in gg.failing(one["checks"])], ["b", "legacy"])
        query_vars = json.loads(self.gh.gql_calls()[0][3])["query"]
        self.assertIn("isRequired(pullRequestNumber: $n2)", query_vars)
        self.assertEqual(one["checksTotal"], 3)
        self.assertEqual(gg.read_cache(keys[1])["data"]["state"], "MERGED")
        missing = gg.read_cache(keys[2])
        self.assertIsNone(missing["data"])
        self.assertIn("Could not resolve", missing["error"])
        self.assertTrue(missing["missing"])
        self.assertEqual(self.daemon.budget["graphql"]["remaining"], 3999)

    def test_new_entry_fills_at_once_then_waits_for_the_cycle(self) -> None:
        self.gh.prs[("o", "r", 1)] = pr_node(1)
        self.gh.prs[("o", "r", 2)] = pr_node(2)
        gg.register("pr:o/r#1")
        self.daemon.tick(NOW)
        gg.register("pr:o/r#2")
        self.daemon.tick(NOW + gg.TICK)  # only the new PR is fetched
        self.assertEqual(len(self.gh.gql_calls()), 2)
        self.assertEqual(json.loads(self.gh.gql_calls()[1][3])["variables"], {"o0": "o", "r0": "r", "n0": 2})
        self.daemon.tick(NOW + 2 * gg.TICK)
        self.assertEqual(len(self.gh.gql_calls()), 2)
        self.daemon.tick(NOW + gg.CYCLE)
        self.assertEqual(len(self.gh.gql_calls()), 3)

    def test_interest_expiry(self) -> None:
        self.gh.prs[("o", "r", 1)] = pr_node(1)
        self.gh.prs[("o", "r", 2)] = pr_node(2, state="MERGED", rollup="SUCCESS")
        self.gh.prs[("o", "r", 3)] = pr_node(3)
        for n in (1, 2, 3):
            gg.register(f"pr:o/r#{n}")
        self.daemon.tick(NOW)
        self.age_watch("pr:o/r#1", gg.IDLE_EXPIRY + 1)  # nobody read it for 15 minutes
        self.age_watch("pr:o/r#2", gg.TERMINAL_GRACE + 1)  # merged, last reader gone
        self.age_watch("pr:o/r#3", gg.TERMINAL_GRACE + 1)  # open: still within the idle window
        status = self.daemon.tick(NOW + 1)
        self.assertEqual(status["watching"], 1)
        left = [gg.read_json(p)["key"] for p in gg.watch_dir().glob("*.json")]
        self.assertEqual(left, ["pr:o/r#3"])

    def test_terminal_entries_are_not_repolled_every_cycle(self) -> None:
        self.gh.prs[("o", "r", 2)] = pr_node(2, state="MERGED", rollup="SUCCESS")
        gg.register("pr:o/r#2")
        self.daemon.tick(NOW)
        for i in range(1, 5):
            gg.register("pr:o/r#2")
            self.age_watch("pr:o/r#2", 1)  # the reader was already there before the last fetch
            self.daemon.tick(NOW + i * gg.CYCLE)
        self.assertEqual(len(self.gh.gql_calls()), 1)

    def test_rerun_of_a_completed_run_is_seen_on_the_next_cycle(self) -> None:
        self.gh.runs[3] = {"id": 3, "status": "completed", "conclusion": "failure", "run_attempt": 1}
        self.gh.jobs[3] = [job("build", run_attempt=1)]
        gg.register("run:o/r/3")
        self.age_watch("run:o/r/3", 1)  # registered before the first fetch
        self.daemon.tick(NOW)
        self.daemon.tick(NOW + gg.CYCLE)  # nobody new is reading: a completed run is not re-read
        self.assertEqual(len(self.gh.run_calls()), 1)
        self.assertEqual(len(self.gh.job_calls()), 1)  # the failed attempt's steps, read once
        self.gh.runs[3] = {"id": 3, "status": "queued", "conclusion": None, "run_attempt": 2}  # gh run rerun 3
        path = gg.watch_dir() / gg.file_name("run:o/r/3")
        gg.register("run:o/r/3")  # a new wait
        os.utime(path, (NOW + gg.CYCLE + 1,) * 2)
        self.daemon.tick(NOW + 2 * gg.CYCLE)
        self.assertEqual(len(self.gh.run_calls()), 2)
        self.assertEqual(gg.read_cache("run:o/r/3")["data"]["status"], "queued")

    def test_budget_floor_goes_lean_then_stops(self) -> None:
        self.gh.runs[9] = {"id": 9, "status": "in_progress"}
        key = "run:o/r/9"
        gg.register(key, jobs=True)
        self.gh.rest_remaining = gg.FLOORS["rest"][0]  # the run fetch leaves it just under the floor
        # GraphQL is stopped too, so the runs stay on REST: this is the guard when both buckets are poor
        self.daemon.budget["graphql"] = {"limit": 5000, "remaining": 50, "reset": NOW + 3000}
        self.daemon.tick(NOW)  # first fill still happens, but without the job list
        self.assertEqual(len(self.gh.rest_calls()), 1)
        self.assertEqual(self.daemon.mode("rest"), "lean")
        self.assertNotIn("jobs", gg.read_cache(key))
        self.gh.runs[9] = {"id": 9, "status": "queued"}
        self.daemon.tick(NOW + gg.CYCLE)  # lean: one normal cycle is not enough
        self.assertEqual(len(self.gh.rest_calls()), 1)
        self.daemon.tick(NOW + gg.CYCLE * gg.LEAN_FACTOR)
        self.assertEqual(len(self.gh.rest_calls()), 2)
        self.daemon.budget["rest"]["remaining"] = gg.FLOORS["rest"][1] - 1  # below the reserve
        gg.register("run:o/r/10")
        self.daemon.tick(NOW + 8 * gg.CYCLE)  # before the reset
        self.assertEqual(len(self.gh.rest_calls()), 2)
        self.assertIsNone(gg.read_cache("run:o/r/10"))
        self.assertEqual(self.daemon.mode("rest"), "stop")
        self.daemon.budget["rest"]["reset"] = NOW  # the quota reset: back to normal
        self.daemon.now = NOW + 1
        self.assertEqual(self.daemon.mode("rest"), "normal")

    def test_graphql_reserve_and_rate_limit_backoff(self) -> None:
        self.daemon.budget["graphql"] = {"limit": 5000, "remaining": 10, "reset": NOW + 600}
        self.daemon.budget["rest"] = {"limit": 5000, "remaining": 10, "reset": NOW + 600}
        gg.register("pr:o/r#1")
        self.daemon.tick(NOW)  # both buckets below their reserve: nothing is read
        self.assertEqual(self.gh.calls, [])
        self.daemon.budget.pop("rest")
        (gg.watch_dir() / gg.file_name("pr:o/r#1")).unlink()  # else it, too, would now go over REST

        def limited(method, url, headers, body):
            return 403, {"retry-after": "120", "x-ratelimit-remaining": "0", "x-ratelimit-resource": "core",
                         "x-ratelimit-limit": "5000", "x-ratelimit-reset": str(int(NOW + 900))}, b'{"message":"rate limited"}'

        self.daemon.transport = limited
        gg.register("run:o/r/5")
        self.daemon.tick(NOW)
        self.assertEqual(self.daemon.mode("rest"), "stop")
        self.assertIn("HTTP 403", gg.read_cache("run:o/r/5")["error"])

    def test_token_never_written(self) -> None:
        self.gh.runs[7] = {"id": 7, "status": "queued"}
        self.gh.prs[("o", "r", 1)] = pr_node(1)
        gg.register("run:o/r/7")
        gg.register("pr:o/r#1")
        gg.register("pr:o/r#404")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.daemon.tick(NOW)
        for path in gg.BASE.rglob("*"):
            if path.is_file():
                self.assertNotIn(TOKEN, path.read_text(), path)
        self.assertNotIn(TOKEN, out.getvalue())

    def test_token_failure_is_reported_without_output(self) -> None:
        self.daemon.token_source = lambda: (_ for _ in ()).throw(gg.WatchError("gh auth token exited 1; is gh signed in?"))
        gg.register("pr:o/r#1")
        self.daemon.tick(NOW)
        self.assertIn("gh signed in", gg.read_cache("pr:o/r#1")["error"])

    def test_failures_are_retried_once_per_cycle(self) -> None:
        def broken(method, url, headers, body):
            raise gg.WatchError("request failed: RemoteDisconnected")

        self.daemon.transport = broken
        gg.register("pr:o/r#1")
        gg.register("run:o/r/2")
        attempts = []
        self.daemon.transport = lambda *a: (attempts.append(a[1]), broken(*a))[1]
        for i in range(20):  # 40 s of ticks
            self.daemon.tick(NOW + i * gg.TICK)
        self.assertEqual(len(attempts), 2)
        self.assertIn("RemoteDisconnected", gg.read_cache("run:o/r/2")["error"])

        def explodes(method, url, headers, body):
            raise KeyError("boom")  # an unexpected bug still costs one attempt per cycle

        self.daemon.transport = lambda *a: (attempts.append(a[1]), explodes(*a))[1]
        for i in range(20):
            self.daemon.tick(NOW + gg.CYCLE + i * gg.TICK)
        self.assertEqual(len(attempts), 4)

    def test_redirects_and_foreign_hosts_are_refused(self) -> None:
        with self.assertRaises(gg.WatchError):
            gg.http("GET", "https://example.com/x", {"Authorization": "Bearer x"})
        handler = gg.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 301, "Moved", {}, "https://evil.example/"))

    def test_token_format_is_checked(self) -> None:
        self.assertTrue(gg.TOKEN_RE.fullmatch("gho_" + "a" * 36))
        self.assertTrue(gg.TOKEN_RE.fullmatch("github_pat_" + "A1_" * 20))
        self.assertFalse(gg.TOKEN_RE.fullmatch("To get started with GitHub CLI, please run: gh auth login"))

    def test_bad_keys_are_refused(self) -> None:
        for kind, text in (("pr", "o/r"), ("pr", "o/r#x"), ("run", "o/r"), ("pr", "o/r#1) { x }")):
            with self.assertRaises(gg.WatchError):
                gg.parse_key(kind, text)
        self.assertEqual(gg.parse_key("pr", "Manaflow-AI/cmux#14542"), "pr:manaflow-ai/cmux#14542")
        self.assertEqual(gg.parse_key("run", "o/r/actions/runs/12/attempts/2"), "run:o/r/12")
        self.assertEqual(gg.parse_key("comment", "https://github.com/T/S/issues/454#issuecomment-5"), "comment:t/s#454")
        self.assertEqual(gg.parse_key("comment", "t/s#454"), "comment:t/s#454")
        with self.assertRaises(gg.WatchError):
            gg.parse_key("pr", "t/s/issues/454")  # an issue is not a pull request

    def test_failed_run_reads_its_jobs_once_per_attempt(self) -> None:
        key = "run:o/r/4"
        self.gh.runs[4] = {"id": 4, "status": "completed", "conclusion": "failure", "run_attempt": 1,
                           "updated_at": iso(NOW)}
        self.gh.jobs[4] = [job("build", end=NOW), job("lint", "success", end=NOW)]
        gg.register(key)  # no --jobs
        self.daemon.tick(NOW)
        entry = gg.read_cache(key)
        self.assertEqual([j["failedSteps"] for j in entry["jobs"]], [["Run selected tests"], []])
        self.assertIn("failed: build (step: Run selected tests)", gg.summary(key, entry))
        path = gg.watch_dir() / gg.file_name(key)
        os.utime(path, (NOW + gg.CYCLE - 1,) * 2)  # a waiter is still reading
        self.daemon.tick(NOW + gg.CYCLE)
        self.assertEqual((len(self.gh.run_calls()), len(self.gh.job_calls())), (2, 1))  # the run answered 304
        self.assertEqual(self.gh.rest_remaining, 3998)
        # attempt 2 fails too: its own job list is read
        self.gh.runs[4] = {**self.gh.runs[4], "run_attempt": 2}
        self.gh.jobs[4] = [job("build", run_attempt=2, end=NOW), job("lint", "success", end=NOW)]
        os.utime(path, (NOW + 2 * gg.CYCLE - 1,) * 2)
        self.daemon.tick(NOW + 2 * gg.CYCLE)
        self.assertEqual(len(self.gh.job_calls()), 2)
        self.assertEqual(gg.jobs_attempt(gg.read_cache(key)["jobs"]), 2)

    def test_failed_check_steps_are_read_once(self) -> None:
        failed = {**check_run("build", conclusion="FAILURE"), "id": "CR_1"}
        self.gh.prs[("o", "r", 1)] = pr_node(1, rollup="FAILURE", checks=[failed, {**check_run("lint"), "id": "CR_2"}])
        self.gh.steps["CR_1"] = [{"name": "Set up job", "conclusion": "SUCCESS"},
                                 {"name": "Run selected tests", "conclusion": "FAILURE"},
                                 {"name": "Upload logs", "conclusion": "CANCELLED"}]
        gg.register("pr:o/r#1")
        self.daemon.tick(NOW)
        self.assertEqual(self.gh.step_queries, [["CR_1"]])  # only the failed check
        data = gg.read_cache("pr:o/r#1")["data"]
        self.assertEqual(data["checks"][0]["failedSteps"], ["Run selected tests"])
        self.assertIn("failed: build (step: Run selected tests)", gg.summary("pr:o/r#1", {"data": data}))
        self.daemon.tick(NOW + gg.CYCLE)
        self.assertEqual(len(self.gh.gql_calls()), 3)  # PR, steps, PR: the steps were kept
        self.assertEqual(gg.read_cache("pr:o/r#1")["data"]["checks"][0]["failedSteps"], ["Run selected tests"])

    def test_comment_watch_polls_fast_and_ends_with_its_waiter(self) -> None:
        self.gh.issues[("t", "s", 454)] = {"__typename": "Issue", "url": "https://github.com/t/s/issues/454",
                                           "state": "OPEN", "comments": {"totalCount": 1, "nodes": [
                                               {"databaseId": 9, "url": "u9", "createdAt": iso(NOW),
                                                "author": {"login": "github-actions"}, "body": "x" * 9000}]}}
        gg.register("comment:t/s#454")
        gg.register("comment:t/s#455")
        self.daemon.tick(NOW)
        self.assertEqual(len(self.gh.gql_calls()), 1)  # both issues in one query
        data = gg.read_cache("comment:t/s#454")["data"]
        self.assertEqual(data["comments"][0]["author"], "github-actions")
        self.assertEqual(len(data["comments"][0]["body"]), gg.COMMENT_BODY_MAX)
        self.assertTrue(gg.read_cache("comment:t/s#455")["missing"])
        self.daemon.tick(NOW + gg.TICK)
        self.assertEqual(len(self.gh.gql_calls()), 1)
        self.daemon.tick(NOW + gg.COMMENT_CYCLE)
        self.assertEqual(len(self.gh.gql_calls()), 2)
        self.age_watch("comment:t/s#454", gg.TERMINAL_GRACE + 1)  # the wait ended
        self.age_watch("comment:t/s#455", 1)
        self.assertEqual(self.daemon.tick(NOW + gg.COMMENT_CYCLE + 1)["watching"], 1)

    def test_an_entry_filled_between_cycles_is_refreshed_on_the_next_one(self) -> None:
        self.gh.prs[("o", "r", 1)] = pr_node(1)
        self.daemon.tick(NOW)  # a cycle with nothing watched still starts the cycle clock
        gg.register("pr:o/r#1")
        self.daemon.tick(NOW + 40)  # filled at once
        self.daemon.tick(NOW + gg.CYCLE)  # the next cycle, 20 s later, refreshes it
        self.assertEqual(len(self.gh.gql_calls()), 2)
        self.assertEqual(gg.read_cache("pr:o/r#1")["fetchedAt"], NOW + gg.CYCLE)

    def test_heartbeat_and_request_counts(self) -> None:
        self.daemon.tick(NOW)  # nothing watched: the heartbeat is still written
        self.assertEqual(gg.read_json(gg.BASE / "daemon.json")["at"], NOW)
        self.daemon.tick(NOW + gg.TICK)
        self.assertEqual(gg.read_json(gg.BASE / "daemon.json")["at"], NOW)  # not every tick
        self.daemon.tick(NOW + gg.HEARTBEAT_EVERY)
        self.assertEqual(gg.read_json(gg.BASE / "daemon.json")["at"], NOW + gg.HEARTBEAT_EVERY)
        self.gh.runs[7] = {"id": 7, "status": "in_progress"}
        gg.register("run:o/r/7")
        self.daemon.tick(NOW + 100)
        self.daemon.tick(NOW + 100 + gg.CYCLE)
        hour = gg.read_json(gg.BASE / "daemon.json")["lastHour"]
        self.assertEqual(hour, {"rest": 1, "rest304": 1, "graphql": 0})
        self.gh.runs[7] = {"id": 7, "status": "queued"}
        self.daemon.tick(NOW + 100 + 3700)
        self.assertEqual(gg.read_json(gg.BASE / "daemon.json")["lastHour"]["rest304"], 0)  # an hour later

    def test_failing_ticks_keep_the_heartbeat_and_say_why(self) -> None:
        self.daemon.tick(NOW)
        self.daemon.tick_failed("AttributeError", "glaeda-gh:337 get", now=NOW + 5)
        doc = gg.read_json(gg.BASE / "daemon.json")
        self.assertEqual(doc["at"], NOW + 5)  # the first failure is written at once
        self.assertEqual(doc["failing"], {"error": "AttributeError", "site": "glaeda-gh:337 get",
                                          "since": NOW + 5, "count": 1})
        self.assertIn("lastHour", doc)  # the last good tick's fields are kept
        self.daemon.tick_failed("AttributeError", "glaeda-gh:337 get", now=NOW + 10)
        self.assertEqual(gg.read_json(gg.BASE / "daemon.json")["at"], NOW + 5)  # not every tick
        self.daemon.tick_failed("KeyError", "glaeda-gh:700 tick", now=NOW + 5 + gg.HEARTBEAT_EVERY)
        doc = gg.read_json(gg.BASE / "daemon.json")
        self.assertEqual(doc["at"], NOW + 5 + gg.HEARTBEAT_EVERY)  # but at the heartbeat's pace
        self.assertEqual(doc["failing"], {"error": "KeyError", "site": "glaeda-gh:700 tick",
                                          "since": NOW + 5, "count": 3})
        self.daemon.tick(NOW + 40)  # recovered: written at once, without the record
        doc = gg.read_json(gg.BASE / "daemon.json")
        self.assertEqual(doc["at"], NOW + 40)
        self.assertNotIn("failing", doc)
        self.daemon.tick_failed("AttributeError", "glaeda-gh:337 get", now=NOW + 45)
        self.assertEqual(gg.read_json(gg.BASE / "daemon.json")["failing"]["since"], NOW + 45)  # a new episode

    def test_restart_drops_the_previous_failing_record(self) -> None:
        """A restart or re-exec is how a fix arrives: clients must not keep exiting 3 on the old
        process's record until the new one's first good tick."""
        gg.BASE.mkdir(parents=True, exist_ok=True)
        now = time.time()
        gg.write_json(gg.BASE / "daemon.json", {
            "at": now, "heartbeat": 30, "lastHour": {"rest": 3},
            "failing": {"error": "AttributeError", "site": "glaeda-gh:337 get", "since": now - gg.FAILING_AFTER - 1,
                        "count": 30}})
        self.assertIsNotNone(gg.daemon_failing())

        class Stop(Exception):
            pass

        def stop(*args, **kwargs):
            raise Stop  # serve() has taken the lock and cleared the record by now

        with mock.patch.object(gg, "Daemon", stop), self.assertRaises(Stop):
            gg.serve()
        doc = gg.read_json(gg.BASE / "daemon.json")
        self.assertNotIn("failing", doc)
        self.assertEqual(doc["lastHour"], {"rest": 3})  # the rest is left alone
        self.assertGreaterEqual(doc["at"], now)  # and the new process's first heartbeat is written at once
        self.assertIsNone(gg.daemon_failing())
        gg.clear_failing()  # nothing to clear: a no-op
        self.assertEqual(gg.read_json(gg.BASE / "daemon.json"), doc)

    def test_restarted_daemon_does_not_look_hung_before_its_first_tick(self) -> None:
        """10-20: daemon.json kept the hung process's heartbeat until the new one finished a tick,
        so every session read "looks hung" again and kickstarted it, killing it in that first tick."""
        gg.BASE.mkdir(parents=True, exist_ok=True)
        gg.write_json(gg.BASE / "daemon.json", {"pid": 1, "at": time.time() - gg.STALE_HEARTBEAT - 400,
                                                 "heartbeat": 30, "watching": 27})
        self.assertIsNotNone(gg.daemon_stale())

        class Stop(Exception):
            pass

        def stop(*args, **kwargs):
            self.assertIsNone(gg.daemon_stale())  # before the first tick, not after it
            raise Stop

        with mock.patch.object(gg, "Daemon", stop), self.assertRaises(Stop):
            gg.serve()
        doc = gg.read_json(gg.BASE / "daemon.json")
        self.assertEqual((doc["pid"], doc["watching"]), (os.getpid(), 27))
        self.assertEqual(doc["startedAt"], doc["at"])


def workflow_check(name: str, run_id: int, workflow: str = "CI", conclusion: str | None = "SUCCESS",
                   suite: str | None = None, node_id: str | None = None) -> dict:
    return {**check_run(name, "COMPLETED" if conclusion else "IN_PROGRESS", conclusion), "id": node_id,
            "databaseId": run_id * 10, "startedAt": iso(NOW - 60),
            "checkSuite": {"conclusion": suite if suite is not None else conclusion,
                           "workflowRun": {"databaseId": run_id, "workflow": {"name": workflow, "databaseId": {"CI": 77, "Lint": 78}[workflow]}}}}


class BalanceTest(Base):
    """REST and GraphQL as one pool: each read goes to the bucket that is richer for its time left."""

    def budgets(self, rest: tuple[int, float], graphql: tuple[int, float]) -> None:
        self.daemon.now = NOW
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": rest[0], "reset": NOW + rest[1]},
                              "graphql": {"limit": 5000, "remaining": graphql[0], "reset": NOW + graphql[1]}}

    def test_richness_is_what_is_left_over_the_time_to_reset(self) -> None:
        self.budgets(rest=(600, 120), graphql=(600, 3000))  # same count: 2 minutes to go is rich
        self.assertGreater(self.daemon.richness("rest"), 10 * self.daemon.richness("graphql"))
        self.assertEqual(self.daemon.route("pr"), "rest")
        self.assertEqual(self.daemon.route("run"), "rest")
        self.assertEqual(self.daemon.route("comment"), "graphql")  # comment watches never move
        self.budgets(rest=(600, 3000), graphql=(600, 120))
        self.assertEqual(self.daemon.route("pr"), "graphql")
        self.assertEqual(self.daemon.route("run"), "graphql")
        # a bucket below its reserve is stopped whatever its time left; the other takes every read
        self.budgets(rest=(gg.FLOORS["rest"][1] - 1, 30), graphql=(150, 3500))
        self.assertEqual(self.daemon.richness("rest"), -1)
        self.assertEqual(self.daemon.route("run"), "graphql")

    def test_a_kind_moves_only_when_the_other_bucket_is_clearly_richer(self) -> None:
        self.budgets(rest=(2200, 1800), graphql=(2000, 1800))  # rest 10% richer: below the margin
        self.assertEqual(self.daemon.route("pr"), "graphql")
        self.budgets(rest=(3000, 1800), graphql=(2000, 1800))
        self.assertEqual(self.daemon.route("pr"), "rest")
        self.budgets(rest=(2200, 1800), graphql=(2000, 1800))  # and it stays there until GraphQL leads
        self.assertEqual(self.daemon.route("pr"), "rest")

    def test_the_cheaper_path_flips_as_buckets_drain(self) -> None:
        """Both buckets drain about evenly: reads follow whichever has more to spend per second."""
        self.budgets(rest=(4500, 1800), graphql=(1000, 1800))
        self.assertEqual(self.daemon.route("pr"), "rest")
        self.budgets(rest=(900, 1800), graphql=(1000, 600))  # REST drained; GraphQL's reset is near
        self.assertEqual(self.daemon.route("pr"), "graphql")
        self.assertEqual(self.daemon.route("run"), "graphql")
        self.budgets(rest=(3000, 1800), graphql=(1000, 1800))  # REST's reset refilled it
        self.assertEqual(self.daemon.route("run"), "rest")

    def test_runs_over_graphql_when_rest_is_poorer(self) -> None:
        self.gh.runs[7] = {"id": 7, "name": "CI", "display_title": "fix", "status": "completed", "conclusion": "failure",
                           "html_url": "https://github.com/o/r/actions/runs/7", "head_sha": "b" * 40, "head_branch": "x",
                           "event": "push", "run_attempt": 2, "created_at": iso(NOW - 99), "updated_at": iso(NOW - 9)}
        self.gh.jobs[7] = [job("build", run_attempt=2)]
        self.budgets(rest=(300, 3000), graphql=(4000, 1800))
        key = gg.parse_key("run", "o/r/7")
        gg.register(key)
        self.daemon.tick(NOW)
        self.assertEqual(self.gh.run_calls(), [])
        self.assertEqual(len(self.gh.gql_calls()), 1)
        self.assertIn("resource(url: $u0)", json.loads(self.gh.gql_calls()[0][3])["query"])
        entry = gg.read_cache(key)
        self.assertEqual(entry["data"], gg.slim_run(self.gh.runs[7]))  # the same answer REST gives
        self.assertEqual(entry["source"], "graphql")
        # the failed attempt's job list still comes from REST, once: only REST names the runner
        self.assertEqual(len(self.gh.job_calls()), 1)
        self.assertEqual(entry["jobs"][0]["runner_name"], "cmuxs-mac-mini-3-glaeda-1")
        self.daemon.tick(NOW + gg.TERMINAL_REFRESH)
        self.assertEqual(len(self.gh.job_calls()), 1)
        gg.register("run:o/r/404")
        self.daemon.tick(NOW + gg.TERMINAL_REFRESH + gg.CYCLE)
        missing = gg.read_cache("run:o/r/404")
        self.assertIn("no workflow run", missing["error"])
        self.assertFalse(missing.get("missing"))  # as REST's 404: an error, not a verdict

    def test_prs_over_rest_when_graphql_is_poorer_read_the_same(self) -> None:
        checks = [workflow_check("build", 11), workflow_check("test", 11, conclusion="FAILURE"),
                  workflow_check("lint", 12, workflow="Lint", conclusion=None, suite=None),
                  # the cancelled run a newer run of the same workflow replaced
                  workflow_check("test", 10, conclusion="CANCELLED"),
                  {"__typename": "StatusContext", "context": "ci/legacy", "state": "PENDING", "targetUrl": "t",
                   "createdAt": iso(NOW - 30)}]
        self.gh.prs[("o", "r", 1)] = {**pr_node(1, checks=checks), "reviewDecision": "APPROVED", "headRefName": "feat"}
        self.gh.prs[("o", "r", 1)]["baseRef"] = {"branchProtectionRule": {"requiredStatusCheckContexts": ["ci-status"]},
                                                 "rules": {"nodes": []}}
        self.gh.protected = ["ci-status"]
        key = gg.parse_key("pr", "o/r#1")
        gg.register(key)
        self.daemon.tick(NOW)  # equal buckets: GraphQL, as before
        over_graphql = gg.read_cache(key)["data"]
        self.assertEqual(self.gh.rest_calls(), [])
        self.daemon.budget["graphql"] = {"limit": 5000, "remaining": 400, "reset": NOW + 3000}
        self.daemon.tick(NOW + gg.CYCLE)
        paths = [c[1].split("/o/r/", 1)[1] for c in self.gh.rest_calls()]
        self.assertEqual(paths, ["pulls/1", f"commits/{'a' * 40}/check-runs?filter=all&per_page=100",
                                 f"commits/{'a' * 40}/status?per_page=100", f"actions/runs?head_sha={'a' * 40}&per_page=100",
                                 "actions/workflows?per_page=100", "rules/branches/main?per_page=100", "branches/main"])
        over_rest = gg.read_cache(key)["data"]
        self.assertEqual(over_rest["checkState"], "FAILURE")  # computed: a failed check outranks pending ones
        for field in ("state", "mergeable", "mergeStateStatus", "headRefOid", "headRefName",
                      "requiredMissing", "superseded", "reviewDecision"):
            self.assertEqual(over_rest[field], over_graphql[field], field)
        strip = lambda cs: sorted((c["name"], c["workflow"], c["status"], c["conclusion"], c["required"]) for c in cs)
        self.assertEqual(strip(over_rest["checks"]), strip(over_graphql["checks"]))
        self.assertEqual(gg.verdict("pr", over_rest), gg.verdict("pr", over_graphql))
        # nothing changed: every conditional read answers 304, which costs no quota
        before = self.gh.rest_remaining
        self.daemon.tick(NOW + 2 * gg.CYCLE)
        self.assertEqual(len(self.gh.rest_calls()), 14)  # 7 reads, all 304 the second time
        self.assertEqual(self.gh.rest_remaining, before)
        self.assertEqual(gg.read_cache(key)["data"]["checks"], over_rest["checks"])

    def test_over_100_checks_rest_pages_and_a_truncated_list_is_never_green(self) -> None:
        checks = [workflow_check(f"job-{i}", 5) for i in range(150)]
        self.gh.prs[("o", "r", 3)] = pr_node(3, checks=checks)
        self.budgets(rest=(4000, 1800), graphql=(300, 3000))
        gg.register("pr:o/r#3")
        self.daemon.tick(NOW)
        data = gg.read_cache("pr:o/r#3")["data"]
        self.assertEqual(len(data["checks"]), 150)  # two pages
        self.assertFalse(data["checksTruncated"])
        self.assertEqual(gg.verdict("pr", data), 0)
        # more checks than the pages it reads: every check read passed, but it is not called green
        with mock.patch.object(gg, "REST_CHECK_PAGES", 1):
            self.daemon.tick(NOW + gg.CYCLE)
        data = gg.read_cache("pr:o/r#3")["data"]
        self.assertTrue(data["checksTruncated"])
        self.assertEqual(data["checkState"], "PENDING")
        self.assertIsNone(gg.verdict("pr", data))

    def test_steps_come_from_rest_so_a_refused_check_is_still_held(self) -> None:
        """PRs move to REST when GraphQL is stopped; the failed steps must not need GraphQL."""
        now = time.time()
        build = {**workflow_check("build", 5, conclusion="FAILURE", node_id="CR_build"),
                 "startedAt": iso(now - 7), "completedAt": iso(now)}
        self.gh.prs[("o", "r", 4)] = pr_node(4, rollup="FAILURE", checks=[build])
        self.gh.job_steps[50] = [{"name": "Set up job", "conclusion": "success"},
                                 {"name": "Set up runner", "conclusion": "failure"}]
        self.budgets(rest=(4000, 1800), graphql=(50, 3000))  # GraphQL below its reserve
        gg.register("pr:o/r#4")
        self.daemon.tick(NOW)
        self.assertEqual(self.gh.gql_calls(), [])
        data = gg.read_cache("pr:o/r#4")["data"]
        self.assertEqual(data["checks"][0]["failedSteps"], ["Set up runner"])
        self.assertIsNone(gg.verdict("pr", data, "green"))  # held for the rescue, not red
        jobs = [c for c in self.gh.rest_calls() if "/actions/jobs/" in c[1]]
        self.assertEqual(len(jobs), 1)
        self.daemon.tick(NOW + gg.CYCLE)  # a completed job's steps are read once
        self.assertEqual(len([c for c in self.gh.rest_calls() if "/actions/jobs/" in c[1]]), 1)
        self.assertEqual(gg.read_cache("pr:o/r#4")["data"]["checks"][0]["failedSteps"], ["Set up runner"])

    def test_a_deleted_base_branch_keeps_the_last_requirements(self) -> None:
        self.gh.prs[("o", "r", 5)] = pr_node(5, checks=[workflow_check("build", 5)])
        self.gh.protected = ["ci-status"]
        self.budgets(rest=(4000, 1800), graphql=(300, 3000))
        gg.register("pr:o/r#5")
        self.daemon.tick(NOW)
        self.assertEqual(gg.read_cache("pr:o/r#5")["data"]["requiredMissing"], ["ci-status"])
        self.gh.base_gone = True
        self.daemon.tick(NOW + gg.CYCLE)
        entry = gg.read_cache("pr:o/r#5")
        self.assertIsNone(entry["error"])
        self.assertEqual(entry["data"]["requiredMissing"], ["ci-status"])

    def test_a_graphql_read_drops_the_kept_rest_answers(self) -> None:
        self.gh.prs[("o", "r", 6)] = pr_node(6, checks=[workflow_check("build", 5)])
        self.budgets(rest=(4000, 1800), graphql=(300, 3000))
        gg.register("pr:o/r#6")
        self.daemon.tick(NOW)
        self.assertTrue(gg.read_cache("pr:o/r#6")["restParts"])
        self.budgets(rest=(300, 3000), graphql=(4000, 1800))
        self.daemon.tick(NOW + gg.CYCLE)
        self.assertIsNone(gg.read_cache("pr:o/r#6")["restParts"])

    def test_a_failed_run_query_says_what_github_said(self) -> None:
        def broken(method, url, headers, body):
            return 200, {}, json.dumps({"errors": [{"message": "Field 'runAttempt' doesn't exist on type 'WorkflowRun'"}]}).encode()
        self.daemon.transport = broken
        self.budgets(rest=(300, 3000), graphql=(4000, 1800))
        gg.register("run:o/r/7")
        self.daemon.tick(NOW)
        self.assertIn("runAttempt", gg.read_cache("run:o/r/7")["error"])

    def test_rulesets_name_required_checks_over_rest(self) -> None:
        self.gh.prs[("o", "r", 2)] = pr_node(2, checks=[workflow_check("build", 5)])
        self.gh.rules = [{"type": "pull_request"}, {"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "ci-status"}, {"context": "build"}]}}]
        self.budgets(rest=(4000, 1800), graphql=(300, 3000))
        gg.register("pr:o/r#2")
        self.daemon.tick(NOW)
        data = gg.read_cache("pr:o/r#2")["data"]
        self.assertEqual(data["requiredMissing"], ["ci-status"])  # pending until it reports, as over GraphQL
        self.assertTrue(data["checks"][0]["required"])
        self.assertIsNone(gg.verdict("pr", data))

    def test_a_missing_pr_over_rest_is_missing(self) -> None:
        self.budgets(rest=(4000, 1800), graphql=(300, 3000))
        gg.register("pr:o/r#404")
        self.daemon.tick(NOW)
        entry = gg.read_cache("pr:o/r#404")
        self.assertTrue(entry["missing"])
        self.assertIn("HTTP 404", entry["error"])
        self.assertEqual(len(self.gh.rest_calls()), 1)

    def test_token_never_written_on_either_path(self) -> None:
        self.gh.prs[("o", "r", 1)] = pr_node(1, checks=[workflow_check("build", 5)])
        self.gh.runs[7] = {"id": 7, "status": "queued", "html_url": "u"}
        self.budgets(rest=(4000, 1800), graphql=(300, 3000))  # PRs over REST
        gg.register("pr:o/r#1")
        self.daemon.tick(NOW)
        self.budgets(rest=(300, 3000), graphql=(4000, 1800))  # runs over GraphQL
        gg.register("run:o/r/7")
        self.daemon.tick(NOW + gg.CYCLE)
        self.assertTrue(self.gh.rest_calls() and self.gh.gql_calls())
        for path in gg.BASE.rglob("*"):
            if path.is_file():
                self.assertNotIn(TOKEN, path.read_text(), path)


class ClientTest(Base):
    def setUp(self) -> None:
        super().setUp()
        gg.BASE.mkdir(parents=True)
        self.lock = open(gg.BASE / "daemon.lock", "a+")
        fcntl.flock(self.lock, fcntl.LOCK_EX)  # stands in for a running daemon

    def tearDown(self) -> None:
        self.lock.close()
        super().tearDown()

    def put(self, key: str, data: dict | None, at: float | None = None, **extra) -> None:
        at = time.time() if at is None else at
        gg.write_json(gg.cache_dir() / gg.file_name(key), {"key": key, "fetchedAt": at,
                                                          "dataAt": at if data else None, "data": data, "error": None, **extra})

    def wait(self, kind: str, target: str, until: str = "green", timeout: float = 0.5, fill: tuple | None = None,
             sha: str | None = None, grace: float = gg.RESCUE_GRACE) -> tuple[int, str]:
        """Wait while a stand-in daemon keeps writing `fill` (data, extra), as the real one refreshes.

        One write at a fixed delay raced the wait's own start: resolving a short --sha runs git,
        which took 0.15 s on a loaded host, so the write landed before the wait began and was
        (rightly) ignored as older data."""
        key = gg.parse_key(kind, target)
        done = threading.Event()

        def daemon() -> None:
            while not done.wait(0.05):
                self.put(key, fill[0], **fill[1])
        writer = threading.Thread(target=daemon) if fill else None
        if writer:
            writer.start()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = gg.cmd_wait(kind, target, until, timeout, False, False, sha, grace)
        finally:
            done.set()
            if writer:
                writer.join()
        return code, out.getvalue()

    def test_wait_exit_codes(self) -> None:
        green = gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a")]))
        self.assertEqual(self.wait("pr", "o/r#1", fill=(green, {}))[0], 0)

        red = gg.parse_pr(pr_node(2, rollup="PENDING", checks=[check_run("a", conclusion="FAILURE"),
                                                                check_run("b", "IN_PROGRESS", None)]))
        code, text = self.wait("pr", "o/r#2", fill=(red, {}))
        self.assertEqual(code, 1)  # green fails fast
        self.assertIn("failed: a  https://ci/a", text)
        self.assertEqual(self.wait("pr", "o/r#2", until="done", fill=(red, {}))[0], 2)  # done waits for b

        closed = gg.parse_pr(pr_node(3, state="CLOSED", rollup="SUCCESS"))
        self.assertEqual(self.wait("pr", "o/r#3", until="merged", fill=(closed, {}))[0], 1)
        merged = gg.parse_pr(pr_node(4, state="MERGED", rollup=None))
        self.assertEqual(self.wait("pr", "o/r#4", until="merged", fill=(merged, {}))[0], 0)

        self.assertEqual(self.wait("run", "o/r/5", fill=({"status": "completed", "conclusion": "failure"}, {}))[0], 1)
        self.assertEqual(self.wait("run", "o/r/6", fill=({"status": "completed", "conclusion": "success"}, {}))[0], 0)
        code, text = self.wait("run", "o/r/7", timeout=0.1)  # nothing cached yet
        self.assertEqual(code, 2)
        self.assertIn("result: timeout", text)
        # waiting registered interest for the daemon to pick up
        self.assertIsNotNone(gg.read_json(gg.watch_dir() / gg.file_name("run:o/r/7")))

    def test_conflicting_pr_stops_green_and_done_but_not_merged(self) -> None:
        pending = [check_run("a"), check_run("b", "IN_PROGRESS", None)]
        node = {**pr_node(1, rollup="PENDING", checks=pending), "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}
        data = gg.parse_pr(node)
        for until in ("green", "done"):
            code, text = self.wait("pr", "o/r#1", until=until, fill=(data, {}))
            self.assertEqual(code, gg.EXIT_CONFLICT)
            self.assertEqual(code, 4)
            self.assertIn("result: conflict", text)
            self.assertIn("note: PR conflicts with its base", text)
        self.assertEqual(self.wait("pr", "o/r#1", until="merged", fill=(data, {}))[0], 2)  # keeps waiting
        # A conflict decides before any check: green checks do not hide it either.
        green = {**pr_node(2, rollup="SUCCESS", checks=[check_run("a")]), "mergeable": "CONFLICTING",
                 "mergeStateStatus": "DIRTY"}
        self.assertEqual(gg.verdict("pr", gg.parse_pr(green), "green"), gg.EXIT_CONFLICT)
        # GitHub computes mergeability lazily: UNKNOWN is pending, not a conflict.
        unknown = gg.parse_pr({**node, "mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"})
        self.assertIsNone(gg.verdict("pr", unknown, "done"))
        self.assertEqual(self.wait("pr", "o/r#3", until="done", fill=(unknown, {}))[0], 2)
        # With --sha, an older head's conflict is pending: the push that resolves it has not landed yet.
        self.assertIsNone(gg.verdict("pr", data, "green", sha="b" * 40))
        self.assertEqual(gg.verdict("pr", data, "green", sha="a" * 40), gg.EXIT_CONFLICT)
        # A merged PR is never a conflict.
        merged = gg.parse_pr({**node, "state": "MERGED"})
        self.assertEqual(gg.verdict("pr", merged, "merged"), 0)

    def test_closed_unmerged_pr_fails_and_says_so(self) -> None:
        closed = gg.parse_pr(pr_node(1, state="CLOSED", rollup="PENDING", checks=[check_run("a", "IN_PROGRESS", None)]))
        for until in ("green", "done", "merged"):
            code, text = self.wait("pr", "o/r#1", until=until, fill=(closed, {}))
            self.assertEqual(code, 1)
            self.assertIn("note: PR was closed without being merged", text)

    def test_wait_ignores_cache_from_before_it_began(self) -> None:
        old_green = gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a")]))
        self.put("pr:o/r#1", old_green, at=time.time() - 30)
        self.assertEqual(self.wait("pr", "o/r#1", timeout=0.3)[0], 2)

    def test_wait_sha_mismatch_is_pending(self) -> None:
        green = gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a")]))  # head is aaaa...
        code, text = self.wait("pr", "o/r#1", fill=(green, {}), sha="b" * 40, timeout=0.4)
        self.assertEqual(code, 2)
        self.assertIn("GitHub still shows head aaaaaaaaaa", text)
        self.assertEqual(self.wait("pr", "o/r#1", fill=(green, {}), sha="aaaaaaa")[0], 0)

    def test_missing_pr_fails(self) -> None:
        code, text = self.wait("pr", "o/r#404", fill=(None, {"error": "Could not resolve", "missing": True}))
        self.assertEqual(code, 1)
        self.assertIn("Could not resolve", text)

    def test_rerun_after_cancel_counts_the_latest_attempt(self) -> None:
        old = {**check_run("Web complexity", conclusion="CANCELLED"), "databaseId": 1, "startedAt": "2026-09-25T10:00:00Z"}
        new = {**check_run("Web complexity"), "databaseId": 2, "startedAt": "2026-09-25T11:00:00Z"}
        data = gg.parse_pr(pr_node(1, rollup="FAILURE", checks=[new, old]))
        self.assertEqual([c["conclusion"] for c in data["checks"]], ["SUCCESS"])
        self.assertEqual(gg.verdict("pr", data), 0)

    def test_queued_rerun_outranks_the_cancelled_attempt(self) -> None:
        old = {**check_run("e2e", conclusion="CANCELLED"), "databaseId": 1, "startedAt": "2026-09-25T10:00:00Z"}
        queued = {**check_run("e2e", "QUEUED", None), "databaseId": 2, "startedAt": None}
        data = gg.parse_pr(pr_node(1, rollup="PENDING", checks=[old, queued]))
        self.assertEqual([c["status"] for c in data["checks"]], ["QUEUED"])
        self.assertIsNone(gg.verdict("pr", data))

    def test_a_required_check_that_has_not_reported_is_pending(self) -> None:
        node = pr_node(1, rollup="SUCCESS", checks=[{**check_run("Web complexity"), "isRequired": True},
                                                     {**check_run("lint"), "isRequired": False}])
        node["baseRef"] = {"branchProtectionRule": None, "rules": {"nodes": [
            {"type": "REQUIRED_STATUS_CHECKS", "parameters": {"requiredStatusChecks": [
                {"context": "Web complexity"}, {"context": "ci-status"}]}},
            {"type": "DELETION", "parameters": None}]}}
        data = gg.parse_pr(node)
        self.assertEqual(data["requiredMissing"], ["ci-status"])
        self.assertIsNone(gg.verdict("pr", data, "green"))
        self.assertIsNone(gg.verdict("pr", data, "done"))
        self.assertIn("required, not reported yet: ci-status", gg.summary("pr:o/r#1", {"data": data}))
        # GitHub's merge state already counts it: CLEAN means nothing required is outstanding.
        self.assertEqual(gg.verdict("pr", {**data, "mergeStateStatus": "CLEAN"}, "green"), 0)
        # A conflicted PR runs no workflows until the next push, and a merged one never will: no wait.
        self.assertEqual(gg.check_verdict({**data, "mergeStateStatus": "DIRTY"}, "done"), 1)
        self.assertEqual(gg.verdict("pr", {**data, "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}, "done"),
                         gg.EXIT_CONFLICT)
        # A lagging DIRTY without CONFLICTING is not called a conflict.
        self.assertNotEqual(gg.verdict("pr", {**data, "mergeable": "MERGEABLE", "mergeStateStatus": "DIRTY"}, "done"),
                            gg.EXIT_CONFLICT)
        self.assertIn("merge conflicts", gg.summary("pr:o/r#1", {"data": {**data, "mergeStateStatus": "DIRTY"}}))
        self.assertEqual(gg.verdict("pr", {**data, "state": "MERGED", "mergeStateStatus": "UNKNOWN"}, "green"), 0)
        # Classic branch protection lists its contexts too.
        node["baseRef"] = {"branchProtectionRule": {"requiredStatusCheckContexts": ["build"]}, "rules": None}
        self.assertEqual(gg.parse_pr(node)["requiredMissing"], ["build"])

    def test_a_run_cancelled_for_a_newer_run_of_its_workflow_is_dropped(self) -> None:
        def in_run(check: dict, run: int, conclusion: str | None) -> dict:
            return {**check, "databaseId": run * 10, "isRequired": True, "checkSuite": {
                "conclusion": conclusion, "workflowRun": {"databaseId": run, "workflow": {"name": "CI"}}}}
        old = [in_run(check_run("ci-status", conclusion="FAILURE"), 1, "CANCELLED"),
               in_run(check_run("changes", conclusion="CANCELLED"), 1, "CANCELLED")]
        new = [in_run(check_run("changes"), 2, None), in_run(check_run("macos", "QUEUED", None), 2, None)]
        data = gg.parse_pr(pr_node(1, rollup="FAILURE", checks=old + new))
        self.assertEqual(sorted(c["name"] for c in data["checks"]), ["changes", "macos"])
        self.assertIsNone(gg.verdict("pr", data, "green"))
        # With no newer run, a cancelled run's failure still counts.
        alone = gg.parse_pr(pr_node(2, rollup="FAILURE", checks=old))
        self.assertEqual(gg.verdict("pr", alone, "green"), 1)

    def test_same_name_in_two_workflows_stays_separate(self) -> None:
        def in_workflow(check: dict, workflow: str, db: int) -> dict:
            return {**check, "databaseId": db, "checkSuite": {"workflowRun": {"workflow": {"name": workflow}}}}
        data = gg.parse_pr(pr_node(1, rollup="FAILURE", checks=[
            in_workflow(check_run("build", conclusion="FAILURE"), "CI", 1),
            in_workflow(check_run("build"), "Release", 2)]))
        self.assertEqual(len(data["checks"]), 2)
        self.assertEqual(gg.verdict("pr", data), 1)
        self.assertIn("failed: CI / build", gg.summary("pr:o/r#1", {"data": data}))

    def test_finished_things_answer_at_once_from_an_older_fetch(self) -> None:
        self.put("pr:o/r#1", gg.parse_pr(pr_node(1, state="MERGED", rollup="SUCCESS")), at=time.time() - 300)
        self.assertEqual(self.wait("pr", "o/r#1", until="merged", timeout=0.3)[0], 0)
        # a completed run may have been rerun since: only data fetched after the wait began decides
        self.put("run:o/r/2", {"status": "completed", "conclusion": "failure"}, at=time.time() - 300)
        self.assertEqual(self.wait("run", "o/r/2", timeout=0.3)[0], 2)
        rerun = ({"status": "queued", "conclusion": None}, {})
        self.assertEqual(self.wait("run", "o/r/2", timeout=0.4, fill=rerun)[0], 2)
        passed = ({"status": "completed", "conclusion": "success"}, {})
        self.assertEqual(self.wait("run", "o/r/2", timeout=0.5, fill=passed)[0], 0)
        self.put("pr:o/r#4", gg.parse_pr(pr_node(4, state="CLOSED", rollup="SUCCESS")), at=time.time() - 300)
        self.assertEqual(self.wait("pr", "o/r#4", until="merged", timeout=0.3)[0], 2)  # may be reopened
        self.put("run:o/r/3", {"status": "in_progress"}, at=time.time() - 300)
        self.assertEqual(self.wait("run", "o/r/3", timeout=0.3)[0], 2)  # live and stale: not trusted

    def test_only_not_found_fails_a_missing_pr(self) -> None:
        code, _ = self.wait("pr", "o/r#5", fill=(None, {"error": "graphql: HTTP 502", "missing": False}), timeout=0.4)
        self.assertEqual(code, 2)  # a transport error or 5xx stays pending
        code, _ = self.wait("pr", "o/r#6", fill=(None, {"error": "gh auth token exited 1; is gh signed in?"}), timeout=0.4)
        self.assertEqual(code, 2)

    def test_a_failed_optional_check_fails_unless_only_required_ones_count(self) -> None:
        req = {**check_run("build"), "isRequired": True}
        optional = {**check_run("lint", conclusion="FAILURE"), "isRequired": False}
        data = gg.parse_pr(pr_node(1, rollup="FAILURE", checks=[req, optional]))
        self.assertEqual(gg.verdict("pr", data, "green"), 1)
        self.assertEqual(gg.verdict("pr", data, "done"), 1)
        self.assertEqual(gg.verdict("pr", data, "required"), 0)
        self.assertIn("lint (not required)", gg.summary("pr:o/r#1", {"data": data}))
        many = {**data, "checksTruncated": True, "checkState": "PENDING"}
        self.assertIsNone(gg.verdict("pr", many))  # over 100 checks: the rollup decides
        self.assertIn("none reported yet", gg.summary("pr:o/r#2", {"data": gg.parse_pr(pr_node(2, rollup=None))}))

    def test_daemon_down_waits_out_a_reexec(self) -> None:
        self.lock.close()
        holder = open(gg.BASE / "daemon.lock", "a+")
        relock = threading.Timer(0.1, lambda: fcntl.flock(holder, fcntl.LOCK_EX))
        relock.start()
        self.assertTrue(gg.daemon_running(grace=0.5))
        relock.join()
        holder.close()

    def test_done_uses_every_check(self) -> None:
        both = gg.parse_pr(pr_node(1, rollup="FAILURE", checks=[check_run("a"), check_run("b", conclusion="CANCELLED")]))
        self.assertEqual(gg.verdict("pr", both, "done"), 1)
        ok = gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a"), check_run("b", conclusion="SKIPPED")]))
        self.assertEqual(gg.verdict("pr", ok, "done"), 0)
        self.assertIsNone(gg.verdict("pr", gg.parse_pr(pr_node(1, rollup=None)), "done"))

    def test_status_prints_cached_entry(self) -> None:
        self.put("pr:o/r#1", gg.parse_pr(pr_node(1, rollup="PENDING", checks=[check_run("a")])))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gg.cmd_status("pr", "o/r#1", False, True, first_wait=0), 0)
        self.assertEqual(json.loads(out.getvalue())["data"]["checkState"], "PENDING")

    def test_daemon_down(self) -> None:
        self.lock.close()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gg.cmd_wait("pr", "o/r#1", "green", 5, False, False), 3)
            self.assertEqual(gg.cmd_status("pr", "o/r#1", False, False), 3)
            self.assertEqual(gg.cmd_budget(False), 3)
        self.assertIn("daemon is not running", err.getvalue())
        self.assertIn("Start it: ", err.getvalue())
        self.assertFalse(gg.watch_dir().exists())  # no interest registered, nothing polled

    def test_wait_rides_out_a_restart(self) -> None:
        """A wait that finds the daemon down keeps waiting for it (launchd can hold a respawn back for
        30 s) instead of exiting 3 and having its session kickstart it again."""
        self.lock.close()
        gg.DOWN_PATIENCE = 30
        holder = open(gg.BASE / "daemon.lock", "a+")
        back = threading.Timer(0.8, lambda: fcntl.flock(holder, fcntl.LOCK_EX))
        back.start()
        try:
            code, text = self.wait("pr", "o/r#1", timeout=1.6, fill=(gg.parse_pr(pr_node(1, rollup="SUCCESS",
                                                                     checks=[check_run("a")])), {}))
        finally:
            back.join()
            holder.close()
        self.assertEqual(code, 0, text)
        self.assertEqual(text.count("waiting up to 30s"), 1)
        self.assertNotIn("not running, so nothing", text)

    def test_down_after_a_restart_says_wait(self) -> None:
        self.lock.close()
        gg.write_json(gg.BASE / "restart.json", {"at": time.time() - 20, "by": 1, "how": "launchctl kickstart"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gg.cmd_budget(False), 3)
        self.assertIn("restarted it 20s ago", err.getvalue())
        self.assertIn("Do not restart it again", err.getvalue())
        gg.write_json(gg.BASE / "restart.json", {"at": time.time() - gg.RESTART_COOLDOWN - 1})
        self.assertEqual(gg.down_message(), gg.DOWN)

    def test_hung_daemon_is_reported(self) -> None:
        gg.write_json(gg.BASE / "daemon.json", {"at": time.time() - gg.STALE_HEARTBEAT - 60, "heartbeat": 30})
        code, text = self.wait("run", "o/r/1", timeout=0.1)
        self.assertEqual(code, 2)
        self.assertIn("looks hung", text)
        self.assertEqual(text.count("looks hung"), 1)  # said once, not every poll
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gg.cmd_budget(False), 3)
        gg.write_json(gg.BASE / "daemon.json", {"at": time.time(), "heartbeat": 30})
        self.assertNotIn("looks hung", self.wait("run", "o/r/1", timeout=0.1)[1])
        # an older daemon, idle, wrote daemon.json only when it last fetched something
        gg.write_json(gg.BASE / "daemon.json", {"at": time.time() - 3 * gg.STALE_HEARTBEAT})
        self.assertNotIn("looks hung", self.wait("run", "o/r/1", timeout=0.1)[1])

    def test_failing_daemon_is_reported_not_waited_on(self) -> None:
        now = time.time()
        failing = {"error": "AttributeError", "site": "glaeda-gh:337 get", "count": 26000}
        gg.write_json(gg.BASE / "daemon.json", {"at": now, "heartbeat": 30,
                                                "failing": {**failing, "since": now - gg.FAILING_AFTER - 1}})
        code, text = self.wait("run", "o/r/1", timeout=30)
        self.assertEqual(code, 3)  # at once, not after the 30 s timeout
        self.assertIn("every cycle since", text)
        self.assertIn("AttributeError at glaeda-gh:337 get", text)
        self.assertIn("Restarting it will not fix this", text)
        self.assertNotIn("looks hung", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gg.cmd_status("run", "o/r/1", False, False, first_wait=0), 3)
            self.assertEqual(gg.cmd_wait_comment("o/r#1", 30, False), 3)
            self.assertEqual(gg.cmd_budget(True), 3)
        self.assertTrue(json.loads(out.getvalue())["failing"])
        # a moment of failure is not a broken daemon: the wait goes on
        gg.write_json(gg.BASE / "daemon.json", {"at": now, "heartbeat": 30, "failing": {**failing, "since": now - 5}})
        self.assertEqual(self.wait("run", "o/r/1", timeout=0.1)[0], 2)

    # -- a glaeda runner refusing a job at setup, then the rescue re-running it

    def run_doc(self, attempt: int, conclusion: str | None, finished: float | None = None) -> dict:
        finished = time.time() if finished is None else finished
        return {"status": "completed" if conclusion else "in_progress", "conclusion": conclusion,
                "run_attempt": attempt, "head_sha": "c" * 40, "updated_at": iso(finished), "html_url": "u"}

    def test_refused_attempt_is_held_then_the_rescue_attempt_decides(self) -> None:
        refusal = (self.run_doc(1, "failure"), cached(refused(), job("lint", "cancelled", seconds=5)))
        code, text = self.wait("run", "o/r/1", fill=refusal, timeout=0.4)
        self.assertEqual(code, 2)  # held, not failed
        self.assertIn("held: attempt 1 failed only because a glaeda runner refused build at setup", text)
        self.assertIn("failed: build (step: Set up runner) (refused by cmuxs-mac-mini-3-glaeda-1)", text)
        # the rescue's attempt 2 lands on Blacksmith and passes
        self.assertEqual(self.wait("run", "o/r/1", fill=(self.run_doc(2, "success"), {}))[0], 0)
        # no rescue came within the grace: the failure stands, and says why it waited
        late = (self.run_doc(1, "failure", time.time() - gg.RESCUE_GRACE - 1), cached(refused()))
        code, text = self.wait("run", "o/r/1", fill=late)
        self.assertEqual(code, 1)
        self.assertIn("no rescue attempt followed within 360s", text)
        # --rescue-grace 0 rules on each attempt
        self.assertEqual(self.wait("run", "o/r/1", fill=refusal, grace=0)[0], 1)

    def test_only_a_refusal_on_a_glaeda_runner_is_held(self) -> None:
        cases = {
            "a real failure": [job("build")],
            "a refusal and a real failure": [refused(), job("test")],
            "setup failed on another runner": [refused(runner="GitHub Actions 12", labels=("ubuntu-latest",))],
            "setup failed after running a while": [job("build", failed_step="Set up runner", seconds=600)],
            "an older attempt's jobs": [refused(run_attempt=1)],
        }
        for why, jobs in cases.items():
            attempt = 2 if why == "an older attempt's jobs" else 1
            code, _ = self.wait("run", "o/r/1", fill=(self.run_doc(attempt, "failure"), cached(*jobs)))
            self.assertEqual(code, 1, why)
        self.assertEqual(self.wait("run", "o/r/1", fill=(self.run_doc(1, "failure"), {}))[0], 1)  # jobs unknown
        # a run the rescue cancelled after the refusal is held as well
        cancelled = (self.run_doc(1, "cancelled"), cached(refused(), job("test", "cancelled")))
        self.assertEqual(self.wait("run", "o/r/1", fill=cancelled, timeout=0.3)[0], 2)

    def test_refused_pr_check_holds_its_run(self) -> None:
        now = time.time()

        def in_run(check: dict, run: int, seconds: int, required: bool = True) -> dict:
            return {**check, "id": f"CR_{check['name']}", "databaseId": run * 10, "isRequired": required,
                    "startedAt": iso(now - seconds), "completedAt": iso(now),
                    "checkSuite": {"conclusion": "FAILURE", "workflowRun": {"databaseId": run, "workflow": {"name": "CI"}}}}
        build = in_run(check_run("build", conclusion="FAILURE"), 5, 7)
        status = in_run(check_run("ci-status", conclusion="FAILURE"), 5, 3)
        data = gg.parse_pr(pr_node(1, rollup="FAILURE", checks=[build, status]))
        data["checks"][0]["failedSteps"], data["checks"][1]["failedSteps"] = ["Set up runner"], ["Check results"]
        self.assertIsNone(gg.verdict("pr", data, "green"))
        text = gg.summary("pr:o/r#1", {"data": data}, "green")
        self.assertIn("checks PENDING: 0 passed, 0 failed, 0 pending, 2 held", text)
        self.assertIn("held: CI / build (step: Set up runner)", text)
        self.assertEqual(gg.verdict("pr", data, "green", grace=0), 1)
        self.assertEqual(gg.verdict("pr", data, "green", now=now + gg.RESCUE_GRACE + 1), 1)
        # a real failure in the same run is not held
        data["checks"][0]["failedSteps"] = ["Run selected tests"]
        self.assertEqual(gg.verdict("pr", data, "green"), 1)

    def test_over_100_checks_a_held_check_still_waits_for_its_rescue(self) -> None:
        """manaflow-ai/cmux#20733 (95 checks shown of more): the rescue cancelled a queued fast tier, and
        the wait exited 1 on GitHub's FAILURE rollup while 13 checks still ran and the re-run was due."""
        now = time.time()
        queued = {**check_run("fast tier", conclusion="CANCELLED"), "id": "CR_q", "databaseId": 5,
                  "startedAt": iso(now - 104), "completedAt": iso(now - 5),
                  "checkSuite": {"conclusion": "CANCELLED", "workflowRun": {"databaseId": 9, "workflow": {"name": "cmux-next"}}}}
        running = check_run("react-apps-check", status="IN_PROGRESS", conclusion=None)
        data = gg.parse_pr(pr_node(1, rollup="FAILURE", checks=[queued, running, check_run("build")]))
        data["checks"][0]["stepCount"] = 0
        many = {**data, "checksTruncated": True, "checkState": "FAILURE"}
        self.assertIsNone(gg.verdict("pr", many, "green"))
        self.assertIsNone(gg.verdict("pr", many, "done"))
        # the re-run never came: the rollup's FAILURE decides once the grace is over
        settled = {**many, "checks": [c for c in many["checks"] if c.get("name") != "react-apps-check"]}
        self.assertEqual(gg.verdict("pr", settled, "green", now=now + gg.RESCUE_GRACE + 1), 1)
        # a real failure in the shown checks still fails at once; with none shown and none held, the
        # rollup's FAILURE is a check past the first 100
        broken = gg.parse_pr(pr_node(2, rollup="FAILURE", checks=[queued, check_run("lint", conclusion="FAILURE")]))
        broken["checks"][0]["stepCount"] = 0
        self.assertEqual(gg.verdict("pr", {**broken, "checksTruncated": True, "checkState": "FAILURE"}, "green"), 1)
        # --until done waits for every shown check to finish, failure or not, as under 100 checks
        busy = gg.parse_pr(pr_node(5, rollup="FAILURE", checks=[check_run("lint", conclusion="FAILURE"), running]))
        self.assertIsNone(gg.verdict("pr", {**busy, "checksTruncated": True, "checkState": "FAILURE"}, "done"))
        self.assertEqual(gg.verdict("pr", {**busy, "checksTruncated": True, "checkState": "FAILURE"}, "green"), 1)
        hidden = gg.parse_pr(pr_node(3, rollup="FAILURE", checks=[check_run("build")]))
        self.assertEqual(gg.verdict("pr", {**hidden, "checksTruncated": True, "checkState": "FAILURE"}, "green"), 1)
        ok = gg.parse_pr(pr_node(4, rollup="SUCCESS", checks=[check_run("build")]))
        self.assertEqual(gg.verdict("pr", {**ok, "checksTruncated": True, "checkState": "SUCCESS"}, "green"), 0)

    def test_a_check_cancelled_before_any_step_is_held_for_the_rescue(self) -> None:
        """manaflow-ai/cmux#20608: the owned-pool rescue cancelled a fast tier still queued for a
        runner (no step ran) and re-ran it; a wait that read the gap exited 1 before the re-run."""
        now = time.time()
        queued = {**check_run("fast tier", conclusion="CANCELLED"), "id": "CR_q", "databaseId": 5,
                  "startedAt": iso(now - 104), "completedAt": iso(now - 5),
                  "checkSuite": {"conclusion": "CANCELLED", "workflowRun": {"databaseId": 9, "workflow": {"name": "cmux-next"}}}}
        data = gg.parse_pr(pr_node(1, rollup="FAILURE", checks=[queued, check_run("build")]))
        data["checks"][0]["stepCount"] = 0
        self.assertIsNone(gg.verdict("pr", data, "green"))
        text = gg.summary("pr:o/r#1", {"data": data}, "green")
        self.assertIn("held: cmux-next / fast tier: cancelled before any step ran", text)
        self.assertEqual(gg.verdict("pr", data, "green", now=now + gg.RESCUE_GRACE + 1), 1)
        self.assertEqual(gg.verdict("pr", data, "green", grace=0), 1)
        # the rescue cancels the whole run: a sibling that was running is cancelled with steps, and held
        # too; a real failure in that run still decides at once
        running = {**check_run("daemon tests", conclusion="CANCELLED"), "id": "CR_r", "databaseId": 6,
                   "startedAt": iso(now - 300), "completedAt": iso(now - 5),
                   "checkSuite": {"conclusion": "CANCELLED", "workflowRun": {"databaseId": 9, "workflow": {"name": "cmux-next"}}}}
        both = gg.parse_pr(pr_node(2, rollup="FAILURE", checks=[queued, running, check_run("build")]))
        both["checks"][0]["stepCount"], both["checks"][1]["stepCount"] = 0, 7
        self.assertIsNone(gg.verdict("pr", both, "green"))
        self.assertIn("held: cmux-next / daemon tests: its run was cancelled while a job was still queued",
                      gg.summary("pr:o/r#2", {"data": both}, "green"))
        broken = {**check_run("lint", conclusion="FAILURE"), "id": "CR_l", "databaseId": 7,
                  "checkSuite": {"conclusion": "FAILURE", "workflowRun": {"databaseId": 9, "workflow": {"name": "cmux-next"}}}}
        mixed = gg.parse_pr(pr_node(3, rollup="FAILURE", checks=[queued, broken]))
        mixed["checks"][0]["stepCount"] = 0
        self.assertEqual(gg.verdict("pr", mixed, "green"), 1)
        # a job that ran steps, or whose steps were never read, was cancelled for real
        data["checks"][0]["stepCount"] = 3
        self.assertEqual(gg.verdict("pr", data, "green"), 1)
        del data["checks"][0]["stepCount"]
        self.assertEqual(gg.verdict("pr", data, "green"), 1)

    def test_steps_are_read_for_a_cancelled_check_too(self) -> None:
        cancelled = {"id": "CR_c", "conclusion": "CANCELLED"}
        self.assertTrue(gg.wants_steps(cancelled))
        self.assertTrue(gg.wants_steps({"id": "CR_f", "conclusion": "FAILURE"}))
        self.assertFalse(gg.wants_steps({"id": "CR_s", "conclusion": "SUCCESS"}))
        self.assertFalse(gg.wants_steps({"conclusion": "CANCELLED"}))  # no node id to ask for

    def test_headline_agrees_with_the_verdict(self) -> None:
        """manaflow-ai/cmux#14667: GitHub's rollup was FAILURE for a Web complexity run cancelled
        between two passing ones, and the headline printed that next to result: success."""
        def web(run: int, conclusion: str) -> dict:
            return {**check_run("Web complexity", conclusion=conclusion), "databaseId": run, "isRequired": True,
                    "checkSuite": {"conclusion": conclusion, "workflowRun": {"databaseId": run, "workflow": {
                        "name": "Web complexity", "databaseId": 77}}}}
        lint = {**check_run("lint", conclusion="CANCELLED"), "isRequired": False}
        node = pr_node(1, rollup="FAILURE", checks=[web(1, "SUCCESS"), web(2, "CANCELLED"), web(3, "SUCCESS"),
                                                     {**check_run("build"), "isRequired": True}, lint])
        node["mergeStateStatus"] = "CLEAN"
        data = gg.parse_pr(node)
        self.assertEqual(gg.verdict("pr", data, "required"), 0)
        text = gg.summary("pr:o/r#1", {"data": data}, "required")
        self.assertIn("checks SUCCESS: 2 passed, 1 failed, 0 pending (of 3, 2 required); GitHub's rollup says FAILURE", text)
        self.assertIn("cancelled: lint (not required)", text)
        self.assertIn("superseded: Web complexity / Web complexity cancelled, replaced by a newer attempt or run", text)
        self.assertNotIn("checks FAILURE", text)
        # the cancelled optional lint fails every other mode, and the headline says so
        self.assertEqual(gg.verdict("pr", data, "done"), 1)
        self.assertIn("checks FAILURE: 2 passed, 1 failed", gg.summary("pr:o/r#1", {"data": data}, "done"))
        # the headline follows the mode the wait used: only `required` ignores optional checks still running
        running = gg.parse_pr(pr_node(2, rollup="PENDING", checks=[{**check_run("build"), "isRequired": True},
                                                                    {**check_run("slow", "IN_PROGRESS", None)}]))
        self.assertIn("checks SUCCESS", gg.summary("pr:o/r#2", {"data": running}, "required"))
        self.assertIn("checks PENDING", gg.summary("pr:o/r#2", {"data": running}, "green"))
        self.assertIn("checks PENDING", gg.summary("pr:o/r#2", {"data": running}, "done"))
        self.assertIsNone(gg.verdict("pr", running, "green"))

    def test_subrouter_612_two_optional_reds_are_a_failure(self) -> None:
        """manaflow-ai/subrouter#612 merged red: `wait pr` printed result: success over
        "21 passed, 2 failed", because only the one required check (CLA Assistant v3) decided.
        The rollup below is that PR's head commit's, as GitHub returned it."""
        rows = [  # name, workflow, conclusion, required
            ("Validate CLA trigger", "CLA policy workflow", "SKIPPED", False),
            ("Detect changed paths", "CI", "SUCCESS", False),
            ("Validate CLA trigger", "CLA policy workflow", "SUCCESS", False),
            ("Validate CLA trigger", "CLA policy workflow", "SUCCESS", False),
            ("Unit tests", "CI", "FAILURE", False),
            ("Lock merged pull request", "CLA policy workflow", "SKIPPED", False),
            ("Lock merged pull request", "CLA policy workflow", "SKIPPED", False),
            ("Lock merged pull request", "CLA policy workflow", "SUCCESS", False),
            ("CLA signer gate", "CLA policy workflow", "SKIPPED", False),
            ("CLA signer gate", "CLA policy workflow", "SKIPPED", False),
            ("CLA signer gate", "CLA policy workflow", "SKIPPED", False),
            ("Deploy scripts", "CI", "SUCCESS", False),
            ("CLA ledger writer", "CLA policy workflow", "SKIPPED", False),
            ("Shadow rehearsal", "CI", "SUCCESS", False),
            ("CLA ledger writer", "CLA policy workflow", "SUCCESS", False),
            ("CLA ledger writer", "CLA policy workflow", "SUCCESS", False),
            ("CLA Assistant v3", "CLA policy workflow", "SKIPPED", True),
            ("Cross-compile release targets (darwin)", "CI", "SUCCESS", False),
            ("CLA Assistant v3", "CLA policy workflow", "SUCCESS", True),
            ("CLA Assistant v3", "CLA policy workflow", "SUCCESS", True),
            ("Cross-compile release targets (linux)", "CI", "SUCCESS", False),
            ("Cross-compile release targets (windows)", "CI", "SUCCESS", False),
            ("Cross-compile release targets (freebsd)", "CI", "SUCCESS", False),
            ("Cross-compile release targets (netbsd-openbsd)", "CI", "SUCCESS", False),
            ("Rerun failed CLA check", "CLA policy workflow", "SKIPPED", False),
            ("Rerun failed CLA check", "CLA policy workflow", "SKIPPED", False),
            ("Rerun failed CLA check", "CLA policy workflow", "SKIPPED", False),
            ("Linux client artifact (exact head)", "CI", "SUCCESS", False),
            ("Docker local and team profiles", "CI", "SUCCESS", False),
            ("macOS LaunchAgent transaction", "CI", "SUCCESS", False),
            ("Build, vet, test", "CI", "FAILURE", False),
            ("cubic · AI code reviewer", None, "NEUTRAL", False),
            ("Socket Security: Project Report", None, "SUCCESS", False),
            ("Socket Security: Pull Request Alerts", None, "SUCCESS", False),
        ]
        checks = []
        for index, (name, workflow, conclusion, required) in enumerate(rows):
            run = {"databaseId": 1000 + index, "workflow": {"name": workflow, "databaseId": 7}} if workflow else None
            checks.append({**check_run(name, conclusion=conclusion), "databaseId": index, "isRequired": required,
                           "checkSuite": {"conclusion": conclusion, "workflowRun": run}})
        checks.append({"__typename": "StatusContext", "context": "CodeRabbit", "state": "SUCCESS",
                       "targetUrl": "https://ci/coderabbit", "isRequired": False})
        node = pr_node(612, rollup="FAILURE", checks=checks)
        node["mergeStateStatus"] = "CLEAN"
        data = gg.parse_pr(node)
        for until in ("green", "done"):
            self.assertEqual(gg.verdict("pr", data, until), 1, until)
            text = gg.summary("pr:manaflow-ai/subrouter#612", {"data": data}, until)
            self.assertIn("checks FAILURE: 21 passed, 2 failed, 0 pending (of 23, 1 required)", text)
            self.assertIn("failed: CI / Unit tests (not required)", text)
            self.assertIn("failed: CI / Build, vet, test (not required)", text)
        # only an explicit --until required keeps the old reading
        self.assertEqual(gg.verdict("pr", data, "required"), 0)
        # neutral and skipped checks are not failures, and a pending one is not a success
        quiet = [c for c in checks if c.get("conclusion") != "FAILURE"]
        self.assertEqual(gg.verdict("pr", gg.parse_pr({**pr_node(1, rollup="SUCCESS", checks=quiet),
                                                      "mergeStateStatus": "CLEAN"}), "green"), 0)
        slow = quiet + [{**check_run("Unit tests", "IN_PROGRESS", None), "isRequired": False}]
        self.assertIsNone(gg.verdict("pr", gg.parse_pr({**pr_node(1, rollup="PENDING", checks=slow),
                                                       "mergeStateStatus": "CLEAN"}), "green"))


    # -- comments

    def comments(self, *items: tuple[int, str, str, float]) -> dict:
        return {"url": "https://github.com/t/s/issues/454", "state": "OPEN", "type": "Issue", "total": len(items),
                "comments": [{"id": i, "author": a, "body": b, "createdAt": iso(t), "url": f"u{i}"}
                             for i, a, b, t in items]}

    def wait_comment(self, fill: dict | None = None, timeout: float = 0.5, **kw) -> tuple[int, str]:
        key = "comment:t/s#454"
        timer = threading.Timer(0.1, lambda: self.put(key, fill)) if fill else None
        if timer:
            timer.start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = gg.cmd_wait_comment("t/s#454", timeout, False, **kw)
        if timer:
            timer.join()
        return code, out.getvalue()

    def test_wait_comment(self) -> None:
        now = time.time()
        receipt = "callsign-receipt/v0\nstatus: accepted\nrequest-comment: https://github.com/t/s/issues/454#issuecomment-100"
        old = (100, "someone", "/callsign reserve Quill", now - 600)
        old_receipt = (101, "github-actions", receipt, now - 590)
        # by default only comments after the wait began count
        self.put("comment:t/s#454", self.comments(old, old_receipt))
        code, text = self.wait_comment(timeout=0.3, author="github-actions[bot]")
        self.assertEqual(code, 2)
        self.assertIn("0 comments since the wait began, none matched; to include a reply posted before", text)
        # --since the request comment's URL finds the receipt that came before the wait
        code, text = self.wait_comment(timeout=0.3, author="github-actions[bot]", match=r"issuecomment-100\b",
                                       since="https://github.com/t/s/issues/454#issuecomment-100")
        self.assertEqual(code, 0)
        self.assertIn("comment 101 by github-actions", text)
        self.assertIn("  status: accepted", text)
        self.assertEqual(self.wait_comment(timeout=0.3, since="10m", author="github-actions")[0], 0)
        self.assertEqual(self.wait_comment(timeout=0.3, since="101")[0], 2)
        self.assertEqual(self.wait_comment(timeout=0.3, since="5m", author="github-actions")[0], 2)
        # a reply that arrives while waiting
        reply = (102, "github-actions", receipt.replace("100", "102"), time.time() + 1)
        code, _ = self.wait_comment(self.comments(old, old_receipt, reply), author="github-actions[bot]",
                                    match="issuecomment-102")
        self.assertEqual(code, 0)
        self.assertEqual(self.wait_comment(self.comments(reply), timeout=0.3, author="someone")[0], 2)
        with self.assertRaises(gg.WatchError):
            self.wait_comment(match="(")
        with self.assertRaises(gg.WatchError):
            gg.parse_since("yesterday", now)
        self.assertEqual(gg.parse_since("2026-09-25T18:00:00Z", now), (None, gg.iso_epoch("2026-09-25T18:00:00Z")))
        self.assertEqual(gg.parse_since(None, now), (None, now))

    def test_wait_comment_on_a_missing_issue_fails(self) -> None:
        key = "comment:t/s#454"
        timer = threading.Timer(0.1, lambda: self.put(key, None, error="Could not resolve", missing=True))
        timer.start()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gg.cmd_wait_comment("t/s#454", 0.5, False), 1)
        timer.join()

    # -- --sha

    def test_sha_is_validated_and_resolved(self) -> None:
        green = gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a")]))  # head is aaaa...
        for bad in ("xyz1234", "abc", "a" * 41):
            with self.assertRaises(gg.WatchError):
                gg.resolve_sha(bad)
        saved = gg.git_rev_parse
        try:
            gg.git_rev_parse = lambda prefix: None  # not a commit in this checkout
            with self.assertRaisesRegex(gg.WatchError, "not the head GitHub shows \\(aaaaaaaaaaaa\\)"):
                self.wait("pr", "o/r#1", fill=(green, {}), sha="abcdef1")
            self.assertEqual(self.wait("pr", "o/r#1", fill=(green, {}), sha="AAAAAAA")[0], 0)
            gg.git_rev_parse = lambda prefix: prefix + "0" * (40 - len(prefix))  # a local commit not pushed yet
            self.assertEqual(self.wait("pr", "o/r#1", fill=(green, {}), sha="bbbbbbb", timeout=0.3)[0], 2)
        finally:
            gg.git_rev_parse = saved
        self.assertEqual(self.wait("pr", "o/r#1", fill=(green, {}), sha="b" * 40, timeout=0.3)[0], 2)  # pending
        with self.assertRaisesRegex(gg.WatchError, "a run's commit never changes"):
            self.wait("run", "o/r/2", fill=(self.run_doc(1, "success"), {}), sha="d" * 40)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(gg.main(["wait", "pr", "o/r#1", "--sha", "HEAD"]), 64)
            self.assertEqual(gg.main(["wait", "comment", "o/r#1", "--sha", "a" * 40]), 64)
            self.assertEqual(gg.main(["wait", "pr", "o/r#1", "--author", "x"]), 64)
            self.assertEqual(gg.main(["wait", "run", "o/r/1", "--rescue-grace", "-1"]), 64)
        self.assertIn("--sha must be a commit id", err.getvalue())


class OverloadTest(Base):
    """GitHub answers 504 when one GraphQL query takes over ~10 s. Measured on cosmic-gray
    (2026-10-19, 89 watched PRs): 25 PRs per query took 10-11 s and one of two answered 504; 10 took
    4-6 s and 5 took 2-3 s, all 200, every one costing 1 point. Latency, not cost, sets the limit."""

    def overloaded(self, limit: int | None = None, poison: int | None = None):
        """A transport that answers 504 to a PR query with more than `limit` PRs, or that includes
        PR `poison` (one PR heavy enough to time out alone)."""
        sizes = []

        def transport(method, url, headers, body):
            if url.endswith("/graphql"):
                doc = json.loads(body)
                prs = [v for k, v in doc["variables"].items() if k.startswith("n")]
                if "pullRequest(number:" in doc["query"]:
                    sizes.append(len(prs))
                    if (limit is not None and len(prs) > limit) or (poison is not None and poison in prs):
                        return 504, {}, b'{"message": "We couldn\'t respond to your request in time."}'
            return self.gh(method, url, headers, body)
        self.daemon.transport = transport
        return sizes

    def watch(self, count: int) -> list[str]:
        keys = []
        for n in range(1, count + 1):
            self.gh.prs[("o", "r", n)] = pr_node(n)
            keys.append(f"pr:o/r#{n}")
            gg.register(keys[-1])
        return keys

    def test_a_504_halves_the_batch_and_retries_the_halves(self) -> None:
        keys = self.watch(30)
        sizes = self.overloaded(limit=6)
        self.daemon.tick(NOW)
        for key in keys:
            entry = gg.read_cache(key)
            self.assertIsNotNone(entry["data"], key)  # every PR read on this tick, none left stale
            self.assertIsNone(entry["error"], key)
        self.assertLessEqual(self.daemon.batch["pr"], 6)
        self.assertLessEqual(max(sizes[1:]), 5)  # 10 failed: halves of 5, then batches of 5
        self.assertEqual(self.daemon.status["batch"]["pr"], self.daemon.batch["pr"])

    def test_the_batch_starts_small_and_grows_back_slowly(self) -> None:
        self.watch(40)
        sizes = self.overloaded()
        self.daemon.tick(NOW)
        self.assertLessEqual(max(sizes), 10)  # 25 PRs per query took 10-11 s: too close to the limit
        self.daemon.batch["pr"] = 2
        for cycle in range(1, 4):
            self.daemon.tick(NOW + cycle * gg.CYCLE)
        self.assertGreater(self.daemon.batch["pr"], 2)
        self.assertLessEqual(self.daemon.batch["pr"], gg.GQL_BATCH)

    def test_one_heavy_pr_does_not_keep_the_others_stale(self) -> None:
        keys = self.watch(12)
        sizes = self.overloaded(poison=7)
        self.daemon.tick(NOW)
        for key in keys:
            entry = gg.read_cache(key)
            if key == "pr:o/r#7":
                self.assertIn("HTTP 504", entry["error"])
            else:
                self.assertIsNotNone(entry["data"], key)
        self.assertEqual(set(self.daemon.heavy), {"pr:o/r#7"})
        self.assertEqual(self.daemon.batch["pr"], 5)  # halved once on this tick, not once per split
        # from now on #7 is read alone and the rest batch normally: one query, not a cascade of splits
        sizes.clear()
        self.daemon.tick(NOW + gg.CYCLE)
        self.assertEqual(sizes, [1, 5, 5, 1])  # #7 alone, then the other 11 in batches of 5
        for cycle in range(2, 12):
            self.daemon.tick(NOW + cycle * gg.CYCLE)
        self.assertGreater(self.daemon.batch["pr"], 5)  # it grows back while #7 keeps failing alone

    def transport(self, answer):
        """Answer PR queries with `answer(prs)` -> (status, body) or None for the fake's normal answer."""
        sizes = []

        def transport(method, url, headers, body):
            if url.endswith("/graphql"):
                doc = json.loads(body)
                if "pullRequest(number:" in doc["query"]:
                    prs = [v for k, v in doc["variables"].items() if k.startswith("n")]
                    sizes.append(len(prs))
                    out = answer(prs)
                    if out is not None:
                        return out[0], {}, out[1]
            return self.gh(method, url, headers, body)
        self.daemon.transport = transport
        return sizes

    def test_a_timeout_reported_as_200_with_null_data_halves_too(self) -> None:
        keys = self.watch(20)
        timeout = json.dumps({"data": None, "errors": [{"message": "Something went wrong while executing your "
                                                         "query. This may be the result of a timeout"}]}).encode()
        self.transport(lambda prs: (200, timeout) if len(prs) > 5 else None)
        self.daemon.tick(NOW)
        self.assertLessEqual(self.daemon.batch["pr"], 5)
        self.assertTrue(all(gg.read_cache(k)["data"] for k in keys))

    def test_a_timeout_on_a_short_tail_batch_halves_the_batch_not_the_tail(self) -> None:
        self.watch(23)  # batches of 10, 10, 3
        failed = []
        self.transport(lambda prs: None if len(prs) != 3 or failed else (failed.append(1) or (504, b"{}")))
        self.daemon.tick(NOW)
        self.assertEqual(self.daemon.batch["pr"], 5)

    def test_a_502_is_split_and_retried_without_shrinking(self) -> None:
        keys = self.watch(10)
        failed = []
        self.transport(lambda prs: None if failed else (failed.append(1) or (502, b"{}")))
        self.daemon.tick(NOW)
        self.assertEqual(self.daemon.batch["pr"], gg.GQL_BATCH_START)
        self.assertTrue(all(gg.read_cache(k)["data"] for k in keys))

    def test_a_heavy_pr_backs_off_and_rejoins_once_it_answers(self) -> None:
        self.watch(6)
        poison = [3]
        sizes = self.transport(lambda prs: (504, b"{}") if poison and poison[0] in prs else None)
        lone = []
        for cycle in range(0, 12):
            sizes.clear()
            self.daemon.tick(NOW + cycle * gg.CYCLE)
            lone.append(sizes.count(1))
        self.assertEqual(self.daemon.heavy["pr:o/r#3"]["fails"] >= gg.HEAVY_AFTER, True)
        # read alone on each of the first cycles, then only every LEAN_FACTOR cycles
        self.assertLess(sum(lone[gg.HEAVY_AFTER + 1:]), len(lone) - gg.HEAVY_AFTER - 1)
        poison.clear()  # it answers again: two lone answers put it back into the batches
        for cycle in range(12, 12 + 2 * gg.LEAN_FACTOR + 2):
            self.daemon.tick(NOW + cycle * gg.CYCLE)
        self.assertNotIn("pr:o/r#3", self.daemon.heavy)

    def test_a_pr_never_read_counts_in_the_oldest_data(self) -> None:
        self.watch(1)
        self.overloaded(poison=1)
        self.daemon.tick(NOW)
        self.daemon.tick(NOW + 5 * gg.CYCLE)
        self.assertEqual(gg.read_json(gg.BASE / "daemon.json")["oldestData"]["pr"], 5 * gg.CYCLE)

    def test_the_oldest_data_age_is_reported(self) -> None:
        keys = self.watch(3)
        self.daemon.tick(NOW)
        self.overloaded(limit=0)  # every PR query fails from now on
        self.daemon.tick(NOW + 5 * gg.CYCLE)
        status = gg.read_json(gg.BASE / "daemon.json")
        self.assertEqual(status["oldestData"]["pr"], 5 * gg.CYCLE)
        self.assertIsNone(status["oldestData"]["run"])
        lock = open(gg.BASE / "daemon.lock", "a+")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(gg, "daemon_stale", return_value=None):
                gg.cmd_budget(False)
            self.assertIn("oldest data of an open watch: PRs 5m", out.getvalue())
        finally:
            lock.close()


class BudgetOutputTest(Base):
    def test_budget_shows_both_buckets_the_paths_and_the_split(self) -> None:
        self.gh.prs[("o", "r", 1)] = pr_node(1)
        self.gh.runs[7] = {"id": 7, "status": "queued", "html_url": "u"}
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 300, "reset": NOW + 3000},
                              "graphql": {"limit": 5000, "remaining": 4000, "reset": NOW + 1800}}
        gg.register("pr:o/r#1")
        gg.register("run:o/r/7")
        self.daemon.tick(NOW)
        status = gg.read_json(gg.BASE / "daemon.json")
        self.assertEqual(status["paths"], {"pr": "graphql", "run": "graphql", "comment": "graphql", "head": "graphql"})
        self.assertEqual(status["reads"]["run"], {"graphql": 1, "rest": 0})
        self.assertEqual(status["reads"]["pr"], {"graphql": 1, "rest": 0})
        lock = open(gg.BASE / "daemon.lock", "a+")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(gg, "daemon_stale", return_value=None):
                gg.cmd_budget(False)
            text = out.getvalue()
            self.assertRegex(text, r"rest +\d+/5000 left, resets \S+, [\d.]+/min to spend until then")
            self.assertRegex(text, r"graphql +\d+/5000 left")
            self.assertIn("reading: PRs over GraphQL, runs over GraphQL, comments over GraphQL", text)
            self.assertIn("reads in the last hour: PRs 1 GraphQL / 0 REST; runs 1 GraphQL / 0 REST", text)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(gg, "daemon_stale", return_value=None):
                gg.cmd_budget(True)
            last = json.loads(out.getvalue())["last"]
            self.assertEqual(last["paths"]["run"], "graphql")
            self.assertIn("perMinute", last)
        finally:
            lock.close()


# ---------------------------------------------------------------- hangs (10-20: one daemon silent for 20 minutes)


class Trickle:
    """A local HTTP server that answers 200 and then sends its body one byte every `every` seconds:
    each read returns well within any socket timeout, so only a whole-request deadline ends it."""

    def __init__(self, every: float = 0.1) -> None:
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.every, self.stop = every, threading.Event()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.drip, args=(conn,), daemon=True).start()

    def drip(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n")
                while not self.stop.is_set():
                    conn.sendall(b" ")
                    time.sleep(self.every)
            except OSError:
                return

    def close(self) -> None:
        self.stop.set()
        self.server.close()


class HardDeadlineTest(unittest.TestCase):
    """urllib's timeout bounds each socket read, not the request: a server that keeps sending a
    byte now and then holds a request (and the daemon's whole tick) for as long as it likes."""

    def setUp(self) -> None:
        self.server = Trickle()
        self.addCleanup(self.server.close)

    def test_github_request_ends_at_its_deadline_while_bytes_still_trickle(self) -> None:
        api = f"http://127.0.0.1:{self.server.port}"
        began = time.monotonic()
        with mock.patch.object(gg, "API", api), mock.patch.object(gg, "HTTP_DEADLINE", 1.0), \
                mock.patch.object(gg, "SOCKET_TIMEOUT", 5), self.assertRaises(gg.WatchError) as caught:
            gg.http("GET", api + "/rate_limit", {})
        took = time.monotonic() - began
        self.assertIn("DeadlineTimeout", str(caught.exception))
        self.assertGreaterEqual(took, 0.9)
        self.assertLess(took, 3.0)
        self.assertIsNotNone(gg.SLOW.search(str(caught.exception)), "a deadline counts as a slow answer")

    def test_controller_request_ends_at_its_deadline(self) -> None:
        ctl = gg.Controller(f"http://127.0.0.1:{self.server.port}", None)
        began = time.monotonic()
        with mock.patch.object(gg, "CONTROLLER_DEADLINE", 1.0), self.assertRaises(gg.WatchError):
            ctl.events(None)
        self.assertLess(time.monotonic() - began, 3.0)

    def test_the_deadline_leaves_no_alarm_behind(self) -> None:
        with gg.hard_deadline(0.2):
            pass
        time.sleep(0.4)  # an armed alarm would raise DeadlineTimeout here
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))


class BoundedSubprocessTest(unittest.TestCase):
    """subprocess.run(timeout=) kills the child, then waits for its pipes: a grandchild that
    inherited them (a credential helper, a pager) keeps run() blocked for as long as it lives."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def hanging_tool(self, name: str) -> str:
        path = Path(self.tmp.name) / name
        path.write_text("#!/bin/sh\nsleep 60 &\nsleep 60\n")  # the background sleep holds stdout
        path.chmod(0o755)
        return os.fspath(path)

    def test_a_grandchild_holding_the_pipes_does_not_outlive_the_deadline(self) -> None:
        began = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            gg.run_bounded([self.hanging_tool("tool")], 0.5)
        self.assertLess(time.monotonic() - began, 5.0)

    def test_gh_auth_token_is_bounded(self) -> None:
        gh = self.hanging_tool("gh")
        began = time.monotonic()
        with mock.patch.object(gg, "find_gh", return_value=gh), mock.patch.object(gg, "SUBPROCESS_DEADLINE", 0.5), \
                self.assertRaises(gg.WatchError) as caught:
            gg.gh_token()
        self.assertIn("TimeoutExpired", str(caught.exception))
        self.assertLess(time.monotonic() - began, 5.0)

    def test_an_answer_within_the_deadline_is_returned(self) -> None:
        result = gg.run_bounded(["sh", "-c", "echo out; echo err >&2; exit 3"], 10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "out\n", "err\n"))


class TickBudgetTest(Base):
    """A tick reads every due entry one request after another. With dozens of PRs over REST (each
    several requests) and slow answers, one tick ran for many minutes without a heartbeat. Past
    TICK_BUDGET a tick starts no request; what it did not reach goes first on the next tick."""

    def spend_budget_after(self, calls: int) -> None:
        inner = self.daemon.transport
        count = [0]

        def transport(method, url, headers, body):
            count[0] += 1
            if count[0] == calls:
                self.daemon.tick_deadline = float("-inf")  # the budget is spent once this answer is in
            return inner(method, url, headers, body)
        self.daemon.transport = transport

    def test_runs_left_over_go_first_next_tick_without_an_error(self) -> None:
        keys = []
        for run_id in range(1, 6):
            self.gh.runs[run_id] = {"id": run_id, "name": "CI", "status": "in_progress", "conclusion": None, "html_url": "u"}
            keys.append(gg.parse_key("run", f"o/r/{run_id}"))
            gg.register(keys[-1])
        self.spend_budget_after(2)
        status = self.daemon.tick(NOW)
        read = [k for k in keys if gg.read_cache(k)]
        self.assertEqual(len(read), 2, "two runs read before the budget ran out")
        self.assertEqual(status["deferred"], 3)
        self.assertEqual(self.daemon.tick_deadline, float("inf"), "the budget ends with the tick")
        self.assertTrue((gg.BASE / "daemon.json").exists(), "the heartbeat is written all the same")
        self.daemon.transport = self.gh
        status = self.daemon.tick(NOW + gg.TICK)  # not a full cycle: only the deferred runs are due
        self.assertEqual(status["fetched"]["run"], 3)
        self.assertEqual(status["deferred"], 0)
        for k in keys:
            self.assertIsNone(gg.read_cache(k)["error"], k)

    def test_pr_batches_left_over_go_first_next_tick(self) -> None:
        keys = []
        for n in range(1, 31):
            self.gh.prs[("o", "r", n)] = pr_node(n)
            keys.append(f"pr:o/r#{n}")
            gg.register(keys[-1])
        self.spend_budget_after(1)  # one GraphQL query of 10 PRs, then the budget is spent
        status = self.daemon.tick(NOW)
        self.assertEqual(sum(1 for k in keys if gg.read_cache(k)), 10)
        self.assertEqual(status["deferred"], 20)
        self.assertEqual(len(self.gh.gql_calls()), 1)
        self.daemon.transport = self.gh
        status = self.daemon.tick(NOW + gg.TICK)
        self.assertEqual(status["fetched"]["pr"], 20)
        self.assertTrue(all(gg.read_cache(k)["data"] for k in keys))

    def test_failed_steps_left_unread_put_their_pr_first_next_tick(self) -> None:
        failed = {**check_run("build", conclusion="FAILURE"), "id": "CR_1"}
        self.gh.prs[("o", "r", 1)] = pr_node(1, rollup="FAILURE", checks=[failed])
        self.gh.prs[("o", "r", 2)] = pr_node(2)
        self.gh.steps["CR_1"] = [{"name": "Run selected tests", "conclusion": "FAILURE"}]
        gg.register("pr:o/r#1")
        gg.register("pr:o/r#2")
        self.spend_budget_after(1)  # the PR query answers; the steps query is never started
        status = self.daemon.tick(NOW)
        self.assertEqual(self.gh.step_queries, [])
        self.assertNotIn("failedSteps", gg.read_cache("pr:o/r#1")["data"]["checks"][0])
        self.assertEqual(status["deferred"], 1, "only the PR whose steps went unread")
        self.daemon.transport = self.gh
        self.daemon.tick(NOW + gg.TICK)  # not a full cycle: the deferred PR is read again, with its steps
        self.assertEqual(self.gh.step_queries, [["CR_1"]])
        self.assertEqual(gg.read_cache("pr:o/r#1")["data"]["checks"][0]["failedSteps"], ["Run selected tests"])


CHILD_PRELUDE = """
import importlib.machinery, importlib.util, json, os, signal, sys, time
from pathlib import Path
loader = importlib.machinery.SourceFileLoader("glaeda_gh", sys.argv[1])
spec = importlib.util.spec_from_loader("glaeda_gh", loader)
gg = importlib.util.module_from_spec(spec)
sys.modules["glaeda_gh"] = gg
loader.exec_module(gg)
gg.BASE = Path(sys.argv[3])
"""


class WatchdogTest(unittest.TestCase):
    def test_check_exits_only_once_the_loop_is_overdue(self) -> None:
        clock, exits = [100.0], []
        dog = gg.Watchdog(300, clock=lambda: clock[0], exit=exits.append)
        clock[0] += 299
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(dog.check())
            dog.beat()
            clock[0] += 300
            self.assertFalse(dog.check())
            clock[0] += 1.5
            self.assertTrue(dog.check())
        self.assertEqual(exits, [gg.EXIT_WATCHDOG])

    def test_the_watchdog_thread_never_takes_the_deadline_signal(self) -> None:
        """SIGALRM must reach the main thread's blocking read; the watchdog thread blocks it."""
        seen: list[set] = []

        class Probe(gg.Watchdog):
            def check(self) -> bool:
                seen.append(signal.pthread_sigmask(signal.SIG_BLOCK, []))
                return True
        Probe(300).start().join(5)
        self.assertEqual(len(seen), 1)
        self.assertIn(signal.SIGALRM, seen[0])
        self.assertNotIn(signal.SIGALRM, signal.pthread_sigmask(signal.SIG_BLOCK, []))

    def test_a_loop_stuck_holding_stdout_still_ends(self) -> None:
        """End to end: the tick blocks inside a write to a pipe nobody reads, so it holds the stdout
        lock the watchdog's own log line needs. The faulthandler backstop ends the process anyway."""
        with tempfile.TemporaryDirectory() as tmp:
            code = CHILD_PRELUDE + """
gg.WATCHDOG_AFTER = 1.0
gg.WATCHDOG_BACKSTOP = 1.0
class Stuck:
    def tick(self):
        while True:
            print("x" * 65536, flush=True)  # blocks once the pipe is full, holding the stream lock
gg.Daemon = Stuck
gg.serve(foreground_ok=True)
"""
            began = time.monotonic()
            with subprocess.Popen([sys.executable, "-c", code, os.fspath(ROOT / "scripts" / "glaeda-gh"), "serve", tmp],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as child:
                try:
                    code_ = child.wait(timeout=30)
                finally:
                    child.kill()
            self.assertLess(time.monotonic() - began, 20)
            self.assertEqual(code_, 1, "faulthandler's exit, not a hang")

    def test_a_hung_tick_ends_the_process_and_frees_the_lock(self) -> None:
        """End to end: serve() with a tick stuck in a blocking call exits EXIT_WATCHDOG on its own,
        names where it was stuck, and releases the lock for the restarted daemon."""
        with tempfile.TemporaryDirectory() as tmp:
            code = CHILD_PRELUDE + """
gg.WATCHDOG_AFTER = 1.0
class Stuck:
    def tick(self):
        time.sleep(120)  # a read no deadline reaches
gg.Daemon = Stuck
gg.serve(foreground_ok=True)
"""
            began = time.monotonic()
            child = subprocess.run([sys.executable, "-c", code, os.fspath(ROOT / "scripts" / "glaeda-gh"), "serve", tmp],
                                   capture_output=True, text=True, timeout=60)
            self.assertLess(time.monotonic() - began, 30)
            self.assertEqual(child.returncode, gg.EXIT_WATCHDOG, child.stderr)
            self.assertIn("watchdog", child.stderr)
            self.assertIn("in tick", child.stderr, "the stack dump names the stuck function")
            events = [json.loads(line)["event"] for line in child.stdout.splitlines() if line.startswith("{")]
            self.assertEqual(events[0], "start")
            self.assertIn("watchdog", events)
            saved = gg.BASE
            try:
                gg.BASE = Path(tmp)
                self.assertFalse(gg.lock_held())
            finally:
                gg.BASE = saved


class TakeoverTest(unittest.TestCase):
    """A daemon waiting for the lock stops a holder whose heartbeat went stale, after checking that
    the holder is a live glaeda-gh serve."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = gg.BASE
        gg.BASE = Path(self.tmp.name)
        self.addCleanup(setattr, gg, "BASE", self.saved)
        self.children: list[subprocess.Popen] = []
        self.addCleanup(self.reap)

    def reap(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()

    def holder(self, heartbeat_age: float, ignore_term: bool = False) -> subprocess.Popen:
        """A glaeda-gh serve that took the lock and then hung (it never writes another heartbeat)."""
        code = CHILD_PRELUDE + f"""
if {ignore_term!r}:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
lock = open(gg.BASE / "daemon.lock", "a+")
assert gg.try_lock(lock)
gg.record_holder(lock)
lock.seek(0)
lock.truncate()
lock.write(json.dumps({{"pid": os.getpid(), "since": time.time() - {heartbeat_age} - 60}}))  # took it long ago
lock.flush()
gg.write_json(gg.BASE / "daemon.json", {{"pid": os.getpid(), "at": time.time() - {heartbeat_age}, "heartbeat": 30}})
print("ready", flush=True)
time.sleep(120)
"""
        child = subprocess.Popen([sys.executable, "-c", code, os.fspath(ROOT / "scripts" / "glaeda-gh"), "serve",
                                  self.tmp.name], stdout=subprocess.PIPE, text=True)
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        return child

    def lock(self):
        lock = open(gg.BASE / "daemon.lock", "a+")
        self.addCleanup(lock.close)
        return lock

    def test_a_stale_holder_is_stopped_and_the_lock_taken(self) -> None:
        child = self.holder(gg.STALE_HEARTBEAT + 60)
        lock = self.lock()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gg.acquire_lock(lock, sleep=lambda s: self.fail("took over without waiting a retry"))
        self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)
        self.assertEqual(gg.lock_holder()["pid"], os.getpid())
        self.assertIn('"takeover"', out.getvalue())

    def test_a_holder_ignoring_sigterm_is_killed(self) -> None:
        child = self.holder(gg.STALE_HEARTBEAT + 60, ignore_term=True)
        lock = self.lock()
        with mock.patch.object(gg, "TAKEOVER_GRACE", 0.5), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(gg.take_over(lock))
        self.assertEqual(child.wait(timeout=10), -signal.SIGKILL)

    def test_a_holder_with_a_fresh_heartbeat_is_left_alone(self) -> None:
        child = self.holder(5)
        lock = self.lock()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(gg.take_over(lock))
        time.sleep(0.3)
        self.assertIsNone(child.poll())
        self.assertFalse(gg.try_lock(lock))

    def test_a_recorded_pid_that_is_not_a_serve_is_left_alone(self) -> None:
        other = subprocess.Popen(["sleep", "60"], stdout=subprocess.PIPE)
        self.children.append(other)
        gg.write_json(gg.BASE / "daemon.lock", {"pid": other.pid, "since": time.time() - 10 * gg.STALE_HEARTBEAT})
        self.assertFalse(gg.is_serve(other.pid))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(gg.take_over(self.lock()))
        self.assertIsNone(other.poll())

    def test_a_dead_or_unrecorded_holder_is_not_taken_over(self) -> None:
        gg.write_json(gg.BASE / "daemon.lock", {"pid": 2 ** 22 + 12345, "since": 0})
        self.assertFalse(gg.is_serve(2 ** 22 + 12345))
        (gg.BASE / "daemon.lock").write_text("")  # an older holder writes no record
        self.assertIsNone(gg.lock_holder())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(gg.take_over(self.lock()))

    def test_silence_counts_from_the_holders_own_heartbeat(self) -> None:
        now = time.time()
        gg.write_json(gg.BASE / "daemon.lock", {"pid": 4242, "since": now - 5000})
        gg.write_json(gg.BASE / "daemon.json", {"pid": 4242, "at": now - 20})
        self.assertAlmostEqual(gg.lock_holder(now)["silent"], 20)
        gg.write_json(gg.BASE / "daemon.json", {"pid": 999, "at": now - 20})  # another process's heartbeat
        self.assertAlmostEqual(gg.lock_holder(now)["silent"], 5000)


class ServeGuardTest(unittest.TestCase):
    """Sessions ran `timeout 25 glaeda-gh serve` themselves; one took the lock and hung (10-20)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fake_service_tools(self, rc: int) -> dict:
        bin_dir = Path(self.tmp.name) / f"bin{rc}"
        bin_dir.mkdir()
        for tool in ("launchctl", "systemctl"):
            (bin_dir / tool).write_text(f"#!/bin/sh\nexit {rc}\n")
            (bin_dir / tool).chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "XPC_SERVICE_NAME"}
        env["PATH"] = os.fspath(bin_dir) + os.pathsep + env.get("PATH", "")
        env["GLAEDA_GH_DIR"] = os.fspath(Path(self.tmp.name) / "gh")
        return env

    def test_service_loaded_reads_the_service_manager(self) -> None:
        for rc, loaded in ((0, True), (113, False)):
            env = self.fake_service_tools(rc)
            with mock.patch.dict(os.environ, {"PATH": env["PATH"]}):
                self.assertEqual(gg.service_loaded(), loaded, rc)

    def test_a_headless_launch_daemon_counts_as_loaded(self) -> None:
        bin_dir = Path(self.tmp.name) / "headless"
        bin_dir.mkdir()
        (bin_dir / "launchctl").write_text('#!/bin/sh\ncase "$2" in system/*) exit 0;; esac\nexit 113\n')
        (bin_dir / "launchctl").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": os.fspath(bin_dir) + os.pathsep + os.environ["PATH"]}), \
                mock.patch.object(gg.sys, "platform", "darwin"):
            self.assertTrue(gg.service_loaded())

    def test_serve_from_a_shell_is_refused_while_the_service_is_loaded(self) -> None:
        """End to end: the real script, a loaded service, a plain shell."""
        child = subprocess.run([sys.executable, os.fspath(ROOT / "scripts" / "glaeda-gh"), "serve"],
                               env=self.fake_service_tools(0), capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 64, child.stderr)
        self.assertIn("refusing to start", child.stderr)
        self.assertIn("--foreground-ok", child.stderr)
        self.assertFalse((Path(self.tmp.name) / "gh" / "daemon.lock").exists(), "it never touched the lock")

    def test_the_service_itself_and_foreground_ok_start(self) -> None:
        with mock.patch.object(gg, "serve", return_value=0) as serve, \
                mock.patch.object(gg, "service_loaded", return_value=True):
            with mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": gg.LAUNCH_LABEL}):
                self.assertEqual(gg.main(["serve"]), 0)
            with mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": "application.com.mitchellh.ghostty"}):
                self.assertEqual(gg.main(["serve", "--foreground-ok"]), 0)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(gg.main(["serve"]), 64)
        self.assertEqual(serve.call_args_list, [mock.call(False), mock.call(True)])

    def test_no_service_loaded_starts(self) -> None:
        with mock.patch.object(gg, "serve", return_value=0) as serve, \
                mock.patch.object(gg, "service_loaded", return_value=False), \
                mock.patch.object(gg, "under_service", return_value=False):
            self.assertEqual(gg.main(["serve"]), 0)
        serve.assert_called_once_with(False)


class RestartTest(Base):
    """`glaeda-gh restart`: however many sessions run it, the service is restarted once."""

    def setUp(self) -> None:
        super().setUp()
        gg.BASE.mkdir(parents=True)
        self.calls: list[bool] = []
        patcher = mock.patch.object(gg, "restart_service", side_effect=self.fake_restart)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lock: Any = None
        self.addCleanup(lambda: self.lock and self.lock.close())

    def fake_restart(self, hung: bool) -> tuple[bool, str]:
        self.calls.append(hung)
        if self.lock is not None:
            self.lock.close()  # the old daemon is gone
        self.lock = open(gg.BASE / "daemon.lock", "a+")
        fcntl.flock(self.lock, fcntl.LOCK_EX)  # the new daemon takes its lock
        gg.first_heartbeat()  # and writes its heartbeat at once
        return True, "launchctl kickstart"

    def run_restart(self, force: bool = False) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = gg.cmd_restart(force)
        return code, out.getvalue()

    def hold(self, stale: bool) -> None:
        self.lock = open(gg.BASE / "daemon.lock", "a+")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        lag = gg.STALE_HEARTBEAT + 60 if stale else 5
        gg.write_json(gg.BASE / "daemon.json", {"pid": 1, "at": time.time() - lag, "heartbeat": 30})

    def test_down_daemon_is_started_once(self) -> None:
        code, text = self.run_restart()
        self.assertEqual((code, self.calls), (0, [False]), text)
        self.assertIn("restarted the daemon", text)
        self.lock.close()  # down again at once: the next session is told to wait, not to restart
        self.lock = None
        code, text = self.run_restart()
        self.assertEqual((code, self.calls), (0, [False]), text)
        self.assertIn("Do not restart it again", text)

    def test_hung_daemon_is_restarted(self) -> None:
        self.hold(stale=True)
        with mock.patch.object(gg, "lock_holder", return_value=None):
            code, text = self.run_restart()
        self.assertEqual((code, self.calls), (0, [True]), text)
        self.assertEqual(gg.restart_record()["how"], "launchctl kickstart")

    def test_hung_holder_is_stopped_and_not_killed_twice(self) -> None:
        self.hold(stale=True)
        with mock.patch.object(gg, "lock_holder", return_value={"pid": 4321, "silent": gg.STALE_HEARTBEAT + 60}), \
                mock.patch.object(gg, "is_serve", return_value=True), \
                mock.patch.object(gg, "stop_process", return_value=True) as stop:
            code, text = self.run_restart()
        stop.assert_called_once_with(4321)
        self.assertEqual((code, self.calls), (0, [False]), text)  # launchd respawns it: no kickstart -k on top
        self.assertIn("pid 4321", gg.restart_record()["how"])

    def test_a_surviving_hung_holder_is_not_success(self) -> None:
        """A lock still held by the hung daemon proves nothing: success is a new heartbeat."""
        self.hold(stale=True)
        gg.RESTART_SETTLE, saved = 0.3, gg.RESTART_SETTLE
        self.addCleanup(setattr, gg, "RESTART_SETTLE", saved)
        with mock.patch.object(gg, "restart_service", return_value=(True, "launchctl kickstart")), \
                mock.patch.object(gg, "lock_holder", return_value=None):
            code, text = self.run_restart()
        self.assertEqual(code, 3, text)
        self.assertIn("Tell the user", text)
        self.assertFalse(gg.restart_record()["ok"])
        self.assertIsNone(gg.recent_restart())  # a failed restart starts no cooldown
        self.assertEqual(gg.down_message(), gg.DOWN)

    def test_stop_process_escalates_to_sigkill(self) -> None:
        child = subprocess.Popen([sys.executable, "-c",
                                  "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print(1, flush=True); time.sleep(60)"],
                                 stdout=subprocess.PIPE)
        child.stdout.readline()  # SIGTERM is ignored from here on
        saved = gg.TAKEOVER_GRACE
        gg.TAKEOVER_GRACE = 0.5
        reaper = threading.Thread(target=child.wait)
        reaper.start()
        try:
            self.assertTrue(gg.stop_process(child.pid))
        finally:
            gg.TAKEOVER_GRACE = saved
            child.kill()
            reaper.join()
            child.stdout.close()
        self.assertEqual(child.returncode, -signal.SIGKILL)

    def test_healthy_daemon_is_left_alone(self) -> None:
        self.hold(stale=False)
        code, text = self.run_restart()
        self.assertEqual((code, self.calls), (0, []), text)
        self.assertIn("nothing to restart", text)

    def test_concurrent_restarts_restart_once(self) -> None:
        guard = open(gg.BASE / "restart.lock", "a+")
        fcntl.flock(guard, fcntl.LOCK_EX)  # another session is in the middle of a restart
        try:
            code, text = self.run_restart()
        finally:
            guard.close()
        self.assertEqual((code, self.calls), (0, []), text)
        self.assertIn("another session is restarting", text)

    def test_many_sessions_at_once(self) -> None:
        self.hold(stale=True)
        results: list[int] = []
        with mock.patch.object(gg, "lock_holder", return_value=None):
            threads = [threading.Thread(target=lambda: results.append(gg.cmd_restart())) for _ in range(10)]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
        self.assertEqual(self.calls, [True])
        self.assertEqual(results, [0] * 10)

    def test_failing_daemon_is_not_restarted(self) -> None:
        self.hold(stale=False)
        now = time.time()
        gg.write_json(gg.BASE / "daemon.json", {"pid": 1, "at": now, "heartbeat": 30, "failing": {
            "error": "KeyError", "site": "x", "since": now - gg.FAILING_AFTER - 1, "count": 9}})
        code, text = self.run_restart()
        self.assertEqual((code, self.calls), (3, []), text)
        self.assertIn("Restarting it will not fix this", text)


class LaunchdRestartTest(unittest.TestCase):
    def run_with(self, answers: dict[str, int], state: str | None = None, hung: bool = True) -> tuple[tuple, list]:
        seen: list[list[str]] = []

        def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, answers.get(argv[1], 0), "", "")

        with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                mock.patch.object(gg, "launchd_state", return_value=state):
            return gg.restart_service(hung), seen

    def test_kickstart(self) -> None:
        (ok, how), seen = self.run_with({})
        self.assertTrue(ok)
        self.assertEqual([a[:3] for a in seen], [["launchctl", "kickstart", "-k"]])

    def test_bootstrap_is_retried_while_bootout_settles(self) -> None:
        calls = {"n": 0}

        def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
            if argv[1] == "bootstrap":
                calls["n"] += 1
                return subprocess.CompletedProcess(argv, 5 if calls["n"] < 3 else 0, "", "")
            return subprocess.CompletedProcess(argv, 5 if argv[1] == "kickstart" else 0, "", "")

        with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                mock.patch.object(gg, "launchd_state", return_value=None), mock.patch.object(gg.time, "sleep"):
            ok, how = gg.restart_service(True)
        self.assertTrue(ok)
        self.assertEqual(calls["n"], 3)

    def test_linux_resets_a_start_limit_first(self) -> None:
        seen: list[list[str]] = []

        def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(gg.sys, "platform", "linux"), mock.patch.object(gg, "run_bounded", fake):
            self.assertTrue(gg.restart_service(True)[0])
        self.assertEqual([a[2] for a in seen], ["reset-failed", "restart"])

    def test_error_5_reloads_once(self) -> None:
        (ok, how), seen = self.run_with({"kickstart": 5})
        self.assertTrue(ok)
        self.assertEqual([a[1] for a in seen], ["kickstart", "bootout", "bootstrap"])
        self.assertIn("exit 5", how)

    def test_scheduled_respawn_is_left_to_launchd(self) -> None:
        (ok, how), seen = self.run_with({}, state="spawn scheduled", hung=False)
        self.assertTrue(ok)
        self.assertEqual(seen, [])


class RestartHintTest(unittest.TestCase):
    def test_darwin_hints_explain_launchctl_error_5(self) -> None:
        with mock.patch.object(gg.sys, "platform", "darwin"):
            for hint in (gg.start_hint(), gg.start_hint(restart=True)):
                self.assertIn("5: Input/output error", hint)
                self.assertIn("already loaded", hint)
                self.assertIn(f"launchctl bootout gui/$(id -u)/{gg.LAUNCH_LABEL}", hint)
                self.assertIn("Never run glaeda-gh serve yourself", hint)

    def test_stale_message_names_a_hung_holder(self) -> None:
        with mock.patch.object(gg, "lock_holder", return_value={"pid": 4321, "silent": 1235}), \
                mock.patch.object(gg, "is_serve", return_value=True):
            message = gg.stale_message(1235)
        self.assertIn("1235s", message)
        self.assertIn("kill 4321", message)
        with mock.patch.object(gg, "lock_holder", return_value=None):
            self.assertNotIn("kill", gg.stale_message(1235))


SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40


class HeadChecksTest(Base):
    """`glaeda-gh checks`: the daemon reads each head once for every session, within each consumer's budget."""

    def setUp(self) -> None:
        super().setUp()
        self.saved_config = gg.CONSUMERS_CONFIG
        gg.CONSUMERS_CONFIG = Path(self.tmp.name) / "consumers.json"
        self.addCleanup(setattr, gg, "CONSUMERS_CONFIG", self.saved_config)
        for sha in (SHA_A, SHA_B, SHA_C):
            self.gh.heads[("o", "r", sha)] = [check_run("CI / build"), check_run("CI / test", "COMPLETED", "FAILURE")]

    def key(self, sha: str) -> str:
        return gg.parse_key("head", f"o/r@{sha}")

    def policy(self, **doc) -> None:
        gg.write_json(gg.CONSUMERS_CONFIG, doc)

    def test_one_batched_read_serves_every_consumer(self) -> None:
        for consumer in ("session-1", "session-2"):
            for sha in (SHA_A, SHA_B):
                gg.register(self.key(sha), consumer=consumer)
        self.daemon.tick(NOW)
        self.assertEqual(len(self.gh.gql_calls()), 1)  # two heads, two consumers: one query
        entry = gg.read_cache(self.key(SHA_A))
        self.assertEqual(entry["data"]["found"], {"check / CI / build": ["completed", "success"],
                                                  "check / CI / test": ["completed", "failure"]})
        self.assertTrue(entry["data"]["settled"])
        self.daemon.tick(NOW + gg.CYCLE)  # settled data is not read again within its TTL
        self.assertEqual(len(self.gh.gql_calls()), 1)
        ttl, gg.HEAD_SETTLED_TTL = gg.HEAD_SETTLED_TTL, 70  # a rerun can change a settled head: read after the TTL
        self.addCleanup(setattr, gg, "HEAD_SETTLED_TTL", ttl)
        self.daemon.tick(NOW + gg.HEAD_SETTLED_TTL + gg.TICK)
        self.assertEqual(len(self.gh.gql_calls()), 2)

    def test_pending_head_is_read_again_and_an_empty_one_is_not_settled(self) -> None:
        self.gh.heads[("o", "r", SHA_A)] = [check_run("CI / build", "IN_PROGRESS", None)]
        self.gh.heads[("o", "r", SHA_B)] = []
        for sha in (SHA_A, SHA_B):
            gg.register(self.key(sha), consumer="autoland")
        self.daemon.tick(NOW)
        self.assertFalse(gg.read_cache(self.key(SHA_A))["data"]["settled"])
        self.assertFalse(gg.read_cache(self.key(SHA_B))["data"]["settled"])  # no checks yet is not "all finished"
        self.gh.heads[("o", "r", SHA_A)] = [check_run("CI / build")]
        self.daemon.tick(NOW + gg.HEAD_PENDING_TTL + gg.TICK)
        self.assertTrue(gg.read_cache(self.key(SHA_A))["data"]["settled"])

    def test_a_reserved_consumer_is_served_when_the_others_have_spent_their_budget(self) -> None:
        self.policy(reserved={"autoland": 5}, limit=1, pool=1)
        gg.register(self.key(SHA_A), consumer="session-1")
        gg.register(self.key(SHA_B), consumer="session-1")
        self.daemon.tick(NOW)
        read = [sha for sha in (SHA_A, SHA_B) if gg.read_cache(self.key(sha))]
        self.assertEqual(len(read), 1)  # limit 1: the second head waits for the cache
        ledger = gg.read_json(gg.BASE / gg.CONSUMERS_FILE)["consumers"]
        self.assertEqual((ledger["session-1"]["spent"], ledger["session-1"]["limit"]), (1, 1))
        self.assertIsNotNone(ledger["session-1"]["deniedAt"])
        gg.register(self.key(SHA_C), consumer="autoland")
        for sha in (SHA_A, SHA_B):
            gg.register(self.key(sha), consumer="autoland")
        self.daemon.tick(NOW + gg.TICK)
        self.assertTrue(all(gg.read_cache(self.key(sha)) for sha in (SHA_A, SHA_B, SHA_C)))
        ledger = gg.read_json(gg.BASE / gg.CONSUMERS_FILE)["consumers"]
        self.assertEqual(ledger["autoland"]["spent"], 2)  # the head session-1 read is shared, not read twice
        self.assertTrue(ledger["autoland"]["reserved"])

    def test_the_shared_pool_caps_all_unreserved_consumers_together(self) -> None:
        self.policy(reserved={"autoland": 5}, limit=5, pool=2)
        for consumer, sha in (("s1", SHA_A), ("s2", SHA_B), ("s3", SHA_C)):
            gg.register(self.key(sha), consumer=consumer)
        self.daemon.tick(NOW)
        self.assertEqual(sum(1 for sha in (SHA_A, SHA_B, SHA_C) if gg.read_cache(self.key(sha))), 2)

    def test_a_lean_quota_serves_only_reserved_consumers(self) -> None:
        self.daemon.note_budget("graphql", 5000, gg.FLOORS["graphql"][0] - 50, NOW + 1800)
        gg.register(self.key(SHA_A), consumer="session-1")
        gg.register(self.key(SHA_B), consumer="autoland")
        self.daemon.tick(NOW)
        self.assertIsNone(gg.read_cache(self.key(SHA_A)))
        self.assertIsNotNone(gg.read_cache(self.key(SHA_B)))

    def test_spend_survives_a_daemon_restart(self) -> None:
        self.policy(reserved={}, limit=1, pool=5)
        gg.register(self.key(SHA_A), consumer="session-1")
        self.daemon.tick(NOW)
        again = gg.Daemon(transport=self.gh, token=lambda: TOKEN, controller=None)
        gg.register(self.key(SHA_B), consumer="session-1")
        again.tick(NOW + gg.TICK)
        self.assertIsNone(gg.read_cache(self.key(SHA_B)))

    def test_more_than_a_hundred_checks_are_followed_page_by_page(self) -> None:
        self.gh.heads[("o", "r", SHA_A)] = [check_run(f"CI / job {i}") for i in range(130)]
        gg.register(self.key(SHA_A), consumer="autoland")
        self.daemon.tick(NOW)
        self.assertEqual(len(gg.read_cache(self.key(SHA_A))["data"]["found"]), 130)
        self.assertEqual(len(self.gh.gql_calls()), 2)

    def test_reads_that_fail_or_find_nothing_are_not_charged(self) -> None:
        self.policy(reserved={"autoland": 5}, limit=5, pool=5)
        gg.register(self.key("d" * 40), consumer="session-1")  # no such commit
        self.daemon.tick(NOW)
        self.assertEqual(len(self.daemon.head_spend.get("session-1", [])), 0)
        gg.register(self.key(SHA_A), consumer="session-1")
        self.gh.heads[("o", "r", SHA_A)] = [check_run("CI / build")] * 1
        self.daemon.transport = lambda *a: (502, {}, b"{}")
        self.daemon.tick(NOW + 5)
        self.assertEqual(len(self.daemon.head_spend.get("session-1", [])), 0)
        self.daemon.transport = self.gh
        self.daemon.tick(NOW + 5 + 2 * gg.CYCLE)
        self.assertEqual(len(self.daemon.head_spend["session-1"]), 1)

    def test_a_missing_commit_is_asked_about_rarely(self) -> None:
        gg.register(self.key("d" * 40), consumer="autoland")
        self.daemon.tick(NOW)
        before = len(self.gh.gql_calls())
        self.daemon.tick(NOW + 3 * gg.CYCLE)
        self.assertEqual(len(self.gh.gql_calls()), before)

    def test_a_rerun_in_progress_is_not_settled(self) -> None:
        failed = check_run("CI / test", "COMPLETED", "FAILURE")
        rerun = {**check_run("CI / test", "IN_PROGRESS", None), "databaseId": 9}
        failed["databaseId"] = 1
        self.gh.heads[("o", "r", SHA_A)] = [failed, rerun]
        gg.register(self.key(SHA_A), consumer="autoland")
        self.daemon.tick(NOW)
        self.assertFalse(gg.read_cache(self.key(SHA_A))["data"]["settled"])

    def test_a_cut_off_head_is_an_error_not_a_green_verdict(self) -> None:
        self.gh.heads[("o", "r", SHA_A)] = [check_run(f"CI / job {i}") for i in range(130)]
        orig = gg.HEAD_BATCH_MAX_CONTEXTS_PAGES
        gg.HEAD_BATCH_MAX_CONTEXTS_PAGES = 1
        self.addCleanup(setattr, gg, "HEAD_BATCH_MAX_CONTEXTS_PAGES", orig)
        gg.register(self.key(SHA_A), consumer="autoland")
        self.daemon.tick(NOW)
        entry = gg.read_cache(self.key(SHA_A))
        self.assertIsNone(entry["data"])
        self.assertIn("more than", entry["error"])

    def test_an_unknown_commit_is_missing_not_pending(self) -> None:
        gg.register(self.key("d" * 40), consumer="autoland")
        self.daemon.tick(NOW)
        entry = gg.read_cache(self.key("d" * 40))
        self.assertTrue(entry["missing"])

    def test_checks_command_serves_cache_and_reports_what_it_could_not_read(self) -> None:
        gg.BASE.mkdir(parents=True, exist_ok=True)
        lock = open(gg.BASE / "daemon.lock", "a+")
        fcntl.flock(lock, fcntl.LOCK_EX)  # stands in for a running daemon
        self.addCleanup(lock.close)
        now = time.time()
        found = {"check / CI / build": ["completed", "success"]}
        gg.write_json(gg.cache_dir() / gg.file_name(self.key(SHA_A)), {
            "key": self.key(SHA_A), "dataAt": now, "fetchedAt": now,
            "data": {"sha": SHA_A, "found": found, "ids": {}, "settled": True}})
        gg.write_json(gg.BASE / gg.CONSUMERS_FILE, {"consumers": {"session-1": {"spent": 3, "limit": 3, "deniedAt": now + 5}}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = gg.cmd_checks("o/r", [SHA_A, SHA_B], "session-1", 120, 0.3, True)
        doc = json.loads(out.getvalue())
        self.assertEqual(code, gg.EXIT_PARTIAL)
        self.assertEqual(doc["heads"][SHA_A]["found"], found)
        self.assertFalse(doc["heads"][SHA_A]["stale"])
        self.assertEqual(doc["unread"], {SHA_B: "budget spent"})
        # asking registered the consumer's interest for the daemon to pick up
        asked = gg.read_json(gg.watch_dir() / gg.file_name(self.key(SHA_B)))["consumers"]
        self.assertIn("session-1", asked)
        # settled data is served; fully served means exit 0
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gg.cmd_checks("o/r", [SHA_A], "autoland", 120, 0, True), gg.EXIT_OK)


class FileAndPidWaitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        poll = gg.WAIT_FILE_POLL
        gg.WAIT_FILE_POLL = 0.02
        self.addCleanup(setattr, gg, "WAIT_FILE_POLL", poll)

    def wait_file(self, name: str, timeout: float = 0.3, mode: str = "nonempty", contains: str | None = None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = gg.cmd_wait_file(str(self.dir / name), timeout, True, mode, contains)
        return code, json.loads(out.getvalue())

    def later(self, delay: float, action) -> threading.Timer:
        timer = threading.Timer(delay, action)
        timer.start()
        self.addCleanup(timer.cancel)
        return timer

    def test_waits_for_a_nonempty_file(self) -> None:
        target = self.dir / "never1.txt"
        target.write_text("")
        self.later(0.1, lambda: target.write_text("x"))
        code, doc = self.wait_file("never1.txt", 2)
        self.assertEqual((code, doc["result"]), (0, "success"))

    def test_an_empty_file_times_out_unless_exists_is_enough(self) -> None:
        (self.dir / "empty").write_text("")
        self.assertEqual(self.wait_file("empty")[0], gg.EXIT_TIMEOUT)
        self.assertEqual(self.wait_file("empty", mode="exists")[0], 0)

    def test_gone_and_contains_and_glob(self) -> None:
        target = self.dir / "flag"
        target.write_text("x")
        self.later(0.1, target.unlink)
        self.assertEqual(self.wait_file("flag", 2, mode="gone")[0], 0)
        (self.dir / "log").write_text("building\n")
        self.assertEqual(self.wait_file("log", contains="finished")[0], gg.EXIT_TIMEOUT)
        self.later(0.1, lambda: (self.dir / "log").write_text("building\nfinished ok\n"))
        self.assertEqual(self.wait_file("log", 2, contains="finished")[0], 0)
        self.later(0.1, lambda: (self.dir / "never7.txt").write_text("y"))
        code, doc = self.wait_file("never*.txt", 2)
        self.assertEqual((code, doc["path"]), (0, str(self.dir / "never7.txt")))

    def test_waits_for_a_process_to_exit(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
        self.addCleanup(proc.wait)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = gg.cmd_wait_pid(str(proc.pid), 0.05, True)
        self.assertEqual(code, gg.EXIT_TIMEOUT)
        threading.Thread(target=proc.wait, daemon=True).start()  # reap it, as a shell would
        with contextlib.redirect_stdout(out):
            self.assertEqual(gg.cmd_wait_pid(str(proc.pid), 5, True), 0)
        with self.assertRaises(gg.WatchError):
            gg.cmd_wait_pid("abc", 1, False)

    def test_cli_parses_the_new_forms(self) -> None:
        target = self.dir / "ready"
        target.write_text("1")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gg.main(["wait", "file", str(target), "--timeout", "2"]), 0)
            self.assertEqual(gg.main(["wait", "file", str(target), "--gone", "--timeout", "0.1"]), gg.EXIT_TIMEOUT)



if __name__ == "__main__":
    unittest.main()
