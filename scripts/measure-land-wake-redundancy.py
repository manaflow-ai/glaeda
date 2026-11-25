#!/usr/bin/env python3
"""Measure: how many land judgments a PR parked on a wait costs per hour from base churn alone -- pushes to the base
that move nothing the verdict depends on.

GitHub turns a PR's `mergeable` UNKNOWN and back on every push to its base. The land queue's wake fingerprint
(land_fingerprint) held that raw value, so each base push moved it twice at an unchanged head with unchanged checks;
past LAND_VERDICT_KEEP the entry was judged again for an answer that cannot differ (counted explain_unmoved_fp_moved:
one host 2026-11-25, 73 of 696 judgments that day). The keep bounds the cost; it does not remove it.

The numbers come from the test suite's simulated queue (LandWorld, simulated time, no GitHub), so one checkout's
numbers compare with another's: run it against the base and the change with the same harness.

Run: python3 scripts/measure-land-wake-redundancy.py [GLAEDA_ROOT]   (GLAEDA_ROOT/scripts/glaeda-gh is measured; the
     default is this checkout). Prints one JSON object.
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import unittest

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
loader = importlib.machinery.SourceFileLoader("glaeda_land_tests", os.path.join(ROOT, "scripts", "test-glaeda-gh.py"))
spec = importlib.util.spec_from_loader("glaeda_land_tests", loader)
t = importlib.util.module_from_spec(spec)
sys.modules["glaeda_land_tests"] = t
loader.exec_module(t)
gg, LA = t.gg, t.LA
HORIZON = 3600.0
BASE_PUSH = 90.0  # a push to the base this often: the measured pace of leo-workspace main on a working day
STEP = 30.0


class Measure(t.LandTest):
    out: dict = {}

    def test_base_churn_judgments_per_parked_hour(self) -> None:
        """Judgments an hour costs a PR parked on a wait_pr verdict whose own timeout is longer than the hour, while
        the base is pushed every BASE_PUSH s and nothing else moves."""
        wait = lambda: t.refusal("checks_running", LA, {"kind": "wait_pr", "sha": LA, "until": "done",
                                                        "timeout_s": 4 * HORIZON})
        self.pr(LA)
        self.land()
        for _ in range(200):
            self.w.answers.append(wait())
        self.tick()
        first = len(self.w.explains())
        start, flipped, flips = self.t, self.t, 0
        while self.t - start < HORIZON:
            if self.t - flipped >= BASE_PUSH:
                flipped, flips = self.t, flips + 1
                self.pr(LA, mergeable="UNKNOWN" if flips % 2 else "MERGEABLE")
            self.tick(STEP)
        judged = len(self.w.explains()) - first
        e = self.q.entries[self.KEY]
        day = (gg.read_json(gg.land_dir() / "counts.json").get("days") or {}).get(gg.land_day(self.t)) or {}
        Measure.out = {"root": ROOT, "hours": HORIZON / 3600.0, "base_pushes": flips,
                       "judgments_from_base_churn": judged,
                       "fp_moved_counted": day.get("explain_unmoved_fp_moved") or 0,
                       "state": e.get("state"), "head_moved": e.get("seenHead") != LA}


if __name__ == "__main__":
    out = io.StringIO()
    with contextlib.redirect_stderr(out), contextlib.redirect_stdout(out):
        # only this file's own case: Measure inherits every LandTest test, and an unrelated red is not this number
        result = unittest.TextTestRunner(stream=out, verbosity=0).run(
            unittest.TestLoader().loadTestsFromNames(["test_base_churn_judgments_per_parked_hour"], Measure))
    if not result.wasSuccessful():
        sys.stderr.write(out.getvalue())
        raise SystemExit(1)
    print(json.dumps(Measure.out, sort_keys=True))
