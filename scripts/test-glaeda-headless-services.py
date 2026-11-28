#!/usr/bin/env python3
"""Lint: a fleet service that must outlive its login session is never installed as a LaunchAgent.

2026-11-20: cmux-lawrence rebooted, its user's login session ended, and every LaunchAgent of that user stopped with it:
the fleet-cas store, seed-archive and more (fleet cache hit rate 0.7% against 75.7%). A store or server role is a
system LaunchDaemon (/Library/LaunchDaemons, UserName=the user, `launchctl bootstrap system`). Two checks:

1. HEADLESS_LABELS (the store/server roles) are never bootstrapped into a gui/ domain by any script, and the
   installers that write them (glaeda-fleet-cas, glaeda-seed-archive) write only /Library/LaunchDaemons plists.
   `bootout gui/...` stays allowed: it is how install migrates the old agent.
2. Every non-test script that mentions Library/LaunchAgents or a gui/ domain is in AGENT_WRITERS with the reason it
   needs a GUI session or is not a server. A new installer fails here until someone decides which it is.

Add a new store or server role to HEADLESS_LABELS; add a new agent writer to AGENT_WRITERS with its reason.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

HEADLESS_LABELS = (
    "com.teamleaderleo.glaeda.fleet-cas-store",
    "com.teamleaderleo.glaeda.fleet-cas-store-gc",
    "com.teamleaderleo.glaeda.seed-archive",
)

RUNNER = "CI-mini runner/hygiene/health agents: the minis run a logged-in GUI session on purpose (Xcode UI jobs); headless minis use glaeda-mini-setup --headless"
AGENT_WRITERS = {
    "cmux_mini_fix.sh": RUNNER,
    "cmux_mini_preflight.sh": RUNNER,
    "cmux_mini_probe.sh": "reads agent state on a mini; writes none",
    "glaeda-cmux-runner": RUNNER,
    "glaeda-cmux-runner-fleet": RUNNER,
    "glaeda-cmux-runner-hook": RUNNER,
    "glaeda-daemon-heal": "installs a root LaunchDaemon itself; mentions agents only to retire them",
    "glaeda-disk": RUNNER + " (disk-pressure agent)",
    "glaeda-fleet-cas": "node, writer, relay and prewarm roles on CI minis (the relay needs the GUI session's Local Network "
                        "privacy); the store and its gc are daemons (checked below)",
    "glaeda-fleet-status": "reads agent/daemon state on minis",
    "glaeda-gh": "gh-watch daemon: has its own system-daemon / logged-out handling (restart picks the domain)",
    "glaeda-mini-fleet": "reads and plans for minis",
    "glaeda-mini-health": RUNNER,
    "glaeda-linux-vm-personal": "a person's own Lima VM on their own Mac: starts with their login (RunAtLoad); not a fleet store or server",
    "glaeda-mini-setup": RUNNER + "; --headless installs daemons",
    "glaeda-projects": "per-user workspace agents",
    "glaeda-seed-archive": "only to boot out and remove the old agent (install migrates it to a daemon)",
    "glaeda-seed-lan": "seed-prefetch client agent on the PR minis; the serving side is not an agent",
    "glaeda-update": "updater: system daemon on --headless hosts, agent elsewhere (launchd_job)",
    "glaeda_fleet_labels.py": "label tables",
    "quarry-trusted-runner.sh": RUNNER,
}
MENTION = re.compile(r"Library/LaunchAgents|gui/|bootstrap\s+.?gui")


def scripts():
    for path in sorted(SCRIPTS.iterdir()):
        if path.is_file() and not path.name.startswith("test-") and path.suffix in ("", ".sh", ".py"):
            yield path


class HeadlessServices(unittest.TestCase):
    def test_every_agent_writer_has_a_reason(self):
        unknown = [p.name for p in scripts() if MENTION.search(p.read_text(errors="replace")) and p.name not in AGENT_WRITERS]
        self.assertEqual(unknown, [], "a new LaunchAgent writer: if it is a store or server role it must be a system "
                         "LaunchDaemon (see glaeda-fleet-cas install_daemon); else add it to AGENT_WRITERS with why")

    def test_headless_labels_are_never_bootstrapped_into_gui(self):
        bad = []
        for path in scripts():
            lines = path.read_text(errors="replace").splitlines()
            for number, line in enumerate(lines, 1):
                if any(label in line for label in HEADLESS_LABELS) and re.search(r"bootstrap.*gui|gui/.*bootstrap", line):
                    bad.append(f"{path.name}:{number}")
        self.assertEqual(bad, [])

    def test_fleet_cas_store_and_gc_install_only_daemons(self):
        text = (SCRIPTS / "glaeda-fleet-cas").read_text()
        for function in ("write_store_prune", "write_service"):
            body = re.search(rf"^{function}\(\) \{{.*?^\}}", text, re.S | re.M).group(0)
            if function == "write_service":  # the store branch only: node and writer stay agents
                body = body.split('if [ "$r" = store ]; then', 1)[1].split("\n  fi\n", 1)[0]
            self.assertNotIn("$AGENTS", body, function)
            self.assertNotIn('bootstrap "gui/', body, function)
            self.assertIn("install_daemon", body, function)

    def test_seed_archive_installs_only_a_daemon(self):
        text = (SCRIPTS / "glaeda-seed-archive").read_text()
        self.assertIn('"/Library/LaunchDaemons"', text)
        self.assertNotIn('"bootstrap", domain', text)
        self.assertNotRegex(text, r'\[LAUNCHCTL, "bootstrap", f"gui/')
        install = re.search(r"^def install_daemon.*?^def install\(", text, re.S | re.M).group(0)
        self.assertIn('"bootstrap", "system"', install)


if __name__ == "__main__":
    unittest.main()
