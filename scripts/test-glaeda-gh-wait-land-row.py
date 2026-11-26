#!/usr/bin/env python3
"""`wait pr` must not become the second watcher of a PR this host's land queue already holds.

A land row the daemon still works reads its PR itself, judges it on its own schedule and ends the row; a `wait pr`
beside it reads nothing the daemon does not already read, and it is not free. A parked row's PR gets one unwaited
read per LAND_PEEK_EVERY (600 s); a wait promotes the same key to waited, read every WAITED_FRESH (45 s). So the
redundant watcher turns 6 reads an hour into 80 of the same shared GraphQL pool whose last 1000 points the land
daemon keeps back precisely so lands stay readable -- one such wait polled 1465 s and ended "the shared GraphQL
quota for this login is spent" on a PR that was a `waiting` land row the whole time.

On-demand only (this file is in no CI job; ci.yml names only test-glaeda-gh.py and test-glaeda-gh-edge.py):

    python3 scripts/test-glaeda-gh-wait-land-row.py

Hermetic: GLAEDA_GH_DIR is a temp tree, so every read is of land/entries files this file writes. The CLI case
refuses before any watcher, interest or GitHub read, so it needs no daemon and no network; the negative cases
(a PR with no row, a finished row, a wait pinned to another commit) are asserted on the predicate rather than by
starting the real wait they are entitled to, which would be the network read this whole change exists to avoid.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "glaeda-gh"
HOME = tempfile.mkdtemp(prefix="glaeda-gh-wait-land-row-")
os.environ["GLAEDA_GH_DIR"] = HOME  # before the import: BASE is read once, at import

loader = importlib.machinery.SourceFileLoader("glaeda_gh_wait_land_row", os.fspath(SCRIPT))
spec = importlib.util.spec_from_loader("glaeda_gh_wait_land_row", loader)
gg = importlib.util.module_from_spec(spec)
sys.modules["glaeda_gh_wait_land_row"] = gg
loader.exec_module(gg)

REPO = "teamleaderleo/glaeda-detector-no-such-repo"
HEAD = "a" * 40
OTHER = "b" * 40


def write_row(number: int, state: str, head: str = HEAD, **extra) -> dict:
    """One land/entries row, as the land queue writes them, and the entry itself."""
    key = f"pr:{REPO}#{number}"
    now = time.time()
    e = {"key": key, "repo": REPO, "number": number, "state": state, "head": head, "pinSha": head,
         "created": now, "updated": now, "wakeAt": now + 900, "requester": "session:detector:agent:detector",
         "laneHost": "detector-host", "gen": 1, "history": [{"at": now, "event": "queued"}], **extra}
    d = Path(HOME) / "land" / "entries"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{number}.json").write_text(json.dumps(e) + "\n")
    return e


class WaitPrSkipsOwnLandRow(unittest.TestCase):
    def setUp(self) -> None:
        d = Path(HOME) / "land" / "entries"
        if d.is_dir():
            for p in d.glob("*.json"):
                p.unlink()

    # the defect
    def test_active_row_is_found(self) -> None:
        """A row in a state the land daemon still works is this host's own watcher of that PR."""
        for state in ("waiting", "queued", "acting", "holding"):
            with self.subTest(state=state):
                write_row(11, state)
                row = gg.land_row_watched_here(f"pr:{REPO}#11")
                self.assertIsNotNone(row, f"a {state} land row must be seen")
                self.assertEqual(row["state"], state)

    def test_refusal_is_a_refusal_not_a_verdict(self) -> None:
        """Exit 64 with glaeda-gh's refusal shape, and never anything a caller could read as "merged"/"done"."""
        row = write_row(12, "waiting")
        code, out, lines = gg.land_row_wait_refusal(f"pr:{REPO}#12", row, 1800, True)
        self.assertEqual(code, 64)
        doc = json.loads(out)
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["code"], "land_row_watching")
        self.assertTrue(doc["reason"] and doc["next"])
        self.assertNotIn("merged", lines.lower().split("next:")[0].replace("unmerged", ""))
        self.assertTrue(lines.startswith("glaeda-gh: "))
        self.assertIn("\nnext: ", lines)

    def test_next_is_a_wait_that_can_end(self) -> None:
        """docs/handbook/wait-that-cannot-end.md: every `next:` names something that can change the state it waits
        on. `land --wait` blocks on this row in the local queue and, re-requesting the land, re-arms it."""
        row = write_row(13, "waiting")
        _, out, lines = gg.land_row_wait_refusal(f"pr:{REPO}#13", row, 600, True)
        nxt = json.loads(out)["next"]
        self.assertEqual(nxt, f"glaeda-gh land {REPO}#13 --wait 600")
        self.assertIn(nxt, lines)

    def test_refusal_names_the_row_the_caller_can_look_up(self) -> None:
        """The row's own words, as `land status` prints them, so the caller can match the two."""
        row = write_row(14, "waiting")
        _, _, lines = gg.land_row_wait_refusal(f"pr:{REPO}#14", row, 60, False)
        self.assertIn("land queue", lines)
        self.assertIn("waiting", lines)

    def test_cli_refuses_instead_of_starting_the_second_watcher(self) -> None:
        """The real command, end to end: it must refuse before any watcher, any interest and any GitHub read."""
        write_row(15, "waiting")
        env = {**os.environ, "GLAEDA_GH_DIR": HOME}
        began = time.monotonic()
        p = subprocess.run([sys.executable, "-I", os.fspath(SCRIPT), "wait", "pr", f"{REPO}#15",
                            "--timeout", "8", "--json"], capture_output=True, text=True, env=env, timeout=120)
        took = time.monotonic() - began
        self.assertEqual(p.returncode, 64, f"exit {p.returncode}; out={p.stdout[-400:]!r} err={p.stderr[-400:]!r}")
        self.assertEqual(json.loads(p.stdout.strip())["code"], "land_row_watching")
        self.assertLess(took, 8, "it must refuse at once, not after waiting out the timeout")

    # a legitimate wait must still work
    def test_cli_with_no_row_still_takes_the_normal_wait_path(self) -> None:
        """The gate swallows nothing it must not: with no row for this PR, the command goes on to the wait it
        always took. Here that path ends at the daemon check (exit 3, no daemon in a temp GLAEDA_GH_DIR), which is
        proof enough that it got past the gate -- and it is the exit the unfixed tree gave for the held PR too."""
        write_row(31, "waiting")  # a row for a different PR must not stand in for this one
        env = {**os.environ, "GLAEDA_GH_DIR": HOME}
        p = subprocess.run([sys.executable, "-I", os.fspath(SCRIPT), "wait", "pr", f"{REPO}#32",
                            "--timeout", "8", "--json"], capture_output=True, text=True, env=env, timeout=120)
        self.assertNotEqual(p.returncode, 64, "a PR with no row here must not be refused")
        self.assertNotIn("land_row_watching", p.stdout + p.stderr)

    def test_no_row_here_waits_as_before(self) -> None:
        gg.land_entries()  # an empty (or absent) queue is not an error
        self.assertIsNone(gg.land_row_watched_here(f"pr:{REPO}#21"))

    def test_finished_row_waits_as_before(self) -> None:
        """A row the daemon has let go watches nothing, including one forwarded to another host's queue."""
        for state in sorted(gg.LAND_DONE):
            with self.subTest(state=state):
                write_row(22, state)
                self.assertIsNone(gg.land_row_watched_here(f"pr:{REPO}#22"),
                                  f"a {state} row is nobody's watcher")

    def test_another_prs_row_waits_as_before(self) -> None:
        write_row(23, "waiting")
        self.assertIsNone(gg.land_row_watched_here(f"pr:{REPO}#24"))

    def test_wait_pinned_to_another_commit_waits_as_before(self) -> None:
        """A wait on a commit this row does not hold asks a question the row's end does not answer."""
        write_row(25, "waiting", head=HEAD)
        self.assertIsNone(gg.land_row_watched_here(f"pr:{REPO}#25", OTHER))
        self.assertIsNotNone(gg.land_row_watched_here(f"pr:{REPO}#25", HEAD.upper()))
        self.assertIsNotNone(gg.land_row_watched_here(f"pr:{REPO}#25", None))

    def test_a_run_wait_is_untouched(self) -> None:
        """The land queue holds PRs; `wait run` never consults it."""
        write_row(26, "waiting")
        self.assertIsNone(gg.land_row_watched_here(f"run:{REPO}/26"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
