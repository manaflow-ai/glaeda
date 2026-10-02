#!/usr/bin/env python3
"""Tests for glaeda-cmux-runner-fleet with fake ssh, scp and gh on PATH (nothing leaves this machine)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "glaeda-cmux-runner-fleet"
# What the tool needs beside itself: the staged files and the labels module. Each test runs a copy from its own
# git checkout with a local bare origin, so HEAD and origin's main are whatever the test makes them.
CHECKOUT_FILES = ("glaeda-cmux-runner-fleet", "glaeda-cmux-runner", "glaeda-cmux-runner-hook",
                  "glaeda_fleet_labels.py", "glaeda_reservation.py")
TOKEN = "AAAREGTOKEN123"
MANIFEST = {
    "defaults": {"xcode": {"apps": [{"path": "/Applications/Xcode_26.6.app", "version": "26.6", "build": "17F113"}]},
                 "disk": {"min_free_gib": 100}, "ios_simulator": {"runtimes": ["23F77"]},
                 "people": {"secret": "never copied"}},
    "hosts": {
        "mini-a": {"class": "std", "availability": "dedicated", "roles": ["ci-runner"], "hardware": "m4pro-48"},
        "mini-b": {"class": "std", "availability": "dedicated", "roles": ["ci-runner"], "hardware": "m4pro-48"},
        "mini-light": {"class": "light", "availability": "dedicated", "roles": ["ci-runner"], "hardware": "m4-16"},
        "cache": {"class": "std", "availability": "dedicated", "roles": ["cache-host"], "hardware": "m4pro-48"},
    },
}

# Each fake appends one JSON line per call: argv, and for ssh the stdin it got (a token or the hook source).
FAKE_SSH = """#!{python}
import json, os, sys, time
log = os.environ["FAKE_LOG"]
args = sys.argv[1:]
while args and args[0] == "-o":
    args = args[2:]
host, remote = args[0], " ".join(args[1:])
data = sys.stdin.read()
open(log, "a").write(json.dumps({{"tool": "ssh", "host": host, "remote": remote, "stdin": data[:40]}}) + "\\n")
if host in os.environ.get("FAKE_DOWN", "").split(","):
    sys.stderr.write("ssh: connect to host " + host + " port 22: Connection refused\\n"); sys.exit(255)
if remote.startswith("head -c") and ".glaeda-source.json" in remote:
    stamps = json.loads(os.environ.get("FAKE_STAMPS", "{{}}"))
    print(stamps[host] if host in stamps else "@no-stamp"); sys.exit(0)
if " check " in remote:
    if host in os.environ.get("FAKE_ELIGIBLE", "").split(","):
        print("glaeda-cmux-runner-hook: eligible"); sys.exit(0)
    print("glaeda-cmux-runner-hook: refused: node not eligible (no Glaeda enrollment on this mini)"); sys.exit(1)
if "--uninstall" in remote and "--apply" not in remote:
    k = remote.split("--instance ")[1].split()[0]
    scopes = dict(x.split("=") for x in os.environ.get("FAKE_SCOPES", "").split(",") if x)
    scope = scopes.get(host + ":" + k, scopes.get(host, "manaflow-ai/cmux"))
    state = ("remove" if "--token-stdin" in remote else "blocked") if scope else "kept"  # no gh on a mini
    print(json.dumps({{"actions": [{{"kind": "deregister", "state": state, "scope": scope}}]}}))
    sys.exit(0)
if "--apply" in remote and host in os.environ.get("FAKE_LAUNCHCTL_FAILS", "").split(","):
    print(json.dumps({{"actions": [{{"kind": "launchctl", "state": "failed", "note": "bootstrap: Input/output error"}}]}}))
    sys.exit(1)
if "--apply" in remote and "--uninstall" not in remote and host in os.environ.get("FAKE_REGISTER_FAILS", "").split(","):
    print(json.dumps({{"actions": [{{"kind": "register", "state": "failed"}}, {{"kind": "verify", "state": "skipped"}}]}}))
    sys.exit(1)
if "--apply" in remote:
    time.sleep(float(os.environ.get("FAKE_APPLY_S", "0")))
    k = remote.split("--instance ")[1].split()[0]
    ok = data.strip() == "{token}"
    print(json.dumps({{"actions": [{{"kind": "register", "state": "create" if ok else "failed",
                                    "name": host + "-glaeda" + ("" if k == "0" else "-" + k)}},
                                   {{"kind": "verify", "state": "ok" if ok else "failed"}}]}}))
    sys.exit(0 if ok else 1)
sys.exit(0)
"""
FAKE_SCP = """#!{python}
import json, os, sys
open(os.environ["FAKE_LOG"], "a").write(json.dumps({{"tool": "scp", "argv": sys.argv[1:]}}) + "\\n")
dest = sys.argv[-1]
for src in sys.argv[1:-1]:
    if src.endswith(".glaeda-source.json"):
        open(os.environ["FAKE_LOG"] + "." + dest.split(":")[0] + ".stamp", "w").write(open(src).read())
if dest.endswith("mini-fleet.json"):
    src = sys.argv[-2]
    open(os.environ["FAKE_LOG"] + "." + dest.split(":")[0] + ".manifest", "w").write(open(src).read())
"""
FAKE_GH = """#!{python}
import json, os, sys
open(os.environ["FAKE_LOG"], "a").write(json.dumps({{"tool": "gh", "argv": sys.argv[1:]}}) + "\\n")
path = next((a for a in sys.argv[2:] if "/" in a and not a.startswith("-")), "")
if path.startswith("orgs/") and path.endswith("runner-groups?per_page=100"):
    groups = json.loads(os.environ.get("FAKE_GROUPS", "[]"))
    print(json.dumps({{"runner_groups": groups}}))
elif "/repositories" in path:
    print(os.environ.get("FAKE_GROUP_REPOS", "manaflow-ai/cmux").replace(",", "\\n"))
elif path.startswith("repos/") and path.count("/") == 2:
    print("false")
elif path.startswith("orgs/") and path.endswith("registration-token") and os.environ.get("FAKE_NO_ORG_TOKEN"):
    sys.exit(1)
else:
    print("{token}")
"""
GROUP = json.dumps([{"id": 7, "name": "glaeda-minis", "visibility": "selected", "allows_public_repositories": True}])


class FleetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        for name, body in (("ssh", FAKE_SSH), ("scp", FAKE_SCP), ("gh", FAKE_GH)):
            path = bin_dir / name
            path.write_text(body.format(python=sys.executable, token=TOKEN))
            path.chmod(0o755)
        self.manifest = self.dir / "mini-fleet.json"
        self.manifest.write_text(json.dumps(MANIFEST))
        self.log = self.dir / "calls.jsonl"
        self.env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": os.fspath(self.dir), "FAKE_LOG": os.fspath(self.log),
                    "FAKE_ELIGIBLE": "mini-a,mini-b"}
        self.origin = self.dir / "origin.git"
        self.repo = self.dir / "glaeda"
        (self.repo / "scripts").mkdir(parents=True)
        for name in CHECKOUT_FILES:
            shutil.copy2(ROOT / "scripts" / name, self.repo / "scripts" / name)
        self.git("init", "-q", "--bare", "-b", "main", os.fspath(self.origin), cwd=self.dir)
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "one")
        self.old = self.git("rev-parse", "HEAD")
        self.head = self.commit("two")
        self.git("remote", "add", "origin", os.fspath(self.origin))
        self.git("push", "-q", "origin", "main")
        self.tool = self.repo / "scripts" / "glaeda-cmux-runner-fleet"

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                               "-c", "commit.gpgsign=false", "-C", os.fspath(cwd or self.repo), *args],
                              capture_output=True, text=True, check=True, timeout=60,
                              env={"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GIT_CONFIG_NOSYSTEM": "1"},
                              ).stdout.strip()

    def commit(self, name: str) -> str:
        (self.repo / f"{name}.txt").write_text(name + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", name)
        return self.git("rev-parse", "HEAD")

    @staticmethod
    def stamp(source: str) -> str:
        return json.dumps({"by": "glaeda-cmux-runner-fleet", "date": "2026-09-30", "source": source})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def fleet(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, os.fspath(self.tool), "--manifest", os.fspath(self.manifest),
                               "--output", "json", *args], capture_output=True, text=True, timeout=120,
                              env={**self.env, **env}, check=False)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_plan_is_read_only_and_skips_ineligible(self) -> None:
        result = self.fleet()
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(result.stdout)
        rows = {r["member"]: r for r in doc["members"]}
        self.assertEqual(set(rows), {"mini-a", "mini-b", "mini-light"})
        self.assertIn("cache", doc["skipped"])
        self.assertEqual(rows["mini-a"]["action"], "would apply")
        self.assertEqual(rows["mini-light"]["action"], "skip (not eligible)")
        self.assertEqual((rows["mini-a"]["runners"], rows["mini-light"]["runners"]), (4, 2))
        tools = {c["tool"] for c in self.calls()}
        self.assertEqual(tools, {"ssh"}, "a plan only runs the gate")
        checks = [c for c in self.calls() if " check " in c["remote"]]
        self.assertTrue(all(c["stdin"].startswith("#!/usr/bin/env python3") for c in checks), "hook sent on stdin")
        self.assertTrue(all("--fleet-class m4pro-48" in c["remote"] or "m4-16" in c["remote"] for c in checks))

    def test_apply_every_instance_in_parallel_with_piped_tokens(self) -> None:
        start = time.monotonic()
        result = self.fleet("--apply", FAKE_APPLY_S="2")
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = {r["member"]: r for r in json.loads(result.stdout)["members"]}
        self.assertEqual([i["name"] for i in rows["mini-a"]["instances"]],
                         ["mini-a-glaeda", "mini-a-glaeda-1", "mini-a-glaeda-2", "mini-a-glaeda-3"])
        self.assertTrue(all(i["state"] == "create/ok" for r in ("mini-a", "mini-b") for i in rows[r]["instances"]))
        self.assertNotIn("instances", rows["mini-light"])
        self.assertLess(elapsed, 8, "8 instances of 2 s each must run at once, not one after another")
        calls = self.calls()
        self.assertNotIn(TOKEN, json.dumps([c.get("argv", c.get("remote")) for c in calls]), "token only on stdin")
        applies = [c for c in calls if c["tool"] == "ssh" and "--apply" in c["remote"]]
        self.assertEqual(len(applies), 8)
        self.assertTrue(all(c["stdin"].strip() == TOKEN for c in applies))
        self.assertEqual(sum(1 for c in calls if c["tool"] == "gh"), 8, "one token per instance")
        staged = json.loads((self.dir / "calls.jsonl.mini-a.manifest").read_text())
        self.assertEqual(set(staged["hosts"]), {"mini-a"})
        self.assertEqual(set(staged["defaults"]), {"xcode", "disk", "ios_simulator"},
                         "no other hosts, keys or people; ios_simulator gates glaeda-ios-sim on the mini")
        # the staged copy says which commit it is, so glaeda-cmux-runner and glaeda-update can tell it from a stale one
        stamp = json.loads((self.dir / "calls.jsonl.mini-a.stamp").read_text())
        head = subprocess.run(["env", "TZ=UTC", "git", "-C", os.fspath(self.repo), "log", "-1",
                               "--format=%H %cd", "--date=format-local:%Y-%m-%d"],
                              capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual((stamp["by"], stamp["source"], stamp["date"]), ("glaeda-cmux-runner-fleet", *head))

    def test_org_migration_goes_one_member_at_a_time_and_skips_trusted(self) -> None:
        manifest = json.loads(json.dumps(MANIFEST))
        manifest["hosts"]["mini-t"] = {"class": "std", "availability": "dedicated", "roles": ["ci-runner"],
                                       "hardware": "m4pro-48", "overrides": {"runner": {
                                           "trustedRef": "refs/heads/main", "trustedRepo": "manaflow-ai/cmux"}}}
        self.manifest.write_text(json.dumps(manifest))
        result = self.fleet("--apply", "--org", "manaflow-ai", "--group", "glaeda-minis",
                            "--migrate-from-repo", "manaflow-ai/cmux", FAKE_ELIGIBLE="mini-a,mini-b,mini-t",
                            FAKE_GROUPS=GROUP, FAKE_SCOPES="mini-b:1=manaflow-ai,mini-b:2=")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = {r["member"]: r for r in json.loads(result.stdout)["members"]}
        self.assertIn("trusted-only runner stays on manaflow-ai/cmux", rows["mini-t"]["action"])
        calls = self.calls()
        gh = [c["argv"][3] for c in calls if c["tool"] == "gh" and len(c["argv"]) > 3]
        # mini-b instance 1 already moved and instance 2 has no registration: only 6 deregistrations
        self.assertEqual(gh.count("repos/manaflow-ai/cmux/actions/runners/remove-token"), 6)
        self.assertEqual(gh.count("orgs/manaflow-ai/actions/runners/registration-token"), 8)
        ssh = [c for c in calls if c["tool"] == "ssh" and "--apply" in c["remote"]]
        self.assertNotIn("mini-t", {c["host"] for c in ssh})
        registers = [c for c in ssh if "--uninstall" not in c["remote"]]
        self.assertEqual(len(registers), 8)
        self.assertTrue(all("--org manaflow-ai --group glaeda-minis" in c["remote"] for c in registers))
        # one member at a time: every mini-a call happens before any mini-b call
        hosts = [c["host"] for c in ssh]
        self.assertEqual(hosts, sorted(hosts), "members migrate one after another")
        self.assertNotIn(TOKEN, json.dumps([c.get("argv", c.get("remote")) for c in calls]), "token only on stdin")

    def test_migration_touches_nothing_without_a_usable_group_or_token(self) -> None:
        args = ("--apply", "--org", "manaflow-ai", "--group", "glaeda-minis", "--migrate-from-repo", "manaflow-ai/cmux")
        missing = self.fleet(*args, FAKE_GROUPS="[]")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("no runner group named", missing.stderr)
        closed = self.fleet(*args, FAKE_GROUPS=GROUP, FAKE_GROUP_REPOS="manaflow-ai/other")
        self.assertEqual(closed.returncode, 2)
        self.assertIn("does not allow manaflow-ai/cmux", closed.stderr)
        self.assertEqual(self.fleet("--apply", "--org", "manaflow-ai", "--migrate-from-repo",
                                    "manaflow-ai/cmux").returncode, 2, "migration needs a group")
        self.assertFalse(any(c["tool"] == "ssh" and "--apply" in c["remote"] for c in self.calls()))
        notoken = self.fleet(*args, FAKE_GROUPS=GROUP, FAKE_NO_ORG_TOKEN="1")
        self.assertEqual(notoken.returncode, 1)
        self.assertIn("nothing changed", notoken.stdout)
        self.assertFalse(any(c["tool"] == "ssh" and "--uninstall" in c["remote"] for c in self.calls()))

    def test_failed_registration_after_deregistering_says_no_runner(self) -> None:
        result = self.fleet("--apply", "--org", "manaflow-ai", "--group", "glaeda-minis", "--hosts", "mini-a",
                            "--migrate-from-repo", "manaflow-ai/cmux", FAKE_GROUPS=GROUP, FAKE_REGISTER_FAILS="mini-a")
        self.assertEqual(result.returncode, 1)
        rows = {r["member"]: r for r in json.loads(result.stdout)["members"]}
        self.assertTrue(all(i["state"].startswith("NO RUNNER: deregistered from manaflow-ai/cmux")
                            for i in rows["mini-a"]["instances"]))

    def test_group_needs_org(self) -> None:
        result = self.fleet("--group", "glaeda-minis")
        self.assertEqual(result.returncode, 2)
        self.assertIn("need --org", result.stderr)

    def test_only_named_hosts_and_include_ineligible_stopped(self) -> None:
        result = self.fleet("--apply", "--hosts", "mini-light", "--include-ineligible", "--skip-launchctl")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = json.loads(result.stdout)["members"]
        self.assertEqual([r["member"] for r in rows], ["mini-light"])
        self.assertEqual(len(rows[0]["instances"]), 2)
        applies = [c for c in self.calls() if c["tool"] == "ssh" and "--apply" in c["remote"]]
        self.assertTrue(applies and all("--skip-launchctl" in c["remote"] for c in applies))
        self.assertFalse(any(c.get("host") in ("mini-a", "mini-b") for c in self.calls()))

    def test_an_unreachable_member_fails_the_apply_not_skips(self) -> None:
        result = self.fleet("--apply", FAKE_DOWN="mini-b")
        self.assertEqual(result.returncode, 1, result.stdout)
        rows = {r["member"]: r for r in json.loads(result.stdout)["members"]}
        self.assertTrue(rows["mini-b"]["action"].startswith("unreachable"), rows["mini-b"])
        self.assertIn("Connection refused", rows["mini-b"]["gate"])
        self.assertEqual(rows["mini-a"]["action"], "applied")
        self.assertEqual(self.fleet(FAKE_DOWN="mini-b").returncode, 0, "a plan still reports and exits 0")

    def test_an_unknown_host_is_an_error(self) -> None:
        result = self.fleet("--hosts", "mini-a,mini-typo")
        self.assertEqual(result.returncode, 2)
        self.assertIn("mini-typo", result.stderr)

    def test_a_failed_instance_fails_the_run(self) -> None:
        bad = self.dir / "bin" / "gh"
        bad.write_text(f"#!{sys.executable}\nprint('not a token!')\n")
        result = self.fleet("--apply", "--hosts", "mini-a")
        self.assertEqual(result.returncode, 1)
        states = [i["state"] for i in json.loads(result.stdout)["members"][0]["instances"]]
        self.assertTrue(all("no registration token" in s for s in states))
        self.assertFalse(any(c["tool"] == "ssh" and "--apply" in c["remote"] for c in self.calls()))


    def test_a_failure_before_register_names_the_step(self) -> None:
        result = self.fleet("--apply", "--hosts", "mini-a", FAKE_LAUNCHCTL_FAILS="mini-a")
        self.assertEqual(result.returncode, 1)
        states = [i["state"] for i in json.loads(result.stdout)["members"][0]["instances"]]
        self.assertTrue(states and all("launchctl: bootstrap: Input/output error" in s for s in states), states)

    # ---- the downgrade guard: what the checkout would deploy against origin's main and every host's stamp

    def assert_nothing_changed(self) -> None:
        calls = self.calls()
        self.assertFalse(any(c["tool"] == "scp" for c in calls), "nothing staged")
        self.assertFalse(any(c["tool"] == "ssh" and ("--apply" in c["remote"] or "mkdir" in c["remote"])
                             for c in calls), "nothing applied")
        self.assertFalse(any(c["tool"] == "gh" for c in calls), "no token minted")

    def test_apply_from_a_checkout_that_is_not_origin_main_refuses_and_changes_nothing(self) -> None:
        newer = self.commit("three")
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-q", "--detach", self.head)  # an old checkout: origin's main moved on
        result = self.fleet("--apply", "--org", "manaflow-ai", "--group", "glaeda-minis", FAKE_GROUPS=GROUP)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"HEAD {self.head} is not origin/main {newer}", result.stderr)
        self.assertIn("--allow-downgrade REASON", result.stderr)
        self.assertIn("nothing changed", result.stderr)
        self.assert_nothing_changed()

    def test_apply_refuses_a_host_whose_stamp_is_not_an_ancestor_of_head(self) -> None:
        self.git("checkout", "-q", "-b", "side", self.old)
        side = self.commit("side")  # a diverged deploy: a commit HEAD does not contain
        self.git("checkout", "-q", "main")
        newer = "f" * 40  # a commit this checkout has never seen: a newer main, so a downgrade
        result = self.fleet("--apply", FAKE_STAMPS=json.dumps({"mini-a": self.stamp(side), "mini-b": self.stamp(newer),
                                                              "mini-light": self.stamp(self.old)}))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"mini-a: stamped {side} is not an ancestor of HEAD {self.head}", result.stderr)
        self.assertIn(f"mini-b: stamped {newer} is not an ancestor of HEAD {self.head}", result.stderr)
        self.assertNotIn("mini-light:", result.stderr, "an older stamp is an upgrade, not a refusal")
        self.assert_nothing_changed()

    def test_plan_reports_the_same_findings_and_does_not_fail(self) -> None:
        self.commit("local only")  # HEAD ahead of origin's main: not origin/main either
        result = self.fleet(FAKE_STAMPS=json.dumps({"mini-b": self.stamp("f" * 40)}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        guard = json.loads(result.stdout)["source"]
        self.assertEqual(guard["originMain"], self.head)
        texts = [r["text"] for r in guard["refusals"]]
        self.assertTrue(any("is not origin/main" in t for t in texts), texts)
        self.assertTrue(any(t.startswith("mini-b: stamped " + "f" * 40) for t in texts), texts)
        self.assertEqual(guard["hosts"]["mini-b"]["state"], "downgrade")
        human = self.fleet("--output", "human", FAKE_STAMPS=json.dumps({"mini-b": self.stamp("f" * 40)}))
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("would refuse --apply", human.stdout)
        self.assertIn("mini-b: stamped " + "f" * 40, human.stdout)

    def test_older_missing_and_unreadable_stamps_are_not_downgrades_but_say_so(self) -> None:
        stamps = json.dumps({"mini-a": self.stamp(self.old), "mini-b": "{not json"})
        result = self.fleet("--apply", FAKE_STAMPS=stamps)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        hosts = json.loads(result.stdout)["source"]["hosts"]
        self.assertEqual((hosts["mini-a"]["state"], hosts["mini-a"]["behind"]), ("behind", 1))
        self.assertEqual(hosts["mini-b"]["state"], "unreadable")
        self.assertEqual(hosts["mini-light"]["state"], "none")
        human = self.fleet("--output", "human", FAKE_STAMPS=stamps)
        self.assertIn("mini-light: no stamp; not counted as a downgrade", human.stdout)
        self.assertIn("mini-b: unreadable stamp", human.stdout)
        self.assertIn(f"mini-a: stamped {self.old[:12]}, 1 behind HEAD", human.stdout)

    def test_allow_downgrade_needs_a_reason_and_records_it_in_the_stamp(self) -> None:
        newer = "f" * 40
        stamps = json.dumps({"mini-a": self.stamp(newer)})
        blank = self.fleet("--apply", "--hosts", "mini-a", "--allow-downgrade", "  ", FAKE_STAMPS=stamps)
        self.assertEqual(blank.returncode, 2)
        self.assertIn("--allow-downgrade needs a reason", blank.stderr)
        self.assert_nothing_changed()
        result = self.fleet("--apply", "--hosts", "mini-a", "--allow-downgrade", "roll back the #1384 hook",
                            FAKE_STAMPS=stamps)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--allow-downgrade (roll back the #1384 hook) overrides", result.stderr)
        self.assertIn(f"mini-a: stamped {newer} is not an ancestor of HEAD {self.head}", result.stderr)
        stamp = json.loads((self.dir / "calls.jsonl.mini-a.stamp").read_text())
        self.assertEqual(stamp["source"], self.head)
        self.assertEqual(stamp["allowDowngrade"]["reason"], "roll back the #1384 hook")
        self.assertEqual(len(stamp["allowDowngrade"]["refusals"]), 1)

    def test_a_squash_merged_canary_rolls_forward_from_main(self) -> None:
        # canary from a PR branch: not origin/main, so it needs --allow-downgrade, which the stamp records
        self.git("checkout", "-q", "-b", "pr", self.head)
        branch = self.commit("pr change")
        canary = self.fleet("--apply", "--hosts", "mini-a", "--allow-downgrade", "canary of the PR branch")
        self.assertEqual(canary.returncode, 0, canary.stdout + canary.stderr)
        stamp = (self.dir / "calls.jsonl.mini-a.stamp").read_text()
        self.assertEqual(json.loads(stamp)["source"], branch)
        # the PR squash-merges: main gets a new commit, and the branch commit is never an ancestor of it
        self.git("checkout", "-q", "main")
        self.commit("pr change squashed")
        self.git("push", "-q", "origin", "main")
        stamps = json.dumps({"mini-a": stamp, "mini-b": self.stamp(self.old)})
        for where in ("present locally", "absent locally"):
            if where == "absent locally":
                self.git("branch", "-q", "-D", "pr")
                self.git("reflog", "expire", "--expire=now", "--all")
                self.git("gc", "-q", "--prune=now")
                missing = subprocess.run(["git", "-C", os.fspath(self.repo), "cat-file", "-e", branch + "^{commit}"],
                                         capture_output=True, check=False)
                self.assertNotEqual(missing.returncode, 0, "the branch commit is gone from this checkout")
            self.log.unlink(missing_ok=True)
            result = self.fleet("--apply", FAKE_STAMPS=stamps)
            self.assertEqual(result.returncode, 0, f"{where}: {result.stdout}{result.stderr}")
            hosts = json.loads(result.stdout)["source"]
            self.assertEqual(hosts["refusals"], [], where)
            self.assertEqual(hosts["hosts"]["mini-a"]["state"], "rolling_forward", where)
            human = self.fleet("--output", "human", FAKE_STAMPS=stamps)
            self.assertIn("mini-a: rolling forward from a deliberate off-main deploy (reason: canary of the PR branch)",
                          human.stdout, where)
        # a non-ancestor stamp without allowDowngrade still refuses the whole fleet, staging nothing
        self.log.unlink(missing_ok=True)
        refused = self.fleet("--apply", FAKE_STAMPS=json.dumps({"mini-a": stamp, "mini-b": self.stamp(branch)}))
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(f"mini-b: stamped {branch} is not an ancestor", refused.stderr)
        self.assert_nothing_changed()

    def test_a_deliberate_off_main_stamp_still_refuses_a_checkout_off_main(self) -> None:
        stamp = json.dumps({"by": "glaeda-cmux-runner-fleet", "date": "2026-09-30", "source": "e" * 40,
                            "allowDowngrade": {"reason": "canary", "refusals": []}})
        self.commit("local only")  # HEAD is not origin's main
        result = self.fleet("--apply", FAKE_STAMPS=json.dumps({"mini-a": stamp}))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("mini-a: stamped " + "e" * 40, result.stderr)
        self.assert_nothing_changed()

    def test_uncommitted_changes_to_staged_files_refuse_apply(self) -> None:
        hook = self.repo / "scripts" / "glaeda-cmux-runner-hook"
        hook.write_text(hook.read_text() + "\n# hand edit\n")
        result = self.fleet("--apply")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("uncommitted changes to staged files: scripts/glaeda-cmux-runner-hook", result.stderr)
        self.assert_nothing_changed()
        plan = self.fleet()
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertTrue(any("uncommitted" in r["text"] for r in json.loads(plan.stdout)["source"]["refusals"]))
        forced = self.fleet("--apply", "--hosts", "mini-a", "--allow-downgrade", "testing a hook edit on mini-a")
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)

    def test_the_stamp_records_the_head_the_guard_checked(self) -> None:
        # an untracked stamp beside the scripts used to win over HEAD
        (self.repo / "scripts" / ".glaeda-source.json").write_text(
            json.dumps({"by": "someone", "date": "2026-01-01", "source": "a" * 40}))
        result = self.fleet("--apply", "--hosts", "mini-a")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        stamp = json.loads((self.dir / "calls.jsonl.mini-a.stamp").read_text())
        self.assertEqual((stamp["source"], stamp["by"]), (self.head, "glaeda-cmux-runner-fleet"))

    def test_the_reason_loses_control_characters(self) -> None:
        result = self.fleet("--apply", "--hosts", "mini-a", "--allow-downgrade", "roll\x1b[31m back\x07 #1384\x7f",
                            FAKE_STAMPS=json.dumps({"mini-a": self.stamp("f" * 40)}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reason = json.loads((self.dir / "calls.jsonl.mini-a.stamp").read_text())["allowDowngrade"]["reason"]
        self.assertEqual(reason, "roll[31m back #1384")
        self.assertFalse(any(ord(ch) < 32 or ord(ch) == 127 for ch in result.stderr.replace("\n", "")))
        blank = self.fleet("--apply", "--allow-downgrade", "\x1b\x07")
        self.assertEqual(blank.returncode, 2)
        self.assertIn("--allow-downgrade needs a reason", blank.stderr)

    def test_files_git_is_told_to_ignore_refuse_apply(self) -> None:
        for flag, name in (("--assume-unchanged", "glaeda-cmux-runner-hook"),
                           ("--skip-worktree", "glaeda-cmux-runner-fleet")):
            self.git("update-index", flag, f"scripts/{name}")
            path = self.repo / "scripts" / name
            path.write_text(path.read_text() + "\n# hand edit git status cannot see\n")
            self.assertEqual(self.git("status", "--porcelain", "--", f"scripts/{name}"), "", "git status hides the edit")
            self.log.unlink(missing_ok=True)
            result = self.fleet("--apply")
            self.assertEqual(result.returncode, 2, f"{flag}: {result.stdout}{result.stderr}")
            self.assertIn(f"scripts/{name}", result.stderr)
            self.assertIn("assume-unchanged or skip-worktree", result.stderr)
            self.assert_nothing_changed()
        forced = self.fleet("--apply", "--hosts", "mini-a", "--allow-downgrade", "testing hidden edits on mini-a")
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)

    def test_a_failed_head_stamp_records_no_stamp_rather_than_one_the_guard_did_not_check(self) -> None:
        (self.repo / "scripts" / ".glaeda-source.json").write_text(
            json.dumps({"by": "someone", "date": "2026-01-01", "source": "a" * 40}))
        loader = importlib.machinery.SourceFileLoader("fleet_under_test", os.fspath(self.tool))
        spec = importlib.util.spec_from_loader("fleet_under_test", loader)
        fleet = importlib.util.module_from_spec(spec)
        loader.exec_module(fleet)
        with mock.patch.object(fleet, "head_stamp", return_value=None):
            self.assertIsNone(fleet.stamp_for(self.head), "a guarded run never falls back to a guess")
        self.assertEqual(fleet.stamp_for(self.head)["source"], self.head)
        # no git checkout (an OTA release copy, run with --allow-downgrade): the copy's own record is all there is
        self.assertEqual(fleet.stamp_for(None)["source"], "a" * 40)

    def test_apply_refuses_when_origin_main_cannot_be_read(self) -> None:
        self.git("remote", "set-url", "origin", os.fspath(self.dir / "gone.git"))
        result = self.fleet("--apply")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("cannot read origin's main", result.stderr)
        self.assert_nothing_changed()


if __name__ == "__main__":
    unittest.main()
