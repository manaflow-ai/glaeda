#!/usr/bin/env python3
"""Measure: how long a ready land (queued pinned, with an override) waits for a worker when every worker is busy, and
how many land refreshes a queue sends per hour while direct reads answer DIRECT_QUOTA (the daemon's GraphQL "stop").

cmux-cp-1, 2026-11-25: cmux#53519 was queued with an override at 06:22:54Z, met 24 of 24 workers busy, and fell into
an hour-long wait on checks a spent GraphQL quota never re-read; the same hour 197 of 440 refreshes came back
DIRECT_QUOTA and were sent again on the next pass. Both numbers come from the test suite's simulated queue (its
LandWorld and FakePool, simulated time, no GitHub), so one checkout's numbers compare with another's: run it against
the base and the change with the same test file.

The same harness counts the land refreshes' own GraphQL queries on the suite's FakeGitHub (MeasureReads): 60 stale PRs of
one repository refreshed the way the checkout's queue sends them, first read and unchanged, and how many keep a rollup
snapshot matching the cache (what a judgment is handed instead of reading the head itself).

Run: python3 scripts/measure-land-ready-starvation.py [GLAEDA_ROOT]   (GLAEDA_ROOT/scripts/glaeda-gh is measured;
     the default is this checkout). Prints one JSON object.
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import time
import unittest
import unittest.mock

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
loader = importlib.machinery.SourceFileLoader("glaeda_land_tests", os.path.join(ROOT, "scripts", "test-glaeda-gh.py"))
spec = importlib.util.spec_from_loader("glaeda_land_tests", loader)
t = importlib.util.module_from_spec(spec)
sys.modules["glaeda_land_tests"] = t
loader.exec_module(t)
gg, LA = t.gg, t.LA
HORIZON = 2 * 3600.0
FREE_EVERY = 60.0  # one busy worker frees a minute


class Measure(t.LandTest):
    out: dict = {}

    def test_ready_land_latency(self) -> None:
        """Seconds from the override row's queueing to a worker taking it: 4 workers, all busy with rows queued
        before it, a worker freeing every FREE_EVERY s, 12 earlier rows due."""
        pool = self.pooled(4)
        for n in range(2, 14):
            self.pr(LA, key=f"pr:o/r#{n}")
            gg.land_request("land", f"o/r#{n}", [], "host/lane")
        self.tick()
        self.pr(LA, checks=[("ci", "IN_PROGRESS", None)])
        gg.land_request("land", "o/r#1", [], "host/lane", sha=LA, override=dict(self.OVERRIDE))
        start, took, freed = self.t, None, self.t
        while self.t - start < HORIZON:
            if self.t - freed >= FREE_EVERY:
                freed = self.t
                live = [job for _, job in pool.handed if not job.finished]
                if live:
                    live[0].finished = True
            self.tick(10.0)
            if "pr:o/r#1" in pool.keys():
                took = self.t - start
                break
        Measure.out["ready_land_to_worker_s"] = took if took is not None else f">{HORIZON:.0f}"
        Measure.out["ready_land_state_meanwhile"] = None if took else self.q.entries["pr:o/r#1"].get("state")

    def test_refresh_reads_per_hour_under_direct_quota(self) -> None:
        """Refreshes sent in an hour by a queue with 20 stale rows while every direct read answers DIRECT_QUOTA."""
        refreshed, _ = self.held_refresh_queue([gg.DIRECT_QUOTA])
        keys = [self.KEY, "pr:o/r#2"]
        for n in range(3, 21):
            key = f"pr:o/r#{n}"
            self.pr(LA, key=key)
            self.w.cache[key]["dataAt"] = self.t - 3600
            self.q.entries[key] = {"key": key, "repo": "o/r", "number": n, "state": "queued"}
            keys.append(key)
        end = self.q.now + 3600
        while self.q.now < end:
            for key in keys:
                self.due(key)  # refresh_due: sends the read (inline) when it is due
            self.q.now += 2.0
        Measure.out["refused_refreshes_per_hour"] = len(refreshed)


REFRESHED = 60  # stale land PRs of one repository refreshed in one pass


class MeasureReads(t.StaleDataTest):
    """The land refreshes' own GitHub reads, on the suite's FakeGitHub (it answers GraphQL and REST and records each
    request): REFRESHED stale PRs refreshed the way this checkout's queue sends them (land_direct_read_many where it
    exists, else one land_direct_read each), and how many of them leave a rollup snapshot at the head and fingerprint
    the cache now holds (what LandQueue.snapshot hands land_judge instead of a live read of the head)."""

    def test_refresh_queries(self) -> None:
        keys = [f"pr:o/r#{n}" for n in range(1, REFRESHED + 1)]
        self.gh.counts = True  # the fingerprint's counts: an unchanged PR is probed, as on GitHub
        for n in range(1, REFRESHED + 1):
            self.gh.prs[("o", "r", n)] = t.pr_node(n, rollup="SUCCESS", checks=[t.check_run("a")])
        gg.BASE.mkdir(parents=True, exist_ok=True)
        real, real_time = gg.Daemon, time.time

        def refresh() -> int:
            before = len(self.gh.gql_calls())
            with unittest.mock.patch.object(gg, "Daemon", side_effect=lambda **kw: real(
                    transport=self.gh, token=lambda: t.TOKEN, controller=None, upstream=None)):
                if hasattr(gg, "land_direct_read_many"):
                    gg.land_direct_read_many(keys)
                else:
                    for key in keys:
                        gg.land_direct_read(key)
            return len(self.gh.gql_calls()) - before
        Measure.out["refresh_graphql_queries_first_read_60_prs"] = refresh()
        with unittest.mock.patch.object(gg.time, "time", lambda: real_time() + gg.LAND_DIRECT_REUSE + 1):
            Measure.out["refresh_graphql_queries_unchanged_60_prs"] = refresh()
        matched = 0
        for key in keys:
            snap, cache = gg.read_rollup(key) or {}, gg.read_cache(key) or {}
            matched += bool(snap) and (snap.get("head"), snap.get("fp")) == \
                ((cache.get("data") or {}).get("headRefOid"), cache.get("fp"))
        Measure.out["refreshed_prs_with_a_matching_snapshot"] = f"{matched}/{len(keys)}"


if __name__ == "__main__":
    suite = unittest.TestSuite([Measure("test_ready_land_latency"), Measure("test_refresh_reads_per_hour_under_direct_quota"),
                                MeasureReads("test_refresh_queries")])
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    for _, tb in result.errors + result.failures:
        print(tb, file=sys.stderr)
    print(json.dumps({"root": ROOT, **Measure.out}))
