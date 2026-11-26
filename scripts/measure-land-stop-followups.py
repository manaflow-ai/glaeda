#!/usr/bin/env python3
"""Measure: after a land row stops, does anything ever reach its requester again -- per stop code, with ages.

A stopped row's requester is told once, at the stop. Whether it hears again depends on the row's code: the daemon
re-drives only LAND_REARM_CODES, and before LAND_OWNER_REMIND re-judging a row and re-reaching its owner were the
same switch, so a code correctly left out of re-judging lost its follow-up with it. This counts, per code, how many
rows sit stopped, how old they are, and how many have been followed up *since that stop*, so a code whose rows sit
unanswered is a number rather than a hunch.

Only a reminder is attributable: the `reminds` counter carries the time it last spoke, so `reminds.at > stop.at`
says this stop's requester heard again. The `rearms` counter carries a day and a count and no time, so a row
re-armed for an earlier, re-armable stop and since stopped on something else (a conflict, say) still shows a
non-zero counter -- it is reported as `rearmed_entry` and is not evidence about the current stop.

The source is the daemon's own entry store (BASE/land/entries, one file per PR), so these are the host's real rows
and the code is the row's -- not the judge's rule code, which differs for some (a base conflict is the row's
`conflict` and land_judge's `conflicts_base`; an own red is `own_red` and `red_not_excused`). Pass --log to also
tally land-judged by the judge's code from the append-only log, labelled as the other namespace.

Run: python3 scripts/measure-land-stop-followups.py [ENTRIES_DIR] [--json] [--log [PATH]]
     Defaults: $GLAEDA_GH_DIR/land/entries or ~/.cache/glaeda/gh/land/entries, log ~/.local/state/glaeda-gh.jsonl.
     Prints codes and counts only; no PR, repo, host, requester or path.
"""
from __future__ import annotations

import collections
import glob
import json
import os
import sys
import time

TOP = 40  # bounded output


def percentile(values: list, q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def read_entries(d: str, now: float) -> dict:
    per: dict = collections.defaultdict(lambda: {"rows": 0, "rearmed_entry": 0, "reminded": 0, "ages": []})
    total = unreadable = 0
    for path in glob.glob(os.path.join(d, "*")):
        try:
            with open(path, "rb") as f:
                e = json.load(f)
        except Exception:
            unreadable += 1
            continue
        if not isinstance(e, dict) or e.get("state") != "stopped":
            continue
        stop = e.get("stop") if isinstance(e.get("stop"), dict) else {}
        code = str(stop.get("code") or "?")
        total += 1
        row = per[code]
        row["rows"] += 1
        if (e.get("rearms") or {}).get("n"):
            row["rearmed_entry"] += 1  # not this stop's: the counter carries no time (see the module docstring)
        try:
            at = float(stop.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        reminds = e.get("reminds") if isinstance(e.get("reminds"), dict) else {}
        try:
            said = float(reminds.get("at") or 0)
        except (TypeError, ValueError):
            said = 0.0
        if reminds.get("n") and said > at:
            row["reminded"] += 1
        if at:
            row["ages"].append(max(0.0, now - at) / 3600.0)
    rows = []
    for code, r in sorted(per.items(), key=lambda kv: (-kv[1]["rows"], kv[0]))[:TOP]:
        ages = r["ages"]
        rows.append({"code": code, "rows": r["rows"], "reminded": r["reminded"],
                     "rearmed_entry": r["rearmed_entry"],
                     "median_h": round(percentile(ages, 0.5), 1), "p90_h": round(percentile(ages, 0.9), 1),
                     "max_h": round(max(ages), 1) if ages else 0.0,
                     "over_24h": sum(1 for a in ages if a > 24), "over_48h": sum(1 for a in ages if a > 48)})
    silent = [r["code"] for r in rows if r["rows"] and not r["reminded"]]
    return {"stopped_rows": total, "unreadable_entries": unreadable, "codes": len(per), "rows": rows,
            "codes_with_no_followup": silent}


def read_log(path: str) -> dict:
    judged: collections.Counter = collections.Counter()
    lines = unparsed = 0
    with open(path, "rb") as f:
        for raw in f:
            lines += 1
            try:
                x = json.loads(raw)
            except ValueError:
                unparsed += 1
                continue
            if isinstance(x, dict) and x.get("event") == "land-judged" and isinstance(x.get("code"), str):
                judged[x["code"]] += 1
    return {"lines": lines, "unparsed": unparsed,
            "judged_by_judge_code": dict(judged.most_common(TOP))}


def main() -> int:
    argv = sys.argv[1:]
    as_json = "--json" in argv
    log_path = None
    if "--log" in argv:
        i = argv.index("--log")
        after = argv[i + 1:i + 2]
        log_path = after[0] if after and not after[0].startswith("-") else os.path.join(
            os.path.expanduser("~"), ".local", "state", "glaeda-gh.jsonl")
        argv = argv[:i] + argv[i + (2 if after and not after[0].startswith("-") else 1):]
    rest = [a for a in argv if not a.startswith("-")]
    base = os.environ.get("GLAEDA_GH_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "glaeda", "gh")
    entries = rest[0] if rest else os.path.join(base, "land", "entries")
    if not os.path.isdir(entries):
        print(f"measure-land-stop-followups: no entry store at {entries} (pass one as the first argument)",
              file=sys.stderr)
        return 2
    out = read_entries(entries, time.time())
    if log_path:
        if not os.path.exists(log_path):
            print(f"measure-land-stop-followups: no log at {log_path}", file=sys.stderr)
            return 2
        out["log"] = read_log(log_path)
    if as_json:
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    cols = ("rows", "reminded", "median_h", "p90_h", "max_h", "over_24h", "over_48h", "rearmed_entry")
    width = {c: max(10, len(c) + 2) for c in cols}  # a header never runs into its neighbour
    print("%-24s" % "stop code" + "".join("%*s" % (width[c], c) for c in cols))
    for r in out["rows"]:
        print("%-24s" % r["code"][:24] + "".join("%*s" % (width[c], r[c]) for c in cols))
    if out["codes_with_no_followup"]:
        print("\nstopped, nobody spoke again since the stop: " + ", ".join(out["codes_with_no_followup"]))
    print(f"\n{out['stopped_rows']} stopped rows, {out['codes']} codes, "
          f"{out['unreadable_entries']} unreadable entries")
    if "log" in out:
        print("\njudged, by the judge's own rule code (the other namespace):")
        for code, n in out["log"]["judged_by_judge_code"].items():
            print("  %-24s %6d" % (code[:24], n))
        print(f"  ({out['log']['lines']} log lines, {out['log']['unparsed']} unparsed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
