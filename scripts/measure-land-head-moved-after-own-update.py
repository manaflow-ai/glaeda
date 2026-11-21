#!/usr/bin/env python3
"""Measure: how often a pinned land stops head_moved right after the queue's own update-branch moved the head.

That stop is a refusal built from a state transition the refuser performed itself: the daemon judges tip_merge, runs
update_branch at the pinned head, GitHub makes a merge commit, and the next tick sees a head that is not the pin. Each
one costs a human round trip (read the trace, read the new head from GitHub, re-queue). LandQueue.pin_follow makes the
pin follow that merge commit when its first parent is the pin, so this count is what that fix removes.

Reads only the land queue's own durable store ($GLAEDA_GH_DIR, else ~/.cache/glaeda/gh), never GitHub. Each entry
keeps the last LAND_HISTORY events, so this is a window of roughly the last day, not all time: the window it covers
is printed with the counts.

Run: python3 -I scripts/measure-land-head-moved-after-own-update.py [--dir DIR] [--json]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import time

OWN_STEPS = ("update_branch", "tip_merge")  # steps of ours that make a commit on the head branch


def rows(entries: str) -> list[dict]:
    """One record per head_moved stop in the store's histories."""
    now = time.time()
    out: list[dict] = []
    for path in sorted(glob.glob(os.path.join(entries, "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                e = json.load(fh)
        except (OSError, ValueError):
            continue
        history = e.get("history") or []
        for i, ev in enumerate(history):
            if ev.get("event") != "stopped" or ev.get("code") != "head_moved":
                continue
            step = next((h for h in reversed(history[:i]) if h.get("event") == "step"), {})
            after = next((h for h in history[i + 1:] if h.get("event") in ("queued", "rearmed")), None)
            out.append({
                "repo": e.get("repo"), "number": e.get("number"), "at": ev.get("at"),
                "step": step.get("kind"), "exit": step.get("exit"),
                # The class: our own successful head-moving step was the last thing this row did.
                "own": step.get("kind") in OWN_STEPS and step.get("exit") == 0,
                "sat": round(((after or {}).get("at") or now) - (ev.get("at") or now), 1),
                "cleared": bool(after),
            })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=os.environ.get("GLAEDA_GH_DIR") or os.path.expanduser("~/.cache/glaeda/gh"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    entries = os.path.join(args.dir, "land", "entries")
    if not os.path.isdir(entries):
        print(f"no land store at {entries}: nothing measured (not zero)")
        return 2
    found = rows(entries)
    own = [r for r in found if r["own"]]
    ats = [r["at"] for r in found if r["at"]]
    out = {
        "store": entries,
        "entries": len(glob.glob(os.path.join(entries, "*.json"))),
        "headMoved": len(found),
        "afterOwnUpdateBranch": len(own),
        "share": round(len(own) / len(found), 3) if found else None,
        "window": {"from": min(ats), "to": max(ats)} if ats else None,
        "satMinutes": {
            "median": round(statistics.median(r["sat"] for r in own) / 60, 1) if own else None,
            "max": round(max(r["sat"] for r in own) / 60, 1) if own else None,
        },
        "rows": own,
    }
    if args.json:
        print(json.dumps(out, indent=1, sort_keys=True))
        return 0
    print(f"store {entries} ({out['entries']} rows; each keeps its last LAND_HISTORY events)")
    print(f"head_moved stops: {out['headMoved']}")
    print(f"  after this row's own successful {'/'.join(OWN_STEPS)}: {out['afterOwnUpdateBranch']}"
          f"{f' ({out['share'] * 100:.0f}%)' if out['share'] is not None else ''}")
    print(f"  minutes sat stopped: median {out['satMinutes']['median']}, max {out['satMinutes']['max']}")
    for r in own:
        print(f"  {r['repo']}#{r['number']} sat {round(r['sat'] / 60, 1)} min"
              f"{'' if r['cleared'] else ' (still stopped at this read)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
