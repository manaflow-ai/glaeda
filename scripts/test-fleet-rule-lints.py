#!/usr/bin/env python3
"""Three fleet rules, checked over every tracked file, with an allowlist that can only shrink.

- keychain: nothing reads the fleet sudo password (keychain service manaflow-fleet-sudo) but the one fleet-sudo
  tool, which pipes it to `sudo -S` alone and redacts it. glaeda has no reader, so its allowlist is empty.
- pkill: kill only processes you started, by the PID or process group you recorded; never by name or pattern
  (`pkill`, `killall`). The few name-based kills glaeda keeps are listed with why each is scoped and safe.
- big-red: the personal Linux workstation is not infrastructure; nothing (a workflow, runner label, script,
  service or install path) may depend on it. Prose under docs/ and *.md is exempt (history and "never" notes).

scripts/fleet-rule-allowlist.json maps rule -> path -> {"count": N, "reason": "..."}; a count is matches (every
occurrence, not lines). A file's matches must equal its count exactly: more fails (a new violation), fewer fails too
(lower the count). An entry for a file with no matches, or with no reason, fails. With FLEET_RULE_BASE set (CI sets
HEAD^1, the base the change merges into), the allowlist may not grow against the base's: no new path and no higher
count, so a new violation cannot be allowlisted in the same change. The lint and its allowlist are not scanned.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "scripts" / "fleet-rule-allowlist.json"
SELF = {"scripts/test-fleet-rule-lints.py", "scripts/fleet-rule-allowlist.json"}

KEYCHAIN_SERVICE = re.compile(r"manaflow-fleet-sudo")
# Any way to read a keychain item: the security CLI, the Security framework, Python keyring (however imported).
KEYCHAIN_READ = re.compile(r"find-(generic|internet)-password|SecItemCopyMatching|SecKeychainFind|\bkeyring\b|"
                           r"get_password\s*\(|security\s+find-")
KEYCHAIN_LINE = re.compile(r"find-generic-password[^\n]*(fleet[-_ ]sudo|KEYCHAIN_SERVICE)")
PKILL = re.compile(r"\b(pkill|killall)\b")
BIG_RED = re.compile(r"big[-_ ]?red", re.IGNORECASE)

FIX = {
    "keychain": "read the fleet sudo password only through `fleet-sudo HOST CMD` (leo-workspace tools/fleet-sudo)",
    "pkill": "signal only the PID or process group you started and recorded; never pkill/killall by name or "
             "pattern. A genuinely scoped exception goes in scripts/fleet-rule-allowlist.json with its reason",
    "big-red": "nothing may depend on the personal Linux workstation: no runner label, workflow, service, install "
               "path or script for it. Use a fleet host or Blacksmith",
}


def keychain_matches(text: str) -> int:
    """A file that names the service and reads a keychain counts every naming of the service; a read that names
    the service only through a variable (KEYCHAIN_SERVICE) counts too."""
    lines = text.splitlines()
    if KEYCHAIN_SERVICE.search(text) and KEYCHAIN_READ.search(text):
        return sum(1 for line in lines if KEYCHAIN_SERVICE.search(line) or KEYCHAIN_LINE.search(line))
    return sum(1 for line in lines if KEYCHAIN_LINE.search(line))


def line_matches(pattern: re.Pattern[str], text: str) -> int:
    """Every occurrence, so a second kill added to an allowlisted line still raises the count."""
    return len(pattern.findall(text))


def big_red_scanned(rel: str) -> bool:
    return not (rel.endswith(".md") or rel.startswith("docs/"))


def scan(root: Path) -> dict[str, dict[str, int]]:
    files = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
    found: dict[str, dict[str, int]] = {"keychain": {}, "pkill": {}, "big-red": {}}
    for rel in filter(None, files.decode().split("\0")):
        path = root / rel
        if rel in SELF or not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        counts = {"keychain": keychain_matches(text), "pkill": line_matches(PKILL, text),
                  "big-red": line_matches(BIG_RED, text) if big_red_scanned(rel) else 0}
        for rule, n in counts.items():
            if n:
                found[rule][rel] = n
    return found


def problems(found: dict[str, dict[str, int]], allow: dict) -> list[str]:
    out = []
    for rule in found:
        entries = allow.get(rule, {})
        for rel, n in sorted(found[rule].items()):
            entry = entries.get(rel)
            if entry is None:
                out.append(f"{rule}: {rel}: {n} new match(es); {FIX[rule]}")
            elif n > entry["count"]:
                out.append(f"{rule}: {rel}: {n} matches, allowlist says {entry['count']}; {FIX[rule]}")
            elif n < entry["count"]:
                out.append(f"{rule}: {rel}: {n} matches, allowlist says {entry['count']}: lower it to {n}")
        for rel, entry in sorted(entries.items()):
            if rel not in found[rule]:
                out.append(f"{rule}: {rel}: allowlisted but has no match: remove the entry")
            if not str(entry.get("reason", "")).strip():
                out.append(f"{rule}: {rel}: allowlist entry has no reason")
    for rule in allow:
        if rule not in found and not rule.startswith("_"):
            out.append(f"unknown rule {rule!r} in the allowlist")
    return out


def growth(allow: dict, base: dict | None) -> list[str]:
    """Entries that are new or higher than in the base allowlist; None (no allowlist at the base) is the
    change that introduces the lint."""
    if base is None:
        return []
    out = []
    for rule, entries in allow.items():
        if rule.startswith("_"):
            continue
        for rel, entry in sorted(entries.items()):
            before = base.get(rule, {}).get(rel)
            if before is None:
                out.append(f"{rule}: {rel}: new allowlist entry; the allowlist only shrinks. {FIX[rule]}")
            elif entry["count"] > before["count"]:
                out.append(f"{rule}: {rel}: allowlist count {entry['count']} is above the base's {before['count']}; "
                           f"{FIX[rule]}")
    return out


def base_allowlist(ref: str) -> dict | None:
    rel = ALLOWLIST.relative_to(ROOT).as_posix()
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                            capture_output=True, text=True)
    if commit.returncode != 0:
        raise AssertionError(f"FLEET_RULE_BASE={ref} is not a commit here (the checkout needs fetch-depth 2)")
    shown = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:{rel}"], capture_output=True, text=True)
    return json.loads(shown.stdout) if shown.returncode == 0 else None


class FleetRuleLints(unittest.TestCase):
    def test_tree_matches_the_allowlist(self):
        allow = json.loads(ALLOWLIST.read_text())
        bad = problems(scan(ROOT), allow)
        self.assertEqual(bad, [], "\n" + "\n".join(bad))

    def test_the_allowlist_did_not_grow_against_the_base(self):
        ref = os.environ.get("FLEET_RULE_BASE")
        if not ref:
            self.skipTest("FLEET_RULE_BASE not set (CI sets it to the base, HEAD^1)")
        bad = growth(json.loads(ALLOWLIST.read_text()), base_allowlist(ref))
        self.assertEqual(bad, [], "\n" + "\n".join(bad))

    def test_patterns_catch_the_violations(self):
        self.assertEqual(keychain_matches(
            'p = run(["security", "find-generic-password", "-s", "manaflow-fleet-sudo", "-w"])\n'), 1)
        self.assertEqual(keychain_matches('KEYCHAIN_SERVICE=manaflow-fleet-sudo\n'
                                          '"$SECURITY" find-generic-password -s "$KEYCHAIN_SERVICE" -w\n'), 2)
        self.assertEqual(keychain_matches("the fleet password lives in manaflow-fleet-sudo\n"), 0)
        self.assertEqual(keychain_matches('from keyring import get_password\npw = get_password("manaflow-fleet-sudo", "x")\n'), 1)
        self.assertEqual(keychain_matches('import keyring as k\nk.get_password("manaflow-fleet-sudo", "x")\n'), 1)
        self.assertEqual(line_matches(PKILL, "pkill -x a -u 1; pkill -f b"), 2)
        for line in ("pkill -f glaeda", "subprocess.run(['/usr/bin/killall', 'Dock'])", "  /usr/bin/pkill -9 x"):
            self.assertEqual(line_matches(PKILL, line), 1, line)
        for line in ("kill -TERM -- -$pgid", "os.killpg(p.pid, signal.SIGTERM)", "skill_set"):
            self.assertEqual(line_matches(PKILL, line), 0, line)
        for line in ("runs-on: big-red-owned-linux", "ssh big_red true", "host = 'BigRed'", "Big Red"):
            self.assertEqual(line_matches(BIG_RED, line), 1, line)
        self.assertFalse(big_red_scanned("docs/history.md"))
        self.assertTrue(big_red_scanned(".github/workflows/x.yml"))

    def test_the_allowlist_only_shrinks(self):
        allow = {"pkill": {"a.sh": {"count": 2, "reason": "scoped"}, "gone.sh": {"count": 1, "reason": "x"}},
                 "keychain": {}, "big-red": {"b.py": {"count": 1, "reason": ""}}}
        found = {"pkill": {"a.sh": 3, "new.sh": 1}, "keychain": {}, "big-red": {"b.py": 1}}
        bad = problems(found, allow)
        self.assertTrue(any("a.sh: 3 matches" in b for b in bad))
        self.assertTrue(any("new.sh: 1 new" in b for b in bad))
        self.assertTrue(any("gone.sh: allowlisted but has no match" in b for b in bad))
        self.assertTrue(any("b.py: allowlist entry has no reason" in b for b in bad))
        self.assertIn("pkill: a.sh: 1 matches, allowlist says 2: lower it to 1",
                      problems({"pkill": {"a.sh": 1}, "keychain": {}, "big-red": {}},
                               {"pkill": {"a.sh": {"count": 2, "reason": "r"}}}))

    def test_the_allowlist_cannot_grow_against_the_base(self):
        base = {"pkill": {"a.sh": {"count": 2, "reason": "r"}}}
        self.assertEqual(growth({"pkill": {"a.sh": {"count": 1, "reason": "r"}}}, base), [])
        self.assertEqual(len(growth({"pkill": {"a.sh": {"count": 3, "reason": "r"}}}, base)), 1)
        self.assertEqual(len(growth({"pkill": {"a.sh": {"count": 2, "reason": "r"},
                                               "b.sh": {"count": 1, "reason": "safe, honest"}}}, base)), 1)
        self.assertEqual(growth({"pkill": {"new.sh": {"count": 1, "reason": "r"}}}, None), [])


if __name__ == "__main__":
    unittest.main()
