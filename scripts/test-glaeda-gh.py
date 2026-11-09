#!/usr/bin/env python3
"""Contract tests for scripts/glaeda-gh, with a fake GitHub transport."""

from __future__ import annotations

import contextlib
import copy
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import signal
import stat
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

# Words that would turn the audit's graphql sentence back into a claim about price. The *last
# paragraph* only, for two measured reasons: over the whole report the bare substring `point` fires
# on a logged path (`repos/o/endpoint-registry`, which reaches the category rows) or on a future
# summary line about "the GraphQL endpoint", and word boundaries alone still fire on a repository
# actually named `points`, which reaches the hottest-targets line. The cost is that a priced
# paragraph printed *before* that one is not caught; the whole-sentence assertion covers a rewrite
# of the sentence itself, which is the likelier way a price comes back.
STALE_COST = (r"\bpoints?\b", r"\bprices?\b", r"\bcheap(er|est)?\b", r"\bper call\b")


def cost_claims(text: str) -> list[str]:
    para = text.split("\n\n")[-1]
    return [pat for pat in STALE_COST if re.search(pat, para)]
loader = importlib.machinery.SourceFileLoader("glaeda_gh", os.fspath(ROOT / "scripts" / "glaeda-gh"))
spec = importlib.util.spec_from_loader("glaeda_gh", loader)
gg = importlib.util.module_from_spec(spec)
sys.modules["glaeda_gh"] = gg
loader.exec_module(gg)
# A host's own upstream.json (a lane VM's) must not turn every Daemon here into an upstream client.
gg.UPSTREAM_CONFIG = Path(tempfile.gettempdir()) / f"glaeda-gh-test-no-upstream-{os.getpid()}.json"
# Nor may a host's own gh-daemon.json change the pacing the tests expect.
gg.DAEMON_CONFIG = Path(tempfile.gettempdir()) / f"glaeda-gh-test-no-daemon-config-{os.getpid()}.json"
gg.FLEET_POLICY = Path(tempfile.gettempdir()) / f"glaeda-gh-test-no-fleet-policy-{os.getpid()}.json"
# Nor may a host's own pacer hear about the tests' secondary 403s.
gg.PACE_CLI = Path(tempfile.gettempdir()) / f"glaeda-gh-test-no-pacer-{os.getpid()}" / "gh"
# Nor may a host's own expected-contexts.json add contexts the tests do not post.
gg.EXPECTED_CONFIG = Path(tempfile.gettempdir()) / f"glaeda-gh-test-no-expected-{os.getpid()}.json"

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


def graphql_requests(query: str, lists: dict[str, int] | None = None) -> int:
    """The requests GitHub counts for QUERY: each connection (a field with first: or last:, or nodes(ids:) over a
    list variable sized in LISTS) costs the product of the sizes of the connections it is nested in."""
    total, stack, pending = 0, [1], 1
    for m in re.finditer(r"(\w+)\s*(\((?:[^()]|\([^()]*\))*\))?|[{}]", query):
        token, args = m.group(0), m.group(2) or ""
        if token == "{":
            stack.append(stack[-1] * pending)
            pending = 1
        elif token == "}":
            stack.pop()
            pending = 1
        else:
            size = re.search(r"\b(?:first|last):\s*(\d+)", args)
            ids = re.search(r"\bids:\s*(\$\w+)", args)
            if size or ids:
                total += stack[-1]
                pending = int(size.group(1)) if size else (lists or {})[ids.group(1)]
            elif m.group(1) not in ("on",):
                pending = 1
    return total


def graphql_cost(query: str, lists: dict[str, int] | None = None) -> int:
    """GitHub's points for QUERY: its requests over 100, rounded, and at least 1."""
    return max(1, round(graphql_requests(query, lists) / 100))


class AgentSummaryTest(unittest.TestCase):
    """The text summary is read by agents: it lists what needs action and counts the rest (--verbose lists all)."""

    def test_failed_checks_are_capped_at_ten_unless_verbose(self):
        data = gg.parse_pr(pr_node(1, rollup="FAILURE",
                                   checks=[check_run(f"job{i:02d}", conclusion="FAILURE") for i in range(15)]))
        with mock.patch.dict(os.environ, {"AGENT_VERBOSE": ""}):
            terse = gg.summary("pr:o/r#1", {"data": data})
        self.assertEqual(terse.count("  failed: "), gg.SUMMARY_FAILED)
        self.assertIn("  ... 5 more failed (--json lists them)", terse)
        full = gg.summary("pr:o/r#1", {"data": data}, verbose=True)
        self.assertEqual(full.count("  failed: "), 15)
        self.assertNotIn("more failed", full)

    def test_the_capped_list_leads_with_required_then_real_failures(self):
        checks = ([check_run(f"cancel{i:02d}", conclusion="CANCELLED") for i in range(12)]
                  + [check_run("lint", conclusion="FAILURE"), {**check_run("build", conclusion="FAILURE"), "isRequired": True}])
        data = gg.parse_pr(pr_node(1, rollup="FAILURE", checks=checks))
        listed = [line for line in gg.summary("pr:o/r#1", {"data": data}, verbose=False).splitlines()
                  if line.startswith(("  failed: ", "  cancelled: "))]
        self.assertTrue(listed[0].startswith("  failed: build"), listed[0])
        self.assertTrue(listed[1].startswith("  failed: lint (not required)"), listed[1])
        self.assertEqual(len(listed), gg.SUMMARY_FAILED)

    def test_a_run_lists_its_real_failure_before_cancelled_jobs(self):
        jobs = ([{"name": f"cancel{i:02d}", "status": "completed", "conclusion": "cancelled"} for i in range(10)]
                + [{"name": "build", "status": "completed", "conclusion": "failure"}])
        data = {"name": "CI", "status": "completed", "conclusion": "failure", "run_attempt": 1,
                "head_branch": "b", "head_sha": "a" * 40, "html_url": "https://x"}
        text = gg.summary("run:o/r/1", {"data": data, "jobs": jobs}, verbose=False)
        listed = [line for line in text.splitlines() if line.startswith(("  failed: ", "  cancelled: "))]
        self.assertTrue(listed[0].startswith("  failed: build"), listed[0])
        self.assertEqual(len(listed), gg.SUMMARY_FAILED)

    def test_the_wait_request_carries_verbose_to_the_daemon(self):
        with mock.patch.dict(os.environ, {"AGENT_VERBOSE": "1"}):
            self.assertTrue(gg.wait_request("run", "o/r/1", "done", 5, False, False, None, True, 0)["verbose"])
        with mock.patch.dict(os.environ, {"AGENT_VERBOSE": ""}):
            self.assertFalse(gg.wait_request("run", "o/r/1", "done", 5, False, False, None, True, 0)["verbose"])

    def test_verbose_flag_sets_agent_verbose(self):
        with mock.patch.dict(os.environ, {"AGENT_VERBOSE": ""}), \
                mock.patch.object(gg, "cmd_status", return_value=0) as status:
            gg.main(["status", "pr", "o/r#1", "--verbose"])
            self.assertEqual(os.environ["AGENT_VERBOSE"], "1")
        status.assert_called_once()


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


def status_context(context: str, state: str = "SUCCESS") -> dict:
    return {"__typename": "StatusContext", "context": context, "state": state, "targetUrl": f"https://ci/{context}",
            "createdAt": "2026-11-05T14:28:31Z"}


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
        # sha -> that commit's check suites [{"id", "latest": runs in its newest attempt, "runs": every attempt's}]
        self.suites: dict[str, list] = {}
        self.suites_unread = False  # GitHub failed the checkSuites field: null beside an error
        self.rate_limit_calls = 0  # GET /rate_limit: free, so it is counted separately
        self.throttle: dict[str, tuple[int, dict, bytes]] = {}  # "rest"|"graphql" -> the answer every read gets
        self.no_protection_feature = False  # a private repository's plan: rules and protection answer 403

    def rate(self, resource: str, remaining: int) -> dict:
        return {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": str(remaining),
                "x-ratelimit-reset": str(int(NOW + 1800)), "x-ratelimit-resource": resource}

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
        self.calls.append((method, url, dict(headers), body))
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        throttled = self.throttle.get("graphql" if url.endswith("/graphql") else "rest")
        if throttled and not url.endswith("/rate_limit"):
            return throttled
        if url.endswith("/graphql"):
            doc = json.loads(body)
            v, data, errors = doc["variables"], {}, []
            self.graphql_remaining -= 1
            data["rateLimit"] = {"limit": 5000, "remaining": self.graphql_remaining, "resetAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 1800)), "cost": 1}
            if "suites" in v:  # every attempt's check runs of some suites, by id
                by_id = {suite["id"]: suite for suites in self.suites.values() for suite in suites}
                data["suiteRuns"] = [None if i not in by_id else {
                    "id": i, "conclusion": by_id[i].get("conclusion", "STARTUP_FAILURE"), "app": {"slug": "github-actions"},
                    "workflowRun": {"databaseId": by_id[i].get("run", 1), "workflow": {"name": "CI", "databaseId": 77}},
                    "checkRuns": {"pageInfo": {"hasNextPage": bool(by_id[i].get("runs_more"))},
                                  "nodes": [{k: v for k, v in r.items() if k not in ("__typename", "checkSuite")}
                                            for r in by_id[i]["runs"]]}} for i in v["suites"]]
                for n, i in enumerate(v["suites"]):  # a suite GitHub fails a field of: null beside a nested error
                    if by_id.get(i, {}).get("null_runs"):
                        data["suiteRuns"][n]["checkRuns"] = None
                        errors.append({"message": "Something went wrong", "path": ["suiteRuns", n, "checkRuns"]})
                body = {"data": data, **({"errors": errors} if errors else {})}
                return 200, self.rate("graphql", self.graphql_remaining), json.dumps(body).encode()
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
                for m in re.finditer(r'(?:([hm]\d+): )?repository\(owner:"([^"]+)",name:"([^"]+)"\)\{object\(oid:"([0-9a-f]+)"\)'
                                     r'\{\.\.\. on Commit\{statusCheckRollup\{contexts\(first:100(?:, after:"(\d+)")?\)', doc["query"]):
                    alias, owner, repo, sha, after = m.groups()
                    nodes = self.heads.get((owner, repo, sha))
                    start = int(after or 0)
                    node = None if nodes is None else {"statusCheckRollup": {"contexts": {
                        "pageInfo": {"hasNextPage": start + 100 < len(nodes), "endCursor": str(start + 100)},
                        "nodes": nodes[start:start + 100]}}, **self.suite_counts(sha, f"{owner}/{repo}")}
                    data["repository" if alias is None else alias] = {"object": node}
                out = {"data": data}
                return 200, self.rate("graphql", self.graphql_remaining), json.dumps(out).encode()
            comments = "issueOrPullRequest" in doc["query"]
            alias, field, table = ("c", "issueOrPullRequest", self.issues) if comments else ("p", "pullRequest", self.prs)
            i = 0
            while f"o{i}" in v:
                node = table.get((v[f"o{i}"], v[f"r{i}"], v[f"n{i}"]))
                if comments and node is not None:  # comments(last: COMMENTS_LAST)
                    node = {**node, "comments": {**node["comments"], "nodes": node["comments"]["nodes"][-gg.COMMENTS_LAST:]}}
                if not comments and node is not None:
                    node = copy.deepcopy(node)
                    node["commits"]["nodes"][0]["commit"].update(self.suite_counts(node["headRefOid"]))
                data[f"{alias}{i}"] = {field: node}
                if node is None:
                    errors.append({"type": "NOT_FOUND", "path": [f"{alias}{i}", field], "message": "Could not resolve"})
                i += 1
            out = {"data": data, **({"errors": errors} if errors else {})}
            return 200, self.rate("graphql", self.graphql_remaining), json.dumps(out).encode()
        path = url.split("api.github.com", 1)[1]
        parts = path.split("?")[0].split("/")
        if path == "/rate_limit":
            # Free: GitHub does not charge this against either primary limit, so neither does the fake.
            self.rate_limit_calls += 1
            body = {"resources": {"core": {"limit": 5000, "remaining": self.rest_remaining,
                                           "reset": int(NOW + 1800)},
                                  "graphql": {"limit": 5000, "remaining": self.graphql_remaining,
                                              "reset": int(NOW + 1800)}}}
            return 200, self.rate("core", self.rest_remaining), json.dumps(body).encode()
        if parts[4] == "issues":
            payload = self.rest_issue(parts, path)
            if payload is None:
                self.rest_remaining -= 1
                return 404, self.rate("core", self.rest_remaining), b'{"message": "Not Found"}'
        elif parts[4] in {"rules", "branches"} and self.no_protection_feature:
            self.rest_remaining -= 1
            return 403, self.rate("core", self.rest_remaining), json.dumps({"message": (
                "Upgrade to GitHub Pro or make this repository public to enable this feature.")}).encode()
        elif parts[4] == "actions" and parts[5] == "jobs":
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

    def suite_counts(self, sha: str, repo: str | None = None) -> dict:
        """A commit's checkSuites as SUITE_ATTEMPTS asks: each suite's check runs in its newest attempt
        (only the suites of REPO when a suite names its "repo"; "more": True cuts the page past 100 suites)."""
        if self.suites_unread:
            return {"checkSuites": None}
        suites = [suite for suite in self.suites.get(sha, []) if repo is None or suite.get("repo", repo) == repo]
        return {"checkSuites": {"pageInfo": {"hasNextPage": any(suite.get("more") for suite in suites)}, "nodes": [
            {"id": suite["id"], "latest": suite["latest"] if suite["latest"] is None else {"totalCount": suite["latest"]}}
            for suite in suites]}}

    def rest_issue(self, parts: list[str], path: str) -> Any:
        """The REST view of the GraphQL issue nodes in self.issues: the issue, or a page of its comments."""
        node = self.issues.get((parts[2], parts[3], int(parts[5])))
        if node is None:
            return None
        comments = node["comments"]["nodes"]
        if len(parts) == 6:
            pull = {"merged_at": "x" if node["state"] == "MERGED" else None} if node["__typename"] == "PullRequest" else None
            return {"html_url": node["url"], "state": "open" if node["state"] == "OPEN" else "closed",
                    "comments": len(comments), **({"pull_request": pull} if pull else {})}
        page = int(path.split("page=")[-1])
        return [{"id": c["databaseId"], "html_url": c["url"], "created_at": c["createdAt"],
                 "user": {"login": c["author"]["login"] + "[bot]" if c["author"]["login"].endswith("-bot")
                          else c["author"]["login"]}, "body": c["body"]}
                for c in comments[100 * (page - 1):100 * page]]

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
        contexts = self.heads.get((owner, repo, sha)) or []
        for node in self.prs.values():
            if node["headRefOid"] == sha:
                contexts = ((node["commits"]["nodes"][0]["commit"]["statusCheckRollup"] or {}).get("contexts") or {}).get("nodes") or []
        runs = {c["checkSuite"]["workflowRun"]["databaseId"]: c for c in contexts
                if ((c.get("checkSuite") or {}).get("workflowRun") or {}).get("databaseId")}
        if what == "actions":
            page = int(path.split("page=")[-1]) if "&page=" in path else 1
            listed = list(runs.items())[100 * (page - 1):100 * page]
            return {"total_count": len(runs), "workflow_runs": [{"id": rid, "name": "run-name " + str(rid),  # REST: the run-name
                                       "workflow_id": {"CI": 77, "Lint": 78}.get(c["checkSuite"]["workflowRun"]["workflow"]["name"]),
                                       "check_suite_id": rid, "conclusion": (c["checkSuite"].get("conclusion") or "").lower() or None}
                                      for rid, c in listed]}
        if parts[-1] == "status":
            listed = [c for c in contexts if c["__typename"] == "StatusContext"]
            page = int(path.split("page=")[-1]) if "&page=" in path else 1
            return {"total_count": len(listed), "statuses": [
                {"context": c["context"], "state": c["state"].lower(), "target_url": c.get("targetUrl"),
                 "created_at": c.get("createdAt")} for c in listed[100 * (page - 1):100 * page]]}
        checks = [c for c in contexts if c["__typename"] == "CheckRun"]
        page = int(path.split("page=")[-1]) if "&page=" in path else 1
        checks_page = checks[100 * (page - 1):100 * page]
        return {"total_count": len(checks), "check_runs": [
            {"id": c.get("databaseId"), "node_id": c.get("id"), "name": c["name"], "status": c["status"].lower(),
             "conclusion": (c.get("conclusion") or "").lower() or None, "details_url": c.get("detailsUrl"),
             "started_at": c.get("startedAt"), "completed_at": c.get("completedAt"),
             "check_suite": {"id": ((c.get("checkSuite") or {}).get("workflowRun") or {}).get("databaseId")},
             "app": {"slug": ((c.get("checkSuite") or {}).get("app") or {}).get("slug")}}
            for c in checks_page]}

    def rest_calls(self) -> list:
        return [c for c in self.calls if not c[1].endswith("/graphql")]

    def gql_calls(self) -> list:
        return [c for c in self.calls if c[1].endswith("/graphql")]

    def run_calls(self) -> list:
        return [c for c in self.rest_calls() if "/jobs" not in c[1]]

    def job_calls(self) -> list:
        return [c for c in self.rest_calls() if "/jobs" in c[1]]


def assert_json_refusal(test: unittest.TestCase, stdout: str, code: str | None) -> None:
    """stdout is exactly one agent-output-contract object (ok false, code, reason, next) and nothing else: no
    usage text, notice or waiting line leaked into the document."""
    doc = json.loads(stdout)
    test.assertEqual(stdout.count("\n"), 1, stdout)
    test.assertIs(doc["ok"], False)
    test.assertTrue(doc["next"] and doc["reason"], doc)
    if code is not None:
        test.assertEqual(doc["code"], code)
    test.assertNotIn("usage:", stdout)
    test.assertNotIn("waiting on", stdout)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        # watch files carry real mtimes, so the fake clock starts at each test, not at import:
        # a test reached 20 s into the suite saw its registrations as newer than NOW + 10 ticks
        global NOW
        NOW = float(int(time.time()))
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (gg.BASE, gg.WAIT_POLL, gg.DOWN_GRACE, gg.DOWN_PATIENCE, gg.BUDGET_REFRESH)
        gg.BASE = Path(self.tmp.name) / "gh"
        gg.WAIT_POLL = 0.05
        gg.DOWN_GRACE = 0.3
        gg.DOWN_PATIENCE = 0.6
        # An idle tick ends with a free GET /rate_limit; these tests count the daemon's requests, so
        # it is off here and BudgetRefreshTest turns it back on.
        gg.BUDGET_REFRESH = 0
        self.gh = FakeGitHub()
        self.daemon = gg.Daemon(transport=self.gh, token=lambda: TOKEN, controller=None)
        # A `wait pr|run` against a daemon that serves waits execs into bash: never the test runner.
        execv = mock.patch.object(gg.os, "execv", side_effect=AssertionError("os.execv in a test process"))
        execv.start()
        self.addCleanup(execv.stop)

    def tearDown(self) -> None:
        gg.BASE, gg.WAIT_POLL, gg.DOWN_GRACE, gg.DOWN_PATIENCE, gg.BUDGET_REFRESH = self.saved
        self.tmp.cleanup()

    def age_watch(self, key: str, seconds: float) -> None:
        path = gg.watch_dir() / gg.file_name(key)
        os.utime(path, (NOW - seconds, NOW - seconds))

    @contextlib.contextmanager
    def diagnostics_in(self, buffer: io.StringIO):
        """Route `say_waiting` into `buffer` for a test that asserts on *what* it says.

        `say_waiting` writes file descriptor 2, which `contextlib.redirect_stderr` does not touch
        (it swaps `sys.stderr`), so an in-process test cannot read it through a `StringIO` -- and
        must not, because the point of fd 2 is that it survives the stream being gone. The two
        halves are tested apart: the wording and the call sites here, through this seam; what the
        body does with fd 2 -- a dead pipe, a closed stream, a full pipe -- through the real CLI,
        in `WaitLineTest`. Patching the seam here is not a way around that: `WaitLineTest` reads fd
        2 of a subprocess, where no patch of ours is in effect."""
        with mock.patch.object(gg, "say_waiting", lambda line: print(line, file=buffer)):
            yield


class UnfinishedStatusTest(Base):
    """teamleaderleo/leo-workspace#3538 at f40f2a3d (2026-11-05): `wait pr --sha` returned success on GitGuardian's
    pass alone, before the fleet-reconciler posted the ws-check status its merges wait on. A pending status, an
    expected status not posted yet, and a head with nothing on it are never success."""

    SHA = "f" * 40

    def data(self, *nodes: dict, repo: str = "o/r", pushed: float | None = None) -> dict:
        node = pr_node(3538, checks=list(nodes))
        node.update(url=f"https://github.com/{repo}/pull/3538", headRefOid=self.SHA, mergeStateStatus="UNSTABLE",
                    updatedAt=iso(NOW if pushed is None else pushed))
        return gg.parse_pr(node)

    def test_a_pending_status_is_not_success(self) -> None:
        data = self.data(check_run("GitGuardian Security Checks"), status_context("ws-check", "PENDING"))
        for until in ("green", "done", "required"):
            self.assertIsNone(gg.verdict("pr", data, until, self.SHA, now=NOW), until)
        view = gg.status_view("pr:o/r#3538", {"data": data})
        self.assertEqual(view["checks"]["state"], "PENDING")
        self.assertEqual([c["name"] for c in view["checks"]["pending"]], ["ws-check"])

    def test_an_expected_status_not_posted_yet_is_not_success(self) -> None:
        # the #3538 head at 14:28: only GitGuardian had reported, ws-check was not posted yet
        data = self.data(check_run("GitGuardian Security Checks"), repo="teamleaderleo/leo-workspace",
                         pushed=NOW - 1500)
        self.assertIsNone(gg.verdict("pr", data, "green", self.SHA, now=NOW))
        self.assertEqual(gg.status_view("pr:teamleaderleo/leo-workspace#3538", {"data": data})["checks"]["state"],
                         "PENDING")
        self.assertIn("expected, not posted yet: ws-check",
                      gg.summary("pr:teamleaderleo/leo-workspace#3538", {"data": data}))
        posted = self.data(check_run("GitGuardian Security Checks"), status_context("ws-check", "PENDING"),
                           repo="teamleaderleo/leo-workspace", pushed=NOW - 1500)
        self.assertIsNone(gg.verdict("pr", posted, "green", self.SHA, now=NOW))
        green = self.data(check_run("GitGuardian Security Checks"), status_context("ws-check"),
                          repo="teamleaderleo/leo-workspace", pushed=NOW - 1500)
        self.assertEqual(gg.verdict("pr", green, "green", self.SHA, now=NOW), gg.EXIT_OK)
        red = self.data(check_run("GitGuardian Security Checks"), status_context("ws-check", "FAILURE"),
                        repo="teamleaderleo/leo-workspace", pushed=NOW - 1500)
        self.assertEqual(gg.verdict("pr", red, "green", self.SHA, now=NOW), gg.EXIT_FAIL)
        # the config file names a repo's contexts, and wins over the built-in list
        gg.write_json(gg.EXPECTED_CONFIG, {"o/r": ["deploy-check"], "teamleaderleo/leo-workspace": []})
        self.addCleanup(gg.EXPECTED_CONFIG.unlink)
        self.assertEqual(gg.verdict("pr", data, "green", self.SHA, now=NOW), gg.EXIT_OK)
        self.assertIsNone(gg.verdict("pr", self.data(check_run("ci")), "green", self.SHA, now=NOW))

    def test_an_empty_head_inside_the_settle_window_is_not_started(self) -> None:
        data = self.data(pushed=NOW - 60)
        self.assertIsNone(gg.verdict("pr", data, "green", self.SHA, now=NOW))
        self.assertIn("not started", gg.summary("pr:o/r#3538", {"data": data}))
        # past the window it says nothing reported, a distinct code, never success
        late = NOW - 60 + gg.SETTLE_GRACE + 1
        self.assertEqual(gg.verdict("pr", data, "green", self.SHA, now=late), gg.EXIT_NOTHING)
        self.assertNotIn(gg.EXIT_NOTHING, (gg.EXIT_OK, gg.EXIT_FAIL, gg.EXIT_TIMEOUT, gg.EXIT_DOWN, gg.EXIT_CONFLICT))
        self.assertEqual(gg.verdict("pr", data, "green", self.SHA, now=NOW, settle=30), gg.EXIT_NOTHING)
        # the daemon's first sighting of the head bounds the push when the PR's updatedAt moved later (a comment)
        seen = {**self.data(pushed=NOW), "headSeenAt": NOW - gg.SETTLE_GRACE - 5}
        self.assertEqual(gg.verdict("pr", seen, "green", self.SHA, now=NOW), gg.EXIT_NOTHING)

    def test_green_statuses_and_green_checks_are_success(self) -> None:
        data = self.data(check_run("build"), check_run("lint", conclusion="SKIPPED"), status_context("ci/legacy"),
                         pushed=NOW - 5)
        for until in ("green", "done", "required"):
            self.assertEqual(gg.verdict("pr", data, until, self.SHA, now=NOW), gg.EXIT_OK, until)

    def test_the_wait_says_so(self) -> None:
        key = "pr:o/r#3538"
        req = gg.wait_request("pr", "o/r#3538", "green", 60, False, False, self.SHA, True, gg.RESCUE_GRACE, settle=0)
        req["began"] = NOW - 5

        def cached(data: dict) -> None:
            gg.write_json(gg.cache_dir() / gg.file_name(key),
                          {"key": key, "fetchedAt": NOW, "dataAt": NOW, "data": data, "error": None})

        cached(self.data(check_run("GitGuardian Security Checks"), status_context("ws-check", "PENDING")))
        self.assertIsNone(gg.wait_step(req, NOW))
        cached(self.data(pushed=NOW - 60))
        code, text = gg.wait_step(req, NOW)
        self.assertEqual(code, gg.EXIT_NOTHING)
        self.assertIn("result: nothing-reported", text)
        self.assertIn("not a pass", text)
        cached(self.data(check_run("GitGuardian Security Checks"), status_context("ws-check")))
        self.assertEqual(gg.wait_step(req, NOW)[0], gg.EXIT_OK)

    def test_the_daemon_keeps_the_first_sighting_of_a_head(self) -> None:
        key = "pr:o/r#3538"
        self.daemon.now = NOW - 100
        self.daemon.store(key, None, data={"headRefOid": "a" * 40, "checks": []}, dataAt=NOW - 100)
        self.assertEqual(gg.read_cache(key)["data"]["headSeenAt"], NOW - 100)
        self.daemon.now = NOW
        self.daemon.store(key, None, data={"headRefOid": "a" * 40, "checks": []}, dataAt=NOW)
        self.assertEqual(gg.read_cache(key)["data"]["headSeenAt"], NOW - 100)
        self.daemon.store(key, None, data={"headRefOid": "b" * 40, "checks": []}, dataAt=NOW)
        self.assertEqual(gg.read_cache(key)["data"]["headSeenAt"], NOW)


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

    def test_controller_run_supports_one_shot_long_poll(self) -> None:
        controller = gg.Controller("http://controller:18765", None)
        payload = {"known": True, "run": ctl_run(41, "completed", "success")}
        with mock.patch.object(controller, "get", return_value=payload) as get:
            self.assertEqual(controller.run(41, wait=7)["id"], 41)
        get.assert_called_once_with("/v1/github/runs/41?wait=7", timeout=10)

    def test_controller_run_rejects_unbounded_wait(self) -> None:
        controller = gg.Controller("http://controller:18765", None)
        with self.assertRaisesRegex(gg.WatchError, r"0\.\.300"):
            controller.run(41, wait=301)

    def test_cache_store_fails_closed_when_write_lock_times_out(self) -> None:
        @contextlib.contextmanager
        def unavailable_lock(*_args, **_kwargs):
            yield False

        key = "run:o/r/42"
        with mock.patch.object(gg, "bounded_lock", unavailable_lock):
            self.daemon.store(key, None, data={"id": 42, "status": "in_progress"})
        self.assertIsNone(gg.read_cache(key))

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
        # attempt 1's run and job list; attempt 2's finish is confirmed a cycle after the last read
        self.assertEqual(len(self.gh.rest_calls()), 2)

    def test_a_finish_only_the_controller_reported_is_confirmed_before_a_wait_rules(self) -> None:
        """manaflow-ai/cmux run 40434431701, 2026-10-27: the controller kept attempt 1's "cancelled" (its rerun's
        deliveries were lost) while GitHub had attempt 2 queued, and `wait run` returned cancelled. A finish only
        the controller has reported is not ruled on until REST reads the run, which shows the rerun."""
        now = time.time()
        key = "run:o/r/46"
        self.ctl.runs[46] = ctl_run(46, "in_progress", "", iso(now - 120))
        self.gh.runs[46] = {"id": 46, "status": "in_progress", "conclusion": None, "run_attempt": 1,
                            "updated_at": iso(now - 120)}
        gg.register(key)
        self.daemon.tick(NOW)
        req = gg.wait_request("run", "o/r/46", "done", 60, False, False, None, True, gg.RESCUE_GRACE)
        req["began"] = time.time() - 1  # the reads below come after the wait began
        self.ctl.runs[46] = ctl_run(46, "completed", "cancelled", iso(now - 60))
        self.gh.runs[46] = {"id": 46, "status": "queued", "conclusion": None, "run_attempt": 2,
                            "updated_at": iso(now - 30)}
        calls = len(self.gh.rest_calls())
        controller = mock.Mock()
        controller.run.return_value = self.ctl.runs[46]
        with mock.patch.object(gg.Controller, "from_env", return_value=controller):
            gg.controller_wait_run(key, 5)  # the client's own long-poll stores the controller's finish
        self.assertEqual(gg.read_cache(key)["data"]["conclusion"], "cancelled")
        self.assertIsNone(gg.wait_step(req, time.time()), "never ruled on the controller's word alone")
        self.daemon.tick(NOW + gg.TICK)  # the daemon confirms it over REST
        self.assertEqual(len(self.gh.rest_calls()), calls + 1)
        entry = gg.read_cache(key)
        self.assertEqual((entry["data"]["status"], entry["data"]["run_attempt"], entry["source"]), ("queued", 2, "rest"))
        self.assertIsNone(gg.wait_step(req, time.time()), "the rerun is still queued")
        self.daemon.tick(NOW + 2 * gg.TICK)
        self.assertEqual(len(self.gh.rest_calls()), calls + 1, "a newer REST read keeps the controller's stale finish out")
        self.assertEqual(gg.read_cache(key)["data"]["status"], "queued")
        # once GitHub shows the rerun finished, the wait rules on it
        self.gh.runs[46] = {"id": 46, "status": "completed", "conclusion": "success", "run_attempt": 2,
                            "updated_at": iso(now)}
        self.ctl.runs[46] = {**ctl_run(46, "completed", "success", iso(now)), "attempt": 2}
        self.daemon.tick(NOW + 3 * gg.TICK)
        self.assertIsNone(gg.wait_step(req, time.time()), "the controller's success is confirmed first")
        self.daemon.tick(NOW + gg.TICK + gg.CYCLE)  # confirmations are paced at one per cycle
        self.assertEqual(len(self.gh.rest_calls()), calls + 2)
        result = gg.wait_step(req, time.time())
        self.assertIsNotNone(result)
        self.assertEqual(result[0], gg.EXIT_OK)

    def test_a_controller_success_is_confirmed_over_rest_once(self) -> None:
        """A green finish takes no failed-attempt read, so only the confirmation reads REST, once, and finds the
        rerun the controller missed."""
        now = time.time()
        key = "run:o/r/47"
        self.ctl.runs[47] = ctl_run(47, "in_progress", "", iso(now - 120))
        gg.register(key)
        self.age_watch(key, 1)  # the reader came before the first fetch, so a reader is not the reason to read REST
        self.daemon.tick(NOW)
        self.ctl.runs[47] = ctl_run(47, "completed", "success", iso(now - 60))
        self.gh.runs[47] = {"id": 47, "status": "queued", "conclusion": None, "run_attempt": 2,
                            "updated_at": iso(now - 30)}
        self.daemon.tick(NOW + gg.TICK)
        self.daemon.tick(NOW + 2 * gg.TICK)
        self.assertEqual(len(self.gh.rest_calls()), 1)
        self.assertEqual(gg.read_cache(key)["data"]["run_attempt"], 2)
        self.daemon.tick(NOW + 3 * gg.TICK)
        self.assertEqual(len(self.gh.rest_calls()), 1, "confirmed once, not every tick")

    def test_a_failed_confirmation_is_retried_once_a_cycle(self) -> None:
        now = time.time()
        key = "run:o/r/48"
        self.ctl.runs[48] = ctl_run(48, "completed", "success", iso(now - 60))
        gg.register(key)
        self.age_watch(key, 1)
        # no self.gh.runs[48]: every GitHub read of it fails (404 here; a 500 or a transport error is the same)
        for t in range(10):
            self.daemon.tick(NOW + t * gg.TICK)
        self.assertEqual(len(self.gh.rest_calls()), 1, "one failed read, not one a tick")
        self.daemon.tick(NOW + gg.CYCLE + 1)
        self.assertEqual(len(self.gh.rest_calls()), 2)

    def test_an_outage_is_read_once_a_cycle_even_with_a_waiting_reader_past_the_safety_age(self) -> None:
        now = time.time()
        key = "run:o/r/49"
        self.ctl.runs[49] = ctl_run(49, "completed", "success", iso(now - 60))
        gg.register(key)
        self.daemon.tick(NOW)
        reads = len(self.gh.rest_calls())  # its confirmation: the run is unknown to GitHub here, so it fails
        start = NOW + gg.REST_SAFETY + 1  # past the safety-net age, with a reader touching it every tick
        for t in range(10):
            self.age_watch(key, -(gg.REST_SAFETY + 1 + t * gg.TICK))
            self.daemon.tick(start + t * gg.TICK)
        self.assertEqual(len(self.gh.rest_calls()), reads + 1, "one failed read a cycle, not one a tick")

    def test_a_confirmation_ignores_an_etag_from_before_the_controller_record(self) -> None:
        """A cache from before restRun, or one whose ETag is from a read the controller's record replaced: a 304 would
        prove nothing about the finish, so the confirmation asks without the ETag and records what GitHub shows."""
        now = time.time()
        key = "run:o/r/50"
        self.gh.runs[50] = {"id": 50, "status": "completed", "conclusion": "success", "run_attempt": 1,
                            "updated_at": iso(now - 60)}
        self.ctl.runs[50] = ctl_run(50, "completed", "success", iso(now - 60))
        gg.register(key)
        self.daemon.tick(NOW)
        entry = gg.read_cache(key)
        self.assertTrue(entry.get("etag"))
        # an older version's cache: an ETag, no restRun
        entry.pop("restRun", None)
        gg.write_json(gg.cache_dir() / gg.file_name(key), entry)
        self.assertTrue(gg.controller_unconfirmed(gg.read_cache(key), time.time()))
        self.daemon.tick(NOW + gg.CYCLE + 1)
        self.assertNotIn("If-None-Match", self.gh.rest_calls()[-1][2])
        self.assertFalse(gg.controller_unconfirmed(gg.read_cache(key), time.time()))

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
        # a finish only the controller reported is read from GitHub once before any wait rules on it
        self.assertEqual(len(self.gh.rest_calls()), 1)
        stale = gg.read_cache(key)["dataAt"]
        self.daemon.tick(NOW - 98)
        self.assertEqual(gg.read_cache(key)["dataAt"], stale, "unchanged data keeps its time")
        self.age_watch(key, 0)  # a new reader
        self.daemon.tick(NOW)
        self.assertEqual(len(self.gh.rest_calls()), 2)
        self.assertEqual(gg.read_cache(key)["dataAt"], NOW)
        self.daemon.tick(NOW + gg.CYCLE)
        self.assertEqual(len(self.gh.rest_calls()), 2, "confirmed once, not every cycle")

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
        # the one REST read: the controller's finish is confirmed before a wait rules on it (a rerun's
        # deliveries can be lost), and only for the run that finished
        self.assertEqual([c[1].rsplit("/", 1)[-1] for c in self.gh.rest_calls()], ["41"])
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


class GraphqlDietTest(Base):
    """2026-11-01: the daemons on huge-blue and cosmic-gray were the account's biggest GraphQL spenders (938 and 833
    queries an hour) while their clients read about two dozen PRs. Idle watches back off, a comment wait on a
    repository the controller covers relies on its webhooks, a running head there is re-read on its own check
    events, heads due close together share a query, and daemon.json says what each query was for."""

    def setUp(self) -> None:
        super().setUp()
        self.ctl = FakeController()
        feed_every, gg.CONTROLLER_FEED_EVERY = gg.CONTROLLER_FEED_EVERY, 0
        self.addCleanup(setattr, gg, "CONTROLLER_FEED_EVERY", feed_every)
        self.daemon = gg.Daemon(transport=self.gh, token=lambda: TOKEN, controller=self.ctl)
        self.saved_config = gg.CONSUMERS_CONFIG
        gg.CONSUMERS_CONFIG = Path(self.tmp.name) / "consumers.json"
        self.addCleanup(setattr, gg, "CONSUMERS_CONFIG", self.saved_config)

    def touch(self, key: str, at: float) -> None:
        os.utime(gg.watch_dir() / gg.file_name(key), (at, at))

    def feed_covers(self, repo: str = "o/r") -> None:
        """A delivery for `repo`: the controller's webhooks cover it from now on."""
        self.ctl.suites.append({"id": 99, "repo": repo, "head_sha": "f" * 40, "status": "completed"})

    def pr_reads(self) -> int:
        return sum(1 for c in self.gh.gql_calls() if "pullRequest(number:" in json.loads(c[3])["query"])

    def test_an_unread_pr_backs_off_and_a_read_brings_it_back(self) -> None:
        self.gh.prs[("o", "r", 1)] = pr_node(1)
        gg.register("pr:o/r#1")
        for i in range(15):  # nobody reads it after registering: 15 cycles to IDLE_EXPIRY
            self.daemon.tick(NOW + i * gg.CYCLE)
        idle = self.pr_reads()
        self.assertLessEqual(idle, 8)  # every cycle for IDLE_BACKOFF_AFTER, then about as often as it was last read
        self.assertGreaterEqual(idle, 6)
        # a read (the broker serving it from the cache touches the watch) puts it back on every cycle
        before = self.pr_reads()
        gg.touch_interest("pr:o/r#1")
        self.touch("pr:o/r#1", NOW + 14 * gg.CYCLE)
        for i in range(15, 18):
            self.daemon.tick(NOW + i * gg.CYCLE)
        self.assertEqual(self.pr_reads() - before, 3)

    def test_a_waited_pr_never_backs_off(self) -> None:
        self.gh.prs[("o", "r", 1)] = pr_node(1)
        for i in range(12):
            if i % 3 == 0:
                gg.register("pr:o/r#1", waited=True)
                self.touch("pr:o/r#1", NOW)  # the wait's own touch is old; its waitedAt is what counts
                gg.write_json(gg.watch_dir() / gg.file_name("pr:o/r#1"),
                              {**gg.read_json(gg.watch_dir() / gg.file_name("pr:o/r#1")), "waitedAt": NOW + i * gg.CYCLE})
                self.touch("pr:o/r#1", NOW)
            self.daemon.tick(NOW + i * gg.CYCLE)
        self.assertEqual(self.pr_reads(), 12)

    def test_an_idle_pr_ignores_check_event_nudges(self) -> None:
        node = pr_node(1)
        node["headRefName"] = "feat/x"
        self.gh.prs[("o", "r", 1)] = node
        gg.register("pr:o/r#1")
        self.daemon.tick(NOW)
        start = NOW + gg.IDLE_BACKOFF_AFTER + gg.CYCLE
        self.daemon.tick(start)
        reads = self.pr_reads()
        self.ctl.suites.append({"id": 1, "repo": "o/r", "head_sha": "a" * 40, "status": "completed"})
        self.daemon.tick(start + gg.NUDGE_MIN)
        self.assertEqual(self.pr_reads(), reads)  # unread for over IDLE_BACKOFF_AFTER: its slower cycle stands
        self.touch("pr:o/r#1", start + gg.NUDGE_MIN)  # someone reads it again: nudges count again
        self.ctl.suites.append({"id": 2, "repo": "o/r", "head_sha": "a" * 40, "status": "completed"})
        self.daemon.tick(start + 2 * gg.NUDGE_MIN)
        self.assertEqual(self.pr_reads(), reads + 1)

    def test_a_closed_or_merged_pr_is_dropped_after_its_last_reader(self) -> None:
        self.gh.prs[("o", "r", 1)] = pr_node(1, state="MERGED")
        gg.register("pr:o/r#1")
        self.daemon.tick(NOW)
        self.assertEqual(self.daemon.tick(NOW + gg.TERMINAL_GRACE + 1)["watching"], 0)
        self.assertEqual(self.pr_reads(), 1)

    def test_a_comment_wait_on_a_covered_repository_polls_only_as_a_safety_net(self) -> None:
        self.gh.issues[("o", "r", 7)] = {"__typename": "Issue", "url": "u", "state": "OPEN",
                                         "comments": {"totalCount": 0, "nodes": []}}
        self.daemon.tick(NOW)  # the feed's first read only learns the cursor
        self.feed_covers()
        key = "comment:o/r#7"
        reads = []
        for i in range(0, 241, gg.COMMENT_CYCLE):
            gg.register(key)
            self.touch(key, NOW + i)  # the wait touches it every TOUCH_EVERY
            self.daemon.tick(NOW + i + 1)
            reads.append(len(self.gh.gql_calls()))
        self.assertLessEqual(reads[-1], 3)  # 0 s, then every COMMENT_FED_CYCLE: 240 s
        # a comment the webhook delivers reaches the cache at once, with no GitHub read
        self.ctl.comments.append({"repo": "o/r", "number": 7, "id": 5, "url": "c", "created_at": "2026-11-01T00:00:00Z",
                                  "author": "x", "body": "done"})
        self.daemon.tick(NOW + 243)
        self.assertEqual(gg.read_cache(key)["data"]["comments"][-1]["body"], "done")
        self.assertEqual(len(self.gh.gql_calls()), reads[-1])

    def test_a_comment_wait_elsewhere_keeps_its_fast_cycle(self) -> None:
        self.gh.issues[("t", "s", 7)] = {"__typename": "Issue", "url": "u", "state": "OPEN",
                                         "comments": {"totalCount": 0, "nodes": []}}
        self.daemon.tick(NOW)
        self.feed_covers("o/r")  # the controller covers o/r, not t/s
        key = "comment:t/s#7"
        for i in range(0, 61, gg.COMMENT_CYCLE):
            gg.register(key)
            self.touch(key, NOW + i)
            self.daemon.tick(NOW + i + 1)
        self.assertEqual(len(self.gh.gql_calls()), 5)  # every COMMENT_CYCLE, as before

    def test_a_running_head_on_a_covered_repository_waits_for_its_check_events(self) -> None:
        sha = "a" * 40
        self.gh.heads[("o", "r", sha)] = [check_run("CI / build", "IN_PROGRESS", None)]
        key = gg.parse_key("head", f"o/r@{sha}")
        self.daemon.tick(NOW)
        self.feed_covers()
        gg.register(key, consumer="merge-guard")
        self.daemon.tick(NOW + gg.TICK)
        self.assertEqual(len(self.gh.gql_calls()), 1)
        gg.register(key, consumer="merge-guard")
        self.daemon.tick(NOW + gg.TICK + gg.HEAD_PENDING_TTL)
        self.assertEqual(len(self.gh.gql_calls()), 1)  # covered: the pending TTL is HEAD_PENDING_FED_TTL
        self.gh.heads[("o", "r", sha)] = [check_run("CI / build")]
        self.ctl.suites.append({"id": 3, "repo": "o/r", "head_sha": sha, "status": "completed"})
        self.daemon.tick(NOW + 2 * gg.TICK + gg.HEAD_PENDING_TTL)
        self.assertEqual(len(self.gh.gql_calls()), 2)  # its check event re-read it at once
        self.assertTrue(gg.read_cache(key)["data"]["settled"])

    def test_a_check_event_inside_the_retry_gate_is_kept_until_the_head_is_read(self) -> None:
        sha = "a" * 40
        self.gh.heads[("o", "r", sha)] = [check_run("CI / build", "IN_PROGRESS", None)]
        key = gg.parse_key("head", f"o/r@{sha}")
        self.daemon.tick(NOW)
        self.feed_covers()
        gg.register(key, consumer="merge-guard")
        self.daemon.tick(NOW + gg.TICK)
        self.assertEqual(len(self.gh.gql_calls()), 1)
        self.ctl.suites.append({"id": 3, "repo": "o/r", "head_sha": sha, "status": "in_progress"})
        gg.register(key, consumer="merge-guard")
        self.daemon.tick(NOW + gg.TICK + gg.NUDGE_MIN)  # nudged, but inside the head's retry gate
        self.assertEqual(len(self.gh.gql_calls()), 1)
        self.daemon.tick(NOW + gg.TICK + gg.CYCLE)  # the gate opens: the event still counts
        self.assertEqual(len(self.gh.gql_calls()), 2)

    def test_a_new_controller_has_to_show_its_coverage_again(self) -> None:
        self.daemon.tick(NOW)
        self.feed_covers()
        self.daemon.tick(NOW + gg.TICK)
        self.assertTrue(self.daemon.fed("comment:o/r#1"))
        other = FakeController()
        self.daemon.controller_source = lambda: other
        other.base = "http://elsewhere"
        self.daemon.tick(NOW + 2 * gg.TICK)
        self.assertFalse(self.daemon.fed("comment:o/r#1"))

    def test_a_running_head_elsewhere_keeps_its_ttl(self) -> None:
        sha = "a" * 40
        self.gh.heads[("o", "r", sha)] = [check_run("CI / build", "IN_PROGRESS", None)]
        key = gg.parse_key("head", f"o/r@{sha}")
        gg.register(key, consumer="merge-guard")
        self.daemon.tick(NOW)
        gg.register(key, consumer="merge-guard")
        self.daemon.tick(NOW + gg.HEAD_PENDING_TTL + gg.TICK)
        self.assertEqual(len(self.gh.gql_calls()), 2)

    def test_heads_due_close_together_share_one_query(self) -> None:
        shas = ["a" * 40, "b" * 40]
        for sha in shas:
            self.gh.heads[("o", "r", sha)] = [check_run("CI / build", "IN_PROGRESS", None)]
        gg.register(gg.parse_key("head", f"o/r@{shas[0]}"), consumer="c")
        self.daemon.tick(NOW)
        gg.register(gg.parse_key("head", f"o/r@{shas[1]}"), consumer="c")
        self.daemon.tick(NOW + 20)  # each read alone so far
        self.assertEqual(len(self.gh.gql_calls()), 2)
        for sha in shas:
            gg.register(gg.parse_key("head", f"o/r@{sha}"), consumer="c")
        self.daemon.tick(NOW + gg.HEAD_PENDING_TTL + gg.TICK)  # the first is due; the second is due in 18 s
        self.assertEqual(len(self.gh.gql_calls()), 3)
        self.daemon.tick(NOW + gg.HEAD_PENDING_TTL + 20 + gg.TICK)
        self.assertEqual(len(self.gh.gql_calls()), 3)  # it rode along: no second query
        self.assertNotIn("c", self.daemon.denied)

    def test_daemon_json_says_what_each_query_was_for(self) -> None:
        self.gh.prs[("o", "r", 1)] = pr_node(1)
        gg.register("pr:o/r#1")
        status = self.daemon.tick(NOW)
        self.assertEqual(status["graphqlBy"]["pr"]["queries"], 1)
        self.assertGreaterEqual(status["graphqlBy"]["pr"]["points"], 1)

    def test_a_fleet_of_watches_stays_well_under_300_queries_an_hour(self) -> None:
        """The shape measured on huge-blue (2026-11-01 05:00Z): 31 PRs (9 waited, 11 unread for 10 minutes or
        more), 40 heads asked for every minute, and a comment wait, on repositories the controller covers."""
        for n in range(1, 32):
            self.gh.prs[("o", "r", n)] = pr_node(n)
        heads = [f"{n:040x}" for n in range(1, 41)]
        for sha in heads:
            self.gh.heads[("o", "r", sha)] = [check_run("CI / build", "IN_PROGRESS", None)]
        self.gh.issues[("o", "r", 900)] = {"__typename": "Issue", "url": "u", "state": "OPEN",
                                           "comments": {"totalCount": 0, "nodes": []}}
        self.daemon.tick(NOW)
        self.feed_covers()
        for n in range(1, 32):
            gg.register(f"pr:o/r#{n}", waited=n <= 9)
            if n > 20:
                self.touch(f"pr:o/r#{n}", NOW - 600)
        t = NOW
        while t < NOW + 3600:
            t += gg.TICK
            minute = int(t - NOW) % 60 < gg.TICK
            if int(t - NOW) % gg.TOUCH_EVERY < gg.TICK:
                for n in range(1, 21):  # nine waits and eleven readers keep reading
                    gg.register(f"pr:o/r#{n}", waited=n <= 9) if n <= 9 else self.touch(f"pr:o/r#{n}", t)
                gg.register("comment:o/r#900")
                self.touch("comment:o/r#900", t)
            if minute:
                for i, sha in enumerate(heads):
                    gg.register(gg.parse_key("head", f"o/r@{sha}"), consumer=f"c{i % 4}")
                    self.touch(gg.parse_key("head", f"o/r@{sha}"), t)
            for n in range(1, 21):
                path = gg.watch_dir() / gg.file_name(f"pr:o/r#{n}")
                doc = gg.read_json(path)
                if n <= 9 and doc is not None:
                    doc["waitedAt"] = t
                    gg.write_json(path, doc)
                    self.touch(f"pr:o/r#{n}", t)
            self.daemon.tick(t)
        hour = len(self.gh.gql_calls())
        self.assertLess(hour, 300, self.daemon.status.get("graphqlBy"))


class UpstreamTest(Base):
    """A host with no login reads through upstream.json's program: one request per tick, never GitHub."""

    def setUp(self) -> None:
        super().setUp()
        self.asked: list[dict] = []
        self.answers: dict[str, dict | None] = {}
        self.broken: Exception | None = None

        def ask(request: dict) -> dict:
            self.asked.append(request)
            if self.broken is not None:
                raise self.broken
            return {"entries": {k["key"]: self.answers.get(k["key"]) for k in request["keys"]}}

        self.daemon = gg.Daemon(transport=self.gh, token=lambda: self.fail("read a token"), controller=None,
                                upstream=ask)

    def pr_data(self) -> dict:
        return {"number": 7, "state": "OPEN", "checks": [], "headRefOid": "a" * 40}

    def test_a_watched_pr_is_filled_from_the_upstream_with_no_github_request(self) -> None:
        gg.register("pr:o/r#7")
        gg.register("run:o/r/9", jobs=True)
        self.answers["pr:o/r#7"] = {"data": self.pr_data(), "dataAt": NOW - 30, "error": None}
        self.answers["run:o/r/9"] = {"data": {"id": 9, "status": "completed"}, "dataAt": NOW - 5, "jobs": [{"id": 1}]}
        status = self.daemon.tick(now=NOW)
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(sorted(k["key"] for k in self.asked[0]["keys"]), ["pr:o/r#7", "run:o/r/9"])
        self.assertIn({"key": "run:o/r/9", "jobs": True}, self.asked[0]["keys"])
        pr = gg.read_cache("pr:o/r#7")
        self.assertEqual(pr["data"]["number"], 7)
        self.assertEqual(pr["dataAt"], NOW - 30)  # the upstream's age, not this host's
        self.assertEqual(pr["source"], "upstream")
        self.assertEqual(gg.read_cache("run:o/r/9")["jobs"], [{"id": 1}])
        self.assertEqual(status["upstream"]["state"], "ok")

    def test_a_controller_only_finish_stays_unconfirmed_downstream(self) -> None:
        """The upstream's provenance travels with the data: a finish only its controller reported is not ruled on
        here either, and one a GitHub read confirmed is."""
        now = time.time()
        gg.register("run:o/r/9")
        req = gg.wait_request("run", "o/r/9", "done", 60, False, False, None, True, gg.RESCUE_GRACE)
        req["began"] = now - 30
        done = {"id": 9, "status": "completed", "conclusion": "cancelled", "run_attempt": 1, "head_sha": "a" * 40}
        self.answers["run:o/r/9"] = {"data": done, "dataAt": now - 5, "origin": "controller"}
        self.daemon.tick(now=NOW)
        entry = gg.read_cache("run:o/r/9")
        self.assertEqual((entry["source"], entry["origin"]), ("upstream", "controller"))
        self.assertIsNone(gg.wait_step(req, time.time()))
        self.answers["run:o/r/9"] = {**self.answers["run:o/r/9"],
                                     "restRun": {"run_attempt": 1, "status": "completed", "conclusion": "cancelled"}}
        self.daemon.tick(now=NOW + gg.UPSTREAM_EVERY + 1)
        # a GitHub read with no time (an older upstream) may be from before a rerun: not ruled on until the bound
        self.assertIsNone(gg.wait_step(req, time.time()))
        self.assertIsNotNone(gg.wait_step(req, req["began"] + gg.RUN_CONFIRM_MAX + 1))
        self.answers["run:o/r/9"] = {**self.answers["run:o/r/9"], "restRunAt": now - 1}  # read since the wait began
        self.daemon.tick(now=NOW + 2 * gg.UPSTREAM_EVERY + 2)
        self.assertEqual(gg.read_cache("run:o/r/9")["restRunAt"], now - 1)
        self.assertIsNotNone(gg.wait_step(req, time.time()))
        # an upstream that does not say where the data came from (an older version) is not trusted for a finish
        self.answers["run:o/r/9"] = {"data": done, "dataAt": time.time() - 5}
        self.daemon.tick(now=NOW + 3 * gg.UPSTREAM_EVERY + 3)
        self.assertIsNone(gg.read_cache("run:o/r/9").get("origin"))
        self.assertTrue(gg.controller_unconfirmed(gg.read_cache("run:o/r/9"), time.time()))
        self.answers["run:o/r/9"] = {**self.answers["run:o/r/9"], "origin": "controller",
                                     "restRun": {"run_attempt": 1, "status": "completed", "conclusion": "cancelled"}}
        self.daemon.tick(now=NOW + 4 * gg.UPSTREAM_EVERY + 4)
        # what this host serves to its own downstream says where the data came from
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(gg, "daemon_running", return_value=True), \
                mock.patch.object(gg, "daemon_failing", return_value=None):
            gg.cmd_read(json.dumps({"keys": [{"key": "run:o/r/9", "jobs": False}]}))
        served = json.loads(out.getvalue())["entries"]["run:o/r/9"]
        self.assertEqual(served["origin"], "controller")
        self.assertEqual(served["restRun"]["conclusion"], "cancelled")

    def test_reads_are_paced_and_a_new_key_is_asked_for_soon(self) -> None:
        gg.register("pr:o/r#7")
        self.daemon.tick(now=NOW)  # the upstream has not read it yet: null
        self.assertIsNone(gg.read_cache("pr:o/r#7"))
        self.daemon.tick(now=NOW + 1)
        self.assertEqual(len(self.asked), 1, "a key with no data waits UPSTREAM_NEW")
        self.answers["pr:o/r#7"] = {"data": self.pr_data(), "dataAt": NOW + 2}
        self.daemon.tick(now=NOW + gg.UPSTREAM_NEW)
        self.assertEqual(len(self.asked), 2)
        self.assertIsNotNone(gg.read_cache("pr:o/r#7")["data"])
        self.daemon.tick(now=NOW + gg.UPSTREAM_NEW + 2)
        self.assertEqual(len(self.asked), 2, "a key with data waits for the full cycle")
        self.daemon.tick(now=NOW + gg.UPSTREAM_EVERY)
        self.assertEqual(len(self.asked), 3)

    def test_no_watch_no_request(self) -> None:
        self.daemon.tick(now=NOW)
        self.daemon.tick(now=NOW + gg.UPSTREAM_EVERY)
        self.assertEqual(self.asked, [])

    def test_a_failing_upstream_names_the_cause_and_keeps_old_data(self) -> None:
        gg.register("pr:o/r#7")
        gg.register("pr:o/r#8")
        self.answers["pr:o/r#7"] = {"data": self.pr_data(), "dataAt": NOW}
        self.daemon.tick(now=NOW)
        self.broken = gg.WatchError("upstream: no broker socket")
        status = self.daemon.tick(now=NOW + gg.UPSTREAM_EVERY)
        self.assertEqual(status["upstream"]["state"], "failing")
        self.assertIn("upstream: no broker socket", status["errors"])
        self.assertEqual(gg.read_cache("pr:o/r#7")["data"]["number"], 7)  # kept, and the outage named on it
        self.assertIn("no broker socket", gg.read_cache("pr:o/r#7")["error"])
        self.assertIn("no broker socket", gg.read_cache("pr:o/r#8")["error"])
        self.broken = None
        self.daemon.tick(now=NOW + 2 * gg.UPSTREAM_EVERY)
        self.assertIsNone(gg.read_cache("pr:o/r#7")["error"])

    def test_past_the_key_cap_the_least_recently_read_go_first(self) -> None:
        keys = [f"pr:o/r#{n}" for n in range(1, 6)]
        for k in keys:
            gg.register(k)
            self.answers[k] = {"data": self.pr_data(), "dataAt": NOW}
        with mock.patch.object(gg, "UPSTREAM_MAX_KEYS", 2):
            for i in range(5):
                self.daemon.tick(now=NOW + i * gg.UPSTREAM_EVERY)
        asked = [k["key"] for r in self.asked for k in r["keys"]]
        self.assertEqual(set(asked), set(keys), "every key is read in turn")
        self.assertTrue(all(len(r["keys"]) <= 2 for r in self.asked))

    def test_keys_that_keep_failing_do_not_starve_one_with_data(self) -> None:
        bad = [f"pr:o/r#{n}" for n in range(10, 14)]
        for k in bad:
            gg.register(k)
            self.answers[k] = {"data": None, "error": "NOT_FOUND", "missing": True}
        gg.register("pr:o/r#7")
        self.answers["pr:o/r#7"] = {"data": self.pr_data(), "dataAt": NOW}
        with mock.patch.object(gg, "UPSTREAM_MAX_KEYS", 2):
            for i in range(30):
                self.daemon.tick(now=NOW + i * gg.UPSTREAM_NEW)
        asked = [k["key"] for r in self.asked for k in r["keys"]]
        self.assertGreaterEqual(asked.count("pr:o/r#7"), 2)
        # a key the upstream answered with an error waits for full cycles, not UPSTREAM_NEW: one full read per
        # UPSTREAM_EVERY, plus the reads that first fill the 5 new keys 2 at a time
        self.assertLessEqual(len(self.asked), 30 * gg.UPSTREAM_NEW // gg.UPSTREAM_EVERY + 1 + 3)

    def test_a_pr_github_says_does_not_exist_fails_a_wait_instead_of_timing_out(self) -> None:
        gg.register("pr:o/r#404")
        self.answers["pr:o/r#404"] = {"data": None, "error": "NOT_FOUND", "missing": True, "fetchedAt": NOW}
        self.daemon.tick(now=NOW)
        entry = gg.read_cache("pr:o/r#404")
        self.assertTrue(entry["missing"])
        self.assertEqual(entry["fetchedAt"], NOW)
        req = {"key": "pr:o/r#404", "kind": "pr", "until": "green", "grace": 0, "began": NOW - 1, "timeout": 60,
               "target": "o/r#404"}
        self.assertEqual(gg.wait_step(req, NOW)[0], gg.EXIT_FAIL)

    def test_heads_and_posts_are_refused_with_a_reason(self) -> None:
        key = gg.parse_key("head", "o/r@" + "b" * 40)
        gg.register(key, consumer="me")
        gg.outbox_dir().mkdir(parents=True)
        gg.write_json(gg.outbox_dir() / "x1.json", {"id": "x1", "kind": "comment", "body": "hi"})
        self.daemon.tick(now=NOW)
        self.assertIn("login", gg.read_cache(key)["error"])
        self.assertEqual(self.asked, [])
        done = gg.read_json(gg.posted_dir() / "x1.json")
        self.assertEqual(done["state"], "failed")
        self.assertIn("no login", done["error"])
        self.assertEqual(self.gh.calls, [])

    def test_upstream_json_switches_the_mode_and_runs_the_program(self) -> None:
        tmp = Path(self.tmp.name)
        program = tmp / "up"
        program.write_text("#!/bin/sh\nread line\nprintf '%%s\\n' \"$line\" > %s\n"
                           "echo '{\"entries\": {\"pr:o/r#7\": {\"data\": {\"number\": 7}, \"dataAt\": 5}}}'\n"
                           % (tmp / "seen"))
        program.chmod(0o755)
        config = tmp / "upstream.json"
        gg.write_json(config, {"command": [os.fspath(program)]})
        with mock.patch.object(gg, "UPSTREAM_CONFIG", config):
            daemon = gg.Daemon(transport=self.gh, token=lambda: self.fail("read a token"), controller=None)
            gg.register("pr:o/r#7")
            daemon.tick(now=NOW)
        self.assertEqual(json.loads((tmp / "seen").read_text()), {"keys": [{"key": "pr:o/r#7", "jobs": False}]})
        self.assertEqual(gg.read_cache("pr:o/r#7")["data"], {"number": 7})
        self.assertEqual(self.gh.calls, [])

    def test_read_registers_and_answers_from_the_cache(self) -> None:
        self.daemon.store("pr:o/r#7", None, data={"number": 7}, dataAt=NOW)
        self.daemon.store("pr:o/r#8", None, error="NOT_FOUND", missing=True)
        out = io.StringIO()
        req = json.dumps({"keys": [{"key": "pr:o/r#7", "jobs": False}, {"key": "run:o/r/9", "jobs": True}]})
        with mock.patch.object(gg, "daemon_running", lambda grace=0.0: True), \
                mock.patch.object(gg, "daemon_failing", lambda: None), contextlib.redirect_stdout(out):
            self.assertEqual(gg.cmd_read(req), 0)
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["entries"]["pr:o/r#7"]["data"], {"number": 7})
        self.assertIsNone(doc["entries"]["run:o/r/9"])
        req = json.dumps({"keys": [{"key": "pr:o/r#8", "jobs": False}]})
        with mock.patch.object(gg, "daemon_running", lambda grace=0.0: True), \
                mock.patch.object(gg, "daemon_failing", lambda: None), contextlib.redirect_stdout(out := io.StringIO()):
            gg.cmd_read(req)
        self.assertTrue(json.loads(out.getvalue())["entries"]["pr:o/r#8"]["missing"])
        self.assertTrue(gg.read_json(gg.watch_dir() / gg.file_name("run:o/r/9"))["jobs"])
        self.assertTrue((gg.watch_dir() / gg.file_name("pr:o/r#7")).exists())

    def test_read_refuses_what_is_not_a_key(self) -> None:
        for bad in ("nope", json.dumps({"keys": [{"key": "head:o/r@" + "a" * 40}]}),
                    json.dumps({"keys": [{"key": "pr:O/r#7"}]}), json.dumps({"keys": [{"key": "pr:o/r#07"}]}),
                    json.dumps({"keys": [{"key": "pr:o/r#7", "jobs": "yes"}]}),
                    json.dumps({"keys": [{"key": "pr:o/r#7"}] * (gg.UPSTREAM_MAX_KEYS + 1)})):
            with self.assertRaises(gg.WatchError, msg=bad):
                gg.read_request(bad)


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

    def sweep(self, many: bool) -> tuple[int, str]:
        """Ten PRs looked at once: one `status pr` with ten targets, or the loop agents wrote (42 times in
        19 transcripts) of ten `status pr` calls. The stand-in daemon ticks as each call registers, which is
        when the real one picks a new watch up (a never-read key sorts first)."""
        for n in range(1, 11):
            self.gh.prs[("o", "r", n)] = pr_node(n, rollup="SUCCESS", checks=[check_run("a")])
        gg.BASE.mkdir(parents=True, exist_ok=True)
        lock = open(gg.BASE / "daemon.lock", "a+")
        fcntl.flock(lock, fcntl.LOCK_EX)  # stands in for the running daemon this test drives by hand
        ticks = iter(range(100))

        def then_tick(real):
            def wrapped(*args, **kwargs):
                real(*args, **kwargs)
                self.daemon.tick(NOW + next(ticks))
            return wrapped
        out = io.StringIO()
        codes = []
        try:
            with mock.patch.object(gg, "register", then_tick(gg.register)), \
                    mock.patch.object(gg, "register_all", then_tick(gg.register_all)), \
                    mock.patch.object(gg, "say_waiting", lambda *_: None), contextlib.redirect_stdout(out):
                if many:
                    codes.append(gg.cmd_status("pr", ["o/r#1", *[str(n) for n in range(2, 11)]], False, False,
                                               first_wait=5))
                else:
                    for n in range(1, 11):
                        codes.append(gg.cmd_status("pr", f"o/r#{n}", False, False, first_wait=5))
        finally:
            lock.close()
        self.assertEqual(set(codes), {0})
        return len(self.gh.gql_calls()) + len(self.gh.rest_calls()), out.getvalue()

    def test_a_ten_pr_status_is_one_upstream_read_where_the_loop_was_ten(self) -> None:
        reads, text = self.sweep(many=True)
        self.assertEqual(reads, 1)  # one batched GraphQL query for all ten
        self.assertEqual([line.split()[0] for line in text.splitlines() if line.startswith("o/r#")],
                         [f"o/r#{n}" for n in range(1, 11)])
        self.assertEqual(text.count("checks SUCCESS: 1 passed"), 10)

    def test_the_old_loop_of_single_status_calls_reads_once_per_pr(self) -> None:
        reads, _ = self.sweep(many=False)
        self.assertEqual(reads, 10)  # the before of the sweep test's after: one query per call

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
        self.daemon.budget.pop("restCounters", None)  # forget every core counter, not only the reading
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
        self.assertEqual(hour, {"rest": 1, "rest304": 1, "graphql": 0, "free": 0, "write": 0})
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
        self.budgets(rest=(1600, 120), graphql=(1600, 3000))  # same count, both above their floors: 2 minutes to go is rich
        self.assertGreater(self.daemon.richness("rest"), 10 * self.daemon.richness("graphql"))
        self.assertEqual(self.daemon.route("pr"), "rest")
        self.assertEqual(self.daemon.route("run"), "rest")
        self.assertEqual(self.daemon.route("comment"), "graphql")  # comment watches never move
        self.budgets(rest=(1600, 3000), graphql=(1600, 120))
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
        # never shown by any read: missing (wait's not-found), as REST's 404 for such a run now is
        self.assertTrue(missing.get("missing"))

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
                                 f"commits/{'a' * 40}/status?per_page=100&page=1", f"actions/runs?head_sha={'a' * 40}&per_page=100&page=1",
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
        # the 4 PR-level reads, all 304; the 3 repo/base-level answers are shared within REST_REPO_PART_TTL
        self.assertEqual(len(self.gh.rest_calls()), 11)
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

    def test_status_pr_follows_graphql_rollup_pages_for_required_checks(self) -> None:
        """The cached PR read must not strand a required check after GraphQL's first 100 contexts."""
        checks = [workflow_check(f"job-{i}", 5) for i in range(150)]
        checks[-1]["isRequired"] = True
        first = pr_node(8, checks=checks[:100])
        contexts = first["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        contexts["totalCount"] = len(checks)
        contexts["pageInfo"] = {"hasNextPage": True, "endCursor": "100"}
        first["baseRef"] = {"branchProtectionRule": {"requiredStatusCheckContexts": ["job-149"]}, "rules": {"nodes": []}}
        self.gh.prs[("o", "r", 8)] = first
        self.gh.heads[("o", "r", "a" * 40)] = checks
        self.budgets(rest=(300, 3000), graphql=(4000, 3000))
        gg.register("pr:o/r#8")
        self.daemon.tick(NOW)
        data = gg.read_cache("pr:o/r#8")["data"]
        self.assertEqual(len(data["checks"]), 150)
        self.assertFalse(data["checksTruncated"])
        self.assertEqual(data["requiredMissing"], [])
        self.assertEqual(len(self.gh.gql_calls()), 2)

    def test_status_pr_does_not_page_when_required_checks_are_in_first_slice(self) -> None:
        checks = [workflow_check(f"job-{i}", 5) for i in range(150)]
        checks[99]["isRequired"] = True
        first = pr_node(9, checks=checks[:100])
        contexts = first["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        contexts["totalCount"] = len(checks)
        contexts["pageInfo"] = {"hasNextPage": True, "endCursor": "100"}
        first["baseRef"] = {"branchProtectionRule": {"requiredStatusCheckContexts": ["job-99"]}, "rules": {"nodes": []}}
        self.gh.prs[("o", "r", 9)] = first
        self.gh.heads[("o", "r", "a" * 40)] = checks
        self.budgets(rest=(300, 3000), graphql=(4000, 3000))
        gg.register("pr:o/r#9")
        self.daemon.tick(NOW)
        data = gg.read_cache("pr:o/r#9")["data"]
        self.assertEqual(len(data["checks"]), 100)
        self.assertTrue(data["checksTruncated"])
        self.assertEqual(data["requiredMissing"], [])
        self.assertEqual(len(self.gh.gql_calls()), 1)

    def test_status_pr_does_not_page_when_the_base_requires_no_check(self) -> None:
        """No required context: the rollup's first 100 and checkState decide, with no follow-up pages."""
        checks = [workflow_check(f"job-{i}", 5) for i in range(150)]
        first = pr_node(10, checks=checks[:100])
        contexts = first["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        contexts["totalCount"] = len(checks)
        contexts["pageInfo"] = {"hasNextPage": True, "endCursor": "100"}
        first["baseRef"] = {"branchProtectionRule": None, "rules": {"nodes": []}}
        self.gh.prs[("o", "r", 10)] = first
        self.gh.heads[("o", "r", "a" * 40)] = checks
        self.budgets(rest=(300, 3000), graphql=(4000, 3000))
        gg.register("pr:o/r#10")
        self.daemon.tick(NOW)
        data = gg.read_cache("pr:o/r#10")["data"]
        self.assertEqual(len(data["checks"]), 100)
        self.assertTrue(data["checksTruncated"])
        self.assertEqual(data["requiredMissing"], [])
        self.assertNotIn("checksPageError", data)
        self.assertEqual(len(self.gh.gql_calls()), 1)

    def test_status_pr_check_pages_stop_at_their_hourly_share(self) -> None:
        """Past PR_PAGES_POINTS_PER_HOUR the follow-up pages stop, and the PR reads pending, never green."""
        checks = [workflow_check(f"job-{i}", 5) for i in range(150)]
        first = pr_node(11, checks=checks[:100])
        contexts = first["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        contexts["totalCount"] = len(checks)
        contexts["pageInfo"] = {"hasNextPage": True, "endCursor": "100"}
        first["baseRef"] = {"branchProtectionRule": {"requiredStatusCheckContexts": ["job-149"]}, "rules": {"nodes": []}}
        self.gh.prs[("o", "r", 11)] = first
        self.gh.heads[("o", "r", "a" * 40)] = checks
        self.budgets(rest=(300, 3000), graphql=(4000, 3000))
        self.daemon.gql_log = [(NOW - 60, "pr-pages", gg.PR_PAGES_POINTS_PER_HOUR)]
        gg.register("pr:o/r#11")
        self.daemon.tick(NOW)
        data = gg.read_cache("pr:o/r#11")["data"]
        self.assertEqual(len(data["checks"]), 100)
        self.assertEqual(data["checksPageError"], "graphql: PR check pages over their hourly share")
        self.assertIsNone(gg.verdict("pr", data))  # a required check past the first page is unread: pending
        self.assertIsNone(gg.verdict("pr", data, until="required"))
        self.assertEqual(len(self.gh.gql_calls()), 1)

    def test_a_stale_graphql_counter_rate_limit_cannot_see_does_not_force_a_read_every_tick(self) -> None:
        """Review of #1834 (F3): a poorer GraphQL counter seen long ago, which /rate_limit does not report, made every
        tick ask /rate_limit. Like REST's counters, a recent /rate_limit answer makes it fresh enough."""
        self.addCleanup(setattr, gg, "BUDGET_REFRESH", gg.BUDGET_REFRESH)
        gg.BUDGET_REFRESH = 120.0
        self.daemon.now = NOW
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4000, "reset": NOW + 900, "seenAt": NOW - 1}}
        self.daemon.note_budget("graphql", 5000, 1200, NOW + 1500)
        self.daemon.now = NOW + 2 * gg.BUDGET_REFRESH
        self.daemon.budget["rest"]["seenAt"] = self.daemon.now - 1
        self.daemon.note_budget("graphql", 5000, 4000, NOW + 3000)  # the counter /rate_limit sees, fresh
        self.daemon.rate_limit_at = self.daemon.now - 1
        before = len([c for c in self.gh.calls if c[1].endswith("/rate_limit")])
        self.daemon.refresh_budget()
        self.assertEqual(len([c for c in self.gh.calls if c[1].endswith("/rate_limit")]), before)

    def test_the_ceiling_takes_every_kind_in_turn(self) -> None:
        """Review of #1834 at e482a2a: with every kind due each tick and a full ceiling, the order skipped a held kind
        that had read nothing (pchch...: runs never read). Every kind reads within a few minutes."""
        reads: list[str] = []

        def fetcher(kind: str):
            def fetch(chunk: list[dict]) -> None:
                reads.append(kind)
                self.daemon.gql_log.append((self.daemon.now, kind, 1))
            return fetch

        fetchers = {k: fetcher(k) for k in ("pr", "comment", "run", "head")}
        keys = {"pr": [f"pr:o/r#{n}" for n in range(30)], "comment": ["comment:o/r#1"], "run": ["run:o/r/1"],
                "head": ["head:o/r@" + "a" * 40]}
        due = lambda kind, entries: [{"key": k, "cache": None} for k in keys[kind]]  # noqa: E731
        self.budgets(rest=(300, 3000), graphql=(4000, 3000))
        self.daemon.paths.update({k: "graphql" for k in keys})
        self.daemon.batch["pr"] = 10
        policy = {**self.daemon.policy, "graphqlPerMinute": 1, "restShare": 0.0}
        with mock.patch.object(gg, "daemon_policy", return_value=policy), \
                mock.patch.object(gg.Daemon, "graphql_fetch", property(lambda d: fetchers)), \
                mock.patch.object(self.daemon, "due", side_effect=due), \
                mock.patch.object(self.daemon, "serve_waits", return_value=0):
            for t in range(0, 8 * 60, 2):
                self.daemon.tick(NOW + t)
        self.assertEqual(set(reads), {"pr", "comment", "run", "head"}, "".join(k[0] for k in reads))
        self.assertTrue(all(reads.count(k) >= 1 for k in ("comment", "run", "head")), reads)

    def test_pr_refreshes_paced_by_the_ceiling_do_not_starve_runs(self) -> None:
        """Review of #1834: with the ceiling held by PR batches every minute, a watched run stayed unread for minutes.
        The kind after the one the ceiling held goes first next tick."""
        for n in range(30, 60):
            self.gh.prs[("o", "r", n)] = pr_node(n, checks=[workflow_check("build", 5)])
            gg.register(f"pr:o/r#{n}")
        self.gh.runs[7] = {"id": 7, "status": "in_progress", "conclusion": None, "run_attempt": 1,
                           "html_url": "https://github.com/o/r/actions/runs/7", "name": "ci"}
        gg.register("run:o/r/7")
        self.budgets(rest=(300, 3000), graphql=(4000, 3000))
        self.daemon.paths["run"] = "graphql"
        self.daemon.policy = {**self.daemon.policy, "graphqlPerMinute": 1, "restShare": 0.0}
        self.daemon.batch["pr"] = 10
        with mock.patch.object(gg, "daemon_policy", return_value=self.daemon.policy):
            self.daemon.tick(NOW)  # PRs first: one query, then the ceiling holds the rest and the run
            self.assertIsNone((gg.read_cache("run:o/r/7") or {}).get("data"))
            for empty in range(2, 60, 2):  # the daemon ticks every 2 s; these meet a full ceiling and read nothing
                self.daemon.tick(NOW + empty)
            self.daemon.tick(NOW + 61)  # PR was held: the comment, run and head kinds go first now
        self.assertIsNotNone((gg.read_cache("run:o/r/7") or {}).get("data"))

    def test_two_graphql_counters_read_as_the_poorer_one(self) -> None:
        """2026-11-04 06:02Z: one daemon's GraphQL reading jumped to the other counter (3938 left, :21 reset) while
        the counter /graphql spends had 1664 (:26 reset). The poorer live counter is the reading, both are kept,
        and one past its reset leaves the other."""
        self.daemon.now = NOW
        self.daemon.budget = {}
        self.daemon.note_budget("graphql", 5000, 1664, NOW + 1440)
        self.daemon.note_budget("graphql", 5000, 3938, NOW + 1140)
        self.assertEqual(self.daemon.budget["graphql"]["remaining"], 1664)
        self.assertEqual(len(self.daemon.budget["graphqlCounters"]), 2)
        self.daemon.note_budget("graphql", 5000, 900, NOW + 1440)  # the spending counter goes on down
        self.assertEqual(self.daemon.budget["graphql"]["remaining"], 900)
        self.daemon.note_budget("graphql", 5000, 400, NOW + 1140)  # now the other counter is the poorer
        self.assertEqual(self.daemon.budget["graphql"]["remaining"], 400)
        self.daemon.now = NOW + 1141  # its window ended; the one still live is the reading
        self.daemon.settle_counters("graphql")
        self.assertEqual(self.daemon.budget["graphql"]["remaining"], 900)
        self.assertEqual(len(self.daemon.budget["graphqlCounters"]), 1)

    def test_graphql_reads_past_the_per_minute_ceiling_wait_for_the_next_minute(self) -> None:
        """A tick that would spend past graphqlPerMinute sends nothing more: its reads are deferred (first next tick),
        not spilled to REST, and once the minute has passed they are read."""
        self.gh.prs[("o", "r", 21)] = pr_node(21, checks=[workflow_check("build", 5)])
        self.budgets(rest=(300, 3000), graphql=(4000, 3000))
        self.daemon.gql_log = [(NOW - 30, "pr", gg.GRAPHQL_PER_MINUTE)]  # this minute's points already spent
        gg.register("pr:o/r#21")
        self.daemon.tick(NOW)
        self.assertEqual(self.gh.gql_calls(), [])
        self.assertEqual([c for c in self.gh.rest_calls() if "/pulls/" in c[1]], [])  # paced, not spilled to REST
        self.assertIn("pr:o/r#21", self.daemon.deferred)
        self.daemon.tick(NOW + 31)  # the spent points are a minute old
        self.assertEqual(len(self.gh.gql_calls()), 1)
        self.assertEqual(gg.read_cache("pr:o/r#21")["data"]["number"], 21)

    def test_a_refresh_of_every_pr_after_a_reset_is_spread_over_minutes(self) -> None:
        """Every watched PR due at once (a reset, a re-exec, a stop below the reserve): at most graphqlPerMinute
        points a minute go out, the rest first in line next tick, so the shared login sees no burst."""
        for n in range(30, 60):
            self.gh.prs[("o", "r", n)] = pr_node(n, checks=[workflow_check("build", 5)])
            gg.register(f"pr:o/r#{n}")
        self.budgets(rest=(300, 3000), graphql=(4000, 3000))
        self.daemon.policy = {**self.daemon.policy, "graphqlPerMinute": 1}
        self.daemon.batch["pr"] = 10
        with mock.patch.object(gg, "daemon_policy", return_value=self.daemon.policy):
            self.daemon.tick(NOW)
            self.assertEqual(len(self.gh.gql_calls()), 1)  # one query (10 PRs); 20 wait
            self.assertEqual(len(self.daemon.deferred), 20)
            self.daemon.tick(NOW + 61)
            self.assertEqual(len(self.gh.gql_calls()), 2)
            self.daemon.tick(NOW + 122)
            self.assertEqual(len(self.gh.gql_calls()), 3)
        unread = [n for n in range(30, 60) if (gg.read_cache(f"pr:o/r#{n}") or {}).get("data") is None]
        self.assertEqual(unread, [], [len(q[1].get("query", "")) if isinstance(q[1], dict) else q for q in self.gh.gql_calls()])

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
        later = NOW + gg.REST_REPO_PART_TTL  # past the shared answer, so the 404 is what is read
        self.daemon.tick(later)
        entry = gg.read_cache("pr:o/r#5")
        self.assertIsNone(entry["error"])
        self.assertEqual(entry["data"]["requiredMissing"], ["ci-status"])
        self.assertEqual(len([c for c in self.gh.rest_calls() if "/branches/main" in c[1]]), 4)  # 404 read
        # a PR first read on the gone base shares the 404, not another PR's kept requirements
        self.gh.prs[("o", "r", 15)] = pr_node(15, checks=[workflow_check("build", 5)])
        gg.register("pr:o/r#15")
        self.daemon.tick(later + gg.CYCLE)
        self.assertIsNone(gg.read_cache("pr:o/r#15")["error"])
        self.assertEqual(gg.read_cache("pr:o/r#15")["data"]["requiredMissing"], [])
        self.assertEqual(gg.read_cache("pr:o/r#5")["data"]["requiredMissing"], ["ci-status"])
        self.assertEqual(len([c for c in self.gh.rest_calls() if "/branches/main" in c[1]]), 4)  # shared

    def test_prs_on_one_base_share_the_repo_and_base_reads(self) -> None:
        """Workflows, rules and the base branch are one answer for every PR on that repository and base: N PRs
        read over REST cost 4 reads each plus 3 in all, within REST_REPO_PART_TTL, and 3 again past it."""
        shared = ("actions/workflows?per_page=100", "rules/branches/main?per_page=100", "branches/main")
        for n in range(1, 6):
            self.gh.prs[("o", "r", n)] = {**pr_node(n, checks=[workflow_check("build", 5)]), "headRefOid": f"{n}" * 40}
            gg.register(f"pr:o/r#{n}")
        self.gh.protected = ["ci-status"]
        self.budgets(rest=(4000, 1800), graphql=(300, 3000))
        asked = lambda: [c[1].split("/o/r/", 1)[1] for c in self.gh.rest_calls()]
        self.daemon.tick(NOW)
        self.assertEqual(sorted(p for p in asked() if p.startswith("pulls/")), [f"pulls/{n}" for n in range(1, 6)])
        for path in shared:
            self.assertEqual(asked().count(path), 1, path)
        self.assertEqual(len(asked()), 5 * 4 + 3)
        for n in range(1, 6):
            entry = gg.read_cache(f"pr:o/r#{n}")
            self.assertEqual(entry["data"]["requiredMissing"], ["ci-status"], n)
            self.assertTrue(all(f"/repos/o/r/{p}" in entry["restParts"] for p in shared), n)  # kept per PR too
        self.daemon.tick(NOW + gg.CYCLE)  # within the TTL: only the PR-level reads (all 304)
        for path in shared:
            self.assertEqual(asked().count(path), 1, path)
        self.assertEqual(len(asked()), 2 * 5 * 4 + 3)
        # past the TTL, one conditional read refreshes each for every PR: a base move is one counted 200
        self.gh.protected = ["ci-status", "lint"]
        before = self.gh.rest_remaining
        self.daemon.tick(NOW + gg.CYCLE + gg.IDLE_BACKOFF_MAX)  # the PRs' next read (idle backoff), past the TTL
        for path in shared:
            self.assertEqual(asked().count(path), 2, path)
        self.assertEqual(before - self.gh.rest_remaining, 1)  # the branch's 200; the rest answered 304
        for n in range(1, 6):
            self.assertEqual(gg.read_cache(f"pr:o/r#{n}")["data"]["requiredMissing"], ["ci-status", "lint"], n)

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


class FallbackTest(Base):
    """A read GraphQL cannot make (its primary quota is spent, or below its floor) is made over REST with the
    same answer, and the reverse; a secondary 403 holds both, never retried in a loop."""

    GQL_SPENT = (200, {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "0", "x-ratelimit-resource": "graphql"},
                 json.dumps({"data": None, "errors": [{"type": "RATE_LIMITED",
                                                        "message": "API rate limit exceeded"}]}).encode())
    REST_SPENT = (403, {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "0", "x-ratelimit-resource": "core"},
                  json.dumps({"message": "API rate limit exceeded"}).encode())
    SECONDARY = (403, {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "3000"},
                 json.dumps({"message": "You have exceeded a secondary rate limit. Please wait a few minutes."}).encode())

    def setUp(self) -> None:
        super().setUp()
        reset = str(int(NOW + 1800))
        for answer in (self.GQL_SPENT, self.REST_SPENT):
            answer[1]["x-ratelimit-reset"] = reset
        checks = [workflow_check("build", 11), workflow_check("test", 11, conclusion="FAILURE"),
                  {"__typename": "StatusContext", "context": "ci/legacy", "state": "PENDING", "targetUrl": "t",
                   "createdAt": iso(NOW - 30)}]
        for c in checks[:2]:
            c["checkSuite"]["app"] = {"slug": "github-actions"}
        self.gh.prs[("o", "r", 1)] = {**pr_node(1, checks=checks), "headRefName": "feat",
                                      "baseRef": {"branchProtectionRule": {"requiredStatusCheckContexts": ["ci-status"]},
                                                  "rules": {"nodes": []}}}
        self.gh.protected = ["ci-status"]
        self.gh.heads[("o", "r", "a" * 40)] = checks
        self.gh.runs[7] = {"id": 7, "name": "CI", "display_title": "fix", "status": "completed", "conclusion": "success",
                           "html_url": "https://github.com/o/r/actions/runs/7", "head_sha": "b" * 40, "head_branch": "x",
                           "event": "push", "run_attempt": 1, "created_at": iso(NOW - 99), "updated_at": iso(NOW - 9)}
        self.gh.issues[("o", "r", 9)] = {"__typename": "PullRequest", "url": "https://github.com/o/r/pull/9", "state": "OPEN",
                                         "comments": {"totalCount": 130, "nodes": [
                                             {"databaseId": 1000 + i, "url": f"https://github.com/o/r/pull/9#c{i}",
                                              "createdAt": iso(NOW - 500 + i),
                                              "author": {"login": "review-bot" if i % 2 else "someone"}, "body": f"comment {i}"}
                                             for i in range(130)]}}
        self.keys = {"pr": "pr:o/r#1", "run": "run:o/r/7", "comment": "comment:o/r#9", "head": f"head:o/r@{'a' * 40}"}

    def force_low(self, *buckets: str) -> None:
        gg.write_json(gg.DAEMON_CONFIG, {"forceLow": list(buckets or ("graphql",))})
        self.addCleanup(lambda: gg.DAEMON_CONFIG.unlink(missing_ok=True))

    def register_all(self) -> None:
        for kind, key in self.keys.items():
            gg.register(key, consumer="autoland" if kind == "head" else None)

    def same(self, kind: str, a: dict, b: dict) -> None:
        if kind == "pr":
            for field in ("state", "mergeable", "mergeStateStatus", "headRefOid", "headRefName", "requiredMissing",
                          "superseded"):  # checkState: the fake rollup is fixed, REST computes it
                self.assertEqual(a[field], b[field], field)
            strip = lambda cs: sorted((c["name"], c["workflow"], c["status"], c["conclusion"], c["required"]) for c in cs)
            self.assertEqual(strip(a["checks"]), strip(b["checks"]))
            self.assertEqual(gg.verdict("pr", a), gg.verdict("pr", b))
        else:
            self.assertEqual(a, b, kind)

    def baseline(self, bucket: str) -> dict[str, dict]:
        """Every kind read once over `bucket` by a daemon of its own, for the answer to compare with."""
        gh, daemon = self.gh, self.daemon
        self.gh = FakeGitHub()
        self.gh.__dict__.update({k: v for k, v in gh.__dict__.items() if k not in {"calls", "step_queries"}})
        self.gh.calls, self.gh.step_queries = [], []
        self.daemon = gg.Daemon(transport=self.gh, token=lambda: TOKEN, controller=None)
        self.daemon.paths = {kind: bucket for kind in gg.CYCLES}
        if bucket == "rest":
            self.force_low()
        self.daemon.tick(NOW)
        gg.DAEMON_CONFIG.unlink(missing_ok=True)
        out = {kind: gg.read_cache(key) for kind, key in self.keys.items()}
        for kind, entry in out.items():
            self.assertEqual(entry.get("via"), bucket, kind)
        for path in gg.cache_dir().glob("*"):
            path.unlink()
        self.gh, self.daemon = gh, daemon
        return {kind: entry["data"] for kind, entry in out.items()}

    def test_graphql_spent_every_read_is_made_over_rest_in_the_same_tick(self) -> None:
        self.register_all()
        over_graphql = self.baseline("graphql")
        self.daemon.paths = {kind: "graphql" for kind in gg.CYCLES}
        self.gh.throttle["graphql"] = self.GQL_SPENT
        self.daemon.tick(NOW)
        self.assertEqual(self.daemon.mode("graphql"), "stop")
        self.assertEqual(len(self.gh.gql_calls()), 1)  # the query that found the quota spent, and no other
        for kind, key in self.keys.items():
            entry = gg.read_cache(key)
            self.assertEqual(entry["via"], "rest", kind)
            self.assertIsNone(entry["error"], kind)
            self.same(kind, entry["data"], over_graphql[kind])
        self.assertIn("read over REST", gg.summary(self.keys["pr"], gg.read_cache(self.keys["pr"])))

    def test_rest_spent_every_read_is_made_over_graphql_in_the_same_tick(self) -> None:
        self.register_all()
        over_rest = self.baseline("rest")
        self.daemon.paths = {kind: "rest" for kind in gg.CYCLES}
        self.force_low()  # so every kind starts on REST
        self.gh.throttle["rest"] = self.REST_SPENT
        self.daemon.tick(NOW)
        self.assertEqual(self.daemon.mode("rest"), "stop")
        self.assertEqual(len(self.gh.rest_calls()), 1)  # the read that found the quota spent, and no other
        for kind, key in self.keys.items():
            entry = gg.read_cache(key)
            self.assertEqual(entry["via"], "graphql", kind)
            self.assertIsNone(entry["error"], kind)
            self.same(kind, entry["data"], over_rest[kind])

    def test_graphql_low_routes_every_kind_to_rest_and_back(self) -> None:
        self.register_all()
        self.daemon.now = NOW
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4000, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 450, "reset": NOW + 1800}}  # below its floor
        self.assertEqual(self.daemon.mode("graphql"), "lean")
        self.assertEqual({kind: self.daemon.route(kind) for kind in gg.CYCLES},
                         {"pr": "rest", "run": "rest", "comment": "rest", "head": "rest"})
        self.daemon.budget["graphql"]["remaining"] = 4000
        self.daemon.budget["rest"]["remaining"] = 1400  # now REST is the low one
        self.assertEqual({kind: self.daemon.route(kind) for kind in gg.CYCLES},
                         {"pr": "graphql", "run": "graphql", "comment": "graphql", "head": "graphql"})
        # both low: nothing to gain, so the cheaper GraphQL reads stay where they are
        self.daemon.budget["graphql"]["remaining"] = 450
        self.assertEqual(self.daemon.route("comment"), "graphql")

    def test_force_low_is_read_from_the_config_and_reads_go_to_rest(self) -> None:
        self.force_low()
        self.register_all()
        self.daemon.tick(NOW)
        self.assertEqual(self.gh.gql_calls(), [])
        self.assertEqual(self.daemon.status["modes"]["graphql"], "lean")
        for kind, key in self.keys.items():
            self.assertEqual(gg.read_cache(key)["via"], "rest", kind)
        gg.write_json(gg.DAEMON_CONFIG, {"forceLow": "graphql"})  # not a list: ignored
        self.assertEqual(gg.daemon_policy()["forceLow"], ())

    def test_a_secondary_403_holds_both_buckets_and_is_never_retried_in_a_loop(self) -> None:
        self.register_all()
        self.daemon.paths = {kind: "graphql" for kind in gg.CYCLES}
        self.gh.throttle["graphql"] = self.SECONDARY
        self.daemon.tick(NOW)
        self.assertEqual(len(self.gh.calls), 1)  # no fallback to REST: the secondary limit is one for both
        self.assertEqual({b: self.daemon.mode(b) for b in ("rest", "graphql")}, {"rest": "stop", "graphql": "stop"})
        for i in range(1, 30):  # a minute of ticks inside the hold: nothing is sent
            self.daemon.tick(NOW + i * gg.TICK)
        self.assertEqual(len(self.gh.calls), 1)
        self.gh.throttle["rest"] = self.SECONDARY
        self.daemon.tick(NOW + gg.SECONDARY_BACKOFF + 1)  # the hold is over: one request, refused again
        self.assertEqual(len(self.gh.calls), 2)
        self.assertEqual(self.daemon.secondary_until - self.daemon.now, 2 * gg.SECONDARY_BACKOFF)  # doubled
        for i in range(1, 2 * gg.SECONDARY_BACKOFF // gg.TICK):
            self.daemon.tick(NOW + gg.SECONDARY_BACKOFF + 1 + i * gg.TICK)
        self.assertEqual(len(self.gh.calls), 2)
        # Retry-After longer than the back-off wins; a clean answer starts the back-off short again
        self.gh.throttle = {"graphql": (403, {"retry-after": "500"}, self.SECONDARY[2])}
        self.daemon.paths = {kind: "graphql" for kind in gg.CYCLES}
        at = NOW + 3 * gg.SECONDARY_BACKOFF + 2
        self.daemon.tick(at)
        self.assertEqual(self.daemon.secondary_until, at + 500)
        self.gh.throttle = {}
        self.register_all()  # the waits are still there (a comment watch lasts only while one is)
        for key in self.keys.values():
            os.utime(gg.watch_dir() / gg.file_name(key), (at + 501, at + 501))
        self.daemon.tick(at + 501)
        self.assertEqual(self.daemon.secondary_strikes, 0)
        # (a head's consumer asked longer ago than HEAD_INTEREST on this clock: nobody wants it read)
        self.assertTrue(all(gg.read_cache(self.keys[kind])["data"] for kind in ("pr", "run", "comment")))

    def test_secondary_hold_refuses_a_read_at_the_request_hook(self) -> None:
        self.daemon.now = NOW
        self.daemon.secondary_hit("graphql", None)
        with self.assertRaises(gg.WatchError):
            self.daemon.request("GET", "/repos/o/r/pulls/1")
        self.assertEqual(self.gh.calls, [])

    def test_the_pacers_hold_stops_every_read_and_a_secondary_403_is_reported_to_it(self) -> None:
        self.register_all()
        gg.BASE.mkdir(parents=True, exist_ok=True)
        gg.write_json(gg.BASE / gg.PACE_FILE, {"hold": {"until": NOW + 120, "since": NOW, "source": "a lane"}})
        self.daemon.tick(NOW)
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self.daemon.status["secondaryHold"]["until"], NOW + 120)
        self.daemon.tick(NOW + 121)
        self.assertTrue(self.gh.calls)  # the hold is over: reads resume
        cli = Path(self.tmp.name) / "pacer" / "gh_quota.py"
        cli.parent.mkdir()
        cli.write_text("")
        (Path(self.tmp.name) / "pacer-gh").symlink_to(cli.parent / "gh")
        ran = []
        self.gh.throttle["graphql"] = self.SECONDARY
        self.daemon.paths = {kind: "graphql" for kind in gg.CYCLES}
        with mock.patch.object(gg, "PACE_CLI", Path(self.tmp.name) / "pacer-gh"), \
                mock.patch.object(gg, "run_bounded", lambda argv, timeout=None: ran.append(argv)):
            self.register_all()
            self.daemon.tick(NOW + 121 + gg.CYCLE)
        self.assertEqual(len(ran), 1)
        self.assertEqual(ran[0][2:], [os.fspath(cli.resolve()), "secondary", "--retry-after", str(gg.SECONDARY_BACKOFF),
                                      "--source", "glaeda-gh"])

    def test_a_hold_stops_free_reads_and_writes_too(self) -> None:
        """The secondary limit counts every request: /rate_limit (free only against the primary limits) and the
        outbox wait the hold out as well."""
        self.daemon.now = NOW
        self.daemon.secondary_hit("rest", None)
        with self.assertRaises(gg.WatchError):
            self.daemon.request("GET", "/rate_limit", free=True)
        gg.BUDGET_REFRESH = 120
        self.daemon.budget = {}
        self.daemon.refresh_budget()
        self.daemon.writing = True
        with self.assertRaises(gg.WatchError):
            self.daemon.request("POST", "/repos/o/r/issues/1/comments", {"body": "x"}, content=True)
        self.daemon.writing = False
        self.daemon.post_tick()
        self.assertEqual(self.gh.calls, [])

    def test_a_refusal_late_in_a_tick_holds_from_when_it_arrived(self) -> None:
        self.daemon.clock_real = True
        self.daemon.now = time.time() - 70  # the tick began 70 s ago
        self.daemon.secondary_hit("graphql", None)
        self.assertGreaterEqual(self.daemon.secondary_until, time.time() + gg.SECONDARY_BACKOFF - 1)
        self.assertFalse(self.daemon.secondary_admit("rest", 1))

    def test_more_statuses_than_rest_reads_is_never_a_verdict(self) -> None:
        many = [{"__typename": "StatusContext", "context": f"ci/{i}", "state": "SUCCESS", "targetUrl": "t",
                 "createdAt": iso(NOW)} for i in range(250)]
        many[240]["state"] = "FAILURE"  # on the third page, past what REST reads
        self.gh.heads[("o", "r", "c" * 40)] = many
        self.gh.prs[("o", "r", 3)] = {**pr_node(3, checks=many), "headRefOid": "c" * 40}
        self.force_low()
        gg.register(f"head:o/r@{'c' * 40}", consumer="autoland")
        gg.register("pr:o/r#3")
        self.daemon.tick(NOW)
        head = gg.read_cache(f"head:o/r@{'c' * 40}")
        self.assertIsNone(head["data"])
        self.assertIn("statuses", head["error"])
        pr = gg.read_cache("pr:o/r#3")
        self.assertEqual(pr["via"], "rest")
        self.assertTrue(pr["data"]["checksTruncated"])
        self.assertIsNone(gg.verdict("pr", pr["data"]))  # pending, never green
        self.assertEqual(len([c for c in self.gh.rest_calls() if "/status?" in c[1] and c[1].endswith("page=2")]), 2)

    def test_more_workflow_runs_than_rest_reads_is_never_a_verdict(self) -> None:
        many = [workflow_check("build", 1000 + i, workflow="CI" if i else "Lint", conclusion="SUCCESS") for i in range(250)]
        many[0]["checkSuite"]["conclusion"] = many[0]["conclusion"] = "FAILURE"
        for c in many:
            c["checkSuite"]["app"] = {"slug": "github-actions"}
        self.gh.heads[("o", "r", "d" * 40)] = many
        self.force_low()
        gg.register(f"head:o/r@{'d' * 40}", consumer="autoland")
        self.daemon.tick(NOW)
        head = gg.read_cache(f"head:o/r@{'d' * 40}")
        self.assertIsNone(head["data"])
        self.assertIn("workflow runs over REST", head["error"])
        # the 200 statuses case says the same: the third page of runs was never read
        self.assertEqual(len([c for c in self.gh.rest_calls() if "actions/runs?" in c[1]]), gg.REST_RUN_PAGES)

    def test_a_write_refused_by_the_secondary_limit_holds_the_reads_too(self) -> None:
        self.daemon.now = NOW
        self.gh.throttle["rest"] = self.SECONDARY
        self.daemon.writing = True
        self.daemon.request("POST", "/repos/o/r/issues/1/comments", {"body": "x"}, content=True)
        self.daemon.writing = False
        self.assertFalse(self.daemon.secondary_admit("graphql", 1))
        self.register_all()
        self.daemon.tick(NOW + gg.TICK)
        self.assertEqual(len(self.gh.calls), 1)

    def test_a_head_whose_second_page_meets_the_limit_is_read_over_rest_and_charged_once(self) -> None:
        many = [check_run(f"CI / job-{i}") for i in range(150)]
        self.gh.heads[("o", "r", "e" * 40)] = many
        key = f"head:o/r@{'e' * 40}"
        gg.register(key, consumer="session-1")
        real = self.gh.__call__
        def second_page_spent(method, url, headers, body):
            if url.endswith("/graphql") and b'after:' in (body or b""):
                return self.GQL_SPENT
            return real(method, url, headers, body)
        self.daemon.transport = second_page_spent
        self.daemon.tick(NOW)
        entry = gg.read_cache(key)
        self.assertEqual(entry["via"], "rest")
        self.assertEqual(len(entry["data"]["found"]), 150)
        self.assertEqual(len(self.daemon.head_spend["session-1"]), 1)  # refunded by the failure, charged by REST

    def test_a_rate_limited_answer_with_partial_data_still_spills_to_rest(self) -> None:
        partial = (200, {"x-ratelimit-remaining": "0", "x-ratelimit-resource": "graphql",
                         "x-ratelimit-reset": str(int(NOW + 1800))},
                   json.dumps({"data": {"rateLimit": {"limit": 5000, "remaining": 0, "resetAt": iso(NOW + 1800), "cost": 1},
                                        "p0": None, "h0": None, "c0": None, "w0": None},
                               "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}).encode())
        self.register_all()
        self.daemon.paths = {kind: "graphql" for kind in gg.CYCLES}
        self.gh.throttle["graphql"] = partial
        self.daemon.tick(NOW)
        for kind, key in self.keys.items():
            entry = gg.read_cache(key)
            self.assertEqual((entry["via"], entry["error"]), ("rest", None), kind)
            self.assertFalse(entry.get("missing"), kind)

    def test_a_low_bucket_always_hands_its_reads_over_and_never_takes_them_back_by_margin(self) -> None:
        self.daemon.now = NOW
        # GraphQL below its floor but about to reset, so "richer" per second than a normal REST
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 2000, "reset": NOW + 3000},
                              "graphql": {"limit": 5000, "remaining": 450, "reset": NOW + 60}}
        self.assertGreater(self.daemon.richness("graphql"), self.daemon.richness("rest"))
        self.daemon.paths["pr"] = "graphql"
        self.assertEqual(self.daemon.route("pr"), "rest")
        self.assertEqual(self.daemon.route("pr"), "rest")  # and stays: no flapping back by the margin

    def test_a_long_pacer_hold_is_honoured(self) -> None:
        gg.BASE.mkdir(parents=True, exist_ok=True)
        self.daemon.clock_real = True
        gg.write_json(gg.BASE / gg.PACE_FILE, {"hold": {"until": time.time() + 1800}})
        self.daemon.adopt_pace_hold()
        self.assertGreater(self.daemon.secondary_until, time.time() + 1700)
        gg.write_json(gg.BASE / gg.PACE_FILE, {"hold": {"until": time.time() + 10 ** 9}})
        self.daemon.adopt_pace_hold()
        self.assertLessEqual(self.daemon.secondary_until, time.time() + gg.PACE_HOLD_MAX)

    def test_a_malformed_force_low_never_stops_the_daemon(self) -> None:
        for value in (True, 1, None, {"graphql": 1}, "graphql"):
            gg.write_json(gg.DAEMON_CONFIG, {"forceLow": value})
            self.assertEqual(gg.daemon_policy()["forceLow"], (), value)
        gg.DAEMON_CONFIG.unlink()

    def test_a_plan_without_rulesets_keeps_graphqls_required_checks(self) -> None:
        gg.register(self.keys["pr"])
        self.daemon.tick(NOW)  # GraphQL names ci-status as required
        self.assertEqual(gg.read_cache(self.keys["pr"])["data"]["requiredMissing"], ["ci-status"])
        self.gh.no_protection_feature = True
        self.gh.throttle["graphql"] = self.GQL_SPENT
        self.daemon.tick(NOW + gg.CYCLE)
        entry = gg.read_cache(self.keys["pr"])
        self.assertIsNone(entry["error"])
        self.assertEqual(entry["via"], "rest")
        self.assertEqual(entry["data"]["requiredMissing"], ["ci-status"])
        asked = len([c for c in self.gh.rest_calls() if "/rules/" in c[1]])
        self.daemon.tick(NOW + 2 * gg.CYCLE)
        self.assertEqual(len([c for c in self.gh.rest_calls() if "/rules/" in c[1]]), asked)  # not asked again
        self.daemon.repo_parts.clear()  # nor once the shared answer is gone: the PR's own kept answer stands
        self.daemon.tick(NOW + 3 * gg.CYCLE + gg.REST_REPO_PART_TTL)
        self.assertEqual(len([c for c in self.gh.rest_calls() if "/rules/" in c[1]]), asked)
        self.assertEqual(gg.read_cache(self.keys["pr"])["data"]["requiredMissing"], ["ci-status"])

    def test_rest_comments_read_the_last_fifty_across_pages(self) -> None:
        self.assertEqual(gg.slim_comments([{"id": 1, "user": {"login": "cmux-bot[bot]"}, "body": "x"}])[0]["author"],
                         {"login": "cmux-bot"})
        self.force_low()
        gg.register(self.keys["comment"])
        self.daemon.tick(NOW)
        data = gg.read_cache(self.keys["comment"])["data"]
        self.assertEqual([c["id"] for c in data["comments"]], list(range(1080, 1130)))  # pages 1 and 2
        self.assertEqual(data["total"], 130)


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
        if key.startswith("run:") and gg.terminal("run", data):
            # what the daemon's own GitHub read of a finished run leaves (run_unconfirmed); a controller's record
            # has no restRun unless a test gives it one
            if "restRun" not in extra and extra.get("source") != "controller":
                extra["restRun"] = gg.rest_run(data)
            if extra.get("restRun") is not None:
                extra.setdefault("restRunAt", at)
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
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), self.diagnostics_in(out):
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

    def test_wait_run_uses_controller_long_poll_once(self) -> None:
        run = {"id": 9, "repo": "o/r", "name": "fast", "event": "workflow_dispatch",
               "head_sha": "a" * 40, "attempt": 1, "status": "completed", "conclusion": "success",
               "url": "https://github.com/o/r/actions/runs/9", "updated_at": iso(time.time())}
        controller = mock.Mock()
        controller.run.return_value = run
        with mock.patch.object(gg.Controller, "from_env", return_value=controller):
            entry = gg.controller_wait_run("run:o/r/9", timeout=5)
        self.assertEqual(entry["data"]["status"], "completed")
        self.assertEqual(controller.run.call_args_list,
                         [mock.call(9, wait=0), mock.call(9, wait=5)])
        self.assertIsNotNone(entry["restAt"])

    def test_the_client_long_poll_ends_by_the_stale_reread(self) -> None:
        """The long-poll does not see the cache: a lost finish the daemon reads from GitHub at STALE_RUN_REREAD reached
        the waiter only when the poll ended, 300 s in (2026-10-27). It ends by then, and the daemon rules from there."""
        controller = mock.Mock()
        controller.run.return_value = ctl_run(9, "in_progress", "", iso(time.time()), repo="o/r")
        with mock.patch.object(gg.Controller, "from_env", return_value=controller):
            gg.controller_wait_run("run:o/r/9", timeout=3600)
        self.assertEqual(controller.run.call_args_list, [mock.call(9, wait=0), mock.call(9, wait=gg.STALE_RUN_REREAD)])
        self.assertLessEqual(gg.CLIENT_RUN_LONG_POLL, gg.STALE_RUN_REREAD)
        self.assertGreater(gg.WAIT_INTEREST, gg.CLIENT_RUN_LONG_POLL, "the wait stays marked through the poll")

    def test_long_poll_result_is_fresh_for_the_wait_started_before_it(self) -> None:
        run = ctl_run(14, "completed", "success", repo="o/r")
        controller = mock.Mock()
        controller.run.return_value = run
        req = gg.wait_request("run", "o/r/14", "green", 5, False, False, None, True, gg.RESCUE_GRACE)
        with mock.patch.object(gg.Controller, "from_env", return_value=controller):
            self.assertIsNotNone(gg.controller_wait_run("run:o/r/14", 5))
        # an empty cache: the controller's finish alone is not ruled on (a rerun's deliveries can be lost)
        self.assertIsNone(gg.wait_step(req, time.time()))
        # once a GitHub read showed the same attempt finished, the long-poll's answer is fresh for the wait
        self.put("run:o/r/14", gg.read_cache("run:o/r/14")["data"], source="controller",
                 restRun={"run_attempt": 1, "status": "completed", "conclusion": "success"})
        with mock.patch.object(gg.Controller, "from_env", return_value=controller):
            self.assertIsNotNone(gg.controller_wait_run("run:o/r/14", 5))
        result = gg.wait_step(req, time.time())
        self.assertIsNotNone(result)
        self.assertEqual(result[0], gg.EXIT_OK)
        # and with GitHub unreadable, the controller's finish counts once it is REST_SAFETY old
        self.put("run:o/r/14", gg.read_cache("run:o/r/14")["data"], source="controller")
        entry = gg.read_cache("run:o/r/14")
        self.assertTrue(gg.controller_unconfirmed(entry, entry["dataAt"] + 1))
        self.assertFalse(gg.controller_unconfirmed(entry, entry["dataAt"] + gg.REST_SAFETY))

    def test_the_handoff_keeps_the_wait_start_and_its_deadline(self) -> None:
        """After the long-poll, the daemon's request keeps the wait's own start: a controller finish read during the
        long-poll still counts once it is REST_SAFETY old, and the deadline does not move."""
        began = time.time() - 40
        with mock.patch.object(gg.os, "mkfifo"):
            rid = gg.write_wait_request("run", "o/r/14", "done", 20, False, False, None, True, gg.RESCUE_GRACE,
                                        os.getpid(), began=began)
        req = gg.read_json(gg.waits_dir() / f"{rid}.json")
        self.assertEqual(req["began"], began)
        self.assertAlmostEqual(req["began"] + req["timeout"], time.time() + 20, delta=2)

    def test_controller_wait_rejects_foreign_repo(self) -> None:
        controller = mock.Mock()
        controller.run.return_value = ctl_run(10, "completed", "success", repo="other/repo")
        with mock.patch.object(gg.Controller, "from_env", return_value=controller):
            self.assertIsNone(gg.controller_wait_run("run:o/r/10", 5))
        controller.run.assert_called_once_with(10, wait=0)

    def test_controller_wait_does_not_clobber_newer_attempt(self) -> None:
        key = "run:o/r/12"
        self.put(key, {"id": 12, "status": "queued", "conclusion": None, "run_attempt": 2,
                       "updated_at": iso(time.time() + 20)})
        controller = mock.Mock()
        controller.run.side_effect = [ctl_run(12, "completed", "failure", iso(time.time()), repo="o/r"),
                                      ctl_run(12, "completed", "failure", iso(time.time()), repo="o/r")]
        with mock.patch.object(gg.Controller, "from_env", return_value=controller):
            self.assertIsNone(gg.controller_wait_run(key, 5))
        self.assertEqual(gg.read_cache(key)["data"]["run_attempt"], 2)

    def test_controller_wait_preserves_controller_rest_safety_clock(self) -> None:
        key = "run:o/r/15"
        old_rest = time.time() - 120
        self.put(key, {"id": 15, "status": "in_progress", "conclusion": None, "run_attempt": 1,
                       "updated_at": iso(time.time())}, source="controller", restAt=old_rest)
        controller = mock.Mock()
        current = iso(time.time() + 1)
        controller.run.side_effect = [ctl_run(15, "in_progress", updated=current, repo="o/r"),
                                       ctl_run(15, "in_progress", updated=current, repo="o/r")]
        with mock.patch.object(gg.Controller, "from_env", return_value=controller):
            entry = gg.controller_wait_run(key, 5)
        self.assertEqual(entry["restAt"], old_rest)

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

    def dispatched_run(self, title: str | None, conclusion: str | None = "success", event: str = "workflow_dispatch") -> dict:
        """A fast-checks dispatch: its head is main's tip (cccc...), whatever commit it tested."""
        return {"id": 9, "name": "Fast checks", "display_title": title, "status": "completed" if conclusion else "in_progress",
                "conclusion": conclusion, "html_url": "https://github.com/o/r/actions/runs/9", "head_sha": "c" * 40,
                "head_branch": "main", "event": event, "run_attempt": 1, "created_at": None, "updated_at": None}

    def test_a_dispatch_naming_the_full_commit_in_its_run_name_is_that_commits(self) -> None:
        tested = "b" * 40
        green = self.dispatched_run(f"Fast checks {'a' * 40} tests o/r@{tested}")
        self.assertEqual(self.wait("run", "o/r/9", fill=(green, {}), sha=tested)[0], 0)
        red = self.dispatched_run(f"Fast checks {'a' * 40} tests o/r@{tested}", "failure")
        self.assertEqual(self.wait("run", "o/r/9", fill=(red, {}), sha=tested)[0], 1)
        running = self.dispatched_run(f"Fast checks {'a' * 40} tests o/r@{tested}", None)
        code, text = self.wait("run", "o/r/9", fill=(running, {}), sha=tested, timeout=0.4)
        self.assertEqual(code, 2)
        self.assertNotIn("GitHub still shows head", text)  # it is the right run, still running
        # its own head still counts, as for any run
        self.assertEqual(self.wait("run", "o/r/9", fill=(green, {}), sha="c" * 40)[0], 0)

    def test_a_run_name_never_matches_another_commit_a_prefix_or_another_repo(self) -> None:
        tested = "b" * 40
        title = f"Fast checks {'a' * 40} tests o/r@{tested}"
        refused = [
            (self.dispatched_run(title), "d" * 40),  # another commit
            (self.dispatched_run(title), "bbbbbbbbbb"),  # a prefix of the named commit (resolves nowhere here)
            (self.dispatched_run(title), "a" * 40),  # the snapshot named bare: not the tested commit
            (self.dispatched_run(f"Fast checks {'a' * 40} tests o/other@{tested}"), tested),  # another repo's
            (self.dispatched_run(f"Fast checks {'a' * 40} tests o/r@{tested}0"), tested),  # longer than an id
            (self.dispatched_run(title, event="push"), tested),  # only a dispatch names what it tested
            (self.dispatched_run(f"Fast checks {tested}"), tested),  # no repo: the old run-name
        ]
        for run, sha in refused:
            with self.subTest(title=run["display_title"], sha=sha, event=run["event"]):
                with self.assertRaisesRegex(gg.WatchError, "is for commit cccccccccc"):
                    self.wait("run", "o/r/9", fill=(run, {}), sha=sha)
                self.assertIsNone(gg.verdict("run", run, sha=sha, repo="o/r"))

    def test_a_dispatch_without_its_run_name_waits_for_one(self) -> None:
        # the controller's record has no run-name: neither accepted nor refused until GitHub's own read
        code, _ = self.wait("run", "o/r/9", fill=(self.dispatched_run(None), {}), sha="b" * 40, timeout=0.4)
        self.assertEqual(code, 2)

    def test_names_commit_is_exact(self) -> None:
        sha = "0123456789abcdef0123456789abcdef01234567"
        self.assertTrue(gg.names_commit(f"Fast checks {'a' * 40} tests manaflow-ai/cmux@{sha}", "manaflow-ai/cmux", sha))
        self.assertTrue(gg.names_commit(f"x Manaflow-AI/cmux@{sha.upper()}", "manaflow-ai/cmux", sha))
        self.assertFalse(gg.names_commit(f"x manaflow-ai/cmux@{sha}", "manaflow-ai/cmux", sha[:12]))
        self.assertFalse(gg.names_commit(f"x manaflow-ai/cmux@{sha[:12]}", "manaflow-ai/cmux", sha))
        self.assertFalse(gg.names_commit(f"x evil/manaflow-ai/cmux@{sha}", "manaflow-ai/cmux", sha))
        self.assertFalse(gg.names_commit(None, "manaflow-ai/cmux", sha))

    def test_missing_pr_fails(self) -> None:
        code, text = self.wait("pr", "o/r#404", fill=(None, {"error": "Could not resolve", "missing": True}))
        self.assertEqual(code, 1)
        self.assertIn("Could not resolve", text)
        # the agent output contract: what is wrong, then `next:` last, on the stream the answer uses
        lines = text.strip().splitlines()
        self.assertEqual(lines[-1], "next: gh pr list -R o/r --state all --limit 5")
        self.assertIn("no such PR or issue on GitHub", lines[-2])

    def test_a_missing_pr_is_not_found_in_json(self) -> None:
        self.put("pr:o/r#404", None, error="Could not resolve", missing=True)
        req = gg.wait_request("pr", "o/r#404", "green", 60, False, True, None, True, 0)
        req["began"] -= 5
        code, out = gg.wait_step(req, time.time())
        doc = json.loads(out)
        self.assertEqual(code, gg.EXIT_FAIL)
        self.assertEqual((doc["ok"], doc["code"], doc["result"]), (False, "not-found", "failure"))
        self.assertEqual(doc["next"], "gh pr list -R o/r --state all --limit 5")
        run = gg.wait_request("run", "o/r/9", "green", 60, False, True, None, True, 0)
        self.assertEqual(gg.not_found_fields(run["key"])["next"], "gh run list -R o/r --limit 5")

    def test_a_timeout_names_its_deadline_and_the_wait_to_run_again(self) -> None:
        running = gg.parse_pr(pr_node(1, rollup="PENDING", checks=[check_run("a", "IN_PROGRESS", None)]))
        code, text = self.wait("pr", "o/r#1", fill=(running, {}), timeout=0.3)
        self.assertEqual(code, gg.EXIT_TIMEOUT)
        lines = text.strip().splitlines()
        self.assertEqual(lines[-1], "next: glaeda-gh wait pr o/r#1 --timeout 0.3")
        self.assertRegex(lines[-2], r"^glaeda-gh: waited 0 s for o/r#1 \(deadline \d\d:\d\d:\d\dZ\); last seen: ")
        self.assertIn("checks PENDING", lines[-2])

    def test_a_wait_says_how_it_stands_while_it_blocks(self) -> None:
        running = gg.parse_pr(pr_node(1, rollup="PENDING", checks=[check_run("a", "IN_PROGRESS", None)]))
        with mock.patch.object(gg, "WAIT_PROGRESS_EVERY", 0.1):
            code, text = self.wait("pr", "o/r#1", fill=(running, {}), timeout=0.5)
        self.assertEqual(code, gg.EXIT_TIMEOUT)
        progress = [line for line in text.splitlines() if line.startswith("glaeda-gh: still waiting on pr:o/r#1: ")]
        self.assertTrue(progress, text)
        self.assertRegex(progress[0], r"\d+s of 0.5s, deadline \d\d:\d\d:\d\dZ; o/r#1 OPEN .*; checks PENDING")

    def test_a_daemon_ruled_wait_keeps_the_state_its_blocker_prints(self) -> None:
        running = gg.parse_pr(pr_node(1, rollup="PENDING", checks=[check_run("a", "IN_PROGRESS", None)]))
        self.put("pr:o/r#1", running)
        req = gg.wait_request("pr", "o/r#1", "green", 60, False, False, None, True, 0)
        d = gg.waits_dir()
        d.mkdir(parents=True, exist_ok=True)
        gg.Daemon.note_wait_state(d, "1-x", req)
        state = (d / "1-x.out.state").read_text()
        self.assertTrue(state.startswith("o/r#1 OPEN") and state.count("\n") == 1, state)
        self.assertIn("checks PENDING", state)
        self.assertIn(".out.state", gg.WAIT_FILES)
        self.assertIn('$out.state', gg.BLOCKER)

    def test_status_says_not_found_and_could_not_judge_apart(self) -> None:
        self.put("pr:o/r#404", None, error="Could not resolve", missing=True)
        self.put("pr:o/r#5", None, error="graphql: HTTP 502")
        out = io.StringIO()
        with mock.patch.object(gg, "daemon_running", return_value=True), \
                mock.patch.object(gg, "daemon_failing", return_value=None), \
                mock.patch.object(gg, "daemon_stale", return_value=None), contextlib.redirect_stdout(out):
            self.assertEqual(gg.cmd_status("pr", "o/r#404", False, False, first_wait=0), gg.EXIT_FAIL)
            self.assertEqual(gg.cmd_status("pr", "o/r#5", False, True, first_wait=0), gg.EXIT_DOWN)
        plain, doc = out.getvalue().split("\n{", 1)
        self.assertEqual(plain.strip().splitlines()[-1], "next: gh pr list -R o/r --state all --limit 5")
        doc = json.loads("{" + doc)
        self.assertEqual((doc["ok"], doc["code"], doc["next"]), (False, "no-data-yet", "glaeda-gh wait pr o/r#5 --timeout 600"))

    def test_a_run_github_cannot_resolve_is_missing_only_when_never_read(self) -> None:
        self.daemon.now = NOW
        entries = [{"key": "run:o/r/1", "cache": None}, {"key": "run:o/r/2", "cache": {"data": {"id": 2}}}]
        with mock.patch.object(self.daemon, "graphql", return_value=({"data": {"w0": None, "w1": None}}, None)):
            self.daemon.fetch_runs_graphql(entries)
        self.assertTrue(gg.read_cache("run:o/r/1")["missing"])
        self.assertFalse(gg.read_cache("run:o/r/2").get("missing"))  # it had data: an error, not gone
        limited = {"data": {"w0": None}, "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}
        with mock.patch.object(self.daemon, "graphql", return_value=(limited, None)):
            self.daemon.fetch_runs_graphql([{"key": "run:o/r/3", "cache": None}])
        self.assertFalse(gg.read_cache("run:o/r/3").get("missing"))

    def test_a_never_read_key_a_wait_is_on_goes_ahead_of_deferred_ones(self) -> None:
        self.daemon.now = NOW
        old = [{"key": f"pr:o/r#{n}", "cache": {"data": {}, "fetchedAt": NOW - 999}, "touched": NOW} for n in range(1, 4)]
        new = {"key": "pr:o/r#999", "cache": None, "touched": NOW, "waitedAt": NOW}
        self.daemon.deferred = {e["key"] for e in old}
        due = [e["key"] for e in self.daemon.due("pr", old + [new])]
        self.assertEqual(due[0], "pr:o/r#999")
        self.assertEqual(due[1:], [e["key"] for e in old])

    def test_a_usage_error_names_the_right_form(self) -> None:
        self.assertEqual(gg.usage_next(["wait", "pr", "manaflow-ai/cmuxterm-hq", "0"]),
                         "glaeda-gh wait pr manaflow-ai/cmuxterm-hq#0 --timeout 1800")
        self.assertEqual(gg.usage_next(["status", "pr", "o/r", "#7", "--json"]), "glaeda-gh status pr o/r#7 --json")
        self.assertEqual(gg.usage_next(["wait", "bogus"]), "glaeda-gh wait --help")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(gg, "say_waiting"), self.assertRaises(SystemExit) as exit_:
            gg.main(["wait", "pr", "o/r", "3", "--json"])
        self.assertEqual(exit_.exception.code, 64)
        doc = json.loads(out.getvalue())
        self.assertEqual((doc["ok"], doc["code"]), (False, "usage"))
        self.assertEqual(doc["next"], "glaeda-gh wait pr o/r#3 --json --timeout 1800")

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

    def test_a_skipped_twin_in_the_same_run_is_not_a_failed_attempt(self) -> None:
        # manaflow-ai/cmux run 40341221417 (PR #25393): "cmux-next Release compile (Xcode 26)" has two jobs in
        # one run. The real one (121636522702, blacksmith macOS) was queued; its twin (121636524434, ubuntu)
        # was skipped with no steps and has the higher id. Status called the queued job a failed attempt
        # replaced by a newer one, and counted the skipped twin as the check.
        name = "cmux-next Release compile (Xcode 26)"
        suite = {"conclusion": None, "workflowRun": {"databaseId": 40341221417, "workflow": {"name": "cmux-next"}}}
        real = {**check_run(name, "QUEUED", None), "databaseId": 121636522702, "startedAt": None,
                "detailsUrl": "https://ci/job/121636522702", "checkSuite": suite}
        twin = {**check_run(name, conclusion="SKIPPED"), "databaseId": 121636524434,
                "startedAt": "2026-10-26T14:37:31Z", "completedAt": "2026-10-26T14:37:31Z",
                "detailsUrl": "https://ci/job/121636524434", "checkSuite": suite}
        for order in ([real, twin], [twin, real]):
            data = gg.parse_pr(pr_node(1, rollup="PENDING", checks=order))
            self.assertEqual([(c["status"], c["url"]) for c in data["checks"]],
                             [("QUEUED", "https://ci/job/121636522702")])
            self.assertEqual(data["superseded"], [])
            self.assertIsNone(gg.verdict("pr", data))
            text = gg.summary("pr:o/r#1", {"data": data})
            self.assertNotIn("superseded", text)
            self.assertIn("1 pending", text)
        # The real job's result is what counts once it finishes, whichever id is higher.
        failed = {**real, "status": "COMPLETED", "conclusion": "FAILURE"}
        data = gg.parse_pr(pr_node(1, rollup="FAILURE", checks=[failed, twin]))
        self.assertEqual(gg.verdict("pr", data), 1)
        passed = {**real, "status": "COMPLETED", "conclusion": "SUCCESS"}
        self.assertEqual(gg.verdict("pr", gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[twin, passed]))), 0)
        # A twin cancelled before it started does not replace the queued job either.
        cancelled = {**twin, "conclusion": "CANCELLED"}
        # It stays as evidence (GitHub's rollup still counts it), but status never names it, whichever
        # of the pair has the lower id.
        live = {**real, "databaseId": 121636524435}
        for order in ([real, cancelled], [cancelled, real], [live, cancelled], [cancelled, live]):
            data = gg.parse_pr(pr_node(1, rollup="PENDING", checks=order))
            self.assertEqual([c["status"] for c in data["checks"]], ["QUEUED"])
            self.assertEqual([c["ranNothing"] for c in data["superseded"]], [True])
            self.assertNotIn("superseded", gg.summary("pr:o/r#1", {"data": data}))
            # Past 100 checks GitHub's FAILURE rollup may be that twin: the queued job is waited for.
            self.assertIsNone(gg.check_verdict({**data, "checkState": "FAILURE", "checksTruncated": True}, "done"))
            self.assertIsNone(gg.check_verdict({**data, "checkState": "FAILURE", "checksTruncated": True}, "green"))
        # A pending check that is superseded reads as pending, never as failed.
        line = gg.summary("pr:o/r#1", {"data": {**data, "superseded": [gg.brief(data["checks"][0])]}}, verbose=True)
        self.assertIn(f"superseded: cmux-next / {name} pending", line)
        # The head reading picks the running job over the skipped twin too.
        self.assertEqual(gg.newest_verdict([(121636522702, "queued", ""), (121636524434, "completed", "skipped")]),
                         ("queued", ""))
        self.assertEqual(gg.newest_verdict([(1, "completed", "skipped")]), ("completed", "skipped"))

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

    def status(self, *args, **kwargs) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = gg.cmd_status(*args, first_wait=0, **kwargs)
        return code, out.getvalue()

    def test_status_takes_several_targets_and_bare_numbers_reuse_the_repository(self) -> None:
        self.assertEqual(gg.status_keys("pr", ["O/R#1", "2", "#3", "o/r#1", "https://github.com/x/y/pull/4", "5"]),
                         ["pr:o/r#1", "pr:o/r#2", "pr:o/r#3", "pr:x/y#4", "pr:x/y#5"])
        self.assertEqual(gg.status_keys("comment", ["o/r#1", "2"]), ["comment:o/r#1", "comment:o/r#2"])
        with self.assertRaisesRegex(gg.WatchError, "needs an OWNER/REPO#N before it"):
            gg.status_keys("pr", ["7", "o/r#1"])
        with self.assertRaises(gg.WatchError):
            gg.status_keys("run", ["o/r/1", "2"])  # a run has no bare form

    def test_several_targets_print_a_block_each_and_json_gives_a_list(self) -> None:
        self.put("pr:o/r#1", gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a")])))
        self.put("pr:o/r#2", gg.parse_pr(pr_node(2, rollup="FAILURE", checks=[check_run("b", conclusion="FAILURE")])))
        code, text = self.status("pr", ["o/r#1", "2"], False, False)
        self.assertEqual(code, 0)
        self.assertEqual(text, gg.render("pr:o/r#1", gg.read_cache("pr:o/r#1"), False)
                         + gg.render("pr:o/r#2", gg.read_cache("pr:o/r#2"), False))
        code, text = self.status("pr", ["o/r#1", "o/r#2"], False, True)
        doc = json.loads(text)
        self.assertEqual([d["key"] for d in doc], ["pr:o/r#1", "pr:o/r#2"])
        self.assertEqual(doc[1]["data"]["checkState"], "FAILURE")
        # one without data yet: its entry is still listed, and the exit says not everything was there (3: a
        # target the daemon has not read is one it could not judge, not a failure)
        code, text = self.status("pr", ["o/r#1", "3"], False, True)
        self.assertEqual(code, gg.EXIT_DOWN)
        self.assertEqual([d["key"] for d in json.loads(text)], ["pr:o/r#1", "pr:o/r#3"])

    def test_one_target_prints_exactly_what_it_always_has(self) -> None:
        self.put("pr:o/r#1", gg.parse_pr(pr_node(1, rollup="PENDING", checks=[check_run("a")])))
        entry = gg.read_cache("pr:o/r#1")
        # --json adds the agent output contract's ok, code, reason and next to the same object
        ok = gg.ok_fields()
        self.assertEqual(self.status("pr", "o/r#1", False, True), (0, gg.render("pr:o/r#1", entry, True, ok)))
        self.assertEqual(self.status("pr", ["o/r#1"], False, True), (0, gg.render("pr:o/r#1", entry, True, ok)))
        self.assertEqual(self.status("pr", ["o/r#1"], False, False), (0, gg.render("pr:o/r#1", entry, False)))

    def test_field_prints_just_those_values(self) -> None:
        red = [check_run("lint", conclusion="FAILURE"), check_run("test", conclusion="FAILURE"), check_run("ok")]
        red[0]["checkSuite"] = {"workflowRun": {"databaseId": 5, "workflow": {"databaseId": 1, "name": "CI"}}}
        self.put("pr:o/r#1", gg.parse_pr(pr_node(1, rollup="FAILURE", checks=red)))
        self.put("pr:o/r#2", gg.parse_pr(pr_node(2, state="MERGED", rollup="SUCCESS", checks=[check_run("ok")])))
        fields = gg.parse_fields("head,checks.failed")
        self.assertEqual(self.status("pr", "o/r#1", False, False, fields=fields), (0, f"{'a' * 40}\tlint,test\n"))
        code, text = self.status("pr", ["o/r#1", "2", "3"], False, False, fields=gg.parse_fields("state,checks.state"))
        self.assertEqual(code, gg.EXIT_DOWN)  # #3 has no data yet: an empty row, and a target not judged (3)
        self.assertEqual(text, "o/r#1\tOPEN\tFAILURE\no/r#2\tMERGED\tSUCCESS\no/r#3\t\t\n")
        code, text = self.status("pr", "o/r#2", False, False, fields=gg.parse_fields("isDraft,checks.total,url"))
        self.assertEqual(text, "false\t1\thttps://github.com/o/r/pull/2\n")

    def test_an_unknown_field_names_the_valid_keys(self) -> None:
        self.put("pr:o/r#1", gg.parse_pr(pr_node(1)))
        with self.assertRaisesRegex(gg.WatchError, r"no 'hed' at the top level; valid keys there: .*\bhead\b"):
            self.status("pr", "o/r#1", False, False, fields=["hed"])
        with self.assertRaisesRegex(gg.WatchError, r"no 'red' at checks; valid keys there: failed, held, passed"):
            self.status("pr", "o/r#1", False, False, fields=["checks.red"])
        # a PR with no data yet refuses the typo too, rather than printing an empty column
        with self.assertRaisesRegex(gg.WatchError, r"no 'hed' at the top level; valid keys there: .*\bhead\b"):
            self.status("pr", ["o/r#1", "9"], False, False, fields=["head", "hed"])
        self.assertEqual(self.status("pr", "o/r#9", False, False, fields=["head", "checks.failed", "mergeable"]),
                         (gg.EXIT_DOWN, "\t\t\n"))
        for bad in ("", "head,", "checks..failed"):
            with self.subTest(bad=bad), self.assertRaises(gg.WatchError):
                gg.parse_fields(bad)

    def test_status_cli_takes_several_targets_and_a_field(self) -> None:
        self.put("pr:o/r#1", gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a")])))
        self.put("pr:o/r#2", gg.parse_pr(pr_node(2, rollup="SUCCESS", checks=[check_run("a")])))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gg.main(["status", "pr", "o/r#1", "2", "--field", "checks.state"]), 0)
        self.assertEqual(out.getvalue(), "o/r#1\tSUCCESS\no/r#2\tSUCCESS\n")

    def test_the_daemon_waits_say_what_they_wait_for_before_they_block(self) -> None:
        """The pr, run and comment kinds, the three that go through the daemon; the file and pid
        kinds are covered in FileAndPidWaitTest. A line wired into some of the four call sites and
        not the others is exactly the failure it exists to rule out, so every kind is driven.
        The deadline is in it too: a reader comparing it with the clock can tell a wait that is
        still inside its timeout from one whose process is gone."""
        for want, call in (("o/r#1", lambda: gg.cmd_wait("pr", "o/r#1", "green", 0.1, False, False)),
                           ("o/r/1", lambda: gg.cmd_wait("run", "o/r/1", "green", 0.1, False, False)),
                           ("o/r#1", lambda: gg.cmd_wait_comment("o/r#1", 0.1, False))):
            with self.subTest(want=want):
                said: list[str] = []
                with mock.patch.object(gg, "say_waiting", said.append), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(call(), gg.EXIT_TIMEOUT)
                self.assertTrue(said, "this kind never said what it was waiting for")
                self.assertIn("waiting on", said[0])
                self.assertIn(want, said[0])
                self.assertIn("0.1s", said[0])

    def test_the_line_comes_out_before_a_daemon_wait_can_finish(self) -> None:
        """Before, not after -- for the kinds that block on the daemon, which is where it matters.
        The file wait had this test; `cmd_wait` did not, so a mutation moving its print into the
        timeout branch (a line on the way *out*) left the whole suite green. A line printed as the
        process exits says nothing about a wait that was killed while blocked, which is the only
        case this exists for. Reading `splitlines()[0]` after the call returns cannot tell the two
        apart: it has to be the order of the writes."""
        order: list[str] = []
        green = gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a")]))
        timer = threading.Timer(0.4, lambda: (order.append("ready"),
                                              self.put("pr:o/r#1", green)))
        timer.start()
        self.addCleanup(timer.cancel)
        with mock.patch.object(gg, "say_waiting", lambda line: order.append("line")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gg.cmd_wait("pr", "o/r#1", "green", 5.0, False, False), 0)
        self.assertEqual(order[:1], ["line"], order)

    def test_daemon_down(self) -> None:
        self.lock.close()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), self.diagnostics_in(err):
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
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), self.diagnostics_in(err):
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

    def test_over_100_checks_a_superseded_cancel_does_not_end_the_wait_while_checks_run(self) -> None:
        """manaflow-ai/cmux#24539 (77 checks shown of more): 0 failed and 7 running, but GitHub's rollup
        said FAILURE over superseded cancelled attempts, and the wait exited 1 after 5s."""
        running = check_run("fast tier", status="IN_PROGRESS", conclusion=None)
        data = gg.parse_pr(pr_node(1, rollup="FAILURE", checks=[check_run("build"), running]))
        cancelled = {"name": "changes", "conclusion": "CANCELLED"}
        many = {**data, "checksTruncated": True, "checkState": "FAILURE", "superseded": [cancelled]}
        self.assertIsNone(gg.verdict("pr", many, "green"))
        self.assertIsNone(gg.verdict("pr", many, "required"))
        # a shown failure still fails at once
        broken = gg.parse_pr(pr_node(2, rollup="FAILURE", checks=[check_run("lint", conclusion="FAILURE"), running]))
        self.assertEqual(gg.verdict("pr", {**broken, "checksTruncated": True, "checkState": "FAILURE",
                                           "superseded": [cancelled]}, "green"), 1)
        self.assertEqual(gg.verdict("pr", {**broken, "checksTruncated": True, "checkState": "FAILURE",
                                           "superseded": [cancelled]}, "required"), 1)
        # with no superseded attempt to explain it, the rollup's FAILURE is a check past the first 100
        self.assertEqual(gg.verdict("pr", {**many, "superseded": []}, "green"), 1)
        # once the shown checks finish, the rollup decides as before
        done = gg.parse_pr(pr_node(3, rollup="FAILURE", checks=[check_run("build")]))
        self.assertEqual(gg.verdict("pr", {**done, "checksTruncated": True, "checkState": "FAILURE",
                                           "superseded": [cancelled]}, "green"), 1)

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
        text = gg.summary("pr:o/r#1", {"data": data}, "required", verbose=True)
        self.assertIn("checks SUCCESS: 2 passed, 1 failed, 0 pending (of 3, 2 required); GitHub's rollup says FAILURE", text)
        self.assertIn("cancelled: lint (not required)", text)
        self.assertIn("superseded: Web complexity / Web complexity cancelled, replaced by a newer attempt or run", text)
        # By default (an agent's read) the superseded attempts are one count line, not one line each.
        with mock.patch.dict(os.environ, {"AGENT_VERBOSE": ""}):
            terse = gg.summary("pr:o/r#1", {"data": data}, "required")
        self.assertNotIn("superseded: Web complexity", terse)
        self.assertIn("  1 superseded, replaced by a newer attempt or run (not counted; --verbose lists up to 10)", terse)
        # parse_pr keeps 20; the count line still counts the ones dropped past that
        many = gg.parse_pr(pr_node(1, rollup="FAILURE", checks=[web(n, "CANCELLED") for n in range(1, 26)]
                                   + [web(26, "SUCCESS"), {**check_run("build"), "isRequired": True}]))
        self.assertEqual((len(many["superseded"]), many["supersededOmitted"]), (20, 5))
        with mock.patch.dict(os.environ, {"AGENT_VERBOSE": ""}):
            self.assertIn("  25 superseded, replaced", gg.summary("pr:o/r#1", {"data": many}, "required"))
            # kept ones that ran nothing are not counted; dropped real ones still are
            kept_empty = {**many, "superseded": [{**c, "ranNothing": True} for c in many["superseded"]]}
            self.assertIn("  5 superseded, replaced", gg.summary("pr:o/r#1", {"data": kept_empty}, "required"))
        self.assertEqual(data["supersededOmitted"], 0)
        self.assertIn("cancelled: lint (not required)", terse)
        with mock.patch.dict(os.environ, {"AGENT_VERBOSE": "1"}):
            self.assertIn("superseded: Web complexity", gg.summary("pr:o/r#1", {"data": data}, "required"))
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
        with contextlib.redirect_stderr(err), self.diagnostics_in(err):
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
            for n in range(1, 7):  # its readers keep reading: no idle backoff
                os.utime(gg.watch_dir() / gg.file_name(f"pr:o/r#{n}"), (NOW + cycle * gg.CYCLE,) * 2)
            self.daemon.tick(NOW + cycle * gg.CYCLE)
            lone.append(sizes.count(1))
        self.assertEqual(self.daemon.heavy["pr:o/r#3"]["fails"] >= gg.HEAVY_AFTER, True)
        # read alone on each of the first cycles, then only every LEAN_FACTOR cycles
        self.assertLess(sum(lone[gg.HEAVY_AFTER + 1:]), len(lone) - gg.HEAVY_AFTER - 1)
        poison.clear()  # it answers again: two lone answers put it back into the batches
        for cycle in range(12, 12 + 2 * gg.LEAN_FACTOR + 2):
            for n in range(1, 7):
                os.utime(gg.watch_dir() / gg.file_name(f"pr:o/r#{n}"), (NOW + cycle * gg.CYCLE,) * 2)
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

    def test_a_busy_daemon_reads_the_waited_and_the_longest_unread_first(self) -> None:
        """2026-10-27: with GraphQL stopped, every PR and run went over REST, PRs always ahead of runs; ticks ran out of
        time on the PRs and a run a `wait run` was blocked on went unread for 581 s and counting."""
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4500, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 0, "reset": NOW + 1800}}  # GraphQL stopped
        for n in range(1, 6):
            gg.register(f"pr:o/r#{n}")
        gg.register("run:o/r/1")
        gg.register("run:o/r/2", waited=True)
        for key in [f"pr:o/r#{n}" for n in range(1, 6)] + ["run:o/r/1", "run:o/r/2"]:  # all read a while ago, alike
            gg.write_json(gg.cache_dir() / gg.file_name(key), {"key": key, "fetchedAt": NOW - 2 * gg.CYCLE,
                                                               "dataAt": NOW - 2 * gg.CYCLE, "error": None,
                                                               "data": {"status": "in_progress"}})
        order: list[str] = []

        reads = [0]

        def read(e: dict) -> None:
            reads[0] += 1
            if reads[0] % 3 == 0:
                raise gg.OutOfTime()  # two reads a tick, then the deadline stops the third before it sends
            order.append(e["key"])
            self.daemon.store(e["key"], e["cache"], data={"status": "in_progress"}, dataAt=self.daemon.now)

        with mock.patch.object(self.daemon, "fetch_pr_rest", side_effect=read), \
                mock.patch.object(self.daemon, "fetch_run", side_effect=read):
            self.daemon.tick(NOW)
            self.assertEqual(order[0], "run:o/r/2", "the waited run first, ahead of every PR")
            for t in range(1, 4):
                self.daemon.tick(NOW + t * gg.TICK)
        self.assertEqual(sorted(set(order)), sorted([f"pr:o/r#{n}" for n in range(1, 6)] + ["run:o/r/1", "run:o/r/2"]),
                         "every key is reached: a deferred key never starves behind the rest")

    def test_a_wait_on_a_finished_run_is_confirmed_ahead_of_deferred_reads(self) -> None:
        """2026-11-09: a 60 s confirmation wait on a run whose failure the cache
        already held (read just before the wait began) timed out every time on a daemon with 88 deferred reads, because
        the confirming read (run_needs_confirm) queued behind every deferred key. It now goes first, and the wait rules."""
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4500, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 0, "reset": NOW + 1800}}  # all over REST
        sha = "a" * 40
        for n in range(1, 6):  # a backlog: read long ago, deferred by earlier ticks
            key = f"run:o/r/{n}"
            self.gh.runs[n] = {"id": n, "status": "in_progress", "conclusion": None, "run_attempt": 1}
            gg.register(key)
            gg.write_json(gg.cache_dir() / gg.file_name(key), {"key": key, "fetchedAt": NOW - 2 * gg.CYCLE,
                                                               "dataAt": NOW - 2 * gg.CYCLE, "error": None,
                                                               "data": {"status": "in_progress"}})
            self.daemon.deferred.add(key)
            self.daemon.deferred_since[key] = NOW - 60
        done = {"id": 9, "status": "completed", "conclusion": "failure", "run_attempt": 1, "head_sha": sha,
                "updated_at": iso(NOW - 600)}
        self.gh.runs[9] = dict(done)
        key = "run:o/r/9"
        gg.write_json(gg.cache_dir() / gg.file_name(key), {
            "key": key, "fetchedAt": NOW - 1, "dataAt": NOW - 1, "restAt": NOW - 1, "error": None, "source": "rest",
            "data": done, "restRun": gg.rest_run(done), "restRunAt": NOW - 1})  # GitHub showed it a second too early
        req = gg.wait_request("run", "o/r/9", "green", 60, False, True, sha, True, 0)
        req["began"] = NOW
        gg.register(key, waited=True)
        self.assertIsNone(gg.wait_step(req, NOW), "a finish read before the wait began is not ruled on (a rerun?)")
        self.spend_budget_after(1)  # one read a tick: the backlog keeps the daemon behind
        status = self.daemon.tick(NOW + gg.TICK)
        self.assertEqual(status["fetched"]["run"], 6)
        self.assertEqual(gg.read_cache(key)["restRunAt"], NOW + gg.TICK, "the waited finished run is read first")
        self.assertTrue(all(f"run:o/r/{n}" in self.daemon.deferred for n in range(1, 6)), "the backlog waits a tick")
        code, text = gg.wait_step(req, NOW + gg.TICK)
        self.assertEqual(code, gg.EXIT_FAIL)
        self.assertEqual(json.loads(text)["result"], "failure")
        entry = next(e for e in self.daemon.watches() if e["key"] == key)
        self.daemon.now = NOW + gg.TICK
        self.assertFalse(self.daemon.wait_confirms(entry), "one read per wait: then it is an ordinary key")

    def test_a_wait_held_on_a_confirmed_run_does_not_keep_it_first(self) -> None:
        """Review of glaeda#1924: a wait held on a finished run (rescue grace) touches waitedAt every 30 s, so the run
        needs confirming again after each successful read. Put first for every touch, two such runs took each tick's
        one read (1, 2, 1, 2) and a healthy deferred wait timed out. Priority is once per finish and wait start."""
        self.held_waits(jobs_deferred=False)

    def test_a_confirmation_whose_jobs_read_is_deferred_has_had_its_turn(self) -> None:
        """Review of glaeda#1924: the run read answered but its failed attempt's jobs read ran out of time, so the key
        was deferred, and a deferred confirming read did not count as made: a held `--jobs` wait took the head again."""
        self.held_waits(jobs_deferred=True)

    def held_waits(self, jobs_deferred: bool) -> None:
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4500, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 0, "reset": NOW + 1800}}
        held = ("run:o/r/1", "run:o/r/2")
        for n, key in enumerate(held, 1):
            done = {"id": n, "status": "completed", "conclusion": "failure", "run_attempt": 1}
            gg.write_json(gg.cache_dir() / gg.file_name(key), {
                "key": key, "fetchedAt": NOW - 60, "dataAt": NOW - 60, "restAt": NOW - 60, "error": None,
                "source": "rest", "data": done, "restRun": gg.rest_run(done), "restRunAt": NOW - 60})
            with mock.patch.object(gg.time, "time", return_value=NOW):
                gg.register(key, waited=True, began=NOW)
        healthy = "run:o/r/3"
        gg.write_json(gg.cache_dir() / gg.file_name(healthy), {"key": healthy, "fetchedAt": NOW - 2 * gg.CYCLE,
                                                               "dataAt": NOW - 2 * gg.CYCLE, "error": None,
                                                               "data": {"status": "in_progress"}})
        gg.register(healthy, waited=True)
        self.daemon.deferred.add(healthy)
        self.daemon.deferred_since[healthy] = NOW - 60
        order: list[str] = []

        def read(e: dict) -> None:
            if order and order[-1] == "cut":
                order.pop()
                raise gg.OutOfTime()  # one read a tick
            order.extend([e["key"], "cut"])
            data = (e["cache"] or {}).get("data")
            self.daemon.store(e["key"], e["cache"], data=data, dataAt=self.daemon.now, restAt=self.daemon.now,
                              restRun=gg.rest_run(data), restRunAt=self.daemon.now)  # confirmed: still failed
            if jobs_deferred and e["key"] != healthy:
                self.daemon.deferred.add(e["key"])  # read_jobs: out of time for the jobs, left for next tick

        with mock.patch.object(self.daemon, "fetch_run", side_effect=read):
            for t in range(4):
                at = NOW + t * (gg.CYCLE + gg.TICK)
                for key in (*held, healthy):  # the held waits keep touching
                    with mock.patch.object(gg.time, "time", return_value=at):
                        gg.register(key, waited=True)
                    os.utime(gg.watch_dir() / gg.file_name(key), (at, at))
                self.daemon.tick(at)
                if order and order[-1] == "cut":
                    order.pop()
        self.assertEqual(sorted(order[:2]), list(held), "each held run gets its one read ahead")
        self.assertEqual(order[2], healthy, "then the deferred run, not the held runs again")

    def test_a_failing_confirmation_is_put_first_once_not_every_cycle(self) -> None:
        """Review of glaeda#1924: a failed read keeps restRunAt, so the run still needs confirming. With absolute priority
        every time, two runs GitHub keeps refusing took each tick's one read (1, 2, 1, 2) once ticks outlasted the retry
        cycle, and a healthy deferred run's wait timed out. A failed confirmation now ages and defers like any key."""
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4500, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 0, "reset": NOW + 1800}}
        for n in (1, 2):  # finished, read before the waits began, and GitHub refuses every read of them now
            key = f"run:o/r/{n}"
            done = {"id": n, "status": "completed", "conclusion": "failure", "run_attempt": 1}
            gg.write_json(gg.cache_dir() / gg.file_name(key), {
                "key": key, "fetchedAt": NOW - 60, "dataAt": NOW - 60, "restAt": NOW - 60, "error": None,
                "source": "rest", "data": done, "restRun": gg.rest_run(done), "restRunAt": NOW - 60})
            gg.register(key, waited=True)
        healthy = "run:o/r/3"
        gg.write_json(gg.cache_dir() / gg.file_name(healthy), {"key": healthy, "fetchedAt": NOW - 2 * gg.CYCLE,
                                                               "dataAt": NOW - 2 * gg.CYCLE, "error": None,
                                                               "data": {"status": "in_progress"}})
        gg.register(healthy, waited=True)
        self.daemon.deferred.add(healthy)
        self.daemon.deferred_since[healthy] = NOW - 60
        order: list[str] = []

        def read(e: dict) -> None:
            if order and order[-1] == "cut":
                order.pop()
                raise gg.OutOfTime()  # one read a tick
            order.extend([e["key"], "cut"])
            if e["key"] == healthy:
                self.daemon.store(e["key"], e["cache"], data={"status": "in_progress"}, dataAt=self.daemon.now)
            else:
                self.daemon.store(e["key"], e["cache"], error="HTTP 502")  # restRunAt keeps its old time

        with mock.patch.object(self.daemon, "fetch_run", side_effect=read):
            for t in range(4):  # each tick outlasts the retry cycle, so the failed ones are due again every tick
                at = NOW + t * (gg.CYCLE + gg.TICK)
                for key in ("run:o/r/1", "run:o/r/2", healthy):  # the waits touch their watches as the clock moves
                    with mock.patch.object(gg.time, "time", return_value=at):
                        gg.register(key, waited=True)
                    os.utime(gg.watch_dir() / gg.file_name(key), (at, at))
                self.daemon.tick(at)
                if order and order[-1] == "cut":
                    order.pop()
        self.assertEqual(sorted(order[:2]), ["run:o/r/1", "run:o/r/2"], "each failing run gets its first read ahead")
        self.assertEqual(order[2], healthy, "then the deferred run goes ahead of the failed confirmations")

    def test_a_wait_that_begins_just_after_a_read_gets_its_confirmation_within_the_cycle(self) -> None:
        """Review of glaeda#1924 (F1): the daemon read the finished run a moment before the wait began, and the read
        that confirms it for the wait was paced a whole cycle, as long as a 60 s confirmation wait. A new wait
        (waitBegan) gets that read at once, and only once: its touches do not buy more."""
        key = "run:o/r/9"
        sha = "a" * 40
        self.gh.runs[9] = {"id": 9, "status": "completed", "conclusion": "failure", "run_attempt": 1, "head_sha": sha}
        gg.register(key)
        self.daemon.tick(NOW)
        reads = lambda: len([c for c in self.gh.rest_calls() if c[1].endswith("/actions/runs/9")])
        self.assertEqual(reads(), 1)
        req = gg.wait_request("run", "o/r/9", "green", 60, False, True, sha, True, 0)
        req["began"] = NOW + 1
        with mock.patch.object(gg.time, "time", return_value=NOW + 1):
            gg.register(key, waited=True, began=req["began"])
        self.assertIsNone(gg.wait_step(req, NOW + 1), "the read came before the wait began")
        self.daemon.tick(NOW + gg.TICK)
        self.assertEqual(reads(), 2, "confirmed at once, not a cycle later")
        code, _ = gg.wait_step(req, NOW + gg.TICK)
        self.assertEqual(code, gg.EXIT_FAIL)
        with mock.patch.object(gg.time, "time", return_value=NOW + 30):
            gg.register(key, waited=True)  # a touch: a later waitedAt, the same waitBegan
        self.daemon.tick(NOW + 30)
        self.assertEqual(reads(), 2, "a touch is not a new wait: the cycle paces it again")

    def test_a_new_finish_gets_its_own_first_confirmation(self) -> None:
        """Review of glaeda#1924 (F2): a confirmation that failed is not put first again for the same finish, but a
        rerun's new finish (another restRun) is."""
        self.daemon.now = NOW
        done = {"id": 9, "status": "completed", "conclusion": "failure", "run_attempt": 1}
        cache = {"data": done, "restRun": gg.rest_run(done), "restRunAt": NOW - 60, "source": "rest"}
        e = {"key": "run:o/r/9", "cache": cache, "waitedAt": NOW}
        self.assertTrue(self.daemon.wait_confirms(e))
        self.daemon.confirm_tried["run:o/r/9"] = (NOW - 2, gg.rest_run(done))  # tried, and the read failed
        self.assertFalse(self.daemon.wait_confirms(e))
        rerun = {**done, "run_attempt": 2, "conclusion": "cancelled"}
        e["cache"] = {**cache, "data": rerun, "source": "controller"}  # the controller's word of a new attempt
        self.assertTrue(self.daemon.wait_confirms(e))

    def entry(self, key: str, source: str, at: float, waited: bool = False, deferred: bool = False) -> dict:
        if deferred:
            self.daemon.deferred_before[key] = NOW - 10
        return {"key": key, "cache": {"source": source, "fetchedAt": at, "restAt": at}, "waitedAt": NOW if waited else None}

    def order(self, entries: list[dict]) -> list[str]:
        """The REST order tick() reads in, from the entries due() returned."""
        self.daemon.now = NOW
        return [e["key"] for e in self.daemon.rest_order(entries)]

    def test_a_deferred_run_goes_ahead_of_new_prs(self) -> None:
        """due() put a deferred key first only within its kind, so a deferred run sat behind every new PR."""
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4500, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 0, "reset": NOW + 1800}}
        for n in range(1, 4):
            gg.register(f"pr:o/r#{n}")
        gg.register("run:o/r/1")
        self.daemon.deferred.add("run:o/r/1")
        order: list[str] = []
        with mock.patch.object(self.daemon, "fetch_pr_rest", side_effect=lambda e: order.append(e["key"])), \
                mock.patch.object(self.daemon, "fetch_run", side_effect=lambda e: order.append(e["key"])):
            self.daemon.tick(NOW)
        self.assertEqual(order[0], "run:o/r/1")

    def test_a_read_cut_off_before_its_request_keeps_its_place(self) -> None:
        """A read the tick deadline stopped before it sent anything was not a read: the key stays deferred, first."""
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4500, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 0, "reset": NOW + 1800}}
        for i in (1, 2):
            self.gh.runs[i] = {"id": i, "status": "in_progress", "conclusion": None, "run_attempt": 1}
            gg.register(f"run:o/r/{i}", waited=True)
        self.spend_budget_after(1)  # the first read's request is answered, then the deadline passes
        status = self.daemon.tick(NOW)
        self.assertEqual(status["deferred"], 1)
        (cut,) = self.daemon.deferred
        self.assertNotIn(cut, self.daemon.rest_tried, "not sent, so not counted as a read")
        self.daemon.transport = self.gh
        status = self.daemon.tick(NOW + gg.TICK)
        self.assertEqual(status["fetched"]["run"], 1)
        self.assertIn(cut, self.daemon.rest_tried)

    def test_new_watches_arriving_every_tick_do_not_starve_a_deferred_one(self) -> None:
        """One read a tick and a new (never-read) watch every tick: a key first deferred earlier is read before the
        newer deferrals, whose never-read age would otherwise sort first every time."""
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4500, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 0, "reset": NOW + 1800}}
        for i in (1, 2):
            key = f"run:o/r/{i}"
            gg.register(key)
            gg.write_json(gg.cache_dir() / gg.file_name(key), {"key": key, "fetchedAt": NOW - 2 * gg.CYCLE,
                                                               "dataAt": NOW - 2 * gg.CYCLE, "error": None,
                                                               "data": {"status": "in_progress"}})
        order: list[str] = []

        def read(e: dict) -> None:
            if order and order[-1] == "cut":
                order.pop()
                raise gg.OutOfTime()  # one read a tick
            order.extend([e["key"], "cut"])
            self.daemon.store(e["key"], e["cache"], data={"status": "in_progress"}, dataAt=self.daemon.now)

        with mock.patch.object(self.daemon, "fetch_run", side_effect=read):
            for t in range(6):
                gg.register(f"run:o/r/{100 + t}")  # a new watch every tick
                self.daemon.tick(NOW + t * gg.TICK)
                if order and order[-1] == "cut":
                    order.pop()
        self.assertIn("run:o/r/1", order[:3])
        self.assertIn("run:o/r/2", order[:3], "both deferred keys are read before the newer arrivals pile up")

    def test_a_read_that_defers_itself_does_not_keep_its_place(self) -> None:
        """A read that stores its data and defers itself for the rest (fill_steps_rest, out of time for the steps)
        was served: it queues behind keys deferred while it was read, not ahead of them forever."""
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4500, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 0, "reset": NOW + 1800}}
        order: list[str] = []

        def read(e: dict) -> None:
            if order and order[-1] == "cut":
                order.pop()
                raise gg.OutOfTime()
            order.extend([e["key"], "cut"])
            self.daemon.store(e["key"], e["cache"], data={"status": "in_progress"}, dataAt=self.daemon.now)
            if e["key"] == "run:o/r/1":
                self.daemon.deferred.add(e["key"])  # served, with work left for the next tick

        with mock.patch.object(self.daemon, "fetch_run", side_effect=read):
            for t in range(5):
                if t == 1:  # arrive after run 1 first deferred itself
                    gg.register("run:o/r/2")
                    gg.register("run:o/r/3")
                else:
                    gg.register("run:o/r/1")
                self.daemon.tick(NOW + t * gg.TICK)
                if order and order[-1] == "cut":
                    order.pop()
        self.assertIn("run:o/r/2", order, order)
        self.assertIn("run:o/r/3", order, order)

    def test_a_pr_read_cut_off_partway_does_not_hold_the_head(self) -> None:
        """The real fetch_pr_rest sends several requests; a deadline inside it (after some answers) had its turn: a
        waited run behind it is read next tick, not after that PR forever."""
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4500, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 0, "reset": NOW + 1800}}
        self.daemon.paths["pr"] = self.daemon.paths["run"] = "rest"
        self.gh.prs[("o", "r", 1)] = pr_node(1)
        gg.register("pr:o/r#1")
        self.daemon.deferred.add("pr:o/r#1")
        self.daemon.deferred_since["pr:o/r#1"] = NOW - 600  # deferred long ago, so it heads the order
        self.gh.runs[2] = {"id": 2, "status": "in_progress", "conclusion": None, "run_attempt": 1}
        gg.register("run:o/r/2", waited=True)
        self.daemon.deferred.add("run:o/r/2")
        inner, count = self.daemon.transport, [0]

        def transport(method, url, headers, body):
            count[0] += 1
            if count[0] % 2 == 0:
                self.daemon.tick_deadline = float("-inf")  # two requests a tick: the PR never finishes in one
            return inner(method, url, headers, body)
        self.daemon.transport = transport
        self.daemon.tick(NOW)
        run_reads = lambda: [c for c in self.gh.rest_calls() if "/actions/runs/2" in c[1]]
        self.assertEqual(run_reads(), [], "the PR, deferred first, takes this tick")
        self.daemon.tick(NOW + gg.TICK)
        self.assertTrue(run_reads(), "the waited run is read the tick after the PR's partial read")

    def test_failed_controller_fed_reads_move_back(self) -> None:
        """A controller-fed run keeps restAt when its REST read fails; the failure still moves it back
        (rest_tried), so failing keys cannot hold the head of the order."""
        failing = [self.entry(f"run:o/r/{i}", "controller", NOW - 3600) for i in (1, 2, 3)]
        healthy = self.entry("run:o/r/4", "controller", NOW - 600)
        self.assertEqual(self.order(failing + [healthy])[-1], "run:o/r/4")
        self.daemon.rest_tried.update({"run:o/r/1": NOW - 1, "run:o/r/2": NOW - 1})  # read and failed last tick
        self.assertEqual(self.order(failing + [healthy])[:2], ["run:o/r/3", "run:o/r/4"])

    def test_a_waited_new_key_breaks_the_tie_with_other_new_keys(self) -> None:
        new_pr = {"key": "pr:o/r#1", "cache": None, "waitedAt": None}
        new_run = {"key": "run:o/r/1", "cache": None, "waitedAt": NOW}
        self.assertEqual(self.order([new_pr, new_run]), ["run:o/r/1", "pr:o/r#1"])

    def test_deferred_keys_go_ahead_of_waited_new_ones(self) -> None:
        old = self.entry("pr:o/r#1", "rest", NOW - 30, deferred=True)
        waited = self.entry("run:o/r/2", "controller", NOW - 3600, waited=True)
        self.assertEqual(self.order([waited, old]), ["pr:o/r#1", "run:o/r/2"])

    def test_waiters_kept_busy_do_not_starve_an_unwaited_key(self) -> None:
        """A waited key is aged WAIT_PRIORITY, not ranked absolutely: with waited runs due every cycle and room for
        one read a tick, an unwaited run unread longer than that still gets its turn."""
        self.daemon.now = NOW
        cached = lambda key, at: {"key": key, "cache": {"source": "rest", "fetchedAt": at}, "waitedAt": None}
        old = cached("run:o/r/9", NOW - gg.WAIT_PRIORITY - 60)
        waited = [{**cached(f"run:o/r/{i}", NOW - 30), "waitedAt": NOW} for i in range(1, 4)]
        fresh_unwaited = cached("run:o/r/8", NOW - 10)
        never = {"key": "run:o/r/7", "cache": None, "waitedAt": None}
        entries = [fresh_unwaited, *waited, old, never]
        ordered = self.order(entries)
        self.assertEqual(ordered[:2], ["run:o/r/7", "run:o/r/9"],
                         "a never-read key, then one unread past the waiters' head start")
        self.assertEqual(ordered[-1], "run:o/r/8", "a fresh unwaited key waits behind the waiters")

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
gg.UPSTREAM_CONFIG = Path(sys.argv[3]) / "no-upstream.json"
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
        base = mock.patch.object(gg, "BASE", Path(self.tmp.name) / "gh")  # service_loaded locks BASE/restart.lock
        base.start()
        self.addCleanup(base.stop)

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

    def test_a_launchctl_that_cannot_tell_counts_as_loaded(self) -> None:
        """Only 113 (or 125 for the gui domain) means not there: a serve from a shell is refused otherwise."""
        for script, loaded in (('case "$2" in gui/*) exit 125;; esac\nexit 113\n', False),
                               ('case "$2" in system/*) exit 5;; gui/*) exit 125;; esac\nexit 113\n', True),
                               ('case "$2" in user/*) exit 1;; esac\nexit 113\n', True)):
            bin_dir = Path(tempfile.mkdtemp(dir=self.tmp.name))
            (bin_dir / "launchctl").write_text("#!/bin/sh\n" + script)
            (bin_dir / "launchctl").chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": os.fspath(bin_dir) + os.pathsep + os.environ["PATH"]}), \
                    mock.patch.object(gg.sys, "platform", "darwin"):
                self.assertEqual(gg.service_loaded(), loaded, script)
        with mock.patch.object(gg.sys, "platform", "darwin"), \
                mock.patch.object(gg, "run_bounded", side_effect=subprocess.TimeoutExpired(["launchctl"], 10)):
            self.assertTrue(gg.service_loaded())

    def test_the_user_domain_copy_counts_as_loaded(self) -> None:
        bin_dir = Path(self.tmp.name) / "logged-out"
        bin_dir.mkdir()
        (bin_dir / "launchctl").write_text('#!/bin/sh\ncase "$2" in user/*) exit 0;; gui/*) exit 125;; esac\nexit 113\n')
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
        self.active_for: float | None = None  # the unit as systemctl reports it; None: not active
        self.main_pid = 1  # its MainPID, the pid hold() and silent() write daemon.json as
        unit = mock.patch.object(gg, "unit_active_for", side_effect=lambda: None if self.active_for is None
                                 else (self.active_for, self.main_pid))
        unit.start()
        self.addCleanup(unit.stop)
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
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), self.diagnostics_in(out):
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

    def test_a_daemon_that_waited_on_a_replaced_lock_file_waits_on_the_new_one(self) -> None:
        """The waiter got the old inode once its holder re-executed off it; it must not poll beside the
        daemon that locked the new file."""
        old = open(gg.BASE / "daemon.lock", "a+")
        self.addCleanup(old.close)
        (gg.BASE / "daemon.lock").rename(gg.BASE / "moved.lock")
        opened: list[Any] = []
        real_open = open

        def first_open_is_the_old_file(path: Any, *args: Any, **kwargs: Any) -> Any:
            if Path(path) == gg.BASE / "daemon.lock" and not opened:
                opened.append(old)
                return old  # it opened the file before it was moved
            handle = real_open(path, *args, **kwargs)
            opened.append(handle)
            return handle

        with mock.patch("builtins.open", side_effect=first_open_is_the_old_file):
            lock = gg.hold_daemon_lock()
        self.addCleanup(lock.close)
        self.assertEqual(len(opened), 2)
        self.assertFalse(gg.lock_replaced(lock))
        self.assertTrue(old.closed, "the old inode's lock was let go")

    def test_lock_replaced_sees_a_removed_or_swapped_lock_file(self) -> None:
        lock = open(gg.BASE / "daemon.lock", "a+")
        self.addCleanup(lock.close)
        self.assertFalse(gg.lock_replaced(lock))
        (gg.BASE / "daemon.lock").rename(gg.BASE / "moved.lock")
        self.assertTrue(gg.lock_replaced(lock), "removed")
        (gg.BASE / "daemon.lock").write_text("")
        self.assertTrue(gg.lock_replaced(lock), "another file at the path")
        os.replace(gg.BASE / "moved.lock", gg.BASE / "daemon.lock")
        self.assertFalse(gg.lock_replaced(lock), "its own file back")

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
        linux = mock.patch.object(gg.sys, "platform", "linux")  # macOS keeps its rule (its own test)
        linux.start()
        self.addCleanup(linux.stop)
        with mock.patch.object(gg, "restart_service", return_value=(True, "systemctl --user restart")), \
                mock.patch.object(gg, "lock_holder", return_value=None):
            code, text = self.run_restart()
        self.assertEqual(code, 3, text)
        self.assertIn("Tell the user", text)
        self.assertFalse(gg.restart_record()["ok"])
        # the restart ran, so it starts the cooldown: the daemon it started may only be slow
        self.assertIsNotNone(gg.recent_restart())
        code, text = self.run_restart()
        self.assertEqual(code, 0, text)
        self.assertIn("Do not restart it again", text)

    def test_a_restart_whose_command_failed_starts_no_cooldown(self) -> None:
        self.hold(stale=True)
        gg.RESTART_SETTLE, saved = 0.3, gg.RESTART_SETTLE
        self.addCleanup(setattr, gg, "RESTART_SETTLE", saved)
        with mock.patch.object(gg, "restart_service", return_value=(False, "systemctl --user restart")), \
                mock.patch.object(gg, "lock_holder", return_value=None):
            code, text = self.run_restart()
        self.assertEqual(code, 3, text)
        self.assertIsNone(gg.recent_restart())
        self.assertEqual(gg.down_message(), gg.DOWN)

    def silent(self, lag: float | None, pid: int = 1, heartbeat: bool = True) -> None:
        """No visible lock; daemon.json's last heartbeat `lag` seconds ago (None: no daemon.json)."""
        if lag is not None:
            record = {"pid": pid, "at": time.time() - lag}
            if heartbeat:
                record["heartbeat"] = 30
            gg.write_json(gg.BASE / "daemon.json", record)

    def test_a_fresh_record_from_another_pid_does_not_hide_a_silent_unit(self) -> None:
        self.active_for = gg.STALE_HEARTBEAT + 120
        self.silent(5, pid=self.main_pid + 1)  # an earlier daemon's, or another serve's
        code, text = self.run_restart()
        self.assertEqual((code, self.calls), (0, [True]), text)

    def test_an_older_daemons_fetch_record_does_not_hide_a_silent_unit(self) -> None:
        self.active_for = gg.STALE_HEARTBEAT + 120
        self.silent(5, heartbeat=False)  # written only when it fetched something: no heartbeat
        code, text = self.run_restart()
        self.assertEqual((code, self.calls), (0, [True]), text)

    def test_a_daemon_before_its_first_heartbeat_is_not_hung(self) -> None:
        """The lock's new holder has not written a heartbeat yet; the stale one is its predecessor's."""
        self.hold(stale=True)  # daemon.json: pid 1, silent past STALE_HEARTBEAT
        (gg.BASE / "daemon.lock").write_text(json.dumps({"pid": 4321, "since": time.time() - 2}) + "\n")
        self.assertIsNone(gg.daemon_stale())
        code, text = self.run_restart()
        self.assertEqual((code, self.calls), (0, []), text)
        self.assertIn("nothing to restart", text)
        (gg.BASE / "daemon.lock").write_text(json.dumps({"pid": 4321, "since": time.time() - gg.STALE_HEARTBEAT - 5}) + "\n")
        self.assertIsNotNone(gg.daemon_stale(), "a new holder that stays silent is hung")

    def test_a_restart_that_died_midway_holds_nobody_back(self) -> None:
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()  # reaped: its pid names no process
        gg.write_json(gg.BASE / "restart.json", {"at": time.time() - 5, "by": gone.pid, "how": "starting"})
        self.assertIsNone(gg.recent_restart(), "its writer is gone: nothing is restarting it")
        gg.write_json(gg.BASE / "restart.json", {"at": time.time() - 5, "by": os.getpid(), "how": "starting"})
        self.assertIsNotNone(gg.recent_restart(), "while its writer runs, that restart is under way")
        code, text = self.run_restart()  # holds restart.lock itself: no other restart can be running
        self.assertEqual((code, self.calls), (0, [False]), text)

    def test_a_hung_holder_whose_status_file_is_gone_is_restarted(self) -> None:
        self.hold(stale=True)
        (gg.BASE / "daemon.lock").write_text(json.dumps({"pid": 4321, "since": time.time() - gg.STALE_HEARTBEAT - 60}) + "\n")
        (gg.BASE / "daemon.json").unlink()
        self.assertIsNotNone(gg.daemon_stale())
        (gg.BASE / "daemon.json").write_text("{not json")
        self.assertIsNotNone(gg.daemon_stale(), "an unreadable one says nothing either")
        with mock.patch.object(gg, "is_serve", return_value=False):
            code, text = self.run_restart()
        self.assertEqual((code, self.calls), (0, [True]), text)

    def test_a_holder_that_writes_heartbeats_is_not_stale(self) -> None:
        self.hold(stale=False)
        (gg.BASE / "daemon.lock").write_text(json.dumps({"pid": 1, "since": time.time() - 3 * gg.STALE_HEARTBEAT}) + "\n")
        self.assertIsNone(gg.daemon_stale(), "its own recent heartbeat counts, not when it took the lock")

    def test_on_macos_a_restart_without_a_heartbeat_starts_no_cooldown(self) -> None:
        self.hold(stale=True)
        gg.RESTART_SETTLE, saved = 0.3, gg.RESTART_SETTLE
        self.addCleanup(setattr, gg, "RESTART_SETTLE", saved)
        with mock.patch.object(gg.sys, "platform", "darwin"), \
                mock.patch.object(gg, "restart_service", return_value=(True, "nothing: launchd has already scheduled the respawn")), \
                mock.patch.object(gg, "lock_holder", return_value=None):
            code, text = self.run_restart()
        self.assertEqual(code, 3, text)
        self.assertIsNone(gg.recent_restart())

    def test_an_active_unit_silent_without_a_visible_lock_is_restarted(self) -> None:
        """huge-blue, 10-23: the lock was on a deleted file and nothing wrote a heartbeat; start was a no-op."""
        self.active_for = gg.STALE_HEARTBEAT + 120
        self.silent(gg.STALE_HEARTBEAT + 60)
        code, text = self.run_restart()
        self.assertEqual((code, self.calls), (0, [True]), text)

    def test_an_active_unit_with_no_heartbeat_file_is_restarted_once_it_is_old(self) -> None:
        self.active_for = gg.STALE_HEARTBEAT + 120
        code, text = self.run_restart()
        self.assertEqual((code, self.calls), (0, [True]), text)

    def test_a_unit_that_became_active_recently_is_only_started(self) -> None:
        self.active_for = 30  # a daemon starting up, before its first heartbeat
        self.silent(gg.STALE_HEARTBEAT + 60)  # the previous daemon's last one
        code, text = self.run_restart()
        self.assertEqual((code, self.calls), (0, [False]), text)

    def test_an_active_unit_with_a_fresh_heartbeat_is_not_restarted(self) -> None:
        """A daemon whose lock file was moved still writes heartbeats and re-locks itself within a tick."""
        self.active_for = gg.STALE_HEARTBEAT + 120
        self.silent(5)
        code, text = self.run_restart()
        self.assertEqual((code, self.calls), (0, [False]), text)

    def test_silent_unit_restarts_once_across_sessions(self) -> None:
        self.active_for = gg.STALE_HEARTBEAT + 120
        self.silent(gg.STALE_HEARTBEAT + 60)
        gg.RESTART_SETTLE, saved = 0.3, gg.RESTART_SETTLE
        self.addCleanup(setattr, gg, "RESTART_SETTLE", saved)
        ran: list[bool] = []

        def slow(hung: bool) -> tuple[bool, str]:
            ran.append(hung)
            return True, "systemctl --user restart glaeda-gh.service"  # ran, but no heartbeat in time

        with mock.patch.object(gg, "restart_service", side_effect=slow), mock.patch.object(gg.sys, "platform", "linux"):
            first, _ = self.run_restart()
            second, text = self.run_restart()
        self.assertEqual((first, second, ran), (3, 0, [True]), text)
        self.assertIn("Do not restart it again", text)

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
    # tests that drive the system-daemon probe through their own launchctl fake; the rest have no system daemon
    REAL_SYSTEM_PROBE = {"test_a_headless_launch_daemon_is_not_doubled_by_a_user_domain_copy",
                         "test_a_probe_that_cannot_tell_fails_closed",
                         "test_a_login_session_restart_never_starts_beside_a_system_daemon",
                         "test_a_logged_out_user_gets_a_background_copy_in_the_user_domain",
                         "test_a_missing_launch_agent_fails_the_user_domain_restart_with_a_reason",
                         "test_a_bad_launch_agent_fails_the_user_domain_restart_with_a_reason",
                         "test_a_login_session_probe_that_cannot_tell_changes_nothing",
                         "test_a_system_probe_that_cannot_tell_is_not_called_loaded"}

    def setUp(self) -> None:
        if self._testMethodName not in self.REAL_SYSTEM_PROBE:
            patcher = mock.patch.object(gg, "system_daemon_loaded", return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_with(self, answers: dict[str, int], state: str | None = None, hung: bool = True) -> tuple[tuple, list]:
        seen: list[list[str]] = []

        def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, answers.get(argv[1], 0), "", "")

        with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                mock.patch.object(gg, "launchd_state", return_value=state), \
                mock.patch.object(gg, "launchd_domain", return_value=f"gui/{os.getuid()}"), \
                mock.patch.object(gg, "drop_background_copy", return_value=(True, "")):
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
                mock.patch.object(gg, "launchd_state", return_value=None), mock.patch.object(gg.time, "sleep"), \
                mock.patch.object(gg, "drop_background_copy", return_value=(True, "")):
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

    def test_unit_active_for_reads_systemd_monotonic_time(self) -> None:
        def answer(out: str, code: int = 0) -> Any:
            return lambda argv, timeout=None: subprocess.CompletedProcess(argv, code, out, "")

        ago = time.monotonic() / 2  # a CI runner may have booted only minutes ago
        entered = int((time.monotonic() - ago) * 1e6)
        with mock.patch.object(gg.sys, "platform", "linux"):
            with mock.patch.object(gg, "run_bounded", answer(f"ActiveState=active\nActiveEnterTimestampMonotonic={entered}\nMainPID=77\n")):
                active, main = gg.unit_active_for()
                self.assertAlmostEqual(active, ago, delta=5)
                self.assertEqual(main, 77)
            with mock.patch.object(gg, "run_bounded", answer(f"ActiveState=inactive\nActiveEnterTimestampMonotonic={entered}\n")):
                self.assertIsNone(gg.unit_active_for())
            with mock.patch.object(gg, "run_bounded", answer("ActiveState=active\nActiveEnterTimestampMonotonic=0\nMainPID=77\n")):
                self.assertIsNone(gg.unit_active_for())
            with mock.patch.object(gg, "run_bounded", answer("", 1)):
                self.assertIsNone(gg.unit_active_for())
        with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded") as run:
            self.assertIsNone(gg.unit_active_for())
            run.assert_not_called()

    def test_error_5_reloads_once(self) -> None:
        (ok, how), seen = self.run_with({"kickstart": 5})
        self.assertTrue(ok)
        self.assertEqual([a[1] for a in seen], ["kickstart", "bootout", "bootstrap"])
        self.assertIn("exit 5", how)

    def test_scheduled_respawn_is_left_to_launchd(self) -> None:
        (ok, how), seen = self.run_with({}, state="spawn scheduled", hung=False)
        self.assertTrue(ok)
        self.assertEqual(seen, [])

    def test_the_domain_is_user_only_when_launchd_says_there_is_no_gui_domain(self) -> None:
        uid = os.getuid()
        # any other answer, or none, is neither: restart then changes nothing
        for answer, domain in ((0, f"gui/{uid}"), (125, f"user/{uid}"), (113, None), (5, None),
                               (OSError("no launchctl"), None)):
            def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
                self.assertEqual(argv, ["launchctl", "print", f"gui/{uid}"])
                if isinstance(answer, Exception):
                    raise answer
                return subprocess.CompletedProcess(argv, answer, "", "")

            with mock.patch.object(gg, "run_bounded", fake):
                self.assertEqual(gg.launchd_domain(), domain, answer)

    def test_a_logged_out_user_gets_a_background_copy_in_the_user_domain(self) -> None:
        """cmux16, 2026-10-24: logged out at the login window, so gui/501 did not exist and kickstart
        and bootstrap there both answered 125. The user domain takes the job once it says Background."""
        uid = os.getuid()
        seen: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "Library/LaunchAgents").mkdir(parents=True)
            job = {"Label": gg.LAUNCH_LABEL, "ProgramArguments": ["/usr/bin/python3", "glaeda-gh", "serve"],
                   "KeepAlive": True}
            with open(home / "Library/LaunchAgents" / f"{gg.LAUNCH_LABEL}.plist", "wb") as handle:
                gg.plistlib.dump(job, handle)

            def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
                seen.append(argv)
                code = ((125 if argv[2] == f"gui/{uid}" else 113) if argv[1] == "print"
                        else {"bootout": 3}.get(argv[1], 0))
                return subprocess.CompletedProcess(argv, code, "", "")

            copy = home / "gh" / f"{gg.LAUNCH_LABEL}.background.plist"
            agent = home / "Library/LaunchAgents" / f"{gg.LAUNCH_LABEL}.plist"
            with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                    mock.patch.object(gg, "launchd_state", return_value=None), \
                    mock.patch.object(gg.Path, "home", return_value=home), \
                    mock.patch.object(gg, "BASE", home / "gh"):
                ok, how = gg.restart_service(True)
                self.assertTrue(ok, how)
                self.assertEqual(seen[1:], [
                    ["launchctl", "print", f"system/{gg.LAUNCH_LABEL}"],
                    ["launchctl", "print", f"gui/{uid}"],  # still logged out, right before the reload
                    ["launchctl", "bootout", f"user/{uid}/{gg.LAUNCH_LABEL}"],
                    ["launchctl", "bootstrap", f"user/{uid}", os.fspath(copy)]])
                with open(copy, "rb") as handle:
                    self.assertEqual(gg.plistlib.load(handle), {**job, "LimitLoadToSessionType": "Background"})
                # launchctl refuses a job file group or others can write, whatever the umask or an older copy's mode
                self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
                copy.chmod(0o666)
                old_umask = os.umask(0o002)
                try:
                    gg.background_plist()
                finally:
                    os.umask(old_umask)
                self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
                seen.clear()
                # the LaunchAgent itself is untouched: it is still the one source
                with open(agent, "rb") as handle:
                    self.assertEqual(gg.plistlib.load(handle), job)
                # a later restart, with the copy loaded and the LaunchAgent changed, reloads the new job:
                # a kickstart would have kept running the old copy
                updated = {**job, "EnvironmentVariables": {"PATH": "/new/bin"}}
                with open(agent, "wb") as handle:
                    gg.plistlib.dump(updated, handle)
                seen.clear()
                ok, how = gg.restart_service(True)
                self.assertTrue(ok, how)
                self.assertEqual([a[1] for a in seen], ["print", "print", "print", "bootout", "bootstrap"])
                # a scheduled respawn does not stop the reload either: it would respawn the old copy
                seen.clear()
                with mock.patch.object(gg, "launchd_state", return_value="spawn scheduled"):
                    ok, how = gg.restart_service(False)
                self.assertTrue(ok, how)
                self.assertEqual([a[1] for a in seen], ["print", "print", "print", "bootout", "bootstrap"])
                with open(copy, "rb") as handle:
                    self.assertEqual(gg.plistlib.load(handle), {**updated, "LimitLoadToSessionType": "Background"})

    def test_a_bad_launch_agent_fails_the_user_domain_restart_with_a_reason(self) -> None:
        """Malformed XML raises expat's error and a plist array a TypeError on assignment: neither may escape
        to cmd_restart, which catches only OSError and TimeoutExpired."""
        for body, why in ((b"<?xml version='1.0'?><plist><dict><key>Label", "ExpatError"),
                          (gg.plistlib.dumps(["not", "a", "job"]), "ValueError")):
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "Library/LaunchAgents").mkdir(parents=True)
                (Path(tmp) / "Library/LaunchAgents" / f"{gg.LAUNCH_LABEL}.plist").write_bytes(body)
                with mock.patch.object(gg.sys, "platform", "darwin"), \
                        mock.patch.object(gg, "run_bounded", side_effect=lambda argv, timeout=None: subprocess.CompletedProcess(
                            argv, 113 if argv[1:] == ["print", f"system/{gg.LAUNCH_LABEL}"] else self.fail(argv), "", "")), \
                        mock.patch.object(gg, "launchd_state", return_value=None), \
                        mock.patch.object(gg, "launchd_domain", return_value=f"user/{os.getuid()}"), \
                        mock.patch.object(gg.Path, "home", return_value=Path(tmp)), \
                        mock.patch.object(gg, "BASE", Path(tmp) / "gh"):
                    ok, how = gg.restart_service(True)
            self.assertFalse(ok)
            self.assertIn(f"no Background copy for user/{os.getuid()}: {why}", how)

    def test_a_probe_that_cannot_tell_fails_closed(self) -> None:
        """Only 113 means not loaded: a copy probe that fails otherwise keeps the LaunchAgent from starting beside
        a copy that may still be there. (The user path probes nothing of its own: it always reloads the copy. The
        system and domain probes have their own tests below.)"""
        uid = os.getuid()
        for domain in (f"gui/{uid}",):
            seen: list[list[str]] = []

            def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
                seen.append(argv)
                # the system probe says "not loaded", so the domain's own path is what has to fail closed
                if argv[1:] == ["print", f"system/{gg.LAUNCH_LABEL}"]:
                    return subprocess.CompletedProcess(argv, gg.NOT_LOADED, "", "")
                return subprocess.CompletedProcess(argv, 5 if argv[1] == "print" else 0, "", "")

            clock = iter(range(0, 1000, 5))
            with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                    mock.patch.object(gg, "launchd_domain", return_value=domain), \
                    mock.patch.object(gg, "launchd_state", return_value=None), \
                    mock.patch.object(gg, "background_plist", side_effect=AssertionError("no copy")), \
                    mock.patch.object(gg.time, "sleep"), mock.patch.object(gg.time, "monotonic", lambda: next(clock)):
                ok, how = gg.restart_service(True)
            self.assertFalse(ok, domain)
            self.assertNotIn("bootstrap", [a[1] for a in seen], domain)
            self.assertNotIn("kickstart", [a[1] for a in seen], domain)
            self.assertIn(["launchctl", "print", f"system/{gg.LAUNCH_LABEL}"], seen, domain)
            self.assertNotIn("system LaunchDaemon", how, domain)  # it got past the system guard
            self.assertIn("still loaded", how, domain)  # and stopped at the copy probe, not elsewhere
            self.assertIn(["launchctl", "print", f"user/{uid}/{gg.LAUNCH_LABEL}"], seen, domain)

    def test_a_shell_serve_is_refused_while_a_restart_holds_its_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gg, "BASE", Path(tmp)), \
                mock.patch.object(gg.sys, "platform", "darwin"), \
                mock.patch.object(gg, "run_bounded", side_effect=lambda argv, timeout=None: subprocess.CompletedProcess(
                    argv, gg.NOT_LOADED, "", "")):
            self.assertFalse(gg.service_loaded())  # nothing loaded, no restart
            with open(Path(tmp) / "restart.lock", "a+") as guard:
                # flock belongs to the open file, so the probe's own open sees this one held, as another process would
                self.assertTrue(gg.try_lock(guard))
                self.assertTrue(gg.service_loaded())
            self.assertFalse(gg.service_loaded())

    def test_no_restart_begins_while_a_shell_serve_runs_its_probes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gg, "BASE", Path(tmp)), \
                mock.patch.object(gg.sys, "platform", "darwin"):
            won: list[bool] = []

            def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
                with open(Path(tmp) / "restart.lock", "a+") as restart:  # a restart's own open, mid-probe
                    won.append(gg.try_lock(restart))
                return subprocess.CompletedProcess(argv, gg.NOT_LOADED, "", "")
            with mock.patch.object(gg, "run_bounded", fake):
                self.assertFalse(gg.service_loaded())
            self.assertEqual(won, [False, False, False])  # one try per probe, each refused
            with open(Path(tmp) / "restart.lock", "a+") as restart:
                self.assertTrue(gg.try_lock(restart))  # and released afterwards

    def test_a_login_or_logout_during_a_restart_changes_nothing(self) -> None:
        uid = os.getuid()
        for first, then in ((f"user/{uid}", f"gui/{uid}"), (f"gui/{uid}", f"user/{uid}")):
            seen: list[list[str]] = []

            def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
                seen.append(argv)
                return subprocess.CompletedProcess(argv, gg.NOT_LOADED if argv[1] == "print" else 0, "", "")
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "Library/LaunchAgents").mkdir(parents=True)
                (Path(tmp) / "Library/LaunchAgents" / f"{gg.LAUNCH_LABEL}.plist").write_bytes(
                    gg.plistlib.dumps({"Label": gg.LAUNCH_LABEL}))
                with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                        mock.patch.object(gg, "launchd_domain", side_effect=[first, then]), \
                        mock.patch.object(gg, "launchd_state", return_value=None), \
                        mock.patch.object(gg.Path, "home", return_value=Path(tmp)), \
                        mock.patch.object(gg, "BASE", Path(tmp) / "gh"):
                    ok, how = gg.restart_service(True)
            self.assertFalse(ok, first)
            self.assertIn("logged in or out while restarting", how, first)
            self.assertFalse({"bootout", "bootstrap", "kickstart"} & {a[1] for a in seen}, first)

    def test_a_login_session_probe_that_cannot_tell_changes_nothing(self) -> None:
        """A gui/UID print that answers neither 0 nor 125 (or none at all) is not taken for a login session:
        on a headless host that would boot the Background copy out and then fail to bootstrap gui/UID."""
        for answer in (5, None):
            seen: list[list[str]] = []

            def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
                seen.append(argv)
                if argv[1:] == ["print", f"gui/{os.getuid()}"] and answer is None:
                    raise subprocess.TimeoutExpired(argv, 10)
                return subprocess.CompletedProcess(argv, answer if argv[1:] == ["print", f"gui/{os.getuid()}"]
                                                   else 0, "", "")

            with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake):
                self.assertIsNone(gg.launchd_domain(), answer)
                ok, how = gg.restart_service(True)
                hint = gg.start_hint(restart=True)
            self.assertFalse(ok, answer)
            self.assertIn("could not say whether anyone is logged in", how)
            self.assertIn("could not say whether anyone is logged in", hint)
            self.assertEqual({a[1] for a in seen}, {"print"}, answer)

    def test_a_system_probe_that_cannot_tell_is_not_called_loaded(self) -> None:
        for domain in (f"user/{os.getuid()}", f"gui/{os.getuid()}"):
            seen: list[list[str]] = []

            def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
                seen.append(argv)
                if argv[1:] == ["print", f"system/{gg.LAUNCH_LABEL}"]:
                    return subprocess.CompletedProcess(argv, 5, "", "")
                return subprocess.CompletedProcess(argv, gg.NOT_LOADED, "", "")

            with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                    mock.patch.object(gg, "launchd_domain", return_value=domain):
                self.assertIsNone(gg.system_daemon_loaded())
                ok, how = gg.restart_service(True)
                hint = gg.start_hint(restart=True)
            self.assertFalse(ok, domain)
            self.assertIn("could not say whether the system LaunchDaemon", how, domain)
            self.assertNotIn("runs the daemon on this headless Mac", how, domain)
            self.assertNotIn("kickstart", [a[1] for a in seen], domain)
            self.assertNotIn("bootstrap", [a[1] for a in seen], domain)
            self.assertNotIn("runs the daemon on this headless Mac", hint, domain)
            self.assertIn("check", hint, domain)

    def test_a_headless_launch_daemon_is_not_doubled_by_a_user_domain_copy(self) -> None:
        seen: list[list[str]] = []

        def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 125 if argv[2] == f"gui/{os.getuid()}" else 0, "", "")

        with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                mock.patch.object(gg, "background_plist", side_effect=AssertionError("no copy")):
            ok, how = gg.restart_service(True)
        self.assertFalse(ok)
        self.assertIn(f"sudo launchctl kickstart -k system/{gg.LAUNCH_LABEL}", how)
        self.assertEqual([a[1] for a in seen], ["print", "print"])

    def test_a_copy_that_will_not_boot_out_keeps_the_launch_agent_from_starting_beside_it(self) -> None:
        seen: list[list[str]] = []

        def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 5 if argv[1] == "bootout" else 0, "", "")

        clock = iter(range(0, 1000, 5))
        with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                mock.patch.object(gg, "launchd_state", return_value="spawn scheduled"), \
                mock.patch.object(gg.time, "sleep"), mock.patch.object(gg.time, "monotonic", lambda: next(clock)):
            ok, how = gg.restart_service(False)
        self.assertFalse(ok)
        self.assertIn("exited 5 and it is still loaded", how)
        self.assertNotIn("kickstart", [a[1] for a in seen])

    def test_a_bootout_that_returns_before_teardown_is_waited_for(self) -> None:
        """bootout answers 0 while the copy is still loaded; the LaunchAgent starts only once print no longer finds it."""
        uid = os.getuid()
        copy = f"user/{uid}/{gg.LAUNCH_LABEL}"
        prints = {"n": 0}
        seen: list[list[str]] = []

        def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
            seen.append(argv)
            if argv[1:] == ["print", copy]:
                prints["n"] += 1
                return subprocess.CompletedProcess(argv, 0 if prints["n"] <= 3 else 113, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                mock.patch.object(gg, "launchd_state", return_value=None), mock.patch.object(gg.time, "sleep"):
            ok, how = gg.restart_service(True)
        self.assertTrue(ok, how)
        self.assertEqual(prints["n"], 4)
        self.assertEqual(seen[-1], ["launchctl", "kickstart", "-k", f"gui/{uid}/{gg.LAUNCH_LABEL}"])
        # and one that never goes keeps the LaunchAgent off
        prints["n"] = -1000
        seen.clear()
        clock = iter(range(0, 1000, 5))
        with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                mock.patch.object(gg, "launchd_state", return_value=None), mock.patch.object(gg.time, "sleep"), \
                mock.patch.object(gg.time, "monotonic", lambda: next(clock)):
            ok, how = gg.restart_service(True)
        self.assertFalse(ok)
        self.assertIn("exited 0 and it is still loaded", how)
        self.assertNotIn("kickstart", [a[1] for a in seen])

    def test_a_scheduled_respawn_in_the_login_session_still_drops_the_copy(self) -> None:
        uid = os.getuid()
        seen: list[list[str]] = []

        def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
            seen.append(argv)
            gone = argv[1] == "print" and argv[2].startswith("user/") and ["launchctl", "bootout", argv[2]] in seen
            return subprocess.CompletedProcess(argv, 113 if gone else 0, "", "")

        with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                mock.patch.object(gg, "launchd_state", return_value="spawn scheduled"):
            ok, how = gg.restart_service(False)
        self.assertTrue(ok, how)
        self.assertIn(["launchctl", "bootout", f"user/{uid}/{gg.LAUNCH_LABEL}"], seen)
        self.assertTrue(how.endswith("nothing: launchd has already scheduled the respawn"), how)

    def test_a_login_session_restart_boots_out_the_background_copy_first(self) -> None:
        uid = os.getuid()
        seen: list[list[str]] = []

        def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
            seen.append(argv)
            gone = argv[1] == "print" and argv[2].startswith("user/") and ["launchctl", "bootout", argv[2]] in seen
            return subprocess.CompletedProcess(argv, 113 if gone else 0, "", "")

        with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                mock.patch.object(gg, "launchd_state", return_value=None):
            ok, how = gg.restart_service(True)
        self.assertTrue(ok, how)
        self.assertEqual(seen, [["launchctl", "print", f"gui/{uid}"],
                                ["launchctl", "print", f"gui/{uid}"],  # still logged in, right before the drop
                                ["launchctl", "print", f"user/{uid}/{gg.LAUNCH_LABEL}"],
                                ["launchctl", "bootout", f"user/{uid}/{gg.LAUNCH_LABEL}"],
                                ["launchctl", "print", f"user/{uid}/{gg.LAUNCH_LABEL}"],
                                ["launchctl", "kickstart", "-k", f"gui/{uid}/{gg.LAUNCH_LABEL}"]])
        self.assertTrue(how.startswith(f"launchctl bootout user/{uid}/{gg.LAUNCH_LABEL}"), how)
        # with no copy loaded, nothing is booted out
        seen.clear()

        def no_copy(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 113 if argv[2].startswith("user/") else 0, "", "")

        with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", no_copy), \
                mock.patch.object(gg, "launchd_state", return_value=None):
            ok, how = gg.restart_service(True)
        self.assertTrue(ok, how)
        self.assertEqual([a[1] for a in seen], ["print", "print", "print", "kickstart"])
        self.assertEqual(how, f"launchctl kickstart -k gui/{uid}/{gg.LAUNCH_LABEL}")

    def test_a_missing_launch_agent_fails_the_user_domain_restart_with_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
                return subprocess.CompletedProcess(argv, 113 if argv[1] in ("kickstart", "print") else 0, "", "")

            with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                    mock.patch.object(gg, "launchd_state", return_value=None), \
                    mock.patch.object(gg, "launchd_domain", return_value=f"user/{os.getuid()}"), \
                    mock.patch.object(gg.Path, "home", return_value=Path(tmp)), \
                    mock.patch.object(gg, "BASE", Path(tmp) / "gh"):
                ok, how = gg.restart_service(True)
        self.assertFalse(ok)
        self.assertIn(f"no Background copy for user/{os.getuid()}: FileNotFoundError", how)


class RestartHintTest(unittest.TestCase):
    def test_darwin_hints_explain_launchctl_error_5(self) -> None:
        with mock.patch.object(gg.sys, "platform", "darwin"), \
                mock.patch.object(gg, "launchd_domain", return_value=f"gui/{os.getuid()}"), \
                mock.patch.object(gg, "system_daemon_loaded", return_value=False), \
                mock.patch.object(gg, "unloaded", return_value=True):
            for hint in (gg.start_hint(), gg.start_hint(restart=True)):
                self.assertIn("5: Input/output error", hint)
                self.assertIn("already loaded", hint)
                self.assertIn(f"launchctl bootout gui/$(id -u)/{gg.LAUNCH_LABEL}", hint)
                self.assertIn("Never run glaeda-gh serve yourself", hint)

    def test_a_login_session_restart_never_starts_beside_a_system_daemon(self) -> None:
        seen: list[list[str]] = []

        def fake(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")  # the gui domain exists; system/LABEL is loaded

        with mock.patch.object(gg.sys, "platform", "darwin"), mock.patch.object(gg, "run_bounded", fake), \
                mock.patch.object(gg, "launchd_state", return_value=None):
            ok, how = gg.restart_service(True)
        self.assertFalse(ok)
        self.assertIn(f"sudo launchctl kickstart -k system/{gg.LAUNCH_LABEL}", how)
        self.assertNotIn("kickstart", [a[1] for a in seen])

    def test_a_login_session_hint_checks_for_a_copy_or_system_daemon_first(self) -> None:
        for system, copy in ((True, False), (False, True)):
            with mock.patch.object(gg.sys, "platform", "darwin"), \
                    mock.patch.object(gg, "launchd_domain", return_value=f"gui/{os.getuid()}"), \
                    mock.patch.object(gg, "system_daemon_loaded", return_value=system), \
                    mock.patch.object(gg, "unloaded", return_value=not copy):
                hint = gg.start_hint(restart=True)
            self.assertIn("check first", hint, (system, copy))
            self.assertNotIn(f"launchctl kickstart -k gui/", hint)
        with mock.patch.object(gg.sys, "platform", "darwin"), \
                mock.patch.object(gg, "launchd_domain", return_value=f"gui/{os.getuid()}"), \
                mock.patch.object(gg, "system_daemon_loaded", return_value=False), \
                mock.patch.object(gg, "unloaded", return_value=True):
            self.assertIn(f"launchctl kickstart -k gui/$(id -u)/{gg.LAUNCH_LABEL}", gg.start_hint(restart=True))

    def test_a_headless_hint_names_the_system_daemon_not_a_copy(self) -> None:
        with mock.patch.object(gg.sys, "platform", "darwin"), \
                mock.patch.object(gg, "launchd_domain", return_value=f"user/{os.getuid()}"), \
                mock.patch.object(gg, "system_daemon_loaded", return_value=True):
            for hint in (gg.start_hint(), gg.start_hint(restart=True)):
                self.assertIn(f"sudo launchctl kickstart -k system/{gg.LAUNCH_LABEL}", hint)
                self.assertNotIn("user/$(id -u)", hint)

    def test_a_system_probe_that_fails_gives_a_hint_that_checks_first(self) -> None:
        with mock.patch.object(gg.sys, "platform", "darwin"), \
                mock.patch.object(gg, "launchd_domain", return_value=f"user/{os.getuid()}"), \
                mock.patch.object(gg, "system_daemon_loaded", side_effect=subprocess.TimeoutExpired(["launchctl"], 10)):
            hint = gg.start_hint(restart=True)
        self.assertIn(f"sudo launchctl print system/{gg.LAUNCH_LABEL}", hint)
        self.assertNotIn("launchctl bootstrap user/", hint)

    def test_a_logged_out_hint_names_the_user_domain_copy(self) -> None:
        with mock.patch.object(gg.sys, "platform", "darwin"), \
                mock.patch.object(gg, "system_daemon_loaded", return_value=False), \
                mock.patch.object(gg, "launchd_domain", return_value=f"user/{os.getuid()}"):
            for hint in (gg.start_hint(), gg.start_hint(restart=True)):
                self.assertIn("nobody is logged in", hint)
                self.assertIn(f"launchctl bootstrap user/$(id -u) {gg.background_copy_path()}", hint)
                self.assertNotIn("gui/$(id -u)/", hint)
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
        self.daemon.note_budget("rest", 5000, gg.FLOORS["rest"][0] - 50, NOW + 1800)  # both low: nowhere to go
        gg.register(self.key(SHA_A), consumer="session-1")
        gg.register(self.key(SHA_B), consumer="autoland")
        self.daemon.tick(NOW)
        self.assertIsNone(gg.read_cache(self.key(SHA_A)))
        self.assertIsNotNone(gg.read_cache(self.key(SHA_B)))

    def test_graphql_low_and_rest_not_reads_heads_over_rest(self) -> None:
        self.daemon.note_budget("graphql", 5000, gg.FLOORS["graphql"][0] - 50, NOW + 1800)
        gg.register(self.key(SHA_A), consumer="session-1")
        self.daemon.tick(NOW)
        entry = gg.read_cache(self.key(SHA_A))
        self.assertEqual(entry["via"], "rest")
        self.assertEqual(self.gh.gql_calls(), [])
        self.assertEqual(entry["data"]["found"], {"check / CI / build": ["completed", "success"],
                                                  "check / CI / test": ["completed", "failure"]})

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

    # cmux#35906 merged as 633d5e38 pinned at e1d1894a with its "publish tree-addressed cmux-tui" red in run
    # 41931705470 unexcused: two reruns ended in startup_failure with no jobs, and statusCheckRollup (each suite's newest
    # attempt only) dropped the red. A check reads as its last real attempt left it; a startup_failure is no result.
    def red_publish(self, run_id: int = 1, conclusion: str = "FAILURE") -> dict:
        return {**check_run("publish", "COMPLETED", conclusion), "id": f"CR{run_id}", "databaseId": run_id}

    def test_a_red_under_startup_failure_reruns_still_reads_red(self) -> None:
        self.gh.heads[("o", "r", SHA_A)] = [check_run("CI / build")]
        self.gh.suites[SHA_A] = [{"id": "S1", "latest": 0, "runs": [self.red_publish()]}]
        gg.register(self.key(SHA_A), consumer="autoland")
        self.daemon.tick(NOW)
        entry = gg.read_cache(self.key(SHA_A))
        self.assertIsNone(entry.get("error"))
        self.assertEqual(entry["data"]["found"]["CI / publish"], ["completed", "failure"])

    def test_a_red_then_a_real_green_rerun_reads_green(self) -> None:
        # the green attempt is the suite's newest real one: GitHub's rollup shows it, and nothing more is read
        self.gh.heads[("o", "r", SHA_A)] = [check_run("CI / build"), self.red_publish(2, "SUCCESS")]
        self.gh.suites[SHA_A] = [{"id": "S1", "latest": 2, "runs": [self.red_publish(1), self.red_publish(2, "SUCCESS")]}]
        gg.register(self.key(SHA_A), consumer="autoland")
        self.daemon.tick(NOW)
        found = gg.read_cache(self.key(SHA_A))["data"]["found"]
        self.assertEqual(found["check / publish"], ["completed", "success"])
        self.assertNotIn("CI / publish", found)
        # red, then green, then a startup_failure: the green is still the last real attempt
        self.gh.heads[("o", "r", SHA_B)] = [check_run("CI / build")]
        self.gh.suites[SHA_B] = [{"id": "S2", "latest": 0, "runs": [self.red_publish(1), self.red_publish(2, "SUCCESS")]}]
        gg.register(self.key(SHA_B), consumer="autoland")
        self.daemon.tick(NOW)
        self.assertEqual(gg.read_cache(self.key(SHA_B))["data"]["found"]["CI / publish"], ["completed", "success"])

    def test_a_startup_failure_alone_is_no_result_not_green(self) -> None:
        self.gh.heads[("o", "r", SHA_A)] = []
        self.gh.suites[SHA_A] = [{"id": "S1", "latest": 0, "runs": []}]
        gg.register(self.key(SHA_A), consumer="autoland")
        self.daemon.tick(NOW)
        data = gg.read_cache(self.key(SHA_A))["data"]
        self.assertEqual(data["found"], {})
        self.assertFalse(data["settled"])
        # the batch's one follow-up read finds nothing ran in any attempt, and adds nothing
        self.assertEqual(len(self.gh.gql_calls()), 2)

    def test_a_pr_with_a_red_under_startup_failure_reruns_is_not_green(self) -> None:
        node = pr_node(1, rollup="SUCCESS", checks=[check_run("build")])
        self.gh.prs[("o", "r", 1)] = node
        self.gh.suites[node["headRefOid"]] = [{"id": "S1", "latest": 0, "runs": [self.red_publish()]}]
        key = gg.parse_key("pr", "o/r#1")
        gg.register(key)
        self.daemon.tick(NOW)
        data = gg.read_cache(key)["data"]
        self.assertIn(("publish", "FAILURE"), [(c["name"], c["conclusion"]) for c in data["checks"]])
        self.assertEqual(gg.check_verdict(data, "green"), gg.EXIT_FAIL)
        self.assertEqual(gg.check_verdict({**data, "checksTruncated": True}, "green"), gg.EXIT_FAIL)

    def test_a_red_under_startup_failure_reruns_in_the_check_of_record_repository_still_reads_red(self) -> None:
        # Review finding F1 on glaeda#1842: the fork's suites are read too, under its prefix.
        self.gh.heads[("teamleaderleo", "glaeda", SHA_A)] = [check_run("Release / build")]
        self.gh.heads[("manaflow-ai", "glaeda", SHA_A)] = [check_run("Verify / build")]
        self.gh.suites[SHA_A] = [{"id": "S9", "latest": 0, "runs": [self.red_publish()], "repo": "manaflow-ai/glaeda"}]
        key = gg.parse_key("head", f"teamleaderleo/glaeda@{SHA_A}")
        gg.register(key, consumer="autoland")
        self.daemon.tick(NOW)
        found = gg.read_cache(key)["data"]["found"]
        self.assertEqual(found["manaflow-ai/glaeda: CI / publish"], ["completed", "failure"])
        self.assertNotIn("CI / publish", found)

    def test_more_suites_than_one_page_is_an_error_not_a_greener_head(self) -> None:
        # Review finding F3 on glaeda#1842: a ran-nothing suite past the first 100 could hide its red.
        self.gh.heads[("o", "r", SHA_A)] = [check_run("CI / build")]
        self.gh.suites[SHA_A] = [{"id": "S1", "latest": 1, "runs": [self.red_publish(1, "SUCCESS")], "more": True}]
        gg.register(self.key(SHA_A), consumer="autoland")
        self.daemon.tick(NOW)
        self.assertIn("more than 100 check suites", gg.read_cache(self.key(SHA_A))["error"])

    def test_a_required_red_under_startup_failure_reruns_fails_until_required(self) -> None:
        # Review finding F2 on glaeda#1842: a run read by suite id has no isRequired; the base's rules decide.
        node = pr_node(1, rollup="SUCCESS", checks=[{**check_run("build"), "isRequired": True}])
        node["baseRef"] = {"branchProtectionRule": {"requiredStatusCheckContexts": ["publish"]}, "rules": {"nodes": []}}
        self.gh.prs[("o", "r", 1)] = node
        self.gh.suites[node["headRefOid"]] = [{"id": "S1", "latest": 0, "runs": [self.red_publish()]}]
        key = gg.parse_key("pr", "o/r#1")
        gg.register(key)
        self.daemon.tick(NOW)
        data = gg.read_cache(key)["data"]
        self.assertEqual(gg.check_verdict(data, "required"), gg.EXIT_FAIL)
        # one the base does not require does not decide --until required while others are marked required
        node["baseRef"] = {"branchProtectionRule": {"requiredStatusCheckContexts": ["build"]}, "rules": {"nodes": []}}
        self.daemon.tick(NOW + gg.CYCLE)
        data = gg.read_cache(key)["data"]
        self.assertNotIn(gg.check_verdict(data, "required"), (gg.EXIT_FAIL,))
        self.assertEqual(gg.check_verdict(data, "green"), gg.EXIT_FAIL)

    def test_a_malformed_suite_count_is_an_error_not_a_complete_read(self) -> None:
        # Review finding F1 on glaeda#1844: a null or missing count (a nested GraphQL error) read as zero, so a partial
        # suite read looked complete and could hide an older red attempt.
        def broken(suite: dict) -> list[dict | None]:
            good = {"id": "S1", "latest": {"totalCount": 0}}
            return [None, {**good, "latest": None}, {k: v for k, v in good.items() if k != "latest"},
                    {**good, "latest": {"totalCount": None}}, {**good, "latest": {}}, {**good, "latest": {"totalCount": "0"}},
                    {**good, "latest": {"totalCount": -1}}, {**good, "latest": {"totalCount": True}}]
        for bad in broken({}):
            with self.subTest(suite=bad):
                found = gg.ran_nothing_suites({"checkSuites": {"pageInfo": {"hasNextPage": False}, "nodes": [bad]}})
                self.assertIsInstance(found, str)
                self.assertIn("malformed", found)
        for nodes in (None, "x", {}):
            with self.subTest(nodes=nodes):
                found = gg.ran_nothing_suites({"checkSuites": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}})
                self.assertIsInstance(found, str)
        # well formed: the same answers as before
        ok = {"id": "S1", "latest": {"totalCount": 0}}
        self.assertEqual(gg.ran_nothing_suites({"checkSuites": {"pageInfo": {"hasNextPage": False}, "nodes": [ok]}}), ["S1"])
        ran = {"id": "S2", "latest": {"totalCount": 3}}
        self.assertEqual(gg.ran_nothing_suites({"checkSuites": {"pageInfo": {"hasNextPage": False}, "nodes": [ok, ran]}}),
                         ["S1"])
        self.assertEqual(gg.ran_nothing_suites({"checkSuites": {"pageInfo": {"hasNextPage": False}, "nodes": []}}), [])

    def test_a_malformed_suite_count_in_a_tick_is_an_error_not_a_green_head(self) -> None:
        self.gh.heads[("o", "r", SHA_A)] = [check_run("CI / build")]
        self.gh.suites[SHA_A] = [{"id": "S1", "latest": None, "runs": [self.red_publish()]}]
        gg.register(self.key(SHA_A), consumer="autoland")
        self.daemon.tick(NOW)
        self.assertIn("malformed", gg.read_cache(self.key(SHA_A))["error"] or "")

    def test_a_batch_of_prs_reads_its_ran_nothing_suites_in_one_query(self) -> None:
        """The batched PR read no longer counts every attempt's runs per suite (100 requests per PR each); the suites
        whose newest attempt ran nothing are read by id, once for the whole batch, and a red under them still counts."""
        for n in (1, 2, 3):
            node = pr_node(n, rollup="SUCCESS", checks=[check_run("build")])
            node["headRefOid"] = f"{n}" * 40
            self.gh.prs[("o", "r", n)] = node
            # an app's suite that never ran anything, beside (on PR 2) a red under a startup_failure rerun
            self.gh.suites[node["headRefOid"]] = [{"id": f"APP{n}", "latest": 0, "runs": []},
                                                  {"id": f"CI{n}", "latest": 1 if n != 2 else 0,
                                                   "runs": [self.red_publish(n)] if n == 2 else [check_run("build")]}]
            gg.register(gg.parse_key("pr", f"o/r#{n}"))
        self.daemon.tick(NOW)
        suite_reads = [json.loads(c[3])["variables"]["suites"] for c in self.gh.gql_calls()
                       if "suites" in json.loads(c[3])["variables"]]
        self.assertEqual(len(suite_reads), 1)  # one query for the batch, in the scheduler's order
        self.assertEqual(sorted(suite_reads[0]), ["APP1", "APP2", "APP3", "CI2"])
        for n in (1, 2, 3):
            data = gg.read_cache(gg.parse_key("pr", f"o/r#{n}"))["data"]
            self.assertNotIn("checksPageError", data)
            red = ("publish", "FAILURE") in [(c["name"], c["conclusion"]) for c in data["checks"]]
            self.assertEqual(red, n == 2)
            self.assertEqual(gg.check_verdict(data, "green"), gg.EXIT_FAIL if n == 2 else 0)

    def test_one_unreadable_suite_in_a_batch_makes_only_its_own_pr_partial(self) -> None:
        """Router-review BLOCK on glaeda#1916 at a01313fc: the batch reads every PR's ran-nothing suites in one query,
        and a suite with more than 100 runs (PR 2's startup_failure suite) made every PR in the batch partial, so PR 1,
        green with only an empty app suite, read checksPageError. Each suite's error is now its own PR's."""
        for n in (1, 2):
            node = pr_node(n, rollup="SUCCESS", checks=[check_run("build")])
            node["headRefOid"] = f"{n}" * 40
            self.gh.prs[("o", "r", n)] = node
            gg.register(gg.parse_key("pr", f"o/r#{n}"))
        self.gh.suites["1" * 40] = [{"id": "APP1", "latest": 0, "runs": []}]
        self.gh.suites["2" * 40] = [{"id": "SF2", "latest": 0, "runs": [self.red_publish(2)], "runs_more": True}]
        self.daemon.tick(NOW)
        one = gg.read_cache(gg.parse_key("pr", "o/r#1"))["data"]
        self.assertNotIn("checksPageError", one)
        self.assertEqual(gg.check_verdict(one, "green"), 0)
        two = gg.read_cache(gg.parse_key("pr", "o/r#2"))["data"]
        self.assertIn("more than 100 check runs", two.get("checksPageError") or "")
        self.assertNotEqual(gg.check_verdict(two, "green"), 0)

    def test_the_batch_follow_up_is_not_held_by_the_minute_its_own_read_spent(self) -> None:
        """The follow-up belongs to the read the per-minute ceiling admitted: holding it would leave the whole batch
        partial whenever the main query used the minute's last room."""
        node = pr_node(1, rollup="SUCCESS", checks=[check_run("build")])
        self.gh.prs[("o", "r", 1)] = node
        self.gh.suites[node["headRefOid"]] = [{"id": "S1", "latest": 0, "runs": [self.red_publish()]}]
        gg.register(gg.parse_key("pr", "o/r#1"))
        # tick() reads the policy afresh: the ceiling is set where it reads it (review on glaeda#1916 at af5363f6, F3)
        with mock.patch.object(gg, "daemon_policy", return_value={**gg.daemon_policy(), "graphqlPerMinute": 1}):
            self.daemon.tick(NOW)
        data = gg.read_cache(gg.parse_key("pr", "o/r#1"))["data"]
        self.assertNotIn("checksPageError", data)
        self.assertEqual(gg.check_verdict(data, "green"), gg.EXIT_FAIL)

    def test_a_batch_follow_up_of_many_chunks_reads_every_pr_under_a_tight_ceiling(self) -> None:
        """Review on glaeda#1916 at 73069ea7, F1: two green PRs of 60 empty suites each fill two suite queries. Holding
        the second chunk under graphqlPerMinute 2 left PR 2 pending every cycle (the next one held it the same way).
        The batch's follow-up is never held, so both PRs read green, cycle after cycle."""
        for n in (1, 2):
            node = pr_node(n, rollup="SUCCESS", checks=[check_run("build")])
            node["headRefOid"] = f"{n}" * 40
            self.gh.prs[("o", "r", n)] = node
            self.gh.suites[node["headRefOid"]] = [{"id": f"E{n}-{k}", "latest": 0, "runs": []} for k in range(60)]
            gg.register(gg.parse_key("pr", f"o/r#{n}"))
        with mock.patch.object(gg, "daemon_policy", return_value={**gg.daemon_policy(), "graphqlPerMinute": 2}):
            for minute in range(3):
                self.daemon.tick(NOW + 61 * minute)
                for n in (1, 2):
                    data = gg.read_cache(gg.parse_key("pr", f"o/r#{n}"))["data"]
                    self.assertNotIn("checksPageError", data)
                    self.assertEqual(gg.check_verdict(data, "green"), 0)

    def test_a_held_paced_chunk_keeps_the_suites_already_read(self) -> None:
        """A paced read (last_real_contexts) that the ceiling stops after its first chunk keeps what that chunk read;
        only the unread suites carry the hold (review on glaeda#1916 at af5363f6, F2)."""
        ids = [f"S{k}" for k in range(120)]
        self.gh.suites[SHA_A] = [{"id": i, "latest": 0, "runs": []} for i in ids]
        self.daemon.now = NOW
        room = iter([True, False])
        with mock.patch.object(self.daemon, "gql_room", side_effect=lambda: next(room)):
            runs, error = self.daemon.suite_runs(ids)
        self.assertIsNone(error)
        self.assertEqual([runs[i] for i in ids[:100]], [[]] * 100)
        self.assertTrue(all("graphqlPerMinute" in runs[i] for i in ids[100:]))

    def test_a_nested_error_on_a_suite_is_partial_not_an_empty_green_suite(self) -> None:
        """Review on glaeda#1916 at af5363f6, F1: GitHub returned the suite with a null checkRuns beside an error, and
        the reader took it for a suite that ran nothing, so an older failed attempt's red was gone: green."""
        node = pr_node(1, rollup="SUCCESS", checks=[check_run("build")])
        self.gh.prs[("o", "r", 1)] = node
        self.gh.suites[node["headRefOid"]] = [{"id": "SF", "latest": 0, "runs": [self.red_publish()],
                                               "null_runs": True}]
        gg.register(gg.parse_key("pr", "o/r#1"))
        self.daemon.tick(NOW)
        data = gg.read_cache(gg.parse_key("pr", "o/r#1"))["data"]
        self.assertIn("Something went wrong", data.get("checksPageError") or "")
        self.assertNotEqual(gg.check_verdict(data, "green"), 0)

    def test_the_batched_pr_read_costs_one_point_per_pr(self) -> None:
        """GitHub prices a query by the requests its connections need: each connection costs the product of the
        first/last of the connections above it, the sum over 100 is the points. The suites' count of every attempt's
        runs, a second connection under checkSuites(first: 100), was 100 more requests per PR: a batch of ten PRs cost
        20 points (the daemon logged 1160 points over 58 PR queries on 2026-11-08). Without it the batch costs 10."""
        keys = [f"pr:o/r#{n}" for n in range(10)]
        query, _ = gg.pr_query(keys)
        self.assertEqual(graphql_requests(gg.pr_query(keys[:1])[0]), 104)  # rules, commits, suites, latest x100, contexts
        self.assertEqual(graphql_cost(query), 10)
        old = query.replace("latest: checkRuns(first: 1) { totalCount }", "latest: checkRuns(first: 1) { totalCount } "
                            "every: checkRuns(first: 1, filterBy: { checkType: ALL }) { totalCount }")
        self.assertEqual(graphql_cost(old), 20)  # the query this replaced, for the record
        # the batch's follow-up reads up to SUITE_RUNS_BATCH suites by id for one point
        self.assertEqual(graphql_cost(gg.SUITE_RUNS, {"$suites": gg.SUITE_RUNS_BATCH}), 1)

    def test_the_recovery_query_is_paced_by_graphql_per_minute(self) -> None:
        # Review finding F2 on glaeda#1844: last_real_contexts sent its query without checking the per-minute ceiling,
        # so a batch of heads with startup_failure suites could spend past graphqlPerMinute.
        self.daemon.policy = {**self.daemon.policy, "graphqlPerMinute": 3}
        self.daemon.gql_log = [(NOW - 30, "pr", 3)]  # this minute's points already spent
        self.daemon.now = NOW
        before = len(self.gh.gql_calls())
        nodes, error = self.daemon.last_real_contexts(["S1"])
        self.assertEqual(nodes, [])
        self.assertIn("graphqlPerMinute", error or "")
        self.assertEqual(len(self.gh.gql_calls()), before)  # nothing sent
        # with room, it reads
        self.gh.suites[SHA_A] = [{"id": "S1", "latest": 0, "runs": [self.red_publish()]}]
        self.daemon.gql_log = []
        nodes, error = self.daemon.last_real_contexts(["S1"])
        self.assertIsNone(error)
        self.assertEqual([n["name"] for n in nodes], ["publish"])

    def test_an_unread_check_suites_field_is_an_error_not_a_greener_read(self) -> None:
        # Review finding F2 on glaeda#1842 at 3af96afb: a null checkSuites beside an error hid nothing as "no suites".
        self.gh.heads[("o", "r", SHA_A)] = [check_run("CI / build")]
        self.gh.suites_unread = True
        gg.register(self.key(SHA_A), consumer="autoland")
        key = gg.parse_key("pr", "o/r#1")
        self.gh.prs[("o", "r", 1)] = pr_node(1, rollup="SUCCESS", checks=[check_run("build")])
        gg.register(key)
        self.daemon.tick(NOW)
        self.assertIn("check suites could not be read", gg.read_cache(self.key(SHA_A))["error"])
        data = gg.read_cache(key)["data"]
        self.assertIn("check suites could not be read", data["checksPageError"])
        self.assertIsNone(gg.check_verdict(data, "green"))

    def test_until_done_waits_for_running_checks_beside_a_recovered_red(self) -> None:
        # Review finding F1 on glaeda#1842 at 3af96afb.
        node = pr_node(1, rollup="PENDING", checks=[check_run("build", "IN_PROGRESS", None)])
        self.gh.prs[("o", "r", 1)] = node
        self.gh.suites[node["headRefOid"]] = [{"id": "S1", "latest": 0, "runs": [self.red_publish()]}]
        key = gg.parse_key("pr", "o/r#1")
        gg.register(key)
        self.daemon.tick(NOW)
        data = gg.read_cache(key)["data"]
        self.assertIsNone(gg.check_verdict(data, "done"))
        self.assertEqual(gg.check_verdict(data, "green"), gg.EXIT_FAIL)

    def test_an_unknown_commit_is_missing_not_pending(self) -> None:
        gg.register(self.key("d" * 40), consumer="autoland")
        self.daemon.tick(NOW)
        entry = gg.read_cache(self.key("d" * 40))
        self.assertTrue(entry["missing"])

    def test_a_glaeda_head_counts_the_forks_checks_at_that_commit(self) -> None:
        # main is verified in manaflow-ai/glaeda on Blacksmith; the personal repository has no Verify for a main head
        self.gh.heads[("teamleaderleo", "glaeda", SHA_A)] = [check_run("Release / build")]
        self.gh.heads[("manaflow-ai", "glaeda", SHA_A)] = [check_run(f"Verify / job {i}") for i in range(120)] + [
            check_run("Verify / ci-status", "COMPLETED", "FAILURE")]
        key = gg.parse_key("head", f"teamleaderleo/glaeda@{SHA_A}")
        gg.register(key, consumer="autoland")
        self.daemon.tick(NOW)
        found = gg.read_cache(key)["data"]["found"]
        self.assertEqual(found["check / Release / build"], ["completed", "success"])
        self.assertEqual(found["manaflow-ai/glaeda: check / Verify / ci-status"], ["completed", "failure"])  # past page 1
        self.assertEqual(len(found), 122)
        self.assertEqual(len(self.gh.gql_calls()), 2)  # one batched query for both repositories, one further page

    def test_a_same_named_check_in_the_fork_never_stands_in_for_one_here(self) -> None:
        # a newer green check of the same name in one repository must not hide a red one in the other
        mine = check_run("Verify / ci-status", "COMPLETED", "FAILURE")
        mine["databaseId"] = 1
        theirs = check_run("Verify / ci-status")
        theirs["databaseId"] = 99
        self.gh.heads[("teamleaderleo", "glaeda", SHA_A)] = [mine]
        self.gh.heads[("manaflow-ai", "glaeda", SHA_A)] = [theirs]
        key = gg.parse_key("head", f"teamleaderleo/glaeda@{SHA_A}")
        gg.register(key, consumer="autoland")
        self.daemon.tick(NOW)
        found = gg.read_cache(key)["data"]["found"]
        self.assertEqual(found["check / Verify / ci-status"], ["completed", "failure"])
        self.assertEqual(found["manaflow-ai/glaeda: check / Verify / ci-status"], ["completed", "success"])

    def test_a_glaeda_head_the_fork_lacks_has_only_its_own_checks(self) -> None:
        self.gh.heads[("teamleaderleo", "glaeda", SHA_B)] = [check_run("Release / build")]
        key = gg.parse_key("head", f"teamleaderleo/glaeda@{SHA_B}")
        gg.register(key, consumer="autoland")
        self.daemon.tick(NOW)
        entry = gg.read_cache(key)
        self.assertFalse(entry.get("missing"))
        self.assertEqual(entry["data"]["found"], {"check / Release / build": ["completed", "success"]})

    def test_an_error_reading_the_forks_checks_is_an_error_not_a_smaller_head(self) -> None:
        self.gh.heads[("teamleaderleo", "glaeda", SHA_A)] = [check_run("Release / build")]
        key = gg.parse_key("head", f"teamleaderleo/glaeda@{SHA_A}")
        gg.register(key, consumer="autoland")
        real = self.gh.__call__

        def partial(method, url, headers, body):
            status, head, raw = real(method, url, headers, body)
            if url.endswith("/graphql") and b"m0: " in (body or b""):
                doc = json.loads(raw)
                doc["data"]["m0"] = None
                doc["errors"] = [{"path": ["m0"], "message": "Something went wrong"}]
                raw = json.dumps(doc).encode()
            return status, head, raw

        self.daemon.transport = partial
        self.daemon.tick(NOW)
        entry = gg.read_cache(key)
        self.assertIsNone(entry["data"])
        self.assertIn("Something went wrong", entry["error"])
        self.assertEqual(len(self.daemon.head_spend.get("autoland", [])), 0)  # refunded

    def test_a_glaeda_head_the_fork_has_without_checks_yet_is_not_settled(self) -> None:
        self.gh.heads[("teamleaderleo", "glaeda", SHA_C)] = [check_run("Release / build")]
        self.gh.heads[("manaflow-ai", "glaeda", SHA_C)] = []
        key = gg.parse_key("head", f"teamleaderleo/glaeda@{SHA_C}")
        gg.register(key, consumer="autoland")
        self.daemon.tick(NOW)
        self.assertFalse(gg.read_cache(key)["data"]["settled"])

    def test_other_repositories_read_only_their_own_checks(self) -> None:
        gg.register(self.key(SHA_A), consumer="autoland")
        self.daemon.tick(NOW)
        self.assertNotIn(b"manaflow-ai", self.gh.gql_calls()[0][3] or b"")

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


def cli_run(ghdir: Path, *args, stderr=None, shell_prefix=()) -> tuple[subprocess.CompletedProcess, float]:
    """The real CLI in a subprocess, because fd 2 is the thing under test here.

    `contextlib.redirect_stderr` swaps `sys.stderr` and leaves file descriptor 2 alone, so every
    in-process test passes whatever fd 2 is doing -- which is exactly how a diagnostic that killed
    the wait got written in the first place. `GLAEDA_GH_DIR` keeps it off this machine's real
    daemon: these runs must never register a watch or ask the daemon for anything."""
    env = {**os.environ, "GLAEDA_GH_DIR": os.fspath(ghdir)}
    cmd = [*shell_prefix, sys.executable, os.fspath(ROOT / "scripts" / "glaeda-gh"), *args]
    start = time.monotonic()
    done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=stderr, text=True,
                          stdin=subprocess.DEVNULL, env=env, timeout=120)
    return done, time.monotonic() - start


class DaemonDiagnosticOnFd2Test(unittest.TestCase):
    """The waiting line is not the only thing a wait prints before it can do any work.

    `down_message`, `failing_message` and `stale_message` are written by the same functions, at the
    same moment, into the same fd, and they carried both faults the review measured on the waiting
    line: a closed stderr put them in the `--json` document, and a dead stderr pipe killed the
    process. They now go through `say_waiting` too, and this is where that is measured -- the
    in-process tests above route the seam into their own buffer, so a revert to
    `print(file=sys.stderr)` would leave every one of them green."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.ghdir = self.dir / "ghdir"  # no daemon.lock in it, so the CLI finds the daemon down

    DOWN_COMMANDS = (("wait", "pr", "o/r#1", "--timeout", "0.1"), ("status", "pr", "o/r#1"),
                     ("budget",))

    def test_a_closed_stderr_does_not_put_the_daemon_down_notice_in_the_json(self) -> None:
        """Every command that says the daemon is down, not just `wait`: `status` and `budget` write
        the same notice from their own copies of the check, and a mutation putting any one of them
        back on `sys.stderr` is a mutation that puts the notice into that command's `--json`."""
        for args in self.DOWN_COMMANDS:
            with self.subTest(args[0]):
                done, _ = cli_run(self.ghdir, *args, "--json",
                                  shell_prefix=("/bin/sh", "-c", 'exec 2>&-; exec "$@"', "sh"))
                self.assertEqual(done.returncode, 3, done.stdout)
                if args[0] in ("wait", "status"):
                    # the agent output contract: the one {ok, code, reason, next} object, and only it
                    assert_json_refusal(self, done.stdout, "daemon-down")
                    continue
                self.assertNotIn("daemon is not running", done.stdout,
                                 "the daemon-down notice went to stdout")
                # `budget --json` answers with a document even when the daemon is down, so the
                # test is that stdout is still *one document* -- which is what the notice landing
                # in front of it destroyed.
                if done.stdout:
                    json.loads(done.stdout)

    def test_a_dead_stderr_pipe_does_not_turn_the_daemon_down_notice_into_exit_120(self) -> None:
        read, write = os.pipe()
        os.close(read)
        try:
            done, _ = cli_run(self.ghdir, "wait", "pr", "o/r#1", "--timeout", "0.1", "--json",
                              stderr=write)
        finally:
            os.close(write)
        self.assertEqual(done.returncode, 3, done.stdout)

    def test_the_daemon_down_notice_reaches_fd_2_when_nothing_is_wrong(self) -> None:
        """The other half: both tests above also pass if the notice is never printed at all."""
        for args in self.DOWN_COMMANDS:
            with self.subTest(args[0]):
                err = self.dir / f"{args[0]}.txt"
                with err.open("w") as fh:
                    done, _ = cli_run(self.ghdir, *args, "--json", stderr=fh)
                self.assertEqual(done.returncode, 3)
                self.assertIn("daemon is not running", err.read_text())

    def test_the_wait_for_a_restart_line_is_on_fd_2_too(self) -> None:
        """The fourth sibling, and the one a mutation put back on `sys.stderr` with the whole suite
        green: `DownWait.patient`'s "the daemon is not running; waiting up to Ns" line, which is
        what a wait prints while it rides out a `launchctl` respawn.

        It needs a timeout longer than `DOWN_GRACE`, because the first `daemon_running` call spends
        that long looking for the daemon before anything is said at all -- which is why the shorter
        down-notice tests above never reach this line."""
        err = self.dir / "patience.txt"
        with err.open("w") as fh:
            done, _ = cli_run(self.ghdir, "wait", "pr", "o/r#1", "--timeout",
                              f"{gg.DOWN_GRACE + 2:g}", "--json", stderr=fh)
        self.assertEqual(done.returncode, 3, done.stdout)
        said = err.read_text()
        self.assertIn("waiting up to", said, "nothing on fd 2 while it rode out the restart")
        self.assertIn("daemon is not running", said)
        closed, _ = cli_run(self.ghdir, "wait", "pr", "o/r#1", "--timeout",
                            f"{gg.DOWN_GRACE + 2:g}", "--json",
                            shell_prefix=("/bin/sh", "-c", 'exec 2>&-; exec "$@"', "sh"))
        self.assertEqual(closed.returncode, 3, closed.stdout)
        assert_json_refusal(self, closed.stdout, "daemon-down")

    def with_a_daemon(self, daemon_json: dict) -> None:
        """A held lock and a `daemon.json`, which is all the CLI reads to decide the daemon is
        there and what state it is in. The lock is held by this process, so from the subprocess it
        looks exactly like a running daemon."""
        self.ghdir.mkdir(parents=True, exist_ok=True)
        lock = (self.ghdir / "daemon.lock").open("a+")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)
        (self.ghdir / "daemon.json").write_text(json.dumps(daemon_json))

    def test_the_failing_and_hung_notices_are_on_fd_2_too(self) -> None:
        """The two siblings of the daemon-down notice, measured the same way.

        They were left on `print(file=sys.stderr)` after the waiting line moved, and mutations
        putting either of them back survived the whole suite -- because the in-process tests route
        the seam into their own buffer, which is the right call there and blind to this. Both
        directions per notice: it is on fd 2, and it is not in the `--json` document when fd 2 is
        closed."""
        now = time.time()
        failing = {"at": now, "heartbeat": 30,
                   "failing": {"error": "KeyError", "site": "x", "count": 9,
                               "since": now - gg.FAILING_AFTER - 1}}
        hung = {"at": now - gg.STALE_HEARTBEAT - 60, "heartbeat": 30}
        for name, doc, want, code in (("failing", failing, "every cycle since", 3),
                                      ("hung", hung, "looks hung", gg.EXIT_TIMEOUT)):
            with self.subTest(name):
                self.setUp()  # a fresh GLAEDA_GH_DIR and a fresh lock per case
                self.with_a_daemon(doc)
                err = self.dir / f"{name}.txt"
                with err.open("w") as fh:
                    done, _ = cli_run(self.ghdir, "wait", "run", "o/r/1", "--timeout", "0.3",
                                      "--json", stderr=fh)
                self.assertEqual(done.returncode, code, done.stdout)
                self.assertIn(want, err.read_text(), f"{name}: nothing on fd 2")
                closed, _ = cli_run(self.ghdir, "wait", "run", "o/r/1", "--timeout", "0.3",
                                    "--json",
                                    shell_prefix=("/bin/sh", "-c", 'exec 2>&-; exec "$@"', "sh"))
                self.assertEqual(closed.returncode, code, closed.stdout)
                if name == "failing":  # --json: the agent output contract's one object, and only it
                    assert_json_refusal(self, closed.stdout, "daemon-failing")
                else:
                    self.assertNotIn(want, closed.stdout, f"{name}: the notice went to stdout")
                if closed.stdout:
                    json.loads(closed.stdout)  # and the document is still one document

    def test_a_refused_argument_does_not_look_like_a_wait_that_timed_out(self) -> None:
        """`argparse` exits **2** on a bad argument and 2 is this tool's "timed out", so
        `--timeout -5` -- refused before any waiting happened -- was indistinguishable from a wait
        that ran its course, to the one kind of caller this tool is for. 64 is what every other bad
        argument here exits with.

        And the usage text is a diagnostic like any other: with stderr closed it used to be
        `print(file=None)`, which writes to stdout, so a typo became the first lines of a `--json`
        document."""
        for bad in ("-5", "nan", "inf", "soon"):
            with self.subTest(bad=bad):
                err = self.dir / "usage.txt"
                with err.open("w") as fh:
                    done, _ = cli_run(self.ghdir, "wait", "file", str(self.dir / "x"),
                                      "--timeout", bad, "--json", stderr=fh)
                self.assertEqual(done.returncode, 64, done.stdout)
                # the agent output contract: --json is one {ok, code, reason, next} object, and no diagnostic
                assert_json_refusal(self, done.stdout, "usage")
                said = err.read_text()
                self.assertIn("is not a number of seconds", said)
                # The usage text as well as the reason: a message naming a flag, with no shape of
                # the command to put it in, is half an answer -- and dropping it left the suite
                # green on the reason alone.
                self.assertIn("usage: glaeda-gh wait", said, said)
        # The tool's *own* refusals (`WatchError`) take the same path and had the same two faults:
        # they are not argparse's, so nothing above reaches them.
        for args, reason in ((["wait", "pr", "o/r#1", "--author", "x"], "for wait comment"),
                             (["wait", "comment", "o/r#1", "--sha", "a" * 40], "not --sha"),
                             (["wait", "run", "o/r/1", "--rescue-grace", "-1"], "rescue-grace")):
            with self.subTest(args=args[1]):
                err = self.dir / "refused.txt"
                with err.open("w") as fh:
                    done, _ = cli_run(self.ghdir, *args, "--json", stderr=fh)
                self.assertEqual(done.returncode, 64, done.stdout)
                assert_json_refusal(self, done.stdout, "refused")
                self.assertIn(reason, err.read_text())
        for args in (["wait", "file", str(self.dir / "x"), "--timeout", "-5"],
                     ["wait", "pr", "o/r#1", "--author", "x"]):
            closed, _ = cli_run(self.ghdir, *args, "--json",
                                shell_prefix=("/bin/sh", "-c", 'exec 2>&-; exec "$@"', "sh"))
            self.assertEqual(closed.returncode, 64)
            assert_json_refusal(self, closed.stdout, None)
        # A timeout that is merely 0 is a legitimate "look once", not a bad argument.
        with (self.dir / "once.txt").open("w") as fh:
            once, _ = cli_run(self.ghdir, "wait", "file", str(self.dir / "x"), "--timeout", "0",
                              "--json", stderr=fh)
        self.assertEqual(once.returncode, gg.EXIT_TIMEOUT, once.stdout)


class RerunWaitTest(Base):
    """2026-11-07: `glaeda-gh wait run manaflow-ai/cmux/42368426030` right after `gh run rerun --failed` returned
    attempt 1's failure at once (waited_s 0, "data 0s ago") while GitHub had attempt 2 queued. The client's
    controller long-poll stored the controller's attempt-1 record as new data, and the restRun of an earlier REST
    read of that same attempt passed it as GitHub's word. A wait now rules on a finish only once GitHub has shown
    it since the wait began (restRunAt), and the daemon reads it once for that."""

    KEY = "run:o/r/50"

    def setUp(self) -> None:
        super().setUp()
        self.ctl = FakeController()
        feed_every, gg.CONTROLLER_FEED_EVERY = gg.CONTROLLER_FEED_EVERY, 0
        self.addCleanup(setattr, gg, "CONTROLLER_FEED_EVERY", feed_every)
        self.daemon = gg.Daemon(transport=self.gh, token=lambda: TOKEN, controller=self.ctl)
        self.old = time.time() - 600
        attempt1 = {"id": 50, "status": "completed", "conclusion": "failure", "run_attempt": 1,
                    "updated_at": iso(self.old)}
        self.ctl.runs[50] = {**ctl_run(50, "completed", "failure", iso(self.old)), "attempt": 1}
        # what an earlier REST read of attempt 1 left: GitHub's own word, ten minutes old
        gg.write_json(gg.cache_dir() / gg.file_name(self.KEY), {
            "key": self.KEY, "fetchedAt": self.old, "dataAt": self.old, "restAt": self.old, "source": "rest",
            "data": attempt1, "restRun": gg.rest_run(attempt1), "restRunAt": self.old, "error": None})

    def begin(self) -> dict:
        """A `wait run` as subscribe_wait starts it: interest, then the client's controller long-poll."""
        req = gg.wait_request("run", "o/r/50", "green", 600, False, False, None, True, 0)
        gg.register(self.KEY, waited=True)
        controller = mock.Mock()
        controller.run.return_value = self.ctl.runs[50]  # the controller has not heard of the rerun
        with mock.patch.object(gg.Controller, "from_env", return_value=controller):
            gg.controller_wait_run(self.KEY, 5)
        return req

    def rerun(self, status: str = "queued", conclusion: str | None = None, at: float | None = None) -> None:
        self.gh.runs[50] = {"id": 50, "status": status, "conclusion": conclusion, "run_attempt": 2,
                            "updated_at": iso(time.time() if at is None else at)}

    def test_a_wait_right_after_a_rerun_blocks_until_attempt_2_ends(self) -> None:
        self.rerun()
        req = self.begin()
        self.assertEqual(gg.read_cache(self.KEY)["data"]["run_attempt"], 1, "the long-poll stored attempt 1")
        self.assertIsNone(gg.wait_step(req, time.time()), "attempt 1's finish decided a wait after its rerun")
        self.daemon.tick(time.time() + 1)
        entry = gg.read_cache(self.KEY)
        self.assertEqual((entry["data"]["run_attempt"], entry["data"]["status"]), (2, "queued"))
        self.assertEqual(len(self.gh.run_calls()), 1, "the wait's one GitHub read")
        self.assertIsNone(gg.wait_step(req, time.time() + 1))
        self.rerun("completed", "success", at=time.time() + 2)
        self.ctl.runs[50] = {**ctl_run(50, "completed", "success", iso(time.time() + 2)), "attempt": 2}
        self.daemon.tick(time.time() + 3 + gg.CYCLE)
        code, text = gg.wait_step(req, time.time() + 3 + gg.CYCLE)
        self.assertEqual(code, gg.EXIT_OK, text)
        self.assertIn("attempt 2", text)

    def test_a_cancelled_attempt_2_is_its_result(self) -> None:
        self.rerun()
        req = self.begin()
        self.daemon.tick(time.time() + 1)
        self.assertIsNone(gg.wait_step(req, time.time() + 1))
        self.rerun("completed", "cancelled", at=time.time() + 2)
        self.daemon.tick(time.time() + 3 + gg.CYCLE)
        code, text = gg.wait_step(req, time.time() + 3 + gg.CYCLE)
        self.assertEqual(code, gg.EXIT_FAIL, text)
        self.assertIn("cancelled", text)
        self.assertIn("attempt 2", text)

    def test_a_final_attempt_with_no_rerun_rules_on_the_first_tick(self) -> None:
        self.gh.runs[50] = {"id": 50, "status": "completed", "conclusion": "failure", "run_attempt": 1,
                            "updated_at": iso(self.old)}
        req = self.begin()
        self.daemon.tick(time.time() + 1)
        self.assertEqual(len(self.gh.run_calls()), 1)
        code, text = gg.wait_step(req, time.time() + 1)
        self.assertEqual(code, gg.EXIT_FAIL, text)
        self.assertIn("attempt 1", text)

    def test_a_finish_github_showed_after_the_wait_began_rules_without_a_read(self) -> None:
        req = gg.wait_request("run", "o/r/50", "green", 600, False, False, None, True, 0)
        entry = gg.read_cache(self.KEY)
        gg.write_json(gg.cache_dir() / gg.file_name(self.KEY),
                      {**entry, "fetchedAt": req["began"] + 1, "dataAt": req["began"] + 1, "restRunAt": req["began"] + 1})
        self.assertEqual(gg.wait_step(req, req["began"] + 1)[0], gg.EXIT_FAIL)

    def test_a_brokered_rerun_makes_the_cached_finish_unknown(self) -> None:
        req = gg.wait_request("run", "o/r/50", "green", 600, False, False, None, True, 0)
        entry = gg.read_cache(self.KEY)
        gg.write_json(gg.cache_dir() / gg.file_name(self.KEY),
                      {**entry, "fetchedAt": req["began"] + 1, "dataAt": req["began"] + 1, "restRunAt": req["began"] + 1})
        gg.note_run_rerun("o/r", ["run 50", "runlists", "jobs", "prs", "other"], ["run", "rerun", "50"], req["began"] + 2)
        self.assertEqual(gg.read_cache(self.KEY)["rerunAt"], req["began"] + 2)
        self.assertIsNone(gg.wait_step(req, req["began"] + 3), "the finish from before the rerun decided the wait")
        # the API form counts too; other writes on the run do not
        gg.note_run_rerun("o/r", ["run 50"], ["api", "repos/o/r/actions/runs/50/rerun-failed-jobs"], req["began"] + 4)
        self.assertEqual(gg.read_cache(self.KEY)["rerunAt"], req["began"] + 4)
        gg.note_run_rerun("o/r", ["run 50"], ["run", "cancel", "50"], req["began"] + 9)
        self.assertEqual(gg.read_cache(self.KEY)["rerunAt"], req["began"] + 4)
        self.rerun()
        gg.register(self.KEY)  # a reader, not a wait
        self.daemon.tick(req["began"] + 5)  # the rerun is read even with no wait on it
        self.assertEqual(gg.read_cache(self.KEY)["data"]["run_attempt"], 2)

    def test_with_github_unreadable_the_wait_still_rules_after_the_bound(self) -> None:
        req = self.begin()
        self.assertIsNone(gg.wait_step(req, req["began"] + 1))
        code, _ = gg.wait_step(req, req["began"] + gg.RUN_CONFIRM_MAX + 1)
        self.assertEqual(code, gg.EXIT_FAIL)


class WaitSubscriptionTest(Base):
    """2026-10-21, app-efficiency: 89 `glaeda-gh wait` processes held 1.6 GB on cosmic-gray, a whole
    interpreter per waiter re-reading the cache every 3 s. A `wait pr|run` is now a request the daemon
    rules on (Daemon.serve_waits), and the waiter is a few-line bash blocked reading a FIFO."""

    def setUp(self) -> None:
        super().setUp()
        gg.BASE.mkdir(parents=True)
        self.lock = open(gg.BASE / "daemon.lock", "a+")
        fcntl.flock(self.lock, fcntl.LOCK_EX)  # stands in for a running daemon
        self.addCleanup(self.lock.close)
        gg.write_json(gg.BASE / "daemon.json", {"at": time.time(), "heartbeat": 30, "waits": gg.WAITS_CONTRACT})

    def put(self, key: str, data: dict | None, at: float | None = None) -> None:
        at = time.time() if at is None else at
        gg.write_json(gg.cache_dir() / gg.file_name(key), {"key": key, "fetchedAt": at, "dataAt": at if data else None,
                                                          "data": data, "error": None})

    def request(self, kind: str = "pr", target: str = "o/r#1", timeout: float = 60, pid: int | None = None,
                **kw) -> tuple[str, int]:
        """A request as subscribe_wait leaves it, and a reader on its FIFO standing in for the blocker."""
        rid = gg.write_wait_request(kind, target, kw.get("until", "green"), timeout, False, kw.get("as_json", False),
                                    kw.get("sha"), True, gg.RESCUE_GRACE, pid or os.getpid())
        reader = os.open(gg.waits_dir() / f"{rid}.fifo", os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        return rid, reader

    def woken(self, reader: int) -> str:
        try:
            return os.read(reader, 64).decode()
        except BlockingIOError:
            return ""

    def test_the_daemon_rules_and_wakes_the_blocker(self) -> None:
        rid, reader = self.request()
        self.assertTrue(gg.read_json(gg.watch_dir() / gg.file_name("pr:o/r#1")), "the wait registered no interest")
        self.daemon.serve_waits()
        self.assertEqual(self.woken(reader), "", "a wait with nothing fetched yet was decided")
        self.put("pr:o/r#1", gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a")])))
        self.daemon.serve_waits()
        self.assertEqual(self.woken(reader), "0\n")
        out = (gg.waits_dir() / f"{rid}.out").read_text()
        self.assertIn("result: success", out)
        self.assertFalse((gg.waits_dir() / f"{rid}.json").exists(), "a decided request stayed")

    def test_a_red_or_a_timeout_says_what_cmd_wait_says(self) -> None:
        rid, reader = self.request("pr", "o/r#2")
        self.put("pr:o/r#2", gg.parse_pr(pr_node(2, rollup="FAILURE", checks=[check_run("a", conclusion="FAILURE")])))
        self.daemon.serve_waits()
        self.assertEqual(self.woken(reader), "1\n")
        self.assertIn("failed: a  https://ci/a", (gg.waits_dir() / f"{rid}.out").read_text())

        rid, reader = self.request("run", "o/r/7", timeout=0)
        self.daemon.serve_waits()
        self.assertEqual(self.woken(reader), "2\n")
        self.assertIn("result: timeout", (gg.waits_dir() / f"{rid}.out").read_text())

    def test_data_from_before_the_wait_began_never_decides_it(self) -> None:
        self.put("pr:o/r#1", gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a")])), at=time.time() - 30)
        _, reader = self.request()
        self.daemon.serve_waits()
        self.assertEqual(self.woken(reader), "")

    def test_a_request_whose_waiter_is_gone_is_dropped(self) -> None:
        child = subprocess.Popen(["true"])
        child.wait()
        rid, _ = self.request(pid=child.pid)
        self.daemon.serve_waits()
        self.assertEqual(list(gg.waits_dir().glob(f"{rid}*")), [])

    def test_what_a_killed_blocker_left_is_swept(self) -> None:
        child = subprocess.Popen(["true"])
        child.wait()
        rid = f"{child.pid}-0000beef"
        gg.waits_dir().mkdir(parents=True, exist_ok=True)
        os.mkfifo(gg.waits_dir() / f"{rid}.fifo")
        (gg.waits_dir() / f"{rid}.out").write_text("result: success\n")
        (gg.waits_dir() / f"{rid}.out.rc").write_text("0\n")
        live, _ = self.request()  # a live wait of this process is left alone
        self.daemon.serve_waits()
        self.assertEqual(sorted(p.name for p in gg.waits_dir().iterdir()), [".served", f"{live}.fifo", f"{live}.json"])

    def test_a_request_long_past_its_deadline_or_unreadable_is_dropped(self) -> None:
        """A blocker hands its wait back at the deadline, so a request still here WAITS_LEFTOVER later has
        none (its pid reused); one that cannot be ruled on must not stop the others."""
        old, _ = self.request(timeout=1)
        req = gg.read_json(gg.waits_dir() / f"{old}.json")
        gg.write_json(gg.waits_dir() / f"{old}.json", {**req, "began": time.time() - gg.WAITS_LEFTOVER - 10})
        bad, _ = self.request("pr", "o/r#3")
        gg.write_json(gg.waits_dir() / f"{bad}.json", {"pid": os.getpid()})
        rid, reader = self.request("pr", "o/r#2")
        self.put("pr:o/r#2", gg.parse_pr(pr_node(2, rollup="SUCCESS", checks=[check_run("a")])))
        self.daemon.serve_waits()
        self.assertEqual(self.woken(reader), "0\n")
        self.assertEqual(list(gg.waits_dir().glob(f"{old}*")), [])
        self.assertEqual(list(gg.waits_dir().glob(f"{bad}*")), [])

    def test_ticks_that_keep_raising_stop_telling_blockers_waits_are_served(self) -> None:
        gg.waits_dir().mkdir(parents=True, exist_ok=True)
        served = gg.waits_dir() / ".served"
        self.daemon.tick_failed("boom", "here")
        with mock.patch.object(self.daemon, "post_tick", side_effect=SystemExit("stop the tick here")):
            with self.assertRaises(SystemExit):
                self.daemon.tick()
        self.assertFalse(served.exists(), "a failing daemon told its blockers it serves waits")
        self.daemon.failing = None
        with mock.patch.object(self.daemon, "post_tick", side_effect=SystemExit("stop the tick here")):
            with self.assertRaises(SystemExit):
                self.daemon.tick()
        self.assertTrue(served.exists(), "a healthy tick did not mark waits served before its reads")

    def test_the_tick_rules_on_waits_and_says_it_does(self) -> None:
        rid, reader = self.request()
        self.put("pr:o/r#1", gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a")])))
        status = self.daemon.tick()
        self.assertEqual(self.woken(reader), "0\n")
        self.assertEqual((status["waits"], status["waiting"]), (gg.WAITS_CONTRACT, 0))
        self.assertTrue((gg.waits_dir() / ".served").exists())

    def test_a_blocker_not_listening_yet_is_woken_on_a_later_tick(self) -> None:
        rid = gg.write_wait_request("pr", "o/r#1", "green", 60, False, False, None, True, gg.RESCUE_GRACE, os.getpid())
        self.put("pr:o/r#1", gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a")])))
        self.daemon.serve_waits()  # decided, but nobody reads the FIFO yet
        self.assertEqual(gg.read_json(gg.waits_dir() / f"{rid}.json")["code"], 0)
        reader = os.open(gg.waits_dir() / f"{rid}.fifo", os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        self.daemon.serve_waits()
        self.assertEqual(self.woken(reader), "0\n")

    def test_the_cli_waits_as_a_shell_and_exits_with_the_verdict(self) -> None:
        env = dict(os.environ, GLAEDA_GH_DIR=os.fspath(gg.BASE))
        child = subprocess.Popen([sys.executable, os.fspath(ROOT / "scripts" / "glaeda-gh"), "wait", "pr", "o/r#1",
                                  "--timeout", "60"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline, comm = time.monotonic() + 20, ""
            while time.monotonic() < deadline:
                comm = subprocess.run(["ps", "-o", "comm=", "-p", str(child.pid)], capture_output=True, text=True).stdout
                if "bash" in comm:
                    break
                time.sleep(0.05)
            self.assertIn("bash", comm, "the waiter is still a Python process")
            self.put("pr:o/r#1", gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a")])))
            self.daemon.serve_waits()
            out, err = child.communicate(timeout=20)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        self.assertEqual(child.returncode, 0, out + err)
        self.assertIn("result: success", out)
        self.assertEqual([p.name for p in gg.waits_dir().iterdir()], [".served"], "the wait left files behind")

    def test_an_older_daemon_gets_the_polling_wait(self) -> None:
        gg.write_json(gg.BASE / "daemon.json", {"at": time.time(), "heartbeat": 30})  # no "waits"
        with mock.patch.object(gg, "cmd_wait", return_value=7) as polling, mock.patch.object(gg.os, "execv") as execv:
            self.assertEqual(gg.subscribe_wait("pr", "o/r#1", "green", 60, False, False, None, gg.RESCUE_GRACE), 7)
        polling.assert_called_once()
        execv.assert_not_called()

    def test_an_unbounded_timeout_gets_the_polling_wait(self) -> None:
        with mock.patch.object(gg, "cmd_wait", return_value=2) as polling:
            self.assertEqual(gg.subscribe_wait("pr", "o/r#1", "green", float("inf"), False, False), 2)
        polling.assert_called_once()

    def blocker(self, rid: str, limit: int, check: int) -> tuple[subprocess.CompletedProcess, float]:
        """BLOCKER on RID's FIFO, handing back to a stand-in for `wait-resume` that says so and exits 5."""
        d, began = gg.waits_dir(), time.monotonic()
        done = subprocess.run([gg.BASH, "-c", gg.BLOCKER, "glaeda-gh-wait", os.fspath(d / f"{rid}.fifo"),
                               os.fspath(d / f"{rid}.out"), str(limit), os.fspath(d / ".served"), str(check),
                               sys.executable, "-c", "import sys; print('resumed', sys.argv[1]); sys.exit(5)", rid],
                              capture_output=True, text=True, timeout=30)
        return done, time.monotonic() - began

    def test_the_blocker_hands_the_wait_back_at_its_deadline(self) -> None:
        rid = gg.write_wait_request("pr", "o/r#1", "green", 60, False, False, None, True, gg.RESCUE_GRACE, os.getpid())
        done, _ = self.blocker(rid, 1, 60)
        self.assertEqual((done.returncode, done.stdout), (5, f"resumed {rid}\n"), done.stderr)
        self.assertFalse((gg.waits_dir() / f"{rid}.out.mark").exists())

    def test_the_blocker_hands_back_once_no_tick_serves_waits(self) -> None:
        """A daemon that died, fails every tick or is an older one stops touching .served: the wait goes
        back to Python after one check, not at its deadline an hour later."""
        rid = gg.write_wait_request("pr", "o/r#1", "green", 600, False, False, None, True, gg.RESCUE_GRACE, os.getpid())
        done, took = self.blocker(rid, 600, 1)
        self.assertEqual(done.returncode, 5, done.stderr)
        self.assertLess(took, 10)

    def test_the_blocker_hands_back_once_its_request_is_dropped(self) -> None:
        rid = gg.write_wait_request("pr", "o/r#1", "green", 600, False, False, None, True, gg.RESCUE_GRACE, os.getpid())
        stop = threading.Event()

        def ticks() -> None:
            gg.drop_wait(rid)  # the daemon dropped it; ticks still serve the other waits
            while not stop.wait(0.2):
                (gg.waits_dir() / ".served").touch()
        timer = threading.Timer(0.5, ticks)
        timer.start()
        try:
            done, took = self.blocker(rid, 600, 2)
        finally:
            stop.set()
            timer.join()
        self.assertEqual(done.returncode, 5, done.stderr)
        self.assertLess(took, 10)

    def test_a_request_that_cannot_be_written_back_stays_for_the_next_tick(self) -> None:
        rid, reader = self.request()
        self.put("pr:o/r#1", gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a")])))
        with mock.patch.object(gg.Path, "write_text", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(self.daemon.serve_waits(), 1)
        self.assertTrue((gg.waits_dir() / f"{rid}.fifo").exists(), "a full disk dropped the wait")
        self.daemon.serve_waits()
        self.assertEqual(self.woken(reader), "0\n")

    def test_the_blocker_sleeps_on_while_ticks_serve_waits(self) -> None:
        rid = gg.write_wait_request("pr", "o/r#1", "green", 60, False, False, None, True, gg.RESCUE_GRACE, os.getpid())
        stop = threading.Event()

        def ticks() -> None:
            while not stop.wait(0.2):
                (gg.waits_dir() / ".served").touch()
        ticker = threading.Thread(target=ticks)
        ticker.start()
        try:
            done, took = self.blocker(rid, 6, 2)  # 2 s: bash 3.2's -nt compares whole seconds
        finally:
            stop.set()
            ticker.join()
        self.assertEqual(done.returncode, 5, done.stderr)
        self.assertGreater(took, 5, "handed back before its deadline while waits were served")

    def resume(self, rid: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.diagnostics_in(err):
            code = gg.main(["wait-resume", rid])
        return code, out.getvalue(), err.getvalue()

    def test_resume_prints_an_answer_the_blocker_missed(self) -> None:
        """The daemon wrote the code while the blocker was handing back: .out.rc still says it."""
        rid = gg.write_wait_request("pr", "o/r#1", "green", 60, False, False, None, True, gg.RESCUE_GRACE, os.getpid())
        (gg.waits_dir() / f"{rid}.out").write_text("result: failure\n")
        (gg.waits_dir() / f"{rid}.out.rc").write_text("1\n")
        self.assertEqual(self.resume(rid), (1, "result: failure\n", ""))
        self.assertEqual(list(gg.waits_dir().glob(f"{rid}*")), [])

    def test_resume_sleeps_again_while_the_daemon_serves_waits(self) -> None:
        rid = gg.write_wait_request("pr", "o/r#1", "green", 60, False, False, None, True, gg.RESCUE_GRACE, os.getpid())
        with mock.patch.object(gg.os, "execv", side_effect=SystemExit(99)) as execv:
            with self.assertRaises(SystemExit):
                self.resume(rid)
        argv = execv.call_args.args[1]
        self.assertEqual(argv[:3], [gg.BASH, "-c", gg.BLOCKER])
        self.assertLessEqual(int(argv[6]), 60)
        self.assertEqual(argv[-2:], ["wait-resume", rid])
        self.assertTrue((gg.waits_dir() / f"{rid}.json").exists(), "the request was dropped")

    def test_resume_of_a_failing_daemon_says_what_the_polling_wait_says(self) -> None:
        rid = gg.write_wait_request("pr", "o/r#1", "green", 60, False, False, None, True, gg.RESCUE_GRACE, os.getpid())
        now = time.time()
        gg.write_json(gg.BASE / "daemon.json", {"at": now, "heartbeat": 30, "waits": gg.WAITS_CONTRACT,
                                                "failing": {"error": "boom", "since": now - 300, "count": 99}})
        code, _, err = self.resume(rid)
        self.assertEqual(code, gg.EXIT_DOWN)
        self.assertIn("every cycle since", err)
        self.assertEqual(list(gg.waits_dir().glob(f"{rid}*")), [])

    def test_the_blocker_leaves_no_progress_writer_behind_on_a_full_stderr(self) -> None:
        """A reader that is alive but never reads: the progress printf blocks on the full pipe. Once the
        wait hands back, that writer is gone, so repeated waits do not pile up blocked children."""
        rid = gg.write_wait_request("pr", "o/r#1", "green", 60, False, False, None, True, gg.RESCUE_GRACE, os.getpid())
        d = gg.waits_dir()
        reader, writer = os.pipe()
        os.set_blocking(writer, False)
        with contextlib.suppress(BlockingIOError):
            while True:
                os.write(writer, b"x" * 4096)
        os.set_blocking(writer, True)
        stop = threading.Event()

        def ticks() -> None:
            while not stop.wait(0.2):
                (d / ".served").touch()
        ticker = threading.Thread(target=ticks)
        ticker.start()
        env = dict(os.environ, GLAEDA_WAIT_PROGRESS="1", GLAEDA_WAIT_KEY="pr:o/r#1", GLAEDA_WAIT_TIMEOUT="60",
                   GLAEDA_WAIT_DEADLINE="00:00:00Z")
        children = "import os, subprocess; print(subprocess.run(['pgrep', '-P', str(os.getpid())], capture_output=True, text=True).stdout.strip() or 'none')"
        try:
            done = subprocess.run([gg.BASH, "-c", gg.BLOCKER, "glaeda-gh-wait", os.fspath(d / f"{rid}.fifo"),
                                   os.fspath(d / f"{rid}.out"), "4", os.fspath(d / ".served"), "2",
                                   sys.executable, "-c", children],
                                  stdout=subprocess.PIPE, stderr=writer, text=True, timeout=30, env=env)
        finally:
            stop.set()
            ticker.join()
            os.close(writer)
            os.close(reader)
        self.assertEqual((done.returncode, done.stdout), (0, "none\n"))

    def test_a_resumed_json_wait_that_is_refused_prints_its_object(self) -> None:
        rid = gg.write_wait_request("run", "o/r/1", "green", 60, False, True, "a" * 40, True, gg.RESCUE_GRACE, os.getpid())
        with mock.patch.object(gg, "serving_waits", return_value=False), \
                mock.patch.object(gg, "poll_wait", side_effect=gg.WatchError("--sha does not match run o/r/1")):
            code, out, err = self.resume(rid)
        self.assertEqual(code, 64)
        assert_json_refusal(self, out, "refused")
        self.assertEqual(err.strip().splitlines()[-1], "next: glaeda-gh wait --help")

    def test_resume_at_the_deadline_times_out_as_the_polling_wait_does(self) -> None:
        rid = gg.write_wait_request("pr", "o/r#1", "green", 0, False, False, None, True, gg.RESCUE_GRACE, os.getpid())
        code, out, _ = self.resume(rid)
        self.assertEqual(code, gg.EXIT_TIMEOUT)
        self.assertIn("result: timeout", out)
        self.assertEqual(list(gg.waits_dir().glob(f"{rid}*")), [])

    def test_the_subscribed_cli_says_the_waiting_line_once_while_it_blocks(self) -> None:
        """The waiting line (waiting_line) on the daemon path: on fd 2 of the real CLI, read while the
        waiter is the bash BLOCKER -- so it was written, and flushed, before the exec -- and said once."""
        env = dict(os.environ, GLAEDA_GH_DIR=os.fspath(gg.BASE))
        child = subprocess.Popen([sys.executable, os.fspath(ROOT / "scripts" / "glaeda-gh"), "wait", "pr", "o/r#1",
                                  "--timeout", "60"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            first = child.stderr.readline()
            self.assertIn("waiting on pr:o/r#1 until green", first)
            deadline, comm = time.monotonic() + 20, ""
            while time.monotonic() < deadline:
                comm = subprocess.run(["ps", "-o", "comm=", "-p", str(child.pid)], capture_output=True, text=True).stdout
                if "bash" in comm:
                    break
                time.sleep(0.05)
            self.assertIn("bash", comm, "the waiter never became the blocker")
            self.put("pr:o/r#1", gg.parse_pr(pr_node(1, rollup="SUCCESS", checks=[check_run("a")])))
            self.daemon.serve_waits()
            out, err = child.communicate(timeout=20)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        self.assertEqual(child.returncode, 0, out + first + err)
        self.assertEqual((first + err).count("waiting on"), 1, first + err)
        self.assertNotIn("waiting on", out, "the line went to stdout")

    def test_the_subscribe_path_says_the_line_once_before_it_blocks(self) -> None:
        said: list[str] = []
        with mock.patch.object(gg, "say_waiting", said.append), \
                mock.patch.object(gg, "block", side_effect=lambda rid, left, *rest: said.append("block")):
            gg.subscribe_wait("pr", "o/r#1", "green", 60, False, False, None, gg.RESCUE_GRACE)
        self.assertEqual(len([s for s in said if "waiting on" in s]), 1, said)
        self.assertEqual(said[-1], "block", "the line came after the wait blocked")

    def test_every_fallback_says_the_line_once(self) -> None:
        """Not served (an older daemon), an unbounded timeout, and a request that cannot be written: each
        polls, and each says the line exactly once -- never once from subscribe_wait and again from cmd_wait."""
        cases = {
            "older daemon": lambda: gg.write_json(gg.BASE / "daemon.json", {"at": time.time(), "heartbeat": 30}),
            "over WAITS_MAX_TIMEOUT": lambda: None,
            "request not written": lambda: None,
        }
        for name, arrange in cases.items():
            with self.subTest(name):
                gg.write_json(gg.BASE / "daemon.json", {"at": time.time(), "heartbeat": 30, "waits": gg.WAITS_CONTRACT})
                arrange()
                timeout = gg.WAITS_MAX_TIMEOUT * 2 if name == "over WAITS_MAX_TIMEOUT" else 0.1
                said: list[str] = []
                patches = [mock.patch.object(gg, "say_waiting", said.append),
                           mock.patch.object(gg.os, "execv", side_effect=AssertionError("blocked"))]
                if name == "request not written":
                    patches.append(mock.patch.object(gg, "write_wait_request", side_effect=OSError(28, "full")))
                if name == "over WAITS_MAX_TIMEOUT":
                    patches.append(mock.patch.object(gg, "poll_wait", return_value=gg.EXIT_TIMEOUT))
                with contextlib.ExitStack() as stack:
                    for patch in patches:
                        stack.enter_context(patch)
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    self.assertEqual(gg.subscribe_wait("pr", "o/r#1", "green", timeout, False, False), gg.EXIT_TIMEOUT)
                self.assertEqual(len([s for s in said if "waiting on" in s]), 1, said)

    def test_a_resumed_wait_does_not_say_the_line_again(self) -> None:
        """BLOCKER handing back (resume_wait), whether it sleeps again or polls, is the same wait: the line
        was said when it began."""
        rid = gg.write_wait_request("pr", "o/r#1", "green", 60, False, False, None, True, gg.RESCUE_GRACE, os.getpid())
        said: list[str] = []
        with mock.patch.object(gg, "say_waiting", said.append), \
                mock.patch.object(gg.os, "execv", side_effect=SystemExit(99)), self.assertRaises(SystemExit):
            gg.resume_wait(rid)
        rid = gg.write_wait_request("pr", "o/r#1", "green", 0, False, False, None, True, gg.RESCUE_GRACE, os.getpid())
        with mock.patch.object(gg, "say_waiting", said.append), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gg.resume_wait(rid), gg.EXIT_TIMEOUT)
        self.assertEqual([s for s in said if "waiting on" in s], [], said)

    def test_seconds_and_the_subscribe_range_agree(self) -> None:
        """What `seconds` lets through, subscribe_wait either serves or polls; nothing it refuses reaches it."""
        for bad in ("inf", "-inf", "nan", "-1"):
            with self.subTest(bad), self.assertRaises(gg.argparse.ArgumentTypeError):
                gg.seconds(bad)
        for good in ("0", "1.5", str(gg.WAITS_MAX_TIMEOUT)):
            with self.subTest(good):
                self.assertTrue(0 <= gg.seconds(good) <= gg.WAITS_MAX_TIMEOUT)


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

    def test_the_file_and_pid_waits_say_what_they_wait_for_before_they_block(self) -> None:
        """Every kind calls `say_waiting`, and calls it *before* it blocks.

        Through the seam rather than through stderr, because what each call site has to get right
        is the call and its position; what `say_waiting` then does with fd 2 is its own test
        below. An earlier version asserted on a captured `sys.stderr` instead, which meant the
        production path was never the one measured: `say_waiting` grew a branch that only the
        suite reached, and a mutation deferring the real write to `atexit` -- the line arriving
        after the wait, which is no use to anyone -- passed all 337 tests."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        for want, call in ((str(self.dir / "nope"), lambda: gg.cmd_wait_file(str(self.dir / "nope"), 0.05, True)),
                           (f"pid {proc.pid}", lambda: gg.cmd_wait_pid(str(proc.pid), 0.05, True))):
            with self.subTest(want=want):
                said: list[str] = []
                out = io.StringIO()
                with mock.patch.object(gg, "say_waiting", said.append), \
                        contextlib.redirect_stdout(out):
                    self.assertEqual(call(), gg.EXIT_TIMEOUT)
                self.assertEqual(len(said), 1, said)
                self.assertIn("waiting on", said[0])
                self.assertIn(want, said[0])
                self.assertIn("0.05", said[0])
                json.loads(out.getvalue())  # one document, and the line is not in it

    def cli_wait(self, *args, stderr=None, shell_prefix=()) -> tuple[subprocess.CompletedProcess, float]:
        return cli_run(self.dir / "ghdir", *args, stderr=stderr, shell_prefix=shell_prefix)

    def test_a_dead_stderr_pipe_does_not_kill_the_wait(self) -> None:
        """The line must never become the failure it exists to diagnose. With stderr a pipe whose
        reader is gone -- `wait ... 2>&1 | head -1`, or the `setsid nohup ... &` the skill
        recommends, from a shell that is being reaped -- `print` raised at interpreter shutdown and
        the process died in 0.19 s with exit **120**, CPython's "failed to flush stderr", which is
        not even in this tool's documented exit codes. It used to wait its whole timeout and exit
        2. Measured through the real CLI: `say_waiting` writes fd 2 directly and suppresses
        `OSError`, so both the exit code and the duration come back."""
        read, write = os.pipe()
        os.close(read)  # nobody will ever read it: the next write is EPIPE
        try:
            done, waited = self.cli_wait("wait", "file", str(self.dir / "never-appears"),
                                         "--timeout", "1", "--json", stderr=write)
        finally:
            os.close(write)
        self.assertEqual(done.returncode, gg.EXIT_TIMEOUT, done.stdout)
        self.assertGreater(waited, 0.9, "it gave up early instead of waiting out the timeout")
        self.assertEqual(json.loads(done.stdout)["result"], "timeout")

    def test_a_closed_stderr_does_not_put_the_line_in_the_json(self) -> None:
        """`2>&-` makes `sys.stderr` None, and `print(file=None)` is documented as printing to
        **stdout** -- so the line became the first line of `--json` output and broke every consumer
        of it, which is the one thing the docstring promised could not happen. fd 2 is closed here
        the way a shell closes it, and the line simply goes nowhere."""
        done, _ = self.cli_wait("wait", "file", str(self.dir / "never-appears"),
                                "--timeout", "0.1", "--json",
                                shell_prefix=("/bin/sh", "-c", 'exec 2>&-; exec "$@"', "sh"))
        self.assertEqual(done.returncode, gg.EXIT_TIMEOUT, done.stdout)
        self.assertNotIn("waiting on", done.stdout)
        self.assertEqual(json.loads(done.stdout)["result"], "timeout")

    def test_the_line_reaches_fd_2_when_nothing_is_wrong(self) -> None:
        """The other half: the two tests above would also pass if the line were never printed at
        all. Through the real CLI, with stderr a file, which is how a caller captures it."""
        err = self.dir / "err.txt"
        with err.open("w") as fh:
            done, _ = self.cli_wait("wait", "file", str(self.dir / "never-appears"),
                                    "--timeout", "0.1", "--json", stderr=fh)
        self.assertEqual(done.returncode, gg.EXIT_TIMEOUT)
        self.assertIn("waiting on", err.read_text())
        self.assertEqual(json.loads(done.stdout)["result"], "timeout")

    def test_the_line_says_what_each_kind_is_waiting_for_and_until_when(self) -> None:
        """`waiting_line`'s content, which nothing pinned: a mutation hardcoding `until` to
        "green" left the suite green, so a `pid` wait could have reported "until green". And the
        sha clause -- the only branching in the function -- had no coverage at all.

        The deadline is a wall-clock time as well as a duration because the duration alone cannot
        be compared with anything: a reader looking at a line in a file does not know when the
        process started."""
        at = 1792587600.0  # 2026-10-21T13:00:00Z: the deadline is UTC with a Z (agent output contract)
        self.assertEqual(gg.deadline_clock(90, at), "13:01:30Z")
        for timeout in (float("inf"), float("nan"), 1e300):
            self.assertEqual(gg.deadline_clock(timeout, at), "unknown", timeout)
        full, short = "a" * 40, "abc1234"
        for want, line in (
                ("until green", gg.waiting_line("pr:o/r#1", "green", None, True, 60)),
                ("until it exits", gg.waiting_line("pid 7", "it exits", None, True, 60)),
                ("until nonempty", gg.waiting_line("/tmp/x", "nonempty", None, True, 60)),
                ("until a comment by bot", gg.waiting_line("comment:o/r#1", "a comment by bot",
                                                           None, True, 60)),
                (f"at {full},", gg.waiting_line("pr:o/r#1", "green", full, True, 60)),
                (f"at {short}... (a prefix", gg.waiting_line("pr:o/r#1", "green", short, False, 60))):
            with self.subTest(want=want):
                self.assertIn(want, line)
                # Every kind carries the deadline, not just the helper that formats it: a mutation
                # dropping the `(at ...)` clause from the line left the suite green while
                # `deadline_clock`'s own assertions above still passed.
                self.assertRegex(line, r"giving up after 60s \(at \d\d:\d\d:\d\dZ\)")
        self.assertNotIn(" at ", gg.waiting_line("pid 7", "it exits", None, True, 60).split(" (at ")[0],
                         "a wait with no sha claimed one")

    def test_the_line_is_printed_before_the_wait_can_finish(self) -> None:
        """Before, not after: a line printed on the way out would say nothing about a wait that
        was killed while blocked, which is the whole case it exists for."""
        order: list[str] = []
        target = self.dir / "appears"
        self.later(0.05, lambda: (order.append("ready"), target.write_text("x")))
        with mock.patch.object(gg, "say_waiting", lambda line: order.append("line")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gg.cmd_wait_file(str(target), 2.0, True), 0)
        self.assertEqual(order[:1], ["line"], order)

    def test_the_line_is_on_fd_2_while_the_wait_is_still_blocked(self) -> None:
        """The claim the whole PR rests on, measured where it is made: fd 2 of the real CLI, read
        while the process is still waiting. Asserting after the process exits cannot see the
        difference -- a mutation deferring the write to `atexit` put the line in the file at exit
        and left the capture empty for the whole wait, which is the ambiguity this exists to end,
        and it passed every other test here."""
        err = self.dir / "while-blocked.txt"
        with err.open("w") as fh:
            child = subprocess.Popen([sys.executable, os.fspath(ROOT / "scripts" / "glaeda-gh"),
                                      "wait", "file", str(self.dir / "never-appears"),
                                      "--timeout", "4", "--json"],
                                     stdout=subprocess.PIPE, stderr=fh, stdin=subprocess.DEVNULL,
                                     text=True, env={**os.environ,
                                                     "GLAEDA_GH_DIR": os.fspath(self.dir / "ghdir")})
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "waiting on" not in err.read_text():
            time.sleep(0.05)
        self.assertIn("waiting on", err.read_text(), "nothing on fd 2 while it was still blocked")
        self.assertIsNone(child.poll(), "it was supposed to still be waiting")

    def test_a_full_stderr_pipe_does_not_hang_the_wait(self) -> None:
        """Errors are not the only way a write can be the problem. With fd 2 a blocking pipe that
        is already full and a reader that is alive but not reading, `os.write` blocks forever: a
        1 s wait hung past 5 s, measured, which is worse for the caller than the exit 120 that
        started all this. So fd 2 is asked whether it can take the line first."""
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        try:
            os.set_blocking(write, False)
            with contextlib.suppress(BlockingIOError):
                while True:
                    os.write(write, b"x" * 65536)
            os.set_blocking(write, True)  # full, blocking, and nobody reads it
            done, waited = self.cli_wait("wait", "file", str(self.dir / "never-appears"),
                                         "--timeout", "1", "--json", stderr=write)
        finally:
            os.close(write)
        self.assertEqual(done.returncode, gg.EXIT_TIMEOUT, done.stdout)
        self.assertLess(waited, 4, "the diagnostic blocked instead of being dropped")
        self.assertEqual(json.loads(done.stdout)["result"], "timeout")

    def test_a_long_line_on_a_nearly_full_pipe_does_not_hang_the_wait(self) -> None:
        """Writable promises room for PIPE_BUF bytes, not for the whole line. A pipe with 8 KiB free
        reports writable, and a 100 KB `--contains` line then blocked a 1 s wait past 3 s. The line
        write is given `DIAGNOSTIC_WRITE_S` and then left behind."""
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        try:
            os.set_blocking(write, False)
            with contextlib.suppress(BlockingIOError):
                while True:
                    os.write(write, b"x" * 65536)
            os.set_blocking(write, True)
            os.read(read, 8192)  # some room, far less than the line
            done, waited = self.cli_wait("wait", "file", str(self.dir / "never-appears"), "--contains",
                                         "y" * 100_000, "--timeout", "1", "--json", stderr=write)
            os.set_blocking(read, False)
            spill = b""
            with contextlib.suppress(BlockingIOError):
                while True:
                    spill += os.read(read, 65536)
        finally:
            os.close(write)
        self.assertEqual(done.returncode, gg.EXIT_TIMEOUT, done.stdout)
        self.assertLess(waited, 4, "the diagnostic blocked on a pipe that had room for part of it")
        line = spill.lstrip(b"x")
        self.assertTrue(line.startswith(b"glaeda-gh: waiting on"), line[:80])

    def test_a_full_stderr_socket_does_not_hang_the_wait(self) -> None:
        """A socket reports writable without room for a whole write: 512-byte writes blocked a
        0.1 s wait past 3 s on a full one. The write is given `DIAGNOSTIC_WRITE_S` and then left behind."""
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        try:
            theirs.setblocking(False)
            with contextlib.suppress(BlockingIOError):
                while True:
                    theirs.send(b"x" * 65536)
            theirs.setblocking(True)  # full, blocking, and nobody reads it
            done, waited = self.cli_wait("wait", "file", str(self.dir / "never-appears"),
                                         "--timeout", "1", "--json", stderr=theirs.fileno())
        finally:
            theirs.close()
        self.assertEqual(done.returncode, gg.EXIT_TIMEOUT, done.stdout)
        self.assertLess(waited, 4, "the diagnostic blocked on a full socket")
        self.assertEqual(json.loads(done.stdout)["result"], "timeout")

    def test_the_diagnostic_never_changes_the_shared_stderr_flags(self) -> None:
        """O_NONBLOCK belongs to the file description the parent shares with every child, so two
        overlapping waits that each set it around their write and restored what they had saved
        left the parent's stderr non-blocking for good (reproduced with two real children). The
        race is microseconds wide, so this checks the cause: fd 2 is still blocking at every
        write, and its flags are what they were."""
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        saved = os.dup(2)
        self.addCleanup(os.close, saved)
        seen = []

        def record(fd, data):
            seen.append((fd, os.get_blocking(fd)))
            return len(data)

        os.dup2(write, 2)
        try:
            before = fcntl.fcntl(2, fcntl.F_GETFL)
            with mock.patch.object(gg.os, "write", record):
                gg.say_waiting("glaeda-gh: waiting on something")
            after = fcntl.fcntl(2, fcntl.F_GETFL)
        finally:
            os.dup2(saved, 2)
        self.assertEqual(seen, [(2, True)])
        self.assertEqual(after, before)

    def test_a_filename_that_is_not_utf8_does_not_kill_the_wait(self) -> None:
        """A surrogate-escaped filename byte raised `UnicodeEncodeError` in the diagnostic: exit 1 and
        no JSON, where main timed out with exit 2. The line is encoded with backslashreplace."""
        name = os.fsdecode(b"never-\xff")
        done, _ = self.cli_wait("wait", "file", str(self.dir / name), "--timeout", "1", "--json",
                                stderr=subprocess.PIPE)
        self.assertEqual(done.returncode, gg.EXIT_TIMEOUT, done.stderr)
        self.assertEqual(json.loads(done.stdout)["result"], "timeout")
        self.assertIn("never-\\udcff", done.stderr)

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


class BudgetRefreshTest(Base):
    """The quota is one pool per account, so every host needs a view of it that is not old. The
    daemon used to learn it only from its own responses' headers, which left an idle host working
    from a figure days out of date. GET /rate_limit is free, so a stale view is refreshed from it."""

    def setUp(self) -> None:
        super().setUp()
        gg.BUDGET_REFRESH = 120.0

    def test_an_idle_tick_refreshes_a_view_it_never_had(self) -> None:
        self.gh.rest_remaining, self.gh.graphql_remaining = 1057, 67
        self.daemon.tick(NOW)
        self.assertEqual(self.gh.rate_limit_calls, 1)
        self.assertEqual(self.daemon.budget["rest"]["remaining"], 1057)
        self.assertEqual(self.daemon.budget["graphql"]["remaining"], 67)
        self.assertEqual(self.daemon.budget["rest"]["seenAt"], NOW)

    def test_a_recent_view_is_not_read_again(self) -> None:
        self.daemon.tick(NOW)
        self.daemon.tick(NOW + gg.BUDGET_REFRESH - 1)
        self.assertEqual(self.gh.rate_limit_calls, 1)
        self.daemon.tick(NOW + gg.BUDGET_REFRESH + 1)
        self.assertEqual(self.gh.rate_limit_calls, 2)

    def test_a_tick_whose_views_are_both_recent_pays_nothing(self) -> None:
        """A busy daemon's own responses carry x-ratelimit headers, so there is nothing to refresh."""
        self.gh.prs[("o", "r", 7)] = pr_node(7)
        gg.register("pr:o/r#7")
        self.daemon.tick(NOW)  # the first tick has no view of either bucket yet
        self.assertEqual(self.gh.rate_limit_calls, 1)
        self.daemon.tick(NOW + gg.BUDGET_REFRESH / 2)
        self.assertEqual(self.gh.rate_limit_calls, 1)
        self.assertEqual(self.daemon.budget["graphql"]["seenAt"], NOW + gg.BUDGET_REFRESH / 2)

    def test_the_bucket_this_host_is_not_using_is_kept_current_for_nothing(self) -> None:
        """The point of the refresh: a daemon reading only GraphQL still has to know what REST has
        left, because another machine on the same account is spending it."""
        self.gh.prs[("o", "r", 7)] = pr_node(7)
        gg.register("pr:o/r#7")
        self.daemon.tick(NOW)
        self.gh.rest_remaining = 1057
        self.daemon.tick(NOW + 10 * gg.BUDGET_REFRESH)
        self.assertEqual(self.daemon.budget["rest"], {"limit": 5000, "remaining": 1057,
                                                      "reset": int(NOW + 1800),
                                                      "seenAt": NOW + 10 * gg.BUDGET_REFRESH})
        self.assertEqual(self.daemon.status["lastHour"]["rest"], 0, "the refresh spends no REST")

    def test_the_free_read_is_not_counted_as_a_request_the_daemon_spent(self) -> None:
        status = self.daemon.tick(NOW)
        self.assertEqual(self.gh.rate_limit_calls, 1)
        self.assertEqual(status["lastHour"], {"rest": 0, "rest304": 0, "graphql": 0, "free": 1, "write": 0},
                         "the refresh is counted, and not as a request that spent anything")

    def test_a_refusal_is_reported_and_leaves_the_last_view_alone(self) -> None:
        self.daemon.tick(NOW)
        kept = dict(self.daemon.budget["rest"])

        def fail(*_args: Any, **_kwargs: Any) -> tuple[int, dict, bytes]:
            return 403, {}, b'{"message": "rate limited"}'

        self.daemon.transport = fail
        status = self.daemon.tick(NOW + 10 * gg.BUDGET_REFRESH)
        self.assertEqual(self.daemon.budget["rest"], kept)
        self.assertTrue(any("rate_limit: HTTP 403" in e for e in status["errors"]))


class RestCountersTest(Base):
    """One token's REST calls are answered from two "core" counters with their own windows (2026-10-30:
    /actions/runs/N reported 3068 left until :44 while /rate_limit and pulls reported 2737 until :10, both
    "core"). The reading must be the poorest live counter, not whichever answered last: the last-answer
    reading jumped from 3126 to 0 in five minutes and was chased as a 3000-call burst nobody logged."""

    def test_the_poorer_counter_is_the_reading_whichever_answered_last(self) -> None:
        self.daemon.now = NOW
        self.daemon.note_budget("rest", 5000, 0, NOW + 600)      # /rate_limit's counter: spent
        self.daemon.note_budget("rest", 5000, 3126, NOW + 2400)  # a run read's counter, answered later
        self.assertEqual(self.daemon.budget["rest"]["remaining"], 0)
        self.assertEqual(self.daemon.budget["rest"]["reset"], NOW + 600)
        self.assertEqual(self.daemon.mode("rest"), "stop")
        self.assertEqual([c["remaining"] for c in self.daemon.budget["restCounters"]], [0, 3126])

    def test_an_actions_read_tags_its_counter_and_the_tag_survives_other_readings(self) -> None:
        """2026-11-04 06:15-06:20Z: runs/N, attempts, artifacts and workflows always answered from one counter;
        pulls and /rate_limit mostly from the other. Only an Actions read names its counter."""
        self.daemon.now = NOW
        self.daemon.note_budget("rest", 5000, 0, NOW + 600, "/repos/o/r/pulls?per_page=1")
        self.daemon.note_budget("rest", 5000, 4911, NOW + 2400, "/repos/o/r/actions/runs/5?exclude_pull_requests=true")
        self.daemon.note_budget("rest", 5000, 4900, NOW + 2400, "/repos/o/r/pulls/3")  # a pull answered from it too
        self.daemon.note_budget("rest", 5000, 4890, NOW + 2401, "/repos/o/r/actions/workflows/7/dispatches")
        tags = {c["reset"]: c.get("counters") for c in self.daemon.budget["restCounters"]}
        self.assertEqual(tags, {NOW + 600: None, NOW + 2401: ["actions"]})
        self.assertEqual(self.daemon.budget["rest"]["remaining"], 0)  # the poorest is still the reading
        for path in ("/repos/o/r/actions/runs?per_page=1", "/repos/o/r/actions/runs/5/jobs", "/rate_limit",
                     "/repos/o/r/actions/runs/5/logs"):
            self.assertIsNone(gg.ACTIONS_COUNTER_PATH.fullmatch(path), path)
        for path in ("/repos/o/r/actions/workflows", "/repos/o/r/actions/workflows/ci.yml/runs?per_page=1",
                     "/repos/o/r/actions/runs/5/attempts/2", "/repos/o/r/actions/runs/5/artifacts?per_page=100"):
            self.assertIsNotNone(gg.ACTIONS_COUNTER_PATH.fullmatch(path), path)

    def test_one_counter_answering_again_is_updated_not_duplicated(self) -> None:
        self.daemon.now = NOW
        self.daemon.note_budget("rest", 5000, 3000, NOW + 600)
        self.daemon.note_budget("rest", 5000, 2900, NOW + 600)
        self.assertEqual(len(self.daemon.budget["restCounters"]), 1)
        self.assertEqual(self.daemon.budget["rest"]["remaining"], 2900)

    def test_a_counter_past_its_reset_leaves_the_reading_to_the_other(self) -> None:
        self.daemon.now = NOW
        self.daemon.note_budget("rest", 5000, 10, NOW + 60)
        self.daemon.note_budget("rest", 5000, 2500, NOW + 2400)
        self.assertEqual(self.daemon.budget["rest"]["remaining"], 10)
        self.daemon.now = NOW + 61
        self.daemon.settle_rest()
        self.assertEqual(self.daemon.budget["rest"]["remaining"], 2500)
        self.assertEqual(len(self.daemon.budget["restCounters"]), 1)

    def test_budget_json_carries_the_poorest_counter_for_every_reader(self) -> None:
        """gh-quota (leo-workspace tools/gh_quota.py) and the gh broker read budget.json's "rest" only."""
        gg.BUDGET_REFRESH = 120.0
        self.gh.rest_remaining = 4000  # /rate_limit's counter, reset NOW + 1800 in the fake
        self.daemon.now = NOW
        self.daemon.note_budget("rest", 5000, 120, NOW + 3000)  # the other counter, nearly spent
        self.daemon.tick(NOW)
        doc = gg.read_json(gg.BASE / "budget.json")
        self.assertEqual(doc["rest"]["remaining"], 120)
        self.assertEqual(sorted(c["remaining"] for c in doc["restCounters"]), [120, 4000])

    def test_a_stale_counter_rate_limit_cannot_see_does_not_force_a_refresh_every_tick(self) -> None:
        gg.BUDGET_REFRESH = 120.0
        self.daemon.now = NOW - 10 * gg.BUDGET_REFRESH
        self.daemon.note_budget("rest", 5000, 50, NOW + 3000)  # seen long ago from a run read's headers
        self.daemon.tick(NOW)
        self.assertEqual(self.gh.rate_limit_calls, 1)
        self.daemon.tick(NOW + gg.BUDGET_REFRESH / 2)
        self.assertEqual(self.gh.rate_limit_calls, 1, "the /rate_limit counter is fresh; the other one cannot be refreshed")
        self.assertEqual(self.daemon.budget["rest"]["remaining"], 50)

    def test_a_reading_without_a_reset_still_counts(self) -> None:
        self.daemon.now = NOW
        self.daemon.note_budget("rest", 5000, 77, None)
        self.assertEqual(self.daemon.budget["rest"]["remaining"], 77)

    def test_a_reading_without_a_reset_does_not_replace_a_known_counter(self) -> None:
        self.daemon.now = NOW
        self.daemon.note_budget("rest", 5000, 40, NOW + 600)
        self.daemon.note_budget("rest", 5000, 4000, None)
        self.assertEqual(self.daemon.budget["rest"]["remaining"], 40)
        self.assertEqual(self.daemon.mode("rest"), "stop")

    def test_a_reading_without_a_reset_still_drops_an_expired_counter(self) -> None:
        self.daemon.now = NOW
        self.daemon.note_budget("rest", 5000, 5, NOW + 60)     # expires first
        self.daemon.note_budget("rest", 5000, 100, NOW + 2400)  # live, below the reserve
        self.daemon.now = NOW + 61
        self.daemon.note_budget("rest", 5000, 4000, None)
        self.assertEqual(self.daemon.budget["rest"]["remaining"], 100)
        self.assertEqual(self.daemon.mode("rest"), "stop")

    def test_a_late_higher_reading_of_a_counter_does_not_raise_it(self) -> None:
        """Within one window a counter only goes down: a delayed answer must not lift what a later one saw."""
        self.daemon.now = NOW
        self.daemon.note_budget("rest", 5000, 100, NOW + 2400)  # counter A
        self.daemon.note_budget("rest", 5000, 50, NOW + 600)    # counter B, the poorer
        self.daemon.note_budget("rest", 5000, 200, NOW + 2400)  # a delayed answer from A
        self.daemon.now = NOW + 601
        self.daemon.settle_rest()  # B's window ended
        self.assertEqual(self.daemon.budget["rest"]["remaining"], 100)

    def test_a_reset_one_second_off_is_the_same_counter(self) -> None:
        self.daemon.now = NOW
        self.daemon.note_budget("rest", 5000, 300, NOW + 600)
        self.daemon.note_budget("rest", 5000, 290, NOW + 601)
        self.assertEqual(len(self.daemon.budget["restCounters"]), 1)
        self.assertEqual(self.daemon.budget["rest"]["remaining"], 290)
        self.assertEqual(self.daemon.budget["rest"]["reset"], NOW + 601)

    def test_a_fresh_hidden_counter_does_not_keep_the_visible_one_from_refreshing(self) -> None:
        gg.BUDGET_REFRESH = 120.0
        self.daemon.tick(NOW)  # /rate_limit: its counter, reset NOW + 1800 in the fake
        self.assertEqual(self.gh.rate_limit_calls, 1)
        later = NOW + 10 * gg.BUDGET_REFRESH
        self.daemon.now = later
        self.daemon.note_budget("rest", 5000, 3000, NOW + 3000)  # a run read's counter, just seen
        self.daemon.note_budget("graphql", 5000, 4000, NOW + 3000)
        self.gh.rest_remaining = 900
        self.daemon.tick(later)
        self.assertEqual(self.gh.rate_limit_calls, 2, "the /rate_limit counter was stale, so it is read again")
        self.assertEqual(self.daemon.budget["rest"]["remaining"], 900)


class BrokerBase(Base):
    """`glaeda-gh gh ARG...` in front of one real gh call."""

    def setUp(self) -> None:
        super().setUp()
        self.home = Path(self.tmp.name) / "home"
        (self.home / "roots").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {
            "GLAEDA_GH_REPO_ROOTS": str(self.home / "roots"),
            "CMUX_GH_LOG": str(self.home / "calls.log"),
            "GLAEDA_GH_REAL": str(self.fake_gh()),
            # gh's own hosts.yml decides its default host, so the broker reads it (see
            # gh_default_host). Pointed at an empty directory here: these tests are about
            # github.com, and none of them should depend on what this machine is logged in to.
            "GH_CONFIG_DIR": str(self.home / "gh-config"),
            "GLAEDA_GH_LANE_CHAIN": str(self.home / ".local/lib/lane-gh/bin/gh"),  # never the host's own chain
            # The broker asks git what a checkout's remotes are (git_remotes), and git composes
            # every config scope -- so without these the answers would depend on whoever's
            # `~/.gitconfig` the suite happens to run under, and an `insteadOf` there would change
            # what these tests measure. Pointed at an empty file, which is also what lets a test
            # write a global scope of its own and mean it.
            "GIT_CONFIG_GLOBAL": str(self.home / "gitconfig"),
            "GIT_CONFIG_SYSTEM": str(self.home / "gitconfig-system"),
            "GIT_CONFIG_NOSYSTEM": "1",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ("GLAEDA_GH_ALLOW_API_SOURCE", "GLAEDA_GH_MAX_AGE", "CMUX_GH_GUARD_LOGGED"):
            os.environ.pop(name, None)
        # The repeat delay is real time, and most of these tests make the same call twice on
        # purpose. BrokerRepeatTest turns it back on and replaces the hold.
        os.environ["CMUX_GH_MIN_REPEAT_SECONDS"] = "0"
        # The answer cache runs gh as a child instead of exec'ing it, and these tests read what the
        # exec was handed. BrokerAnswerCacheTest turns it back on.
        os.environ["GLAEDA_GH_CACHE_TTL"] = "0"
        for name in ("GLAEDA_GH_CACHE_DISABLE", "GH_TOKEN", "GITHUB_TOKEN", "GH_REPO", "GH_HOST"):
            os.environ.pop(name, None)
        # As on a host, where gh reaches this broker through the gh-quota guard, which exports this once it has
        # judged the call. BrokerMergeGuardTest takes it away: a merge typed as `glaeda-gh gh ...` goes to the guard.
        os.environ["GH_QUOTA_CHECKED"] = "1"
        os.environ.pop("GLAEDA_MERGE_GUARD", None)

    def fake_gh(self) -> Path:
        """A stand-in for the real gh that records the arguments it was handed."""
        path = self.home / "bin" / "gh"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.passed = self.home / "passed.txt"
        path.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" >> " + str(self.passed) + "\n")
        path.chmod(0o755)
        return path

    def checkout(self, owner_repo: str, name: str | None = None) -> Path:
        """A checkout of `owner_repo` under the configured root, as the refusal looks for."""
        return self.git_checkout(self.home / "roots" / (name or owner_repo.split("/")[1]),
                                 f'[remote "origin"]\n\turl = git@github.com:{owner_repo}.git\n')

    def git_checkout(self, where: Path, config: str) -> Path:
        """A real git repository at `where` whose config is exactly `config`.

        Real, because the broker asks git what the remotes are rather than reading the file
        (git_remotes), and a handcrafted `.git` directory is not a repository git answers about.
        The config is written afterwards rather than through `git remote add` so that a test can
        still pin an exact config *form* -- a dotted section, a BOM, a continuation -- and have
        real git read it, which is the only way to know what gh would see."""
        where.mkdir(parents=True, exist_ok=True)
        if not (where / ".git").is_dir():  # a probe directory a subtest before this one set up
            done = subprocess.run(["git", "init", "-q", str(where)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, f"git init failed: {done.stderr}")
        (where / ".git" / "config").write_text(config)
        return where

    def repo(self, local: str, global_config: str = "") -> str | None:
        """What broker_repo resolves for a real checkout with this local config and this global
        scope. The global scope is a file git really reads: GIT_CONFIG_GLOBAL, set in setUp."""
        (self.home / "gitconfig").write_text(global_config)
        where = self.git_checkout(self.home / "roots" / "probe", local)
        with mock.patch.object(gg.Path, "cwd", staticmethod(lambda: where)):
            return gg.broker_repo({})

    def cache_pr(self, number: int, data: dict, age: float = 0.0) -> str:
        key = f"pr:o/r#{number}"
        gg.write_json(gg.cache_dir() / gg.file_name(key),
                      {"key": key, "fetchedAt": time.time() - age, "dataAt": time.time() - age,
                       "data": data, "error": None})
        return key

    def run_gh(self, args: list[str], keep_mark: bool = False) -> tuple[int, str, str, list[str] | None]:
        """(exit code, stdout, stderr, the arguments the real gh got, or None when it was not run)."""
        out, err, execed = io.StringIO(), io.StringIO(), []
        # Each call is its own process in life; BROKER_MARK is set to survive the exec, so a test
        # that calls cmd_gh twice in one process has to drop it the way a fresh shell would.
        if not keep_mark:
            os.environ.pop(gg.BROKER_MARK, None)
            os.environ.pop(gg.BROKER_DEPTH, None)

        def execv(path: str, argv: list[str]) -> None:
            execed.append((path, argv))
            raise SystemExit(0)

        with mock.patch.object(os, "execv", execv), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                code = gg.cmd_gh(args)
            except SystemExit:
                code = None
        return code, out.getvalue(), err.getvalue(), (execed[0][1][1:] if execed else None)


class BrokerRouteTest(BrokerBase):
    def test_a_fresh_cache_entry_answers_pr_view_byte_for_byte(self) -> None:
        self.cache_pr(7, {"number": 7, "state": "MERGED", "isDraft": False, "mergeable": "UNKNOWN",
                          "headRefOid": "a" * 40, "updatedAt": "2026-10-21T04:33:12Z"})
        code, out, _err, passed = self.run_gh(
            ["pr", "view", "7", "-R", "o/r", "--json", "headRefOid,isDraft,mergeable,number,state,updatedAt"])
        self.assertEqual(code, 0)
        self.assertIsNone(passed, "a served read never reaches GitHub")
        # gh prints compact JSON with its keys sorted and one trailing newline
        self.assertEqual(out, '{"headRefOid":"' + "a" * 40 + '","isDraft":false,"mergeable":"UNKNOWN",'
                              '"number":7,"state":"MERGED","updatedAt":"2026-10-21T04:33:12Z"}\n')

    def test_a_pr_url_carries_its_own_repository(self) -> None:
        self.cache_pr(7, {"state": "OPEN"})
        code, out, _err, passed = self.run_gh(["pr", "view", "https://github.com/o/r/pull/7", "--json", "state"])
        self.assertEqual((code, out, passed), (0, '{"state":"OPEN"}\n', None))

    def test_a_stale_entry_is_not_served(self) -> None:
        self.cache_pr(7, {"state": "OPEN"}, age=gg.BROKER_MAX_AGE + 5)
        _code, out, _err, passed = self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        self.assertEqual(out, "")
        self.assertEqual(passed, ["pr", "view", "7", "-R", "o/r", "--json", "state"])

    def test_a_field_the_daemon_does_not_hold_is_passed_through(self) -> None:
        self.cache_pr(7, {"state": "OPEN"})
        _code, out, _err, passed = self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state,body"])
        self.assertEqual(out, "")
        self.assertIsNotNone(passed)

    def test_a_field_missing_from_this_entry_is_passed_through(self) -> None:
        """Serving a null gh would have filled in is worse than spending one request."""
        self.cache_pr(7, {"state": "OPEN"})
        _code, _out, _err, passed = self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state,mergedAt"])
        self.assertIsNotNone(passed)

    def test_jq_and_template_are_passed_through(self) -> None:
        self.cache_pr(7, {"state": "OPEN"})
        for shape in (["-q", ".state"], ["--jq", ".state"], ["--template", "{{.state}}"]):
            with self.subTest(shape=shape):
                _code, out, _err, passed = self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state", *shape])
                self.assertEqual(out, "")
                self.assertIsNotNone(passed)

    def test_human_output_is_passed_through(self) -> None:
        self.cache_pr(7, {"state": "OPEN"})
        _code, out, _err, passed = self.run_gh(["pr", "view", "7", "-R", "o/r"])
        self.assertEqual(out, "")
        self.assertIsNotNone(passed)

    def test_a_run_is_served_from_the_rest_shape_the_daemon_stores(self) -> None:
        key = "run:o/r/12"
        gg.write_json(gg.cache_dir() / gg.file_name(key),
                      {"key": key, "fetchedAt": time.time(), "dataAt": time.time(), "error": None,
                       "data": {"id": 12, "name": "CI", "status": "completed", "conclusion": "success",
                                "head_sha": "b" * 40, "html_url": "https://x/12", "run_attempt": 2}})
        code, out, _err, passed = self.run_gh(
            ["run", "view", "12", "-R", "o/r", "--json", "attempt,conclusion,databaseId,headSha,status,url"])
        self.assertEqual((code, passed), (0, None))
        self.assertEqual(json.loads(out), {"attempt": 2, "conclusion": "success", "databaseId": 12,
                                           "headSha": "b" * 40, "status": "completed",
                                           "url": "https://x/12"})

    def test_a_mutation_is_never_touched(self) -> None:
        for args in (["pr", "merge", "7", "-R", "o/r", "--squash"],
                     ["pr", "comment", "7", "-R", "o/r", "--body", "hi"],
                     ["api", "-X", "POST", "repos/o/r/issues/7/comments", "-f", "body=hi"]):
            with self.subTest(args=args):
                _code, out, _err, passed = self.run_gh(args)
                self.assertEqual(out, "")
                self.assertEqual(passed, args)


class BrokerWrongAnswerTest(BrokerBase):
    """Every way the broker could answer a question other than the one that was asked. A spent
    request is cheap; a wrong answer sends a lane to merge the wrong thing."""

    def setUp(self) -> None:
        super().setUp()
        self.here = self.checkout("o/r", name="here")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.here)
        self.cache_pr(100, {"state": "MERGED"})

    def test_a_merge_and_a_ready_for_review_are_never_served_from_a_stale_entry(self) -> None:
        """mergedAt, isDraft and updatedAt are mutation clocks. A merge changes mergedAt, state and
        updatedAt; marking a PR ready changes isDraft. Only `state` was in the live table, so on
        the lean horizon -- exactly when every lane is hammering gh -- an entry from before the
        merge answered `{"mergedAt":null}` to a lane applying the one-week take-back rule, and
        `{"isDraft":true}` to one about to add ready-to-land. `updatedAt` is worse in kind: it is
        the change detector itself, told "nothing changed" for the whole horizon."""
        self.cache_pr(101, {"state": "OPEN", "mergedAt": None, "isDraft": True,
                            "updatedAt": "2026-10-21T00:00:00Z", "title": "a fix"}, age=300.0)
        gg.write_json(gg.BASE / "budget.json",  # under the floor: the lean horizon would cover 300 s
                      {"graphql": {"limit": 5000, "remaining": 50, "reset": time.time() + 600,
                                   "seenAt": time.time()}})
        for field in ("mergedAt", "isDraft", "updatedAt"):
            _code, out, _err, passed = self.run_gh(["pr", "view", "101", "--json", field])
            self.assertEqual(out, "", f"{field} changes the moment the PR does")
            self.assertIsNotNone(passed, field)
        # The same entry still answers something that does not move under the caller.
        code, out, _err, _passed = self.run_gh(["pr", "view", "101", "--json", "title"])
        self.assertEqual((code, out), (0, '{"title":"a fix"}\n'))

    def test_a_call_from_a_subdirectory_resolves_the_same_repository(self) -> None:
        """gh resolves a repository by walking up from the working directory, so this has to as
        well. Looking only at the cwd meant `gh pr view` from `repo/src` -- most of how agents run
        it -- resolved nothing, passed through, and spent a request on a cached answer."""
        deep = self.here / "src" / "inner"
        deep.mkdir(parents=True)
        os.chdir(deep)
        code, out, _err, passed = self.run_gh(["pr", "view", "100", "--json", "state"])
        self.assertEqual((code, out, passed), (0, '{"state":"MERGED"}\n', None))

    def test_a_value_flag_repeated_with_no_value_is_never_served(self) -> None:
        """`gh pr view 100 -R o/r --json state -R` makes gh exit 1 (`flag needs an argument`). The
        earlier occurrence's value must not stand in for the empty one, or the broker answers 0
        with JSON where gh answers 1 with an error."""
        for tail in (["-R"], ["--repo"], ["--json"]):
            args = ["pr", "view", "100", "-R", "o/r", "--json", "state", *tail]
            _code, out, _err, passed = self.run_gh(args)
            self.assertEqual(out, "", tail)
            self.assertEqual(passed, args, tail)

    def test_a_glued_repo_flag_is_not_read_as_this_checkouts_repo(self) -> None:
        """`gh pr view 100 -Rother/elsewhere` once served o/r's PR 100 as if it were theirs: the
        glued shorthand never reached `values`, so the repository fell back to the cwd's remote."""
        _code, out, _err, passed = self.run_gh(["pr", "view", "100", "-Rother/elsewhere", "--json", "state"])
        self.assertEqual(out, "")
        self.assertEqual(passed, ["pr", "view", "100", "-Rother/elsewhere", "--json", "state"])

    def test_gh_repo_in_the_environment_is_honoured(self) -> None:
        with mock.patch.dict(os.environ, {"GH_REPO": "other/elsewhere"}):
            _code, out, _err, passed = self.run_gh(["pr", "view", "100", "--json", "state"])
        self.assertEqual(out, "")
        self.assertIsNotNone(passed)

    def test_the_cwd_answers_only_when_it_names_one_github_remote(self) -> None:
        code, out, _err, _passed = self.run_gh(["pr", "view", "100", "--json", "state"])
        self.assertEqual((code, out), (0, '{"state":"MERGED"}\n'))
        (self.here / ".git" / "config").write_text(
            '[remote "origin"]\n\turl = git@github.com:o/r.git\n'
            '[remote "fork"]\n\turl = git@github.com:someone/r.git\n')
        _code, out, _err, passed = self.run_gh(["pr", "view", "100", "--json", "state"])
        self.assertEqual(out, "", "two remotes: gh's choice is not this one's to guess")
        self.assertIsNotNone(passed)

    def test_a_default_set_with_gh_repo_set_default_is_not_second_guessed(self) -> None:
        (self.here / ".git" / "config").write_text(
            '[remote "origin"]\n\turl = git@github.com:o/r.git\n\tgh-resolved = base\n')
        _code, out, _err, passed = self.run_gh(["pr", "view", "100", "--json", "state"])
        self.assertEqual(out, "")
        self.assertIsNotNone(passed)

    def test_a_flag_the_broker_does_not_know_is_never_answered(self) -> None:
        """`run view ID --attempt 2` wants an attempt the cache does not hold, `--job ID` reads a
        job as a run, and `--exit-status` makes gh exit 1 on a failed run. None can be served, and
        neither can the next flag gh adds."""
        key = "run:o/r/12"
        gg.write_json(gg.cache_dir() / gg.file_name(key),
                      {"key": key, "fetchedAt": time.time(), "dataAt": time.time(), "error": None,
                       "data": {"id": 12, "status": "completed", "conclusion": "failure"}})
        served = ["run", "view", "12", "-R", "o/r", "--json", "conclusion"]
        code, out, _err, _passed = self.run_gh(served)
        self.assertEqual((code, out), (0, '{"conclusion":"failure"}\n'), "the plain form is served")
        for extra in (["--attempt", "2"], ["--job", "99"], ["--exit-status"], ["--some-flag-gh-adds-later"]):
            with self.subTest(extra=extra):
                _code, out, _err, passed = self.run_gh([*served, *extra])
                self.assertEqual(out, "")
                self.assertIsNotNone(passed)

    def test_a_null_string_field_is_printed_as_gh_prints_it(self) -> None:
        """gh unmarshals into Go structs: a null string comes out "" and a null time stays null.
        Checked against real gh on 2026-10-21: mergedAt null on an open PR, conclusion "" on a run
        still going."""
        self.cache_pr(101, {"state": "OPEN", "headRefName": None, "mergedAt": None})
        code, out, _err, _passed = self.run_gh(
            ["pr", "view", "101", "-R", "o/r", "--json", "headRefName,mergedAt,state"])
        self.assertEqual((code, out), (0, '{"headRefName":"","mergedAt":null,"state":"OPEN"}\n'))
        key = "run:o/r/13"
        gg.write_json(gg.cache_dir() / gg.file_name(key),
                      {"key": key, "fetchedAt": time.time(), "dataAt": time.time(), "error": None,
                       "data": {"id": 13, "status": "in_progress", "conclusion": None}})
        code, out, _err, _passed = self.run_gh(["run", "view", "13", "-R", "o/r", "--json", "conclusion,status"])
        self.assertEqual((code, out), (0, '{"conclusion":"","status":"in_progress"}\n'))

    def test_a_null_where_a_number_belongs_is_not_guessed_at(self) -> None:
        key = "run:o/r/14"
        gg.write_json(gg.cache_dir() / gg.file_name(key),
                      {"key": key, "fetchedAt": time.time(), "dataAt": time.time(), "error": None,
                       "data": {"id": 14, "run_attempt": None}})
        _code, out, _err, passed = self.run_gh(["run", "view", "14", "-R", "o/r", "--json", "attempt"])
        self.assertEqual(out, "")
        self.assertIsNotNone(passed)

    def test_a_runs_name_is_never_served(self) -> None:
        """REST's `name` is the run-name when a workflow sets one, GraphQL's is the workflow name,
        and the daemon moves runs between the two by budget: neither could be answered twice the
        same way, so both gh fields pass through."""
        key = "run:o/r/15"
        gg.write_json(gg.cache_dir() / gg.file_name(key),
                      {"key": key, "fetchedAt": time.time(), "dataAt": time.time(), "error": None,
                       "data": {"id": 15, "name": "CI"}})
        for field in ("name", "workflowName"):
            with self.subTest(field=field):
                _code, out, _err, passed = self.run_gh(["run", "view", "15", "-R", "o/r", "--json", field])
                self.assertEqual(out, "")
                self.assertIsNotNone(passed)

    def test_a_field_only_some_sources_carry_is_never_served(self) -> None:
        """The cache has one shape and three writers, and two of them leave a key None to mean
        "this source does not carry it". Serving that as "GitHub said null" prints a confident
        wrong answer: `reviewDecision` is None after any REST read (REST has no equivalent, so
        fetch_pr_rest keeps GraphQL's last value), and "" there reads as "nobody has reviewed
        this" for a PR that is CHANGES_REQUESTED. controller_run has no display_title or
        created_at in the webhook record, and an empty head_branch when the delivery omits it."""
        self.cache_pr(103, {"state": "OPEN", "reviewDecision": None})
        _code, out, _err, passed = self.run_gh(
            ["pr", "view", "103", "-R", "o/r", "--json", "reviewDecision,state"])
        self.assertEqual(out, "", "a lane would read this as nothing blocking the merge")
        self.assertIsNotNone(passed)
        key = "run:o/r/16"
        gg.write_json(gg.cache_dir() / gg.file_name(key),
                      {"key": key, "fetchedAt": time.time(), "dataAt": time.time(), "error": None,
                       "data": {"id": 16, "display_title": None, "created_at": None, "head_branch": ""}})
        for field in ("displayTitle", "createdAt", "headBranch"):
            with self.subTest(field=field):
                _code, out, _err, passed = self.run_gh(["run", "view", "16", "-R", "o/r", "--json", field])
                self.assertEqual(out, "")
                self.assertIsNotNone(passed)

    def test_a_run_url_is_not_a_run_id(self) -> None:
        """`gh run view` takes a run ID; a URL there is a call gh rejects, so answering it would
        make the broker the only thing on the host that accepted it."""
        key = "run:o/r/17"
        gg.write_json(gg.cache_dir() / gg.file_name(key),
                      {"key": key, "fetchedAt": time.time(), "dataAt": time.time(), "error": None,
                       "data": {"id": 17, "status": "completed"}})
        _code, out, _err, passed = self.run_gh(
            ["run", "view", "https://github.com/o/r/actions/runs/17", "--json", "status"])
        self.assertEqual(out, "")
        self.assertIsNotNone(passed)

    def test_another_github_host_is_never_answered_from_this_cache(self) -> None:
        """gh takes `[HOST/]OWNER/REPO` and reads $GH_HOST. An enterprise host has its own PR 7,
        and this cache only ever holds github.com's."""
        self.cache_pr(7, {"state": "OPEN"})
        for env, args in (({}, ["--repo", "ghe.corp.example/o/r"]),
                          ({"GH_HOST": "ghe.corp.example"}, ["-R", "o/r"]),
                          ({"GH_ENTERPRISE_TOKEN": "x"}, ["-R", "o/r"])):
            with self.subTest(env=env, args=args):
                with mock.patch.dict(os.environ, env):
                    _code, out, _err, passed = self.run_gh(["pr", "view", "7", *args, "--json", "state"])
                self.assertEqual(out, "")
                self.assertIsNotNone(passed)
        code, out, _err, _passed = self.run_gh(["pr", "view", "7", "--repo", "github.com/o/r", "--json", "state"])
        self.assertEqual((code, out), (0, '{"state":"OPEN"}\n'), "github.com spelled out is still us")

    def test_two_json_flags_union_in_gh_so_they_are_not_served(self) -> None:
        _code, out, _err, passed = self.run_gh(
            ["pr", "view", "100", "-R", "o/r", "--json", "state", "--json", "url"])
        self.assertEqual(out, "")
        self.assertIsNotNone(passed)

    def test_a_live_field_never_gets_the_lean_horizon(self) -> None:
        """A ten-minute-old headRefOid means waiting on a head that was superseded, and a spent
        quota is exactly when a lane is hammering gh to find out whether its push is green."""
        gg.write_json(gg.BASE / "budget.json",
                      {"graphql": {"limit": 5000, "remaining": 50, "reset": time.time() + 600,
                                   "seenAt": time.time()}})
        self.cache_pr(102, {"state": "OPEN", "title": "a fix", "headRefOid": "a" * 40},
                      age=gg.BROKER_MAX_AGE + 30)
        for field in ("headRefOid", "state"):
            with self.subTest(field=field):
                _code, out, _err, passed = self.run_gh(["pr", "view", "102", "-R", "o/r", "--json", field])
                self.assertEqual(out, "", f"{field} is what a lane pivots on")
                self.assertIsNotNone(passed)
        code, out, _err, _passed = self.run_gh(["pr", "view", "102", "-R", "o/r", "--json", "title"])
        self.assertEqual((code, out), (0, '{"title":"a fix"}\n'), "a title is slow enough to widen")


class BrokerRefusalTest(BrokerBase):
    def test_reading_source_through_the_api_is_refused_when_the_repo_is_here(self) -> None:
        where = self.checkout("o/r")
        for path in ("repos/o/r/contents/.github/workflows/ci.yml",
                     "repos/o/r/git/blobs/" + "c" * 40,
                     "repos/o/r/git/trees/main?recursive=1",
                     "https://raw.githubusercontent.com/o/r/main/README.md"):
            with self.subTest(path=path):
                code, out, err, passed = self.run_gh(["api", path])
                self.assertEqual(code, gg.BROKER_EXIT_REFUSED)
                self.assertEqual((out, passed), ("", None))
                self.assertIn(str(where), err)
                self.assertIn("git -C", err)

    def test_a_repo_that_is_not_checked_out_here_is_passed_through(self) -> None:
        self.checkout("o/r")
        args = ["api", "repos/other/elsewhere/contents/README.md"]
        code, _out, _err, passed = self.run_gh(args)
        self.assertIsNone(code)
        self.assertEqual(passed, args)

    def test_the_checkout_named_is_the_one_the_reader_means(self) -> None:
        """A host has many copies of a repository. Glob order is arbitrary, so without a rule the
        refusal pointed at /home/leo/Projects/cmux-history rather than .../cmux (seen live)."""
        self.checkout("o/cmux", name="cmux-history")
        self.checkout("o/cmux", name="cmux")
        self.checkout("o/cmux", name="cmux-scratch")
        _code, _out, err, _passed = self.run_gh(["api", "repos/o/cmux/contents/x"])
        self.assertIn(str(self.home / "roots" / "cmux") + ":", err)

    def test_the_shortest_path_wins_when_no_name_matches(self) -> None:
        self.checkout("o/r", name="a-very-long-checkout-name")
        self.checkout("o/r", name="short")
        _code, _out, err, _passed = self.run_gh(["api", "repos/o/r/contents/x"])
        self.assertIn(str(self.home / "roots" / "short") + ":", err)

    def test_a_worktree_counts_as_a_checkout(self) -> None:
        # A real `git worktree add`, not a handcrafted `.git` file: the worktree layout is what
        # both halves have to agree about -- the index reads the common directory's config text,
        # and git_remotes asks git from inside the worktree.
        clone = self.home / "clone"
        where = self.home / "roots" / "lane"
        for argv in (["init", "-q", str(clone)],
                     ["-C", str(clone), "remote", "add", "origin", "https://github.com/o/r"],
                     ["-C", str(clone), "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                      "commit", "-q", "--allow-empty", "-m", "x"],
                     ["-C", str(clone), "worktree", "add", "-q", "--detach", str(where)]):
            done = subprocess.run(["git", *argv], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, f"git {argv[0]} failed: {done.stderr}")
        code, _out, err, _passed = self.run_gh(["api", "repos/o/r/contents/x"])
        self.assertEqual(code, gg.BROKER_EXIT_REFUSED)
        self.assertIn(str(where), err)

    def test_an_escape_hatch_passes_one_call_through(self) -> None:
        self.checkout("o/r")
        with mock.patch.dict(os.environ, {"GLAEDA_GH_ALLOW_API_SOURCE": "1"}):
            args = ["api", "repos/o/r/contents/x"]
            code, _out, _err, passed = self.run_gh(args)
        self.assertIsNone(code)
        self.assertEqual(passed, args)

    def test_a_blobless_clone_is_still_a_checkout(self) -> None:
        """A partial clone's fetch line in `git remote -v` ends in its filter (`(fetch) [blob:none]`).
        Reading that as a shape git_remotes cannot understand declined every blobless clone, and on
        cosmic-gray 353 source reads an hour of manaflow-ai/cmux went to the API past this refusal."""
        where = self.git_checkout(self.home / "roots" / "r",
                                  '[remote "origin"]\n\turl = git@github.com:o/r.git\n'
                                  "\tpromisor = true\n\tpartialclonefilter = blob:none\n")
        self.assertEqual(gg.git_remotes(where), ["o/r"])
        code, _out, err, passed = self.run_gh(["api", "repos/o/r/contents/README.md"])
        self.assertEqual((code, passed), (gg.BROKER_EXIT_REFUSED, None))
        self.assertIn(str(where), err)

    def test_a_filter_is_only_read_after_a_fetch_marker(self) -> None:
        for line, want in (("origin\tgit@github.com:o/r.git (fetch) [blob:none]", ["o/r"]),
                           ("origin\tgit@github.com:o/r.git (fetch) [tree:0]", ["o/r"]),
                           ("origin\tgit@github.com:o/r.git (push) [blob:none]", None),
                           ("origin\tgit@github.com:o/r.git (fetch) blob:none", None),
                           ("origin\tgit@github.com:o/r.git (fetch) [a b]", None)):
            with self.subTest(line=line):
                listed = subprocess.CompletedProcess([], 0, line + "\n", "")
                none = subprocess.CompletedProcess([], 1, "", "")
                with mock.patch.object(gg, "run_bounded", side_effect=[none, listed]):
                    self.assertEqual(gg.git_remotes(self.home), want)

    def test_a_write_to_a_contents_path_is_not_a_read(self) -> None:
        self.checkout("o/r")
        args = ["api", "-X", "PUT", "repos/o/r/contents/x", "-f", "message=x"]
        code, _out, _err, passed = self.run_gh(args)
        self.assertIsNone(code)
        self.assertEqual(passed, args)


class BrokerWatchTest(BrokerBase):
    def setUp(self) -> None:
        super().setUp()
        gg.write_json(gg.BASE / "daemon.json", {"pid": os.getpid(), "at": time.time(),
                                                "heartbeat": gg.HEARTBEAT_EVERY})
        self.lock = (gg.BASE / "serve.lock")
        self.held = mock.patch.object(gg, "daemon_running", lambda grace=0.0: True)
        self.held.start()
        self.addCleanup(self.held.stop)

    def watched(self) -> list[str]:
        return sorted((gg.read_json(p) or {}).get("key") for p in gg.watch_dir().glob("*.json"))

    def test_one_read_of_a_target_never_starts_a_watch(self) -> None:
        """A sweep over fifty PRs would otherwise cost the daemon more than the sweep saved."""
        self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        self.assertEqual(self.watched(), [])

    def test_a_second_read_within_the_window_starts_one(self) -> None:
        self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        self.assertEqual(self.watched(), ["pr:o/r#7"])

    def test_different_targets_do_not_add_up(self) -> None:
        self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        self.run_gh(["pr", "view", "8", "-R", "o/r", "--json", "state"])
        self.assertEqual(self.watched(), [])

    def test_a_read_the_cache_could_not_have_answered_is_not_worth_watching(self) -> None:
        for _ in range(3):
            self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state,body"])
        self.assertEqual(self.watched(), [])


class BrokerBudgetTest(BrokerBase):
    def test_the_cache_horizon_widens_as_the_shared_quota_drains(self) -> None:
        self.assertEqual(gg.broker_max_age(["title"]), gg.BROKER_MAX_AGE)
        gg.write_json(gg.BASE / "budget.json",
                      {"rest": {"limit": 5000, "remaining": 90, "reset": time.time() + 600, "seenAt": time.time()}})
        self.assertEqual(gg.broker_max_age(["title"]), gg.BROKER_LEAN_AGE)
        self.assertEqual(gg.broker_max_age(["title", "state"]), gg.BROKER_MAX_AGE, "one live field is enough")

    def test_a_spent_bucket_lets_an_older_entry_answer(self) -> None:
        self.cache_pr(7, {"title": "a fix"}, age=gg.BROKER_MAX_AGE + 30)
        _code, out, _err, passed = self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "title"])
        self.assertEqual((out, passed), ("", ["pr", "view", "7", "-R", "o/r", "--json", "title"]))
        gg.write_json(gg.BASE / "budget.json",
                      {"graphql": {"limit": 5000, "remaining": 67, "reset": time.time() + 600, "seenAt": time.time()}})
        code, out, _err, passed = self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "title"])
        self.assertEqual((code, out, passed), (0, '{"title":"a fix"}\n', None))

    def test_a_reset_that_has_passed_is_not_treated_as_lean(self) -> None:
        gg.write_json(gg.BASE / "budget.json",
                      {"rest": {"limit": 5000, "remaining": 0, "reset": time.time() - 1, "seenAt": time.time()}})
        self.assertEqual(gg.broker_max_age(["title"]), gg.BROKER_MAX_AGE)

    def test_the_horizon_can_be_set_for_one_call(self) -> None:
        with mock.patch.dict(os.environ, {"GLAEDA_GH_MAX_AGE": "3600"}):
            self.assertEqual(gg.broker_max_age(["title"]), 3600)


class BrokerSpentBucketTest(BrokerBase):
    """Leo, 2026-10-21: "just use rest gh then?" A `pr view` gh would answer over a spent GraphQL
    bucket is read by the daemon over REST instead, and served from that read."""

    ARGS = ["pr", "view", "7", "-R", "o/r", "--json", "title"]

    def setUp(self) -> None:
        super().setUp()
        self.running = mock.patch.object(gg, "daemon_running", lambda grace=0.0: True)
        self.running.start()
        self.addCleanup(self.running.stop)
        self.wait = mock.patch.object(gg, "BROKER_SPENT_WAIT", 0.2)
        self.wait.start()
        self.addCleanup(self.wait.stop)

    def budget(self, graphql: int, rest: int, reset: float = 600.0) -> None:
        at = time.time()
        gg.write_json(gg.BASE / "budget.json", {
            "graphql": {"limit": 5000, "remaining": graphql, "reset": at + reset, "seenAt": at},
            "rest": {"limit": 5000, "remaining": rest, "reset": at + reset, "seenAt": at}})

    def watched(self) -> list[str]:
        return sorted((gg.read_json(p) or {}).get("key") for p in gg.watch_dir().glob("*.json"))

    def test_a_miss_over_spent_graphql_is_served_from_the_daemons_read(self) -> None:
        self.budget(graphql=0, rest=4000)
        real_register = gg.register

        def register(key: str, *a, **kw) -> None:  # the daemon's REST read, landing a tick later
            real_register(key, *a, **kw)
            self.cache_pr(7, {"title": "a fix"})

        with mock.patch.object(gg, "register", register):
            code, out, _err, passed = self.run_gh(self.ARGS)
        self.assertEqual((code, out, passed), (0, '{"title":"a fix"}\n', None))
        noted = [json.loads(line) for line in gg.broker_log().read_text().splitlines()]
        self.assertEqual({k: noted[-1].get(k) for k in ("route", "key", "spent")},
                         {"route": "serve", "key": "pr:o/r#7", "spent": True})

    def test_no_answer_in_time_still_runs_gh(self) -> None:
        self.budget(graphql=0, rest=4000)
        _code, out, _err, passed = self.run_gh(self.ARGS)
        self.assertEqual((out, passed), ("", self.ARGS))
        self.assertEqual(self.watched(), ["pr:o/r#7"], "the daemon was asked even so")

    def test_a_healthy_bucket_does_not_wait_or_watch(self) -> None:
        self.budget(graphql=4000, rest=4000)
        with mock.patch.object(gg, "broker_await", side_effect=AssertionError("waited")):
            _code, _out, _err, passed = self.run_gh(self.ARGS)
        self.assertEqual(passed, self.ARGS)
        self.assertEqual(self.watched(), [])

    def test_both_buckets_spent_goes_straight_to_gh(self) -> None:
        self.budget(graphql=0, rest=0)
        self.assertFalse(gg.broker_bucket_spent("pr:o/r#7", time.time()))

    def test_a_reset_that_has_passed_is_not_spent(self) -> None:
        self.budget(graphql=0, rest=4000, reset=-1)
        self.assertFalse(gg.broker_bucket_spent("pr:o/r#7", time.time()))

    def test_each_read_is_judged_by_the_bucket_gh_spends_on_it(self) -> None:
        self.budget(graphql=4000, rest=0)
        now = time.time()
        self.assertFalse(gg.broker_bucket_spent("pr:o/r#7", now), "gh pr view is GraphQL")
        self.assertTrue(gg.broker_bucket_spent("run:o/r/9", now), "gh run view is REST")

    def test_a_throttled_or_forced_low_bucket_is_spent_for_gh(self) -> None:
        self.budget(graphql=4000, rest=4000)
        now = time.time()
        gg.write_json(gg.BASE / "daemon.json", {"at": now, "modes": {"graphql": "stop", "rest": "normal"}})
        self.assertTrue(gg.broker_bucket_spent("pr:o/r#7", now), "GitHub refused GraphQL: the daemon reads it over REST")
        self.assertFalse(gg.broker_bucket_spent("run:o/r/9", now))
        gg.write_json(gg.BASE / "daemon.json", {"at": now, "modes": {"graphql": "stop", "rest": "stop"}})
        self.assertFalse(gg.broker_bucket_spent("pr:o/r#7", now), "a secondary hold stops both: nothing to gain")
        gg.write_json(gg.BASE / "daemon.json", {"at": now - 10 * gg.CYCLE, "modes": {"graphql": "stop", "rest": "normal"}})
        self.assertFalse(gg.broker_bucket_spent("pr:o/r#7", now), "an old heartbeat says nothing about now")
        gg.write_json(gg.DAEMON_CONFIG, {"forceLow": ["graphql"]})
        self.addCleanup(lambda: gg.DAEMON_CONFIG.unlink(missing_ok=True))
        self.assertTrue(gg.broker_bucket_spent("pr:o/r#7", now))

    def test_without_the_daemon_nothing_waits(self) -> None:
        self.budget(graphql=0, rest=4000)
        with mock.patch.object(gg, "daemon_running", lambda grace=0.0: False), \
                mock.patch.object(gg, "broker_await", side_effect=AssertionError("waited")):
            _code, _out, _err, passed = self.run_gh(self.ARGS)
        self.assertEqual(passed, self.ARGS)


class BrokerFailOpenTest(BrokerBase):
    def test_an_error_anywhere_in_the_broker_still_runs_the_real_gh(self) -> None:
        with mock.patch.object(gg, "broker_plan", side_effect=RuntimeError("boom")):
            args = ["pr", "view", "7", "-R", "o/r", "--json", "state"]
            code, _out, _err, passed = self.run_gh(args)
        self.assertIsNone(code)
        self.assertEqual(passed, args)
        noted = [json.loads(line) for line in gg.broker_log().read_text().splitlines()]
        self.assertEqual(noted[-1]["route"], "error")

    def test_a_cache_entry_that_is_not_a_dict_is_passed_through(self) -> None:
        key = "pr:o/r#7"
        gg.write_json(gg.cache_dir() / gg.file_name(key), {"key": key, "data": "nonsense", "dataAt": time.time()})
        _code, out, _err, passed = self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        self.assertEqual(out, "")
        self.assertIsNotNone(passed)

    def test_an_entry_the_daemon_marked_failed_is_passed_through(self) -> None:
        key = "pr:o/r#7"
        gg.write_json(gg.cache_dir() / gg.file_name(key),
                      {"key": key, "data": {"state": "OPEN"}, "dataAt": time.time(), "error": "graphql: HTTP 502"})
        _code, out, _err, passed = self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        self.assertEqual(out, "")
        self.assertIsNotNone(passed)


class BrokerMergeGuardTest(BrokerBase):
    """A merge typed as `glaeda-gh gh ...` is handed to the gh-quota guard, never run on the real gh unjudged."""

    def setUp(self) -> None:
        super().setUp()
        os.environ.pop("GH_QUOTA_CHECKED", None)
        self.guard = self.home / "quota" / "gh"
        self.guard.parent.mkdir(parents=True)
        self.guard.write_text("#!/bin/sh\n# gh guard: gh-quota (a test stand-in)\nexit 0\n")
        self.guard.chmod(0o755)
        os.environ["GLAEDA_MERGE_GUARD"] = str(self.guard)

    def route(self, args: list[str]) -> tuple[int | None, str, str | None, list[str] | None]:
        """(exit code, stderr, the path exec'd, its arguments)."""
        out, err, execed = io.StringIO(), io.StringIO(), []
        os.environ.pop(gg.BROKER_MARK, None)
        os.environ.pop(gg.BROKER_DEPTH, None)

        def execv(path: str, argv: list[str]) -> None:
            execed.append((path, argv))
            raise SystemExit(0)

        with mock.patch.object(os, "execv", execv), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                code = gg.cmd_gh(args)
            except SystemExit:
                code = None
        return code, err.getvalue(), (execed[0][0] if execed else None), (execed[0][1][1:] if execed else None)

    def test_every_merge_route_goes_to_the_guard(self) -> None:
        query = tempfile.NamedTemporaryFile("w", suffix=".json", dir=self.home, delete=False)
        query.write(json.dumps({"query": "mutation { mergePullRequest(input: {pullRequestId: \"X\"}) { clientMutationId } }"}))
        query.close()
        gql = self.home / "q.graphql"
        gql.write_text("mutation($id: ID!) { enablePullRequestAutoMerge(input: {pullRequestId: $id}) { clientMutationId } }")
        (self.home / "benign.graphql").write_text("query { viewer { login } }")
        (self.home / "benign.json").write_text(json.dumps({"query": "query { viewer { login } }"}))
        for args in (["pr", "merge", "3589", "-R", "o/r", "--squash", "--match-head-commit", "a" * 40],
                     ["api", "--method", "PUT", "repos/o/r/pulls/3589/merge", "-f", "sha=" + "a" * 40],
                     ["api", "-X", "PUT", "/repos/o/r/pulls/3589/merge/", "-f", "sha=" + "a" * 40],
                     ["api", "-XPUT", "https://api.github.com/repos/o/r/pulls/3589/merge"],
                     ["api", "graphql", "-f", "query=mutation { mergePullRequest(input: {pullRequestId: \"X\", "
                      "expectedHeadOid: \"" + "a" * 40 + "\"}) { clientMutationId } }"],
                     ["api", "graphql", "--input", query.name],
                     ["api", "graphql", "-F", "query=@" + str(gql), "-f", "id=X"],
                     ["api", "graphql", "--input", "-"],
                     ["api", "-X", "PUT", "repos/o/r/pulls/3589/merge?x=y", "-f", "sha=" + "a" * 40],  # F1 on #1878
                     ["api", "graphql?x=y", "-f", "query=mutation { mergePullRequest(input: {}) { clientMutationId } }"],
                     # F2 on #1878: a file is the guard's to read (it snapshots it), whatever it says now
                     ["api", "graphql", "-F", "query=@" + str(self.home / "benign.graphql")],
                     ["api", "graphql", "--input=" + str(self.home / "benign.json")],
                     ["api", "-X", "PUT", "HTTPS://API.GITHUB.COM/repos/o/r/pulls/5/merge"],  # second review F1
                     ["api", "HTTPS://api.github.com/graphql", "-f", "query=mutation { mergePullRequest(input: {}) "
                      "{ clientMutationId } }"]):
            with self.subTest(args=args):
                code, _err, path, passed = self.route(args)
                self.assertIsNone(code)
                self.assertEqual(path, str(self.guard))
                self.assertEqual(passed, args)
                self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], str(self.guard.parent))

    def test_the_switch_that_skips_the_broker_never_skips_the_guard(self) -> None:
        os.environ["CMUX_GH_BROKER_DISABLE"] = "1"
        self.addCleanup(os.environ.pop, "CMUX_GH_BROKER_DISABLE", None)
        _code, _err, path, _passed = self.route(["pr", "merge", "7", "-R", "o/r", "--squash"])
        self.assertEqual(path, str(self.guard))

    def test_reads_and_other_writes_never_go_to_the_guard(self) -> None:
        for args in (["pr", "view", "7", "-R", "o/r", "--json", "state"],
                     ["api", "repos/o/r/pulls/7/merge"],  # GET: whether it merged
                     ["api", "-X", "PUT", "repos/o/r/pulls/7/update-branch"],
                     ["api", "graphql", "-f", "query=query { viewer { login } }"],
                     ["api", "graphql", "-f", "query=mutation { addComment(input: {subjectId: \"X\", body: \"mergePull\"}) "
                      "{ clientMutationId } }"]):
            with self.subTest(args=args):
                _code, _err, path, _passed = self.route(args)
                self.assertNotEqual(path, str(self.guard))

    def test_a_merge_the_guard_judged_passes_to_the_real_gh(self) -> None:
        os.environ["GH_QUOTA_CHECKED"] = "1"
        args = ["pr", "merge", "7", "-R", "o/r", "--squash", "--match-head-commit", "a" * 40]
        _code, _err, path, passed = self.route(args)
        self.assertEqual(path, os.environ["GLAEDA_GH_REAL"])
        self.assertEqual(passed, args)

    def test_a_guard_lookup_that_fails_refuses_the_merge(self) -> None:
        os.environ["GLAEDA_MERGE_GUARD"] = "/" + "x" * 5000  # is_file() raises: name too long
        code, err, path, _passed = self.route(["pr", "merge", "7", "-R", "o/r", "--squash"])
        self.assertEqual(code, gg.BROKER_EXIT_REFUSED)
        self.assertIsNone(path)
        self.assertIn("refused", err)

    def test_a_help_request_is_never_a_merge(self) -> None:
        os.environ["GLAEDA_MERGE_GUARD"] = str(self.home / "nowhere" / "gh")
        for args in (["pr", "merge", "--help"], ["pr", "merge", "-h"],
                     ["api", "-X", "PUT", "repos/o/r/pulls/5/merge", "--help"]):
            with self.subTest(args=args):
                code, _err, path, _passed = self.route(args)
                self.assertNotEqual(code, gg.BROKER_EXIT_REFUSED)
                self.assertNotEqual(path, str(self.home / "nowhere" / "gh"))

    def test_no_guard_on_the_host_refuses_the_merge(self) -> None:
        os.environ["GLAEDA_MERGE_GUARD"] = str(self.home / "nowhere" / "gh")
        code, err, path, _passed = self.route(["api", "--method", "PUT", "repos/o/r/pulls/7/merge", "-f", "sha=x"])
        self.assertEqual(code, gg.BROKER_EXIT_REFUSED)
        self.assertIsNone(path)
        self.assertIn("gh-quota guard", err)


class BrokerArgumentTest(unittest.TestCase):
    def test_a_flags_value_is_never_read_as_a_target(self) -> None:
        words, method, seen, values = gg.broker_words(
            ["pr", "view", "7", "-R", "o/r", "--json", "state", "--jq", ".state"])
        self.assertEqual(words, ["pr", "view", "7"])
        self.assertEqual(method, "GET")
        self.assertEqual(values["--repo"], "o/r")
        self.assertEqual(values["--json"], "state")
        self.assertIn("--jq", seen)

    def test_a_short_flag_and_its_long_spelling_are_one_flag_last_one_winning(self) -> None:
        # cobra's last-wins: `gh pr view 7 -R a/b --repo c/d` reads c/d. Keeping the two spellings
        # apart made the broker answer with a/b -- another repository's PR, under the same number.
        _, _, seen, values = gg.broker_words(["pr", "view", "7", "-R", "a/b", "--repo", "c/d"])
        self.assertEqual(values["--repo"], "c/d")
        self.assertEqual(seen, {"--repo"})
        self.assertEqual(gg.broker_words(["pr", "view", "7", "--repo", "c/d", "-Ra/b"])[3]["--repo"], "a/b")

    def test_a_value_flag_with_no_value_is_present_without_one(self) -> None:
        # `gh pr view 7 --json` is an error in gh ("flag needs an argument"). Treating the flag as
        # absent would serve every field of a call gh refuses to make.
        _, _, seen, values = gg.broker_words(["pr", "view", "7", "--json"])
        self.assertIn("--json", seen)
        self.assertNotIn("--json", values)
        self.assertEqual(gg.broker_plan(["pr", "view", "7", "--json"], 0.0)[0], "pass")
        self.assertEqual(gg.broker_plan(["pr", "view", "7", "--json", "state", "-R"], 0.0)[0], "pass")

    def test_fields_or_a_body_make_it_a_post_without_minus_x(self) -> None:
        # gh POSTs when it is given fields. Reading those calls as GETs had the broker refusing a
        # write to .../contents as if it were a source read.
        for args in (["api", "repos/o/r/contents/f", "-f", "message=x"],
                     ["api", "repos/o/r/contents/f", "--field", "sha=x"],
                     ["api", "repos/o/r/contents/f", "--input", "-"]):
            with self.subTest(args=args):
                self.assertEqual(gg.broker_words(args)[1], "POST")
        self.assertEqual(gg.broker_words(["api", "repos/o/r/contents/f", "-X", "GET", "-f", "a=b"])[1], "GET")

    def test_the_method_is_found_in_each_spelling(self) -> None:
        for args, expect in ((["api", "x"], "GET"), (["api", "-X", "post", "x"], "POST"),
                             (["api", "-XDELETE", "x"], "DELETE"), (["api", "--method=patch", "x"], "PATCH"),
                             (["api", "--method", "put", "x"], "PUT")):
            with self.subTest(args=args):
                self.assertEqual(gg.broker_words(args)[1], expect)

    def test_everything_after_a_bare_double_dash_is_positional(self) -> None:
        self.assertEqual(gg.broker_words(["pr", "view", "--", "-R"])[0], ["pr", "view", "-R"])


class AuditTest(BrokerBase):
    def write_log(self, lines: list[tuple[int, str]]) -> Path:
        path = self.home / "audit.log"
        path.write_text("".join(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(NOW + at))} s1 {text}\n"
                                for at, text in lines))
        return path

    def test_it_counts_what_a_cache_would_have_answered(self) -> None:
        path = self.write_log([(0, "pr view pr:o/r#7"), (10, "pr view pr:o/r#7"), (20, "pr view pr:o/r#7"),
                               (0, "pr merge"), (0, "api repos/o/r/contents/x"),
                               (0, "api graphql")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gg.main(["audit", "--log", str(path), "--json"]), 0)
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["calls"], 6)
        self.assertEqual(doc["cacheableReads"], 3)
        self.assertEqual(doc["repeatsWithin"], {"seconds": gg.AUDIT_WINDOW, "calls": 2})
        self.assertEqual(doc["categories"]["source read through the API"], 1)
        self.assertEqual(doc["categories"]["raw gh api graphql"], 1)
        self.assertEqual(doc["categories"]["pr merge"], 1)

    def audit_json(self, path: Path) -> dict:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gg.main(["audit", "--log", str(path), "--json"]), 0)
        return json.loads(out.getvalue())

    def audit_text(self, path: Path) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gg.main(["audit", "--log", str(path)]), 0)
        return out.getvalue()

    def test_a_graphql_call_is_counted_however_the_endpoint_was_typed(self) -> None:
        """One endpoint, four spellings, and the log keeps whichever the caller typed. Counted as
        one category, or the count is wrong for a caller writing a shape this file's own brake test
        pins as plausible (`api /graphql`), and a host writing only that spelling gets no summary
        line at all.

        Both endpoints, through one `endpoint_re`: `BROKER_FREE_RE` had the same api.github.com
        pin, and it gates the broker's free-call exemption as well as this category, so a GHE
        host's `rate_limit` read was counted in a category of its own *and* held behind the repeat
        delay -- a free call, slowed by the brake meant for calls that spend the quota."""
        path = self.write_log([(0, "api graphql"), (5, "api /graphql"),
                               (10, "api https://api.github.com/graphql"),
                               # A GHE host, which this file handles deliberately elsewhere: its
                               # endpoint lives under /api/, and pinning api.github.com put it in
                               # a category of its own, so the count undercounted on exactly the
                               # hosts whose remotes this tool goes out of its way to support.
                               (12, "api https://ghe.example/api/graphql"),
                               # The same four spellings for the endpoint beside it, which shares
                               # the pattern -- and whose GHE spelling the broker also charges a
                               # repeat delay for (see the brake test).
                               (15, "api /rate_limit"), (16, "api rate_limit"),
                               (17, "api https://api.github.com/rate_limit"),
                               # The two GHE bases, which are not the same one: GraphQL is at
                               # /api/graphql and REST is under /api/v3/, so this is the spelling a
                               # GHE host really serves for the free call. With `/api/` alone it
                               # was filed as an ordinary path and still held 30 s -- the defect the
                               # helper claimed to have removed, measured by a review.
                               (18, "api https://ghe.example/api/rate_limit"),
                               (19, "api https://ghe.example/api/v3/rate_limit"),
                               (20, "pr view pr:o/r#7")])
        doc = self.audit_json(path)
        self.assertEqual(doc["categories"]["raw gh api graphql"], 4, doc["categories"])
        self.assertEqual(doc["categories"]["rate_limit (free)"], 5, doc["categories"])
        # And not so wide that an unrelated path under a host reads as either endpoint.
        for other in ("api https://ghe.example/api/v3/repos/o/r", "api repos/o/graphql-tools",
                      "api https://ghe.example/api/rate_limit/history",
                      "api https://ghe.example/api/v3/rate_limit/history"):
            with self.subTest(other):
                doc = self.audit_json(self.write_log([(0, other)]))
                self.assertNotIn("raw gh api graphql", doc["categories"], doc["categories"])
                self.assertNotIn("rate_limit (free)", doc["categories"], doc["categories"])

    def test_the_hidden_reads_move_a_share_in_both_directions(self) -> None:
        """The sentence's one directional claim, held by measurement instead of by review.

        It says a share can go up, down or stay where it is, and four rounds of this PR each
        asserted a direction the log cannot see -- "grows the numerator and the denominator", which
        reads as both always growing, and then "can grow the numerator and *does* grow the
        denominator", which a review broke by writing the hidden read out in *place* of the batched
        call that carried it: 33.3% to 33.3%, with the denominator flat. Nothing in the suite could
        tell: the only guard was `assertIn` on the whole sentence, so a wrong direction *inside*
        the pinned string was green, and every fixture here added lines to the log, which makes a
        growing denominator the fixture's property rather than a measurement.

        So the property is driven, under both counterfactuals -- the hidden reads written out
        beside the batched call, and written out in place of it, which is the honest one when the
        call carried exactly those reads. Each case is the same traffic counted twice. The repeat
        count never falls under insertion -- that is what "floor" means, and it is asserted in
        every case -- while the share goes whichever way the two counts take it.

        Times go forwards in every fixture: `at - seen <= AUDIT_WINDOW` counts a negative delta as
        a repeat, and a fixture that relies on that is measuring the test rather than the tool. Not
        "a shape no writer produces", which a review corrected -- `broker_log_call` stamps the line
        before it takes the append lock, so two sessions on one host can interleave by up to the
        lock budget. It changes none of the counts here (recomputed in time order: 1, 2, 2)."""
        hidden = "the objects inside the graphql call, counted"
        visible = [(0, "api graphql"), (10, "pr view pr:o/r#1"), (20, "pr view pr:o/r#1")]
        for name, batched, want in (
                # One hidden read of an object nothing re-reads: denominator up, numerator flat.
                ("a share that falls", [(21, "pr view pr:o/r#9")], "down"),
                # The hidden read repeats a visible one: both parts up, and the share with it.
                ("a share that rises", [(21, "pr view pr:o/r#1")], "up"),
                # Three hidden reads carrying one repeat between them, against 1/3 before: 2/6.
                ("a share that does not move", [(21, "pr view pr:o/r#2"), (22, "pr view pr:o/r#2"),
                                                (23, "pr view pr:o/r#3")], "flat")):
            with self.subTest(name):
                before = self.audit_json(self.write_log(visible))
                after = self.audit_json(self.write_log(visible + batched))
                self.assertGreaterEqual(after["repeatsWithin"]["calls"],
                                        before["repeatsWithin"]["calls"],
                                        f"{hidden}: the repeat count fell, so it is no floor")
                self.assertGreater(after["calls"], before["calls"], hidden)
                shares = (before["repeatsWithin"]["calls"] / before["calls"],
                          after["repeatsWithin"]["calls"] / after["calls"])
                if want == "down":
                    self.assertGreater(shares[0], shares[1], shares)
                elif want == "up":
                    self.assertLess(shares[0], shares[1], shares)
                else:
                    self.assertEqual(shares[0], shares[1], shares)
        # The replacement counterfactual: the batched call stood for the one read it carried, so
        # writing that read out takes the graphql line away with it. Nothing moves -- which is why
        # the sentence says a share can stay where it is and claims no direction for either half.
        before = self.audit_json(self.write_log(visible))
        swapped = self.audit_json(self.write_log([(1, "pr view pr:o/r#9")] + visible[1:]))
        self.assertEqual(swapped["calls"], before["calls"], "the denominator was supposed to be flat")
        self.assertEqual(swapped["repeatsWithin"]["calls"] / swapped["calls"],
                         before["repeatsWithin"]["calls"] / before["calls"],
                         "a share that does not move when a hidden read replaces its own call")
        # And the sentence is held to what was just measured, not only pinned as a literal string.
        # A review rewrote the claim *and* the pinned copy together -- "always grows the
        # denominator, so a share can only go down", round 5's "None of them is counted above", and
        # the floor re-scoped to "what a cache could have answered" -- and the suite stayed green
        # each time. That coupling is how every wrong claim in this PR's history got in.
        text = self.audit_text(self.write_log(visible))
        self.assertIn("can go up, down or stay where it is", text, text)
        for refuted in ("can only go down", "None of them is counted above",
                        "does grow the denominator", "a floor on what a cache could have answered"):
            self.assertNotIn(refuted, text, f"a claim three rounds of review refuted: {refuted}")
        # "the calls are counted above" means counted in the total the share is taken over, which
        # is arithmetic rather than wording: take the graphql lines out and the total falls by
        # exactly the number the sentence names.
        batched = before["categories"]["raw gh api graphql"]
        self.assertIn(f"{before['calls']} gh calls logged", text, text)
        self.assertIn(f"{batched} of those calls was `gh api graphql`", text, text)
        quiet = self.audit_json(self.write_log([line for line in visible if "graphql" not in line[1]]))
        self.assertEqual(quiet["calls"], before["calls"] - batched)

    def test_the_floor_is_over_one_set_of_calls(self) -> None:
        """The sentence says "in this report", and this is the measurement that makes those three
        words load-bearing rather than hedging.

        `--last N` reports on the last N lines, so writing hidden reads out as separate lines
        pushes older calls out of the window and the two reports are over different sets: the
        repeat count *falls*, 1 to 0. Held to one set it cannot, because at most one repeat is
        counted per line per target. Only a comment said so."""
        visible = [(0, "pr view pr:o/r#1"), (10, "pr view pr:o/r#1"), (20, "api graphql")]
        self.assertEqual(self.audit_json(self.write_log(visible))["repeatsWithin"]["calls"], 1)
        written_out = self.write_log(visible + [(21, "pr view pr:o/r#9")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gg.main(["audit", "--log", str(written_out), "--last", "3", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["repeatsWithin"]["calls"], 0,
                         "the known limit: --last moves the window, so this is a different report")
        # The whole log, which is one set of calls: it does not fall.
        self.assertEqual(self.audit_json(written_out)["repeatsWithin"]["calls"], 1)

    def test_a_graphql_call_hides_reads_from_the_repeat_figure_and_the_report_says_so(self) -> None:
        """Coverage, not cost. An earlier version of this line said a batching caller "looks
        cheaper here than its GraphQL points are", which was backwards: GitHub prices a GraphQL
        call at the requests its connections need divided by 100, rounded, minimum 1, so one
        aliased query over forty PRs is about 1 point against about 40 for the loop -- and
        `skills/github-ci-wait/SKILL.md` says exactly that, so the report contradicted this repo's
        own guidance. Per-call shares are the right unit for GraphQL points.

        What a `graphql` line does hide is *reads*: the category is not cacheable, so nothing
        inside it reaches `cacheableReads`, `repeatsWithin` or `perRead`, which makes the repeat
        *count* a floor rather than an estimate -- the count and not the shares printed beside it,
        which can go up, down or stay put (the three fixtures below). An earlier version of this
        docstring said they "fall ... because the denominator grows too", fifteen lines above its
        own assertion that a share can move either way: one of the two had to be wrong, and a
        review measured that it was this one. The sentence makes no claim about cost, because the
        cost is the part that goes stale -- and that is asserted here, not just intended.

        The primary guard is the sentence itself, asserted whole: a rewrite that smuggles a price
        back in has to change it. The word ban is the backstop, and it is scoped to that last
        paragraph with word boundaries, which takes two faults out of it: over the whole report
        the bare substring `point` fires on a logged path (`repos/o/endpoint-registry`, which
        reaches the category rows, or a future summary line about "the GraphQL endpoint"), and word
        boundaries alone would still fire on a repository actually named `points`. The cost of
        scoping is that a priced paragraph printed *before* this one is no longer caught; the
        sentence assertion is what covers a rewrite of this one."""
        path = self.write_log([(0, "api graphql"), (5, "api graphql"), (10, "pr view pr:o/r#7")])
        doc = self.audit_json(path)
        self.assertEqual(doc["cacheableReads"], 1, "a graphql line was counted as a cacheable read")
        self.assertEqual(doc["repeatsWithin"]["calls"], 0)
        text = self.audit_text(path)
        self.assertIn("2 of those calls were `gh api graphql`, each carrying however many objects "
                      "the caller asked for, which this cannot see. The calls are counted above; "
                      "none of the objects inside them is, so none of them is counted as a "
                      "cacheable read or as a repeat. That makes the repeat count a floor on the "
                      "same-target re-reads a cache could have been asked for in this report, not "
                      "an estimate. The shares beside it are not floors: a hidden read written out "
                      "can land on either side of a share, so a share can go up, down or stay "
                      "where it is.", text)
        # The *total* is what makes "the calls are counted above" true, and what made the earlier
        # "None of them is counted above" false in the same report. Not the category row: the
        # table is the top 20 by count, so on a host with more than twenty distinct categories
        # there is no graphql row and the sentence still holds -- which is why the row is asserted
        # here as a nicety and the total is asserted as the reason.
        self.assertIn("3 gh calls logged", text)
        self.assertIn("raw gh api graphql", text)
        crowded = self.audit_text(self.write_log(
            [(0, "api graphql"), (1, "pr view pr:o/r#7")]
            # Twenty-one categories of two calls each, so the single graphql call sorts below
            # every one of them and the top-20 table has no room for it.
            + [(2 + i, f"api repos/o/r/thing{i // 2}") for i in range(42)]))
        self.assertNotIn("raw gh api graphql", crowded,
                         "the premise: twenty-one bigger categories push the row out of the table")
        self.assertEqual(sum(1 for i in range(21) if f"api repos/o/r/thing{i}" in crowded), 20,
                         "the table is the top twenty by count, which is the premise above")
        # "was", for the one call: N=1 is the common case on a host that batches rarely.
        self.assertIn("1 of those calls was `gh api graphql`", crowded, crowded)
        self.assertIn("44 gh calls logged", crowded, crowded)
        self.assertEqual(cost_claims(text), [], "the report is making a claim about cost again")

    def test_the_cost_ban_fires_on_a_priced_sentence_and_only_in_the_last_paragraph(self) -> None:
        """The guard from the side that can fail. Every other assertion on it is
        `cost_claims(...) == []`, so a mutation gutting it (`return []`, or reading the *first*
        paragraph) left all 334 green: a guard that cannot fail when disabled is not a guard.
        This also exercises the three patterns no fixture reaches, and pins the cost the scoping
        was accepted for -- a priced paragraph printed before the graphql one is not caught."""
        # With a trailing newline, because `print` leaves one and nothing the ban is ever
        # shown ends without it: on a sample without it, `split("\n")[-1]` -- a mutation of
        # the paragraph split -- read the priced text and looked like a working guard, while
        # on a real report that same mutation reads the empty string after the last newline
        # and returns nothing for any price at all. Measured: all 335 tests green with it.
        priced = "head\n\nthe cheapest call is 2 points per call, at any price\n"
        # The patterns by name, not `list(STALE_COST)`: that adapts to whatever the tuple holds,
        # so cutting it down to one pattern stayed green (measured).
        self.assertEqual(cost_claims(priced),
                         [r"\bpoints?\b", r"\bprices?\b", r"\bcheap(er|est)?\b", r"\bper call\b"],
                         priced)
        self.assertEqual(cost_claims("2 points per call\n\nclean tail"), [],
                         "the ban reads the last paragraph, and that is what it costs")
        # And the whole of it. The sample above is a one-liner, so it pins the patterns and not the
        # extent: `[-1]` -> `[-1][:200]` stayed green against it while the real last paragraph is
        # over 500 characters, i.e. a price smuggled into the *tail* of the shipped sentence would
        # not be caught. So the sample is a real report with a price spliced into its last
        # sentence, which is the shape a rewrite actually takes.
        real_report = self.audit_text(self.write_log([(0, "api graphql"), (5, "api graphql"),
                                                      (10, "pr view pr:o/r#7")]))
        self.assertEqual(cost_claims(real_report), [], "the premise: the shipped report is clean")
        tail_priced = real_report.rstrip("\n") + " And one batched call costs about 1 point.\n"
        self.assertGreater(len(tail_priced.split("\n\n")[-1]), 300,
                           "the paragraph has to be long enough for its tail to be cut off")
        self.assertEqual(cost_claims(tail_priced), [r"\bpoints?\b"], tail_priced)
        # The word boundaries, which the scoping hides: inside the last paragraph `point` fires on
        # an ordinary logged path and `\bpoints?\b` does not. Asserted here because no report
        # fixture can show it -- a category row is never in the last paragraph, so
        # `test_the_word_ban_does_not_fire_on_an_ordinary_logged_path` passes on the scoping alone
        # and `\bpoints?\b` -> `point` survived it (measured).
        self.assertEqual(cost_claims("head\n\napi repos/o/endpoint-registry was read twice\n"), [],
                         "the boundary is what keeps an endpoint path out of the ban")
        self.assertEqual(cost_claims("head\n\nthat read cost a point\n"), [r"\bpoints?\b"],
                         "and the boundary still catches the word itself")

    def test_the_word_ban_does_not_fire_on_an_ordinary_logged_path(self) -> None:
        """Both halves of the scoping, demonstrated rather than asserted away.

        `api repos/o/endpoint-registry` is not a cacheable read, so it never reaches the
        hottest-targets line -- it arrives on a *category* row, which no filter touches. The bare
        substring `point` fires on it and word boundaries do not, which is the first half.

        `pr view pr:o/points#7` is the second half, and the one that makes the paragraph scoping
        load-bearing rather than decorative: it is a cacheable read, it goes through `AUDIT_SAFE`
        into the hottest-targets line, and `\\bpoints?\\b` matches it. Over the whole report that
        would fail the ban; over the last paragraph it does not. Without this fixture, dropping
        `split("\\n\\n")[-1]` left the suite green."""
        for name, line in (("a category row", "api repos/o/endpoint-registry"),
                           ("the hottest-targets line", "pr view pr:o/points#7")):
            with self.subTest(name):
                path = self.write_log([(0, "api graphql"), (5, line), (10, line)])
                text = self.audit_text(path)
                self.assertIn("point", text, "without this the test proves nothing")
                self.assertEqual(cost_claims(text), [], text)
        # The claim about where each one lands, so the docstring above is checked too.
        hot = self.audit_text(self.write_log([(0, "api graphql"), (5, "pr view pr:o/points#7"),
                                              (10, "pr view pr:o/points#7")]))
        self.assertIn("hottest targets: pr view pr:o/points#7", hot)
        rows = self.audit_text(self.write_log([(0, "api graphql"),
                                               (5, "api repos/o/endpoint-registry"),
                                               (10, "api repos/o/endpoint-registry")]))
        self.assertNotIn("hottest targets", rows, "an api path is not a cacheable read")
        self.assertIn("api repos/o/endpoint-registry", rows)
        # What makes the scoping load-bearing rather than decorative: over the *whole* report this
        # fixture does trip the ban, and `cost_claims` reads the last paragraph only. Unscope it
        # and this test goes red, which is the only way a guard can be covered at all.
        self.assertTrue([pat for pat in STALE_COST if re.search(pat, hot)], hot)
        self.assertEqual(cost_claims(hot), [], hot)

    def test_the_graphql_line_is_absent_when_there_are_no_graphql_calls(self) -> None:
        """The conditionality is what makes it safe to print at all: a host with none of these
        calls gets no sentence about them. Nothing held it until a mutation (`if batched:` ->
        `if True:`) stayed green."""
        path = self.write_log([(0, "pr view pr:o/floorplan#7"), (5, "pr view pr:o/floorplan#7"),
                               (10, "pr merge")])
        text = self.audit_text(path)
        self.assertNotIn("gh api graphql", text)
        # The sentence, not the word, and the word really is in this report: the repo is called
        # `floorplan`, so "floor" reaches the hottest-targets line of a report with no graphql
        # sentence in it. The banned substring is taken from the shipped sentence and proved
        # present in a report that does have graphql calls -- an earlier version pinned a draft's
        # wording, which no mutation could have put in `text` at all, so the assertion could not
        # fail. A guard needs a positive sample.
        floor = "a floor on the same-target re-reads"
        self.assertIn("floor", text, "without this the assertion below proves nothing")
        self.assertNotIn(floor, text)
        self.assertIn(floor, self.audit_text(self.write_log([(0, "api graphql"),
                                                             (5, "pr view pr:o/r#1")])),
                      "the banned substring is not one this report can print")

    def test_an_old_logs_lines_are_counted_but_not_quoted_back(self) -> None:
        """The guard kept the value of a flag it did not know took one until 2026-10-21, and an
        audit is read, pasted into reports and committed. Those lines stay in the counts; the
        hottest-targets line only ever repeats the shape today's writers produce."""
        leaked = "api repos/o/r/issues/7/comments Authorization: Bearer ghp_" + "y" * 36
        path = self.write_log([(0, leaked), (5, leaked), (0, "pr view pr:o/r#7"), (5, "pr view pr:o/r#7")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gg.main(["audit", "--log", str(path)]), 0)
        text = out.getvalue()
        self.assertIn("4 gh calls logged", text, "every line is still counted")
        self.assertIn("pr:o/r#7 x2", text)
        self.assertNotIn("Bearer", text)
        self.assertNotIn("ghp_", text)

    def test_the_guards_own_line_shape_is_quoted_back(self) -> None:
        """The PATH guard writes `pr view 7 owner/repo` where this writes a resolved cache key:
        both are identifiers, and an audit that dropped one would under-report every Mac."""
        path = self.write_log([(0, "pr view 7 manaflow-ai/cmux"), (5, "pr view 7 manaflow-ai/cmux"),
                               (0, "run view 12 o/r"), (5, "run view 12 o/r")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gg.main(["audit", "--log", str(path)])
        self.assertIn("pr view 7 manaflow-ai/cmux x2", out.getvalue())
        self.assertIn("run view 12 o/r x2", out.getvalue())

    def test_a_read_outside_the_window_is_not_a_repeat(self) -> None:
        path = self.write_log([(0, "run view run:o/r/12"),
                               (int(gg.AUDIT_WINDOW) + 1, "run view run:o/r/12")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gg.main(["audit", "--log", str(path), "--json"])
        self.assertEqual(json.loads(out.getvalue())["repeatsWithin"]["calls"], 0)

    def test_a_line_stamped_before_the_one_it_follows_is_not_a_repeat(self) -> None:
        """Review of #1581: 00:03:20 then 00:00:00 counted 1 repeat, 200 s apart in time."""
        path = self.write_log([(200, "run view run:o/r/12"), (0, "run view run:o/r/12"),
                               (210, "api graphql")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gg.main(["audit", "--log", str(path), "--json"])
        self.assertEqual(json.loads(out.getvalue())["repeatsWithin"]["calls"], 0)

    def test_an_endpoint_that_is_not_a_path_is_never_printed(self) -> None:
        """Review of #1581: an old line's endpoint word can carry a query or a token."""
        for word in ("repos/o/r/pulls?access_token=ghp_abc", "ghp_0123456789abcdef", "a=b",
                     "repos/o/r/github_pat_11AB"):
            self.assertEqual(gg.audit_category(["api", word]), ("api (endpoint not shown)", False), word)
        self.assertEqual(gg.audit_category(["api", "repos/o/r/pulls"]), ("api repos/o/r/pulls", False))

    def test_a_decision_record_of_another_shape_is_skipped(self) -> None:
        """Review of #1581: a JSON list or a non-string route raised out of the audit."""
        path = self.write_log([(0, "pr view pr:o/r#7")])
        gg.broker_log().parent.mkdir(parents=True, exist_ok=True)
        gg.broker_log().write_text('[1]\n"x"\n{"route": ["serve"]}\nnull\n{"route": "serve"}\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gg.main(["audit", "--log", str(path), "--json"])
        self.assertEqual(json.loads(out.getvalue())["brokerDecisions"], {"serve": 1})

    def test_a_line_with_no_repository_is_counted_but_never_as_a_repeat(self) -> None:
        """The guard logged `pr view 7` before 2026-10-21, and still does where the repository
        came from -R. Two repositories' PR 7 are two reads, and a cache would have served
        neither: calling them a repeat would overstate what the broker can do."""
        path = self.write_log([(0, "pr view 7"), (10, "pr view 7"), (20, "pr view 7")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gg.main(["audit", "--log", str(path), "--json"])
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["cacheableReads"], 3)
        self.assertEqual(doc["repeatsWithin"]["calls"], 0)

    def test_it_reports_the_brokers_own_decisions(self) -> None:
        gg.broker_note({"route": "serve"})
        gg.broker_note({"route": "refuse"})
        gg.broker_note({"route": "serve"})
        path = self.write_log([(0, "pr view 7 o/r")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gg.main(["audit", "--log", str(path), "--json"])
        self.assertEqual(json.loads(out.getvalue())["brokerDecisions"], {"serve": 2, "refuse": 1})

    def test_an_empty_or_missing_log_says_so(self) -> None:
        with self.assertRaises(gg.WatchError):
            gg.cmd_audit(self.home / "nope.log", 10, False)
        empty = self.home / "empty.log"
        empty.write_text("")
        with self.assertRaises(gg.WatchError):
            gg.cmd_audit(empty, 10, False)


    def test_the_repeats_the_cache_answered_are_told_apart_from_those_it_did_not(self) -> None:
        log = self.write_log([(0, "pr view 7 o/r"), (10, "pr view 7 o/r"), (20, "pr view 7 o/r"),
                              (30, "run view 5 o/r"), (40, "run view 5 o/r")])
        gg.broker_log().parent.mkdir(parents=True, exist_ok=True)
        records = [(0, "pass", "pr view 7 o/r"), (10, "cache", "pr view 7 o/r"), (20, "pass", "pr view 7 o/r"),
                   (30, "pass", "run view 5 o/r"), (40, "cache", "run view 5 o/r"), (41, "pass", None),
                   (-500, "pass", "run view 5 o/r")]
        gg.broker_log().write_text("".join(json.dumps({"at": NOW + at, "route": route, **({"line": line} if line else {})})
                                           + "\n" for at, route, line in sorted(records)))
        doc = self.audit_json(log)
        self.assertEqual(doc["repeatsWithin"]["calls"], 3)
        self.assertEqual(doc["decisionReads"], {"reads": 5, "repeats": 3, "repeatsReachedGitHub": 1})
        self.assertIn("1 (20.0%) re-read within 90 seconds", self.audit_text(log))

    def test_a_call_through_the_broker_records_its_read(self) -> None:
        os.environ["GLAEDA_GH_CACHE_TTL"] = "90"
        args = ["pr", "view", "7", "-R", "o/r", "--json", "body"]
        self.run_gh(args)
        self.run_gh(args)
        self.run_gh(["pr", "comment", "7", "-R", "o/r", "--body", "x"])
        records = [json.loads(line) for line in gg.broker_log().read_text().splitlines()]
        self.assertEqual([(r["route"], r.get("line")) for r in records],
                         [("pass", "pr view pr:o/r#7"), ("cache", "pr view pr:o/r#7"), ("pass", None)])


class BrokerRepeatTest(BrokerBase):
    """The 30 s repeat delay lives here rather than in the PATH guard, because only this side knows
    whether a call is about to spend a request."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["CMUX_GH_MIN_REPEAT_SECONDS"] = str(gg.BROKER_MIN_REPEAT)
        self.slept: list[float] = []
        # broker_hold, not the clock: the daemon's own waits are built on time.sleep, and a test
        # that replaces that spins through every one of them.
        patch = mock.patch.object(gg, "broker_hold",
                                  lambda s: self.slept.append(s) if s > 0 else None)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_served_read_never_waits(self) -> None:
        """The whole point: the 22% of calls the cache answers were the ones the guard's delay hit
        hardest, and they spend nothing."""
        self.cache_pr(7, {"state": "OPEN"})
        args = ["pr", "view", "7", "-R", "o/r", "--json", "state"]
        for _ in range(3):
            code, out, _err, passed = self.run_gh(args)
            self.assertEqual((code, out, passed), (0, '{"state":"OPEN"}\n', None))
        self.assertEqual(self.slept, [])

    def test_the_same_read_twice_waits_before_spending_a_request(self) -> None:
        args = ["pr", "view", "9", "-R", "o/r", "--json", "state"]
        self.run_gh(args)
        self.assertEqual(self.slept, [], "the first read of it costs nothing extra")
        _code, _out, _err, passed = self.run_gh(args)
        self.assertEqual(len(self.slept), 1)
        self.assertGreater(self.slept[0], 25)
        self.assertLessEqual(self.slept[0], gg.BROKER_MIN_REPEAT)
        self.assertIsNotNone(passed, "it is held, then made: never refused")

    def test_a_different_caller_or_a_different_call_is_not_a_repeat(self) -> None:
        self.run_gh(["pr", "view", "9", "-R", "o/r", "--json", "state"])
        self.run_gh(["pr", "view", "10", "-R", "o/r", "--json", "state"])
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "another-session"}):
            self.run_gh(["pr", "view", "9", "-R", "o/r", "--json", "state"])
        self.assertEqual(self.slept, [])

    def test_a_write_is_never_delayed(self) -> None:
        for args in (["pr", "merge", "9", "--squash"], ["api", "repos/o/r/issues/9/comments", "-f", "body=hi"],
                     ["pr", "edit", "9", "--add-label", "x"]):
            with self.subTest(args=args):
                self.run_gh(args)
                self.run_gh(args)
        self.assertEqual(self.slept, [], "a merge held for 30 s is a lane held for 30 s")

    def test_a_read_that_spends_nothing_is_never_delayed(self) -> None:
        """`GET /rate_limit` costs neither pool -- this file says so itself (`request(free=True)`,
        audit_category's "rate_limit (free)"). Holding it for 30 s punished the session doing the
        right thing: reading the budget around a batch of calls. `--web` opens a browser.

        The GHE spelling too, since `BROKER_FREE_RE` pinned api.github.com: on a GHE remote -- a
        shape this file handles deliberately -- the free call was held 30 s by the brake for calls
        that spend the quota."""
        for args in (["api", "rate_limit"], ["api", "/rate_limit"],
                     ["api", "https://api.github.com/rate_limit"],
                     ["api", "https://ghe.example/api/rate_limit"],
                     # GHE's REST base is /api/v3/, so this is the one a GHE session really sends;
                     # with `/api/` alone it was held 29.9 s on the second call.
                     ["api", "https://ghe.example/api/v3/rate_limit"],
                     ["pr", "view", "9", "-R", "o/r", "--web"]):
            with self.subTest(args=args):
                self.run_gh(args)
                self.run_gh(args)
        self.assertEqual(self.slept, [])
        # The point of contrast: an api GET that does spend a request is still held -- including a
        # path *under* the free endpoint on a GHE host, so the widening is a spelling of one
        # endpoint and not a hole.
        for args in (["api", "repos/o/r/pulls/9"],
                     ["api", "https://ghe.example/api/rate_limit/history"],
                     ["api", "https://ghe.example/api/v3/rate_limit/history"]):
            with self.subTest(args=args):
                before = len(self.slept)
                self.run_gh(args)
                self.run_gh(args)
                self.assertEqual(len(self.slept), before + 1)

    def test_a_working_batched_graphql_read_is_never_held_in_any_shape(self) -> None:
        """The caller that batches is the one this must not punish, and it is the one the delay
        would hit hardest: a card that reads forty PRs in one query has one command line, so a
        refresh loop repeats it exactly. It is exempt because gh POSTs a GraphQL query, and the
        method is read from the whole command line -- `-f`, `-F`, `--field`, `--raw-field` and
        `--input` each mean POST with no `-X` on the line. Written as a test because `--input -`
        takes its query from *stdin*: two such calls are one digest here and different queries at
        GitHub, so if any of these shapes were read as a GET, the second would be held 30 s for a
        read the first never made. Asked for by the pr-cards lane, whose path this is.

        *Working* shapes only, which is what the name promises. GitHub's GraphQL endpoint is
        POST-only, so `gh api graphql` with no fields, or with `-X GET`, gets a 404 rather than an
        answer and is held like any other repeated GET -- and `-X GET -f a=b` meaning GET is
        pinned deliberately by `test_fields_or_a_body_make_it_a_post_without_minus_x`. The
        exemption is also incidental: nothing in `broker_repeat_wait` knows the word `graphql`, it
        falls out of the method. What this pins is that every way of writing a working one lands
        on the POST side of that inference."""
        for args in (["api", "graphql", "-f", "query=query{viewer{login}}"],
                     ["api", "graphql", "-F", "query=query{viewer{login}}"],
                     ["api", "graphql", "--field", "query=query{viewer{login}}"],
                     ["api", "graphql", "--raw-field", "query=query{viewer{login}}"],
                     ["api", "graphql", "--input", "-"],
                     ["api", "/graphql", "-f", "query=query{viewer{login}}"]):
            with self.subTest(args=args):
                for _ in range(3):
                    _code, _out, _err, passed = self.run_gh(args)
                    self.assertEqual(passed, args, "a batched read must reach gh unchanged")
                self.assertEqual(self.slept, [], "a batching caller was held for repeating itself")

    def test_the_delay_can_be_turned_off(self) -> None:
        with mock.patch.dict(os.environ, {"CMUX_GH_MIN_REPEAT_SECONDS": "0"}):
            self.run_gh(["pr", "view", "9", "-R", "o/r", "--json", "state"])
            self.run_gh(["pr", "view", "9", "-R", "o/r", "--json", "state"])
        self.assertEqual(self.slept, [])

    def test_the_probe_says_the_whole_contract_is_here(self) -> None:
        """A wrapper in front of gh delegates only to a broker that passes this, because exec'ing
        an older glaeda-gh -- which answers `gh` with an argparse error -- would break every gh
        call on the host."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gg.main(["gh", "--probe"]), 0)
        self.assertEqual(out.getvalue(), f"broker {gg.BROKER_CONTRACT}\n")


class BrokerHoldTest(Base):
    """The hold itself, outside BrokerRepeatTest, which replaces it."""

    def test_the_hold_says_what_it_is_waiting_for(self) -> None:
        err = io.StringIO()
        with mock.patch.object(gg.time, "sleep") as slept, contextlib.redirect_stderr(err):
            gg.broker_hold(0.0)
            self.assertEqual(slept.call_count, 0, "nothing to wait for, nothing printed")
            self.assertEqual(err.getvalue(), "")
            gg.broker_hold(7.0)
            slept.assert_called_once_with(7.0)
        self.assertIn("waiting 7 s", err.getvalue())
        self.assertIn("CMUX_GH_MIN_REPEAT_SECONDS", err.getvalue())


class BrokerFieldTableTest(unittest.TestCase):
    """The rule behind BROKER_PR_FIELDS and BROKER_RUN_FIELDS: a field may be served only when
    every source the daemon reads it from actually fills it.

    That rule was prose, and prose let `reviewDecision`, `displayTitle`, `createdAt` and
    `headBranch` into the tables -- each of which printed a confident "" or null wherever the
    source it came from does not carry the field, which is not what GitHub answering null means.
    Pinning it here is what makes a later edit to a writer break a test rather than a lane: the
    run writers in particular read records defined in another repository (the build controller's
    feed, cmuxterm-hq#675), so a field could stop arriving without a line of this file changing.
    """

    FULL_CONTROLLER_RUN = {"id": 12, "name": "CI", "status": "completed", "conclusion": "success",
                           "url": "https://github.com/o/r/actions/runs/12", "head_sha": "a" * 40,
                           "head_branch": "main", "event": "push", "attempt": 2,
                           "updated_at": "2026-10-21T00:00:00Z"}
    FULL_GRAPHQL_RUN = {"databaseId": 12, "workflow": {"name": "CI"}, "displayTitle": "a fix",
                        "url": "https://github.com/o/r/actions/runs/12", "event": "push",
                        "runAttempt": 2, "createdAt": "2026-10-21T00:00:00Z",
                        "updatedAt": "2026-10-21T00:00:00Z",
                        "checkSuite": {"status": "COMPLETED", "conclusion": "SUCCESS",
                                       "commit": {"oid": "a" * 40}, "branch": {"name": "main"}}}
    FULL_REST_RUN = {"id": 12, "name": "CI", "display_title": "a fix", "status": "completed",
                     "conclusion": "success", "html_url": "https://github.com/o/r/actions/runs/12",
                     "head_sha": "a" * 40, "head_branch": "main", "event": "push", "run_attempt": 2,
                     "created_at": "2026-10-21T00:00:00Z", "updated_at": "2026-10-21T00:00:00Z"}

    def run_writers(self) -> dict[str, dict]:
        """Every writer of a run cache entry, each handed a fully populated source record."""
        return {"controller_run": gg.controller_run(self.FULL_CONTROLLER_RUN),
                "graphql_run": gg.graphql_run(self.FULL_GRAPHQL_RUN),
                "slim_run": gg.slim_run(self.FULL_REST_RUN)}

    def pr_writers(self) -> dict[str, dict]:
        """Both writers of a PR cache entry. The REST one is given review=None on purpose: that is
        a host that has only ever read this PR over REST, which has no reviewDecision at all."""
        node = pr_node(7, state="MERGED")
        node["mergedAt"] = "2026-10-21T00:00:00Z"
        node["headRefName"] = "a-branch"
        node["reviewDecision"] = "APPROVED"
        pull = {"number": 7, "html_url": node["url"], "title": "t", "state": "closed", "merged": True,
                "merged_at": node["mergedAt"], "draft": False, "mergeable": True,
                "mergeable_state": "clean", "updated_at": node["updatedAt"],
                "head": {"sha": node["headRefOid"], "ref": node["headRefName"]}}
        return {"graphql": gg.parse_pr(node),
                "rest": gg.parse_pr(gg.rest_pr_node(pull, {}, [], [], set(), None, {}))}

    def test_every_served_field_is_one_every_writer_fills(self) -> None:
        for table, writers in ((gg.BROKER_RUN_FIELDS, self.run_writers()),
                               (gg.BROKER_PR_FIELDS, self.pr_writers())):
            for field, (source, _kind) in table.items():
                for name, written in writers.items():
                    with self.subTest(field=field, writer=name):
                        self.assertIn(source, written, f"{name} has no {source}")
                        self.assertIsNotNone(
                            written[source],
                            f"{name} leaves {source} empty, so serving {field} from it would be a "
                            f"confident wrong answer: take the field out of the table")

    def test_a_field_a_writer_cannot_carry_is_kept_out_of_the_table(self) -> None:
        """The other direction, so the first test cannot be satisfied by an empty table: the keys
        the writers do leave empty are exactly the ones these tables leave out."""
        for table, writers in ((gg.BROKER_RUN_FIELDS, self.run_writers()),
                               (gg.BROKER_PR_FIELDS, self.pr_writers())):
            empty = {key for written in writers.values() for key, value in written.items() if value is None}
            served = {source for source, _kind in table.values()}
            self.assertTrue(empty, "a writer that fills everything would make this vacuous")
            self.assertEqual(empty & served, set())

    def test_the_fields_a_lane_pivots_on_are_all_live(self) -> None:
        """Whatever is served has to be in one table or the other; whatever moves the moment the
        PR or run does has to be in BROKER_LIVE_FIELDS, or the lean horizon covers it for ten
        minutes. These are the ones automation acts on."""
        served = set(gg.BROKER_PR_FIELDS) | set(gg.BROKER_RUN_FIELDS)
        self.assertLessEqual(gg.BROKER_LIVE_FIELDS, served, "a live field nothing serves is dead weight")
        for field in ("state", "mergedAt", "isDraft", "updatedAt", "mergeable", "mergeStateStatus",
                      "headRefOid", "status", "conclusion", "attempt"):
            self.assertIn(field, gg.BROKER_LIVE_FIELDS, field)

    def test_every_served_field_is_classified_live_or_stable(self) -> None:
        """The assertion above names the ten fields that are live today, so it says nothing about
        the eleventh. A review added `merged` to BROKER_PR_FIELDS and `checksTotal` to it -- both
        mutable, both filled by every writer, neither classified -- and all 91 broker tests stayed
        green while a merged PR was served as open from a ten-minute-old entry. The obligation has
        to be on the tables, not on a list of names: a new served field belongs to exactly one of
        the two sets, and adding it without deciding which fails here."""
        served = set(gg.BROKER_PR_FIELDS) | set(gg.BROKER_RUN_FIELDS)
        self.assertEqual(served, gg.BROKER_LIVE_FIELDS | gg.BROKER_STABLE_FIELDS,
                         "a served field is live or stable; this one is neither")
        self.assertEqual(gg.BROKER_LIVE_FIELDS & gg.BROKER_STABLE_FIELDS, set(),
                         "a field cannot be both")

    def test_a_stable_field_is_not_one_a_lane_could_act_on(self) -> None:
        """What 'stable' is allowed to mean: fixed for the life of the object, or a value whose
        being a few minutes old is not an answer anything acts on. A field that decides a merge,
        a rerun or a wait is live, whatever its name suggests."""
        for field in ("state", "mergeable", "mergeStateStatus", "conclusion", "status",
                      "headRefOid", "mergedAt", "isDraft"):
            self.assertNotIn(field, gg.BROKER_STABLE_FIELDS, field)
        self.assertEqual(gg.BROKER_STABLE_FIELDS - {"number", "url", "title", "headRefName",
                                                    "databaseId", "headSha", "event"}, set(),
                         "a new stable field needs the comment above it to say why it is stable")


class BrokerLogTest(BrokerBase):
    def test_a_host_without_the_path_guard_writes_the_guards_log_itself(self) -> None:
        self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        line = Path(os.environ["CMUX_GH_LOG"]).read_text().strip()
        self.assertRegex(line, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \S+ pr view pr:o/r#7$")

    def test_a_host_with_the_guard_does_not_log_the_call_twice(self) -> None:
        with mock.patch.dict(os.environ, {"CMUX_GH_GUARD_LOGGED": "1"}):
            self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        self.assertFalse(Path(os.environ["CMUX_GH_LOG"]).exists())

    def test_the_log_is_not_readable_by_other_accounts_on_the_host(self) -> None:
        """Credentials are stripped, but the line is still every repository, PR, run and API path
        every session on this host reads, plus the caller's session id. Every other state path
        here is 0700; this one was whatever the umask said."""
        log = self.home / "state" / "cmux-gh" / "calls.log"  # as on a host that has neither yet
        with mock.patch.dict(os.environ, {"CMUX_GH_LOG": str(log)}):
            self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        self.assertTrue(log.exists())
        self.assertEqual(stat.S_IMODE(log.parent.stat().st_mode) & 0o077, 0,
                         "the directory holding the shared call log")

    def test_the_caller_slot_cannot_be_anything_but_a_caller(self) -> None:
        """The caller comes from the environment and goes into a line-oriented log that
        AUDIT_LINE reads as three whitespace-separated fields. The subcommand slot is shape
        checked; leaving this one open would only move the hole."""
        log = Path(os.environ["CMUX_GH_LOG"])
        for bad in ("a b", "has\nnewline", "x" * 200, "", "ghp_" + "z" * 36 + " "):
            log.unlink(missing_ok=True)
            with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": bad}):
                self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
            written = log.read_text()
            self.assertEqual(len(written.splitlines()), 1, repr(bad))
            self.assertRegex(written, r"^\S+ ppid-\d+ pr view pr:o/r#7\n$", repr(bad))
        log.unlink(missing_ok=True)
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "session-1.2:3"}):
            self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        self.assertIn(" session-1.2:3 ", log.read_text(), "a real session id is kept as it is")

    def test_a_query_string_never_reaches_the_log(self) -> None:
        self.run_gh(["api", "repos/o/r/actions/runs?head_sha=" + "d" * 40])
        self.assertEqual(Path(os.environ["CMUX_GH_LOG"]).read_text().split()[2:],
                         ["api", "repos/o/r/actions/runs"])

    def test_a_secret_in_a_positional_slot_never_reaches_the_log(self) -> None:
        """The log is a long-lived plain file every session appends to (117 MB on cosmic-gray).
        `gh secret set NAME --body VALUE` and `gh pr create --title T BODY` both put text worth
        hiding where an unparsed command line's tail would be, so only an identifier is kept."""
        token = "ghp_" + "z" * 36
        for args, want in ((["secret", "set", "MY_TOKEN", token], "secret set"),
                           (["variable", "set", "V", "another-secret"], "variable set"),
                           (["pr", "create", "--title", "t", "a body nobody asked to keep"], "pr create"),
                           (["config", "set", "oauth_token", token], "config set"),
                           (["api", "repos/o/r/issues/7/comments", "-f", f"body=here is {token}"],
                            "api repos/o/r/issues/7/comments")):
            with self.subTest(args=args):
                Path(os.environ["CMUX_GH_LOG"]).unlink(missing_ok=True)
                self.run_gh(args)
                text = Path(os.environ["CMUX_GH_LOG"]).read_text()
                # the whole line, not just "the token is absent": what is kept is exactly the
                # subcommand and, for an api call, its path
                self.assertRegex(text, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \S+ " + re.escape(want) + "\n$")

    def test_a_credential_is_dropped_however_it_reached_the_line(self) -> None:
        """A backstop under the parse, which is a parse of someone else's command line: a flag gh
        adds next year, or one that is boolean in one subcommand and value-taking in another, can
        desynchronise it, and the log must not be where that shows up."""
        token = "github_pat_" + "A" * 30
        self.assertEqual(gg.BROKER_SECRET_RE.sub("[redacted]", f"api repos/o/{token}"),
                         "api repos/o/[redacted]")
        self.run_gh(["api", f"repos/o/{token}/contents/x"])
        text = Path(os.environ["CMUX_GH_LOG"]).read_text()
        self.assertNotIn(token, text)
        self.assertIn("[redacted]", text)

    def test_a_newline_in_a_subcommand_slot_cannot_forge_a_log_line(self) -> None:
        forged = "2026-10-21T00:00:00Z other-session pr view pr:o/r#1"
        self.run_gh(["pr", f"view\n{forged}", "7"])
        text = Path(os.environ["CMUX_GH_LOG"]).read_text()
        self.assertEqual(len(text.splitlines()), 1, text)
        self.assertNotIn("other-session", text)
        # `view\n...` is not a subcommand word, so the head stops at `pr`. What may follow is an
        # identifier, and whether `7` resolves to one depends on the checkout this runs in: under
        # CI the cwd is a clone with one GitHub remote, in a worktree with two it is not.
        # Tight on what may follow: `\S+` would let a regression put anything at all there.
        self.assertRegex(text, r"^\S+ \S+ pr(?: pr:[\w.-]+/[\w.-]+#\d+)?\n$")

    def test_a_logged_read_carries_its_repository(self) -> None:
        """`pr view 7` in two repositories is two reads; counting them as one would overstate
        what a cache can do, and the audit's numbers are what justifies the broker."""
        self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        self.run_gh(["pr", "view", "7", "-R", "other/elsewhere", "--json", "state"])
        logged = [line.split()[2:] for line in Path(os.environ["CMUX_GH_LOG"]).read_text().splitlines()]
        self.assertEqual(logged, [["pr", "view", "pr:o/r#7"], ["pr", "view", "pr:other/elsewhere#7"]])


class BrokerLoopTest(BrokerBase):
    """Nothing the broker does may become an endless exec between two wrappers. Every gh call on
    every machine goes through here."""

    def test_a_call_is_marked_so_it_is_never_brokered_twice(self) -> None:
        self.cache_pr(7, {"state": "OPEN"})
        with mock.patch.dict(os.environ, {gg.BROKER_MARK: "1"}):
            _code, out, _err, passed = self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"],
                                                   keep_mark=True)
        self.assertEqual(out, "", "a second pass would have served it")
        self.assertIsNotNone(passed)

    def test_the_mark_is_set_before_handing_the_call_on(self) -> None:
        self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        self.assertEqual(os.environ.get(gg.BROKER_MARK), f"1:{os.getpid()}")
        self.assertLessEqual(gg.broker_hops(), gg.BROKER_MAX_HOPS)

    def test_a_gh_that_runs_gh_is_a_new_call_rather_than_another_hop(self) -> None:
        """gh extensions, `gh alias set x '!... gh ...'` and a `gh pr create` whose $EDITOR runs gh
        all spawn gh from gh. The mark is carried in the environment, which those inherit, so a
        bare count made the third generation exit 127 blaming a PATH loop that does not exist --
        and made the second generation invisible: not served, not refused, not even logged.
        `exec` keeps the pid and a spawn does not, which is the difference the mark records."""
        self.assertEqual(gg.broker_hops(), 0, "no mark")
        with mock.patch.dict(os.environ, {gg.BROKER_MARK: f"2:{os.getpid()}"}):
            self.assertEqual(gg.broker_hops(), 2, "this call's own history, carried across an exec")
        with mock.patch.dict(os.environ, {gg.BROKER_MARK: "2:999999"}):
            self.assertEqual(gg.broker_hops(), 0, "another process's mark is not this call's")
        # End to end: a descendant of a brokered gh is brokered, served and logged like any other.
        self.cache_pr(7, {"state": "OPEN"})
        with mock.patch.dict(os.environ, {gg.BROKER_MARK: "1:999999"}):
            code, out, _err, passed = self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"],
                                                  keep_mark=True)
        self.assertEqual((code, out, passed), (0, '{"state":"OPEN"}\n', None))
        self.assertIn("pr view", (self.home / "calls.log").read_text())

    def test_a_wrapper_that_forks_gh_instead_of_execing_it_still_ends(self) -> None:
        """The pid-scoped mark fixed a spawned `gh` being refused for a loop that did not exist,
        but it is scoped precisely so a new pid starts at zero -- and a wrapper that *forks* `gh`
        gets a new pid every generation, so the scoped count resets to 1 forever. That turned one
        call exiting 127 into a host forking until something kills it, with a call-log line per
        generation. BROKER_DEPTH is unscoped for exactly this shape."""
        self.assertEqual(gg.broker_depth(), 0)
        with mock.patch.dict(os.environ, {gg.BROKER_DEPTH: str(gg.BROKER_MAX_DEPTH)}):
            # a fork resets the pid, so hops is 0 here -- depth is what has to end it
            with mock.patch.dict(os.environ, {gg.BROKER_MARK: "1:999999"}):
                self.assertEqual(gg.broker_hops(), 0)
                code, _out, err, passed = self.run_gh(["pr", "view", "7", "-R", "o/r"], keep_mark=True)
        self.assertEqual(code, gg.BROKER_EXIT_NO_GH)
        self.assertIsNone(passed, "the chain ends instead of forking another generation")
        self.assertIn(gg.BROKER_DEPTH, err)
        # and the depth is carried on, unscoped, so the next generation counts from here
        with mock.patch.dict(os.environ, {gg.BROKER_DEPTH: "3"}):
            self.run_gh(["pr", "view", "7", "-R", "o/r"], keep_mark=True)
            self.assertEqual(os.environ.get(gg.BROKER_DEPTH), "4")

    def test_a_real_extension_chain_is_well_under_the_depth_limit(self) -> None:
        # The bound only exists to end a loop. A gh extension that calls gh, whose $EDITOR calls
        # gh, is three deep; it must never see this.
        self.assertGreaterEqual(gg.BROKER_MAX_DEPTH, 6)
        self.cache_pr(7, {"state": "OPEN"})
        for depth in range(gg.BROKER_MAX_DEPTH - 1):
            with mock.patch.dict(os.environ, {gg.BROKER_DEPTH: str(depth)}):
                code, out, _err, _passed = self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
                self.assertEqual((code, out), (0, '{"state":"OPEN"}\n'), f"depth {depth}")

    def test_an_unreadable_depth_ends_the_chain_rather_than_extending_it(self) -> None:
        with mock.patch.dict(os.environ, {gg.BROKER_DEPTH: "not-a-number"}):
            self.assertEqual(gg.broker_depth(), gg.BROKER_MAX_DEPTH)

    def test_the_brokers_hand_back_does_not_disable_the_persons_own_switch(self) -> None:
        """The broker used to set CMUX_GH_BROKER_DISABLE=1 for the hand-back, which every
        descendant inherited -- so the broker was off for a whole subtree, and once inherited, a
        person's own export of it did nothing. The hand-back is GLAEDA_GH_BROKER_PID's job."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CMUX_GH_BROKER_DISABLE", None)
            self.run_gh(["pr", "view", "7", "-R", "o/r"])
            self.assertIsNone(os.environ.get("CMUX_GH_BROKER_DISABLE"))
            self.assertEqual(os.environ.get("GLAEDA_GH_BROKER_PID"), str(os.getpid()))

    def test_an_older_bare_mark_still_reads_as_a_hop(self) -> None:
        # Mid-rollout a host can have a broker that wrote the count with no pid.
        with mock.patch.dict(os.environ, {gg.BROKER_MARK: "1"}):
            self.assertEqual(gg.broker_hops(), 1)
        with mock.patch.dict(os.environ, {gg.BROKER_MARK: "not-a-number"}):
            self.assertEqual(gg.broker_hops(), 1, "unreadable ends the chain sooner, never later")

    def test_the_hop_count_ends_a_chain_no_mark_could_stop(self) -> None:
        """The mark stops the broker working twice on one call; it cannot stop a wrapper handing
        the call back, and `exec` leaves nothing to unwind. Counting hops is what makes it finite:
        without this, a `gh` on PATH that runs `gh` again is a hang with nothing in any log."""
        args = ["pr", "view", "7", "-R", "o/r", "--json", "state"]
        with mock.patch.dict(os.environ, {gg.BROKER_MARK: str(gg.BROKER_MAX_HOPS)}):
            code, _out, err, passed = self.run_gh(args, keep_mark=True)
        self.assertEqual(code, gg.BROKER_EXIT_NO_GH)
        self.assertIsNone(passed, "running it again is what loops")
        self.assertIn("hands `gh` back here", err)
        with mock.patch.dict(os.environ, {gg.BROKER_MARK: "1"}):
            _code, _out, _err, passed = self.run_gh(args, keep_mark=True)
            self.assertIsNotNone(passed, "one hop is a wrapper, not a loop")
            self.assertEqual(os.environ.get(gg.BROKER_MARK), f"2:{os.getpid()}",
                         "the next pass knows it is the last")

    def test_a_mark_that_is_not_a_number_counts_as_a_hop(self) -> None:
        with mock.patch.dict(os.environ, {gg.BROKER_MARK: "yes"}):
            self.assertEqual(gg.broker_hops(), 1)
        with mock.patch.dict(os.environ, {gg.BROKER_MARK: "1"}):
            self.assertEqual(gg.broker_hops(), 1, "older versions wrote 1")

    def test_the_path_guard_is_told_not_to_hand_the_call_back(self) -> None:
        """By pid, not by a blanket switch: GLAEDA_GH_BROKER_PID names the process that has been
        through here, and the guard compares it to its own $$. CMUX_GH_BROKER_DISABLE is left
        alone because it belongs to a person -- setting it here turned the broker off for every
        descendant of the call, and once inherited, a person's own export of it did nothing."""
        os.environ.pop("CMUX_GH_BROKER_DISABLE", None)
        self.addCleanup(os.environ.pop, "CMUX_GH_BROKER_DISABLE", None)
        self.addCleanup(os.environ.pop, "GLAEDA_GH_BROKER_PID", None)
        self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        self.assertEqual(os.environ.get("GLAEDA_GH_BROKER_PID"), str(os.getpid()))
        self.assertIsNone(os.environ.get("CMUX_GH_BROKER_DISABLE"))

    def test_a_guard_that_names_a_wrapper_as_the_real_gh_is_not_believed(self) -> None:
        """Mid-rollout a host has two generations of the PATH guard on PATH, and the older one's
        idea of the real gh is the newer one. Taking its word for it hands the call straight back."""
        other = self.home / "guard-b"
        other.write_text("#!/usr/bin/env bash\n# gh guard: an older generation\nexit 9\n")
        other.chmod(0o755)
        real = self.home / "bin" / "gh"
        with mock.patch.dict(os.environ, {"GLAEDA_GH_REAL": str(other),
                                          "PATH": str(self.home / "bin")}):
            self.assertEqual(gg.broker_real_gh(), str(real))

    def test_a_wrapper_on_path_is_stepped_over_rather_than_run(self) -> None:
        shim = self.home / "shim"
        shim.mkdir()
        (shim / "gh").write_text(f"#!/bin/sh\n# {gg.BROKER_MARK} shim\nexit 9\n")
        (shim / "gh").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{shim}:{self.home / 'bin'}"}):
            os.environ.pop("GLAEDA_GH_REAL", None)
            self.assertEqual(gg.broker_real_gh(), str(self.home / "bin" / "gh"))

    def test_a_gh_this_account_cannot_read_is_not_mistaken_for_a_wrapper(self) -> None:
        """A wrapper is recognised by reading it. A gh that cannot be read is a binary, or a file
        this account may execute but not read -- not a wrapper. Treating it as one stepped over a
        perfectly runnable gh, and with no fallback path on the host the broker then became the
        reason the call failed, which is the one thing it may never be."""
        opaque = self.home / "bin" / "opaque-gh"
        opaque.write_text("#!/bin/sh\nexit 0\n")
        opaque.chmod(0o311)  # executable, not readable
        self.assertFalse(gg.broker_is_wrapper(opaque))
        with mock.patch.object(gg.Path, "open", side_effect=OSError("permission denied")):
            self.assertFalse(gg.broker_is_wrapper(opaque))

    def test_a_lane_vm_uses_its_credential_chain_not_the_unauthenticated_gh(self) -> None:
        """On a lane VM, `glaeda-gh gh ...` with no GLAEDA_GH_REAL stepped over the lane wrapper and ran the
        distribution's /usr/bin/gh, signed in to nothing ("please run: gh auth login"; gcp-codex-lane-1,
        2026-11-01). The credential chain behind the wrapper is the real gh there."""
        wrapper_dir = self.home / ".local/bin"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        (wrapper_dir / "gh").write_text("#!/bin/sh\n# gh guard: lane VM broker chain\nexit 9\n")
        (wrapper_dir / "gh").chmod(0o755)
        chain = self.home / ".local/lib/lane-gh/bin/gh"
        chain.parent.mkdir(parents=True)
        chain.write_text("#!/bin/sh\nexec lane-gh-credential gh \"$@\"\n")
        chain.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{wrapper_dir}:{self.home / 'bin'}"}), \
                mock.patch.object(gg.Path, "home", staticmethod(lambda: self.home)):
            os.environ.pop("GLAEDA_GH_REAL", None)
            self.assertEqual(gg.broker_real_gh(), str(chain))
            with mock.patch.dict(os.environ, {"GLAEDA_GH_REAL": str(self.home / "bin" / "gh")}):
                self.assertEqual(gg.broker_real_gh(), str(self.home / "bin" / "gh"), "a named real gh still wins")
            chain.unlink()
            self.assertEqual(gg.broker_real_gh(), str(self.home / "bin" / "gh"), "off a lane VM nothing changes")

    def test_a_gh_nowhere_on_path_is_still_found_where_it_is_installed(self) -> None:
        """A LaunchAgent's PATH is nearly empty; /usr/bin/gh is where the host actually has it."""
        installed = self.home / ".local/bin" / "gh"
        installed.parent.mkdir(parents=True)
        installed.write_text("#!/bin/sh\nexit 0\n")
        installed.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": ""}):
            os.environ.pop("GLAEDA_GH_REAL", None)
            with mock.patch.object(gg.Path, "home", staticmethod(lambda: self.home)):
                found = gg.broker_real_gh()
        self.assertEqual(found, str(installed))


class BrokerReviewTest(BrokerBase):
    """What a second independent review of the broker found by running it, 2026-10-21."""

    def test_only_a_real_remote_puts_a_repository_in_the_local_index(self) -> None:
        """The remotes were regexed out of the config text, so a `[submodule]` url, an `[url]`
        insteadOf rewrite rule and a commented-out line all counted as remotes. On big-red that
        indexed 81 repositories -- every submodule and vendored dependency of the cmux checkout --
        so `gh api /repos/manaflow-ai/homebrew-cmux/contents/Formula/cmux.rb` was refused and the
        reader was pointed at a cmux worktree whose submodules are not initialised, where the
        advised `git show` answers `path does not exist`. It also meant the cmux checkout looked
        like it had ten remotes, so the repository the fleet reads most was never served."""
        where = self.git_checkout(
            self.home / "roots" / "cmux",
            '[remote "origin"]\n'
            "\turl = git@github.com:manaflow-ai/cmux.git\n"
            '[submodule "ghostty"]\n'
            "\turl = https://github.com/manaflow-ai/ghostty\n"
            '[submodule "vendor/fmt"]\n'
            "\turl = https://github.com/fmtlib/fmt.git\n"
            '[url "ssh://git@github.com/acme/mirror"]\n'
            "\tinsteadOf = https://gitlab.com/acme/mirror\n"
            "# url = https://github.com/o/oldname\n"
            "; url = https://github.com/o/older\n")
        self.assertEqual(gg.broker_remotes(where), ["manaflow-ai/cmux"])
        self.assertEqual(sorted(gg.broker_local_repos(now=time.time())), ["manaflow-ai/cmux"])
        # and so the refusal only speaks for the one repository that is really here
        code, _out, _err, passed = self.run_gh(["api", "repos/manaflow-ai/ghostty/contents/x"])
        self.assertIsNone(code, "a submodule's URL is not a checkout of it")
        self.assertIsNotNone(passed)

    def test_a_checkout_with_a_fork_and_an_upstream_is_still_resolved(self) -> None:
        """Two real remotes stay two: the one-remote rule is what makes a served answer safe, and
        the fix must not buy its accuracy by making every checkout look single-remote."""
        where = self.git_checkout(
            self.home / "roots" / "cli",
            '[remote "origin"] url = git@github.com:me/cli.git\n'
            '[remote "upstream"]\n\turl = https://github.com/cli/cli\n')
        self.assertEqual(gg.broker_remotes(where), ["me/cli", "cli/cli"])
        with mock.patch.object(gg.Path, "cwd", staticmethod(lambda: where)):
            self.assertIsNone(gg.broker_repo({}), "two remotes is a guess, so it declines")

    def test_a_url_a_remote_section_does_not_own_is_not_a_remote(self) -> None:
        for config, want in (
                ('[remote "origin"]\n\tpushurl = git@github.com:o/push.git\n', []),
                ('[remote "origin"]\n\turl = git@github.com:o/r.git # a comment\n', ["o/r"]),
                ('[remote "origin"]\n\turl = "git@github.com:o/r.git"\n', ["o/r"]),
                ('[remote "origin"] url = git@github.com:o/r.git\n', ["o/r"]),
                ("[REMOTE \"origin\"]\n\tURL = git@github.com:o/r.git\n", ["o/r"]),
                ('[remote "origin"]\n\turl = https://gitlab.com/o/r.git\n', []),
                ('[branch "main"]\n\tremote = origin\n\turl = github.com/o/wrong\n', [])):
            with self.subTest(config=config):
                where = self.home / "roots" / "probe"
                (where / ".git").mkdir(parents=True, exist_ok=True)
                (where / ".git" / "config").write_text(config)
                self.assertEqual(gg.broker_remotes(where), want)

    def test_the_refusal_says_nothing_about_another_github_host(self) -> None:
        """`repos/acme/thing` on an enterprise host is not `acme/thing` on github.com.
        broker_repo() always declined these; the refusal did not, so it offered a github.com
        clone's bytes as the enterprise repository's."""
        self.checkout("acme/thing")
        args = ["api", "repos/acme/thing/contents/README.md"]
        for extra, env in ((["--hostname", "ghe.example.com"], {}),
                           ([], {"GH_HOST": "ghe.example.com"}),
                           ([], {"GH_ENTERPRISE_TOKEN": "x"}),
                           ([], {"GITHUB_ENTERPRISE_TOKEN": "x"})):
            with self.subTest(extra=extra, env=env):
                with mock.patch.dict(os.environ, env):
                    code, _out, _err, passed = self.run_gh([*args, *extra])
                self.assertIsNone(code, "refused a repository it cannot know is the same one")
                self.assertEqual(passed, [*args, *extra])
        # github.com itself is unaffected, named or not
        for extra in ([], ["--hostname", "github.com"], ["--hostname", "api.github.com"]):
            with self.subTest(extra=extra):
                code, _out, _err, _passed = self.run_gh([*args, *extra])
                self.assertEqual(code, gg.BROKER_EXIT_REFUSED)

    def test_a_held_lock_is_never_the_reason_gh_does_not_run(self) -> None:
        """Three read-modify-writes took LOCK_EX with no deadline, on paths every gh call on the
        host touches. With `.broker-seen.lock` held by a stuck process, `gh pr view 7 -R o/r
        --json state` was still blocked past a 20 s kill, having never reached the real gh. What
        these protect is bookkeeping; a gh call is not."""
        gg.BASE.mkdir(parents=True, exist_ok=True)
        # A non-zero delay for the repeat lock: with CMUX_GH_MIN_REPEAT_SECONDS=0 the function
        # returns before it ever opens the lock, so the subtest would pass against the unbounded
        # version. (It did; the mutation found it.)
        for name, call in ((".broker-seen.lock", lambda: gg.broker_seen_count("pr:o/r#7", time.time())),
                           (".broker-repeat.lock",
                            lambda: gg.broker_repeat_wait(["api", "repos/o/r/pulls"],
                                                          ["api", "repos/o/r/pulls"],
                                                          "GET", set(), time.time())),
                           (".register.lock", lambda: gg.register("pr:o/r#7"))):
            with self.subTest(lock=name):
                path = gg.BASE / name if name != ".register.lock" else gg.watch_dir() / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a+") as holder:
                    fcntl.flock(holder, fcntl.LOCK_EX)  # stands in for a wedged sibling
                    began = time.monotonic()
                    with mock.patch.dict(os.environ, {"CMUX_GH_MIN_REPEAT_SECONDS": "30"}):
                        call()
                    waited = time.monotonic() - began
                self.assertLess(waited, gg.LOCK_WAIT + 2.0, f"{name} waited {waited:.1f}s")

    def test_a_repeat_delay_is_still_applied_when_the_lock_is_free(self) -> None:
        # The other half: giving up on a held lock must not mean giving up on the bookkeeping.
        with mock.patch.dict(os.environ, {"CMUX_GH_MIN_REPEAT_SECONDS": "30"}):
            args = ["api", "repos/o/r/pulls"]
            words, method, seen, _values = gg.broker_words(args)
            now = time.time()
            self.assertEqual(gg.broker_repeat_wait(args, words, method, seen, now), 0.0)
            self.assertGreater(gg.broker_repeat_wait(args, words, method, seen, now + 1), 0.0)

    def test_the_horizon_override_cannot_cover_a_live_field(self) -> None:
        """GLAEDA_GH_MAX_AGE was read before the live-field rule, so one export in a shell profile
        silently turned off the only protection the merge path has: a two-hour-old
        `{"state":"MERGED","mergeable":"MERGEABLE"}` was served as the answer."""
        for value in ("999999", "inf", "1e9"):
            with self.subTest(GLAEDA_GH_MAX_AGE=value):
                with mock.patch.dict(os.environ, {"GLAEDA_GH_MAX_AGE": value}):
                    self.assertEqual(gg.broker_max_age(["state"]), gg.BROKER_MAX_AGE)
                    self.assertEqual(gg.broker_max_age(["conclusion"]), gg.BROKER_MAX_AGE)
                    self.assertGreater(gg.broker_max_age(["title"]), gg.BROKER_MAX_AGE)
        self.cache_pr(8, {"state": "MERGED"}, age=7200)
        with mock.patch.dict(os.environ, {"GLAEDA_GH_MAX_AGE": "999999"}):
            _code, out, _err, passed = self.run_gh(["pr", "view", "8", "-R", "o/r", "--json", "state"])
        self.assertEqual(out, "", "a two-hour-old state is not an answer")
        self.assertIsNotNone(passed)

    def test_a_persons_switch_hands_the_call_straight_to_the_real_gh(self) -> None:
        """The fatal loop message advertises CMUX_GH_BROKER_DISABLE=1, and nothing here read it:
        the only documented way out of a wedged chain was a no-op."""
        self.cache_pr(7, {"state": "OPEN"})
        with mock.patch.dict(os.environ, {"CMUX_GH_BROKER_DISABLE": "1"}):
            code, out, _err, passed = self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
            self.assertEqual((code, out), (None, ""), "served nothing, ran gh")
            self.assertEqual(passed, ["pr", "view", "7", "-R", "o/r", "--json", "state"])
            # and it is honoured past the point where the chain would otherwise be refused
            with mock.patch.dict(os.environ, {gg.BROKER_DEPTH: str(gg.BROKER_MAX_DEPTH + 5)}):
                code, _out, err, passed = self.run_gh(["pr", "view", "7"], keep_mark=True)
            self.assertIsNone(code, err)
            self.assertEqual(passed, ["pr", "view", "7"])

    def test_a_person_at_a_terminal_gets_ghs_own_output(self) -> None:
        """gh indents and colours `--json` on a tty and this prints compact json.dumps, so serving
        an interactive call would answer it in a different shape than gh does. A tty is a person,
        not the polling this exists to cut."""
        self.cache_pr(7, {"state": "OPEN"})
        plan = gg.broker_plan(["pr", "view", "7", "-R", "o/r", "--json", "state"], time.time())
        self.assertEqual(plan[0], "serve")
        with mock.patch.object(gg.sys, "stdout", mock.Mock(isatty=lambda: True)):
            route, payload, key = gg.broker_plan(["pr", "view", "7", "-R", "o/r", "--json", "state"],
                                                 time.time())
        self.assertEqual((route, payload), ("pass", None))
        self.assertEqual(key, "pr:o/r#7", "still worth watching for the next reader")

    def test_a_git_env_var_stops_the_walk_rather_than_guessing(self) -> None:
        """Hooks set GIT_DIR, and under it the walk up from cwd lands on a different repository
        than gh would: in a checkout of one repo with GIT_DIR naming another's, real gh answers
        the second and the walk answers the first. No test covered this -- deleting the bail-out
        left the whole suite green."""
        where = self.checkout("o/r")
        with mock.patch.object(gg.Path, "cwd", staticmethod(lambda: where)):
            self.assertEqual(gg.broker_repo({}), "o/r")
            for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CEILING_DIRECTORIES",
                         "GIT_DISCOVERY_ACROSS_FILESYSTEM"):
                with self.subTest(env=name):
                    with mock.patch.dict(os.environ, {name: str(where / ".git")}):
                        self.assertIsNone(gg.broker_repo({}))

    def test_a_missing_field_is_declined_on_purpose_not_crashed_into_the_fallback(self) -> None:
        """`test_a_field_missing_from_this_entry_is_passed_through` passes against a broker that
        raises KeyError here: cmd_gh's fail-open catches it, gh still runs, and its one assertion
        holds. The route it logs is `error`, not `pass`. The decision is what has to be pinned."""
        key = self.cache_pr(7, {"state": "OPEN"})
        self.assertEqual(gg.broker_plan(["pr", "view", "7", "-R", "o/r", "--json", "state,mergedAt"],
                                        time.time()), ("pass", None, key))
        self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state,mergedAt"])
        routes = [json.loads(line)["route"] for line in
                  gg.broker_log().read_text().splitlines() if "route" in line]
        self.assertEqual(routes[-1], "pass", "a declined field is a decision, not an error")

    def test_the_hop_cap_stays_small_enough_to_bound_a_loop(self) -> None:
        # Raising it to 40 changed nothing in the suite, and a cap is only a bound if it is small:
        # one wrapper this broker could not recognise, and no more.
        self.assertLessEqual(gg.BROKER_MAX_HOPS, 3)
        self.assertLessEqual(gg.BROKER_MAX_DEPTH, 12)

    def test_the_call_log_is_rotated_and_the_audit_reads_across_it(self) -> None:
        """One line per gh call per host, and this was the one append here with no cap at all:
        117 MB on cosmic-gray. The audit's window must not end at the rotation."""
        log = Path(os.environ["CMUX_GH_LOG"])
        log.write_text("x" * (gg.GUARD_LOG_MAX + 1))
        self.run_gh(["pr", "view", "7", "-R", "o/r"])
        self.assertTrue(log.with_name(log.name + ".1").exists(), "not rotated")
        self.assertLess(log.stat().st_size, 1024, "the new log starts empty")
        older = "2026-10-20T00:00:00Z session-a api repos/o/r/pulls\n"
        log.with_name(log.name + ".1").write_text(older)
        code = gg.cmd_audit(log, 0, True)
        self.assertEqual(code, gg.EXIT_OK)


class BrokerThirdReviewTest(BrokerBase):
    """What a third independent review found by running it, 2026-10-21. Seven of its eight
    confirmed findings were this head's own -- introduced by the fixes for the second review --
    which is why each one here is a case the suite did not have."""

    def remotes(self, config: str) -> list[str]:
        return gg.broker_remotes(self.git_checkout(self.home / "roots" / "probe", config))

    def test_a_config_read_in_part_declines_instead_of_naming_one_remote(self) -> None:
        """The dangerous direction is not missing every remote, it is missing *one*: broker_repo()
        serves when it sees exactly one, so dropping one of two turns a decline into answering one
        repository's question with another's.

        Both halves matter. A form git reads and this did not -- the deprecated `[remote.upstream]`
        spelling, and a UTF-8 BOM in front of `[remote "origin"]` -- left one remote of two, and
        the BOM case served the *upstream* as the checkout's repository. A form nothing can read
        out of this text at all -- an `[include]` git would follow, a value continued on the next
        line -- has to yield nothing rather than a subset.

        What git itself says about these configs was read off git 2.53.0 rather than assumed, and
        it is not one claim but four (`git config --get-regexp '^remote\\..*\\.url$'`). An earlier
        version of this docstring said git "sees two in every config here", which was wrong for
        five of them:
          - **two remotes**, so the one-remote rule must not fire: the dotted section, the BOM, the
            quoted escape, and both continuations.
          - **`fatal: bad config line N`** -- git refuses the whole file, so nothing may be read out
            of it: the unterminated quote, the unclassifiable line, and either unreadable header.
          - **one remote in this text**: `[include]` and `[includeIf]`, whose other file is not in
            this text. One remote in part of a config is not one remote, which is the point.
          - **none at all**: `[remote]` with no name. `remote.url` is a key git stores and not a
            remote git has, so reading it as one serves a repository the checkout does not have.

        Which of these the *serve* path answers is no longer this parser's business: it asks git
        (git_remotes), so for the two `[include]` forms -- where the included file does not exist
        and git therefore lists exactly the one remote the file holds -- the broker now resolves
        `me/cli`, which is what gh resolves there too. Following git is the whole point; this
        parser's all-or-nothing rule keeps the local index honest, not a served answer."""
        fork, upstream = "git@github.com:me/cli.git", "https://github.com/cli/cli"
        for name, config, want in (
                ("dotted section", f'[remote.origin]\n\turl = {fork}\n'
                                   f'[remote.upstream]\n\turl = {upstream}\n', ["me/cli", "cli/cli"]),
                # Both sections dotted was the safe accident: a reader that knows neither finds no
                # remote at all and declines. One of each is the wrong-answer shape -- the quoted
                # section is read, the dotted one is not, so exactly one remote is left and the
                # one-remote rule served the fork for a checkout that has two.
                ("one dotted section beside a quoted one", f'[remote "origin"]\n\turl = {fork}\n'
                                                           f'[remote.upstream]\n\turl = {upstream}\n',
                 ["me/cli", "cli/cli"]),
                ("BOM", f'﻿[remote "origin"]\n\turl = {fork}\n'
                        f'[remote "upstream"]\n\turl = {upstream}\n', ["me/cli", "cli/cli"]),
                ("quoted escape", f'[remote "origin"]\n\turl = {fork}\n'
                                  f'[remote "up\\"stream"]\n\turl = {upstream}\n', ["me/cli", "cli/cli"]),
                ("include", f'[remote "origin"]\n\turl = {fork}\n'
                            "[include]\n\tpath = ../upstream-remote\n", []),
                ("includeIf", f'[remote "origin"]\n\turl = {fork}\n'
                              '[includeIf "gitdir:~/work/"]\n\tpath = work\n', []),
                ("continuation", f'[remote "origin"]\n\turl = {fork}\n'
                                 '[remote "upstream"]\n\turl = https://github.com/\\\n\tcli/cli\n', []),
                # The continuation above is caught by luck, not by the rule: its second physical
                # line is `cli/cli`, which is not a key either, so even a parser that took the
                # unfinished value and carried on would stop at the next line. This one's second
                # line reads as a valueless key, so carrying on leaves exactly the fork -- and git
                # joins the two lines into `https://github.com/cli/cli` and lists two remotes.
                ("continuation that reads as a key", f'[remote "origin"]\n\turl = {fork}\n'
                                                     '[remote "upstream"]\n\turl = https://github.com/cli/\\\ncli\n', []),
                ("unterminated quote", f'[remote "origin"]\n\turl = "{fork}\n', []),
                ("not a key or a header", f'[remote "origin"]\n\turl = {fork}\n!!garbage!!\n', []),
                ("unreadable header", f'[remote "origin"\n\turl = {fork}\n', []),
                # Same: the broken header above is last, so skipping it loses the url under it and
                # the subset is empty. Put it first and skipping it keeps the section that follows,
                # which leaves one remote out of a file git will not read at all.
                ("unreadable header before a good one", f'[remote "upstream"\n\turl = {upstream}\n'
                                                        f'[remote "origin"]\n\turl = {fork}\n', []),
                # `remote.url` is not a remote: git lists none for this and serving `o/r` would
                # answer with a repository the checkout does not have.
                ("remote with no name", '[remote]\n\turl = https://github.com/o/r\n', [])):
            with self.subTest(config=name):
                self.assertEqual(self.remotes(config), want)
                where = self.home / "roots" / "probe"
                # git lists one remote for the two `[include]` forms -- the included file is not
                # there, so there is nothing to compose in -- and refuses the four configs with
                # `fatal: bad config line N`, both measured on git 2.53.0. So the serve path
                # declines everything here except those two, where it answers what gh answers.
                with mock.patch.object(gg.Path, "cwd", staticmethod(lambda: where)):
                    self.assertEqual(gg.broker_repo({}),
                                     "me/cli" if name in ("include", "includeIf") else None,
                                     "did not resolve this checkout the way git does")

    def test_a_config_form_this_does_understand_still_resolves(self) -> None:
        # The decline must not be bought by declining everything: these are the shapes real configs
        # have, and a checkout the broker cannot resolve is one it cannot serve.
        for name, config, want in (
                ("one remote", '[remote "origin"]\n\turl = git@github.com:o/r.git\n', ["o/r"]),
                ("valueless key", '[core]\n\tbare\n[remote "origin"]\n\turl = https://github.com/o/r\n', ["o/r"]),
                ("key on the header line", '[remote "origin"] url = https://github.com/o/r\n', ["o/r"]),
                ("escapes in another value", '[alias]\n\tlg = "log --format=\\"%h\\t%s\\""\n'
                                             '[remote "origin"]\n\turl = https://github.com/o/r\n', ["o/r"]),
                ("comments and blanks", "# a comment\n; another\n\n"
                                        '[remote "origin"]\n\turl = https://github.com/o/r # here\n', ["o/r"]),
                ("submodule still not a remote", '[remote "origin"]\n\turl = https://github.com/o/r\n'
                                                 '[submodule "x"]\n\turl = https://github.com/o/dep\n', ["o/r"]),
                # The other side of "remote with no name": git reads this config fine and lists one
                # remote, so counting `remote.url` as a second one would decline a checkout the
                # broker can serve. Both directions, because only one of them is a wrong answer.
                ("a nameless remote next to a real one", '[remote]\n\turl = https://github.com/o/dep\n'
                                                         '[remote "origin"]\n\turl = https://github.com/o/r\n', ["o/r"])):
            with self.subTest(config=name):
                self.assertEqual(self.remotes(config), want)

    def test_github_com_has_to_be_the_host_and_not_a_substring(self) -> None:
        """Unanchored, `github.com` anywhere in the URL matched, so a mirror's checkout answered as
        the upstream repository's: `https://notgithub.com/o/r`, a path under
        `/srv/mirrors/github.com/`, and `https://gitlab.com/mirror/github.com/o/r` all resolved to
        github.com's `o/r`. Inherited, and the commit that touched this regex left the hole."""
        for url in ("https://notgithub.com/o/r", "https://notgithub.com/o/r.git",
                    "git@notgithub.com:o/r.git", "https://gitlab.com/mirror/github.com/o/r",
                    "/srv/mirrors/github.com/o/r", "file:///srv/mirrors/github.com/o/r",
                    "https://github.com.evil.example/o/r", "https://example.com/?u=github.com/o/r"):
            with self.subTest(url=url):
                self.assertEqual(self.remotes(f'[remote "origin"]\n\turl = {url}\n'), [],
                                 "a URL that merely contains github.com is not a github.com remote")
        for url in ("https://github.com/o/r", "https://github.com/o/r.git", "http://github.com/o/r",
                    "git@github.com:o/r.git", "github.com:o/r", "ssh://git@github.com/o/r.git",
                    "git://github.com/o/r", "https://user:token@github.com/o/r",
                    "ssh://git@github.com:22/o/r.git", "https://github.com/o/r/"):
            with self.subTest(url=url):
                self.assertEqual(self.remotes(f'[remote "origin"]\n\turl = {url}\n'), ["o/r"])

    def test_the_host_gh_itself_would_use_is_the_one_this_speaks_for(self) -> None:
        """gh does not simply default to github.com: with exactly one host logged in, that host is
        its default. With GH_CONFIG_DIR naming a config whose only host was `ghe.invalid.example`,
        real gh 2.98.0 went there -- and this still printed the github.com source refusal and still
        served `pr view 7 -R acme/thing --json state` out of the github.com cache. The same
        wrong-answer shape as an explicit `--hostname`, through the one channel not read."""
        self.checkout("acme/thing")
        self.cache_pr(7, {"state": "OPEN"})
        hosts = Path(os.environ["GH_CONFIG_DIR"]) / "hosts.yml"
        hosts.parent.mkdir(parents=True, exist_ok=True)
        for name, text, elsewhere in (
                ("no file at all", None, False),
                ("github.com only", "github.com:\n    user: leo\n    oauth_token: x\n", False),
                ("an enterprise host only", "ghe.invalid.example:\n    user: leo\n", True),
                ("both, so gh keeps github.com", "github.com:\n    user: leo\n"
                                                 "ghe.invalid.example:\n    user: leo\n", False),
                ("a file this cannot read as hosts", "not: yaml: this: shape\n  - [\n", True)):
            with self.subTest(hosts=name):
                hosts.unlink(missing_ok=True)
                if text is not None:
                    hosts.write_text(text)
                self.assertEqual(gg.broker_elsewhere({}), elsewhere)
                code, out, _err, passed = self.run_gh(
                    ["pr", "view", "7", "-R", "o/r", "--json", "state"])
                if elsewhere:
                    self.assertEqual((code, out), (None, ""), "served a github.com answer for another host")
                    self.assertIsNotNone(passed)
                else:
                    self.assertEqual(code, gg.EXIT_OK)
                code, _out, _err, passed = self.run_gh(["api", "repos/acme/thing/contents/README.md"])
                if elsewhere:
                    self.assertIsNone(code, "refused a repository it cannot know is the same one")
                else:
                    self.assertEqual(code, gg.BROKER_EXIT_REFUSED)

    def test_no_home_at_all_is_still_github_com(self) -> None:
        """Finding which host gh would use must not be able to throw: `Path.home()` raises with no
        HOME -- a launchd job, a systemd unit, `env -u HOME` -- and this is on the path of every
        brokered call. The same bug took the cmuxterm-hq PATH guard out entirely the same week. No
        HOME means gh has no config either, so its built-in default is the honest answer."""
        self.cache_pr(7, {"state": "OPEN"})
        stripped = {k: v for k, v in os.environ.items() if k not in ("HOME", "XDG_CONFIG_HOME", "GH_CONFIG_DIR")}
        with mock.patch.dict(os.environ, stripped, clear=True):
            self.assertEqual(gg.gh_hosts_file(), None)
            self.assertIs(gg.broker_elsewhere({}), False)
            code, out, _err, _passed = self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        self.assertEqual((code, out.strip()), (gg.EXIT_OK, '{"state":"OPEN"}'))

    def test_a_person_at_a_terminal_is_not_held_by_the_repeat_brake(self) -> None:
        """broker_plan's tty branch returns "pass" before cmd_gh reaches the brake, so adding it
        turned a person's second interactive `pr view --json` from served instantly into held 30 s.
        Measured on a real pty: 0.12 s, then 30.00 s. The brake is for a loop; `--web` is already
        exempt for exactly this reason."""
        args = ["pr", "view", "7", "-R", "o/r", "--json", "state"]
        words, method, seen, _values = gg.broker_words(args)
        with mock.patch.dict(os.environ, {"CMUX_GH_MIN_REPEAT_SECONDS": "30"}):
            now = time.time()
            self.assertEqual(gg.broker_repeat_wait(args, words, method, seen, now), 0.0)
            with mock.patch.object(gg.sys, "stdout", mock.Mock(isatty=lambda: True)):
                self.assertEqual(gg.broker_repeat_wait(args, words, method, seen, now + 1), 0.0,
                                 "held an interactive call behind the brake")
            # and a piped caller -- every agent's call -- still gets it
            self.assertGreater(gg.broker_repeat_wait(args, words, method, seen, now + 1), 0.0)

    def test_one_call_cannot_spend_more_than_the_lock_budget_waiting(self) -> None:
        """Bounding each lock separately let them add up: with a wedged sibling holding all three,
        one call waited 6.16 s before gh ran. The number a caller cares about is when gh runs,
        which is one number, so the budget is per call and not per lock."""
        self.assertLessEqual(gg.LOCK_WAIT, 3, "a bound is only a bound if it is small")
        self.assertLessEqual(gg.LOCK_BUDGET, 3)
        gg.BASE.mkdir(parents=True, exist_ok=True)
        gg.watch_dir().mkdir(parents=True, exist_ok=True)
        holders = [open(p, "a+") for p in (gg.BASE / ".broker-seen.lock",  # noqa: SIM115
                                           gg.BASE / ".broker-repeat.lock",
                                           gg.watch_dir() / ".register.lock")]
        self.addCleanup(lambda: [h.close() for h in holders])
        for handle in holders:
            fcntl.flock(handle, fcntl.LOCK_EX)  # stands in for one wedged sibling holding all three
        args = ["pr", "view", "7", "-R", "o/r", "--json", "state"]
        with mock.patch.dict(os.environ, {"CMUX_GH_MIN_REPEAT_SECONDS": "30"}), \
                mock.patch.object(gg, "daemon_running", lambda *a, **k: True), \
                mock.patch.object(gg, "broker_hold", lambda seconds: None):
            for attempt in (1, 2):  # the second reaches register too, so all three locks are tried
                with self.subTest(call=attempt):
                    began = time.monotonic()
                    _code, _out, _err, passed = self.run_gh(args)
                    waited = time.monotonic() - began
                    self.assertIsNotNone(passed, "the call has to reach gh either way")
                    self.assertLess(waited, gg.LOCK_BUDGET + 1.0, f"waited {waited:.1f}s on locks")

    def test_a_lock_that_is_free_is_really_held(self) -> None:
        """Nothing asserted the lock was *taken* when it was available, so `yield False` always,
        and `with bounded_lock(...)` replaced by `if True:` in broker_seen_count, both left the
        suite green: the mutual exclusion these exist for was untested from either side."""
        gg.BASE.mkdir(parents=True, exist_ok=True)
        path = gg.BASE / ".exclusion.lock"
        with gg.bounded_lock(path) as held:
            self.assertTrue(held, "an uncontended lock must be held, not given up on")
            with gg.bounded_lock(path, 0.05) as again:
                self.assertFalse(again, "two holders at once: this is not excluding anything")
        with gg.bounded_lock(path, 0.05) as after:
            self.assertTrue(after, "the lock was not released")
        # and the bookkeeping really goes through it: held by a sibling, the count has to wait
        with open(gg.BASE / ".broker-seen.lock", "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            began = time.monotonic()
            gg.broker_seen_count("pr:o/r#7", time.time())
            waited = time.monotonic() - began
        self.assertGreater(waited, 0.5, "returned without ever trying for the lock")
        self.assertLess(waited, gg.LOCK_WAIT + 1.0)

    def test_a_lock_path_that_cannot_be_opened_is_not_an_error(self) -> None:
        # The fail-open guard this function opens with was untested: with the OSError let through,
        # gh still runs (cmd_gh catches everything) but the call is logged as `error`, which is the
        # exact shape test_a_missing_field_is_declined_on_purpose was added to call out.
        gg.BASE.mkdir(parents=True, exist_ok=True)
        directory = gg.BASE / "not-a-lock-file"
        directory.mkdir()
        with gg.bounded_lock(directory) as held:
            self.assertFalse(held)
        self.cache_pr(7, {"state": "OPEN"})
        self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "state"])
        routes = [json.loads(line)["route"] for line in
                  gg.broker_log().read_text().splitlines() if "route" in line]
        self.assertEqual(routes[-1], "serve")

    def test_the_audit_counts_what_the_rotated_file_holds(self) -> None:
        """`test_the_call_log_is_rotated_and_the_audit_reads_across_it` asserted only the exit code,
        so deleting the read of `.1` left it green while `audit` reported one call instead of two.
        And concatenating the two files bare glued the rotated file's last line to the current
        log's first, losing *both* records: one call counted as
        `api repos/o/r/pulls2026-10-20T00:00:05Z`."""
        log = Path(os.environ["CMUX_GH_LOG"])
        current = "2026-10-20T00:00:05Z session-b api repos/o/r/pulls\n"
        for name, older in (("a clean rotated file", "2026-10-20T00:00:00Z session-a api repos/o/r/pulls\n"),
                            ("one with no trailing newline", "2026-10-20T00:00:00Z session-a api repos/o/r/pulls")):
            with self.subTest(rotated=name):
                log.write_text(current)
                log.with_name(log.name + ".1").write_text(older)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(gg.cmd_audit(log, 0, True), gg.EXIT_OK)
                report = json.loads(out.getvalue())
                self.assertEqual(report["calls"], 2, "a rotated line is a call or it is lost")
                self.assertEqual(report["categories"], {"api repos/o/r/pulls": 2})

    def test_two_callers_at_the_threshold_cannot_rotate_the_history_away(self) -> None:
        """`stat` then `replace`, with no lock: two callers straddling the cap both rotated, and the
        second replaced the full `.1` with the 51-byte log the first had just started -- 67 MB of
        audit history in neither file, which is the one thing rotation exists to keep. Driven
        directly to confirm it before fixing it. Under the lock the second caller declines to
        rotate, and the log grows a little past the cap instead."""
        log = Path(os.environ["CMUX_GH_LOG"])
        log.parent.mkdir(parents=True, exist_ok=True)
        history = "2026-10-20T00:00:00Z session-a api repos/o/r/pulls\n"
        log.write_text(history + "x" * gg.GUARD_LOG_MAX)
        with open(log.parent / ".guard-log-rotate.lock", "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)  # the sibling that is mid-rotation
            gg.broker_log_call(["api", "repos/o/r/pulls"], {})
        self.assertFalse(log.with_name(log.name + ".1").exists(), "rotated behind another rotation")
        self.assertIn(history, log.read_text(), "the history was dropped, not kept")
        # with nobody holding it, the rotation still happens and still keeps everything
        gg.broker_log_call(["api", "repos/o/r/pulls"], {})
        self.assertIn(history, log.with_name(log.name + ".1").read_text())
        self.assertLess(log.stat().st_size, 1024, "the new log starts empty")

    def test_the_call_log_is_not_readable_by_other_local_accounts(self) -> None:
        """The comment above it says 0700 "like every other state path here", and under a normal
        umask the log came out 0664 and so did the world-readable `.1` it was rotated into.
        Credentials are stripped, so this is not a secret leak; it is every repo, PR, run and API
        path every session on the host read."""
        log = Path(os.environ["CMUX_GH_LOG"])
        log.parent.mkdir(parents=True, exist_ok=True)
        old = os.umask(0o002)
        self.addCleanup(os.umask, old)
        gg.broker_log_call(["api", "repos/o/r/pulls"], {})
        self.assertEqual(log.stat().st_mode & 0o777, 0o600, "created readable by this host's other accounts")
        log.chmod(0o664)  # the copies every host this has already run on is holding
        gg.broker_log_call(["api", "repos/o/r/pulls"], {})
        self.assertEqual(log.stat().st_mode & 0o777, 0o600, "a loose log is not repaired")
        log.write_text("x" * (gg.GUARD_LOG_MAX + 1))
        gg.broker_log_call(["api", "repos/o/r/pulls"], {})
        self.assertEqual(log.with_name(log.name + ".1").stat().st_mode & 0o777, 0o600)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_a_log_that_is_a_symlink_is_not_narrowed_through(self) -> None:
        # chmod follows a symlink, so a CMUX_GH_LOG pointing at one would narrow whatever the link
        # names -- someone else's file, or /dev/null if this ever runs as root. A mode is only ours
        # to set on a file we would have created.
        target = self.home / "elsewhere.log"
        target.write_text("")
        target.chmod(0o644)
        link = self.home / "linked.log"
        link.symlink_to(target)
        with mock.patch.dict(os.environ, {"CMUX_GH_LOG": str(link)}):
            gg.broker_log_call(["api", "repos/o/r/pulls"], {})
        self.assertEqual(target.stat().st_mode & 0o777, 0o644, "narrowed a file it did not create")
        self.assertIn("api repos/o/r/pulls", target.read_text(), "and the call is still logged")

    def test_the_watch_decision_does_not_wait_for_a_daemon_that_is_down(self) -> None:
        """DOWN_GRACE exists so a client waiting on the daemon rides out its re-exec. This caller
        only decides whether to register a watch, which is worthless if the daemon really is down,
        and paying the grace cost 5.12 s on the hottest read shape on the host -- per call, exactly
        when none of the cache's value exists to pay for it. The fix had no test at all: putting
        the grace back left the suite green."""
        self.assertFalse(gg.daemon_running(), "this test needs the daemon down")
        args = ["pr", "view", "7", "-R", "o/r", "--json", "state"]
        began = time.monotonic()
        for _ in range(2):  # the second is the one that would reach the watch decision
            _code, _out, _err, passed = self.run_gh(args)
            self.assertIsNotNone(passed)
        waited = time.monotonic() - began
        self.assertLess(waited, max(2 * gg.DOWN_GRACE, 1.0), f"paid the grace twice: {waited:.1f}s")


class BrokerFourthReviewTest(BrokerBase):
    """What a fourth independent review found by running it, 2026-10-21.

    Finding 1 is the one that matters, and it is this suite's own blind spot: the round these tests
    are named for made the config *parser* all-or-nothing, and the claim was true of the parser and
    false of `broker_remotes`, its only consumer, which went on silently skipping any URL its one
    pattern missed. A subset of two remotes is one, and one is what broker_repo acts on -- the same
    wrong-repository bug the round fixed, one layer out, which 288 passing tests did not see."""

    def remotes(self, config: str) -> list[str]:
        return gg.broker_remotes(self.git_checkout(self.home / "roots" / "probe", config))

    def test_a_github_url_this_cannot_read_declines_the_whole_config(self) -> None:
        """Measured against git 2.53.0 and gh 2.98.0: git lists both remotes and gh, with two
        github remotes and no `gh-resolved`, resolves the base repository through the API and
        answers the *parent's* PR 7. Dropping the second URL left `['me/cli']`, so the broker
        answered the fork's from cache -- confidently, and for a different repository."""
        fork = "git@github.com:me/cli.git"
        for name, url in (
                ("a query string", "https://github.com/cli/cli?x=1"),
                ("a doubled slash", "https://github.com//cli/cli"),
                ("an extra path segment", "https://github.com/cli/cli/extra"),
                # Quoted, because an unquoted `#` is a comment to git: it reads the URL as
                # `https://github.com/cli/cli` and so does gh, so that one resolves (below).
                ("a quoted fragment", '"https://github.com/cli/cli#frag"'),
                ("a trailing space inside quotes", '"https://github.com/cli/cli "'),
                # api.github.com is in BROKER_GITHUB_HOSTS because gh's hosts.yml names it. It is
                # not a clone host, so a remote pointing there is a shape we do not understand.
                ("the api host", "https://api.github.com/repos/cli/cli"),
                ("a github subdomain", "https://codeload.github.com/cli/cli"),
                ("no repository at all", "https://github.com/cli")):
            with self.subTest(url=name):
                config = f'[remote "origin"]\n\turl = {fork}\n[remote "upstream"]\n\turl = {url}\n'
                self.assertEqual(self.remotes(config), [],
                                 "a github URL this cannot read leaves a subset of the remotes git lists")
                where = self.home / "roots" / "probe"
                with mock.patch.object(gg.Path, "cwd", staticmethod(lambda: where)):
                    self.assertIsNone(gg.broker_repo({}),
                                      "served one repository for a checkout that has two")

    def test_a_github_url_gh_does_resolve_is_read_rather_than_dropped(self) -> None:
        """The other half: these are github remotes gh resolves to `github.com/cli/cli`, and the
        old pattern dropped them for being spelled differently. Declining everything is not the
        fix -- a checkout the broker cannot resolve is one it cannot serve."""
        for name, url in (("www", "https://www.github.com/cli/cli"),
                          ("an upper-case host", "https://GITHUB.COM/cli/cli"),
                          ("a mixed-case host", "https://GitHub.com/cli/cli.git"),
                          ("www over ssh", "ssh://git@www.github.com/cli/cli.git"),
                          ("an upper-case scp host", "git@GITHUB.COM:cli/cli.git"),
                          ("an absolute scp path", "git@github.com:/cli/cli.git"),
                          # An unquoted `#` ends the value: git's remote URL is the part before it.
                          ("an unquoted fragment, which git reads as a comment",
                           "https://github.com/cli/cli#frag")):
            with self.subTest(url=name):
                self.assertEqual(self.remotes(f'[remote "origin"]\n\turl = {url}\n'), ["cli/cli"])

    def test_another_forge_beside_a_github_remote_is_skipped_not_poisonous(self) -> None:
        """Only a *github* URL this cannot read poisons the config. A remote on another forge, a
        local path or a relative one is not ours to understand, and treating it as unreadable would
        decline every checkout that has an alternate or a vendored mirror."""
        fork = "git@github.com:me/cli.git"
        for name, url in (("gitlab", "https://gitlab.com/mirror/github.com/cli/cli"),
                          ("a local path", "/srv/mirrors/github.com/cli/cli"),
                          ("a relative remote", "../sibling"),
                          ("a file url", "file:///srv/mirrors/cli"),
                          ("a path with a colon in it", "/srv/a:b/cli"),
                          ("a host that only looks like github", "https://notgithub.com/cli/cli"),
                          ("a host github.com is a prefix of", "https://github.com.evil.example/cli/cli")):
            with self.subTest(url=name):
                config = f'[remote "origin"]\n\turl = {fork}\n[remote "other"]\n\turl = {url}\n'
                self.assertEqual(self.remotes(config), ["me/cli"])

    def test_a_relative_gitdir_is_read_from_beside_the_git_file(self) -> None:
        """git resolves a relative `gitdir:` against the directory holding the `.git` file, not the
        process cwd -- and relative is the shape git writes for a submodule and for
        `git worktree add --relative-paths`. Resolved against cwd, `gitdir: ../real/.git` read a
        *different* checkout's config: git answers `right/repo` there and this answered
        `wrong/repo`, which is a wrong repository, not a decline."""
        root = self.home / "roots"
        for where, owner_repo in ((root / "real", "right/repo"), (root / "a" / "real", "wrong/repo")):
            (where / ".git").mkdir(parents=True)
            (where / ".git" / "config").write_text(
                f'[remote "origin"]\n\turl = https://github.com/{owner_repo}\n')
        inner = root / "a" / "inner"
        inner.mkdir(parents=True)
        (root / "a" / ".git").write_text("gitdir: ../real/.git\n")
        with mock.patch.object(gg.Path, "cwd", staticmethod(lambda: inner)):
            self.assertEqual(gg.broker_remotes(root / "a"), ["right/repo"])
        # An absolute gitdir, and a worktree's `gitdir: .../worktrees/NAME`, still resolve as before
        (root / "b").mkdir()
        (root / "b" / ".git").write_text(f"gitdir: {root / 'real' / '.git'}\n")
        self.assertEqual(gg.broker_remotes(root / "b"), ["right/repo"])
        (root / "c").mkdir()
        (root / "c" / ".git").write_text(f"gitdir: {root / 'real' / '.git' / 'worktrees' / 'wt'}\n")
        self.assertEqual(gg.broker_remotes(root / "c"), ["right/repo"])

    def test_a_quoted_value_keeps_the_spaces_git_keeps(self) -> None:
        """`.strip()` on the whole value stripped what quotes protect. git keeps them, so git's
        remote URL is `  https://github.com/me/cli`, which gh does not recognise as a github remote
        at all -- the broker answering where gh would not."""
        self.assertEqual(self.remotes('[remote "origin"]\n\turl = "  https://github.com/me/cli"\n'), [])
        self.assertEqual(self.remotes('[remote "origin"]\n\turl = "https://github.com/me/cli  "\n'), [])
        # and unquoted whitespace around a value is still trimmed, as git trims it
        self.assertEqual(
            self.remotes('[remote "origin"]\n\turl =    https://github.com/me/cli   \n'), ["me/cli"])
        # a quoted value with no stray space is unchanged
        self.assertEqual(
            self.remotes('[remote "origin"]\n\turl = "https://github.com/me/cli"\n'), ["me/cli"])

    def test_two_callers_at_the_threshold_cannot_rotate_the_decisions_away(self) -> None:
        """The same `stat`-then-`replace` race the call log's rotation fixed, still in broker_note
        170 lines above it. Two callers straddling the cap both rotated, and the second overwrote
        the full `.1` with the handful of bytes the first had just started: the decision history --
        the broker's own before/after number -- in neither file."""
        log = gg.broker_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        history = json.dumps({"at": 1, "route": "serve"}) + "\n"
        log.write_text(history + "x" * gg.BROKER_LOG_MAX)
        with open(log.parent / ".broker-log-rotate.lock", "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)  # the sibling that is mid-rotation
            gg.broker_note({"route": "pass"})
        self.assertFalse(log.with_name(log.name + ".1").exists(), "rotated behind another rotation")
        self.assertIn(history, log.read_text(), "the decision history was dropped, not kept")
        gg.broker_note({"route": "pass"})  # nobody holding it: the rotation happens and keeps it
        self.assertIn(history, log.with_name(log.name + ".1").read_text())
        self.assertLess(log.stat().st_size, 1024, "the new log starts empty")

    def test_the_audit_counts_the_decisions_the_rotated_file_holds(self) -> None:
        """`brokerDecisions` is the number the broker is judged by, and it was read from the live
        file only, so every rotation truncated it -- with or without the race above."""
        calls = Path(os.environ["CMUX_GH_LOG"])
        calls.parent.mkdir(parents=True, exist_ok=True)
        calls.write_text("2026-10-20T00:00:05Z session-b api repos/o/r/pulls\n")
        log = gg.broker_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({"at": 2, "route": "serve"}) + "\n")
        log.with_name(log.name + ".1").write_text(
            json.dumps({"at": 1, "route": "serve"}) + "\n" + json.dumps({"at": 1, "route": "refuse"}) + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gg.cmd_audit(calls, 0, True), gg.EXIT_OK)
        self.assertEqual(json.loads(out.getvalue())["brokerDecisions"], {"serve": 2, "refuse": 1})

    def test_a_torn_line_in_the_rotated_decisions_loses_one_record_not_the_file(self) -> None:
        calls = Path(os.environ["CMUX_GH_LOG"])
        calls.parent.mkdir(parents=True, exist_ok=True)
        calls.write_text("2026-10-20T00:00:05Z session-b api repos/o/r/pulls\n")
        log = gg.broker_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({"at": 2, "route": "serve"}) + "\n")
        log.with_name(log.name + ".1").write_text(
            json.dumps({"at": 1, "route": "refuse"}) + "\n" + '{"at": 1, "rou')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gg.cmd_audit(calls, 0, True), gg.EXIT_OK)
        self.assertEqual(json.loads(out.getvalue())["brokerDecisions"], {"serve": 1, "refuse": 1})

    def test_a_read_that_never_returns_trips_the_scan_deadline_rather_than_reading_as_empty(self) -> None:
        """The hung mount this bound exists for, in the form a test can make: a `.git/config` that
        is a FIFO nobody writes to.

        The bound did not exist. DeadlineTimeout is a TimeoutError, which *is* an OSError, so the
        alarm raised into the scan landed in git_config_text's `except (OSError, ValueError)` and
        was read as "no config here" -- and setitimer is one-shot, so the rest of the scan then ran
        with no deadline at all. Worse than unbounded: the interrupted checkout was silently
        dropped and the incomplete map was cached as complete for the whole TTL. Measured on the
        head that added it: git_config_text returned None 0.30s after the alarm, and a
        20 000-checkout scan under a 0.05s deadline took 0.76s and cached 19 999 of 20 000.

        The previous test for this could not see any of that: it replaced hard_deadline with a
        context manager that raised before yielding, i.e. in broker_local_repos's own frame,
        outside both handlers. It pinned the `except` clause, not the bound.

        The hung checkout is the *last* candidate the scan reaches -- the only one at the second
        level, under a first-level directory that is not a checkout -- so that nothing runs after
        the interrupted read. That is what makes this test see the alarm: with any candidate left,
        the clock check in the loop body catches the overrun on the next iteration and the answer
        comes out the same, so the first cut of this test passed against a swallowed alarm. Here a
        swallowed alarm leaves `complete` true and writes the map, which is the harm itself."""
        self.assertFalse(issubclass(gg.ScanDeadline, OSError),
                         "an OSError here is caught by the frames this has to escape")
        root = self.home / "hung-root"
        hung = root / "parent" / "checkout"
        (hung / ".git").mkdir(parents=True)
        os.mkfifo(hung / ".git" / "config")  # read_text blocks here until the deadline
        with mock.patch.dict(os.environ, {"GLAEDA_GH_REPO_ROOTS": str(root)}), \
                mock.patch.object(gg, "BROKER_SCAN_DEADLINE", 0.3):
            began = time.monotonic()
            self.assertEqual(gg.broker_local_repos(now=time.time()), {})
            waited = time.monotonic() - began
        self.assertLess(waited, 5.0, f"the scan was not bounded: {waited:.1f}s")
        self.assertFalse(gg.broker_repos_path().exists(),
                         "cached a scan that gave up in the middle of a read")
        # and with nothing hung the scan finishes, so the cache is still written
        self.checkout("manaflow-ai/cmux")
        self.assertEqual(sorted(gg.broker_local_repos(now=time.time())), ["manaflow-ai/cmux"])
        self.assertTrue(gg.broker_repos_path().exists())

    def test_the_scan_is_bounded_where_the_alarm_does_nothing_at_all(self) -> None:
        """hard_deadline is a no-op off the main thread and on a platform without `setitimer`, and
        a scan runs wherever a gh call is brokered. The clock check in the loop body is the half of
        the bound that holds there, and it is the only half that holds for a scan that is merely
        slow rather than blocked."""
        self.checkout("manaflow-ai/cmux")
        with mock.patch.object(gg, "hard_deadline", lambda *_a, **_k: contextlib.nullcontext()), \
                mock.patch.object(gg, "BROKER_SCAN_DEADLINE", 0.0):
            self.assertEqual(gg.broker_local_repos(now=time.time()), {})
        self.assertFalse(gg.broker_repos_path().exists(), "cached a partial scan")
        # unbounded again, the scan finishes and is cached
        self.assertEqual(sorted(gg.broker_local_repos(now=time.time())), ["manaflow-ai/cmux"])
        self.assertTrue(gg.broker_repos_path().exists())

    def test_the_scan_deadline_is_small_enough_to_bound_a_gh_call(self) -> None:
        # A bound is only a bound if a person waiting on `gh pr view` would accept it.
        self.assertLessEqual(gg.BROKER_SCAN_DEADLINE, 10.0)


FORK, UPSTREAM = "git@github.com:me/cli.git", "https://github.com/cli/cli"


class BrokerFifthReviewTest(BrokerBase):
    """What a fifth independent review found by running it, 2026-10-21.

    One finding, five more doors to it, and they are not about URL shapes at all: a checkout's
    remotes are not in `<checkout>/.git/config`. gh runs `git remote -v` and
    `git config --get-regexp '^remote\\..*\\.gh-resolved$'`, and both compose every config scope and
    apply `url.<base>.insteadOf` rewriting. Every case here is measured against git 2.53.0 and
    gh 2.98.0, and every one of them served a repository's answer for a different repository --
    which is what broker_repo exists to prevent, two rounds of parser work later. So the serve path
    asks git (git_remotes) and the text parser is the local index's, where being approximate only
    costs a request."""

    def test_a_remote_written_in_another_config_scope_is_still_a_remote(self) -> None:
        """`remote.upstream.url` in `~/.gitconfig` is a remote git lists and gh resolves. Reading
        only the checkout's own file left `['me/cli']` -- one remote, so served -- for a checkout
        where gh resolves the upstream and answers the parent's PR 7."""
        self.assertIsNone(self.repo(f'[remote "origin"]\n\turl = {FORK}\n',
                                    f'[remote "upstream"]\n\turl = {UPSTREAM}\n'))

    def test_an_insteadof_rewrite_in_another_scope_moves_the_host(self) -> None:
        """`url.<base>.insteadOf` is applied by `git remote -v` (measured, not assumed), and it
        normally lives in `~/.gitconfig`. Two directions, and the second is the sharper one:

        `gh:cli/cli` looks like another forge's `gh` host to a reader of the text, so it was
        skipped, leaving one remote of two.

        With `https://github.com/` rewritten to a mirror, gh exits 1 -- "none of the git remotes
        ... point to a known GitHub host" -- and the broker answered `cli/cli` with exit 0. A
        wrapper in front of gh inventing success where gh refuses is worse than a wrong
        repository: there is no later call that corrects it."""
        self.assertIsNone(self.repo(f'[remote "origin"]\n\turl = {FORK}\n'
                                    '[remote "upstream"]\n\turl = gh:cli/cli\n',
                                    '[url "https://github.com/"]\n\tinsteadOf = gh:\n'))
        self.assertIsNone(self.repo('[remote "origin"]\n\turl = https://github.com/cli/cli\n',
                                    '[url "https://mirror.invalid/gh/"]\n'
                                    '\tinsteadOf = https://github.com/\n'))

    def test_a_remote_with_only_a_pushurl_is_a_remote_git_lists(self) -> None:
        """`git remote -v` lists it -- `NAME<TAB>` for the fetch line and the pushurl on the push
        line -- and gh resolves it (it falls back to the push URL when a remote has no fetch URL).
        The text reader looked at `url` only, so this was one remote of two again. Both lines of
        every remote are read now, which is what BrokerSixthReviewTest is about."""
        self.assertIsNone(self.repo(f'[remote "origin"]\n\turl = {FORK}\n'
                                    f'[remote "upstream"]\n\tpushurl = {UPSTREAM}\n'))

    def test_gh_resolved_is_found_however_git_would_read_the_key(self) -> None:
        """`gh repo set-default` writes `remote.<name>.gh-resolved`, and gh resolves through it to
        a repository this cannot see, so a checkout that has one is never served. The pattern over
        the text was line-anchored and case-sensitive: it missed the key on the section-header line
        -- the very form the header regex has a group for -- and missed `GH-Resolved`, though git
        config keys are case-insensitive. `git config --get-regexp` finds both (measured)."""
        for name, local in (("on the header line", f'[remote "origin"] gh-resolved = base\n'
                                                   f'\turl = {FORK}\n'),
                            ("spelled in capitals", f'[remote "origin"]\n\tGH-Resolved = base\n'
                                                    f'\turl = {FORK}\n'),
                            ("as git writes it", f'[remote "origin"]\n\turl = {FORK}\n'
                                                 "\tgh-resolved = base\n")):
            with self.subTest(form=name):
                self.assertIsNone(self.repo(local))

    def test_a_credential_in_a_remote_url_does_not_move_the_host(self) -> None:
        """The userinfo part runs to the *last* `@`: git hands the whole authority to ssh or curl
        and gh parses it with net/url, all of which do. With the userinfo stopping at the first
        `@`, `https://user@name@github.com/cli/cli` read its host as `name@github.com`, was skipped
        as another forge's, and left a subset -- a password with an `@` in it is all it takes."""
        self.assertIsNone(self.repo(f'[remote "origin"]\n\turl = {FORK}\n'
                                    '[remote "upstream"]\n'
                                    '\turl = https://user@name@github.com/cli/cli\n'))
        # and the other direction: alone, it is the github remote gh resolves, so it is served
        self.assertEqual(self.repo('[remote "origin"]\n'
                                   '\turl = https://user@name@github.com/cli/cli\n'), "cli/cli")

    def test_the_scp_form_splits_at_the_first_colon_the_way_git_does(self) -> None:
        """`user:pw@github.com:o/r` is not github's `o/r`: git runs
        `ssh user git-upload-pack 'pw@github.com:o/r'` (measured), so the host is `user` and
        github.com is part of a path. The old pattern let the userinfo cross a colon and read the
        host as github.com, so a remote git does not even clone from GitHub with was served as
        GitHub's."""
        self.assertEqual(
            gg.broker_remotes(self.git_checkout(self.home / "roots" / "probe",
                                                '[remote "origin"]\n\turl = user:pw@github.com:o/r\n')),
            [], "read a path as a host")
        self.assertIsNone(self.repo('[remote "origin"]\n\turl = user:pw@github.com:o/r\n'))

    def test_a_checkout_git_resolves_differently_is_not_named_in_a_refusal(self) -> None:
        """The refusal tells the reader to run `git show` in the checkout it names, so an index
        entry that points at a checkout of some other repository hands back the wrong bytes. The
        index is text, and text misses the scopes above -- so the one entry a refusal is about is
        confirmed with git first. Declining costs one request; naming the wrong checkout costs the
        reader a wrong answer they have no reason to doubt."""
        self.git_checkout(self.home / "roots" / "r",
                          '[remote "origin"]\n\turl = https://github.com/o/r\n')
        self.assertEqual(sorted(gg.broker_local_repos(now=time.time())), ["o/r"])
        (self.home / "gitconfig").write_text('[url "https://mirror.invalid/gh/"]\n'
                                             '\tinsteadOf = https://github.com/\n')
        code, _out, _err, passed = self.run_gh(["api", "repos/o/r/contents/x"])
        self.assertIsNone(code, "named a checkout git does not say is o/r")
        self.assertIsNotNone(passed)
        # and with nothing rewriting it, the same call is refused and names that checkout
        (self.home / "gitconfig").write_text("")
        code, _out, err, _passed = self.run_gh(["api", "repos/o/r/contents/x"])
        self.assertEqual(code, gg.BROKER_EXIT_REFUSED)
        self.assertIn(str(self.home / "roots" / "r"), err)

    def test_git_itself_being_unrunnable_declines_rather_than_guessing(self) -> None:
        """A wrapper in front of gh is never allowed to be the reason a call fails, and it must not
        answer from a worse source when the good one is unavailable: no git means no answer, which
        is also gh's position -- gh resolves repositories by running git too."""
        where = self.git_checkout(self.home / "roots" / "probe",
                                  '[remote "origin"]\n\turl = https://github.com/o/r\n')
        with mock.patch.object(gg.Path, "cwd", staticmethod(lambda: where)):
            self.assertEqual(gg.broker_repo({}), "o/r")
            for name, failure in (("git is not installed", FileNotFoundError("git")),
                                  ("git never returns", subprocess.TimeoutExpired("git", 5.0))):
                with self.subTest(failure=name):
                    with mock.patch.object(gg, "run_bounded", side_effect=failure):
                        self.assertIsNone(gg.broker_repo({}))

    def test_asking_git_is_bounded_like_everything_else_here(self) -> None:
        # Two forks in front of a real gh call: a checkout on a hung mount has to decline, not hang
        # the call. Both questions are asked with a deadline, and it is a short one.
        calls: list[float | None] = []

        def record(argv, timeout=None):
            calls.append(timeout)
            return subprocess.CompletedProcess(argv, 1, "", "")

        where = self.git_checkout(self.home / "roots" / "probe",
                                  '[remote "origin"]\n\turl = https://github.com/o/r\n')
        with mock.patch.object(gg, "run_bounded", record):
            gg.git_remotes(where)
        self.assertTrue(calls, "asked git nothing")
        self.assertTrue(all(t == gg.BROKER_GIT_DEADLINE for t in calls), calls)
        self.assertLessEqual(gg.BROKER_GIT_DEADLINE, 10.0)


class BrokerSixthReviewTest(BrokerBase):
    """What a sixth independent review found by running the new git-backed serve path, 2026-10-21.

    Both findings are the same shape as the one the path was written for -- the broker serving a
    repository gh would not -- and both come from *skipping* a remote URL instead of declining on
    it. Every case is measured against gh 2.98.0 and git 2.53.0, with gh's requests pointed at a
    refused proxy so no quota is spent and the host it would have asked is still visible."""

    def test_a_github_push_url_behind_another_forges_fetch_url_is_not_an_answer(self) -> None:
        """gh resolves a remote's *fetch* URL and falls back to its push URL only when there is no
        fetch URL at all. So with `url = https://gitlab.com/a/b.git` and
        `pushurl = https://github.com/cli/cli.git`, gh exits 1 -- "none of the git remotes ...
        point to a known GitHub host" (measured) -- and reading the push line as an answer left
        exactly one GitHub remote, which the broker served from cache with exit 0. A wrapper
        inventing success where gh refuses is the worst answer here: nothing downstream corrects
        it."""
        for fetch in ("https://gitlab.com/a/b.git", "git@gitlab.com:mirror/thing.git",
                      "https://ghe.example/acme/thing.git"):
            with self.subTest(fetch=fetch):
                self.assertIsNone(self.repo(f'[remote "origin"]\n\turl = {fetch}\n'
                                            f'\tpushurl = {UPSTREAM}\n'))

    def test_an_enterprise_remote_is_not_answered_out_of_github_coms_cache(self) -> None:
        """gh picks a remote by name across every host it has credentials for, so with
        `origin = https://github.com/o/r` and `upstream = https://ghe.example/acme/thing` it
        resolves `acme/thing` **on ghe.example** -- measured: `gh pr view 7` posts to
        `https://ghe.example/api/graphql`. Skipping the enterprise remote as "another forge" left
        `o/r` and answered another host's repository out of github.com's cache."""
        self.assertIsNone(self.repo('[remote "origin"]\n\turl = https://github.com/o/r\n'
                                    '[remote "upstream"]\n\turl = https://ghe.example/acme/thing\n'))

    def test_a_remote_this_cannot_read_as_github_coms_ends_the_answer(self) -> None:
        """The rule that makes the one-name answer sound: every URL of every remote is read, and
        one this cannot place on github.com declines the checkout. Deciding which foreign hosts gh
        really ignores means reproducing gh's own set of known hosts, and being wrong there is the
        enterprise case above -- so a GitLab mirror or a local-path remote beside the GitHub remote
        costs one request, where gh does resolve the GitHub one."""
        for name, extra in (("another forge", '[remote "mirror"]\n\turl = https://gitlab.com/a/b\n'),
                            ("a local path", '[remote "local"]\n\turl = /srv/mirrors/thing\n'),
                            ("a github URL with no repository",
                             '[remote "two"]\n\turl = https://github.com/cli\n')):
            with self.subTest(name):
                self.assertIsNone(self.repo('[remote "origin"]\n\turl = https://github.com/o/r\n'
                                            + extra))
        self.assertEqual(self.repo('[remote "origin"]\n\turl = https://github.com/o/r\n'), "o/r")

    def test_a_lone_remote_on_another_host_is_never_this_caches_repository(self) -> None:
        """The sharp case for reading the host at all, and the one the first cut of this fix had no
        test for: a checkout whose *only* remote is somewhere else. `acme/thing` on ghe.example and
        `a/b` on gitlab.com are owner/repo-shaped, so a reader that took the path and ignored the
        host would answer them out of github.com's cache -- a different repository's JSON, offered
        as this one's -- and with one remote there is no second name to decline on."""
        for url in ("https://ghe.example/acme/thing", "https://gitlab.com/a/b",
                    "git@gitlab.com:a/b.git", "/srv/mirrors/a/b"):
            with self.subTest(url=url):
                self.assertIsNone(self.repo(f'[remote "origin"]\n\turl = {url}\n'))

    def test_the_same_repository_under_both_urls_of_one_remote_is_still_one_answer(self) -> None:
        """A fetch and a push URL naming the same repository, and a `pushurl`-only remote, which
        `git remote -v` prints as `NAME<TAB>` with no URL: gh falls back to the push URL there
        (measured: `gh browse -n` asks for `repos/cli/cli`), so it is an answer and not a
        decline."""
        self.assertEqual(self.repo('[remote "origin"]\n\turl = https://github.com/o/r\n'
                                   '\tpushurl = https://github.com/o/r.git\n'), "o/r")
        self.assertEqual(self.repo('[remote "origin"]\n\tpushurl = https://github.com/o/r\n'), "o/r")

    def test_a_remote_url_that_is_not_utf8_declines_rather_than_raising(self) -> None:
        """`git remote -v` hands back the bytes the config holds, and `run_bounded(text=True)`
        raises UnicodeDecodeError on them. cmd_gh's catch-all would exec gh anyway, so the contract
        held; declining here says so instead of relying on it."""
        where = self.git_checkout(self.home / "roots" / "probe",
                                  '[remote "origin"]\n\turl = https://github.com/o/r\n')

        def raising(argv, timeout=None):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        with mock.patch.object(gg, "run_bounded", raising):
            self.assertIsNone(gg.git_remotes(where))


class Scripted:
    """A transport that answers each request from a list of (status, headers, doc) or an exception, in order.

    `GET /rate_limit` is answered here instead of from the list. A full `tick()` ends with the free
    budget refresh, and counting it as a scripted answer made every test that calls `tick()` either
    consume one of its own answers out of order or run out of them. It is free and the tests that
    care about it count `rate_limit_calls` on the other transport, so it is kept out of `calls` too."""

    LIMITS = {"resources": {"core": {"limit": 5000, "remaining": 5000, "reset": 0},
                            "graphql": {"limit": 5000, "remaining": 5000, "reset": 0}}}

    def __init__(self, *answers) -> None:
        self.answers, self.calls = list(answers), []
        self.rate_limit_calls = 0

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
        path = url.replace(gg.API, "")
        if path == "/rate_limit":
            self.rate_limit_calls += 1
            return 200, {}, json.dumps(self.LIMITS).encode()
        self.calls.append((method, path, json.loads(body) if body else None))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, h, doc = answer
        return status, h, json.dumps(doc).encode()


def no_guard(test: unittest.TestCase) -> None:
    """Run `post` as on a host without the public-body guard, whatever this host has."""
    env = mock.patch.dict(os.environ, {gg.BODY_GUARD_ENV: ""})
    pace = mock.patch.object(gg, "PACE_CLI", Path(tempfile.gettempdir()) / "glaeda-test-no-guard" / "gh")
    env.start()
    pace.start()
    test.addCleanup(env.stop)
    test.addCleanup(pace.stop)


class PostTest(unittest.TestCase):
    """`post`: sessions queue writes; the daemon sends them one at a time, paced, and holds on GitHub's
    secondary limit ("was submitted too quickly" refused PR comments and PR creation for four lanes at
    once on 2026-10-21, each retrying on its own)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = gg.BASE
        gg.BASE = Path(self.tmp.name) / "gh"
        self.addCleanup(setattr, gg, "BASE", saved)
        no_guard(self)  # the host's own body guard is PostBodyGuardTest's business

    def daemon(self, *answers) -> tuple[Any, Scripted]:
        transport = Scripted(*answers)
        return gg.Daemon(transport=transport, token=lambda: TOKEN, controller=None), transport

    def queue(self, *argv: str, stdin: str = "hello `x` $HOME") -> dict:
        args = gg.parser().parse_args(["post", *argv])
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            item = gg.post_item(args)
        # Two items queued in one millisecond by one process sort by their random suffix, so a test that
        # queues two in a row saw them sent in either order (a fast CI runner did): keep queue order.
        last = getattr(self, "_last_id", "")
        if item["id"] <= last:
            item["id"] = f"{int(last.split('-', 1)[0]) + 1:013d}-{item['id'].split('-', 1)[1]}"
        self._last_id = item["id"]
        gg.write_json(gg.outbox_dir() / f"{item['id']}.json", item)
        return item

    def step(self, daemon: Any, at: float) -> None:
        daemon.now = at
        daemon.post_tick()

    def test_comment_is_posted_and_its_url_kept(self) -> None:
        item = self.queue("comment", "o/r#7", "--body-file", "-")
        d, t = self.daemon((201, {}, {"html_url": "https://github.com/o/r/pull/7#issuecomment-1"}))
        self.step(d, NOW)
        self.assertEqual(t.calls, [("POST", "/repos/o/r/issues/7/comments", {"body": "hello `x` $HOME"})])
        state = gg.post_state(item["id"])
        self.assertEqual((state["state"], state["url"]), ("posted", "https://github.com/o/r/pull/7#issuecomment-1"))
        self.assertNotIn("body", state)
        self.assertEqual(list(gg.outbox_dir().glob("*.json")), [])

    def test_the_outbox_is_not_held_or_counted_at_the_rest_share(self) -> None:
        item = self.queue("comment", "o/r#7", "--body", "landed")
        d, t = self.daemon((201, {}, {"html_url": "u"}))
        d.rest_minutes = {int(NOW // 60): 2000}  # the daemon's polling share is spent
        d.clock_real = False
        self.step(d, NOW)
        self.assertEqual(gg.post_state(item["id"])["state"], "posted")
        self.assertEqual(d.rest_spent(), 2000)

    def test_writes_are_spaced_and_oldest_first(self) -> None:
        first = self.queue("comment", "o/r#1", "--body", "one")
        second = self.queue("review", "https://github.com/o/r/pull/2", "--event", "approve", "--body", "two")
        ok = (201, {}, {"html_url": "u"})
        d, t = self.daemon(ok, ok)
        self.step(d, NOW)
        self.step(d, NOW + gg.POST_SPACING - 1)  # too soon: nothing sent
        self.assertEqual(len(t.calls), 1)
        self.step(d, NOW + gg.POST_SPACING)
        self.assertEqual([c[1] for c in t.calls], ["/repos/o/r/issues/1/comments", "/repos/o/r/pulls/2/reviews"])
        self.assertEqual(t.calls[1][2], {"body": "two", "event": "APPROVE"})
        self.assertEqual([gg.post_state(i["id"])["state"] for i in (first, second)], ["posted", "posted"])

    def test_secondary_limit_holds_the_outbox_not_the_reads(self) -> None:
        item = self.queue("pr", "o/r", "--head", "lane/x", "--base", "main", "--title", "T", "--body", "b",
                          "--reviewer", "lawrencecchen")
        too_quick = (422, {}, {"message": "Validation Failed",
                               "errors": [{"message": "was submitted too quickly"}]})
        d, t = self.daemon(too_quick, (403, {"retry-after": "120"}, {"message": (
            "You have exceeded a secondary rate limit and have been temporarily blocked from content creation.")}),
                           (201, {}, {"html_url": "https://github.com/o/r/pull/9", "number": 9}), (201, {}, {}))
        self.step(d, NOW)
        self.assertEqual(d.post_hold, NOW + gg.POST_BACKOFF)
        self.assertEqual(d.mode("rest"), "normal")
        self.step(d, NOW + gg.POST_BACKOFF - 1)
        self.assertEqual(len(t.calls), 1)
        self.step(d, NOW + gg.POST_BACKOFF)  # Retry-After wins over the backoff
        self.assertEqual(d.post_hold, NOW + gg.POST_BACKOFF + 120)
        self.assertEqual(d.mode("rest"), "normal")
        self.assertEqual(gg.post_state(item["id"])["state"], "queued")
        self.step(d, NOW + gg.POST_BACKOFF + 120)
        self.assertEqual(t.calls[2], ("POST", "/repos/o/r/pulls",
                                      {"title": "T", "head": "lane/x", "base": "main", "body": "b", "draft": False}))
        self.assertEqual(t.calls[3], ("POST", "/repos/o/r/pulls/9/requested_reviewers", {"reviewers": ["lawrencecchen"]}))
        state = gg.post_state(item["id"])
        self.assertEqual((state["state"], state["url"], state["number"], state["throttled"]),
                         ("posted", "https://github.com/o/r/pull/9", 9, 2))
        self.assertEqual(d.post_backoff, gg.POST_BACKOFF)

    def test_a_lost_answer_is_looked_up_before_writing_again(self) -> None:
        item = self.queue("comment", "o/r#7", "--body", "receipt")
        d, t = self.daemon(gg.WatchError("request failed: TimeoutError"),
                           (200, {}, [{"body": "other", "html_url": "a"}, {"body": "receipt", "html_url": "landed"}]))
        self.step(d, NOW)
        queued = gg.post_state(item["id"])
        self.assertEqual((queued["state"], queued["attempts"], queued["uncertain"]), ("queued", 1, True))
        self.step(d, NOW + gg.POST_RETRY)
        self.assertEqual(t.calls[1][0], "GET")
        self.assertIn("/repos/o/r/issues/7/comments?per_page=100&since=", t.calls[1][1])
        state = gg.post_state(item["id"])
        self.assertEqual((state["state"], state["url"]), ("posted", "landed"))
        self.assertEqual(len(t.calls), 2)  # never posted twice

    def test_an_open_pr_for_the_head_is_the_answer(self) -> None:
        item = self.queue("pr", "o/r", "--head", "lane/x", "--base", "main", "--title", "T", "--body", "b")
        d, t = self.daemon((422, {}, {"message": "Validation Failed",
                                      "errors": [{"message": "A pull request already exists for o:lane/x."}]}),
                           (200, {}, [{"html_url": "https://github.com/o/r/pull/3"}]))
        self.step(d, NOW)
        self.assertEqual(t.calls[1][1], "/repos/o/r/pulls?state=open&head=o%3Alane/x&base=main")
        state = gg.post_state(item["id"])
        self.assertEqual((state["state"], state["url"]), ("posted", "https://github.com/o/r/pull/3"))

    def test_a_refusal_fails_and_transient_errors_give_up(self) -> None:
        bad = self.queue("comment", "o/r#404", "--body", "x")
        forbidden = self.queue("comment", "o/r#403", "--body", "x")
        d, t = self.daemon((404, {}, {"message": "Not Found"}),
                           (403, {}, {"message": "Resource not accessible by integration"}),
                           (502, {}, {}), *[(200, {}, []), (502, {}, {})] * (gg.POST_ATTEMPTS - 1))
        self.step(d, NOW)
        self.assertEqual(gg.post_state(bad["id"])["error"], "HTTP 404: Not Found")
        self.step(d, NOW + gg.POST_SPACING)  # a permission refusal fails; it does not hold the queue
        self.assertEqual(gg.post_state(forbidden["id"])["state"], "failed")
        self.assertEqual(d.post_hold, 0.0)
        flaky = self.queue("comment", "o/r#5", "--body", "y")
        at = NOW
        for _ in range(gg.POST_ATTEMPTS):
            at += gg.POST_RETRY * gg.POST_ATTEMPTS
            self.step(d, at)
        state = gg.post_state(flaky["id"])
        self.assertEqual(state["state"], "failed")
        self.assertIn(f"gave up after {gg.POST_ATTEMPTS} attempts: HTTP 502", state["error"])
        # each retry after a 502 looked first: a POST that 502s often landed
        self.assertEqual([c[0] for c in t.calls[2:6]], ["POST", "GET", "POST", "GET"])

    def test_a_failed_look_never_sends(self) -> None:
        item = self.queue("comment", "o/r#7", "--body", "receipt")
        d, t = self.daemon(gg.WatchError("request failed: TimeoutError"), (502, {}, {}),
                           (200, {}, [{"body": "receipt", "html_url": "landed"}]))
        self.step(d, NOW)
        self.step(d, NOW + gg.POST_RETRY)  # the look gets a 502: no send, still uncertain
        self.assertEqual([c[0] for c in t.calls], ["POST", "GET"])
        self.assertTrue(gg.read_json(gg.outbox_dir() / f"{item['id']}.json")["uncertain"])
        self.step(d, NOW + 3 * gg.POST_RETRY)
        self.assertEqual([c[0] for c in t.calls], ["POST", "GET", "GET"])
        self.assertEqual(gg.post_state(item["id"])["url"], "landed")

    def test_a_5xx_that_landed_is_not_posted_again(self) -> None:
        item = self.queue("comment", "o/r#7", "--body", "receipt")
        d, t = self.daemon((502, {}, {}), (200, {}, [{"body": "receipt", "html_url": "landed"}]))
        self.step(d, NOW)
        self.step(d, NOW + gg.POST_RETRY)
        self.assertEqual([c[0] for c in t.calls], ["POST", "GET"])
        self.assertEqual(gg.post_state(item["id"])["url"], "landed")

    def test_a_daemon_that_died_mid_send_looks_first(self) -> None:
        item = self.queue("comment", "o/r#7", "--body", "receipt")

        def die(*a):
            raise SystemExit  # the watchdog or launchd stopped the daemon mid-request
        d, _ = self.daemon()
        d.transport = die
        with self.assertRaises(SystemExit):
            self.step(d, NOW)
        d, t = self.daemon((200, {}, [{"body": "receipt", "html_url": "landed"}]))
        self.step(d, NOW + 10)
        self.assertEqual([c[0] for c in t.calls], ["GET"])
        self.assertEqual(gg.post_state(item["id"])["url"], "landed")

    def test_a_bad_entry_fails_alone(self) -> None:
        broken = self.queue("comment", "o/r#7", "--body", "x")
        doc = gg.read_json(gg.outbox_dir() / f"{broken['id']}.json")
        doc["kind"] = "issue"
        gg.write_json(gg.outbox_dir() / f"{broken['id']}.json", doc)
        good = self.queue("comment", "o/r#8", "--body", "y")
        d, t = self.daemon((201, {}, {"html_url": "u"}))
        self.step(d, NOW)
        self.assertIn("unknown write kind", gg.post_state(broken["id"])["error"])
        self.step(d, NOW + 1)
        self.assertEqual(gg.post_state(good["id"])["state"], "posted")

    def test_review_lookup_reads_the_last_page(self) -> None:
        item = self.queue("review", "o/r#7", "--body", "LGTM")
        link = {"link": f'<{gg.API}/repos/o/r/pulls/7/reviews?per_page=100&page=2>; rel="next", '
                        f'<{gg.API}/repos/o/r/pulls/7/reviews?per_page=100&page=3>; rel="last"'}
        d, t = self.daemon(gg.WatchError("request failed: TimeoutError"), (200, link, [{"body": "old"}]),
                           (200, {}, [{"body": "LGTM", "html_url": "review-url"}]))
        self.step(d, NOW)
        self.step(d, NOW + gg.POST_RETRY)
        self.assertEqual(t.calls[2][1], "/repos/o/r/pulls/7/reviews?per_page=100&page=3")
        self.assertEqual(gg.post_state(item["id"])["url"], "review-url")

    def test_a_restarted_daemon_keeps_githubs_hold(self) -> None:
        self.queue("comment", "o/r#7", "--body", "x")
        d, _ = self.daemon((403, {"retry-after": "600"}, {"message": "You have exceeded a secondary rate limit"}))
        d.tick(now=NOW)
        again, t = self.daemon()
        self.assertEqual(again.post_hold, NOW + 600)
        self.step(again, NOW + 10)
        self.assertEqual(t.calls, [])

    def test_cli_queues_and_returns_without_the_daemon_posting(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gg, "daemon_running", return_value=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO("body")):
            code = gg.main(["post", "comment", "o/r#7", "--body-file", "-"])
        self.assertEqual(code, 0)
        self.assertRegex(out.getvalue(), r"^queued as \d{13}-\d+-[0-9a-f]{6}; 0 ahead\n$")
        self.assertIn("glaeda-gh post status", err.getvalue())
        with mock.patch.object(gg, "daemon_running", return_value=False), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gg.main(["post", "comment", "o/r#8", "--body", "b"]), 3)
        self.assertEqual(len(list(gg.outbox_dir().glob("*.json"))), 2)  # kept for when the daemon runs
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gg.main(["post", "pr", "o/r", "--body", "b"]), 64)
            self.assertEqual(gg.main(["post", "comment", "o/r#7", "--body", "b", "--body-file", "-"]), 64)
            self.assertEqual(gg.main(["post", "status", "../../etc/x"]), 64)

    def test_status_waits_for_the_url(self) -> None:
        item = self.queue("comment", "o/r#7", "--body", "x")
        poll = gg.WAIT_FILE_POLL
        gg.WAIT_FILE_POLL = 0.02
        self.addCleanup(setattr, gg, "WAIT_FILE_POLL", poll)
        d, _ = self.daemon((201, {}, {"html_url": "posted-url"}))
        quiet = mock.patch.object(gg, "log")  # the daemon's log line would land in the captured stdout
        quiet.start()
        self.addCleanup(quiet.stop)
        timer = threading.Timer(0.1, lambda: self.step(d, NOW))
        timer.start()
        self.addCleanup(timer.cancel)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = gg.cmd_post_status(item["id"], 2, False)
        self.assertEqual((code, out.getvalue()), (0, "posted-url\n"))

    def test_tick_posts_first_and_reports_the_outbox(self) -> None:
        self.queue("comment", "o/r#7", "--body", "x")
        self.queue("comment", "o/r#8", "--body", "y")
        d, t = self.daemon((201, {}, {"html_url": "u"}))
        status = d.tick(now=NOW)
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(status["outbox"], {"queued": 1, "holdUntil": None})


class PostBodyGuardTest(unittest.TestCase):
    """`post` asks the host's public-body guard before it queues, as the gh shim does for `gh pr|issue` writes
    (cmuxterm-hq#4946: ten public cmux PRs named fleet hosts after `glaeda-gh post pr`)."""

    GUARD = (
        "import re, subprocess\n"
        "CALLS = []\n"
        "def _body(args):\n"
        "    return open(args[args.index('--body-file') + 1]).read()\n"
        "def refusal(args, env):\n"
        "    CALLS.append(list(args))\n"
        "    repo = args[args.index('-R') + 1]\n"
        "    if repo.startswith('pub/') and re.search(r'(?<![\\w-])cmux-mini-7(?![\\w-])', _body(args)):\n"
        "        return 'public-body-guard: refused: ' + repo + ' is public and this text names fleet host cmux-mini-7'\n"
        "def footer_refusal(args, env):\n"
        "    if args[:2] == ['pr', 'create'] and 'Agent session:' not in _body(args):\n"
        "        return 'pr-footer-guard: refused: no footer'\n"
    )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = gg.BASE
        gg.BASE = Path(self.tmp.name) / "gh"
        self.addCleanup(setattr, gg, "BASE", saved)
        self.guard = Path(self.tmp.name) / "public_body_guard.py"
        self.guard.write_text(self.GUARD)
        env = mock.patch.dict(os.environ, {gg.BODY_GUARD_ENV: str(self.guard)})
        env.start()
        self.addCleanup(env.stop)

    def post(self, *argv: str) -> tuple[int, str]:
        err = io.StringIO()
        with mock.patch.object(gg, "daemon_running", return_value=True), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            code = gg.main(["post", *argv])
        return code, err.getvalue()

    def queued(self) -> int:
        return len(list(gg.outbox_dir().glob("*.json"))) if gg.outbox_dir().is_dir() else 0

    def test_a_comment_naming_a_host_in_a_public_repo_is_refused_and_not_queued(self) -> None:
        code, err = self.post("comment", "pub/cmux#7", "--body", "built on cmux-mini-7")
        self.assertEqual(code, 3)
        self.assertIn("not queued: public-body-guard: refused: pub/cmux is public", err)
        self.assertEqual(self.queued(), 0)

    def test_the_same_text_in_a_private_repo_or_clean_text_is_queued(self) -> None:
        self.assertEqual(self.post("comment", "priv/hq#7", "--body", "built on cmux-mini-7")[0], 0)
        self.assertEqual(self.post("comment", "pub/cmux#7", "--body", "built on the fleet")[0], 0)
        self.assertEqual(self.queued(), 2)

    def test_reviews_and_prs_are_checked_as_the_gh_calls_they_stand_for(self) -> None:
        self.assertEqual(self.post("review", "pub/cmux#7", "--body", "cmux-mini-7")[0], 3)
        code, err = self.post("pr", "pub/cmux", "--head", "h", "--base", "b", "--title", "t", "--body", "x")
        self.assertEqual(code, 3)
        self.assertIn("pr-footer-guard", err)
        self.assertEqual(self.post("pr", "pub/cmux", "--head", "h", "--base", "b", "--title", "t",
                                   "--body", "x\n\nAgent session: [s](https://cmux.com/deeplink/agent?id=aid-1)")[0], 0)
        self.assertEqual(self.queued(), 1)

    def test_a_guard_that_breaks_queues_nothing(self) -> None:
        self.guard.write_text("def refusal(args, env):\n    raise RuntimeError('boom')\n")
        code, err = self.post("comment", "pub/cmux#7", "--body", "x")
        self.assertEqual(code, 3)
        self.assertIn("the body guard failed (RuntimeError: boom)", err)
        self.guard.unlink()
        self.assertIn("is missing", self.post("comment", "pub/cmux#7", "--body", "x")[1])
        self.assertEqual(self.queued(), 0)

    def test_the_body_file_is_removed_even_when_the_write_fails(self) -> None:
        tmp = Path(self.tmp.name) / "t"
        tmp.mkdir()
        with mock.patch.object(gg.tempfile, "tempdir", str(tmp)), \
                mock.patch.object(gg.os, "fdopen", side_effect=OSError(28, "No space left on device")):
            code, err = self.post("comment", "pub/cmux#7", "--body", "x")
        self.assertEqual(code, 3)
        self.assertIn("No space left on device", err)
        self.assertEqual(list(tmp.iterdir()), [])

    def test_no_guard_on_the_host_posts_as_before(self) -> None:
        with mock.patch.dict(os.environ, {gg.BODY_GUARD_ENV: ""}), \
                mock.patch.object(gg, "PACE_CLI", Path(self.tmp.name) / "nowhere" / "gh"):
            self.assertEqual(self.post("comment", "pub/cmux#7", "--body", "cmux-mini-7")[0], 0)

    def test_the_args_are_the_gh_calls_the_shim_guards(self) -> None:
        item = {"repo": "o/r", "kind": "comment", "number": 5}
        self.assertEqual(gg.body_guard_args(item, "F"), ["issue", "comment", "5", "-R", "o/r", "--body-file", "F"])
        item.update(kind="review")
        self.assertEqual(gg.body_guard_args(item, "F"), ["pr", "review", "5", "-R", "o/r", "--body-file", "F"])
        pr = {"repo": "o/r", "kind": "pr", "title": "T", "head": "h", "base": "b"}
        self.assertEqual(gg.body_guard_args(pr, "F"), ["pr", "create", "-R", "o/r", "--title", "T", "--head", "h",
                                                        "--base", "b", "--body-file", "F"])


class RiskReceiptPostTest(unittest.TestCase):
    """A delivered exact-head review wakes cmux's existing risk grader, without replaying content."""

    setUp = PostTest.setUp
    daemon = PostTest.daemon
    queue = PostTest.queue
    step = PostTest.step
    head = "a" * 40
    url = "https://github.com/manaflow-ai/cmux/pull/7#issuecomment-1"

    def receipt(self, **changes) -> dict:
        return {"body": f"Review (independent Codex): SHIP at {self.head}", "html_url": self.url,
                "user": {"type": "User", "login": "reviewer"}, "author_association": "COLLABORATOR", **changes}

    def pull(self, **changes) -> dict:
        return {"state": "open", "base": {"ref": "feat-cmux-next"}, "head": {"sha": self.head},
                "labels": [{"name": "risk:high"}, {"name": "ready-to-land"}], **changes}

    def test_risk_receipt_immediately_regrades_comments_and_reviews_including_block(self) -> None:
        for kind, verdict in (("comment", "SHIP"), ("review", "SHIP"), ("comment", "BLOCK"),
                              ("review", "FIX"), ("comment", "NOT SHIP"), ("comment", "HOLD")):
            with self.subTest(kind=kind, verdict=verdict):
                body = f"Review (independent Codex): {verdict} at {self.head}\nDetails."
                item = self.queue(kind, "manaflow-ai/cmux#7", "--body", body)
                d, t = self.daemon((201, {}, self.receipt(body=body)), (200, {}, self.pull()), (200, {}, []))
                self.step(d, NOW)
                self.assertEqual([c[:2] for c in t.calls[1:]], [
                    ("GET", "/repos/manaflow-ai/cmux/pulls/7"),
                    ("DELETE", "/repos/manaflow-ai/cmux/issues/7/labels/risk%3Ahigh")])
                state = gg.post_state(item["id"])
                self.assertEqual((state["state"], state["riskRegrade"]["state"]), ("posted", "triggered"))
                self.assertEqual(state["riskRegrade"]["head"], self.head)
                self.step(d, NOW + gg.POST_SPACING)
                self.assertEqual(len(t.calls), 3)

    def test_risk_receipt_reconciliation_triggers_without_a_second_comment(self) -> None:
        for kind in ("comment", "review"):
            with self.subTest(kind=kind):
                body = "\n" + self.receipt()["body"]  # the classifier reads the first nonblank line
                item = self.queue(kind, "manaflow-ai/cmux#7", "--body", body)
                d, t = self.daemon((502, {}, {}), (200, {}, [self.receipt(body=body)]),
                                   (200, {}, self.pull()), (200, {}, []))
                self.step(d, NOW)
                self.step(d, NOW + gg.POST_RETRY)
                self.assertEqual([c[0] for c in t.calls], ["POST", "GET", "GET", "DELETE"])
                self.assertEqual(gg.post_state(item["id"])["riskRegrade"]["state"], "triggered")

    def test_risk_receipt_ignores_unrelated_untrusted_self_and_nonfirstline_posts(self) -> None:
        for target, body, posted in (
                ("o/r#7", self.receipt()["body"], self.receipt()),
                ("manaflow-ai/cmux#7", "ordinary comment", self.receipt(body="ordinary comment")),
                ("manaflow-ai/cmux#7", f"Intro\n{self.receipt()['body']}", self.receipt()),
                ("manaflow-ai/cmux#7", f"Review (self): SHIP at {self.head}", self.receipt()),
                ("manaflow-ai/cmux#7", f"Review (Codex): SHIP at {self.head[:12]}", self.receipt()),
                ("manaflow-ai/cmux#7", f"Review (Codex): SHIP at {self.head}a", self.receipt()),
                ("manaflow-ai/cmux#7", self.receipt()["body"], self.receipt(author_association="CONTRIBUTOR")),
                ("manaflow-ai/cmux#7", self.receipt()["body"], self.receipt(user={"type": "Bot"})),
                ("manaflow-ai/cmux#7", self.receipt()["body"], {"html_url": self.url})):
            with self.subTest(target=target, body=body, posted=posted):
                item = self.queue("comment", target, "--body", body)
                d, t = self.daemon((201, {}, posted))
                self.step(d, NOW)
                self.assertEqual(len(t.calls), 1)
                self.assertEqual(gg.post_state(item["id"])["state"], "posted")

    def test_risk_receipt_reads_fresh_identity_and_only_removes_one_current_tier(self) -> None:
        # Only risk:high reads a receipt (cmux scripts/ci/risk_tier.py), so low and normal are left alone: their
        # label churn re-ran every labeled-triggered workflow (a whole CI run) and changed no verdict.
        for pull, label in ((self.pull(), "risk:high"),
                            (self.pull(labels=[{"name": "risk:normal"}]), None),
                            (self.pull(labels=[{"name": "risk:low"}]), None),
                            (self.pull(state="closed"), None),
                            (self.pull(base={"ref": "main"}), None),
                            (self.pull(head={"sha": "b" * 40}), None),
                            (self.pull(labels=[{"name": "risk:human-ok"}]), None),
                            (self.pull(labels=[{"name": "risk:high"}, {"name": "risk:low"}]), None)):
            with self.subTest(pull=pull):
                item = self.queue("comment", "manaflow-ai/cmux#7", "--body", self.receipt()["body"])
                d, t = self.daemon((201, {}, self.receipt()), (200, {}, pull), (200, {}, []))
                self.step(d, NOW)
                self.assertEqual(len(t.calls), 3 if label else 2)
                if label:
                    self.assertEqual(t.calls[-1][1], f"/repos/manaflow-ai/cmux/issues/7/labels/{label.replace(':', '%3A')}")
                self.assertEqual(gg.post_state(item["id"])["riskRegrade"]["state"], "triggered" if label else "skipped")

    def test_risk_receipt_regrade_failure_never_reposts_or_retries_the_comment(self) -> None:
        for answer in ((403, {"retry-after": "120"}, {"message": "secondary rate limit"}),
                       (502, {}, {}), gg.WatchError("request failed: TimeoutError")):
            with self.subTest(answer=answer):
                item = self.queue("comment", "manaflow-ai/cmux#7", "--body", self.receipt()["body"])
                d, t = self.daemon((201, {}, self.receipt()), (200, {}, self.pull()), answer)
                self.step(d, NOW)
                state = gg.post_state(item["id"])
                self.assertEqual((state["state"], state["url"]), ("posted", self.url))
                self.assertEqual(state["riskRegrade"]["state"], "failed")
                self.assertIn("risk regrade", state["note"])
                self.step(d, NOW + 600)
                self.assertEqual(len(t.calls), 3)

    def test_risk_receipt_checkpoint_resumes_after_delivery_without_reposting(self) -> None:
        item = self.queue("comment", "manaflow-ai/cmux#7", "--body", self.receipt()["body"])
        d, _ = self.daemon((201, {}, self.receipt()), gg.OutOfTime())
        self.step(d, NOW)
        queued = gg.read_json(gg.outbox_dir() / f"{item['id']}.json")
        self.assertEqual(queued["delivered"]["url"], self.url)
        again, t = self.daemon((200, {}, self.pull()), (200, {}, []))
        self.step(again, NOW + gg.POST_SPACING)
        self.assertEqual([c[0] for c in t.calls], ["GET", "DELETE"])
        self.assertEqual(gg.post_state(item["id"])["riskRegrade"]["state"], "triggered")

    def test_risk_receipt_crash_during_label_write_does_not_repeat_an_uncertain_effect(self) -> None:
        item = self.queue("comment", "manaflow-ai/cmux#7", "--body", self.receipt()["body"])
        d, transport = self.daemon((201, {}, self.receipt()), (200, {}, self.pull()))

        def crash(method, *args):
            if method == "DELETE":
                raise SystemExit
            return transport(method, *args)

        d.transport = crash
        with self.assertRaises(SystemExit):
            self.step(d, NOW)
        again, t = self.daemon()
        self.step(again, NOW + gg.POST_SPACING)
        self.assertEqual(t.calls, [])
        state = gg.post_state(item["id"])
        self.assertEqual((state["state"], state["riskRegrade"]["state"]), ("posted", "uncertain"))

    def test_risk_receipt_respects_stopped_rest_budget(self) -> None:
        item = self.queue("comment", "manaflow-ai/cmux#7", "--body", self.receipt()["body"])
        d, t = self.daemon((201, {}, self.receipt()))
        d.note_budget("rest", 5000, 0, NOW + 300)
        self.step(d, NOW)
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(gg.post_state(item["id"])["riskRegrade"]["state"], "failed")

    def test_risk_receipt_metadata_failure_finishes_delivery_without_label_effect(self) -> None:
        for answer in ((404, {}, {}), (429, {"retry-after": "120"}, {}),
                       gg.WatchError("request failed: TimeoutError")):
            with self.subTest(answer=answer):
                item = self.queue("comment", "manaflow-ai/cmux#7", "--body", self.receipt()["body"])
                d, t = self.daemon((201, {}, self.receipt()), answer)
                self.step(d, NOW)
                state = gg.post_state(item["id"])
                self.assertEqual((state["state"], state["riskRegrade"]["state"]), ("posted", "failed"))
                self.assertEqual(len(t.calls), 2)
                if isinstance(answer, tuple) and answer[0] == 429:
                    # held from when the refusal arrived (the real clock, a moment after the tick's NOW)
                    self.assertGreaterEqual(d.backoff["rest"], NOW + 120)
                    self.assertLess(d.backoff["rest"], NOW + 130)

    def test_risk_receipt_label_throttle_holds_other_writes_without_replaying_delivery(self) -> None:
        item = self.queue("comment", "manaflow-ai/cmux#7", "--body", self.receipt()["body"])
        d, t = self.daemon((201, {}, self.receipt()), (200, {}, self.pull()),
                           (429, {"retry-after": "120"}, {}), (201, {}, {"html_url": "other"}))
        self.step(d, NOW)
        other = self.queue("comment", "o/r#8", "--body", "other")
        self.step(d, NOW + gg.POST_SPACING)
        self.assertEqual(len(t.calls), 3)
        self.assertEqual(d.post_hold, NOW + 120)
        self.step(d, NOW + 120)
        self.assertEqual(len(t.calls), 4)
        self.assertEqual(gg.post_state(item["id"])["riskRegrade"]["state"], "failed")
        self.assertEqual(gg.post_state(other["id"])["url"], "other")


class DaemonPolicyTest(Base):
    """~/.config/glaeda/gh-daemon.json: the cycle, the floors and the daemon's own REST share."""

    def setUp(self) -> None:
        super().setUp()
        self.saved_config = gg.DAEMON_CONFIG
        gg.DAEMON_CONFIG = Path(self.tmp.name) / "gh-daemon.json"
        self.addCleanup(setattr, gg, "DAEMON_CONFIG", self.saved_config)
        for name in ("FLEET_POLICY", "UPDATE_CONFIG"):
            self.addCleanup(setattr, gg, name, getattr(gg, name))
            setattr(gg, name, Path(self.tmp.name) / f"{name.lower()}.json")
        self.daemon.clock_real = False  # the share runs on daemon.now here, as tick(now=...) pins it

    def configure(self, doc: Any) -> None:
        gg.DAEMON_CONFIG.write_text(json.dumps(doc))
        self.daemon.policy = gg.daemon_policy()

    def spend_rest(self, n: int, at: float | None = None) -> None:
        minute = int(((NOW - 10) if at is None else at) // 60)
        self.daemon.rest_minutes[minute] = self.daemon.rest_minutes.get(minute, 0) + n

    def test_defaults_without_a_config(self) -> None:
        policy = gg.daemon_policy()
        self.assertEqual(policy["cycle"], 60)
        self.assertEqual(policy["commentCycle"], 15)
        self.assertEqual(policy["cycles"], {"pr": 60, "run": 60, "comment": 15, "head": 60})
        self.assertEqual(policy["floors"], {"rest": (1500, 300), "graphql": (500, 100)})
        self.assertEqual(policy["restShare"], 0.4)

    def test_values_are_read_and_bad_ones_keep_their_default(self) -> None:
        self.configure({"cycle": 120, "commentCycle": 30, "restShare": 0.25,
                        "floors": {"rest": {"floor": 2000, "reserve": 400}, "graphql": {"floor": 800}}})
        policy = self.daemon.policy
        self.assertEqual(policy["cycles"], {"pr": 120, "run": 120, "comment": 30, "head": 120})
        self.assertEqual(policy["floors"], {"rest": (2000, 400), "graphql": (800, 100)})
        self.assertEqual(policy["restShare"], 0.25)
        # out of range, the wrong type, NaN, a bool, and a reserve above its floor
        self.configure({"cycle": 1, "commentCycle": "5", "restShare": -0.1, "floors": {
            "rest": {"floor": 100, "reserve": 200}, "graphql": {"floor": True, "reserve": float("nan")}}})
        policy = self.daemon.policy
        self.assertEqual(policy["cycles"], {"pr": 60, "run": 60, "comment": 15, "head": 60})
        self.assertEqual(policy["floors"], {"rest": (1500, 300), "graphql": (500, 100)})
        self.assertEqual(policy["restShare"], 0.4)
        self.configure({"restShare": 7, "cycle": 100000})
        self.assertEqual((self.daemon.policy["restShare"], self.daemon.policy["cycle"]), (0.4, 60))
        gg.DAEMON_CONFIG.write_text("{not json")
        self.assertEqual(gg.daemon_policy()["cycle"], 60)

    def test_the_fleet_policy_gives_this_host_its_share_under_the_local_config(self) -> None:
        gg.FLEET_POLICY.write_text(json.dumps({"default": {"restShare": 0.0, "cycle": 90},
                                               "hosts": {"lane-a": {"restShare": 0.25}, "lane-b": "junk"}}))
        gg.UPDATE_CONFIG.write_text(json.dumps({"host": "lane-a", "ring": "stable"}))
        self.assertEqual((gg.daemon_policy()["restShare"], gg.daemon_policy()["cycle"]), (0.25, 90))
        gg.UPDATE_CONFIG.write_text(json.dumps({"host": "lane-b"}))
        self.assertEqual(gg.daemon_policy()["restShare"], 0.0)  # a bad entry is the default
        gg.UPDATE_CONFIG.write_text(json.dumps({"host": "lane-c"}))
        self.assertEqual(gg.daemon_policy()["restShare"], 0.0)  # not named: the default
        gg.DAEMON_CONFIG.write_text(json.dumps({"restShare": 0.3}))
        self.assertEqual((gg.daemon_policy()["restShare"], gg.daemon_policy()["cycle"]), (0.3, 90))
        gg.UPDATE_CONFIG.unlink()  # no update.json: the short hostname
        gg.DAEMON_CONFIG.unlink()
        with mock.patch.object(gg.socket, "gethostname", return_value="Lane-A.local"):
            self.assertEqual(gg.daemon_policy()["restShare"], 0.25)

    def test_the_shipped_fleet_policy_keeps_every_daemon_within_the_account_share(self) -> None:
        doc = json.loads((ROOT / "config" / "gh-daemon.json").read_text())
        shares = [layer["restShare"] for layer in (doc["default"], *doc["hosts"].values())]
        self.assertTrue(all(0 <= share <= 1 for share in shares), shares)
        self.assertEqual(doc["default"]["restShare"], 0)  # a host not named must not add to the sum
        self.assertLessEqual(sum(shares), doc["maxRestShare"])
        # 0.1: REST is what gh, landers and reruns must use; the daemons read over GraphQL (2026-10-30)
        self.assertLessEqual(doc["maxRestShare"], 0.1)

    def test_the_shipped_fleet_policy_keeps_a_graphql_reserve_for_lands(self) -> None:
        """config/gh-daemon.json: every daemon stops GraphQL below 1000 left, the share of the hour kept for lands,
        and the per-minute ceiling is read from the policy."""
        gg.FLEET_POLICY.write_text((ROOT / "config" / "gh-daemon.json").read_text())
        gg.UPDATE_CONFIG.write_text(json.dumps({"host": "not-named"}))
        policy = gg.daemon_policy()
        self.assertGreaterEqual(policy["floors"]["graphql"][1], 1000)
        self.assertGreater(policy["floors"]["graphql"][0], policy["floors"]["graphql"][1])
        self.assertEqual(policy["graphqlPerMinute"], gg.GRAPHQL_PER_MINUTE)
        self.daemon.policy = policy
        self.daemon.now = NOW
        self.daemon.budget = {"graphql": {"limit": 5000, "remaining": 999, "reset": NOW + 600}}
        self.assertEqual(self.daemon.mode("graphql"), "stop")
        self.daemon.budget["graphql"]["remaining"] = 1200
        self.assertEqual(self.daemon.mode("graphql"), "lean")
        self.configure({"graphqlPerMinute": 12})
        self.assertEqual(self.daemon.policy["graphqlPerMinute"], 12)
        self.configure({"graphqlPerMinute": 0})  # out of range: the default
        self.assertEqual(self.daemon.policy["graphqlPerMinute"], gg.GRAPHQL_PER_MINUTE)

    def test_a_zero_share_reads_prs_and_runs_over_graphql(self) -> None:
        """A host the fleet policy does not name: every PR and run read is GraphQL. The one REST read left
        is the cap's own exemption, a failed attempt's job list, once per attempt."""
        self.daemon.now = NOW
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 5000, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 1000, "reset": NOW + 1800}}
        self.configure({"restShare": 0})
        self.assertEqual((self.daemon.rest_cap(), self.daemon.rest_capped()), (0, True))
        self.assertEqual(self.daemon.route("pr"), "graphql")
        self.assertEqual(self.daemon.route("run"), "graphql")
        self.gh.prs[("o", "r", 1)] = pr_node(1)
        self.gh.runs[7] = {"id": 7, "name": "CI", "display_title": "fix", "status": "completed", "conclusion": "failure",
                           "html_url": "https://github.com/o/r/actions/runs/7", "head_sha": "b" * 40, "head_branch": "x",
                           "event": "push", "run_attempt": 1, "created_at": iso(NOW - 99), "updated_at": iso(NOW - 9)}
        self.gh.jobs[7] = [job("build")]
        gg.register("pr:o/r#1")
        gg.register("run:o/r/7")
        gg.DAEMON_CONFIG.write_text(json.dumps({"restShare": 0}))
        self.daemon.tick(NOW)
        self.assertGreaterEqual(len(self.gh.gql_calls()), 1)
        self.assertEqual(self.gh.run_calls(), [])
        self.assertEqual(len(self.gh.job_calls()), 1)
        gg.register("run:o/r/7")
        self.daemon.tick(NOW + 200)
        self.assertEqual(len(self.gh.job_calls()), 1)  # once per attempt

    def test_settled_heads_stay_fresh_for_two_configured_cycles(self) -> None:
        entry = {"data": {"settled": True}, "dataAt": NOW - (gg.HEAD_SETTLED_TTL + 2 * 60 + 30)}
        self.assertFalse(gg.head_served(entry, NOW, 30)[1])  # the default cycle: stale
        self.configure({"cycle": 300})
        self.assertTrue(gg.head_served(entry, NOW, 30)[1])  # the daemon re-reads it later than that
        self.assertFalse(gg.head_served({"data": {"settled": False}, "dataAt": NOW - 60}, NOW, 30)[1])

    def test_floors_from_the_config_drive_the_mode(self) -> None:
        self.daemon.now = NOW
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 1000, "reset": NOW + 1800}}
        self.assertEqual(self.daemon.mode("rest"), "lean")  # under the 1500 default
        self.configure({"floors": {"rest": {"floor": 800, "reserve": 100}}})
        self.assertEqual(self.daemon.mode("rest"), "normal")
        self.configure({"floors": {"rest": {"floor": 3000, "reserve": 1200}}})
        self.assertEqual(self.daemon.mode("rest"), "stop")

    def test_a_tick_rereads_the_config(self) -> None:
        self.configure({"cycle": 300})
        gg.DAEMON_CONFIG.write_text(json.dumps({"cycle": 90}))
        self.daemon.tick(NOW)
        self.assertEqual(self.daemon.policy["cycles"]["pr"], 90)

    def test_the_cycle_paces_reads(self) -> None:
        self.configure({"cycle": 120})
        self.gh.prs[("o", "r", 1)] = pr_node(1)
        gg.register("pr:o/r#1")
        gg.DAEMON_CONFIG.write_text(json.dumps({"cycle": 120}))
        self.daemon.tick(NOW)
        self.assertEqual(len(self.gh.gql_calls()), 1)
        gg.register("pr:o/r#1")
        self.daemon.tick(NOW + 61)
        self.assertEqual(len(self.gh.gql_calls()), 1)  # 60 s was the old cycle: not due yet
        gg.register("pr:o/r#1")
        self.daemon.tick(NOW + 121)
        self.assertEqual(len(self.gh.gql_calls()), 2)

    def test_the_rest_share_moves_balanced_kinds_to_graphql_and_leans_rest(self) -> None:
        self.daemon.now = NOW
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4500, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 1000, "reset": NOW + 1800}}
        self.assertEqual(self.daemon.route("pr"), "rest")  # REST is far richer
        self.assertEqual(self.daemon.mode("rest"), "normal")
        self.spend_rest(2000 - gg.REST_READ_MAX["pr"])
        self.assertFalse(self.daemon.rest_capped())  # the largest read still fits
        self.spend_rest(1)
        self.assertEqual(self.daemon.rest_cap(), 2000)
        self.assertTrue(self.daemon.rest_capped())  # it no longer does: REST is lean, reads go to GraphQL
        self.assertEqual(self.daemon.richness("rest"), -1)
        self.assertEqual(self.daemon.mode("rest"), "lean")
        self.assertEqual(self.daemon.route("pr"), "graphql")
        self.assertEqual(self.daemon.route("run"), "graphql")
        # an hour later (and the minute they were sent in) those requests no longer count
        self.daemon.now = NOW + 3660
        self.daemon.budget["rest"]["reset"] = NOW + 5000
        self.daemon.budget["graphql"]["reset"] = NOW + 5000
        self.assertFalse(self.daemon.rest_capped())
        self.assertEqual(self.daemon.mode("rest"), "normal")

    def test_the_share_follows_the_config_and_the_limit(self) -> None:
        self.daemon.now = NOW
        self.daemon.budget = {"rest": {"limit": 15000, "remaining": 14000, "reset": NOW + 1800}}
        self.configure({"restShare": 0.1})
        self.assertEqual(self.daemon.rest_cap(), 1500)
        self.spend_rest(1500)
        self.assertEqual(self.daemon.mode("rest"), "lean")

    def test_capped_with_graphql_stopped_reads_nothing_over_rest(self) -> None:
        self.daemon.now = NOW
        self.gh.rest_remaining = gg.FLOORS["rest"][0]  # and GitHub agrees: REST is at its floor
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": gg.FLOORS["rest"][0], "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 10, "reset": NOW + 1800}}
        self.daemon.paths["run"] = "rest"
        self.spend_rest(2000)
        self.assertEqual(self.daemon.route("run"), "rest")  # nowhere richer to go
        self.assertEqual(self.daemon.mode("rest"), "lean")
        # REST at its floor: the share holds even so, and the run waits rather than spend what the sessions have left
        self.gh.runs[7] = {"id": 7, "status": "in_progress", "html_url": "u"}
        gg.register("run:o/r/7")
        self.daemon.tick(NOW)
        self.assertEqual(self.gh.rest_calls(), [])

    def test_the_share_yields_while_graphql_is_out_and_rest_is_above_its_floor(self) -> None:
        """A host whose REST share is spent (or 0) still reads over REST while GraphQL cannot, down to REST's floor."""
        self.configure({"restShare": 0.0})
        self.daemon.policy = gg.daemon_policy()
        self.daemon.now = NOW
        floor = gg.FLOORS["rest"][0]
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": floor + 20, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 10, "reset": NOW + 1800}}
        self.assertTrue(self.daemon.rest_room(2))
        self.assertFalse(self.daemon.rest_room(21))  # it would take REST below its floor
        # nor is REST "capped" for routing then: every kind, the GraphQL ones too, goes to REST
        self.daemon.budget["rest"]["remaining"] = 4000
        self.assertFalse(self.daemon.rest_capped())
        self.assertEqual({k: self.daemon.route(k) for k in gg.CYCLES},
                         {"pr": "rest", "run": "rest", "comment": "rest", "head": "rest"})
        self.daemon.budget["rest"]["remaining"] = floor + 20
        self.daemon.budget["graphql"]["remaining"] = 4000  # GraphQL can read: the share holds again
        self.assertFalse(self.daemon.rest_room(1))
        self.daemon.budget["graphql"]["remaining"] = 10
        self.gh.runs[7] = {"id": 7, "status": "in_progress", "html_url": "u"}
        gg.register("run:o/r/7")
        self.daemon.tick(NOW)
        self.assertEqual(len(self.gh.run_calls()), 1)
        self.assertEqual(gg.read_cache("run:o/r/7")["via"], "rest")

    def test_a_yielding_share_reads_over_rest_at_the_lean_pace(self) -> None:
        """Past its share with GraphQL out, the daemon still reads over REST, but a quarter as often: on 2026-11-09 the
        yield ran at the full cycle, 1,271 counted REST requests in 15 minutes from one host whose share is 300/h."""
        self.configure({"restShare": 0.0})
        self.daemon.now = NOW
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4000, "reset": NOW + 3000},
                              "graphql": {"limit": 5000, "remaining": 10, "reset": NOW + 3000}}
        self.assertTrue(self.daemon.past_share())
        self.assertFalse(self.daemon.rest_capped())  # the share yields: REST still reads
        self.assertEqual(self.daemon.mode("rest"), "lean")
        self.assertEqual({k: self.daemon.route(k) for k in gg.CYCLES},
                         {"pr": "rest", "run": "rest", "comment": "rest", "head": "rest"})
        self.gh.runs[7] = {"id": 7, "status": "in_progress", "html_url": "u"}
        gg.register("run:o/r/7")
        self.daemon.tick(NOW)
        self.assertEqual(len(self.gh.run_calls()), 1)
        for later in (NOW + 61, NOW + 121, NOW + 181):
            self.daemon.budget["rest"]["remaining"] = 4000
            self.daemon.budget["graphql"] = {"limit": 5000, "remaining": 10, "reset": NOW + 3000}
            gg.register("run:o/r/7")
            self.daemon.tick(later)
        self.assertEqual(len(self.gh.run_calls()), 1)  # the full cycle would have read it three more times
        self.daemon.budget["rest"]["remaining"] = 4000
        self.daemon.budget["graphql"] = {"limit": 5000, "remaining": 10, "reset": NOW + 3000}
        gg.register("run:o/r/7")
        self.daemon.tick(NOW + gg.CYCLE * gg.LEAN_FACTOR + 1)
        self.assertEqual(len(self.gh.run_calls()), 2)

    def test_a_yielding_share_admits_head_consumers_and_paces_every_head(self) -> None:
        """Review F1/F2 at 99070e23: lean only for being past the share must still admit an unreserved consumer, and a
        reserved one's head must be read at most once per lean cycle too."""
        self.configure({"restShare": 0.0})
        sha = "a" * 40
        for consumer in ("session-1", "autoland"):
            self.daemon.now = NOW
            self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4000, "reset": NOW + 3000},
                                  "graphql": {"limit": 5000, "remaining": 10, "reset": NOW + 3000}}
            self.daemon.attempted.clear()
            self.daemon.head_spend = {}
            entry = {"key": f"head:o/r@{sha}", "cache": None, "consumers": {consumer: NOW}}
            self.assertEqual(self.daemon.mode("rest"), "lean")
            self.assertEqual([e["key"] for e in self.daemon.due("head", [entry])], [entry["key"]], consumer)
            for later in (NOW + 91, NOW + 182):
                self.daemon.now = later
                entry["consumers"] = {consumer: later}
                self.assertEqual(self.daemon.due("head", [entry]), [], (consumer, later))
            self.daemon.now = NOW + gg.CYCLE * gg.LEAN_FACTOR + 1
            entry["consumers"] = {consumer: self.daemon.now}
            self.assertEqual([e["key"] for e in self.daemon.due("head", [entry])], [entry["key"]], consumer)

    def test_within_its_share_the_daemon_reads_rest_at_the_full_cycle(self) -> None:
        self.configure({"restShare": 0.1})
        self.daemon.now = NOW
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4000, "reset": NOW + 3000},
                              "graphql": {"limit": 5000, "remaining": 10, "reset": NOW + 3000}}
        self.assertFalse(self.daemon.past_share())
        self.assertEqual(self.daemon.mode("rest"), "normal")
        self.daemon.budget["rest"]["remaining"] = gg.FLOORS["rest"][0] - 1  # below the floor: lean, no fallback
        self.assertEqual(self.daemon.mode("rest"), "lean")
        self.assertFalse(self.daemon.rest_above_floor())
        self.assertEqual(self.daemon.route("comment"), "graphql")

    def test_only_counted_rest_requests_spend_the_share(self) -> None:
        self.daemon.now = NOW
        answers = iter([(200, {}, b"{}"), (304, {}, b""), (200, {}, b"{}")])
        self.daemon.transport = lambda *a: next(answers)
        self.daemon.request("GET", "/repos/o/r/pulls/1")
        self.daemon.request("GET", "/repos/o/r/pulls/1", etag="x")  # a 304 is free
        self.daemon.request("GET", "/rate_limit", free=True)
        self.assertEqual(self.daemon.rest_spent(), 1)

    def test_the_spend_survives_a_restart_and_expires_after_an_hour(self) -> None:
        self.spend_rest(1200)
        self.spend_rest(900, at=NOW - 3700)  # over an hour ago
        self.daemon.tick(NOW)
        again = gg.Daemon(transport=self.gh, token=lambda: TOKEN, controller=None)
        again.now = NOW
        self.assertEqual(again.rest_spent(), 1200)
        self.assertNotIn(str(int((NOW - 3700) // 60)), gg.read_json(gg.BASE / gg.REST_SPEND_FILE))

    def test_the_spend_is_on_disk_as_each_request_is_made(self) -> None:
        self.daemon.now = NOW
        self.daemon.transport = lambda *a: (200, {}, b"{}")
        self.daemon.request("GET", "/repos/o/r/pulls/1")
        self.daemon.request("GET", "/repos/o/r/pulls/2")
        # no finish_tick: a daemon killed here still leaves its spend for the next one
        again = gg.Daemon(transport=self.gh, token=lambda: TOKEN, controller=None)
        again.now = NOW
        self.assertEqual(again.rest_spent(), 2)

    def test_the_broker_horizon_follows_a_longer_cycle(self) -> None:
        self.assertEqual(gg.broker_max_age(["title"]), gg.BROKER_MAX_AGE)
        self.configure({"cycle": 300})
        self.assertEqual(gg.broker_max_age(["title"]), 330)
        self.assertEqual(gg.broker_max_age(["headRefOid"]), gg.BROKER_MAX_AGE)  # live fields keep theirs
        with mock.patch.dict(os.environ, {"GLAEDA_GH_MAX_AGE": "-1"}):
            self.assertEqual(gg.broker_max_age(["title"]), 330)
        gg.write_json(gg.BASE / "budget.json", {"rest": {"limit": 5000, "remaining": 1000, "reset": time.time() + 900}})
        self.assertEqual(gg.broker_max_age(["title"]), 300 * gg.LEAN_FACTOR + 30)  # lean polls that rarely

    def test_reaching_the_share_mid_tick_defers_the_rest_reads(self) -> None:
        for n in range(1, 21):
            self.gh.runs[n] = {"id": n, "status": "in_progress", "html_url": f"u{n}"}
            gg.register(f"run:o/r/{n}")
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4500, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 600, "reset": NOW + 1800}}
        self.daemon.paths["run"] = "rest"
        self.spend_rest(1985, at=NOW)  # one request a read; a read starts only while 2 more fit
        self.daemon.tick(NOW)
        self.assertEqual(len(self.gh.run_calls()), 14)
        self.assertEqual(self.daemon.rest_spent(), 1999)
        # the six REST had no room for are read over GraphQL in the same tick, in one query
        self.assertEqual(len(self.gh.gql_calls()), 1)
        self.assertEqual(self.daemon.deferred, set())
        self.assertEqual(sum(1 for n in range(1, 21) if gg.read_cache(f"run:o/r/{n}")["via"] == "graphql"), 6)
        # and the next tick routes them to GraphQL
        for n in range(1, 21):
            gg.register(f"run:o/r/{n}")
        self.daemon.tick(NOW + 2)
        self.assertEqual(self.daemon.paths["run"], "graphql")
        self.assertEqual(len(self.gh.run_calls()), 14)
        self.assertEqual(self.daemon.deferred, set())

    def test_a_request_whose_answer_never_arrives_is_still_counted(self) -> None:
        self.daemon.now = NOW
        def lost(*a):
            raise gg.WatchError("request failed: TimeoutError")  # GitHub may have counted it anyway
        self.daemon.transport = lost
        with self.assertRaises(gg.WatchError):
            self.daemon.request("GET", "/repos/o/r/pulls/1")
        self.assertEqual(self.daemon.rest_spent(), 1)
        self.daemon.transport = lambda *a: (304, {}, b"")
        self.daemon.request("GET", "/repos/o/r/pulls/1", etag="x")
        self.assertEqual(self.daemon.rest_spent(), 1)  # a 304 is refunded
        self.assertEqual(gg.read_json(gg.BASE / gg.REST_SPEND_FILE), {str(int(NOW // 60)): 1})

    def test_a_failed_attempt_gets_its_job_list_when_runs_cannot_be_read(self) -> None:
        """Capped REST and a stopped GraphQL: runs wait, but a failed attempt's job list does not."""
        failed = {"id": 7, "name": "CI", "display_title": "fix", "status": "completed", "conclusion": "failure",
                  "html_url": "https://github.com/o/r/actions/runs/7", "head_sha": "b" * 40, "head_branch": "x",
                  "event": "push", "run_attempt": 1, "created_at": iso(NOW - 99), "updated_at": iso(NOW - 9)}
        self.gh.runs[7] = failed
        self.gh.runs[8] = {**failed, "id": 8, "status": "in_progress", "conclusion": None}
        self.gh.jobs[7] = [job("build")]
        key, other = gg.parse_key("run", "o/r/7"), gg.parse_key("run", "o/r/8")
        self.daemon.store(key, None, data=gg.slim_run(failed), dataAt=NOW - 5)  # as the controller filled it
        self.daemon.store(other, None, data=gg.slim_run(self.gh.runs[8]), dataAt=NOW - 5)
        gg.register(key)
        gg.register(other)
        self.gh.rest_remaining = gg.FLOORS["rest"][0]  # and GitHub agrees: REST is at its floor
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": gg.FLOORS["rest"][0], "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 10, "reset": NOW + 1800}}
        self.daemon.paths["run"] = "rest"
        self.spend_rest(2000, at=NOW)
        self.daemon.tick(NOW)
        self.assertEqual(self.gh.run_calls(), [])  # neither run itself is read
        self.assertEqual(len(self.gh.job_calls()), 1)
        self.assertIn("/runs/7/jobs", self.gh.job_calls()[0][1])
        self.assertEqual(gg.read_cache(key)["jobs"][0]["name"], "build")
        gg.register(key)
        self.daemon.tick(NOW + 2 * gg.CYCLE)
        self.assertEqual(len(self.gh.job_calls()), 1)  # once per attempt

    def test_the_job_list_exemption_holds_with_runs_routed_to_a_stopped_graphql(self) -> None:
        failed = {"id": 7, "name": "CI", "display_title": "fix", "status": "completed", "conclusion": "failure",
                  "html_url": "https://github.com/o/r/actions/runs/7", "head_sha": "b" * 40, "head_branch": "x",
                  "event": "push", "run_attempt": 1, "created_at": iso(NOW - 99), "updated_at": iso(NOW - 9)}
        self.gh.runs[7] = failed
        self.gh.jobs[7] = [job("build")]
        key = gg.parse_key("run", "o/r/7")
        self.daemon.store(key, None, data=gg.slim_run(failed), dataAt=NOW - 5)
        gg.register(key)
        self.gh.rest_remaining = gg.FLOORS["rest"][0]  # and GitHub agrees: REST is at its floor
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": gg.FLOORS["rest"][0], "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 10, "reset": NOW + 1800}}
        self.daemon.paths["run"] = "graphql"  # routed there while REST was capped; then GraphQL stopped
        self.spend_rest(2000, at=NOW)
        self.daemon.tick(NOW)
        self.assertEqual(self.gh.run_calls(), [])
        self.assertEqual(self.gh.gql_calls(), [])
        self.assertEqual(len(self.gh.job_calls()), 1)

    def test_an_unanswered_job_list_is_not_asked_again_at_the_cap_over_graphql(self) -> None:
        self.gh.runs[7] = {"id": 7, "name": "CI", "display_title": "fix", "status": "completed", "conclusion": "failure",
                           "html_url": "https://github.com/o/r/actions/runs/7", "head_sha": "b" * 40, "head_branch": "x",
                           "event": "push", "run_attempt": 1, "created_at": iso(NOW - 99), "updated_at": iso(NOW - 9)}
        self.gh.jobs[7] = []  # an empty answer leaves the attempt without jobs
        key = gg.parse_key("run", "o/r/7")
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4500, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 4000, "reset": NOW + 1800}}
        self.spend_rest(2000, at=NOW)
        for i in range(4):  # inside TERMINAL_GRACE, a reader newer than each read: re-read every cycle
            gg.register(key)
            self.daemon.tick(NOW + i * gg.CYCLE / 2 + i)
        self.assertGreaterEqual(len(self.gh.gql_calls()), 2)  # the run itself is re-read over GraphQL
        self.assertEqual(len(self.gh.job_calls()), 1)

    def test_an_unanswered_job_list_is_not_asked_again_at_the_cap(self) -> None:
        failed = {"id": 7, "name": "CI", "display_title": "fix", "status": "completed", "conclusion": "failure",
                  "html_url": "https://github.com/o/r/actions/runs/7", "head_sha": "b" * 40, "head_branch": "x",
                  "event": "push", "run_attempt": 1, "created_at": iso(NOW - 99), "updated_at": iso(NOW - 9)}
        self.gh.runs[7] = failed
        self.gh.jobs[7] = []  # an empty answer leaves the attempt without jobs
        key = gg.parse_key("run", "o/r/7")
        self.daemon.store(key, None, data=gg.slim_run(failed), dataAt=NOW - 5)
        self.gh.rest_remaining = gg.FLOORS["rest"][0]  # and GitHub agrees: REST is at its floor
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": gg.FLOORS["rest"][0], "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 10, "reset": NOW + 1800}}
        self.daemon.paths["run"] = "rest"
        self.spend_rest(2000, at=NOW)
        for i in range(4):  # inside TERMINAL_GRACE, so the finished run stays watched
            gg.register(key)
            self.daemon.tick(NOW + i * 25)
            self.assertEqual(gg.read_cache(key)["jobsTried"], 1)
        self.assertEqual(len(self.gh.job_calls()), 1)
        # a new attempt is a new exemption
        cached = gg.read_cache(key)
        self.daemon.store(key, cached, data={**cached["data"], "run_attempt": 2})
        gg.register(key)
        self.daemon.tick(NOW + 110)
        self.assertEqual(len(self.gh.job_calls()), 2)

    def test_a_request_is_dated_by_its_own_minute(self) -> None:
        self.daemon.now = NOW - 90  # the tick began 90 s ago
        self.daemon.transport = lambda *a: (200, {}, b"{}")
        with mock.patch.object(self.daemon, "clock", return_value=NOW):
            self.daemon.request("GET", "/repos/o/r/pulls/1")
        self.assertEqual(self.daemon.rest_minutes, {int(NOW // 60): 1})

    def test_a_pr_read_is_admitted_only_when_its_worst_case_fits(self) -> None:
        self.gh.prs[("o", "r", 1)] = pr_node(1)
        gg.register("pr:o/r#1")
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4500, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 600, "reset": NOW + 1800}}
        self.daemon.paths["pr"] = "rest"
        self.spend_rest(2000 - gg.REST_READ_MAX["pr"] + 1, at=NOW)
        self.daemon.tick(NOW)
        self.assertEqual(self.gh.rest_calls(), [])  # 11 no longer fit: it went over GraphQL instead
        self.assertEqual(len(self.gh.gql_calls()), 1)
        self.assertFalse(self.daemon.rest_room(gg.REST_READ_MAX["pr"]))
        self.assertTrue(self.daemon.rest_room(gg.REST_READ_MAX["run"]))

    def test_the_outbox_does_not_spend_the_share(self) -> None:
        self.daemon.now = NOW
        self.daemon.transport = lambda *a: (201, {}, b"{}")
        self.daemon.writing = True  # as post_tick sets it around one write
        self.daemon.request("POST", "/repos/o/r/issues/1/comments", {"body": "x"}, content=True)
        self.daemon.request("DELETE", "/repos/o/r/issues/1/labels/x")
        self.daemon.request("GET", "/repos/o/r/issues/1/comments")  # the write's own dedupe lookup
        self.daemon.writing = False
        self.assertEqual(self.daemon.rest_spent(), 0)
        self.assertEqual([w for _, w in self.daemon.sent], ["write"] * 3)  # budget shows them apart
        self.daemon.request("GET", "/repos/o/r/pulls/1")  # outside the outbox, a read counts
        self.assertEqual(self.daemon.rest_spent(), 1)

    def test_a_job_list_a_tick_had_no_time_for_is_not_counted_as_tried(self) -> None:
        failed = {"id": 7, "name": "CI", "display_title": "fix", "status": "completed", "conclusion": "failure",
                  "html_url": "https://github.com/o/r/actions/runs/7", "head_sha": "b" * 40, "head_branch": "x",
                  "event": "push", "run_attempt": 1, "created_at": iso(NOW - 99), "updated_at": iso(NOW - 9)}
        key = gg.parse_key("run", "o/r/7")
        self.daemon.now = NOW
        self.spend_rest(2000, at=NOW)
        e = {"key": key, "jobs": False, "cache": {"data": gg.slim_run(failed)}}
        with mock.patch.object(self.daemon, "get", side_effect=gg.OutOfTime):
            self.daemon.read_jobs(e, {}, False)
        self.assertNotIn("jobsTried", gg.read_cache(key))
        self.assertIn(key, self.daemon.deferred)

    def test_a_tried_job_list_is_remembered_across_a_restart(self) -> None:
        failed = {"id": 7, "name": "CI", "display_title": "fix", "status": "completed", "conclusion": "failure",
                  "html_url": "https://github.com/o/r/actions/runs/7", "head_sha": "b" * 40, "head_branch": "x",
                  "event": "push", "run_attempt": 1, "created_at": iso(NOW - 99), "updated_at": iso(NOW - 9)}
        self.gh.runs[7] = failed
        self.gh.jobs[7] = []
        key = gg.parse_key("run", "o/r/7")
        self.daemon.now = NOW
        self.spend_rest(2000, at=NOW)
        self.daemon.read_jobs({"key": key, "jobs": False, "cache": None}, {"data": gg.slim_run(failed)}, True)
        self.assertEqual(len(self.gh.job_calls()), 1)
        again = gg.Daemon(transport=self.gh, token=lambda: TOKEN, controller=None)
        again.now, again.clock_real = NOW, False
        again.read_jobs({"key": key, "jobs": False, "cache": gg.read_cache(key)}, {}, False)
        self.assertEqual(len(self.gh.job_calls()), 1)  # the new daemon knows it was tried

    def test_a_controller_wait_keeps_the_tried_attempt(self) -> None:
        key = gg.parse_key("run", "o/r/7")
        gg.cache_dir().mkdir(parents=True, exist_ok=True)
        gg.write_json(gg.cache_dir() / gg.file_name(key),
                      {"key": key, "data": {"status": "completed", "run_attempt": 1}, "jobsTried": 1})
        controller = mock.Mock()
        controller.run.return_value = {"id": 7, "repo": "o/r", "attempt": 1}
        with mock.patch.object(gg.Controller, "from_env", return_value=controller), \
                mock.patch.object(gg, "controller_run", return_value={"status": "completed", "run_attempt": 1}):
            gg.controller_wait_run(key, 1)
        entry = gg.read_cache(key)
        self.assertEqual(entry["source"], "controller")  # the wait did rewrite it
        self.assertEqual(entry.get("jobsTried"), 1)

    def test_a_failed_attempt_still_gets_its_job_list_at_the_cap(self) -> None:
        """The one read the share does not hold back: a wait cannot rule on a failed run without it."""
        self.gh.runs[7] = {"id": 7, "name": "CI", "display_title": "fix", "status": "completed", "conclusion": "failure",
                           "html_url": "https://github.com/o/r/actions/runs/7", "head_sha": "b" * 40, "head_branch": "x",
                           "event": "push", "run_attempt": 1, "created_at": iso(NOW - 99), "updated_at": iso(NOW - 9)}
        self.gh.jobs[7] = [job("build")]
        self.daemon.budget = {"rest": {"limit": 5000, "remaining": 4500, "reset": NOW + 1800},
                              "graphql": {"limit": 5000, "remaining": 4000, "reset": NOW + 1800}}
        self.spend_rest(2000, at=NOW)
        gg.register("run:o/r/7")
        self.daemon.tick(NOW)
        self.assertEqual(self.gh.run_calls(), [])  # the run itself went over GraphQL
        self.assertEqual(len(self.gh.job_calls()), 1)

    def test_status_and_budget_show_the_share(self) -> None:
        self.spend_rest(990)
        self.daemon.tick(NOW)
        status = gg.read_json(gg.BASE / "daemon.json")
        self.assertEqual(status["restShare"], {"spent": 990, "cap": 2000, "share": 0.4, "capped": False})
        lock = open(gg.BASE / "daemon.lock", "a+")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(gg, "daemon_stale", return_value=None):
                gg.cmd_budget(False)
            self.assertIn("daemon REST spend 990/2000 this hour (restShare 0.4)", out.getvalue())
        finally:
            lock.close()

    def test_the_broker_reads_the_configured_floors(self) -> None:
        gg.write_json(gg.BASE / "budget.json", {"rest": {"limit": 5000, "remaining": 1000, "reset": time.time() + 900}})
        self.assertEqual(gg.broker_max_age(["title"]), gg.BROKER_LEAN_AGE)  # under the 1500 default
        self.configure({"floors": {"rest": {"floor": 800, "reserve": 100}}})
        self.assertEqual(gg.broker_max_age(["title"]), gg.BROKER_MAX_AGE)


class CountingGhBase(BrokerBase):
    """A fake gh that counts its runs and answers "answer N"."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["GLAEDA_GH_CACHE_TTL"] = "60"
        self.count = self.home / "count"
        gh = self.home / "bin" / "gh"
        gh.write_text("#!/bin/sh\n"
                      f"n=$(($(cat {self.count} 2>/dev/null || echo 0) + 1)); echo $n > {self.count}\n"
                      f"printf '%s\\n' \"$@\" >> {self.passed}\n"
                      "echo \"answer $n\"\necho \"note $n\" >&2\nexit ${FAKE_GH_RC:-0}\n")
        gh.chmod(0o755)

    def runs(self) -> int:
        return int(self.count.read_text()) if self.count.exists() else 0


class BrokerAnswerCacheTest(CountingGhBase):
    """Identical read-only calls within the TTL share one real gh run."""

    def test_an_identical_read_is_answered_once(self) -> None:
        args = ["api", "repos/o/r/actions/runs/5/jobs", "--jq", ".jobs[].name"]
        first = self.run_gh(args)
        second = self.run_gh(args)
        self.assertEqual(first[:3], (0, "answer 1\n", "note 1\n"))
        self.assertEqual(second[:3], (0, "answer 1\n", "note 1\n"))
        self.assertEqual(self.runs(), 1)
        routes = [json.loads(line).get("route") for line in gg.broker_log().read_text().splitlines()]
        self.assertIn("cache", routes)

    def test_an_answer_from_the_cache_still_counts_as_interest(self) -> None:
        """glaeda#1791: a read the answer cache serves keeps the daemon's watch wanted (its idle backoff and
        IDLE_EXPIRY go by the watch's mtime)."""
        args = ["pr", "view", "7", "-R", "o/r", "--json", "state,title"]
        self.run_gh(args)
        gg.register("pr:o/r#7")
        path = gg.watch_dir() / gg.file_name("pr:o/r#7")
        os.utime(path, (time.time() - 600,) * 2)
        self.run_gh(args)
        self.assertEqual(self.runs(), 1)  # answered from the cache
        self.assertLess(time.time() - path.stat().st_mtime, 60)

    def test_pr_view_and_run_view_are_kept(self) -> None:
        for args in (["pr", "view", "7", "-R", "o/r", "--json", "body"],
                     ["run", "view", "12", "-R", "o/r", "--log-failed"]):
            with self.subTest(args=args):
                before = self.runs()
                self.run_gh(args)
                _code, out, _err, _ = self.run_gh(args)
                self.assertEqual(self.runs(), before + 1)
                self.assertEqual(out, f"answer {before + 1}\n")

    def test_different_arguments_are_different_answers(self) -> None:
        self.run_gh(["api", "repos/o/r/actions/runs/5/jobs"])
        self.run_gh(["api", "repos/o/r/actions/runs/6/jobs"])
        self.run_gh(["api", "repos/o/r/actions/runs/5/jobs", "--jq", ".total_count"])
        self.assertEqual(self.runs(), 3)

    def test_an_answer_past_the_ttl_is_read_again(self) -> None:
        args = ["api", "repos/o/r/pulls/7"]
        self.run_gh(args)
        for path in gg.gh_cache_dir().glob("*.json"):
            if path.name != "writes.json":
                doc = json.loads(path.read_text())
                doc["at"] -= 61
                path.write_text(json.dumps(doc))
        _code, out, _err, _ = self.run_gh(args)
        self.assertEqual((out, self.runs()), ("answer 2\n", 2))

    def test_a_failed_answer_is_not_kept(self) -> None:
        os.environ["FAKE_GH_RC"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_GH_RC", None)
        args = ["api", "repos/o/r/pulls/7"]
        first = self.run_gh(args)
        second = self.run_gh(args)
        self.assertEqual((first[0], second[0], self.runs()), (1, 1, 2))

    def test_a_write_to_the_repository_is_never_answered_with_the_read_before_it(self) -> None:
        read = ["pr", "view", "7", "-R", "o/r", "--json", "body"]
        self.run_gh(read)
        self.run_gh(["api", "repos/o/other/pulls/9"])
        comment = ["pr", "comment", "7", "-R", "o/r", "--body", "hi"]
        code, _out, _err, passed = self.run_gh(comment)
        self.assertEqual((code, passed, self.runs()), (0, None, 3), "a write runs once, as a child")
        self.assertIn("comment", self.passed.read_text())
        _code, out, _err, _ = self.run_gh(read)
        self.assertEqual(out, "answer 4\n")
        _code, out, _err, _ = self.run_gh(["api", "repos/o/other/pulls/9"])
        self.assertEqual(out, "answer 2\n", "a write to another repository leaves this one's answers")

    def test_a_read_during_a_write_is_not_kept_for_others(self) -> None:
        """A write is stamped again when it finishes, so an answer read while it was in flight is
        older than that stamp and is not served."""
        key = "e" * 40
        started = time.time()
        gg.gh_cache_put(key, started, 0, b"old\n", b"")
        self.assertIsNotNone(gg.gh_cache_get(key, "o/r", started + 1))
        gg.gh_cache_note_write("o/r", started + 0.5)
        self.assertIsNone(gg.gh_cache_get(key, "o/r", started + 1))

    def test_a_failing_write_still_returns_its_code_and_stamps(self) -> None:
        os.environ["FAKE_GH_RC"] = "3"
        self.addCleanup(os.environ.pop, "FAKE_GH_RC", None)
        code, _out, _err, passed = self.run_gh(["pr", "merge", "7", "-R", "o/r"])
        self.assertEqual((code, passed, self.runs()), (3, None, 1))
        self.assertTrue({"o/r pr 7", "o/r prs"} <= set(gg.gh_cache_writes()))

    def test_a_write_naming_no_repository_stamps_them_all(self) -> None:
        self.run_gh(["api", "repos/o/r/pulls/7"])
        self.run_gh(["api", "-X", "PATCH", "user", "-f", "name=x"])
        _code, out, _err, _ = self.run_gh(["api", "repos/o/r/pulls/7"])
        self.assertEqual(out, "answer 3\n")

    def test_uncacheable_reads_go_straight_to_gh(self) -> None:
        for args in (["api", "-i", "repos/o/r/pulls/7"],
                     ["api", "--verbose", "repos/o/r/pulls/7"],
                     ["api", "repos/{owner}/{repo}/branches/{branch}"],
                     ["api", "rate_limit"],
                     ["pr", "view"],
                     ["pr", "view", "7", "-R", "o/r", "--web"],
                     ["run", "view", "12", "-R", "o/r", "-w"],
                     ["pr", "checks", "-R", "o/r"],
                     ["pr", "diff", "--color", "never", "-R", "o/r"],
                     ["pr", "checks", "my-branch", "-R", "o/r"],
                     ["pr", "list", "-R", "o/r", "--web"]):
            with self.subTest(args=args):
                _code, _out, _err, passed = self.run_gh(args)
                self.assertEqual(passed, args)
        self.assertEqual(self.runs(), 0)

    def test_lists_searches_and_targeted_reads_are_kept(self) -> None:
        """The reads repeated most within 90 s on cosmic-gray (2026-10-29): issue list 82%, pr list
        64%, search prs 61%, run list 46%."""
        for args in (["pr", "list", "-R", "o/r", "--state", "open", "--json", "number,title"],
                     ["issue", "list", "-R", "o/r", "--label", "research"],
                     ["run", "list", "-R", "o/r", "--workflow", "ci.yml", "--limit", "5"],
                     ["search", "prs", "--repo", "o/r", "--state", "open", "fix"],
                     ["pr", "checks", "7", "-R", "o/r", "--json", "name,state"],
                     ["pr", "diff", "7", "-R", "o/r", "--name-only"],
                     ["issue", "view", "https://github.com/o/r/issues/9", "--json", "body"]):
            with self.subTest(args=args):
                before = self.runs()
                self.run_gh(args)
                _code, out, _err, _ = self.run_gh(args)
                self.assertEqual((self.runs(), out), (before + 1, f"answer {before + 1}\n"))

    def test_a_search_needs_no_checkout_and_any_write_ends_it(self) -> None:
        nowhere = self.home / "nowhere"
        nowhere.mkdir()
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(nowhere)
        args = ["search", "prs", "repo:o/r is:open", "--json", "number"]
        self.run_gh(args)
        self.run_gh(args)
        self.assertEqual(self.runs(), 1)
        self.run_gh(["pr", "comment", "7", "-R", "o/other", "--body", "x"])
        self.run_gh(args)
        self.assertEqual(self.runs(), 3, "a write to any repository can change a search")

    def test_a_data_read_is_shared_across_terminals_but_not_across_forced_colour(self) -> None:
        args = ["pr", "list", "-R", "o/r", "--json", "number"]
        words, method, seen, values = gg.broker_words(args)
        with mock.patch.object(sys.stdout, "isatty", return_value=False):
            plain = gg.gh_cache_key(args, words, values, seen, method, "/gh")
            for name, value in (("COLUMNS", "80"), ("TERM", "dumb"), ("LANG", "C"), ("GLAMOUR_STYLE", "dark"),
                                ("PAGER", "less")):
                with self.subTest(name=name), mock.patch.dict(os.environ, {name: value}):
                    self.assertEqual(gg.gh_cache_key(args, words, values, seen, method, "/gh"), plain)
            for name in ("GH_FORCE_TTY", "CLICOLOR_FORCE"):
                with self.subTest(name=name), mock.patch.dict(os.environ, {name: "1", "COLUMNS": "80"}):
                    self.assertNotEqual(gg.gh_cache_key(args, words, values, seen, method, "/gh"), plain)
            rendered = ["pr", "list", "-R", "o/r"]
            words, method, seen, values = gg.broker_words(rendered)
            table = gg.gh_cache_key(rendered, words, values, seen, method, "/gh")
            with mock.patch.dict(os.environ, {"COLUMNS": "80"}):
                self.assertNotEqual(gg.gh_cache_key(rendered, words, values, seen, method, "/gh"), table)

    def test_a_job_log_is_kept_past_the_ttl(self) -> None:
        logs = ["api", "repos/o/r/actions/jobs/77/logs"]
        jobs = ["api", "repos/o/r/actions/runs/5/jobs"]
        self.run_gh(logs)
        self.run_gh(jobs)

        def age(seconds: float) -> None:
            for path in gg.gh_cache_dir().glob("*.json"):
                if path.name != "writes.json":
                    doc = json.loads(path.read_text())
                    doc["at"] -= seconds
                    path.write_text(json.dumps(doc))
        age(120)
        self.assertEqual(self.run_gh(logs)[1], "answer 1\n")
        self.assertEqual(self.run_gh(jobs)[1], "answer 3\n", "a jobs list is not a log")
        age(gg.GH_CACHE_LONG_TTL)
        self.assertEqual(self.run_gh(logs)[1], "answer 4\n")

    def test_the_resolved_repository_is_part_of_a_question_that_names_none(self) -> None:
        one, two = self.checkout("o/one"), self.checkout("o/two")
        nowhere = self.home / "nowhere"
        nowhere.mkdir()
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        for where in (one, two, one):
            os.chdir(where)
            self.run_gh(["pr", "view", "7", "--json", "body"])
            self.run_gh(["api", "repos/{owner}/{repo}/pulls/7"])
        self.assertEqual(self.runs(), 4)
        # the same directory, its remote changed: another repository, another question
        self.git_checkout(one, '[remote "origin"]\n\turl = git@github.com:o/three.git\n')
        os.chdir(one)
        self.run_gh(["pr", "view", "7", "--json", "body"])
        self.assertEqual(self.runs(), 5)
        # a directory whose repository cannot be known is never cached: exec'd, never filled
        os.chdir(nowhere)
        for _ in range(2):
            _code, _out, _err, passed = self.run_gh(["pr", "view", "7", "--json", "body"])
            self.assertIsNotNone(passed)
        self.assertEqual(self.runs(), 5)

    def test_writes_go_through_once_and_are_never_kept(self) -> None:
        for args in (["api", "-X", "POST", "repos/o/r/issues/7/comments", "-f", "body=hi"],
                     ["api", "repos/o/r/issues/7/comments", "-f", "body=hi"],
                     ["api", "graphql", "--input", "q.json"],
                     ["api", "graphql", "-F", "query=@q.graphql"],
                     ["auth", "switch"]):
            with self.subTest(args=args):
                before = self.runs()
                self.run_gh(args)
                self.run_gh(args)
                self.assertEqual(self.runs(), before + 2)

    def test_debug_output_is_never_kept(self) -> None:
        with mock.patch.dict(os.environ, {"GH_DEBUG": "api"}):
            _code, _out, _err, passed = self.run_gh(["api", "repos/o/r/pulls/7"])
        self.assertIsNotNone(passed)

    def test_another_account_in_hosts_yml_is_another_question(self) -> None:
        config = self.home / "gh-config"
        config.mkdir(exist_ok=True)
        (config / "hosts.yml").write_text("github.com:\n    user: one\n")
        args = ["api", "repos/o/r/pulls/7"]
        self.run_gh(args)
        self.run_gh(args)
        (config / "hosts.yml").write_text("github.com:\n    user: two\n")
        self.run_gh(args)
        self.assertEqual(self.runs(), 2)

    def test_a_fill_whose_lock_is_held_runs_gh_the_ordinary_way(self) -> None:
        key = "f" * 40
        gg.gh_cache_dir().mkdir(parents=True, exist_ok=True)
        with mock.patch.object(gg, "GH_CACHE_FILL_WAIT", 0.1), \
                gg.bounded_lock(gg.gh_cache_dir() / f".{key}.lock") as held:
            self.assertTrue(held)
            ran: list[int] = []
            self.assertIsNone(gg.gh_cache_fill(str(self.home / "bin" / "gh"), ["api", "x"], key, None, ran))
        self.assertEqual((ran, self.runs()), ([], 0))

    def test_a_hung_fill_is_stopped_at_its_deadline(self) -> None:
        hung = self.home / "bin" / "hung"
        hung.write_text("#!/bin/sh\nsleep 30\n")
        hung.chmod(0o755)
        started = time.monotonic()
        rc, _out, err = gg.gh_cache_child(str(hung), [], True, 0.5)
        self.assertEqual(rc, 124)
        self.assertIn(b"stopped", err)
        self.assertLess(time.monotonic() - started, 10)

    def test_a_queued_post_stamps_its_repository(self) -> None:
        no_guard(self)
        args = mock.Mock(kind="comment", target="O/R#7", body="hi", body_file=None, wait=0, json=False,
                         head=None, base=None, title=None, reviewer=None, event="comment")
        with mock.patch.object(gg, "daemon_running", return_value=False), \
                contextlib.redirect_stderr(io.StringIO()):
            gg.cmd_post(args)
        self.assertEqual(set(gg.gh_cache_writes()), {"o/r pr 7", "o/r runlists", "o/r other"})

    def test_another_token_is_another_question(self) -> None:
        args = ["api", "repos/o/r/pulls/7"]
        self.run_gh(args)
        os.environ["GH_TOKEN"] = "ghs_installation_token_for_another_account"
        self.run_gh(args)
        self.assertEqual(self.runs(), 2)
        stored = "".join(p.read_text() for p in gg.gh_cache_dir().glob("*.json"))
        self.assertNotIn("ghs_installation", stored)

    def test_off_when_the_ttl_is_zero_or_disabled(self) -> None:
        args = ["api", "repos/o/r/pulls/7"]
        for env in ({"GLAEDA_GH_CACHE_TTL": "0"}, {"GLAEDA_GH_CACHE_DISABLE": "1"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                _code, _out, _err, passed = self.run_gh(args)
                self.assertEqual(passed, args)

    def test_a_fill_that_finds_a_sibling_answer_does_not_run_gh(self) -> None:
        """Identical calls arriving together: the second takes the key's lock after the first stored
        its answer, and reads that instead of running gh again."""
        key = "k" * 40
        gg.gh_cache_put(key, time.time(), 0, b"stored\n", b"")
        ran: list[int] = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = gg.gh_cache_fill(str(self.home / "bin" / "gh"), ["api", "x"], key, None, ran)
        self.assertEqual((code, out.getvalue(), ran, self.runs()), (0, "stored\n", [0], 0))

    def test_a_big_answer_is_not_kept(self) -> None:
        key = "b" * 40
        gg.gh_cache_put(key, time.time(), 0, b"x" * (gg.GH_CACHE_MAX_BYTES + 1), b"")
        self.assertIsNone(gg.gh_cache_get(key, None, time.time()))

    def test_old_fill_locks_are_pruned_and_fresh_ones_kept(self) -> None:
        gg.gh_cache_dir().mkdir(parents=True, exist_ok=True)
        old, fresh = gg.gh_cache_dir() / (".o" * 20 + ".lock"), gg.gh_cache_dir() / (".f" * 20 + ".lock")
        old.touch()
        fresh.touch()
        os.utime(old, (time.time() - gg.GH_CACHE_LOCK_KEEP - 5,) * 2)
        gg.gh_cache_prune(time.time())
        self.assertEqual((old.exists(), fresh.exists()), (False, True))

    def test_a_held_lock_is_never_pruned(self) -> None:
        gg.gh_cache_dir().mkdir(parents=True, exist_ok=True)
        lock = gg.gh_cache_dir() / ".h.lock"
        lock.touch()
        os.utime(lock, (time.time() - gg.GH_CACHE_LOCK_KEEP - 5,) * 2)
        with open(lock, "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            gg.gh_cache_prune(time.time())
            self.assertTrue(lock.exists())

    def test_a_fill_lock_whose_file_was_replaced_is_not_held(self) -> None:
        """The interleaving: a fill opens the lock file, the pruner removes it, another fill creates a new one;
        the first, taking its lock, finds the path no longer its file and yields False."""
        gg.gh_cache_dir().mkdir(parents=True, exist_ok=True)
        lock = gg.gh_cache_dir() / ".r.lock"
        lock.touch()
        real_open = open

        def opened_then_replaced(path, *a, **k):
            handle = real_open(path, *a, **k)
            if Path(path) == lock:
                lock.unlink()
                lock.touch()
            return handle
        with mock.patch("builtins.open", opened_then_replaced):
            with gg.gh_cache_fill_lock(lock, 0.1) as held:
                self.assertFalse(held)
        with gg.gh_cache_fill_lock(lock, 0.1) as held:
            self.assertTrue(held)

    def test_old_answers_are_pruned(self) -> None:
        gg.gh_cache_put("c" * 40, time.time(), 0, b"x", b"")
        old = gg.gh_cache_dir() / ("c" * 40 + ".json")
        os.utime(old, (time.time() - gg.GH_CACHE_KEEP - 5,) * 2)
        (gg.gh_cache_dir() / ".pruned").unlink()
        gg.gh_cache_put("d" * 40, time.time(), 0, b"x", b"")
        self.assertFalse(old.exists())

    def test_what_counts_as_a_write(self) -> None:
        def write(args: list[str]) -> bool:
            words, method, _seen, _values = gg.broker_words(args)
            return gg.gh_cache_is_write(args, words, method)
        for args in (["pr", "merge", "7"], ["pr", "edit", "7"], ["run", "rerun", "5"], ["workflow", "run", "x"],
                     ["auth", "switch"], ["api", "graphql", "--input", "q.json"],
                     ["api", "-X", "DELETE", "repos/o/r/x"], ["api", "repos/o/r/x", "-F", "a=1"],
                     ["api", "graphql", "-f", "query=mutation{x}"], ["something-new", "verb"]):
            with self.subTest(args=args):
                self.assertTrue(write(args))
        for args in (["pr", "view", "7"], ["pr", "list"], ["run", "list"], ["api", "repos/o/r"],
                     ["api", "graphql", "-f", "query=query{viewer{login}}"], ["search", "prs", "x"], []):
            with self.subTest(args=args):
                self.assertFalse(write(args))


class BrokerAnswerHitRateTest(CountingGhBase):
    """What raised the answer cache's hit rate (2026-10-31): one key per question however it is spelled,
    write stamps no wider than what the write changes, a 90 s window, the daemon's own copy of a PR or
    run as the change signal, and a longer life for what cannot change."""

    def setUp(self) -> None:
        super().setUp()
        os.environ.pop("GLAEDA_GH_CACHE_TTL", None)  # the shipped default, not the 60 those tests pin

    def age(self, seconds: float) -> None:
        for path in gg.gh_cache_dir().glob("*.json"):
            if path.name != "writes.json":
                doc = json.loads(path.read_text())
                doc["at"] -= seconds
                path.write_text(json.dumps(doc))

    def daemon_entry(self, key: str, data: dict, jobs: list | None = None, fetched_age: float = 0.0) -> None:
        entry = {"key": key, "fetchedAt": time.time() - fetched_age, "dataAt": time.time() - fetched_age,
                 "data": data, "error": None}
        if jobs is not None:
            entry["jobs"] = jobs
        gg.write_json(gg.cache_dir() / gg.file_name(key), entry)

    def test_one_question_is_one_answer_however_it_is_spelled(self) -> None:
        spellings = (
            [["pr", "view", "7", "-R", "O/R", "--json", "state,headRefOid"],
             ["pr", "view", "7", "--repo=o/r", "--json=headRefOid,state"],
             ["pr", "view", "--json", "headRefOid,state,state", "7", "-Ro/r"],
             ["pr", "view", "https://github.com/o/r/pull/7", "--json", "state,headRefOid"]],
            [["api", "repos/o/r/actions/runs/5/jobs?per_page=100&filter=latest", "--jq", ".jobs[].name"],
             ["api", "/repos/o/r/actions/runs/5/jobs?filter=latest&per_page=100", "-q", ".jobs[].name"]],
            [["run", "view", "12", "-R", "o/r", "--log-failed"], ["run", "view", "--log-failed", "12", "--repo", "o/r"]],
        )
        for group in spellings:
            with self.subTest(group=group[0]):
                before = self.runs()
                outs = {self.run_gh(args)[1] for args in group}
                self.assertEqual((self.runs(), outs), (before + 1, {f"answer {before + 1}\n"}))

    def test_different_questions_stay_different(self) -> None:
        for args in (["pr", "view", "7", "-R", "o/r", "--json", "state"],
                     ["pr", "view", "7", "-R", "o/r", "--json", "state,title"],
                     ["pr", "view", "8", "-R", "o/r", "--json", "state"],
                     ["pr", "view", "7", "-R", "o/other", "--json", "state"],
                     ["api", "repos/o/r/actions/runs/5/jobs?page=1"],
                     ["api", "repos/o/r/actions/runs/5/jobs?page=2"],
                     ["api", "repos/o/r/issues?labels=a&labels=b"],
                     ["api", "repos/o/r/issues?labels=b&labels=a"],
                     ["api", "https://api.github.com/repos/o/r/pulls/7"],
                     ["api", "repos/o/r/pulls/7"],
                     ["issue", "view", "https://github.com/o/r/pull/7", "--json", "title"],
                     ["pr", "view", "7", "-R", "o/r", "--json", "state", "--jq", ".state"],
                     # the last of a repeated field wins, whichever spelling carried it
                     ["api", "-X", "GET", "repos/o/r/issues", "-f", "state=open", "--raw-field", "state=closed"],
                     ["api", "-X", "GET", "repos/o/r/issues", "--raw-field", "state=closed", "-f", "state=open"],
                     ["api", "-X", "GET", "repos/o/r/issues", "-F", "per_page=5", "-f", "per_page=6"]):
            self.run_gh(args)
        self.assertEqual(self.runs(), 15)
        self.run_gh(["api", "-X", "GET", "repos/o/r/issues", "--raw-field", "state=open", "--raw-field", "state=closed"])
        self.assertEqual(self.runs(), 15, "-f and --raw-field are one flag")

    def test_a_line_with_a_flag_it_does_not_know_is_kept_as_typed(self) -> None:
        """`--unknown 7` could be a flag with a value: moving it could make 7 the PR number."""
        args = ["pr", "view", "--unknown", "7", "-R", "o/r"]
        self.assertIsNone(gg.gh_cache_canonical(args))
        self.assertIsNone(gg.gh_cache_canonical(["api", "x", "--", "-y"]))
        self.assertIsNone(gg.gh_cache_canonical(["pr", "view", "7", "-abc"]))
        self.run_gh(args)
        self.run_gh(args)
        self.run_gh(["pr", "view", "-R", "o/r", "--unknown", "7"])
        self.assertEqual(self.runs(), 2)

    def test_a_write_stamps_what_it_changes_and_no_more(self) -> None:
        pr7 = ["pr", "view", "7", "-R", "o/r", "--json", "body"]
        pr8 = ["pr", "view", "8", "-R", "o/r", "--json", "body"]
        run5 = ["run", "view", "5", "-R", "o/r", "--json", "status"]
        listed = ["pr", "list", "-R", "o/r", "--json", "number"]
        reads = (pr7, pr8, run5, listed)

        def fresh() -> dict[str, bool]:
            before = self.runs()
            fetched = {}
            for name, args in zip(("pr7", "pr8", "run5", "list"), reads):
                ran = self.runs()
                self.run_gh(args)
                fetched[name] = self.runs() > ran
            return fetched
        fresh()
        self.run_gh(["pr", "comment", "7", "-R", "o/r", "--body", "hi"])
        self.assertEqual(fresh(), {"pr7": True, "pr8": False, "run5": False, "list": True})
        self.assertEqual(gg.gh_cache_post_scopes({"kind": "pr", "repo": "o/r"}), ["runlists", "other"],
                         "a queued PR stamps what `pr create` does")
        self.run_gh(["pr", "merge", "8", "-R", "o/r", "--squash", "--match-head-commit", "a" * 40])
        self.assertEqual(fresh(), {"pr7": True, "pr8": True, "run5": False, "list": True},
                         "a merge moves the base: every PR's mergeability, not the runs")
        done = {"id": 5, "status": "completed", "conclusion": "failure", "run_attempt": 1}
        gg.write_json(gg.cache_dir() / gg.file_name("run:o/r/5"), {"key": "run:o/r/5", "data": done, "fetchedAt": 1})
        before = time.time()
        self.run_gh(["run", "rerun", "5", "-R", "o/r", "--failed"])
        self.assertGreaterEqual(gg.read_cache("run:o/r/5").get("rerunAt") or 0, before,
                                "the watched run's cached finish is marked as a superseded attempt's")
        self.assertEqual(fresh(), {"pr7": True, "pr8": True, "run5": True, "list": True},
                         "a rerun changes the run and the checks of the PRs it reports to")
        self.run_gh(["api", "-X", "POST", "repos/o/r/issues/8/comments", "-f", "body=x"])
        self.assertEqual(fresh(), {"pr7": False, "pr8": True, "run5": False, "list": True})
        self.run_gh(["label", "create", "x", "-R", "o/r"])
        self.assertEqual(fresh(), {"pr7": True, "pr8": True, "run5": True, "list": True},
                         "a write this does not know is as wide as it was")
        self.run_gh(["pr", "comment", "7", "--weird-flag", "8", "-R", "o/r", "--body", "x"])
        self.assertEqual(fresh(), {"pr7": True, "pr8": True, "run5": True, "list": True},
                         "an unknown flag could make any word the PR: the whole repository")
        self.run_gh(["api", "graphql", "-f", "query=mutation{x}"])
        self.assertEqual(fresh(), {"pr7": True, "pr8": True, "run5": True, "list": True},
                         "a mutation names no repository: every answer")

    def test_a_list_of_runs_is_stamped_only_by_writes_that_make_runs(self) -> None:
        lists = (["run", "list", "-R", "o/r", "--workflow", "ci.yml", "--json", "databaseId"],
                 ["api", "repos/o/r/actions/workflows/ci.yml/runs?per_page=5"],
                 ["api", "repos/o/r/actions/runs?branch=main"])

        def fetched() -> bool:
            before = self.runs()
            for args in lists:
                self.run_gh(args)
            return {0: False, len(lists): True}[self.runs() - before]
        fetched()
        for write, starts in ((["pr", "comment", "7", "-R", "o/r", "--body", "x"], True),
                              (["issue", "create", "-R", "o/r", "--title", "t", "--body", "b"], True),
                              (["api", "-X", "POST", "repos/o/r/issues/7/comments", "-f", "body=x"], True),
                              (["api", "-X", "POST", "repos/o/r/issues", "-f", "title=t"], True),
                              (["api", "-X", "POST", "repos/o/r/pulls/7/reviews", "-f", "event=COMMENT"], True),
                              (["api", "-X", "PUT", "repos/o/r/branches/main/protection"], True),
                              (["pr", "edit", "7", "-R", "o/r", "--add-label", "full-ci"], True),
                              (["workflow", "run", "ci.yml", "-R", "o/r"], True),
                              (["api", "-X", "POST", "repos/o/r/actions/workflows/ci.yml/dispatches", "-f", "ref=main"], True),
                              (["run", "rerun", "5", "-R", "o/r"], True),
                              (["pr", "merge", "7", "-R", "o/r", "--squash"], True),
                              (["pr", "create", "-R", "o/r", "--title", "t", "--body", "b"], True)):
            with self.subTest(write=write):
                self.run_gh(write)
                self.assertEqual(fetched(), starts)

    def test_a_write_still_runs_every_time_and_never_reads_the_cache(self) -> None:
        merge = ["pr", "merge", "7", "-R", "o/r", "--squash", "--match-head-commit", "b" * 40]
        self.run_gh(["pr", "view", "7", "-R", "o/r", "--json", "headRefOid"])
        for _ in range(2):
            code, _out, _err, passed = self.run_gh(merge)
            self.assertEqual((code, passed), (0, None))
        self.assertEqual(self.runs(), 3)
        self.assertEqual(self.passed.read_text().count("--match-head-commit"), 2)

    def test_git_credential_is_not_a_write(self) -> None:
        words, method, _seen, _values = gg.broker_words(["auth", "git-credential", "get"])
        self.assertFalse(gg.gh_cache_is_write(["auth", "git-credential", "get"], words, method))

    def test_the_window_is_ninety_seconds(self) -> None:
        args = ["pr", "view", "7", "-R", "o/r", "--json", "body"]
        self.run_gh(args)
        self.age(80)
        self.assertEqual(self.run_gh(args)[1], "answer 1\n")
        self.age(11)
        self.assertEqual(self.run_gh(args)[1], "answer 2\n")

    def test_a_change_the_daemon_sees_drops_the_answer_from_any_host(self) -> None:
        key = "pr:o/r#7"
        self.daemon_entry(key, {"number": 7, "headRefOid": "a" * 40, "state": "OPEN"})
        args = ["pr", "view", "7", "-R", "o/r", "--json", "body"]
        self.run_gh(args)
        self.assertEqual(self.run_gh(args)[1], "answer 1\n", "the daemon's copy unchanged: kept")
        self.daemon_entry(key, {"number": 7, "headRefOid": "b" * 40, "state": "OPEN"})
        self.assertEqual(self.run_gh(args)[1], "answer 2\n", "a push another host made, seen by the daemon")
        (gg.cache_dir() / gg.file_name(key)).unlink()
        self.assertEqual(self.run_gh(args)[1], "answer 2\n", "no daemon entry: the TTL decides")

    def test_a_completed_run_the_daemon_watches_is_kept_until_it_changes(self) -> None:
        key = "run:o/r/5"
        done = {"id": 5, "status": "completed", "conclusion": "failure", "run_attempt": 1}
        self.daemon_entry(key, done, jobs=[{"id": 1, "status": "completed"}])
        view = ["run", "view", "5", "-R", "o/r", "--log-failed"]
        jobs = ["api", "repos/o/r/actions/runs/5/jobs"]
        self.run_gh(view)
        self.run_gh(jobs)
        self.age(300)
        self.assertEqual((self.run_gh(view)[1], self.run_gh(jobs)[1]), ("answer 1\n", "answer 2\n"))
        self.daemon_entry(key, {**done, "status": "queued", "conclusion": None, "run_attempt": 2})
        self.assertEqual((self.run_gh(view)[1], self.run_gh(jobs)[1]), ("answer 3\n", "answer 4\n"),
                         "a rerun the controller's webhook reported")

    def test_a_run_the_daemon_stopped_reading_gets_the_ordinary_window(self) -> None:
        self.daemon_entry("run:o/r/5", {"id": 5, "status": "completed", "conclusion": "success"},
                          fetched_age=gg.TERMINAL_REFRESH + 3 * gg.CYCLE)
        view = ["run", "view", "5", "-R", "o/r", "--json", "jobs"]
        self.run_gh(view)
        self.age(120)
        self.assertEqual(self.run_gh(view)[1], "answer 2\n")

    def test_a_live_run_gets_the_ordinary_window(self) -> None:
        self.daemon_entry("run:o/r/5", {"id": 5, "status": "in_progress", "conclusion": None})
        view = ["run", "view", "5", "-R", "o/r", "--json", "jobs"]
        self.run_gh(view)
        self.age(120)
        self.assertEqual(self.run_gh(view)[1], "answer 2\n")

    def test_a_completed_job_is_kept_and_a_running_one_is_not(self) -> None:
        gh = self.home / "bin" / "gh"
        reply = self.home / "reply.json"
        gh.write_text("#!/bin/sh\n"
                      f"n=$(($(cat {self.count} 2>/dev/null || echo 0) + 1)); echo $n > {self.count}\n"
                      f"cat {reply}\n")
        cases = ((["api", "repos/o/r/actions/jobs/9"], {"id": 9, "status": "completed"}, True),
                 (["api", "repos/o/r/actions/jobs/10"], {"id": 10, "status": "in_progress"}, False),
                 (["api", "repos/o/r/actions/runs/5/attempts/1/jobs"],
                  {"total_count": 2, "jobs": [{"status": "completed"}, {"status": "completed"}]}, True),
                 (["api", "repos/o/r/actions/runs/5/attempts/2/jobs"],
                  {"total_count": 2, "jobs": [{"status": "completed"}, {"status": "queued"}]}, False),
                 (["api", "repos/o/r/actions/runs/5/attempts/3/jobs"],
                  {"total_count": 40, "jobs": [{"status": "completed"}]}, False),
                 (["api", "repos/o/r/actions/jobs/11", "--jq", ".status"], {"id": 11, "status": "completed"}, False))
        for args, doc, kept in cases:
            with self.subTest(args=args):
                reply.write_text(json.dumps(doc))
                self.run_gh(args)
                self.age(300)
                before = self.runs()
                self.run_gh(args)
                self.assertEqual(self.runs() == before, kept)

    def test_identical_calls_arriving_together_run_gh_once(self) -> None:
        """Three processes, three spellings, one gh run: the canonical key is what they lock on."""
        gh = self.home / "bin" / "gh"
        gh.write_text("#!/bin/sh\n"
                      f"echo x >> {self.count}.runs\nsleep 1\necho together\n")
        env = {**os.environ, "GLAEDA_GH_DIR": str(gg.BASE), "CMUX_GH_MIN_REPEAT_SECONDS": "0"}
        env.pop(gg.BROKER_MARK, None)
        env.pop(gg.BROKER_DEPTH, None)
        script = os.fspath(ROOT / "scripts" / "glaeda-gh")
        lines = (["pr", "view", "7", "-R", "o/r", "--json", "state,body"],
                 ["pr", "view", "7", "--repo=O/R", "--json=body,state"],
                 ["pr", "view", "https://github.com/o/r/pull/7", "--json", "body,state"])
        procs = [subprocess.Popen([sys.executable, script, "gh", *args], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, stdin=subprocess.DEVNULL) for args in lines]
        outs = [proc.communicate(timeout=60)[0] for proc in procs]
        self.assertEqual(outs, [b"together\n"] * 3)
        self.assertEqual(len((self.home / "count.runs").read_text().splitlines()), 1)


class BrokerSweepTest(BrokerBase):
    """A per-job occupancy sweep is refused past GH_SWEEP_MAX distinct targets; polling is not."""

    def test_a_sweep_is_refused_with_a_message_naming_the_replacement(self) -> None:
        with mock.patch.object(gg, "GH_SWEEP_MAX", 3):
            for n in range(3):
                _code, _out, _err, passed = self.run_gh(["api", f"repos/o/r/actions/runs/{n}/jobs"])
                self.assertIsNotNone(passed)
            code, _out, err, passed = self.run_gh(["api", "repos/o/r/actions/jobs/99", "--jq", ".runner_name"])
        self.assertEqual((code, passed), (gg.GH_SWEEP_EXIT, None))
        self.assertIn("glaeda-gh runners o", err)
        self.assertIn("GLAEDA_GH_SWEEP_OK=1", err)

    def test_polling_one_run_is_never_a_sweep(self) -> None:
        with mock.patch.object(gg, "GH_SWEEP_MAX", 2):
            for _ in range(10):
                _code, _out, _err, passed = self.run_gh(["api", "repos/o/r/actions/runs/5/jobs"])
                self.assertIsNotNone(passed)

    def test_the_window_forgets_and_the_override_passes(self) -> None:
        with mock.patch.object(gg, "GH_SWEEP_MAX", 1):
            self.run_gh(["api", "repos/o/r/actions/jobs/1"])
            with mock.patch.dict(os.environ, {"GLAEDA_GH_SWEEP_OK": "1"}):
                self.assertIsNotNone(self.run_gh(["api", "repos/o/r/actions/jobs/2"])[3])
            self.assertIsNone(self.run_gh(["api", "repos/o/r/actions/jobs/3"])[3])
            later = time.time() + gg.GH_SWEEP_WINDOW + 1
            self.assertIsNone(gg.gh_sweep_refusal(["api", "repos/o/r/actions/jobs/4"], "GET", later))

    def test_what_is_a_sweep_target(self) -> None:
        for path, want in (("repos/O/R/actions/runs/5/jobs?per_page=100", "o/r run 5"),
                           ("/repos/o/r/actions/runs/5/attempts/2/jobs", "o/r run 5"),
                           ("https://api.github.com/repos/o/r/actions/jobs/7", "o/r job 7"),
                           ("repos/o/r/actions/jobs/7/logs", None), ("repos/o/r/actions/runs/5", None),
                           ("orgs/o/actions/runners", None)):
            with self.subTest(path=path):
                self.assertEqual(gg.gh_sweep_target(["api", path], "GET"), want)
        self.assertIsNone(gg.gh_sweep_target(["api", "repos/o/r/actions/jobs/7"], "POST"))


class RunnersTest(BrokerBase):
    def test_runners_counts_online_busy_and_idle_per_label(self) -> None:
        lines = "\n".join(json.dumps(r) for r in (
            {"name": "a", "status": "online", "busy": True, "labels": ["self-hosted", "mini"]},
            {"name": "b", "status": "online", "busy": False, "labels": ["self-hosted", "mini"]},
            {"name": "c", "status": "offline", "busy": False, "labels": ["self-hosted"]}))
        done = subprocess.CompletedProcess([], 0, lines + "\n", "")
        out = io.StringIO()
        with mock.patch.object(gg, "run_bounded", return_value=done) as ran, contextlib.redirect_stdout(out):
            self.assertEqual(gg.cmd_runners("o", None, True), 0)
        argv = ran.call_args[0][0]
        self.assertIn("orgs/o/actions/runners?per_page=100", argv)
        self.assertEqual(argv[2:4], ["gh", "api"], "read through this broker, so the cache shares it")
        bad = subprocess.CompletedProcess([], 0, "not json\n", "")
        with mock.patch.object(gg, "run_bounded", return_value=bad), self.assertRaises(gg.WatchError):
            gg.cmd_runners("o", None, True)
        with mock.patch.object(gg, "run_bounded", side_effect=OSError("no")), self.assertRaises(gg.WatchError):
            gg.cmd_runners("o", None, True)
        doc = json.loads(out.getvalue())
        self.assertEqual((doc["runners"], doc["online"], doc["busy"], doc["busyRunners"]), (3, 2, 1, ["a"]))
        self.assertEqual(doc["labels"]["mini"], {"online": 2, "busy": 1})
        with mock.patch.object(gg, "run_bounded", return_value=done) as ran, contextlib.redirect_stdout(io.StringIO()):
            gg.cmd_runners("o/r", "mini", False)
        self.assertIn("repos/o/r/actions/runners?per_page=100", ran.call_args[0][0])
        with self.assertRaises(gg.WatchError):
            gg.cmd_runners("not a scope", None, False)


class BrokerGraphqlCacheTest(BrokerBase):
    """A `gh api graphql` query is kept like a GET, keyed on the query and its variables."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["GLAEDA_GH_CACHE_TTL"] = "60"
        self.count = self.home / "count"
        gh = self.home / "bin" / "gh"
        gh.write_text("#!/bin/sh\n"
                      f"n=$(($(cat {self.count} 2>/dev/null || echo 0) + 1)); echo $n > {self.count}\n"
                      "echo \"answer $n\"\n")
        gh.chmod(0o755)

    def runs(self) -> int:
        return int(self.count.read_text()) if self.count.exists() else 0

    Q = "query=query($id:ID!,$after:String){node(id:$id){... on Workflow{runs(first:50,after:$after){nodes{databaseId}}}}}"

    def test_the_same_page_is_read_once_whatever_the_flag_order(self) -> None:
        first = self.run_gh(["api", "graphql", "-f", self.Q, "-f", "id=W1", "-f", "after=C1"])
        second = self.run_gh(["api", "graphql", "-f", "after=C1", "-f", self.Q, "-f", "id=W1"])
        third = self.run_gh(["api", "graphql", "--raw-field=after=C1", "-f", self.Q, "-fid=W1"])
        self.assertEqual((first[1], second[1], third[1], self.runs()), ("answer 1\n",) * 3 + (1,))
        self.assertEqual((first[3], second[3], third[3]), (None, None, None))

    def test_another_page_or_variable_kind_is_another_question(self) -> None:
        self.run_gh(["api", "graphql", "-f", self.Q, "-f", "id=W1", "-f", "after=C1"])
        self.run_gh(["api", "graphql", "-f", self.Q, "-f", "id=W1", "-f", "after=C2"])
        self.run_gh(["api", "graphql", "-f", self.Q, "-f", "id=W2", "-f", "after=C1"])
        self.run_gh(["api", "graphql", "-f", self.Q, "-F", "id=W1", "-f", "after=C1"])
        self.run_gh(["api", "graphql", "-f", self.Q, "-f", "id=W1", "-f", "after=C1", "--jq", ".data"])
        self.assertEqual(self.runs(), 5)

    def test_a_mutation_is_a_write_and_clears_every_answer(self) -> None:
        read = ["api", "graphql", "-f", self.Q, "-f", "id=W1"]
        self.run_gh(read)
        mutation = ["api", "graphql", "-f", "query=mutation{addComment(input:{}){clientMutationId}}"]
        self.run_gh(mutation)
        self.run_gh(mutation)
        _code, out, _err, _ = self.run_gh(read)
        self.assertEqual((out, self.runs()), ("answer 4\n", 4))

    def test_a_repeated_field_keeps_its_order(self) -> None:
        self.run_gh(["api", "graphql", "-f", "query=query{a}", "-f", "query=query{b}"])
        self.run_gh(["api", "graphql", "-f", "query=query{b}", "-f", "query=query{a}"])
        self.run_gh(["api", "graphql", "-f", self.Q, "-f", "ids[]=1", "-f", "ids[]=2"])
        self.run_gh(["api", "graphql", "-f", self.Q, "-f", "ids[]=2", "-f", "ids[]=1"])
        self.assertEqual(self.runs(), 4)

    def test_a_branch_placeholder_in_a_field_is_never_kept(self) -> None:
        args = ["api", "graphql", "-f", self.Q, "-F", "ref={branch}"]
        self.run_gh(args)
        _code, _out, _err, passed = self.run_gh(args)
        self.assertEqual(passed, args, "exec'd every time: the checked-out branch is not on the line")

    def test_a_query_from_a_file_is_never_kept(self) -> None:
        for args in (["api", "graphql", "-F", "query=@q.graphql"], ["api", "graphql", "--input", "q.json"]):
            with self.subTest(args=args):
                before = self.runs()
                self.run_gh(args)
                self.run_gh(args)
                self.assertEqual(self.runs(), before + 2)


class BrokerAnswerCacheReviewTest(BrokerBase):
    """Round-two findings: output environment, process group, and ^C on a write."""

    def test_output_environment_is_part_of_the_key(self) -> None:
        os.environ["GLAEDA_GH_CACHE_TTL"] = "60"
        args = ["pr", "view", "7", "-R", "o/r"]
        words, method, seen, values = gg.broker_words(args)
        with mock.patch.object(sys.stdout, "isatty", return_value=False):
            plain = gg.gh_cache_key(args, words, values, seen, method, "/gh")
            self.assertIsNotNone(plain)
            for name, value in (("GH_FORCE_TTY", "1"), ("CLICOLOR_FORCE", "1"), ("NO_COLOR", "1"),
                                ("GLAMOUR_STYLE", "dark"), ("COLUMNS", "80")):
                with self.subTest(name=name), mock.patch.dict(os.environ, {name: value}):
                    self.assertNotEqual(gg.gh_cache_key(args, words, values, seen, method, "/gh"), plain)

    def test_a_fill_child_stays_in_the_callers_process_group(self) -> None:
        probe = self.home / "bin" / "pgid"
        probe.write_text("#!/bin/sh\nps -o pgid= -p $$\n")
        probe.chmod(0o755)
        rc, out, _err = gg.gh_cache_child(str(probe), [], True, 10)
        self.assertEqual((rc, int(out.decode().strip())), (0, os.getpgrp()))

    def test_a_write_child_gets_default_sigint(self) -> None:
        probe = self.home / "bin" / "sigint"
        out = self.home / "sigint.txt"
        # SigIgn in /proc is Linux only; ask the shell instead: `trap` prints an ignored INT
        probe.write_text(f"#!/bin/sh\ntrap > {out}\n")
        probe.chmod(0o755)
        rc, _o, _e = gg.gh_cache_child(str(probe), [], False, None)
        self.assertEqual(rc, 0)
        self.assertNotIn("INT", out.read_text())
        self.assertIsNot(signal.getsignal(signal.SIGINT), signal.SIG_IGN)


class StaleRunRereadTest(Base):
    """manaflow-ai/cmux run 40557947333, 2026-10-27: GitHub finished the run (failure) at 18:21Z, but the controller
    lost the completion delivery and kept it in_progress, so the daemon (which reads a controller-tracked run over
    REST only every REST_SAFETY) kept a blocked `wait run` on that record. A waited run that stays queued or in
    progress, unchanged and unread for STALE_RUN_REREAD, is read once more; a fresh or unwaited one is not."""

    request, woken = WaitSubscriptionTest.request, WaitSubscriptionTest.woken

    def setUp(self) -> None:
        super().setUp()
        gg.BASE.mkdir(parents=True)
        lock = open(gg.BASE / "daemon.lock", "a+")
        fcntl.flock(lock, fcntl.LOCK_EX)  # stands in for a running daemon that serves waits
        self.addCleanup(lock.close)
        gg.write_json(gg.BASE / "daemon.json", {"at": time.time(), "heartbeat": 30, "waits": gg.WAITS_CONTRACT})
        self.ctl = FakeController()
        feed_every, gg.CONTROLLER_FEED_EVERY = gg.CONTROLLER_FEED_EVERY, 0
        self.addCleanup(setattr, gg, "CONTROLLER_FEED_EVERY", feed_every)
        self.daemon = gg.Daemon(transport=self.gh, token=lambda: TOKEN, controller=self.ctl)

    def tick(self, key: str, at: float, waited: bool = True) -> None:
        """One tick at `at`, with the wait's interest as fresh as serve_waits keeps it on a real clock."""
        path = gg.watch_dir() / gg.file_name(key)
        doc = gg.read_json(path)
        if waited:
            doc["waitedAt"] = at
        else:
            doc.pop("waitedAt", None)
        gg.write_json(path, doc)
        os.utime(path, (at, at))
        self.daemon.tick(at)

    def in_progress(self, run_id: int, updated: float) -> None:
        self.ctl.runs[run_id] = ctl_run(run_id, "in_progress", "", iso(updated))
        self.gh.runs[run_id] = {"id": run_id, "status": "in_progress", "conclusion": None, "run_attempt": 1,
                                "updated_at": iso(updated)}

    def test_a_stale_in_progress_run_is_reread_and_its_waiter_released(self) -> None:
        now = time.time()
        key = "run:o/r/60"
        self.in_progress(60, now - 600)
        rid, reader = self.request("run", "o/r/60", timeout=3600)
        self.assertTrue(gg.read_json(gg.watch_dir() / gg.file_name(key)).get("waitedAt"), "a wait marks its watch")
        self.tick(key, NOW)
        self.assertEqual(self.gh.rest_calls(), [], "the controller's baseline costs no REST")
        # GitHub finishes it; the controller never hears (a lost delivery)
        self.gh.runs[60] = {"id": 60, "status": "completed", "conclusion": "failure", "run_attempt": 1,
                            "updated_at": iso(now - 60)}
        self.gh.jobs[60] = [job("build", end=now - 60)]
        self.tick(key, NOW + gg.STALE_RUN_REREAD - gg.CYCLE)
        self.assertEqual(self.gh.rest_calls(), [])
        self.assertEqual(self.woken(reader), "")
        self.tick(key, NOW + gg.STALE_RUN_REREAD + 1)
        self.assertEqual(len(self.gh.run_calls()), 1, "one read of the run, well before REST_SAFETY")
        entry = gg.read_cache(key)
        self.assertEqual((entry["source"], entry["data"]["conclusion"]), ("rest", "failure"))
        self.assertEqual(self.woken(reader), "1\n", "the waiter is released on the failure")
        self.assertIn("failure", (gg.waits_dir() / f"{rid}.out").read_text())

    def test_a_stale_in_progress_run_is_reread_and_released_on_success(self) -> None:
        now = time.time()
        key = "run:o/r/63"
        self.in_progress(63, now - 600)
        _, reader = self.request("run", "o/r/63", timeout=3600)
        self.tick(key, NOW)
        self.gh.runs[63] = {"id": 63, "status": "completed", "conclusion": "success", "run_attempt": 1,
                            "updated_at": iso(now - 60)}
        self.tick(key, NOW + gg.STALE_RUN_REREAD + 1)
        self.assertEqual(self.woken(reader), "0\n")

    def test_a_fresh_or_unwaited_run_is_not_reread(self) -> None:
        now = time.time()
        key = "run:o/r/61"
        self.in_progress(61, now - 600)
        gg.register(key)  # a `status` reader, no wait
        self.tick(key, NOW, waited=False)
        # fresh: a wait on it, but the controller's record is younger than STALE_RUN_REREAD
        for t in range(0, gg.STALE_RUN_REREAD, gg.TICK):
            self.tick(key, NOW + t)
        self.assertEqual(self.gh.rest_calls(), [], "a fresh entry costs no REST")
        # old enough, but nobody waits on it: the REST_SAFETY net is enough for a reader that only looks
        self.tick(key, NOW + gg.STALE_RUN_REREAD + 1, waited=False)
        self.assertEqual(self.gh.rest_calls(), [])
        # a controller update keeps it fresh: no read while it keeps changing
        self.ctl.runs[61] = ctl_run(61, "in_progress", "", iso(now - 30))
        self.tick(key, NOW + gg.STALE_RUN_REREAD + 2)
        self.tick(key, NOW + 2 * gg.STALE_RUN_REREAD)
        self.assertEqual(self.gh.rest_calls(), [])

    def test_a_still_running_run_is_reread_once_per_period(self) -> None:
        now = time.time()
        key = "run:o/r/62"
        self.in_progress(62, now - 600)
        self.request("run", "o/r/62", timeout=3600)
        self.tick(key, NOW)
        start = NOW + gg.STALE_RUN_REREAD + 1
        self.tick(key, start)
        self.assertEqual(len(self.gh.rest_calls()), 1)
        for t in range(gg.TICK, gg.STALE_RUN_REREAD, gg.TICK):
            self.tick(key, start + t)
        self.assertEqual(len(self.gh.rest_calls()), 1, "a read resets the clock: one per STALE_RUN_REREAD")
        self.tick(key, start + gg.STALE_RUN_REREAD)
        self.assertEqual(len(self.gh.rest_calls()), 2)
        self.assertIn("If-None-Match", self.gh.rest_calls()[-1][2], "conditional: an unchanged run answers a free 304")

    def test_a_run_wait_marks_its_watch_before_the_controller_long_poll(self) -> None:
        """subscribe_wait long-polls the controller (up to CLIENT_RUN_LONG_POLL) before it leaves its request: the
        daemon must already see the wait, and keep seeing it through that poll, or a lost finish waits it out."""
        key = "run:o/r/67"
        seen: list = []

        def long_poll(k: str, timeout: float) -> None:
            seen.append(gg.read_json(gg.watch_dir() / gg.file_name(k)))

        with mock.patch.object(gg, "controller_wait_run", side_effect=long_poll), \
                mock.patch.object(gg, "block", side_effect=SystemExit(9)), mock.patch.object(gg, "say_waiting"):
            with self.assertRaises(SystemExit):
                gg.subscribe_wait("run", "o/r/67", "green", 600, False, False, None, gg.RESCUE_GRACE)
        self.assertTrue(seen and isinstance(seen[0].get("waitedAt"), float), seen)
        e = {"key": key, "waitedAt": seen[0]["waitedAt"]}
        self.daemon.now = seen[0]["waitedAt"] + gg.CLIENT_RUN_LONG_POLL  # the long-poll's whole length
        self.assertTrue(self.daemon.waited(e))

    def test_a_long_poll_that_ends_on_the_same_in_progress_record_keeps_its_age(self) -> None:
        key = "run:o/r/68"
        old = time.time() - 400
        updated = iso(time.time() - 600)
        gg.write_json(gg.cache_dir() / gg.file_name(key), {
            "key": key, "fetchedAt": old, "dataAt": old, "restAt": old, "source": "controller", "error": None,
            "data": gg.controller_run(ctl_run(68, "in_progress", "", updated))})
        controller = mock.Mock()
        controller.run.return_value = ctl_run(68, "in_progress", "", updated)
        with mock.patch.object(gg.Controller, "from_env", return_value=controller):
            entry = gg.controller_wait_run(key, 5)
        self.assertEqual((entry["dataAt"], entry["restAt"]), (old, old), "an unchanged record is not new data")
        self.assertGreater(entry["fetchedAt"], old)
        controller.run.return_value = ctl_run(68, "in_progress", "", iso(time.time()))
        with mock.patch.object(gg.Controller, "from_env", return_value=controller):
            entry = gg.controller_wait_run(key, 5)
        self.assertGreater(entry["dataAt"], old, "a changed record is")

    def test_a_downstream_wait_marks_the_upstream_watch(self) -> None:
        out = io.StringIO()
        with mock.patch.object(gg, "daemon_running", lambda grace=0.0: True), \
                mock.patch.object(gg, "daemon_failing", lambda: None), contextlib.redirect_stdout(out):
            gg.cmd_read(json.dumps({"keys": [{"key": "run:o/r/64", "jobs": False, "wait": True},
                                             {"key": "run:o/r/65", "jobs": False}]}))
        self.assertTrue(gg.read_json(gg.watch_dir() / gg.file_name("run:o/r/64")).get("waitedAt"))
        self.assertIsNone(gg.read_json(gg.watch_dir() / gg.file_name("run:o/r/65")).get("waitedAt"))
        with self.assertRaises(gg.WatchError):
            gg.read_request(json.dumps({"keys": [{"key": "run:o/r/64", "wait": "yes"}]}))
        asked: list[dict] = []
        daemon = gg.Daemon(transport=self.gh, token=lambda: self.fail("read a token"), controller=None,
                           upstream=lambda req: asked.append(req) or {"entries": {}})
        self.request("run", "o/r/66", timeout=3600)
        daemon.tick(now=time.time())
        keys = {k["key"]: k for k in asked[0]["keys"]}
        self.assertEqual(keys["run:o/r/66"].get("wait"), True)
        self.assertNotIn("wait", keys["run:o/r/65"])


class QuietWritesTest(Base):
    """cosmic-gray, 2026-11-05: fseventsd at a 27 GB footprint. In 40 s, 14 watch files were replaced with the same
    bytes and waits/.served was touched every tick. A write that changes nothing a reader decides is skipped."""

    def watch(self, key: str) -> Path:
        return gg.watch_dir() / gg.file_name(key)

    def test_a_same_content_register_refreshes_the_mtime_without_replacing_the_file(self) -> None:
        gg.register("pr:o/r#1")
        path = self.watch("pr:o/r#1")
        inode = path.stat().st_ino
        self.age_watch("pr:o/r#1", 60)
        gg.register("pr:o/r#1")
        self.assertEqual(path.stat().st_ino, inode, "the same registration replaced the file")
        self.assertLess(time.time() - path.stat().st_mtime, 5, "the registration did not count as interest")
        gg.register("pr:o/r#1", jobs=True)  # a real change is still written
        self.assertTrue(gg.read_json(path)["jobs"])
        self.assertNotEqual(path.stat().st_ino, inode)

    def test_a_fresh_touch_is_skipped_unless_the_cache_was_read_after_it(self) -> None:
        gg.register("pr:o/r#1")
        path = self.watch("pr:o/r#1")
        at = time.time() - 2
        os.utime(path, (at, at))
        gg.touch_interest("pr:o/r#1")
        self.assertEqual(path.stat().st_mtime, at, "a touch 2 s after the last one was written")
        gg.register("pr:o/r#1")
        self.assertEqual(path.stat().st_mtime, at, "a same-content registration 2 s after the last touch was written")
        # A fetch since the last touch: a reader now is newer than it (Daemon.due re-reads on that), so it is touched.
        gg.write_json(gg.cache_dir() / gg.file_name("pr:o/r#1"), {"key": "pr:o/r#1", "fetchedAt": at + 1})
        gg.touch_interest("pr:o/r#1")
        self.assertGreater(path.stat().st_mtime, at + 1)
        # Older than INTEREST_QUIET: touched.
        os.utime(path, (time.time() - gg.INTEREST_QUIET - 1,) * 2)
        gg.touch_interest("pr:o/r#1")
        self.assertLess(time.time() - path.stat().st_mtime, 2)

    def test_served_is_touched_at_most_once_per_quiet_window(self) -> None:
        gg.waits_dir().mkdir(parents=True, exist_ok=True)
        served = gg.waits_dir() / ".served"
        gg.mark_waits_served()
        self.assertTrue(served.exists())
        at = time.time() - 3
        os.utime(served, (at, at))
        gg.mark_waits_served()
        self.assertEqual(served.stat().st_mtime, at)
        old = time.time() - gg.SERVED_QUIET - 1
        os.utime(served, (old, old))
        gg.mark_waits_served()
        self.assertGreater(served.stat().st_mtime, old + 1)
        # Well under a blocker's look: a mark WAITS_CHECK old is always older than a live daemon's .served.
        self.assertLess(gg.SERVED_QUIET + gg.TICK, gg.WAITS_CHECK / 2)


if __name__ == "__main__":
    unittest.main()
