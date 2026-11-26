#!/usr/bin/env python3
"""On-demand detector: a land_judge refusal whose named next step is a rerun is a step, not a stop.

    python3 scripts/test-glaeda-gh-unpostable.py

Not a CI suite (ci.yml names its suites one by one): run it by hand. It is the sibling of
test-glaeda-gh.py's "a command that is a glaeda-gh wait is run as that wait" cases, for the other
half of land_judge's deferred-daemon answer.

land_judge's deferred_daemon_wait (leo-workspace tools/land_judge/engine.py) answers a head whose
`cmux-next path routing` routed the daemon tier with the same-tree cmux-tui deferred with one of two
next steps, both carried as a "command" step under code `checks_running`:

  - `glaeda-gh wait run OWNER/REPO/ID --timeout N`: the same-tree run is going, so the status may yet
    post.  Pending.  land_command_wait already takes this as the wait it is.
  - `gh run rerun ID [--failed] -R OWNER/REPO`: nothing will dispatch or post
    `cmux-next daemon tests (same-tree)` at this head.  The routing settled it; no wait can end it.

Today the second stops the land as `checks_running`, a wait-shaped stop a person has to clear by
running the printed rerun and queueing the land again (manaflow-ai/cmux#51287 and #49867, 2026-11-22).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("glaeda_gh", os.fspath(ROOT / "scripts" / "glaeda-gh"))
spec = importlib.util.spec_from_loader("glaeda_gh", loader)
gg = importlib.util.module_from_spec(spec)
loader.exec_module(gg)

HEAD = "73624632940ac4153c570988f0f379fb91b9ef10"
REPO = "manaflow-ai/cmux"
RUN = 45095171002
SAME_TREE_RUN = 45072160126


def answer(text: str, code: str = "checks_running", repo: str = REPO, number: str = "51287") -> dict:
    """A land_judge answer carrying NEXT as the v1 "command" step, as deferred_daemon_refusal's does."""
    return {"ok": False, "code": code, "repo": repo, "number": number, "head": HEAD,
            "reason": f"land-judge: refused: checks still running at {HEAD}: cmux-next daemon tests "
                      f"(same-tree) is not posted yet", "next": text,
            "action": {"v": 1, "kind": "command", "text": text, "repo": repo, "number": number}}


class UnpostableCheckIsNotAWait(unittest.TestCase):
    def test_a_rerun_the_refusal_names_is_a_step_the_queue_runs(self):
        """The orphan case: no same-tree run exists and none will be dispatched, so the whole run is
        rerun (`gh run rerun ID`, never --failed: path routing must route again)."""
        act = gg.land_action(answer(f"gh run rerun {RUN} -R {REPO}"), HEAD)
        self.assertNotIn(act["kind"], ("command", "stop"), act)  # a stop here needs a person
        self.assertEqual(act["kind"], "rerun_failed", act)
        self.assertEqual(act.get("runs"), [RUN], act)
        self.assertIs(act.get("all"), True, act)  # the whole run, so routing routes again
        self.assertEqual(act.get("repo"), REPO, act)
        self.assertEqual(act.get("from"), "command", act)

    def test_the_same_tree_runs_failed_jobs_rerun_is_a_step_too(self):
        """The same-tree run ended without posting the status: its failed jobs are rerun."""
        act = gg.land_action(answer(f"gh run rerun {SAME_TREE_RUN} --failed -R {REPO}"), HEAD)
        self.assertEqual(act["kind"], "rerun_failed", act)
        self.assertEqual(act.get("runs"), [SAME_TREE_RUN], act)
        self.assertIs(act.get("all"), False, act)

    def test_an_unpostable_check_never_reads_as_passed(self):
        """A check that will never post has not succeeded: no answer of this shape becomes a merge."""
        for text in (f"gh run rerun {RUN} -R {REPO}", f"gh run rerun {SAME_TREE_RUN} --failed -R {REPO}"):
            act = gg.land_action(answer(text), HEAD)
            self.assertNotEqual(act["kind"], "merge", text)
            self.assertNotEqual(act["kind"], "update_branch", text)

    def test_a_pending_same_tree_run_is_still_the_wait_it_was(self):
        """The other half of the same answer: a run still going is pending, and stays a wait."""
        act = gg.land_action(answer(f"glaeda-gh wait run {REPO}/{RUN} --timeout 3600"), HEAD)
        self.assertEqual(act["kind"], "wait_run", act)
        self.assertEqual((act.get("repo"), act.get("run")), (REPO, str(RUN)), act)

    def test_only_this_refusals_own_rerun_is_taken(self):
        """Narrow on purpose: another code's command, another repo's run, and anything that is not
        exactly the rerun land_judge names are left a command, which stops as before."""
        for text, code, repo in (
            (f"gh run rerun {RUN} -R {REPO}", "red_not_excused", REPO),
            (f"gh run rerun {RUN} -R {REPO}", "own_red", REPO),
            (f"gh run rerun {RUN} -R other/repo", "checks_running", REPO),
            (f"gh run rerun {RUN}", "checks_running", REPO),
            (f"gh run rerun {RUN} -R {REPO} && rm -rf /", "checks_running", REPO),
            (f"gh run rerun {RUN} --failed -R {REPO}; echo hi", "checks_running", REPO),
            ("gh run rerun abc -R " + REPO, "checks_running", REPO),
            (f"gh run view {RUN} -R {REPO}", "checks_running", REPO),
        ):
            act = gg.land_action(answer(text, code=code, repo=repo), HEAD)
            self.assertEqual(act["kind"], "command", (text, code))


if __name__ == "__main__":
    unittest.main(verbosity=2)
