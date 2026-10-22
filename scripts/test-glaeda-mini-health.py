#!/usr/bin/env python3
"""Tests for scripts/glaeda-mini-health."""

from __future__ import annotations

import contextlib
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, os.fspath(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    sys.modules[name] = module
    loader.exec_module(module)
    return module


health = load("glaeda_mini_health", ROOT / "scripts" / "glaeda-mini-health")
NOW = 1_790_500_000.0
TM = health.TESTMANAGERD
ROUTES = json.loads((ROOT / "config" / "claude-routes.json").read_text())
SR2, POOL = ROUTES["routes"]


class FakeMini(health.Mini):
    """Every read and heal from fields; heals are recorded, never run."""

    def __init__(self) -> None:
        super().__init__(Path("/nonexistent"))
        self._hook = False  # no runner hook: the module's own patterns
        self.uid = 501
        self.console_state = ("unlocked", "cmux")
        self.lw: dict = {}
        self.pm = " displaysleep         0\n"
        self.procs = [(10, 501, "S", TM)]
        self.lines = [(NOW - 600, "Created session with socket 3"), (NOW - 590, "Tearing down <XCIDESession>")]
        self.ts: tuple[str, str | None, str] | None = ("app", "Running", "")
        self.route: str | None = "utun4"
        self.ts_after: list[str] = []
        self.agents = [("com.teamleaderleo.glaeda.cmux-runner", True, True, "(never exited)"),
                       ("com.teamleaderleo.glaeda.cmux-runner.1", True, True, "(never exited)")]
        self.recycler = lambda: "testmanagerd: stopped pid 10"
        self.did: list[str] = []
        self.leaks: list[tuple[int, str]] = []
        self.disk: dict = {}
        self.refusal: str | None = None
        self.toolchain_installs: list[str] = []
        self.claude: str | None = None  # no Claude Code here: no route to check
        self.claude_env: dict = {}
        self.answers: dict[str, bool] = {}  # route -> whether `claude -p` through it answers
        self.probe_memory: dict = {}
        self.fleet = True  # a fleet mini, not a personal Mac (--hygiene-only)
        # the tailscale_no_route restart's gates: an idle CI runner mini unless a test says otherwise
        self.names: list[str] | None = ["cmux9s-mac-mini"]
        self.opt_in: tuple[dict | None, str] = ({"schema": "glaeda-tailscale-self-heal/v1", "host": "cmux9s-mac-mini",
                                                 "source": "fleet-registry.json"}, "")
        self.restart_ran = True  # every restart command exited 0
        self.slept = 0.0
        self.facts = {"platform": "darwin", "runners": 2, "setup_args": ["--role=fleet-mini"], "role": "fleet-mini"}
        self.restart_off = ""
        self.busy = ""
        self.lock_why = ""
        self.jobs = ""
        self.sudo_allowed = False
        self.restart_state: Path | None = None  # the restart state file, read when the restart runs
        self.state_at_restart: list[dict] = []
        self.after_restart: tuple[str | None, str | None] = ("Running", "utun4")
        self.restart_cmds: list[list[str]] = []

    def host_names(self):
        return self.names

    def ci_mini_facts(self):
        return self.facts

    def tailscale_opt_in(self):
        return self.opt_in

    def tailscale_restart_off(self):
        return self.restart_off

    def host_busy(self):
        return self.busy

    @contextlib.contextmanager
    def hold_host_lock(self):
        if not self.lock_why:
            self.did.append("lock")
        yield self.lock_why

    def jobs_running(self):
        return self.jobs

    def sudo_allows(self, argv):
        return self.sudo_allowed

    def run_restart(self, commands):
        self.state_at_restart.append(health.load_json(self.restart_state) if self.restart_state else {})
        self.restart_cmds.extend(commands)
        self.did.append("restart " + self.ts[0])
        state, self.route = self.after_restart
        self.ts = (self.ts[0], state, "")
        return self.restart_ran, " ; ".join(" ".join(c) for c in commands)

    def console(self):
        return self.console_state

    def loginwindow(self):
        return self.lw

    def pmset(self):
        return self.pm

    def processes(self):
        return self.procs

    def elapsed_s(self, pid):
        return 3600

    def session_lines(self, pid, minutes):
        return self.lines if self.lines is not None else None

    def tailscale(self):
        if self.ts_after and self.did and self.did[-1].startswith(("up", "vpn")):
            self.ts = (self.ts[0], self.ts_after.pop(0), "")
        return self.ts

    def tailnet_route(self):
        return self.route

    def vpn_service(self):
        return "C7E1635B-DD04-49D8-A16F-FB4E34804764"

    def runner_agents(self):
        return self.agents

    def recycle_testmanagerd(self):
        self.did.append("recycle")
        return self.recycler() if self.recycler else None

    def tailscale_up(self):
        self.did.append("up")

    def restart_vpn(self, service):
        self.did.append("vpn " + service)

    def kickstart(self, label):
        self.did.append("kickstart " + label)
        self.agents = [(l, lo, True if l == label else r, x) for l, lo, r, x in self.agents]

    def sleep(self, seconds):
        self.slept += seconds

    def clock(self):
        return self.slept  # only differences are read: the time the sleeps took

    def leaked_test_apps(self):
        return self.leaks

    def kill_leaked_test_apps(self):
        self.did.append("kill leaked apps")
        self.leaks = []
        return ["killed pid 99"]

    def disk_summary(self):
        return self.disk

    def node_refusal(self):
        return self.refusal

    def install_rust_toolchain(self, toolchain):
        self.toolchain_installs.append(toolchain)
        return True, f"installed Rust toolchain {toolchain}"

    heal_status: dict | None = None
    sudo = False

    def daemon_heal_status(self):
        return self.heal_status

    def daemon_policy(self):
        return Path("/p/macos-daemons.json")

    def sudo_ok(self):
        return self.sudo

    def fleet_host(self):
        return self.fleet

    def claude_bin(self):
        return Path(self.claude) if self.claude else None

    def claude_routes(self):
        return ROUTES

    def claude_settings(self):
        return {"env": dict(self.claude_env)}

    def claude_probe(self, url, token):
        self.did.append("probe " + url)
        ok = self.answers.get(url, False)
        return ok, "PONG" if ok else "no answer in 60 s"

    def set_claude_route(self, url, token):
        self.did.append("route " + url)
        self.claude_env = {k: v for k, v in self.claude_env.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
        self.claude_env.update(ANTHROPIC_BASE_URL=url, ANTHROPIC_AUTH_TOKEN=token)
        return f"~/.claude/settings.json routes Claude to {url}"

    def claude_probe_last(self):
        return self.probe_memory

    def save_claude_probe(self, doc):
        self.probe_memory = dict(doc)

    def install_daemon_heal(self):
        self.did.append("install daemon-heal")
        self.heal_status = {**(self.heal_status or {}), "installed": True, "current": True, "loaded": True,
                            "reportFresh": True}
        return True, "sudo -n glaeda-daemon-heal --install --apply: applied"


class NodeEligibility(unittest.TestCase):
    """A node the runner hook finds ineligible refuses every job, so health says so instead of the jobs."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def mini(self, started: str) -> tuple[health.Mini, list[str]]:
        hooks = self.home / "actions-runner-glaeda" / "glaeda-hooks"
        hooks.mkdir(parents=True)
        (hooks / "job-started.sh").write_text("#!/bin/bash\n# generated\n" + started + "\n")
        asked: list[str] = []

        class Hook:
            @staticmethod
            def node_refusal(fleet_class):
                asked.append(fleet_class)
                return "node not eligible (no node status from generation 3809eed51fdd)", {}

        mini = health.Mini(self.home)
        mini._hook = Hook()
        return mini, asked

    def test_reads_the_fleet_class_the_job_started_hook_requires(self) -> None:
        mini, asked = self.mini("exec /usr/bin/python3 hook job-started --allowed-owner manaflow-ai "
                                "--require-eligible --fleet-class m4-16 --toolchain-xcode /Applications/Xcode.app")
        self.assertEqual(mini.node_refusal(), "node not eligible (no node status from generation 3809eed51fdd)")
        self.assertEqual(asked, ["m4-16"])

    def test_a_runner_that_does_not_require_eligibility_is_not_checked(self) -> None:
        mini, asked = self.mini("exec /usr/bin/python3 hook job-started --allowed-owner manaflow-ai")
        self.assertIsNone(mini.node_refusal())
        self.assertEqual(asked, [])


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state, self.report = self.dir / "state.json", self.dir / "health.json"
        self.mini = FakeMini()
        self.mini.restart_state = health.restart_state_path(self.state)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_once(self, at: float = NOW, apply: bool = True, heal_ok: bool = True) -> dict:
        return health.run(self.mini, apply, now=at, state_path=self.state, report_path=self.report, heal_ok=heal_ok)

    def ids(self, report: dict) -> list[str]:
        return [f["id"] for f in report["findings"]]


class Detection(Base):
    def test_healthy_mini_reports_nothing(self) -> None:
        report = self.run_once()
        self.assertEqual(report["findings"], [])
        self.assertEqual(json.loads(self.report.read_text())["schema"], "glaeda-mini-health/v1")

    def test_the_report_carries_what_the_updater_says_this_host_runs(self) -> None:
        doc = {"schema": "glaeda-ota-converge/v1", "at": "2026-09-25T00:00:00Z", "converged": False,
               "staleAfter": "2099-01-01T00:00:00Z", "alertAfter": "2000-01-01T00:00:00Z", "blocking": False,
               "since": "2026-09-24T00:00:00Z", "attempts": 4, "why": ["inconclusive: offline"],
               "running": {"tag": "r-20260924-aaaaaaaaaaaa", "tools": {"drift": []},
                           "hooks": [{"runnerDir": "/x/actions-runner-glaeda-1", "glaeda-cmux-runner-hook": "ab",
                                      "current": True}]},
               "wants": {"tag": "r-20260925-bbbbbbbbbbbb"}}
        self.mini.ota_converge = lambda: doc
        report = self.run_once()
        self.assertEqual(self.ids(report), ["ota_not_converging"])
        self.assertEqual(report["findings"][0]["evidence"], "running r-20260924-aaaaaaaaaaaa, wants "
                         "r-20260925-bbbbbbbbbbbb, because inconclusive: offline")
        self.assertEqual(report["ota"]["hooks"], [{"runner": "actions-runner-glaeda-1", "sha256": "ab", "current": True}])
        self.assertEqual(json.loads(self.report.read_text())["ota"]["wants"], "r-20260925-bbbbbbbbbbbb")
        doc["converged"] = True
        self.assertEqual(self.run_once()["findings"], [])

    def test_an_ineligible_node_is_an_error_this_user_cannot_heal(self) -> None:
        # cmux-austin-mini-0/-1 refused every job for days on 10-08..12: their staged generation was gone.
        self.mini.refusal = "node not eligible (no node status from generation 3809eed51fdd)"
        report = self.run_once()
        self.assertEqual(self.ids(report), ["node_ineligible"])
        f = report["findings"][0]
        self.assertEqual((f["severity"], f["auto_fix"]), ("error", "impossible"))
        self.assertIn("3809eed51fdd", f["evidence"])

    def test_missing_exact_rust_toolchain_is_self_healed(self) -> None:
        self.mini.refusal = "node not eligible (rust toolchain 1.98.1-aarch64-apple-darwin is not installed)"
        report = self.run_once()
        self.assertEqual(report["findings"], [])
        self.assertEqual(self.mini.toolchain_installs, ["1.98.1-aarch64-apple-darwin"])
        self.assertEqual(report["heals"][0]["id"], "node_toolchain_missing:1.98.1-aarch64-apple-darwin")

    def test_missing_toolchain_refusal_is_pending_when_install_fails(self) -> None:
        self.mini.refusal = "node not eligible (rust toolchain 1.98.1-aarch64-apple-darwin is not installed)"
        self.mini.install_rust_toolchain = lambda toolchain: (False, "network unavailable")
        report = self.run_once()
        self.assertEqual(self.ids(report), ["node_toolchain_missing:1.98.1-aarch64-apple-darwin"])
        f = report["findings"][0]
        self.assertEqual((f["severity"], f["auto_fix"]), ("error", "pending"))
        self.assertIn("network unavailable", f["note"])

    def test_locked_console_autologin_key_and_display_sleep_need_root(self) -> None:
        self.mini.console_state = ("locked", "cmux")
        self.mini.lw = {"autoLoginUserScreenLocked": True}
        self.mini.pm = " displaysleep         10\n"
        report = self.run_once()
        self.assertEqual(self.ids(report), ["console_locked", "autologin_locks", "display_sleep"])
        self.assertTrue(all(f["auto_fix"] == "impossible" for f in report["findings"]))
        self.assertEqual(report["findings"][0]["severity"], "error")
        self.assertIn("10 min", report["findings"][2]["evidence"])
        self.assertEqual(self.mini.did, [])

    def test_no_console_user(self) -> None:
        self.mini.console_state = ("no_user", None)
        self.assertEqual(self.ids(self.run_once()), ["console_no_user"])

    def test_since_is_the_first_sighting(self) -> None:
        self.mini.pm = " displaysleep         10\n"
        self.run_once(NOW)
        report = self.run_once(NOW + 120)
        self.assertEqual(report["findings"][0]["since"], health.iso(NOW))
        self.mini.pm = " displaysleep         0\n"
        self.run_once(NOW + 240)
        self.mini.pm = " displaysleep         10\n"
        self.assertEqual(self.run_once(NOW + 360)["findings"][0]["since"], health.iso(NOW + 360))

    def test_plan_mode_writes_and_heals_nothing(self) -> None:
        self.mini.lines.append((NOW - 300, health.WEDGE_SIGNATURE))
        report = self.run_once(apply=False)
        self.assertEqual(self.ids(report), ["testmanagerd_wedged"])
        self.assertEqual(self.mini.did, [])
        self.assertFalse(self.report.exists())


class DiskAndLeaks(Base):
    def test_stuck_eviction_is_reported_as_health_finding(self) -> None:
        self.mini.disk = {"schema": "glaeda-disk/v1", "at": NOW, "findings": [{
            "id": "eviction_stuck", "severity": "error", "path": "/Users/Shared/cmux-build-fleet",
            "evidence": "du timed out after 30s", "auto_fix": "pending", "count": 1
        }]}
        report = self.run_once()
        self.assertEqual(self.ids(report), ["eviction_stuck"])
        self.assertEqual(report["findings"][0]["severity"], "error")
        self.assertIn("du timed out", report["findings"][0]["evidence"])
        self.assertEqual(report["findings"][0]["observed_at"], NOW)

    def test_eviction_contention_is_reported_without_a_heal(self) -> None:
        self.mini.disk = {"schema": "glaeda-disk/v1", "at": NOW,
                          "eviction_contention": {"at_epoch": NOW, "owner": {
                              "pid": 42, "owner": "runner", "state": "live"}}}
        report = self.run_once()
        self.assertEqual(self.ids(report), ["eviction_contention"])
        finding = report["findings"][0]
        self.assertEqual(finding["auto_fix"], "impossible")
        self.assertIn("pid 42", finding["evidence"])
        self.assertEqual(self.mini.did, [])

    def test_stuck_evictions_are_one_bounded_aggregate(self) -> None:
        self.mini.disk = {"schema": "glaeda-disk/v1", "at": NOW, "findings": [
            {"id": "eviction_stuck", "severity": "error", "path": "/tmp/a", "count": 2,
             "evidence": "du timed out after 45s"},
            {"id": "eviction_stuck", "severity": "error", "path": "/tmp/b", "count": 3,
             "evidence": "du timed out after 45s"},
            {"id": "eviction_stuck", "severity": "error", "path": "/tmp/a", "count": 1,
             "evidence": "du timed out after 45s"},
        ]}
        report = self.run_once()
        self.assertEqual(self.ids(report), ["eviction_stuck"])
        stuck = report["findings"][0]
        self.assertEqual(stuck["timeout_count"], 6)
        self.assertEqual(stuck["path_count"], 2)
        self.assertEqual(stuck["paths"], ["/tmp/a", "/tmp/b"])
        self.assertEqual(stuck["observed_at"], NOW)
        self.assertIn("du timed out after 45s", stuck["evidence"])
        self.assertIn("6 path(s)", stuck["evidence"])

    def test_unmeasured_space_is_a_health_finding(self) -> None:
        self.mini.disk = {"schema": "glaeda-disk/v1", "accounting": [{"unmeasured": 5 * 1024**3,
                                                                   "top_level": [{"path": "/Users/Shared", "bytes": 8 * 1024**3}]}]}
        report = self.run_once()
        self.assertEqual(self.ids(report), ["disk_unmeasured"])
        self.assertIn("5.0 GiB", report["findings"][0]["evidence"])
        self.assertIn("/Users/Shared", report["findings"][0]["evidence"])

    def test_runner_profile_unmeasured_remainder_is_not_a_volume_finding(self) -> None:
        self.mini.disk = {"schema": "glaeda-disk/v1", "accounting": [{
            "scope": "runner-cache-profile", "unclassified": 300 * 1024**3,
            "measured": 2 * 1024**3, "reclaimable": 1 * 1024**3,
        }]}
        self.assertEqual(self.run_once()["findings"], [])

    def test_malformed_disk_accounting_is_ignored(self) -> None:
        self.mini.disk = {"schema": "glaeda-disk/v1", "accounting": [{"unmeasured": "unknown"},
                                                                       {"unmeasured": None}]}
        self.assertEqual(self.run_once()["findings"], [])

    def test_orphaned_apps_are_healed_by_the_backstop(self) -> None:
        self.mini.leaks = [(99, "/tmp/cmux DEV.app")]
        report = self.run_once()
        self.assertEqual(report["findings"], [])
        self.assertEqual(self.mini.did, ["kill leaked apps"])
        self.assertEqual(report["healed"][0]["code"], "leaked_test_apps")
        self.assertIn("killed pid 99", report["healed"][0]["action"])


class Testmanagerd(Base):
    def test_a_finished_teardown_is_healthy(self) -> None:
        self.mini.lines += [(NOW - 300, health.WEDGE_SIGNATURE), (NOW - 300, "Closing session <XCIDESession>"),
                            (NOW - 300, "Tearing down <XCIDESession>")]
        self.assertEqual(self.run_once()["findings"], [])

    def test_a_fresh_signature_is_not_yet_a_wedge(self) -> None:
        self.mini.lines.append((NOW - 5, health.WEDGE_SIGNATURE))
        self.assertEqual(self.run_once()["findings"], [])

    def test_wedge_is_recycled_while_no_test_runs(self) -> None:
        self.mini.lines.append((NOW - 300, health.WEDGE_SIGNATURE))
        report = self.run_once()
        self.assertEqual(self.mini.did, ["recycle"])
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["healed"][0]["code"], "testmanagerd_wedged")
        self.assertEqual(report["healed"][0]["auto_fix"], "done")
        self.assertIn("stopped pid 10", report["healed"][0]["action"])

    def test_a_running_test_keeps_the_last_verdict(self) -> None:
        self.mini.lines.append((NOW - 300, health.WEDGE_SIGNATURE))
        self.mini.recycler = None  # a hook without the recycler: reported, never healed here
        first = self.run_once()
        self.assertEqual(first["findings"][0]["auto_fix"], "pending")
        self.assertIn("glaeda#1281", first["findings"][0]["note"])
        self.mini.procs.append((20, 501, "S", "/usr/bin/xcodebuild test-without-building -xctestrun x"))
        again = self.run_once(NOW + 120)
        self.assertEqual(self.ids(again), ["testmanagerd_wedged"])
        self.assertEqual(again["findings"][0]["since"], health.iso(NOW))
        self.assertEqual(self.mini.did, ["recycle"], "no heal while a test runs")

    def test_a_test_running_past_the_heal_gate_never_uses_up_tries(self) -> None:
        self.mini.lines.append((NOW - 300, health.WEDGE_SIGNATURE))
        self.mini.recycler = lambda: "testmanagerd: kept (a test is running on this mini)"
        self.run_once()
        self.mini.procs.append((20, 501, "S", "/usr/bin/xcodebuild test -scheme cmux"))
        for i in range(1, 6):
            report = self.run_once(NOW + i * health.HEAL_EVERY_S)
        self.assertEqual(self.mini.did, ["recycle"])
        self.assertEqual(report["findings"][0]["auto_fix"], "pending")

    def test_an_unreadable_log_keeps_the_last_verdict(self) -> None:
        self.mini.lines.append((NOW - 300, health.WEDGE_SIGNATURE))
        self.mini.recycler = None
        self.run_once()
        self.mini.lines = None
        self.assertEqual(self.ids(self.run_once(NOW + 120)), ["testmanagerd_wedged"])

    def test_tries_run_out(self) -> None:
        self.mini.lines.append((NOW - 300, health.WEDGE_SIGNATURE))
        self.mini.recycler = lambda: "testmanagerd: stuck: pid 10 outlived SIGKILL"
        for i in range(health.HEAL_TRIES):
            report = self.run_once(NOW + i * health.HEAL_EVERY_S)
            self.assertEqual(report["findings"][0]["auto_fix"], "pending")
        report = self.run_once(NOW + health.HEAL_TRIES * health.HEAL_EVERY_S)
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible")
        self.assertEqual(self.mini.did.count("recycle"), health.HEAL_TRIES)

    def test_kill_switch_reports_without_healing(self) -> None:
        self.mini.lines.append((NOW - 300, health.WEDGE_SIGNATURE))
        report = self.run_once(heal_ok=False)
        self.assertEqual(self.mini.did, [])
        self.assertIn("healing is off", report["findings"][0]["note"])


class Tailscale(Base):
    def test_stopped_gets_up_on_the_second_run(self) -> None:
        self.mini.ts, self.mini.ts_after = ("app", "Stopped", ""), ["Running"]
        first = self.run_once()
        self.assertEqual(self.ids(first), ["tailscale_down"])
        self.assertEqual(self.mini.did, [], "one sighting never heals")
        second = self.run_once(NOW + 120)
        self.assertEqual(self.mini.did, ["up"])
        self.assertEqual(second["findings"], [])
        self.assertEqual(second["healed"][0]["action"], "tailscale up: Running")

    def test_stuck_starting_restarts_the_vpn_service(self) -> None:
        self.mini.ts, self.mini.ts_after = ("app", "Starting", ""), ["Running"]
        self.run_once()
        self.run_once(NOW + 120)
        self.assertEqual(self.mini.did, ["vpn C7E1635B-DD04-49D8-A16F-FB4E34804764"])

    def test_running_without_a_tunnel_route_off_a_ci_mini_is_reported_on_the_second_run_never_healed(self) -> None:
        # cmuxs-mac-mini-5, 2026-10-21: Running, but 100.64/10 gone, so tailnet ssh timed out all afternoon.
        self.mini.route = "en0"
        self.mini.names = ["cosmic-gray"]
        self.mini.opt_in = (None, "no ~/.config/glaeda/tailscale-self-heal.json")
        first = self.run_once()
        self.assertEqual(first["findings"], [], "one sighting can be a reconnect's gap")
        second = self.run_once(NOW + 120)
        self.assertEqual(self.ids(second), ["tailscale_no_route"])
        self.assertIn("(app)", second["findings"][0]["evidence"])
        self.assertIn("out of en0", second["findings"][0]["evidence"])
        self.assertIn("scutil --nc", second["findings"][0]["note"])
        self.assertEqual(second["findings"][0]["auto_fix"], "impossible")
        self.run_once(NOW + 240)
        self.assertEqual(self.mini.did, [], "only a CI runner mini restarts its own Tailscale")

    def test_a_route_gap_that_heals_by_the_next_run_is_never_reported(self) -> None:
        self.mini.route = "en0"
        self.assertEqual(self.run_once()["findings"], [])
        self.mini.route = "utun4"
        self.assertEqual(self.run_once(NOW + 120)["findings"], [])
        self.mini.route = "en0"
        self.assertEqual(self.run_once(NOW + 240)["findings"], [], "the count starts again")

    def test_a_standalone_tailscaled_without_a_route_names_its_own_fix(self) -> None:
        self.mini.ts, self.mini.route = ("standalone", "Running", ""), "en0"
        self.run_once()
        note = self.run_once(NOW + 120)["findings"][0]["note"]
        self.assertIn("kickstart -k system/com.tailscale.tailscaled", note)

    def test_by_hand_it_reports_at_once(self) -> None:
        self.mini.route = "en0"
        self.assertEqual(self.ids(self.run_once(apply=False)), ["tailscale_no_route"])

    def test_a_route_it_cannot_read_is_no_finding(self) -> None:
        self.mini.route = None
        self.assertEqual(self.run_once()["findings"], [])

    def test_a_runner_host_without_any_tailscale_cli_is_a_warning_not_healthy(self) -> None:
        self.mini.ts = None  # neither /usr/local/bin/tailscale nor the app bundle's CLI
        report = self.run_once()
        self.assertEqual(self.ids(report), ["tailscale_cli_missing"])
        f = report["findings"][0]
        self.assertEqual((f["severity"], f["auto_fix"]), ("warn", "impossible"))
        self.assertIn(health.TAILSCALE_STANDALONE, f["evidence"])
        self.assertIn(health.TAILSCALE_APP, f["evidence"])
        self.run_once(NOW + 120)
        self.assertEqual(self.mini.did, [])

    def test_a_host_without_runners_or_tailscale_reports_nothing(self) -> None:
        self.mini.ts, self.mini.agents = None, []
        self.assertEqual(self.run_once()["findings"], [])

    def test_no_backend_state_is_a_probe_error_never_a_heal(self) -> None:
        self.mini.ts = ("app", None, "Tailscale status --json exited 0 without a BackendState: The Tailscale GUI "
                        "failed to start")
        self.run_once()
        report = self.run_once(NOW + 120)
        self.assertEqual(self.ids(report), ["tailscale_probe_error"])
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible")
        self.assertIn("GUI failed to start", report["findings"][0]["evidence"])
        self.assertEqual(self.mini.did, [])

    def test_standalone_and_logged_out_are_reported_only(self) -> None:
        self.mini.ts = ("standalone", "Starting", "")
        self.assertEqual(self.run_once()["findings"][0]["auto_fix"], "impossible")
        self.mini.ts = ("app", "NeedsLogin", "")
        self.assertEqual(self.run_once(NOW + 120)["findings"][0]["auto_fix"], "impossible")
        self.run_once(NOW + 240)
        self.assertEqual(self.mini.did, [])


SERVICE = "C7E1635B-DD04-49D8-A16F-FB4E34804764"
SIX_H = 6 * 3600


class TailscaleRouteRestart(Base):
    """tailscale_no_route restarts Tailscale itself, only on an idle CI runner mini, at most once per 6 h."""

    def setUp(self) -> None:
        super().setUp()
        self.mini.route = "en0"

    def broken_twice(self, at: float = NOW, **kw) -> dict:
        self.run_once(at, **kw)
        return self.run_once(at + 120, **kw)

    def restarts(self) -> int:
        return sum(1 for d in self.mini.did if d.startswith("restart"))

    def test_an_idle_ci_mini_restarts_once_and_reports_it_healed(self) -> None:
        report = self.broken_twice()
        self.assertEqual(self.mini.did, ["lock", "restart app"])
        self.assertEqual(self.mini.restart_cmds, [["/usr/sbin/scutil", "--nc", "stop", SERVICE],
                                                  ["/usr/sbin/scutil", "--nc", "start", SERVICE]])
        self.assertEqual(report["findings"], [])
        self.assertEqual([h["id"] for h in report["heals"]], ["tailscale_no_route"])
        self.assertTrue(report["heals"][0]["ok"])
        healed = report["healed"][0]
        self.assertEqual((healed["code"], healed["auto_fix"]), ("tailscale_no_route", "done"))
        self.assertIn("restarted", healed["action"])
        self.assertIn("utun4", healed["action"])

    def test_the_attempt_is_written_before_the_restart_runs(self) -> None:
        self.broken_twice()
        self.assertEqual(len(self.mini.state_at_restart), 1)
        self.assertEqual(self.mini.state_at_restart[0]["last_attempt_at"], NOW + 120)
        doc = json.loads(self.mini.restart_state.read_text())
        self.assertEqual(doc["last_attempt_at"], NOW + 120)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["checked_at"], health.iso(NOW + 120 + health.TAILSCALE_RESTART_SETTLE_S),
                         "the re-check's time, after the settle wait")

    def test_an_unwritable_restart_state_is_no_restart_and_alerts(self) -> None:
        real = health.write_json

        def write_json(path, doc):
            if path == self.mini.restart_state:
                raise PermissionError(path)
            real(path, doc)

        self.addCleanup(setattr, health, "write_json", real)
        health.write_json = write_json
        report = self.broken_twice()
        self.assertEqual(self.restarts(), 0)
        f = report["findings"][0]
        self.assertEqual(f["auto_fix"], "impossible")
        self.assertIn("cannot write the restart state", f["note"])

    def test_a_bad_last_attempt_time_is_unreadable_state_never_a_crash(self) -> None:
        for at in (float("nan"), float("inf"), float("-inf"), 1e300, -1.0, NOW + 10 * SIX_H):
            for failures in (0, 1, 3):
                self.mini.restart_state.parent.mkdir(parents=True, exist_ok=True)
                self.mini.restart_state.write_text(json.dumps({"last_attempt_at": at, "ok": False,
                                                               "result": "still Running", "failures": failures}))
                report = self.broken_twice()
                self.assertEqual(self.restarts(), 0, (at, failures))
                f = report["findings"][0]
                self.assertEqual(f["auto_fix"], "impossible", (at, failures))
                self.assertIn("unreadable", f["note"], (at, failures))

    def test_still_broken_after_the_restart_is_reported_and_not_retried_within_6_h(self) -> None:
        self.mini.after_restart = ("Running", "en0")
        report = self.broken_twice()
        self.assertEqual(self.restarts(), 1)
        self.assertEqual(self.ids(report), ["tailscale_no_route"])
        self.assertFalse(report["heals"][0]["ok"])
        f = report["findings"][0]
        self.assertIn("still", f["note"])
        self.assertIn("en0", f["note"])
        self.assertIn(health.iso(NOW + 120 + SIX_H), f["note"], "names when the next try is allowed")
        self.assertEqual(f["auto_fix"], "impossible", "a restart that did not fix it alerts (ci-dash health_alerts)")
        for i in range(1, 20):  # every two minutes for the next 40 minutes, then hours later
            report = self.run_once(NOW + 120 + i * 120)
            self.assertEqual(report["findings"][0]["auto_fix"], "impossible", i)
        report = self.run_once(NOW + 120 + SIX_H - 60)
        self.assertEqual(self.restarts(), 1)
        f = report["findings"][0]
        self.assertEqual(f["auto_fix"], "impossible")
        self.assertIn(f"Tailscale restart at {health.iso(NOW + 120)} did not restore the route", f["note"])
        self.assertIn(f"next try after {health.iso(NOW + 120 + SIX_H)}", f["note"])

    def test_a_restart_command_that_failed_alerts(self) -> None:
        self.mini.restart_ran = False
        self.mini.after_restart = ("Running", "en0")
        report = self.broken_twice()
        self.assertEqual(self.restarts(), 1)
        f = report["findings"][0]
        self.assertEqual(f["auto_fix"], "impossible")
        self.assertIn("a command failed", f["note"])
        self.assertEqual(json.loads(self.mini.restart_state.read_text())["failures"], 1)

    def test_after_a_failed_restart_every_wait_still_alerts(self) -> None:
        self.mini.after_restart = ("Running", "en0")
        self.broken_twice()
        self.mini.busy = "a CI job is running (Runner.Worker)"  # the window is over, but the mini is busy
        report = self.run_once(NOW + 120 + SIX_H)
        self.assertEqual(self.restarts(), 1)
        f = report["findings"][0]
        self.assertEqual(f["auto_fix"], "impossible", "pending only while no restart has failed")
        self.assertIn("did not restore the route", f["note"])
        self.assertIn("Runner.Worker", f["note"])

    def test_a_crash_mid_restart_counts_as_a_failed_restart(self) -> None:
        self.mini.restart_state.parent.mkdir(parents=True, exist_ok=True)
        self.mini.restart_state.write_text(json.dumps({"schema": health.TAILSCALE_RESTART_SCHEMA,
                                                       "last_attempt_at": NOW - 60, "result": "started",
                                                       "ok": False, "failures": 1}))
        report = self.broken_twice()
        self.assertEqual(self.restarts(), 0)
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible")
        self.assertIn("did not restore the route", report["findings"][0]["note"])

    def test_three_failed_restarts_in_a_row_stop_restarting_until_the_route_is_seen_clear(self) -> None:
        self.mini.after_restart = ("Running", "en0")
        self.broken_twice()
        for i in range(1, 6):  # every 6 h: the second and third restarts, then no more
            report = self.run_once(NOW + 120 + i * SIX_H)
        self.assertEqual(self.restarts(), health.TAILSCALE_RESTART_GIVE_UP)
        self.assertEqual(json.loads(self.mini.restart_state.read_text())["failures"], 3)
        f = report["findings"][0]
        self.assertEqual(f["auto_fix"], "impossible")
        self.assertIn("3 Tailscale restarts in a row", f["note"])
        # the route comes back (an operator fixed it): the count resets, and a later break is restarted again
        self.mini.ts, self.mini.route = ("app", "Running", ""), "utun4"
        self.assertEqual(self.run_once(NOW + 120 + 6 * SIX_H)["findings"], [])
        self.assertEqual(json.loads(self.mini.restart_state.read_text())["failures"], 0)
        self.mini.route = "en0"
        self.broken_twice(NOW + 120 + 7 * SIX_H)
        self.assertEqual(self.restarts(), health.TAILSCALE_RESTART_GIVE_UP + 1)

    def test_a_restart_that_worked_resets_the_failure_count(self) -> None:
        self.mini.restart_state.parent.mkdir(parents=True, exist_ok=True)
        self.mini.restart_state.write_text(json.dumps({"last_attempt_at": NOW - 2 * SIX_H, "ok": False,
                                                       "result": "still Running, 100.100.100.100 via en0",
                                                       "failures": 2}))
        report = self.broken_twice()
        self.assertEqual(self.restarts(), 1)
        self.assertEqual(report["findings"], [])
        self.assertEqual(json.loads(self.mini.restart_state.read_text())["failures"], 0)

    def test_an_unreadable_failure_count_gives_up(self) -> None:
        self.mini.restart_state.parent.mkdir(parents=True, exist_ok=True)
        self.mini.restart_state.write_text(json.dumps({"last_attempt_at": NOW - 2 * SIX_H, "failures": "many"}))
        report = self.broken_twice()
        self.assertEqual(self.restarts(), 0)
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible")

    def test_a_second_sighting_within_6_h_does_not_restart_again(self) -> None:
        self.broken_twice()
        self.assertEqual(self.restarts(), 1)
        self.mini.ts, self.mini.route = ("app", "Running", ""), "en0"  # it broke again an hour later
        report = self.broken_twice(NOW + 3600)
        self.assertEqual(self.restarts(), 1)
        self.assertEqual(self.ids(report), ["tailscale_no_route"])
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible", "no restart is coming for hours: alert")
        self.assertIn("6 h", report["findings"][0]["note"])
        self.assertIn(f"next after {health.iso(NOW + 120 + SIX_H)}", report["findings"][0]["note"])

    def test_after_6_h_a_restart_is_allowed_again(self) -> None:
        self.mini.after_restart = ("Running", "en0")
        self.broken_twice()
        self.run_once(NOW + 120 + SIX_H)
        self.assertEqual(self.restarts(), 2)

    def test_a_busy_mini_is_not_restarted(self) -> None:
        self.mini.busy = "a CI job is running (/Users/cmux/actions-runner-glaeda/bin/Runner.Worker spawnclient 1 2)"
        report = self.broken_twice()
        self.assertEqual(self.mini.did, [])
        f = report["findings"][0]
        self.assertEqual(f["auto_fix"], "pending")
        self.assertIn("Runner.Worker", f["note"])
        self.assertFalse(self.mini.restart_state.exists(), "a refused attempt is no attempt")
        self.mini.busy = ""
        self.run_once(NOW + 240)
        self.assertEqual(self.restarts(), 1, "restarted once the mini went idle")

    def test_an_unreadable_busy_state_is_busy(self) -> None:
        self.mini.busy = "cannot list processes"
        self.broken_twice()
        self.assertEqual(self.mini.did, [])

    def test_a_wait_older_than_6_h_alerts(self) -> None:
        # a stranded host lock or a stuck jobs/ entry looks busy forever: past 6 h it is no short wait
        self.mini.busy = "a dev-build worker job is in /Users/Shared/cmux-build-fleet/jobs"
        report = self.broken_twice()
        self.assertEqual(report["findings"][0]["auto_fix"], "pending")
        report = self.run_once(NOW + SIX_H - 1)
        self.assertEqual(report["findings"][0]["auto_fix"], "pending")
        report = self.run_once(NOW + SIX_H)
        f = report["findings"][0]
        self.assertEqual(f["auto_fix"], "impossible")
        self.assertIn(f"waiting for an idle mini since {health.iso(NOW)}", f["note"])
        self.assertIn("jobs", f["note"])
        self.assertEqual(self.restarts(), 0)

    def test_a_held_lock_older_than_6_h_alerts(self) -> None:
        self.mini.lock_why = "a job or a fleet build holds the fleet host lock"
        self.broken_twice()
        report = self.run_once(NOW + SIX_H + 600)
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible")
        self.assertIn("host lock", report["findings"][0]["note"])

    def test_a_host_gate_that_cannot_answer_alerts(self) -> None:
        self.mini.busy = health.NoAnswer("glaeda-disk's host gate could not answer (RuntimeError)")
        report = self.broken_twice()
        self.assertEqual(self.mini.did, [])
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible")
        self.assertIn("could not answer", report["findings"][0]["note"])

    def test_a_held_host_lock_is_not_idle(self) -> None:
        self.mini.lock_why = "the fleet host lock is held"
        report = self.broken_twice()
        self.assertEqual(self.mini.did, [])
        self.assertIn("host lock", report["findings"][0]["note"])
        self.assertEqual(report["findings"][0]["auto_fix"], "pending", "a short wait")

    def test_no_fleet_dir_alerts(self) -> None:
        self.mini.lock_why = health.NoAnswer("no fleet dir /Users/Shared/cmux-build-fleet")
        report = self.broken_twice()
        self.assertEqual(self.mini.did, [])
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible")
        self.assertIn("no fleet dir", report["findings"][0]["note"])

    def test_job_patterns_that_cannot_be_read_alert(self) -> None:
        self.mini.jobs = health.NoAnswer("glaeda-disk's job patterns could not be read (AttributeError)")
        report = self.broken_twice()
        self.assertEqual(self.restarts(), 0)
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible")

    def test_a_job_that_started_before_the_lock_was_taken_stops_the_restart(self) -> None:
        self.mini.jobs = "a CI job is running (Runner.Worker)"
        self.broken_twice()
        self.assertEqual(self.mini.did, ["lock"])
        self.assertFalse(self.mini.restart_state.exists())

    def test_cmux_lawrence_is_never_restarted_even_opted_in(self) -> None:
        for names in (["cmux-lawrence"], ["Cmux Lawrence’s Mac mini"], ["cmux9s-mac-mini", "cmux-lawrences-mac-mini"]):
            self.mini.names = names
            self.mini.opt_in = ({"schema": health.TAILSCALE_OPT_IN_SCHEMA, "host": names[0]}, "")
            report = self.broken_twice()
            self.assertEqual(self.mini.did, [], names)
            self.assertEqual(report["findings"][0]["auto_fix"], "impossible")
            self.assertIn("never", report["findings"][0]["note"])

    def test_a_host_without_the_opt_in_is_never_restarted(self) -> None:
        # which hosts restart is declared in leo-workspace's fleet-registry.json and arrives as the opt-in file
        self.mini.opt_in = (None, "no ~/.config/glaeda/tailscale-self-heal.json")
        report = self.broken_twice()
        self.assertEqual(self.mini.did, [])
        f = report["findings"][0]
        self.assertEqual(f["auto_fix"], "impossible")
        self.assertIn("tailscale-self-heal.json", f["note"])

    def test_an_opt_in_for_another_host_or_of_another_schema_is_no_opt_in(self) -> None:
        for doc in ({"schema": health.TAILSCALE_OPT_IN_SCHEMA, "host": "cmuxs-mac-mini-5"},
                    {"schema": "glaeda-tailscale-self-heal/v2", "host": "cmux9s-mac-mini"},
                    {"host": "cmux9s-mac-mini"},
                    {"schema": health.TAILSCALE_OPT_IN_SCHEMA, "host": ["cmux9s-mac-mini"]},
                    {"schema": health.TAILSCALE_OPT_IN_SCHEMA, "host": ""}):
            self.mini.opt_in = (doc, "")
            report = self.broken_twice()
            self.assertEqual(self.mini.did, [], doc)
            self.assertEqual(report["findings"][0]["auto_fix"], "impossible", doc)

    def test_an_unreadable_tailnet_name_is_no_restart(self) -> None:
        self.mini.names = None
        report = self.broken_twice()
        self.assertEqual(self.mini.did, [])
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible")
        self.assertIn("tailnet name", report["findings"][0]["note"])

    def test_an_unknown_role_is_never_restarted(self) -> None:
        for facts in ({"platform": "darwin", "runners": 2, "setup_args": None, "role": "fleet-mini"},
                      {"platform": "darwin", "runners": 2, "setup_args": [], "role": None},
                      {"platform": "darwin", "runners": None, "setup_args": [], "role": "fleet-mini"}):
            self.mini.facts = facts
            self.broken_twice()
            self.assertEqual(self.mini.did, [], facts)
        self.mini.facts = self.mini.facts | {"runners": 2, "setup_args": [], "role": "fleet-mini"}
        self.mini.names = []
        self.broken_twice()
        self.assertEqual(self.mini.did, [], "a host that cannot be named is not restarted")

    def test_the_first_sighting_never_restarts(self) -> None:
        report = self.run_once()
        self.assertEqual(report["findings"], [])
        self.assertEqual(self.mini.did, [])

    def test_report_mode_never_restarts(self) -> None:
        report = self.run_once(apply=False)
        self.assertEqual(self.ids(report), ["tailscale_no_route"])
        self.assertEqual(report["findings"][0]["auto_fix"], "pending", "the LaunchAgent's --apply run restarts it")
        self.run_once(NOW + 120, apply=False)
        self.assertEqual(self.mini.did, [])
        self.assertFalse(self.mini.restart_state.exists())

    def test_the_kill_switch_stops_the_restart(self) -> None:
        self.mini.restart_off = "GLAEDA_TAILSCALE_RESTART=off"
        report = self.broken_twice()
        self.assertEqual(self.mini.did, [])
        self.assertIn("GLAEDA_TAILSCALE_RESTART=off", report["findings"][0]["note"])
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible", "no restart is coming: alert")

    def test_mini_health_s_own_kill_switch_stops_it_too(self) -> None:
        report = self.broken_twice(heal_ok=False)
        self.assertEqual(self.mini.did, [])
        self.assertIn("healing is off", report["findings"][0]["note"])
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible", "no restart is coming: alert")

    def test_an_unreadable_restart_state_is_no_restart(self) -> None:
        self.mini.restart_state.parent.mkdir(parents=True, exist_ok=True)
        self.mini.restart_state.write_text("{not json")
        report = self.broken_twice()
        self.assertEqual(self.mini.did, [])
        self.assertIn("unreadable", report["findings"][0]["note"])

    def test_a_standalone_tailscaled_is_kickstarted_with_its_sudo_rule(self) -> None:
        self.mini.ts = ("standalone", "Running", "")
        self.mini.sudo_allowed = True
        self.broken_twice()
        self.assertEqual(self.mini.restart_cmds, [["/usr/bin/sudo", "-n", "/bin/launchctl", "kickstart", "-k",
                                                   "system/com.tailscale.tailscaled"]])

    def test_a_standalone_tailscaled_without_the_sudo_rule_is_reported_only(self) -> None:
        self.mini.ts = ("standalone", "Running", "")
        report = self.broken_twice()
        self.assertEqual(self.mini.did, [])
        f = report["findings"][0]
        self.assertEqual(f["auto_fix"], "impossible")
        self.assertIn("NOPASSWD", f["note"])

    def test_an_app_without_a_vpn_service_is_reported_only(self) -> None:
        self.mini.vpn_service = lambda: None
        report = self.broken_twice()
        self.assertEqual(self.mini.did, [])
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible")


class TailscaleRestartGates(unittest.TestCase):
    """The restart's gates as pure functions, and the real Mini's reads against stand-ins (never the host)."""

    CI = {"platform": "darwin", "runners": 3, "setup_args": ["--role=fleet-mini"], "role": "fleet-mini"}

    def test_ci_mini_refusal(self) -> None:
        self.assertEqual(health.ci_mini_refusal(["cmux9s-mac-mini"], self.CI), "")
        self.assertEqual(health.ci_mini_refusal(["cmuxs-Mac-mini-5", "cmuxs-mac-mini-5"], self.CI), "")
        self.assertIn("tailnet name", health.ci_mini_refusal(None, self.CI))
        refused = {
            "no names": ([], self.CI),
            "tailnet name unreadable": (None, self.CI),
            "lawrence": (["cmux-lawrence"], self.CI),
            "lawrence word": (["Cmux Lawrence's Mac mini"], self.CI),
            "linux": (["cmux9s-mac-mini"], self.CI | {"platform": "linux"}),
            "no runner": (["cmux9s-mac-mini"], self.CI | {"runners": 0}),
            "runners unreadable": (["cmux9s-mac-mini"], self.CI | {"runners": None}),
            "setup unreadable": (["cmux9s-mac-mini"], self.CI | {"setup_args": None}),
            "hygiene": (["cmux9s-mac-mini"], self.CI | {"setup_args": ["--hygiene-only"]}),
            "gh only": (["cmux9s-mac-mini"], self.CI | {"setup_args": ["--gh-only"]}),
            "headless": (["cmux9s-mac-mini"], self.CI | {"setup_args": ["--role=fleet-mini", "--headless"]}),
            "devbox": (["cmux9s-mac-mini"], self.CI | {"setup_args": ["--role=devbox"], "role": "devbox"}),
            "args say devbox": (["cmux9s-mac-mini"], self.CI | {"setup_args": ["--role=devbox"]}),
            "role unknown": (["cmux9s-mac-mini"], self.CI | {"role": None}),
        }
        for what, (names, facts) in refused.items():
            self.assertNotEqual(health.ci_mini_refusal(names, facts), "", what)

    def test_opt_in_refusal(self) -> None:
        doc = {"schema": "glaeda-tailscale-self-heal/v1", "host": "cmuxs-mac-mini-5", "source": "fleet-registry.json"}
        names = ["cmuxs-Mac-mini-5", "cmuxs-mac-mini-5.local", "cmuxs-mac-mini-5"]
        self.assertEqual(health.opt_in_refusal((doc, ""), names), "")
        self.assertEqual(health.opt_in_refusal(({**doc, "host": "CMUXS-MAC-MINI-5"}, ""), names), "")
        refused = {
            "missing": ((None, "no ~/.config/glaeda/tailscale-self-heal.json"), names),
            "another host": (({**doc, "host": "cmux9s-mac-mini"}, ""), names),
            "no schema": (({"host": "cmuxs-mac-mini-5"}, ""), names),
            "another schema": (({**doc, "schema": "glaeda-tailscale-self-heal/v0"}, ""), names),
            "host not a string": (({**doc, "host": {"name": "cmuxs-mac-mini-5"}}, ""), names),
            "empty host": (({**doc, "host": " "}, ""), names),
            "no names": ((doc, ""), []),
        }
        for what, (opt_in, host_names) in refused.items():
            self.assertNotEqual(health.opt_in_refusal(opt_in, host_names), "", what)
        self.assertIn("no ~/.config", health.opt_in_refusal((None, "no ~/.config/glaeda/tailscale-self-heal.json"), names))

    def test_sudo_nopasswd(self) -> None:
        kick = "/bin/launchctl kickstart -k system/com.tailscale.tailscaled"
        head = ("Matching Defaults entries for cmux on cmuxs-mac-mini-5:\n    env_reset, env_keep+=BLOCKSIZE\n\n"
                "User cmux may run the following commands on cmuxs-mac-mini-5:\n")
        allowed = {
            "exact": f"    (root) NOPASSWD: {kick}\n",
            "ALL under NOPASSWD": "    (ALL) NOPASSWD: ALL\n",
            "ALL : ALL runas": f"    (ALL : ALL) NOPASSWD: {kick}\n",
            "listed among others": f"    (root) NOPASSWD: /usr/bin/true, {kick}\n",
            "the later rule wins": f"    (ALL) ALL\n    (root) NOPASSWD: {kick}\n",
            "tags carry": f"    (root) NOPASSWD: SETENV: /usr/bin/true, {kick}\n",
        }
        for what, rules in allowed.items():
            self.assertTrue(health.sudo_nopasswd(head + rules, kick), what)
        refused = {
            "password needed": "    (ALL) ALL\n",
            "a later password rule wins": f"    (root) NOPASSWD: {kick}\n    (ALL) ALL\n",
            "another command": "    (root) NOPASSWD: /bin/launchctl kickstart -k system/com.example\n",
            "another runas user": f"    (_tailscale) NOPASSWD: {kick}\n",
            "PASSWD after NOPASSWD": f"    (root) NOPASSWD: /usr/bin/true, PASSWD: {kick}\n",
            "negated": f"    (root) NOPASSWD: ALL, !{kick}\n",
            "a prefix only": "    (root) NOPASSWD: /bin/launchctl kickstart\n",
        }
        for what, rules in refused.items():
            self.assertFalse(health.sudo_nopasswd(head + rules, kick), what)
        self.assertFalse(health.sudo_nopasswd("", kick))
        self.assertFalse(health.sudo_nopasswd(f"    (root) NOPASSWD: {kick}\n", kick), "no rules header: no answer")

    def test_restart_window(self) -> None:
        self.assertEqual(health.restart_window_refusal({}, NOW), "")
        self.assertEqual(health.restart_window_refusal({"last_attempt_at": NOW - SIX_H}, NOW), "")
        self.assertIn("6 h", health.restart_window_refusal({"last_attempt_at": NOW - SIX_H + 1}, NOW))
        self.assertNotEqual(health.restart_window_refusal(None, NOW), "", "unreadable state fails closed")
        self.assertNotEqual(health.restart_window_refusal({"last_attempt_at": "x"}, NOW), "")
        self.assertNotEqual(health.restart_window_refusal({"last_attempt_at": NOW + 600}, NOW), "",
                            "a stamp from the future (a clock step) still counts")
        for at in (float("nan"), float("inf"), float("-inf"), 1e300, -1.0, NOW + 10 * SIX_H, True):
            self.assertIn("unreadable", health.restart_window_refusal({"last_attempt_at": at}, NOW), at)
            self.assertIsNone(health.restart_failures({"last_attempt_at": at, "failures": 0}, NOW), at)
            self.assertIn("did not restore", health.restart_failed_note({"last_attempt_at": at}, 1, NOW))

    def test_load_restart_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            self.assertEqual(health.load_restart_state(path), {}, "never attempted")
            path.write_text("[1]")
            self.assertIsNone(health.load_restart_state(path))
            path.write_text("garbage")
            self.assertIsNone(health.load_restart_state(path))
            path.write_text(json.dumps({"last_attempt_at": NOW}))
            self.assertEqual(health.load_restart_state(path)["last_attempt_at"], NOW)
            path.unlink()
            path.mkdir()
            self.assertIsNone(health.load_restart_state(path))

    def test_restart_switch(self) -> None:
        self.assertEqual(health.restart_switch_off({}, False), "")
        self.assertEqual(health.restart_switch_off({"GLAEDA_TAILSCALE_RESTART": "on"}, False), "")
        for value in ("0", "off", "OFF", "false", "no"):
            self.assertIn("GLAEDA_TAILSCALE_RESTART", health.restart_switch_off({"GLAEDA_TAILSCALE_RESTART": value}, False))
        self.assertIn("tailscale-restart-off", health.restart_switch_off({}, True))

    def test_restart_commands_per_flavor(self) -> None:
        self.assertEqual(health.restart_commands("standalone", None),
                         [["/usr/bin/sudo", "-n", "/bin/launchctl", "kickstart", "-k", "system/com.tailscale.tailscaled"]])
        self.assertEqual(health.restart_commands("app", SERVICE),
                         [["/usr/sbin/scutil", "--nc", "stop", SERVICE], ["/usr/sbin/scutil", "--nc", "start", SERVICE]])
        self.assertEqual(health.restart_commands("app", None), [])
        for argv in health.restart_commands("standalone", None) + health.restart_commands("app", SERVICE):
            self.assertFalse({"up", "down", "set", "login", "logout", "--reset"} & set(argv), "restart only")

    def test_route_ok(self) -> None:
        self.assertTrue(health.route_ok("Running", "utun3"))
        self.assertFalse(health.route_ok("Running", "en0"))
        self.assertFalse(health.route_ok("Running", None))
        self.assertFalse(health.route_ok("Starting", "utun3"))
        self.assertFalse(health.route_ok(None, "utun3"))


class TailscaleRestartReads(unittest.TestCase):
    """The real Mini's gate reads, against a temporary home, fleet dir and glaeda-disk stand-in."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.home = self.dir / "home"
        self.home.mkdir()
        saved = health.FLEET_DIR
        self.addCleanup(setattr, health, "FLEET_DIR", saved)
        health.FLEET_DIR = self.dir / "fleet"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def disk(self, body: str) -> health.Mini:
        path = self.dir / "glaeda-disk"
        path.write_text(body + "\ndef host_busy_unreadable(reason):\n    return reason == 'cannot list processes'\n")
        mini = health.Mini(self.home)
        mini.disk_path = lambda: path
        return mini

    def test_host_busy_is_glaeda_disk_s(self) -> None:
        mini = self.disk("def host_busy(fleet=None, now=None):\n    return 'a CI job is running (x)'\n")
        self.assertEqual(mini.host_busy(), "a CI job is running (x)")
        mini = self.disk("def host_busy(fleet=None, now=None):\n    return ''\n")
        self.assertEqual(mini.host_busy(), "")

    def test_host_busy_fails_closed(self) -> None:
        self.assertNotEqual(self.disk("raise RuntimeError('broken')\n").host_busy(), "")
        self.assertNotEqual(self.disk("def host_busy(fleet=None, now=None):\n    raise OSError('x')\n").host_busy(), "")
        self.assertNotEqual(self.disk("X = 1\n").host_busy(), "", "a glaeda-disk without host_busy")
        mini = health.Mini(self.home)
        mini.disk_path = lambda: self.dir / "missing"
        self.assertNotEqual(mini.host_busy(), "")
        self.assertIsInstance(mini.host_busy(), health.NoAnswer, "no answer, not a short wait")
        self.assertIsInstance(self.disk("X = 1\n").host_busy(), health.NoAnswer)
        busy = self.disk("def host_busy(fleet=None, now=None):\n    return 'a CI job is running (x)'\n").host_busy()
        self.assertNotIsInstance(busy, health.NoAnswer)
        (self.dir / "old-disk").write_text("def host_busy(fleet=None, now=None):\n    return 'the host lock is unreadable'\n")
        mini = health.Mini(self.home)
        mini.disk_path = lambda: self.dir / "old-disk"
        self.assertIsInstance(mini.host_busy(), health.NoAnswer, "a glaeda-disk without host_busy_unreadable")

    def test_the_real_glaeda_disk_loads(self) -> None:
        # its dataclasses need it in sys.modules while it loads; without that every restart read "could not answer"
        disk = health.Mini(self.home).disk_module()
        self.assertTrue(callable(disk.host_busy) and callable(disk.host_busy_unreadable))

    def glaeda_disk_says(self, reason: str) -> str:
        """Mini.host_busy over the real glaeda-disk, its host_busy answering reason."""
        mini = health.Mini(self.home)
        disk = mini.disk_module()
        disk.host_busy = lambda fleet=None, now=None: reason
        return mini.host_busy()

    def test_glaeda_disk_cannot_list_processes_is_no_answer(self) -> None:
        self.assertIsInstance(self.glaeda_disk_says("cannot list processes"), health.NoAnswer)

    def test_glaeda_disk_cannot_read_jobs_is_no_answer(self) -> None:
        self.assertIsInstance(self.glaeda_disk_says("cannot read /Users/Shared/cmux-build-fleet/jobs"), health.NoAnswer)

    def test_glaeda_disk_unreadable_reservation_is_no_answer(self) -> None:
        self.assertIsInstance(self.glaeda_disk_says("the host reservation is unreadable"), health.NoAnswer)

    def test_glaeda_disk_unreadable_host_lock_is_no_answer(self) -> None:
        self.assertIsInstance(self.glaeda_disk_says("the host lock is unreadable"), health.NoAnswer)

    def test_glaeda_disk_busy_reasons_are_a_wait(self) -> None:
        for reason in ("a CI job is running (Runner.Worker)", "a build is running (xcodebuild)", "the host is reserved",
                       "a dev-build worker job is in /Users/Shared/cmux-build-fleet/jobs",
                       "a fleet build holds the host lock"):
            got = self.glaeda_disk_says(reason)
            self.assertEqual(got, reason)
            self.assertNotIsInstance(got, health.NoAnswer, reason)

    def test_the_host_lock_is_taken_exclusively_and_refused_while_a_job_holds_it(self) -> None:
        mini = health.Mini(self.home)
        with mini.hold_host_lock() as why:
            self.assertIn("no fleet dir", why, "fails closed without the fleet dir jobs lock")
            self.assertIsInstance(why, health.NoAnswer)
        health.FLEET_DIR.mkdir()
        with mini.hold_host_lock() as why:
            self.assertEqual(why, "")
            fd = os.open(health.FLEET_DIR / "host.lock", os.O_RDONLY)
            with self.assertRaises(OSError, msg="a job's shared take fails while it is held"):
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            os.close(fd)
        job = os.open(health.FLEET_DIR / "host.lock", os.O_RDONLY)
        fcntl.flock(job, fcntl.LOCK_SH | fcntl.LOCK_NB)  # a PR job's holder
        try:
            with mini.hold_host_lock() as why:
                self.assertIn("host lock", why)
                self.assertNotIsInstance(why, health.NoAnswer, "held is a short wait")
        finally:
            os.close(job)

    def test_jobs_running_uses_glaeda_disk_s_patterns(self) -> None:
        mini = self.disk("import re\nCI_WORKER = re.compile(r'Runner\\.Worker')\n"
                         "BUILD_PROCESS = re.compile(r'xcodebuild')\n")
        mini.processes = lambda: [(1, 501, "S", "/bin/zsh")]
        self.assertEqual(mini.jobs_running(), "")
        mini.processes = lambda: [(1, 501, "S", "/x/actions-runner-glaeda/bin/Runner.Worker spawnclient")]
        self.assertIn("Runner.Worker", mini.jobs_running())
        mini.processes = lambda: []
        self.assertNotEqual(mini.jobs_running(), "", "an empty process list is unreadable")

    def test_ci_mini_facts(self) -> None:
        mini = health.Mini(self.home)
        facts = mini.ci_mini_facts()
        self.assertEqual((facts["runners"], facts["setup_args"], facts["role"]), (0, None, None))
        runner = self.home / "actions-runner-glaeda-1"
        (runner / "glaeda-hooks").mkdir(parents=True)
        (runner / ".runner").write_text("{}")
        (self.home / "actions-runner-glaeda" / "glaeda-hooks").mkdir(parents=True)  # not registered
        (self.home / ".config/glaeda").mkdir(parents=True)
        (self.home / ".config/glaeda/update.json").write_text(json.dumps({"setupArgs": ["--role=fleet-mini"]}))
        (self.home / ".local/state/glaeda/mini-setup").mkdir(parents=True)
        (self.home / ".local/state/glaeda/mini-setup/receipt.json").write_text(json.dumps({"role": "fleet-mini"}))
        facts = mini.ci_mini_facts()
        self.assertEqual((facts["runners"], facts["setup_args"], facts["role"]), (1, ["--role=fleet-mini"], "fleet-mini"))
        (self.home / ".local/state/glaeda/mini-setup/receipt.json").write_text(json.dumps({"profile": "build-host"}))
        self.assertEqual(mini.ci_mini_facts()["role"], "fleet-mini", "mini-setup's own reading of an older receipt")
        (self.home / ".config/glaeda/update.json").write_text("{bad")
        self.assertIsNone(mini.ci_mini_facts()["setup_args"])
        for receipt in ({"role": ["fleet-mini"]}, {"role": {"fleet-mini": 1}, "profile": "build-host"}):
            (self.home / ".local/state/glaeda/mini-setup/receipt.json").write_text(json.dumps(receipt))
            self.assertIsNone(mini.ci_mini_facts()["role"], f"{receipt} is no role, and no crash")

    def test_the_opt_in_file(self) -> None:
        mini = health.Mini(self.home)
        doc, why = mini.tailscale_opt_in()
        self.assertIsNone(doc)
        self.assertIn("tailscale-self-heal.json", why)
        path = self.home / ".config/glaeda/tailscale-self-heal.json"
        path.parent.mkdir(parents=True)
        for body in ("{not json", "[1]", ""):
            path.write_text(body)
            doc, why = mini.tailscale_opt_in()
            self.assertIsNone(doc, body)
            self.assertIn("unreadable", why, body)
        want = {"schema": "glaeda-tailscale-self-heal/v1", "host": "cmuxs-mac-mini-5", "source": "fleet-registry.json"}
        path.write_text(json.dumps(want))
        self.assertEqual(mini.tailscale_opt_in(), (want, ""))

    def test_sudo_allows_reads_the_rules_not_the_exit_code(self) -> None:
        # `sudo -n -l <cmd>` exits 0 for a rule that still asks for a password; the listing says which it is
        mini = health.Mini(self.home)
        argv = health.restart_commands("standalone", None)[0]
        kick = " ".join(argv[2:])
        calls = []

        def fake_sh(cmd, timeout=15, env=None):
            calls.append(cmd)
            return self.sudo

        saved = health.sh
        self.addCleanup(setattr, health, "sh", saved)
        health.sh = fake_sh
        header = "User cmux may run the following commands on cmux9s-mac-mini:\n"
        self.sudo = (0, header + "    (ALL) ALL\n")
        self.assertFalse(mini.sudo_allows(argv))
        self.sudo = (0, header + f"    (root) NOPASSWD: {kick}\n")
        self.assertTrue(mini.sudo_allows(argv))
        self.sudo = (1, header + f"    (root) NOPASSWD: {kick}\n")
        self.assertFalse(mini.sudo_allows(argv), "a listing sudo refused is no answer")
        self.assertTrue(all(c[:3] == ["/usr/bin/sudo", "-n", "-l"] for c in calls), calls)

    def test_the_switch_reads_the_environment_and_the_file(self) -> None:
        mini = health.Mini(self.home)
        saved = os.environ.pop("GLAEDA_TAILSCALE_RESTART", None)
        self.addCleanup(lambda: saved is None or os.environ.__setitem__("GLAEDA_TAILSCALE_RESTART", saved))
        switch = self.home / ".config/glaeda/tailscale-restart-off"
        prior = health.TAILSCALE_RESTART_OFF
        self.addCleanup(setattr, health, "TAILSCALE_RESTART_OFF", prior)
        health.TAILSCALE_RESTART_OFF = switch
        self.assertEqual(mini.tailscale_restart_off(), "")
        switch.parent.mkdir(parents=True)
        switch.touch()
        self.assertNotEqual(mini.tailscale_restart_off(), "")


class TailnetNames(unittest.TestCase):
    """Mini.tailnet_names and host_names against a stand-in Tailscale CLI: no Self name is no answer."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.saved = (health.TAILSCALE_APP, health.TAILSCALE_STANDALONE)
        health.TAILSCALE_APP, health.TAILSCALE_STANDALONE = str(self.dir / "app"), str(self.dir / "standalone")

    def tearDown(self) -> None:
        health.TAILSCALE_APP, health.TAILSCALE_STANDALONE = self.saved
        self.tmp.cleanup()

    def cli(self, body: str) -> None:
        Path(health.TAILSCALE_APP).write_text("#!/bin/sh\n" + body + "\n")
        os.chmod(health.TAILSCALE_APP, 0o755)

    def test_the_self_names(self) -> None:
        self.cli("echo '{\"Self\": {\"HostName\": \"cmuxs-Mac-mini-5\", \"DNSName\": \"cmuxs-mac-mini-5.tail1.ts.net.\"}}'")
        self.assertEqual(health.Mini(self.dir).tailnet_names(), ["cmuxs-Mac-mini-5", "cmuxs-mac-mini-5"])

    def test_no_self_name_is_no_answer(self) -> None:
        for body in ("exit 1", "echo 'not json'", "echo '{}'", "echo '{\"Self\": []}'",
                     "echo '{\"Self\": {\"HostName\": \"\", \"DNSName\": \"\"}}'",
                     "echo '{\"Self\": {\"HostName\": \"cmux9s-mac-mini\"}}'; exit 1"):
            self.cli(body)
            self.assertIsNone(health.Mini(self.dir).tailnet_names(), body)
        os.unlink(health.TAILSCALE_APP)
        self.assertIsNone(health.Mini(self.dir).tailnet_names(), "no CLI")

    def test_host_names_refuses_without_the_tailnet_name(self) -> None:
        mini = health.Mini(self.dir)
        mini.tailnet_names = lambda: None
        self.assertIsNone(mini.host_names())
        mini.tailnet_names = lambda: ["cmuxs-mac-mini-5"]
        self.assertIn("cmuxs-mac-mini-5", mini.host_names())


class TailscaleCli(unittest.TestCase):
    """Mini.tailscale against a stand-in CLI: which binary it runs, with what environment, and what it makes of it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.saved = (health.TAILSCALE_APP, health.TAILSCALE_STANDALONE)
        health.TAILSCALE_APP, health.TAILSCALE_STANDALONE = str(self.dir / "app"), str(self.dir / "standalone")

    def tearDown(self) -> None:
        health.TAILSCALE_APP, health.TAILSCALE_STANDALONE = self.saved
        self.tmp.cleanup()

    def cli(self, path: str, body: str) -> None:
        Path(path).write_text("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def test_app_cli_runs_as_the_cli_when_no_standalone(self) -> None:
        # like the app binary: the status only with TAILSCALE_BE_CLI (or a terminal), else it starts as the GUI
        self.cli(health.TAILSCALE_APP, 'if [ "$TAILSCALE_BE_CLI" = 1 ]; then echo \'{"BackendState": "Running"}\'; '
                 'else echo "The Tailscale GUI failed to start"; fi')
        self.assertEqual(health.Mini().tailscale(), ("app", "Running", ""))

    def test_a_real_standalone_cli_wins(self) -> None:
        self.cli(health.TAILSCALE_APP, "echo '{\"BackendState\": \"Running\"}'")
        self.cli(health.TAILSCALE_STANDALONE, "echo '{\"BackendState\": \"Stopped\"}'")
        self.assertEqual(health.Mini().tailscale(), ("standalone", "Stopped", ""))

    def test_a_link_into_the_app_is_the_app(self) -> None:
        self.cli(health.TAILSCALE_APP, "echo '{\"BackendState\": \"Running\"}'")
        os.symlink(self.dir / "Tailscale.app", health.TAILSCALE_STANDALONE)  # dangling, as a broken "Install CLI"
        self.assertEqual(health.Mini().tailscale(), ("app", "Running", ""))

    def test_no_cli_is_no_tailscale(self) -> None:
        self.assertIsNone(health.Mini().tailscale())

    def test_an_error_without_json_is_a_probe_error(self) -> None:
        self.cli(health.TAILSCALE_APP, "echo 'failed to connect to local Tailscale service' >&2; exit 1")
        flavor, state, said = health.Mini().tailscale()
        self.assertEqual((flavor, state), ("app", None))
        self.assertIn("exited 1", said)
        self.assertIn("failed to connect", said)


class Runners(Base):
    def test_stopped_agent_is_kickstarted_after_two_runs(self) -> None:
        self.mini.agents[1] = ("com.teamleaderleo.glaeda.cmux-runner.1", True, False, "0")
        first = self.run_once()
        self.assertEqual(self.ids(first), ["runner_stopped:1"])
        self.assertIn("last exit code 0", first["findings"][0]["evidence"])
        second = self.run_once(NOW + 120)
        self.assertEqual(self.mini.did, ["kickstart com.teamleaderleo.glaeda.cmux-runner.1"])
        self.assertEqual(second["findings"], [])

    def test_a_runner_that_keeps_exiting_stops_being_kickstarted(self) -> None:
        stopped = ("com.teamleaderleo.glaeda.cmux-runner.1", True, False, "0")
        t = NOW
        for _ in range(12):
            self.mini.agents[1] = stopped  # it exits cleanly again after every kickstart
            report = self.run_once(t)
            t += health.HEAL_EVERY_S
        self.assertEqual(self.mini.did.count("kickstart com.teamleaderleo.glaeda.cmux-runner.1"), health.HEAL_TRIES)
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible")

    def test_a_held_runner_is_not_a_finding(self) -> None:
        held = self.dir / "held"
        (held / "actions-runner-glaeda-1").parent.mkdir(parents=True)
        (held / "actions-runner-glaeda-1").touch()
        saved, health.HELD = health.HELD, held
        self.addCleanup(setattr, health, "HELD", saved)
        self.mini.agents[1] = ("com.teamleaderleo.glaeda.cmux-runner.1", False, False, "?")
        self.assertEqual(self.run_once()["findings"], [])

    def test_the_first_runner_agent_is_zero(self) -> None:
        self.mini.agents[0] = ("com.teamleaderleo.glaeda.cmux-runner", True, False, "0")
        self.run_once()
        self.run_once(NOW + 120)
        self.assertEqual(self.mini.did, ["kickstart com.teamleaderleo.glaeda.cmux-runner"])

    def test_unloaded_agent_is_left_to_an_operator(self) -> None:
        self.mini.agents[0] = ("com.teamleaderleo.glaeda.cmux-runner", False, False, "?")
        self.run_once()
        report = self.run_once(NOW + 120)
        self.assertEqual(self.ids(report), ["runner_unloaded:0"])
        self.assertEqual(report["findings"][0]["auto_fix"], "impossible")
        self.assertEqual(self.mini.did, [])


class DaemonHeal(Base):
    """The root daemon-heal LaunchDaemon is kept installed; its hot daemons show up in the report."""

    def test_no_healer_on_this_host_is_no_finding(self) -> None:
        self.mini.heal_status = None
        self.assertEqual(self.ids(self.run_once()), [])

    def test_stale_healer_is_reinstalled_where_sudo_n_works(self) -> None:
        self.mini.sudo = True
        self.mini.heal_status = {"installed": False, "current": False, "stale": ["/x"], "report": "/none"}
        r = self.run_once()
        self.assertEqual(self.mini.did, ["install daemon-heal"])
        self.assertEqual(self.ids(r), [])
        self.assertEqual([h["id"] for h in r["healed"]], ["daemon_heal_stale"])

    def test_stale_healer_without_sudo_names_the_root_command(self) -> None:
        self.mini.heal_status = {"installed": True, "current": False, "stale": ["/x"], "report": "/none"}
        r = self.run_once()
        f = r["findings"][0]
        self.assertEqual((f["id"], f["auto_fix"]), ("daemon_heal_stale", "impossible"))
        self.assertIn("--install --policy /p/macos-daemons.json --apply", f["note"])
        self.assertEqual(self.mini.did, [])

    def test_hot_daemons_from_the_root_report(self) -> None:
        root = self.dir / "report.json"
        root.write_text(json.dumps({"hot": [{"name": "fseventsd", "pid": 421, "cores": 1.02, "since": "x",
                                             "hot_for_s": 5400, "waiting": "restarts at 2 h"}],
                                    "healed": [{"name": "audiomxd", "t": NOW - 60, "at": "a", "since": "s",
                                                "cores": 1.1, "action": "audiomxd pid 5 stopped (SIGTERM)"}]}))
        self.mini.heal_status = {"installed": True, "current": True, "loaded": True, "reportFresh": True,
                                 "report": os.fspath(root)}
        r = self.run_once()
        f = r["findings"]
        self.assertEqual([x["id"] for x in f], ["daemon_hot:fseventsd"])
        self.assertEqual([(h["id"], h["auto_fix"]) for h in r["healed"]], [("daemon_hot:audiomxd", "done")])
        self.assertIn("1.5 h", f[0]["evidence"])
        self.assertIn("restarts at 2 h", f[0]["note"])


class DaemonHealLiveness(Base):
    """An installed copy that is unloaded, or whose passes stopped, is no healthy healer."""

    def status(self, **kw) -> dict:
        return {"installed": True, "current": True, "loaded": True, "reportFresh": True, "report": "/none", **kw}

    def test_unloaded_healer_is_stale(self) -> None:
        self.mini.heal_status = self.status(loaded=False)
        f = self.run_once()["findings"][0]
        self.assertEqual(f["id"], "daemon_heal_stale")
        self.assertIn("not loaded", f["evidence"])

    def test_old_report_means_passes_stopped(self) -> None:
        self.mini.heal_status = self.status(reportFresh=False, reportAgeS=900)
        self.assertIn("900 s old", self.run_once()["findings"][0]["evidence"])

    def test_healthy_healer_is_no_finding(self) -> None:
        self.mini.heal_status = self.status()
        self.assertEqual(self.ids(self.run_once()), [])

    def test_operator_command_pins_release_hashes(self) -> None:
        self.mini.heal_status = self.status(loaded=False)
        self.assertIn("--expect-sha256", self.run_once()["findings"][0]["note"])


class ClaudeRoute(Base):
    """Claude Code on a fleet mini goes through a fleet route that answers, or capture-host admits no lease here."""

    def setUp(self) -> None:
        super().setUp()
        self.mini.claude = "/Users/cmux/.local/bin/claude"
        self.mini.claude_env = {"ANTHROPIC_BASE_URL": SR2, "ANTHROPIC_AUTH_TOKEN": "subrouter"}
        self.mini.answers = {SR2: True, POOL: True}

    def claude(self, report: dict) -> list[dict]:
        return [f for f in report["findings"] if f["code"] == "claude_route"]

    def test_the_config_names_routes_and_a_placeholder(self) -> None:
        self.assertEqual(ROUTES["schema"], "glaeda-claude-routes/v1")
        self.assertTrue(all(r.startswith("http://") for r in ROUTES["routes"]))
        self.assertTrue(ROUTES["placeholder"])
        self.assertGreaterEqual(ROUTES["probe_every_s"], 600)

    def test_a_mini_without_claude_is_not_checked(self) -> None:
        self.mini.claude = None
        self.mini.claude_env = {}
        self.assertEqual(self.claude(self.run_once()), [])
        self.assertEqual(self.mini.did, [])

    def test_a_route_that_answers_is_probed_once_per_window(self) -> None:
        every = ROUTES["probe_every_s"]
        self.assertEqual(self.claude(self.run_once()), [])
        self.assertEqual(self.claude(self.run_once(at=NOW + 120)), [])
        self.assertEqual(self.mini.did, ["probe " + SR2])
        self.assertEqual(self.claude(self.run_once(at=NOW + every + 1)), [])
        self.assertEqual(self.mini.did, ["probe " + SR2] * 2)

    def test_a_personal_login_is_rerouted_on_the_first_run(self) -> None:
        self.mini.claude_env = {}  # ~/.claude/.credentials.json only: it expires (cmux-mac-mini, 2026-10-21)
        report = self.run_once()
        self.assertEqual(self.claude(report), [])
        self.assertEqual(self.mini.did, ["probe " + SR2, "route " + SR2])
        self.assertEqual(self.mini.claude_env, {"ANTHROPIC_BASE_URL": SR2, "ANTHROPIC_AUTH_TOKEN": "subrouter"})
        self.assertEqual([h["code"] for h in report["healed"]], ["claude_route"])

    def test_check_only_reports_a_personal_login_and_changes_nothing(self) -> None:
        self.mini.claude_env = {}
        found = self.claude(self.run_once(apply=False))
        self.assertEqual([(f["severity"], f["auto_fix"]) for f in found], [("error", "pending")])
        self.assertIn("personal login", found[0]["evidence"])
        self.assertEqual(self.mini.did, [])

    def test_check_only_probes_but_keeps_nothing(self) -> None:
        self.assertEqual(self.claude(self.run_once(apply=False)), [])
        self.assertEqual(self.mini.did, ["probe " + SR2])
        self.assertEqual(self.mini.probe_memory, {})

    def test_a_route_that_hangs_fails_over_to_one_that_answers(self) -> None:
        self.mini.claude_env = {"ANTHROPIC_BASE_URL": POOL, "ANTHROPIC_AUTH_TOKEN": "subrouter"}
        self.mini.answers = {SR2: True, POOL: False}  # :31415 hung cmux11s and cmux7s, 2026-10-21
        self.assertEqual(self.claude(self.run_once()), [])
        self.assertEqual(self.mini.did, ["probe " + POOL, "probe " + SR2, "route " + SR2])
        self.assertEqual((self.mini.probe_memory["route"], self.mini.probe_memory["ok"]), (SR2, True),
                         "the heal's own probe stands for the window")
        self.assertEqual(self.claude(self.run_once(at=NOW + 120)), [])

    def test_a_route_that_answers_again_during_the_heal_is_kept(self) -> None:
        self.mini.answers = {SR2: False, POOL: False}
        self.run_once(heal_ok=False)
        self.mini.answers = {SR2: True, POOL: False}  # a hiccup: the route in use answers again
        self.assertEqual(self.claude(self.run_once(at=NOW + health.HEAL_EVERY_S)), [])
        self.assertEqual(self.mini.did[-3:], ["probe " + POOL, "probe " + SR2, "route " + SR2])
        self.assertEqual((self.mini.probe_memory["route"], self.mini.probe_memory["ok"]), (SR2, True),
                         "the old failure no longer stands for the window")

    def test_a_tunnel_off_its_route_holds_the_claude_reroute(self) -> None:
        # tailscale_no_route: no fleet route over the tailnet answers, but rerouting Claude would only churn
        self.mini.route = "en0"
        self.mini.opt_in = (None, "no ~/.config/glaeda/tailscale-self-heal.json")  # no restart: only the hold
        self.mini.answers = {SR2: False, POOL: False}
        for at in (NOW, NOW + 120):
            found = self.claude(self.run_once(at=at))
            self.assertEqual(len(found), 1)
            self.assertIn("tunnel is down", found[0]["note"])
        self.assertFalse([d for d in self.mini.did if d.startswith("route ")], "no reroute on a broken tunnel")
        self.assertEqual(self.mini.probe_memory, {}, "a probe through a broken tunnel stands for nothing")
        self.mini.route = "utun4"  # the tunnel is back: the route in use answers again and is kept
        self.mini.answers = {SR2: True, POOL: True}
        self.assertEqual(self.claude(self.run_once(at=NOW + 240)), [])
        self.assertEqual(self.mini.claude_env["ANTHROPIC_BASE_URL"], SR2)

    def test_a_tunnel_that_is_down_is_never_probed_through(self) -> None:
        # a `claude -p` through a broken tunnel hangs up to 60 s every 2-minute run and delays the heals after it
        self.mini.route = "en0"
        self.mini.opt_in = (None, "no ~/.config/glaeda/tailscale-self-heal.json")
        for at in (NOW, NOW + 120):
            found = self.claude(self.run_once(at=at))
            self.assertEqual([f["evidence"] for f in found], ["not probed: the Tailscale tunnel is down"],
                             "the finding stays, so capture-host stays closed")
        self.assertFalse([d for d in self.mini.did if d.startswith("probe ")], "zero claude_probe calls")
        self.assertEqual(self.mini.probe_memory, {}, "nothing written to the probe cache")
        self.mini.route = "utun4"
        self.assertEqual(self.claude(self.run_once(at=NOW + 240)), [])
        self.assertEqual(self.mini.did, ["probe " + SR2], "the first run after recovery probes at once")

    def test_a_fresh_cached_answer_stands_while_the_tunnel_is_down(self) -> None:
        self.run_once()
        self.assertEqual(self.mini.did, ["probe " + SR2])
        self.mini.route = "en0"
        self.mini.opt_in = (None, "no ~/.config/glaeda/tailscale-self-heal.json")
        self.assertEqual(self.claude(self.run_once(at=NOW + 120)), [])
        self.assertEqual(self.mini.did, ["probe " + SR2], "the window's answer stands; no new probe")

    def test_a_tailscale_restart_and_a_claude_reroute_never_share_a_run(self) -> None:
        self.mini.route = "en0"
        self.mini.answers = {SR2: False, POOL: False}
        self.run_once()
        report = self.run_once(at=NOW + 120)  # the second sighting restarts Tailscale, which fixes the route
        self.assertEqual([h["id"] for h in report["heals"]], ["tailscale_no_route"])
        self.assertFalse([d for d in self.mini.did if d.startswith("route ")])
        self.mini.answers = {SR2: True, POOL: True}
        self.assertEqual(self.claude(self.run_once(at=NOW + 240)), [], "re-probed once the tunnel is back")
        self.assertEqual(self.mini.claude_env["ANTHROPIC_BASE_URL"], SR2)

    def test_the_other_routes_are_tried_before_the_one_that_just_failed(self) -> None:
        self.mini.answers = {SR2: False, POOL: True}
        self.assertEqual(self.claude(self.run_once()), [])
        self.assertEqual(self.mini.did, ["probe " + SR2, "probe " + POOL, "route " + POOL])

    def test_a_personal_mac_is_never_checked_or_rerouted(self) -> None:
        self.mini.fleet = False  # mini-setup --hygiene-only: Leo's Macs, a teammate's laptop
        self.mini.claude_env = {}
        self.assertEqual(self.claude(self.run_once()), [])
        self.assertEqual(self.mini.did, [])
        self.assertEqual(self.mini.claude_env, {})

    def test_the_token_is_kept_on_a_fleet_route_and_else_the_placeholder(self) -> None:
        self.mini.answers = {SR2: False, POOL: True}
        self.mini.claude_env = {"ANTHROPIC_BASE_URL": SR2, "ANTHROPIC_AUTH_TOKEN": "fleet-token"}
        self.run_once()
        self.assertEqual(self.mini.claude_env, {"ANTHROPIC_BASE_URL": POOL, "ANTHROPIC_AUTH_TOKEN": "fleet-token"})
        for n, env in enumerate(({"ANTHROPIC_BASE_URL": "http://elsewhere:1", "ANTHROPIC_AUTH_TOKEN": "meant-for-elsewhere"},
                                 {"ANTHROPIC_BASE_URL": "http://elsewhere:1"}), 1):
            with self.subTest(env=env):
                self.mini.claude_env = dict(env)
                self.run_once(at=NOW + 3600 * n)
                self.assertEqual(self.mini.claude_env["ANTHROPIC_AUTH_TOKEN"], ROUTES["placeholder"])

    def test_a_settings_finding_reroutes_in_the_config_order(self) -> None:
        self.mini.claude_env = {"ANTHROPIC_BASE_URL": SR2}  # no placeholder: the route itself did not fail
        self.run_once()
        self.assertEqual(self.mini.did, ["probe " + SR2, "route " + SR2], "never drifts to a later route")

    def test_a_route_outside_the_config_or_a_personal_token_is_a_finding(self) -> None:
        for env, why in (({"ANTHROPIC_BASE_URL": "http://elsewhere:1", "ANTHROPIC_AUTH_TOKEN": "x"}, "elsewhere"),
                         ({"ANTHROPIC_BASE_URL": SR2}, "no ANTHROPIC_AUTH_TOKEN"),
                         ({"ANTHROPIC_BASE_URL": SR2, "ANTHROPIC_AUTH_TOKEN": "x",
                           "CLAUDE_CODE_OAUTH_TOKEN": "sk-personal-value"}, "CLAUDE_CODE_OAUTH_TOKEN")):
            with self.subTest(why=why):
                self.mini.claude_env = env
                found = self.claude(self.run_once(apply=False))
                self.assertEqual(len(found), 1)
                self.assertIn(why, found[0]["evidence"])
                self.assertNotIn("sk-personal-value", json.dumps(found), "a token value is never reported")

    def test_when_no_route_answers_the_finding_stays_and_the_heals_stop(self) -> None:
        self.mini.answers = {SR2: False, POOL: False}
        at = NOW
        for n in range(1, 4):
            found = self.claude(self.run_once(at=at))
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["severity"], "error")
            at += health.HEAL_EVERY_S
        self.assertEqual(found[0]["auto_fix"], "pending")
        self.assertIn("no fleet route answered", found[0]["note"])
        found = self.claude(self.run_once(at=at))
        self.assertEqual(found[0]["auto_fix"], "impossible")
        on_disk = [f for f in json.loads(self.report.read_text())["findings"] if f["code"] == "claude_route"]
        self.assertEqual(len(on_disk), 1, "capture-host reads health.json")

    def test_a_failed_probe_is_remembered_within_the_window(self) -> None:
        self.mini.answers = {SR2: False, POOL: False}
        self.run_once(heal_ok=False)
        self.run_once(at=NOW + 120, heal_ok=False)
        self.assertEqual(self.mini.did, ["probe " + SR2], "a dead route costs one hung probe per window, not per run")
        self.mini.answers = {SR2: True}
        self.assertEqual(self.claude(self.run_once(at=NOW + ROUTES["probe_every_s"] + 1, heal_ok=False)), [])

    def test_a_host_without_runners_checks_only_claude(self) -> None:
        self.mini.console_state = ("locked", "cmux")  # a runner finding: not this host's
        self.mini.claude_env = {}
        self.mini.answers = {SR2: False, POOL: False}
        report = health.run(self.mini, True, now=NOW, state_path=self.state, report_path=self.report, runner=False)
        self.assertEqual([f["code"] for f in report["findings"]], ["claude_route"])


class ClaudeRouteOnDisk(unittest.TestCase):
    """The real reads and writes: settings.json keeps everything else, the probe never sees a personal token."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.mini = health.Mini(self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_set_route_keeps_the_rest_of_the_settings_and_a_backup(self) -> None:
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir()
        before = {"hooks": {"Stop": []}, "env": {"FOO": "1", "CLAUDE_CODE_OAUTH_TOKEN": "personal"}}
        settings.write_text(json.dumps(before))
        what = self.mini.set_claude_route(SR2, "subrouter")
        after = json.loads(settings.read_text())
        self.assertEqual(after["hooks"], {"Stop": []})
        self.assertEqual(after["env"], {"FOO": "1", "ANTHROPIC_BASE_URL": SR2, "ANTHROPIC_AUTH_TOKEN": "subrouter"})
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        backups = list(settings.parent.glob("settings.json.bak-mini-health-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600, "the backup can hold a token")
        self.assertEqual(json.loads(backups[0].read_text()), before)
        self.assertIn(SR2, what)
        self.assertNotIn("personal", what)

    def test_only_a_fleet_mini_counts_as_one(self) -> None:
        conf = self.home / ".config/glaeda/update.json"
        self.assertFalse(self.mini.fleet_host(), "no update.json: glaeda-update's default is --hygiene-only")
        conf.parent.mkdir(parents=True)
        for args, fleet in ((["--hygiene-only"], False), ([], True), (["--role=builder"], True)):
            conf.write_text(json.dumps({"setupArgs": args}))
            self.assertEqual(self.mini.fleet_host(), fleet, args)

    def test_set_route_writes_settings_where_there_were_none(self) -> None:
        self.mini.set_claude_route(SR2, "subrouter")
        self.assertEqual(json.loads((self.home / ".claude/settings.json").read_text())["env"]["ANTHROPIC_BASE_URL"], SR2)

    def fake_claude(self, body: str) -> Path:
        path = self.home / ".local/bin/claude"
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return path

    def test_the_probe_routes_through_settings_and_drops_a_personal_token(self) -> None:
        seen = self.home / "seen"
        self.fake_claude(f'printf "%s\\n" "$@" > {seen}; env >> {seen}; echo PONG\n')
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "personal"
        try:
            ok, detail = self.mini.claude_probe(SR2, "subrouter")
        finally:
            del os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        self.assertEqual((ok, detail), (True, "PONG"))
        text = seen.read_text()
        self.assertIn(json.dumps({"env": {"ANTHROPIC_BASE_URL": SR2, "ANTHROPIC_AUTH_TOKEN": "subrouter"}}), text)
        self.assertNotIn("personal", text)

    def test_a_probe_that_hangs_or_fails_is_a_failure(self) -> None:
        self.fake_claude("sleep 30\n")
        orig = health.CLAUDE_PROBE_S
        health.CLAUDE_PROBE_S = 1
        try:
            self.assertEqual(self.mini.claude_probe(SR2, "subrouter"), (False, "no answer in 1 s"))
        finally:
            health.CLAUDE_PROBE_S = orig
        (self.home / ".local/bin/claude").write_text("#!/bin/sh\necho 'API Error: 401 OAuth session expired'\nexit 1\n")
        ok, detail = self.mini.claude_probe(SR2, "subrouter")
        self.assertFalse(ok)
        self.assertIn("exit 1", detail)
        self.assertIn("401", detail)

    def test_no_claude_binary_means_no_check(self) -> None:
        self.assertIsNone(self.mini.claude_bin())


class PersonalMac(unittest.TestCase):
    def status_with(self, update: dict | None) -> object:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            if update is not None:
                (home / ".config/glaeda").mkdir(parents=True)
                (home / ".config/glaeda/update.json").write_text(json.dumps(update))
            mini = health.Mini(home)
            mini.daemon_policy = lambda: health.SCRIPT_DIR.parent / "config/macos-daemons.json"
            calls = []
            orig = health.sh
            health.sh = lambda argv, **kw: (calls.append(argv), (1, ""))[1]
            try:
                mini.daemon_heal_status()
            finally:
                health.sh = orig
            return calls

    def test_missing_update_config_or_setup_args_is_hygiene_only_like_glaeda_update(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("the daemon healer is macOS-only")
        self.assertEqual(self.status_with(None), [])
        self.assertEqual(self.status_with({"ring": "stable"}), [])
        self.assertEqual(self.status_with({"setupArgs": ["--hygiene-only"]}), [])
        self.assertEqual(len(self.status_with({"setupArgs": []})), 1, "a fleet host asks the healer")


class Parsing(unittest.TestCase):
    def test_no_runner_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(health.Mini(Path(home)).runner_dirs(), [])

    def test_xctest_pattern(self) -> None:
        for command in ("/usr/bin/xcodebuild test -scheme cmux", "/Applications/Xcode.app/x/usr/bin/xctest a.xctest"):
            self.assertTrue(health.XCTEST_RUNNING.search(command), command)
        self.assertFalse(health.XCTEST_RUNNING.search("/usr/bin/xcodebuild build -scheme cmux"))


if __name__ == "__main__":
    unittest.main()
