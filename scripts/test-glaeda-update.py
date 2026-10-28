#!/usr/bin/env python3
"""Contract tests for scripts/glaeda-update and scripts/glaeda_release.py (over-the-air updates)."""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import glaeda_release as gr  # noqa: E402

loader = importlib.machinery.SourceFileLoader("glaeda_update", os.fspath(ROOT / "scripts" / "glaeda-update"))
spec = importlib.util.spec_from_loader("glaeda_update", loader)
gu = importlib.util.module_from_spec(spec)
loader.exec_module(gu)
health_loader = importlib.machinery.SourceFileLoader("glaeda_mini_health", os.fspath(ROOT / "scripts" / "glaeda-mini-health"))
mh = importlib.util.module_from_spec(importlib.util.spec_from_loader("glaeda_mini_health", health_loader))
health_loader.exec_module(mh)

TARGET = "aarch64-apple-darwin"
SOURCES = ["a" * 40, "b" * 40, "c" * 40]

# A stand-in for a release's glaeda-mini-setup: installs three tools into $GLAEDA_TEST_BIN that exit
# with the release's health code, and records each run.
FAKE_SETUP = """#!/usr/bin/env python3
import os, sys
from pathlib import Path
if "--help" in sys.argv:
    print("options: --hygiene-only --headless --reclaim-binary")
    raise SystemExit(0)
bin_dir = Path(os.environ["GLAEDA_TEST_BIN"])
bin_dir.mkdir(parents=True, exist_ok=True)
code = int(Path(__file__).with_name("health-code").read_text())
for name in ("glaeda-disk", "glaeda-worktree", "glaeda-projects", "glaeda-update", "glaeda-gh"):
    tool = bin_dir / name
    tool.write_text("#!/usr/bin/env python3\\nimport sys\\nprint('{\\\"result\\\": \\\"plan\\\"}')\\nsys.exit(%d)\\n" % code)
    tool.chmod(0o755)
# as the real setup does: the release's prebuilt reclaim binary, byte for byte
reclaim = bin_dir / "glaeda-worktree-reclaim"
reclaim.write_bytes(Path(sys.argv[sys.argv.index("--reclaim-binary") + 1]).read_bytes())
reclaim.chmod(0o755)
policy = Path(__file__).parents[1] / "config/gh-daemon.json"
if policy.is_file():  # as the real setup does: the fleet's glaeda-gh pacing, in every profile
    (bin_dir.parent / "share/glaeda").mkdir(parents=True, exist_ok=True)
    (bin_dir.parent / "share/glaeda/gh-daemon.json").write_bytes(policy.read_bytes())
with open(bin_dir / "setup-runs", "a") as log:
    log.write(Path(__file__).parents[2].name + " " + " ".join(sys.argv[1:]) + " " + os.environ.get("GLAEDA_UPDATE_RUNNING", "") + "\\n")
"""


def tar_gz(files: dict[str, tuple[bytes, int]]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


class Server:
    """Serves the release files glaeda-update fetches, keyed by URL."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.paused = False
        self.rings: dict[str, dict] = {}
        self.sources: list[str] = []
        self.source = gu.REPOSITORY  # the repository publish() puts releases in

    def publish(self, source: str, health_code: int = 0, archive: bytes | None = None,
                runner_files: dict[str, bytes] | None = None, ancestry: list[str] | None = None,
                hook_history: dict[str, list[bytes]] | None = None,
                support_headless: bool = True) -> dict:
        tag = gr.release_tag(source, dt.datetime(2026, 9, 25))
        runner = {f"glaeda/scripts/{name}": ((runner_files or {}).get(name, f"{name} at {source}".encode()),
                                             0o755 if "." not in name else 0o644)
                  for name in gu.RUNNER_FILES}
        if ancestry is None:
            ancestry = [source, *reversed(self.sources)]
        extra = {"ancestry.txt": ("".join(c + "\n" for c in ancestry).encode(), 0o644)}
        if runner_files:  # the release's own hook versions, plus the older ones the test names
            history = {name: sorted({gr.sha256(runner[f"glaeda/scripts/{name}"][0])}
                                    | {gr.sha256(v) for v in (hook_history or {}).get(name, [])})
                       for name in gr.HOOK_FILES}
            extra["hook-history.json"] = (gr.canonical(history), 0o644)
        archive = archive or tar_gz({
            "glaeda/scripts/glaeda-mini-setup":
                ((FAKE_SETUP if support_headless else FAKE_SETUP.replace(" --headless", "")).encode(), 0o755),
            "glaeda/scripts/health-code": (str(health_code).encode(), 0o644),
            "glaeda/config/gh-daemon.json": (b'{"default": {"restShare": 0}}\n', 0o644),
            "bin/glaeda-worktree-reclaim": (f"#!/bin/sh\nexit {health_code}\n".encode(), 0o755),
            **runner,
            **extra,
        })
        name = gr.hygiene_asset(TARGET)
        release_raw = gr.canonical(gr.release_manifest(source, tag, {name: archive}))
        self.files[gu.release_url(tag, name, self.source)] = archive
        self.files[gu.release_url(tag, "release.json", self.source)] = release_raw
        entry = gr.channel_entry("canary", json.loads(release_raw), release_raw, "2026-09-25T00:00:00Z")
        self.rings["canary"] = entry
        self.rings["stable"] = {**entry, "ring": "stable"}
        self.sources.append(source)
        return entry

    def channel(self, ring: str) -> tuple[bytes, bytes | None]:
        entry = self.rings.get(ring)
        return gr.canonical(gr.control(self.paused)), gr.canonical(entry) if entry else None

    def fetch(self, url: str, limit: int) -> bytes:
        return self.files[url]


def time_stamp(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.state = self.root / "state"
        self.server = Server()
        self.saved = (gu.fetch, gu.attestation, gu.local_target, os.environ.get("GLAEDA_TEST_BIN"))
        gu.fetch = self.server.fetch
        gu.attestation = lambda path: (True, "verified")
        gu.local_target = lambda: TARGET
        os.environ["GLAEDA_TEST_BIN"] = str(self.bin)
        self.config = {"ring": "canary", "setupArgs": ["--hygiene-only"], "host": "test", "reportStatus": False}

    def tearDown(self) -> None:
        gu.fetch, gu.attestation, gu.local_target, test_bin = self.saved
        if test_bin is None:
            os.environ.pop("GLAEDA_TEST_BIN", None)
        self.tmp.cleanup()

    def test_status_post_timeout_is_inconclusive_not_an_exception(self) -> None:
        config = {**self.config, "reportStatus": True}
        entry = {"tag": "r-20260925-" + "a" * 12, "source": "a" * 40}
        old_find_gh = gu.find_gh
        gu.find_gh = lambda: "/usr/bin/gh"
        try:
            with mock.patch.object(gu.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 60)), \
                    mock.patch.object(gu.time, "sleep"):
                self.assertTrue(gu.report(config, entry, True, "installed")
                                .startswith("not reported (transient: TimeoutExpired"))
        finally:
            gu.find_gh = old_find_gh

    def test_attestation_auth_timeout_is_unavailable(self) -> None:
        with mock.patch.object(gu, "find_gh", return_value="/usr/bin/gh"), \
                mock.patch.object(gu.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 60)):
            verdict, why = self.saved[1](Path("/tmp/archive.tar.gz"))
        self.assertIsNone(verdict)
        self.assertIn("not signed in", why)

    def test_host_profile_mismatch_reports_pending(self) -> None:
        config = {**self.config, "reportStatus": True}
        entry = {"tag": "r-20260925-" + "a" * 12, "source": "a" * 40}
        old_find_gh = gu.find_gh
        gu.find_gh = lambda: "/usr/bin/gh"
        try:
            completed = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(gu.subprocess, "run", return_value=completed) as run:
                self.assertEqual(gu.report(config, entry, None, "host profile mismatch"), "reported")
                self.assertIn("state=pending", run.call_args.args[0])
        finally:
            gu.find_gh = old_find_gh

    def test_fetch_timeout_is_inconclusive_and_does_not_quarantine(self) -> None:
        entry = self.server.publish(SOURCES[0])
        old_fetch = gu.fetch
        gu.fetch = mock.Mock(side_effect=urllib.error.URLError("offline"))
        try:
            result = self.run_update()
        finally:
            gu.fetch = old_fetch
        self.assertEqual(result["result"], "inconclusive")
        self.assertNotIn(entry["tag"], gu.load_state(self.state)["quarantined"])

    # 2026-10-19: air-blue's canary runs at 21:32Z and 22:32Z ended "inconclusive" ("a canary must verify
    # the attestation: install gh and sign in", "not reported (gh exited 1)") while gh was installed and
    # signed in: the shared REST quota was spent, so `gh auth status` and the status POST failed, and the
    # next try was an hour away. Stable waited two hours for a healthy release.

    def gh(self, *results: tuple[int, str]) -> mock.Mock:
        """subprocess.run answering each gh call with (exit code, stderr) in order."""
        return mock.Mock(side_effect=[subprocess.CompletedProcess([], code, "", err) for code, err in results])

    def test_rate_limited_attestation_retries_with_backoff_in_the_same_run(self) -> None:
        run = self.gh((0, ""), (1, "HTTP 403: API rate limit exceeded for user ID 1."), (0, ""))
        with mock.patch.object(gu, "find_gh", return_value="/usr/bin/gh"), \
                mock.patch.object(gu, "rate_limit_reset", return_value=None), \
                mock.patch.object(gu.subprocess, "run", run), mock.patch.object(gu.time, "sleep") as sleep:
            self.assertEqual(self.saved[1](Path("/tmp/a.tar.gz")), (True, "verified"))
        argvs = [call.args[0] for call in run.call_args_list]
        # signed-in is a local token check, never `gh auth status`, which spends quota and fails with it
        self.assertEqual(argvs[0][1:], ["auth", "token"])
        self.assertNotIn("status", [arg for argv in argvs for arg in argv[1:3]])
        self.assertEqual([argv[1:3] for argv in argvs[1:]], [["attestation", "verify"]] * 2)
        self.assertEqual(sleep.call_count, 1)

    def test_spent_quota_is_named_with_its_reset_and_costs_no_verify(self) -> None:
        run = self.gh((0, ""))
        reset = gu.time.time() + 40 * 60
        with mock.patch.object(gu, "find_gh", return_value="/usr/bin/gh"), \
                mock.patch.object(gu, "rate_limit_reset", return_value=reset), \
                mock.patch.object(gu.subprocess, "run", run), mock.patch.object(gu.time, "sleep") as sleep:
            verdict, why = self.saved[1](Path("/tmp/a.tar.gz"))
        self.assertIsNone(verdict)
        self.assertIn("rate-limited until " + gu.time.strftime("%H:%MZ", gu.time.gmtime(reset)), why)
        self.assertEqual(run.call_count, 1)  # only the local token check
        sleep.assert_not_called()

    def test_a_reset_within_minutes_is_waited_for_in_the_run(self) -> None:
        run = self.gh((0, ""), (0, ""))
        resets = iter([gu.time.time() + 90, None])
        with mock.patch.object(gu, "find_gh", return_value="/usr/bin/gh"), \
                mock.patch.object(gu, "rate_limit_reset", side_effect=lambda gh: next(resets)), \
                mock.patch.object(gu.subprocess, "run", run), mock.patch.object(gu.time, "sleep") as sleep:
            self.assertEqual(self.saved[1](Path("/tmp/a.tar.gz"))[0], True)
        self.assertGreater(sleep.call_args.args[0], 60)

    def test_signed_out_gh_is_inconclusive_with_its_own_words(self) -> None:
        run = self.gh((1, "no oauth token found for github.com"))
        with mock.patch.object(gu, "find_gh", return_value="/usr/bin/gh"), mock.patch.object(gu.subprocess, "run", run):
            verdict, why = self.saved[1](Path("/tmp/a.tar.gz"))
        self.assertIsNone(verdict)
        self.assertIn("not signed in", why)
        self.assertIn("no oauth token", why)

    def test_a_real_verification_failure_is_false_and_not_retried(self) -> None:
        run = self.gh((0, ""), (1, "Error: verifying with issuer: no matching attestations found"))
        with mock.patch.object(gu, "find_gh", return_value="/usr/bin/gh"), \
                mock.patch.object(gu, "rate_limit_reset", return_value=None), \
                mock.patch.object(gu.subprocess, "run", run), mock.patch.object(gu.time, "sleep") as sleep:
            verdict, why = self.saved[1](Path("/tmp/a.tar.gz"))
        self.assertIs(verdict, False)
        self.assertIn("no matching attestations", why)
        sleep.assert_not_called()

    def test_persistent_rate_limit_stays_inconclusive_after_bounded_backoff(self) -> None:
        limited = (1, "HTTP 403: API rate limit exceeded")
        run = self.gh((0, ""), *[limited] * (len(gu.GH_BACKOFF) + 1))
        with mock.patch.object(gu, "find_gh", return_value="/usr/bin/gh"), \
                mock.patch.object(gu, "rate_limit_reset", return_value=None), \
                mock.patch.object(gu.subprocess, "run", run), mock.patch.object(gu.time, "sleep") as sleep:
            verdict, why = self.saved[1](Path("/tmp/a.tar.gz"))
        self.assertIsNone(verdict)
        self.assertTrue(why.startswith("rate-limited: "), why)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], list(gu.GH_BACKOFF))
        self.assertLessEqual(sum(gu.GH_BACKOFF), 10 * 60)  # one hourly run stays well inside its hour

    def test_status_post_names_why_it_failed(self) -> None:
        config = {**self.config, "reportStatus": True}
        entry = {"tag": "r-20260925-" + "a" * 12, "source": "a" * 40}
        run = self.gh(*[(1, "HTTP 403: API rate limit exceeded for user")] * (len(gu.GH_BACKOFF) + 1))
        with mock.patch.object(gu, "find_gh", return_value="/usr/bin/gh"), \
                mock.patch.object(gu, "rate_limit_reset", return_value=None), \
                mock.patch.object(gu.subprocess, "run", run), mock.patch.object(gu.time, "sleep"):
            status = gu.report(config, entry, True, "installed")
        self.assertTrue(status.startswith("not reported (rate-limited: HTTP 403: API rate limit"), status)
        run = self.gh((1, "HTTP 502: Bad Gateway"), (0, ""))
        with mock.patch.object(gu, "find_gh", return_value="/usr/bin/gh"), \
                mock.patch.object(gu, "rate_limit_reset", return_value=None), \
                mock.patch.object(gu.subprocess, "run", run), mock.patch.object(gu.time, "sleep"):
            self.assertEqual(gu.report(config, entry, True, "installed"), "reported")

    def test_an_undelivered_status_is_posted_again_by_the_next_run(self) -> None:
        self.config = {**self.config, "reportStatus": True}
        entry = self.server.publish(SOURCES[0])
        with mock.patch.object(gu, "report", return_value="not reported (rate-limited: HTTP 403)"):
            first = self.run_update()
        self.assertEqual(first["result"], "updated")
        self.assertEqual([o["tag"] for o in gu.load_state(self.state)["unreported"]], [entry["tag"]])
        # still rate-limited an hour later: kept, not dropped
        with mock.patch.object(gu, "report", return_value="not reported (rate-limited until 23:10Z)"):
            self.assertEqual(self.run_update()["redelivered"], {entry["tag"]: "not reported (rate-limited until 23:10Z)"})
        self.assertEqual(len(gu.load_state(self.state)["unreported"]), 1)
        with mock.patch.object(gu, "report", return_value="reported") as report:
            second = self.run_update()
        self.assertEqual(second["result"], "current")
        self.assertEqual(second["redelivered"], {entry["tag"]: "reported"})
        posted_entry, ok, detail = report.call_args.args[1:]
        self.assertEqual((posted_entry["tag"], posted_entry["source"], ok, detail), (entry["tag"], entry["source"], True, "tools run"))
        self.assertNotIn("unreported", gu.load_state(self.state))
        with mock.patch.object(gu, "report") as report:
            self.assertNotIn("redelivered", self.run_update())
        report.assert_not_called()

    def test_rate_limit_reset_reads_the_core_quota(self) -> None:
        def answer(code: int, out: str):
            return mock.patch.object(gu.subprocess, "run", return_value=subprocess.CompletedProcess([], code, out, ""))
        with answer(0, "0\t1792463616\n"):
            self.assertEqual(gu.rate_limit_reset("gh"), 1792463616.0)
        for code, out in ((0, "3086\t1792463616\n"), (1, "0\t1792463616\n"), (0, ""), (0, "x y")):
            with answer(code, out):
                self.assertIsNone(gu.rate_limit_reset("gh"), (code, out))

    def test_a_forbidden_attestation_read_is_unavailable_not_failed(self) -> None:
        run = self.gh((0, ""), (1, "HTTP 403: Resource not accessible by integration"))
        with mock.patch.object(gu, "find_gh", return_value="/usr/bin/gh"), \
                mock.patch.object(gu, "rate_limit_reset", return_value=None), \
                mock.patch.object(gu.subprocess, "run", run), mock.patch.object(gu.time, "sleep") as sleep:
            verdict, why = self.saved[1](Path("/tmp/a.tar.gz"))
        self.assertIsNone(verdict)
        self.assertIn("cannot read attestations", why)
        sleep.assert_not_called()

    def test_a_gh_without_the_attestation_command_is_unavailable_not_failed(self) -> None:
        # huge-blue, 2026-10-21: apt's gh 2.45 quarantined a good stable release
        for said in ('unknown command "attestation" for "gh"\n\nUsage:  gh <command> <subcommand> [flags]',
                     "unknown flag: --source-ref\n\nUsage:  gh attestation verify"):
            run = self.gh((0, ""), (1, said))
            with mock.patch.object(gu, "find_gh", return_value="/usr/bin/gh"), \
                    mock.patch.object(gu, "rate_limit_reset", return_value=None), \
                    mock.patch.object(gu.subprocess, "run", run), mock.patch.object(gu.time, "sleep"):
                verdict, why = self.saved[1](Path("/tmp/a.tar.gz"))
            self.assertIsNone(verdict, said)
            self.assertIn("too old", why)

    def test_a_failure_waits_out_a_spent_quota_a_success_does_not(self) -> None:
        # a failure that is not posted lets another canary's success promote the release 15 minutes later
        config = {**self.config, "reportStatus": True}
        entry = {"tag": "r-20260925-" + "a" * 12, "source": "a" * 40}
        for ok, waited in ((False, True), (True, False)):
            reset = gu.time.time() + 40 * 60
            resets = iter([reset, None])
            run = self.gh((0, ""))
            with mock.patch.object(gu, "find_gh", return_value="/usr/bin/gh"), \
                    mock.patch.object(gu, "rate_limit_reset", side_effect=lambda gh: next(resets)), \
                    mock.patch.object(gu.subprocess, "run", run), mock.patch.object(gu.time, "sleep") as sleep:
                status = gu.report(config, entry, ok, "x")
            self.assertEqual(status == "reported", waited, status)
            self.assertEqual(sleep.called, waited)
            if waited:
                self.assertLessEqual(sleep.call_args.args[0], gu.FAILURE_WAIT_MAX + 5)

    def test_redelivery_stops_at_the_first_report_that_fails_and_drops_debts_of_a_former_canary(self) -> None:
        config = {**self.config, "reportStatus": True}
        owed = [{"tag": "r-20260925-" + c * 12, "source": c * 40, "ok": True, "detail": "x"} for c in "ab"]
        state = {"unreported": list(owed)}
        with mock.patch.object(gu, "report", return_value="not reported (transient: x)") as report:
            self.assertEqual(list(gu.redeliver(state, config)), [owed[0]["tag"]])
        self.assertEqual(report.call_count, 1)
        self.assertEqual(state["unreported"], owed)
        with mock.patch.object(gu, "report", return_value="not reported (not a reporting canary)"):
            gu.redeliver(state, {**config, "ring": "stable"})
        self.assertNotIn("unreported", state)

    def test_a_report_is_owed_before_it_is_posted(self) -> None:
        # a run killed during the retries must not lose the verdict
        self.config = {**self.config, "reportStatus": True}
        entry = self.server.publish(SOURCES[0])
        seen = []
        def report(config, e, ok, detail):
            seen.append([o["tag"] for o in gu.load_state(self.state).get("unreported", [])])
            return "reported"
        with mock.patch.object(gu, "report", side_effect=report):
            self.run_update()
        self.assertEqual(seen, [[entry["tag"]]])
        self.assertNotIn("unreported", gu.load_state(self.state))

    def test_only_a_verdict_is_owed_and_a_stable_host_owes_nothing(self) -> None:
        config = {**self.config, "reportStatus": True}
        entry = {"tag": "r-20260925-" + "a" * 12, "source": "a" * 40}
        state: dict = {}
        self.assertFalse(gu.owe_report(state, config, entry, None, "inconclusive"))
        self.assertFalse(gu.owe_report(state, {**config, "ring": "stable"}, entry, True, "installed"))
        self.assertEqual(state, {})
        self.assertTrue(gu.owe_report(state, config, entry, False, "setup failed"))
        self.assertTrue(gu.owe_report(state, config, entry, True, "installed"))
        self.assertEqual(state["unreported"], [{"tag": entry["tag"], "source": entry["source"], "ok": True,
                                                "detail": "installed"}])  # one per tag, the newest
        (self.state).mkdir(parents=True, exist_ok=True)
        (self.state / "state.json").write_text(json.dumps({"current": None, "previous": None, "quarantined": [],
                                                            "unreported": [{"tag": 1}]}))
        self.assertNotIn("unreported", gu.load_state(self.state))
        bad_source = {"tag": entry["tag"], "source": "../../x", "ok": True, "detail": "x"}
        (self.state / "state.json").write_text(json.dumps({"current": None, "previous": None, "quarantined": [],
                                                            "unreported": [bad_source]}))
        self.assertNotIn("unreported", gu.load_state(self.state))  # a malformed debt never blocks an update

    def run_update(self, apply: bool = True) -> dict:
        return gu.update(apply, self.config, self.state, self.server.channel(self.config["ring"]), self.bin)

    def test_plan_changes_nothing_then_apply_installs_and_is_idempotent(self) -> None:
        entry = self.server.publish(SOURCES[0])
        plan = self.run_update(apply=False)
        self.assertEqual(plan["result"], "plan")
        self.assertFalse(self.bin.exists())
        done = self.run_update()
        self.assertEqual(done["result"], "updated", done)
        self.assertEqual(gu.load_state(self.state)["current"], entry["tag"])
        runs = (self.bin / "setup-runs").read_text().split()
        self.assertEqual(runs[0], entry["tag"])  # ran the release's own setup
        self.assertIn("--hygiene-only", runs)
        self.assertIn("--reclaim-binary", runs)
        self.assertEqual(runs[-1], "1")  # setup knows glaeda-update is running it
        self.assertEqual(self.run_update()["result"], "current")

    def test_stable_plan_exposes_installed_and_candidate_source_drift(self) -> None:
        self.config["ring"] = "stable"
        stable = self.server.publish(SOURCES[0])
        self.assertEqual(self.run_update()["result"], "updated")

        candidate = self.server.publish(SOURCES[1])
        # The fixture publishes at one fixed time; make the observed candidate
        # age explicit so the diagnostic can report its ordering too.
        self.server.rings["stable"] = {**stable, "ring": "stable"}
        self.server.rings["canary"] = {**candidate, "published": "2026-09-26T00:00:00Z"}
        plan = gu.update(False, self.config, self.state, self.server.channel("stable"), self.bin,
                         candidate_channel=self.server.channel("canary"))

        self.assertEqual(plan["result"], "current")
        self.assertEqual(plan["currentSource"], SOURCES[0])
        self.assertEqual(plan["targetSource"], SOURCES[0])
        self.assertEqual(plan["targetPublished"], stable["published"])
        self.assertEqual(plan["candidate"], {"tag": candidate["tag"], "source": SOURCES[1],
                                               "published": "2026-09-26T00:00:00Z"})
        self.assertEqual(plan["sourceDrift"], "candidate_newer")

    def test_incompatible_setup_args_are_refused_before_install_or_rollback(self) -> None:
        self.config["setupArgs"] = ["--hygiene-only", "--headless"]
        good = self.server.publish(SOURCES[0], support_headless=True)
        self.assertEqual(self.run_update()["result"], "updated")
        bad = self.server.publish(SOURCES[1], support_headless=False)
        result = self.run_update()
        self.assertEqual((result["result"], result["detail"]),
                         ("refused", "release setup does not support --headless"))
        self.assertEqual(gu.load_state(self.state)["current"], good["tag"])
        self.assertEqual((self.bin / "setup-runs").read_text().splitlines(),
                         [f"{good['tag']} --apply --hygiene-only --headless --reclaim-binary "
                          f"{self.state / 'generations' / good['tag'] / 'bin/glaeda-worktree-reclaim'} 1"])
        self.assertNotIn(bad["tag"], gu.load_state(self.state)["quarantined"])
        self.assertEqual(self.run_update()["result"], "refused")

    def test_older_target_is_refused_before_setup(self) -> None:
        older = self.server.publish(SOURCES[0])
        newer = self.server.publish(SOURCES[1])
        self.assertEqual(self.run_update()["result"], "updated")
        self.server.rings["canary"] = older
        result = self.run_update()
        self.assertEqual(result["result"], "refused")
        self.assertIn("older than installed", result["detail"])
        self.assertEqual(gu.load_state(self.state)["current"], newer["tag"])
        self.assertNotIn(older["tag"], gu.load_state(self.state)["quarantined"])
        unknown = self.server.publish(SOURCES[2], ancestry=[SOURCES[2], "d" * 40])
        self.server.rings["canary"] = unknown
        result = self.run_update()
        self.assertEqual(result["result"], "refused")
        self.assertIn("unknown than installed", result["detail"])
        self.assertNotIn(unknown["tag"], gu.load_state(self.state)["quarantined"])

    def test_a_stale_runner_copy_is_refreshed_and_an_operators_newer_copy_kept(self) -> None:
        copy = self.root / "glaeda-runner/scripts"
        self.assertEqual(gu.refresh_runner_scripts(self.state, copy), "absent")  # not a runner mini: nothing made
        self.assertFalse(copy.exists())
        entry = self.server.publish(SOURCES[0])
        self.assertEqual(self.run_update()["result"], "updated")
        copy.mkdir(parents=True)
        for name in gu.RUNNER_FILES:  # a Sep 24 copy with no stamp, as cmux15 had
            (copy / name).write_text("old " + name)
        self.assertEqual(gu.refresh_runner_scripts(self.state, copy), f"refreshed to {entry['tag']}")
        self.assertEqual((copy / "glaeda-cmux-runner").read_text(), f"glaeda-cmux-runner at {SOURCES[0]}")
        self.assertTrue(os.access(copy / "glaeda-cmux-runner", os.X_OK))
        stamp = json.loads((copy / gu.SOURCE_STAMP).read_text())
        self.assertEqual((stamp["by"], stamp["tag"], stamp["source"], stamp["date"]),
                         ("glaeda-update", entry["tag"], SOURCES[0], "2026-09-25"))
        self.assertEqual(gu.refresh_runner_scripts(self.state, copy), "current")
        # an operator staged main the same day (fleet runner relabel): kept, never downgraded
        (copy / "glaeda-cmux-runner").write_text("operator main")
        (copy / gu.SOURCE_STAMP).write_text(json.dumps({"by": "fleet", "date": "2026-09-25", "source": "d" * 40}))
        self.assertIn("kept", gu.refresh_runner_scripts(self.state, copy))
        self.assertEqual((copy / "glaeda-cmux-runner").read_text(), "operator main")
        # the same operator copy is older than the next day's release: refreshed
        (copy / gu.SOURCE_STAMP).write_text(json.dumps({"by": "fleet", "date": "2026-09-24", "source": "d" * 40}))
        self.assertIn("refreshed", gu.refresh_runner_scripts(self.state, copy))
        # a copy this updater wrote is always brought to the newer release, even on the same day
        newer = self.server.publish(SOURCES[1])
        self.assertEqual(self.run_update()["result"], "updated")
        self.assertEqual(gu.refresh_runner_scripts(self.state, copy), f"refreshed to {newer['tag']}")

    def runner_mini(self) -> tuple[Path, Path, Path]:
        """A mini as glaeda-cmux-runner --apply left it: an owned runner whose hooks came from the staged copy."""
        home = self.root / "home"
        runner = home / "actions-runner-glaeda"
        hooks = runner / "glaeda-hooks"
        hooks.mkdir(parents=True)
        (runner / ".glaeda-cmux-runner").write_text("install-1\n")
        state = home / ".local/state/glaeda/cmux-runner"
        state.mkdir(parents=True)
        (state / "receipt.json").write_text(json.dumps({"runnerDir": str(runner), "runnerDirOwned": True,
                                                        "installId": "install-1"}))
        hook = hooks / "glaeda-cmux-runner-hook"
        (hooks / "job-started.sh").write_text(f"#!/bin/bash\nexec {sys.executable} {hook} job-started "
                                              "--allowed-repo manaflow-ai/cmux\n")
        (hooks / "job-started.sh").chmod(0o755)
        staged = home / "glaeda-runner/scripts"
        staged.mkdir(parents=True)
        for name, data in self.runner_files("an older release").items():
            (staged / name).write_bytes(data)
            target = hooks / name
            if name in ("glaeda-cmux-runner-hook", "glaeda_reservation.py"):
                target.write_bytes(data)
                target.chmod(0o755 if name == "glaeda-cmux-runner-hook" else 0o644)
        return home, staged, hook

    def runner_files(self, which: str) -> dict[str, bytes]:
        """The real runner scripts, with a hook that says which release it came from."""
        files = {name: (ROOT / "scripts" / name).read_bytes() for name in gu.RUNNER_FILES}
        files["glaeda-cmux-runner-hook"] += f"\n# {which}\n".encode()
        return files

    def older(self) -> dict[str, list[bytes]]:
        """The hook versions runner_mini installed, as history a release descends from."""
        files = self.runner_files("an older release")
        return {name: [files[name]] for name in gr.HOOK_FILES}

    def test_an_update_installs_the_releases_runner_hook(self) -> None:
        home, staged, hook = self.runner_mini()
        wrapper = (hook.parent / "job-started.sh").read_bytes()
        entry = self.server.publish(SOURCES[0], runner_files=self.runner_files("the fixed release"),
                                    hook_history=self.older())
        self.assertEqual(self.run_update()["result"], "updated")
        self.assertEqual(gu.runner_steps(self.state, staged, home),
                         {"runnerScripts": f"refreshed to {entry['tag']}", "runnerHooks": f"{entry['tag']}: 1 updated"})
        self.assertEqual(hook.read_bytes(), self.runner_files("the fixed release")["glaeda-cmux-runner-hook"])
        self.assertEqual(os.stat(hook).st_mode & 0o777, 0o755)
        self.assertEqual((hook.parent / "job-started.sh").read_bytes(), wrapper, "wrappers are --apply's")
        self.assertEqual(gu.runner_steps(self.state, staged, home)["runnerHooks"], f"{entry['tag']}: 1 unchanged")

    def test_a_hook_newer_than_the_release_is_never_downgraded(self) -> None:
        home, staged, hook = self.runner_mini()
        newer = self.runner_files("main, after the release")["glaeda-cmux-runner-hook"]
        hook.write_bytes(newer)  # fleet runner relabel --apply from a temporary copy of main
        entry = self.server.publish(SOURCES[0], runner_files=self.runner_files("the release"),
                                    hook_history=self.older())
        self.assertEqual(self.run_update()["result"], "updated")
        self.assertEqual(gu.runner_steps(self.state, staged, home)["runnerHooks"],
                         f"{entry['tag']}: 1 kept (the installed glaeda-cmux-runner-hook is not a version this "
                         "release descends from; --apply owns it)")
        self.assertEqual(hook.read_bytes(), newer)

    def test_a_same_day_operator_copy_the_release_descends_from_is_refreshed(self) -> None:
        home, staged, hook = self.runner_mini()
        (staged / gu.SOURCE_STAMP).write_text(json.dumps({"by": "glaeda-cmux-runner-fleet", "date": "2026-09-25",
                                                          "source": "d" * 40}))
        self.server.publish(SOURCES[0], runner_files=self.runner_files("the release"), ancestry=[SOURCES[0]])
        self.assertEqual(self.run_update()["result"], "updated")
        self.assertIn("kept", gu.refresh_runner_scripts(self.state, staged))
        newer = self.server.publish(SOURCES[1], runner_files=self.runner_files("the next release"),
                                    ancestry=[SOURCES[1], SOURCES[0], "d" * 40], hook_history=self.older())
        self.assertEqual(self.run_update()["result"], "updated")
        self.assertEqual(gu.runner_steps(self.state, staged, home),
                         {"runnerScripts": f"refreshed to {newer['tag']}", "runnerHooks": f"{newer['tag']}: 1 updated"})
        self.assertEqual(hook.read_bytes(), self.runner_files("the next release")["glaeda-cmux-runner-hook"])

    def test_unhealthy_release_rolls_back_and_is_quarantined(self) -> None:
        good = self.server.publish(SOURCES[0])
        self.run_update()
        bad = self.server.publish(SOURCES[1], health_code=1)
        result = self.run_update()
        self.assertEqual(result["result"], "rolled-back")
        self.assertEqual(result["rollback"], f"restored {good['tag']}")
        state = gu.load_state(self.state)
        self.assertEqual(state["current"], good["tag"])
        self.assertIn(bad["tag"], state["quarantined"])
        # the restored tools are the good release's again
        self.assertEqual(subprocess.run([sys.executable, str(self.bin / "glaeda-disk")],
                                        capture_output=True).returncode, 0)
        self.assertEqual(self.run_update()["result"], "quarantined")
        # a newer release is still tried
        newer = self.server.publish(SOURCES[2])
        self.assertEqual(self.run_update()["result"], "updated")
        state = gu.load_state(self.state)
        self.assertEqual((state["current"], state["previous"]), (newer["tag"], good["tag"]))
        kept = sorted(p.name for p in (self.state / "generations").iterdir())
        self.assertEqual(kept, sorted([newer["tag"], good["tag"]]))

    def test_a_health_check_that_times_out_rolls_back_without_quarantine(self) -> None:
        good = self.server.publish(SOURCES[0])
        self.run_update()
        slow = self.server.publish(SOURCES[1])
        timed_out = (None, "glaeda-disk: Command '--help' timed out after 120 seconds")
        with mock.patch.object(gu, "health", return_value=timed_out):
            result = self.run_update()
        self.assertEqual(result["result"], "inconclusive")
        self.assertIn("timed out", result["detail"])
        self.assertEqual(result["rollback"], f"restored {good['tag']}")
        state = gu.load_state(self.state)
        self.assertEqual(state["current"], good["tag"])
        self.assertNotIn(slow["tag"], state["quarantined"])
        # an idle host tries the same release again and keeps it
        self.assertEqual(self.run_update()["result"], "updated")
        self.assertEqual(gu.load_state(self.state)["current"], slow["tag"])

    def test_a_release_that_times_out_every_time_is_quarantined_in_the_end(self) -> None:
        self.server.publish(SOURCES[0])
        self.run_update()
        hangs = self.server.publish(SOURCES[1])
        timed_out = (None, "glaeda-disk: Command '--help' timed out after 120 seconds")
        with mock.patch.object(gu, "health", return_value=timed_out):
            results = [self.run_update()["result"] for _ in range(gu.HEALTH_TIMEOUTS)]
        self.assertEqual(results, ["inconclusive"] * (gu.HEALTH_TIMEOUTS - 1) + ["rolled-back"])
        state = gu.load_state(self.state)
        self.assertIn(hangs["tag"], state["quarantined"])
        self.assertNotIn("healthTimeouts", state)

    def test_a_malformed_timeout_count_starts_over(self) -> None:
        self.server.publish(SOURCES[0])
        self.run_update()
        state = gu.load_state(self.state)
        for bad in ("x", {"tag": "t", "n": "2"}, {"n": 1}):
            with self.subTest(bad=bad):
                gu.save_state({**state, "healthTimeouts": bad}, self.state)
                self.assertNotIn("healthTimeouts", gu.load_state(self.state))
        hangs = self.server.publish(SOURCES[1])
        with mock.patch.object(gu, "health", return_value=(None, "glaeda-disk: timed out")):
            self.assertEqual(self.run_update()["result"], "inconclusive")
        self.assertEqual(gu.load_state(self.state)["healthTimeouts"], {"tag": hangs["tag"], "n": 1})

    def test_a_timeout_whose_rollback_fails_is_blocking(self) -> None:
        good = self.server.publish(SOURCES[0])
        self.run_update()
        self.server.publish(SOURCES[1])
        real_setup = gu.run_setup
        calls = []

        def setup(gen, args):
            calls.append(gen.name)
            return real_setup(gen, args) if len(calls) == 1 else (False, "timed out")

        with mock.patch.object(gu, "health", return_value=(None, "glaeda-disk: timed out")), \
                mock.patch.object(gu, "run_setup", setup):
            result = self.run_update()
        self.assertEqual((result["result"], result["rollback"]),
                         ("rolled-back", f"restoring {good['tag']} failed too"))
        self.assertNotIn(self.server.rings["canary"]["tag"], gu.load_state(self.state)["quarantined"])

    def test_tampered_bytes_are_refused(self) -> None:
        entry = self.server.publish(SOURCES[0])
        url = gu.release_url(entry["tag"], gr.hygiene_asset(TARGET))
        self.server.files[url] += b"x"
        result = self.run_update()
        self.assertEqual((result["result"], result["detail"]), ("refused", "archive does not match release.json"))
        self.assertIn(entry["tag"], gu.load_state(self.state)["quarantined"])
        self.assertEqual(self.run_update()["result"], "quarantined")
        entry = self.server.publish(SOURCES[1])
        self.server.rings["canary"]["releaseSha256"] = "0" * 64
        self.assertEqual(self.run_update()["detail"], "release.json does not match the channel")
        self.assertFalse(self.bin.exists())

    def test_failed_attestation_is_refused(self) -> None:
        self.server.publish(SOURCES[0])
        gu.attestation = lambda path: (False, "no attestation from release.yml")
        self.assertIn("did not verify", self.run_update()["detail"])
        # a canary that cannot check refuses; stable, which only names canary-verified releases, installs
        self.server.publish(SOURCES[1])
        gu.attestation = lambda path: (None, "no gh on this host")
        inconclusive = self.run_update()
        self.assertEqual(inconclusive["result"], "inconclusive")
        self.assertIn("canary must verify", inconclusive["detail"])
        self.assertNotIn(self.server.rings["canary"]["tag"], gu.load_state(self.state)["quarantined"])
        self.assertFalse(self.bin.exists())
        self.config["ring"] = "stable"
        self.state = self.root / "stable-host-state"  # another host, which has quarantined nothing
        self.assertEqual(self.run_update()["result"], "updated")

    def test_first_update_rolls_back_to_the_tools_it_replaced(self) -> None:
        self.bin.mkdir()
        (self.bin / "glaeda-disk").write_text("hand-installed\n")
        self.server.publish(SOURCES[0], health_code=1)
        result = self.run_update()
        self.assertEqual(result["result"], "rolled-back")
        self.assertEqual(result["rollback"], "restored the tools installed before the first update")
        self.assertEqual((self.bin / "glaeda-disk").read_text(), "hand-installed\n")
        self.assertFalse((self.bin / "glaeda-worktree").exists())
        self.assertFalse((self.bin / "glaeda-projects").exists())

    def test_malformed_channel_names_are_refused(self) -> None:
        entry = self.server.publish(SOURCES[0])
        for tag in ("../../../../cli/cli/releases/download/v2.0.0", "r-20260925-ABC", "latest"):
            with self.subTest(tag=tag):
                self.server.rings["canary"] = {**entry, "tag": tag}
                with self.assertRaisesRegex(gu.UpdateError, "malformed"):
                    self.run_update()
        self.server.rings["canary"] = {**entry, "ring": "stable"}
        with self.assertRaisesRegex(gu.UpdateError, "malformed"):
            self.run_update()

    def test_health_plans_offline_from_the_fetched_channel(self) -> None:
        self.server.publish(SOURCES[0])
        self.run_update()
        channel_dir = self.state / "health-channel"
        self.assertEqual(json.loads((channel_dir / "control.json").read_bytes()), gr.control(False))
        self.assertEqual(json.loads((channel_dir / "canary.json").read_bytes())["tag"],
                         self.server.rings["canary"]["tag"])
        self.assertFalse((self.state / "pre-ota").exists())  # snapshot dropped after the first success

    def test_a_gh_only_host_checks_only_the_tools_it_installs(self) -> None:
        ran = []

        def fake_run(argv, **kw):
            ran.append(Path(argv[1] if argv[0] == sys.executable else argv[0]).name)
            return subprocess.CompletedProcess(argv, 0, '{"result": "current"}\n', "")

        with mock.patch.object(gu.subprocess, "run", fake_run):
            self.assertEqual(gu.health(self.bin, self.root, gh_only=True), (True, "tools run"))
            self.assertEqual(ran, ["glaeda-gh", "glaeda-update"])
            ran.clear()
            gu.health(self.bin, self.root)
        self.assertEqual(ran, ["glaeda-disk", "glaeda-worktree", "glaeda-projects", "glaeda-worktree-reclaim",
                               "glaeda-gh", "glaeda-update"])

    def test_a_check_that_times_out_is_inconclusive_not_failed(self) -> None:
        def slow_run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])

        with mock.patch.object(gu.subprocess, "run", slow_run):
            ok, why = gu.health(self.bin, self.root)
        self.assertIsNone(ok)
        self.assertIn("glaeda-disk", why)

        def missing(argv, **kw):
            raise FileNotFoundError(argv[0])

        with mock.patch.object(gu.subprocess, "run", missing):
            self.assertIs(gu.health(self.bin, self.root)[0], False)

    def test_real_updater_plans_from_a_channel_dir_without_network(self) -> None:
        entry = self.server.publish(SOURCES[0])
        channel_dir = self.root / "channel"
        channel_dir.mkdir()
        control_raw, ring_raw = self.server.channel("stable")
        (channel_dir / "control.json").write_bytes(control_raw)
        (channel_dir / "stable.json").write_bytes(ring_raw)
        home = self.root / "home"
        home.mkdir()
        env = {**os.environ, "HOME": str(home), "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9"}
        result = subprocess.run([sys.executable, str(ROOT / "scripts/glaeda-update"), "--channel-dir", str(channel_dir)],
                                capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual((plan["result"], plan["target"]), ("plan", entry["tag"]))

    def test_bad_state_stops_the_run(self) -> None:
        self.server.publish(SOURCES[0])
        self.state.mkdir()
        for text in ("{not json", json.dumps({"current": "../x", "previous": None, "quarantined": []}), "[]"):
            (self.state / "state.json").write_text(text)
            with self.assertRaises(gu.UpdateError):
                self.run_update()

    def test_unsafe_archive_entries_are_refused(self) -> None:
        for name in ("../escape", "/abs/path"):
            with self.subTest(name=name):
                self.server.publish(SOURCES[0 if name.startswith("..") else 1], archive=tar_gz({name: (b"x", 0o644)}))
                self.assertIn("refused", self.run_update()["detail"])
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            link = tarfile.TarInfo("glaeda/link")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(link)
        self.server.publish(SOURCES[2], archive=out.getvalue())
        self.assertIn("refused", self.run_update()["detail"])

    def test_paused_and_empty_rings_do_nothing(self) -> None:
        self.server.publish(SOURCES[0])
        self.server.paused = True
        self.assertEqual(self.run_update()["result"], "idle")
        self.server.paused = False
        del self.server.rings["stable"]
        self.config["ring"] = "stable"
        self.assertEqual(self.run_update()["result"], "idle")
        self.assertFalse(self.bin.exists())

    def test_a_mirror_host_downloads_only_from_the_mirror_and_still_checks_the_repository(self) -> None:
        self.server.source = "manaflow-ai/glaeda"
        entry = self.server.publish(SOURCES[0])
        self.config["source"] = "manaflow-ai/glaeda"
        done = self.run_update()
        self.assertEqual((done["result"], done["source"]), ("updated", "manaflow-ai/glaeda"), done)
        self.assertEqual(gu.load_state(self.state)["current"], entry["tag"])
        # the same bytes at this repository's URLs only: a host on the default source would not find them
        self.config["source"] = gu.REPOSITORY
        self.state = self.root / "state-default"
        with self.assertRaises(KeyError):
            self.run_update()

    def test_config_defaults_and_refusals(self) -> None:
        path = self.root / "update.json"
        self.assertEqual(gu.load_config(path)["ring"], "stable")
        self.assertEqual(gu.load_config(path)["source"], gu.REPOSITORY)
        path.write_text(json.dumps({"source": "manaflow-ai/glaeda"}))
        self.assertEqual(gu.load_config(path)["source"], "manaflow-ai/glaeda")
        path.write_text(json.dumps({"source": "someone/glaeda"}))
        with self.assertRaises(gu.UpdateError):
            gu.load_config(path)
        path.write_text(json.dumps({"ring": "canary", "host": "air-blue"}))
        config = gu.load_config(path)
        self.assertTrue(config["reportStatus"])
        self.assertEqual(config["setupArgs"], ["--hygiene-only"])
        for bad in ({"ring": "beta"}, {"setupArgs": ["--uninstall"]}, {"setupArgs": ["rm"]}):
            path.write_text(json.dumps(bad))
            with self.assertRaises(gu.UpdateError):
                gu.load_config(path)


    # ---- step 8: convergence (2026-10-20: aws-m4pro-9's runners showed the 80/70 CPU gate a day after
    # #1535's 85/80 hook shipped, and nothing on the host said which hook bytes it ran)

    def apply(self, now: float, home: Path | None = None, staged: Path | None = None) -> dict:
        home = home or self.root / "nohome"
        return gu.apply_once(self.config, self.state, self.bin, home, staged or home / "glaeda-runner/scripts",
                             now=now, channel=self.server.channel(self.config["ring"]))

    def converge(self) -> dict:
        return gu.load_converge(self.state / gu.CONVERGE.name)

    def health_findings(self, now: float) -> list[dict]:
        mini = mh.Mini(self.root)
        mini.ota_converge = self.converge  # the file glaeda-update just wrote
        return mh.check_ota(mini, now)

    def test_stale_hook_digest_is_detected_reported_and_alerted_only_past_its_grace(self) -> None:
        home, staged, hook = self.runner_mini()
        stale = hook.read_bytes()
        # the installed hook is no version this release descends from: --refresh-hooks keeps it
        entry = self.server.publish(SOURCES[0], runner_files=self.runner_files("the 85/80 release"))
        now = 1_800_000_000.0
        result = self.apply(now, home, staged)
        self.assertEqual(result["result"], "updated", result)
        self.assertEqual(hook.read_bytes(), stale)
        doc = self.converge()
        self.assertEqual(doc["running"]["tag"], entry["tag"])
        self.assertEqual(doc["wants"]["tag"], entry["tag"])  # the tag matches; the bytes do not
        self.assertFalse(doc["converged"])
        self.assertTrue(doc["blocking"])
        [row] = doc["running"]["hooks"]
        self.assertEqual(row["glaeda-cmux-runner-hook"], gr.sha256(stale))
        self.assertFalse(row["current"])
        self.assertIn(f"hook {gr.sha256(stale)[:12]} is not {entry['tag']}'s", doc["why"][0])
        self.assertIn("kept", doc["why"][0])
        # health says nothing inside the grace, then an error naming running, wants and why
        self.assertEqual(self.health_findings(now + 60), [])
        [f] = self.health_findings(now + gu.ALERT_AFTER_S + 1)
        self.assertEqual((f["id"], f["severity"], f["auto_fix"]), ("ota_not_converging", "error", "impossible"))
        self.assertTrue(f["evidence"].startswith(f"running {entry['tag']}, wants {entry['tag']}, because runner"))
        self.assertIn(gr.sha256(stale)[:12], f["evidence"])
        # status reads the same by content, offline
        self.assertFalse(gu.observe(self.state, self.bin, home, staged)["hooks"][0]["current"])

    def test_a_hook_the_release_descends_from_converges_and_clears_the_record(self) -> None:
        home, staged, hook = self.runner_mini()
        entry = self.server.publish(SOURCES[0], runner_files=self.runner_files("the 85/80 release"),
                                    hook_history=self.older())
        self.apply(1_800_000_000.0, home, staged)
        doc = self.converge()
        self.assertTrue(doc["converged"], doc["why"])
        self.assertEqual(doc["running"]["hooks"][0]["glaeda-cmux-runner-hook"],
                         gr.sha256(self.runner_files("the 85/80 release")["glaeda-cmux-runner-hook"]))
        self.assertEqual((doc["since"], doc["alertAfter"], doc["attempts"]), (None, None, 0))
        self.assertEqual(self.health_findings(1_800_000_000.0 + 10 * gu.ALERT_AFTER_S)[1:], [])
        self.assertEqual(entry["tag"], doc["running"]["tag"])

    def test_a_deferred_hook_converges_on_a_later_run_between_jobs(self) -> None:
        home, staged, hook = self.runner_mini()
        self.server.publish(SOURCES[0], runner_files=self.runner_files("new"), hook_history=self.older())
        busy = mock.patch.object(gu, "refresh_runner_hooks",
                                 side_effect=lambda s, h, d: d.append({"runnerDir": str(hook.parent.parent),
                                                                       "state": "deferred", "note": "a job"}) or "x")
        with busy:
            self.apply(1_800_000_000.0, home, staged)
        doc = self.converge()
        self.assertFalse(doc["converged"])
        self.assertFalse(doc["blocking"])  # a job holds the runner: retrying is the fix
        # the release is installed, so the next run is not backed off: it refreshes between jobs
        result = self.apply(1_800_000_000.0 + 60, home, staged)
        self.assertNotEqual(result["result"], "backoff")
        self.assertTrue(self.converge()["converged"], self.converge()["why"])

    def test_an_unconverged_host_backs_off_and_a_new_release_is_tried_at_once(self) -> None:
        first = self.server.publish(SOURCES[0])
        now = 1_800_000_000.0
        with mock.patch.object(gu, "download", side_effect=urllib.error.URLError("offline")):
            self.assertEqual(self.apply(now)["result"], "inconclusive")
            doc = self.converge()
            self.assertEqual((doc["converged"], doc["blocking"], doc["attempts"]), (False, False, 1))
            self.assertEqual(doc["wants"]["tag"], first["tag"])
            self.assertIsNone(doc["running"]["tag"])
            self.assertEqual(self.apply(now + gu.APPLY_EVERY_S - 1)["result"], "backoff")
            self.assertEqual(self.apply(now + gu.APPLY_EVERY_S)["result"], "inconclusive")
            self.assertEqual(self.converge()["attempts"], 2)
            self.assertEqual(self.apply(now + gu.APPLY_EVERY_S + 2 * gu.APPLY_EVERY_S - 1)["result"], "backoff")
            # the backoff never exceeds the old hourly run, and the first sighting is kept for the alert
            self.assertEqual(gu.backoff_s(30), gu.BACKOFF_MAX_S)
            self.assertEqual(self.converge()["since"], time_stamp(now))
            [f] = self.health_findings(now + gu.ALERT_AFTER_S)
            self.assertEqual((f["severity"], f["auto_fix"]), ("warn", "pending"))
            self.assertIn(f"running no release, wants {first['tag']}, because inconclusive", f["evidence"])
        second = self.server.publish(SOURCES[1])
        result = self.apply(now + gu.APPLY_EVERY_S + 1)  # inside the old backoff: a new release goes at once
        self.assertEqual(result["result"], "updated", result)
        self.assertTrue(self.converge()["converged"])
        self.assertEqual(self.converge()["running"]["tag"], second["tag"])

    def test_tools_that_drift_from_the_installed_release_are_reinstalled(self) -> None:
        self.server.publish(SOURCES[0])
        now = 1_800_000_000.0
        self.assertEqual(self.apply(now)["result"], "updated")
        reclaim = self.bin / "glaeda-worktree-reclaim"
        reclaim.write_text("#!/bin/sh\n# a hand install from another checkout\nexit 0\n")
        self.assertEqual(gu.observe(self.state, self.bin, self.root, self.root / "x")["tools"]["drift"],
                         ["glaeda-worktree-reclaim"])
        result = self.apply(now + 1)
        self.assertEqual((result["result"], result["repair"]), ("current", "reinstalled glaeda-worktree-reclaim"))
        self.assertTrue(self.converge()["converged"])
        self.assertEqual(self.converge()["running"]["tools"]["drift"], [])

    def test_a_missing_or_edited_fleet_gh_policy_is_reinstalled(self) -> None:
        self.server.publish(SOURCES[0])
        now = 1_800_000_000.0
        self.assertEqual(self.apply(now)["result"], "updated")
        policy = self.bin.parent / "share/glaeda/gh-daemon.json"
        self.assertTrue(policy.is_file())
        for n, damage in enumerate((policy.unlink, lambda: policy.write_text('{"default": {"restShare": 1}}'))):
            damage()
            self.assertEqual(gu.observe(self.state, self.bin, self.root, self.root / "x")["tools"]["drift"],
                             ["gh-daemon.json"])
            result = self.apply(now + 1 + n)
            self.assertEqual((result["result"], result["repair"]), ("current", "reinstalled gh-daemon.json"))
            self.assertEqual(policy.read_bytes(), b'{"default": {"restShare": 0}}\n')

    def test_a_gh_only_host_does_not_repair_tools_it_no_longer_installs(self) -> None:
        self.server.publish(SOURCES[0])
        now = 1_800_000_000.0
        self.assertEqual(self.apply(now)["result"], "updated")
        (self.bin / "glaeda-worktree-reclaim").write_text("#!/bin/sh\n# left from the hygiene profile\nexit 0\n")
        self.config = {**self.config, "setupArgs": ["--gh-only"]}
        self.assertEqual(gu.observe(self.state, self.bin, self.root, self.root / "x", gh_only=True)["tools"]["drift"], [])
        result = self.apply(now + 1)
        self.assertEqual(result["result"], "current", result)
        self.assertNotIn("repair", result)

    def test_a_run_that_dies_still_records_why(self) -> None:
        self.server.publish(SOURCES[0])
        with mock.patch.object(gu, "update", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.apply(1_800_000_000.0)
        doc = self.converge()
        self.assertEqual((doc["result"], doc["converged"]), ("error", False))
        self.assertIn("RuntimeError: boom", doc["why"][0])

    def test_a_host_whose_updater_stopped_is_reported_stale(self) -> None:
        self.server.publish(SOURCES[0])
        now = 1_800_000_000.0
        self.apply(now)
        self.assertEqual(self.health_findings(now + gu.STALE_AFTER_S - 1), [])
        [f] = self.health_findings(now + gu.STALE_AFTER_S)
        self.assertEqual(f["id"], "ota_stale")

    def test_the_canary_that_installed_a_release_still_nudges_after_its_soak(self) -> None:
        entry = self.server.publish(SOURCES[0])
        published = gu.parse_utc(entry["published"])
        soak = float(entry["policy"]["soakHours"]) * 3600
        self.assertGreater(soak, 120)
        with mock.patch.object(gu, "nudge_promotion", return_value="dispatched") as nudge:
            self.assertEqual(self.apply(published + 60)["promotion"], "dispatched")  # the install's nudge
            self.assertNotIn("nudged", self.converge())
            self.assertNotIn("promotion", self.apply(published + 120))  # current, still soaking
            self.assertEqual(self.apply(published + soak + 3600)["promotion"], "dispatched")  # after the soak
            self.assertEqual(self.converge()["nudged"], entry["tag"])
            self.assertNotIn("promotion", self.apply(published + soak + 7200))  # once per release
        self.assertEqual(nudge.call_count, 2)

    def test_a_first_run_that_never_read_the_channel_is_not_converged(self) -> None:
        with mock.patch.object(gu, "fetch_channel", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(urllib.error.URLError):
                gu.apply_once(self.config, self.state, self.bin, self.root, self.root / "x", now=1_800_000_000.0)
        doc = self.converge()
        self.assertFalse(doc["converged"])
        self.assertIsNone(doc["wants"])
        self.paused_is_converged()

    def paused_is_converged(self) -> None:
        self.server.publish(SOURCES[0])
        self.server.paused = True
        self.assertEqual(self.apply(1_800_000_100.0)["result"], "idle")
        self.assertTrue(self.converge()["converged"])

    def test_a_current_canary_nudges_promotion_once_per_release_after_its_soak(self) -> None:
        entry = {"tag": "r-20260925-" + "a" * 12, "published": "2026-09-25T00:00:00Z", "policy": {"soakHours": 0.25}}
        soaked = gu.parse_utc("2026-09-25T00:15:00Z")
        self.assertTrue(gu.nudge_due({"result": "updated"}, entry, entry["tag"], 0))
        self.assertFalse(gu.nudge_due({"result": "current"}, entry, None, soaked - 1))
        self.assertTrue(gu.nudge_due({"result": "current"}, entry, None, soaked))
        self.assertFalse(gu.nudge_due({"result": "current"}, entry, entry["tag"], soaked + 3600))
        self.assertFalse(gu.nudge_due({"result": "inconclusive"}, entry, None, soaked))

    def test_the_schedule_and_the_promotion_window_derive_from_the_updater(self) -> None:
        setup = importlib.machinery.SourceFileLoader("glaeda_mini_setup_for_update", os.fspath(ROOT / "scripts/glaeda-mini-setup"))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(setup.name, setup))
        setup.exec_module(module)
        self.assertEqual(module.update_every_s(), gu.APPLY_EVERY_S)
        timer = (ROOT / "ops/systemd/glaeda-update.timer").read_text()
        self.assertIn(f"OnCalendar=*:0/{gu.APPLY_EVERY_S // 60}\n", timer)
        # a failing canary's report (waited out up to FAILURE_WAIT_MAX, then retried after a backoff) lands
        # before the CI smoke may promote a release no canary reported on
        self.assertGreater(gr.CANARY_SILENT_HOURS * 3600, gu.FAILURE_WAIT_MAX + gu.BACKOFF_MAX_S)


class ScheduleReloadTest(unittest.TestCase):
    """setup writes the agent's new StartInterval but cannot reload the agent of the run that called it, so Macs that
    never log out kept running hourly after APPLY_EVERY_S became 600 (cosmic-gray, air-blue, cmux16, 2026-10-21)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.state = self.home / "state"
        self.state.mkdir()
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        self.plist = agents / f"{gu.LABEL}.plist"
        self.plist.write_bytes(gu.plistlib.dumps({"Label": gu.LABEL, "StartInterval": 600}))

    def run_it(self, shown: str, config: dict | None = None, platform: str = "darwin", popen=None):
        calls, spawned = [], []

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, shown, "")

        with mock.patch.object(gu.sys, "platform", platform), mock.patch.object(gu.subprocess, "run", fake_run), \
                mock.patch.object(gu.subprocess, "Popen", popen or (lambda argv, **kw: spawned.append((argv, kw)))):
            note = gu.schedule_reload(config or {"setupArgs": ["--hygiene-only"]}, home=self.home, pid=4242,
                                      state_dir=self.state)
        return note, calls, spawned

    def test_an_old_interval_is_reloaded_after_this_run(self) -> None:
        note, calls, spawned = self.run_it("\tstate = running\n\trun interval = 3600 seconds\n")
        self.assertEqual(note, "reload scheduled: launchd runs every 3600s, the plist says 600s")
        self.assertEqual(calls, [["/bin/launchctl", "print", f"gui/{os.getuid()}/{gu.LABEL}"]])
        (argv, kw), = spawned
        self.assertTrue(kw["start_new_session"])  # outside the job's process group: survives this run's end
        self.assertEqual(argv[:4], [sys.executable, "-I", "-c", gu.RELOAD_HELPER])
        self.assertEqual(json.loads(argv[4]), [4242, f"{self.state}/lock", f"{self.state}/reload.log", [],
                                               "/bin/launchctl", f"gui/{os.getuid()}/{gu.LABEL}",
                                               f"gui/{os.getuid()}", os.fspath(self.plist), 600])

    def test_nothing_to_do(self) -> None:
        for shown in ("\trun interval = 600 seconds\n", "Could not find service\n"):
            with self.subTest(shown=shown):
                note, _, spawned = self.run_it(shown)
                self.assertIsNone(note)
                self.assertEqual(spawned, [])
        note, calls, _ = self.run_it("\trun interval = 3600 seconds\n", platform="linux")
        self.assertEqual((note, calls), (None, []))  # systemd re-reads its timer on daemon-reload

    def test_a_helper_that_cannot_start_is_reported_not_raised(self) -> None:
        def fail(argv, **kw):
            raise OSError("fork failed")
        note, _, _ = self.run_it("\trun interval = 3600 seconds\n", popen=fail)
        self.assertEqual(note, "reload not started (fork failed): launchd runs every 3600s, the plist says 600s")

    def test_a_headless_mac_reloads_its_system_daemon_through_sudo(self) -> None:
        plist, target, prefix = gu.launchd_job({"setupArgs": ["--hygiene-only", "--headless"]}, self.home)
        self.assertEqual((plist, target, prefix), (Path(f"/Library/LaunchDaemons/{gu.LABEL}.plist"),
                                                   f"system/{gu.LABEL}", ["/usr/bin/sudo", "-n"]))


@unittest.skipUnless(sys.platform == "darwin", "the helper lints the plist with /usr/bin/plutil")
class ReloadHelperTest(unittest.TestCase):
    """RELOAD_HELPER itself, against a fake launchctl that keeps the job's state in a file."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.plist = self.dir / "job.plist"
        self.plist.write_bytes(gu.plistlib.dumps({"Label": gu.LABEL, "StartInterval": 600}))
        self.job, self.calls = self.dir / "job", self.dir / "calls"
        self.job.write_text("3600")
        self.ctl = self.dir / "launchctl"
        # print: shows the loaded interval or fails; bootout: unloads; bootstrap: fails while FAILS has lines left
        self.ctl.write_text(f"""#!/bin/sh
echo "$*" >> {self.calls}
case "$1" in
print) [ -s {self.job} ] || {{ echo "Could not find service"; exit 113; }}; echo "	run interval = $(cat {self.job}) seconds";;
bootout) : > {self.job};;
bootstrap) if [ -s {self.dir}/fails ]; then sed -i '' 1d {self.dir}/fails; echo "5: Input/output error"; exit 5; fi
  /usr/bin/plutil -extract StartInterval raw "$3" > {self.job};;
esac
""")
        self.ctl.chmod(0o755)

    def helper(self, pid: int | None = None, timeout: float = 30) -> dict:
        if pid is None:
            run = subprocess.Popen(["/usr/bin/true"])
            run.wait()
            pid = run.pid  # the run that scheduled the reload has exited
        args = [pid, os.fspath(self.dir / "lock"), os.fspath(self.dir / "reload.log"), [], os.fspath(self.ctl),
                "gui/501/x", "gui/501", os.fspath(self.plist), 600]
        subprocess.run([sys.executable, "-I", "-c", gu.RELOAD_HELPER, json.dumps(args)], timeout=timeout, check=False)
        *_, last = (self.dir / "reload.log").read_text().splitlines()
        return json.loads(last)

    def test_reloads_and_confirms_the_new_interval(self) -> None:
        out = self.helper()
        self.assertEqual((out["result"], out["bootstrap"]), ("reloaded", [0]))
        self.assertEqual(self.job.read_text().strip(), "600")
        calls = self.calls.read_text().splitlines()
        self.assertEqual(calls[1:3], ["bootout gui/501/x", "print gui/501/x"])  # waited for launchd to let go
        self.assertIn(f"bootstrap gui/501 {self.plist}", calls)

    def test_a_refused_bootstrap_is_retried(self) -> None:
        (self.dir / "fails").write_text("1\n2\n")
        out = self.helper()
        self.assertEqual((out["result"], out["bootstrap"]), ("reloaded", [5, 5, 0]))

    def test_a_plist_that_does_not_lint_leaves_the_job_loaded(self) -> None:
        self.plist.write_text("not a plist")
        out = self.helper()
        self.assertEqual(out["result"], "refused")
        self.assertEqual(self.job.read_text(), "3600")
        self.assertNotIn("bootout", self.calls.read_text())

    def test_waits_for_the_run_and_its_lock(self) -> None:
        import fcntl
        run = subprocess.Popen(["/bin/sleep", "1"])
        with open(self.dir / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            helper = subprocess.Popen([sys.executable, "-I", "-c", gu.RELOAD_HELPER, json.dumps(
                [run.pid, os.fspath(self.dir / "lock"), os.fspath(self.dir / "reload.log"), [], os.fspath(self.ctl),
                 "gui/501/x", "gui/501", os.fspath(self.plist), 600])])
            run.wait()
            time.sleep(1.5)  # the run is gone but this lock is held: a newer run is working, so nothing is touched
            self.assertFalse(self.calls.exists())
        helper.wait(timeout=30)
        self.assertEqual(json.loads((self.dir / "reload.log").read_text())["result"], "reloaded")

    def test_an_interval_already_right_is_left_alone(self) -> None:
        self.job.write_text("600")
        self.assertEqual(self.helper()["result"], "reloaded")
        self.assertEqual(self.calls.read_text().splitlines(), ["print gui/501/x"])


class ReleaseTest(unittest.TestCase):
    NOW = dt.datetime(2026, 9, 25, 12, tzinfo=dt.timezone.utc)

    def canary(self, published: str = "2026-09-25T00:00:00Z") -> dict:
        return {"schema": gr.CHANNEL_SCHEMA, "ring": "canary", "tag": "r-20260925-" + "a" * 12,
                "source": "a" * 40, "releaseSha256": "0" * 64, "published": published}

    def status(self, host: str, state: str, at: str) -> dict:
        return {"context": gr.STATUS_PREFIX + host, "state": state, "updated_at": at}

    def test_policy_classifies_exact_paths_and_sets_coverage(self) -> None:
        low = gr.policy_for_paths(["docs/GLAEDA_OTA.md", "tests/test_policy.md"])
        self.assertEqual((low["risk"], low["soakHours"], low["requiredHosts"]), ("low", 0.25, []))
        normal = gr.policy_for_paths(["scripts/telemetry.py"])
        self.assertEqual((normal["risk"], normal["soakHours"], normal["requiredHosts"]), ("normal", 0.25, []))
        high = gr.policy_for_paths(["scripts/glaeda-update"])
        self.assertEqual((high["risk"], high["soakHours"], high["requiredHosts"]), ("high", 0.25, []))
        self.assertEqual(gr.policy_for_paths([])["risk"], "high")

    def test_soak_override_rejects_non_finite_or_negative_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidates.jsonl"
            path.write_text("")
            for value in ("-1", "nan", "inf"):
                self.assertEqual(gr.main(["promote", "--candidates", str(path), "--soak-hours", value]), 1)

    def test_a_smoked_release_no_canary_could_report_on_is_promoted_after_the_silent_window(self) -> None:
        # 2026-10-19: the fleet's REST quota was spent for hours, no canary could post, stable sat 15 h.
        canary = self.canary("2026-09-25T00:00:00Z")  # legacy policy: 6 h soak
        late = dt.datetime(2026, 9, 25, 8, 30, tzinfo=dt.timezone.utc)
        self.assertTrue(gr.promotion(canary, None, [], late, smoke_passed=True)[0])
        early = dt.datetime(2026, 9, 25, 8, 29, tzinfo=dt.timezone.utc)
        ok, why = gr.promotion(canary, None, [], early, smoke_passed=True)
        self.assertFalse(ok)
        self.assertIn("CI smoke promotes it at 8.5 h", why)
        self.assertFalse(gr.promotion(canary, None, [], late)[0])  # unsmoked (older) releases need a host
        failed = [self.status("big-red", "failure", "2026-09-25T07:00:00Z")]
        self.assertFalse(gr.promotion(canary, None, failed, late, smoke_passed=True)[0])
        candidate = {"entry": canary, "statuses": [], "descendsFromStable": True, "smoked": True}
        self.assertEqual(gr.choose([candidate], None, late)[0]["tag"], canary["tag"])
        self.assertIsNone(gr.choose([{**candidate, "smoked": False}], None, late)[0])

    def test_smoke_runs_the_release_tools_and_its_receipt_marks_the_release_smoked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            files = {f"glaeda/scripts/{name}": ((ROOT / "scripts" / name).read_bytes(), 0o755)
                     for name in ("glaeda-disk", "glaeda-worktree", "glaeda-projects", "glaeda-gh", "glaeda-mini-health",
                                 "glaeda-update")}
            files["bin/glaeda-worktree-reclaim"] = (b"#!/bin/sh\nexit 0\n", 0o755)
            (out / gr.hygiene_asset(TARGET)).write_bytes(tar_gz(files))
            self.assertEqual(gr.main(["smoke", "--source", "a" * 40, "--target", TARGET, "--output", str(out)]), 0)
            receipt = json.loads((out / gr.smoke_asset(TARGET)).read_text())
            self.assertEqual((receipt["schema"], receipt["target"]), (gr.SMOKE_SCHEMA, TARGET))
            self.assertIn("glaeda-update", receipt["checks"])
            files["bin/glaeda-worktree-reclaim"] = (b"#!/bin/sh\nexit 3\n", 0o755)
            (out / gr.hygiene_asset(TARGET)).write_bytes(tar_gz(files))
            self.assertEqual(gr.main(["smoke", "--source", "a" * 40, "--target", TARGET, "--output", str(out)]), 1)
        assets = {gr.smoke_asset(t): {} for t in gr.TARGETS}
        self.assertTrue(gr.smoked({"assets": assets}))
        self.assertFalse(gr.smoked({"assets": {gr.smoke_asset(TARGET): {}}}))

    def test_promotion_needs_soak_success_and_no_failure(self) -> None:
        ok = [self.status("air-blue", "success", "2026-09-25T01:00:00Z")]
        self.assertFalse(gr.promotion(self.canary("2026-09-25T10:00:00Z"), None, ok, self.NOW)[0])
        self.assertEqual(gr.promotion(self.canary(), None, [], self.NOW),
                         (False, "no canary host reported success"))
        failed = ok + [self.status("big-red", "failure", "2026-09-25T02:00:00Z")]
        self.assertFalse(gr.promotion(self.canary(), None, failed, self.NOW)[0])
        # a host's newest status counts: a later success clears its earlier failure
        recovered = failed + [self.status("big-red", "success", "2026-09-25T03:00:00Z")]
        self.assertTrue(gr.promotion(self.canary(), None, recovered, self.NOW)[0])
        # unrelated contexts are ignored
        noise = ok + [{"context": "ci/verify", "state": "failure", "updated_at": "2026-09-25T04:00:00Z"}]
        self.assertTrue(gr.promotion(self.canary(), None, noise, self.NOW)[0])
        self.assertFalse(gr.promotion(self.canary(), {**self.canary(), "ring": "stable"}, ok, self.NOW)[0])
        self.assertEqual(gr.promotion(None, None, ok, self.NOW), (False, "no canary release"))


    def test_one_canary_success_promotes_and_any_failure_blocks(self) -> None:
        # Leo, 2026-10-19: "why does it need literally everybody to report success". No host is required.
        at = "2026-09-25T01:00:00Z"
        for paths in (["scripts/telemetry.py"], ["scripts/glaeda-update"], ["docs/x.md"]):
            candidate = {**self.canary(), "policy": gr.policy_for_paths(paths)}
            for host in ("air-blue", "big-red", "cosmic-gray"):
                self.assertTrue(gr.promotion(candidate, None, [self.status(host, "success", at)], self.NOW)[0], (paths, host))
            # a silent or pending canary holds nothing past the soak
            pending = [self.status("big-red", "success", at), self.status("air-blue", "pending", at)]
            self.assertTrue(gr.promotion(candidate, None, pending, self.NOW)[0])
            failed = [self.status("big-red", "success", at), self.status("cosmic-gray", "failure", at)]
            self.assertFalse(gr.promotion(candidate, None, failed, self.NOW)[0])
            self.assertEqual(gr.promotion(candidate, None, [self.status("air-blue", "pending", at)], self.NOW),
                             (False, "no canary host reported success"))

    def test_policies_signed_with_required_hosts_parse_and_are_not_enforced(self) -> None:
        legacy = {"schema": gr.POLICY_SCHEMA, "risk": "high", "soakHours": 0.25,
                  "requiredHosts": ["air-blue", "big-red"], "paths": ["scripts/glaeda-update"]}
        self.assertEqual(gr.parse_policy(legacy)["requiredHosts"], ["air-blue", "big-red"])
        candidate = {**self.canary(), "policy": legacy}
        self.assertTrue(gr.promotion(candidate, None, [self.status("big-red", "success", "2026-09-25T01:00:00Z")],
                                     self.NOW)[0])
        for bad in ({**legacy, "requiredHosts": ["air-blue"]}, {**legacy, "requiredHosts": ["nobody"]},
                    {**legacy, "risk": "low", "soakHours": 0.25}):
            with self.assertRaises(gr.ReleaseError):
                gr.parse_policy(bad)

    def test_the_15_hour_stall_promotes_on_big_reds_success_alone(self) -> None:
        # 2026-10-19: r-20261019-a3a8519563ef, signed with requiredHosts [air-blue, big-red], had big-red's
        # success and air-blue's pending (the quota was spent) and was not promoted; stable stayed on 07:31Z.
        legacy = {"schema": gr.POLICY_SCHEMA, "risk": "high", "soakHours": 0.25,
                  "requiredHosts": ["air-blue", "big-red"], "paths": ["scripts/glaeda-update"]}
        held = {"entry": {**self.entry("b", "2026-09-25T04:00:00Z"), "policy": legacy}, "policy": legacy,
                "statuses": [self.status("big-red", "success", "2026-09-25T04:30:00Z"),
                             self.status("air-blue", "pending", "2026-09-25T04:50:00Z")],
                "descendsFromStable": True}
        stable = {**self.entry("a", "2026-09-25T01:00:00Z"), "ring": "stable"}
        chosen, why = gr.choose([held], stable, self.NOW)
        self.assertEqual(chosen["tag"], held["entry"]["tag"], why)
        # a newer release nobody has reported on yet holds nothing either, once it has soaked
        silent = {"entry": self.entry("c", "2026-09-25T10:00:00Z"), "statuses": [], "descendsFromStable": True}
        self.assertEqual(gr.choose([silent, held], stable, self.NOW)[0]["tag"], held["entry"]["tag"])
        # but cosmic-gray's failure, from any canary, still blocks it
        failed = {**held, "statuses": held["statuses"] + [self.status("cosmic-gray", "failure", "2026-09-25T05:00:00Z")]}
        self.assertIsNone(gr.choose([failed], stable, self.NOW)[0])

    def test_historical_signed_policy_remains_promotable_alongside_new_policy(self) -> None:
        historical = {**self.canary(), "policy": {**gr.policy_for_paths(["scripts/glaeda-update"]),
                                                    "soakHours": 12}}
        parsed = gr.parse_policy(historical["policy"])
        self.assertEqual(parsed["soakHours"], 12)
        current = gr.policy_for_paths(["scripts/glaeda-update"])
        self.assertEqual(current["soakHours"], 0.25)

    def entry(self, letter: str, published: str) -> dict:
        return {**self.canary(published), "tag": "r-20260925-" + letter * 12, "source": letter * 40}

    def test_choose_takes_the_newest_soaked_healthy_release_not_only_the_canary(self) -> None:
        ok = [self.status("air-blue", "success", "2026-09-25T01:00:00Z")]
        bad = [self.status("big-red", "failure", "2026-09-25T01:00:00Z")]
        old = {"entry": self.entry("a", "2026-09-25T01:00:00Z"), "statuses": ok, "descendsFromStable": True}
        mid = {"entry": self.entry("b", "2026-09-25T04:00:00Z"), "statuses": bad, "descendsFromStable": True}
        new = {"entry": self.entry("c", "2026-09-25T10:00:00Z"), "statuses": ok, "descendsFromStable": True}
        # c is still soaking: a, the newest soaked healthy release, is promoted
        chosen, why = gr.choose([new, old], None, self.NOW)
        self.assertEqual(chosen["tag"], old["entry"]["tag"], why)
        # a canary failure on b, newer than a, may be a's updater failing to install b: nothing older goes
        self.assertIsNone(gr.choose([new, mid, old], None, self.NOW)[0])
        # a failure on a newer release that is still soaking blocks too
        soaking_bad = {**new, "statuses": bad}
        self.assertIsNone(gr.choose([soaking_bad, old], None, self.NOW)[0])
        # nothing older than stable is considered, and never a release off stable's line
        stable = {**old["entry"], "ring": "stable"}
        self.assertIsNone(gr.choose([new, mid, old], stable, self.NOW)[0])
        side = {**mid, "statuses": ok, "descendsFromStable": False}
        self.assertIsNone(gr.choose([side, old], stable, self.NOW)[0])
        self.assertEqual(gr.choose([], None, self.NOW), (None, "no releases"))

    def test_cli_candidate_then_promote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            release_raw = gr.canonical(gr.release_manifest("a" * 40, "r-20260925-" + "a" * 12, {"x.tar.gz": b"x"}))
            (tmp / "release.json").write_bytes(release_raw)
            (tmp / "statuses.json").write_text(json.dumps([self.status("air-blue", "success", "2026-09-25T01:00:00Z")]))
            out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
            with contextlib.redirect_stdout(out):
                self.assertEqual(gr.main(["candidate", "--release", str(tmp / "release.json"), "--published",
                                          "2026-09-25T00:00:00Z", "--statuses", str(tmp / "statuses.json"),
                                          "--descends", "true"]), 0)
            out.flush()
            (tmp / "candidates.jsonl").write_bytes(out.buffer.getvalue())
            out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
            with open(os.devnull, "w") as null, contextlib.redirect_stderr(null), contextlib.redirect_stdout(out):
                self.assertEqual(gr.main(["promote", "--candidates", str(tmp / "candidates.jsonl"),
                                          "--soak-hours", "0"]), 0)
                (tmp / "empty.jsonl").write_text("")
                self.assertEqual(gr.main(["promote", "--candidates", str(tmp / "empty.jsonl")]), 3)
            out.flush()
            stable = gr.parse_channel(out.buffer.getvalue(), "stable")
            self.assertEqual((stable["tag"], stable["releaseSha256"]), ("r-20260925-" + "a" * 12, gr.sha256(release_raw)))

    def test_manifest_and_channel_round_trip(self) -> None:
        release_raw = gr.canonical(gr.release_manifest("a" * 40, "r-20260925-" + "a" * 12, {"x.tar.gz": b"data"}))
        doc = gr.parse_release(release_raw, gr.sha256(release_raw))
        entry = gr.channel_entry("canary", doc, release_raw, "2026-09-25T00:00:00Z")
        self.assertEqual(gr.parse_channel(gr.canonical(entry), "canary"), entry)
        self.assertEqual(gu.resolve(gr.canonical(gr.control(False)), gr.canonical(entry), "canary"), entry)
        self.assertIsNone(gu.resolve(gr.canonical(gr.control(True)), gr.canonical(entry), "canary"))
        self.assertEqual(gu.check_release(release_raw, entry)["tag"], entry["tag"])
        with self.assertRaises(gr.ReleaseError):
            gr.parse_release(release_raw, "0" * 64)

    def test_hook_history_keeps_a_merged_side_branch_version_the_merge_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

            def git(*args: str) -> str:
                return subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True, text=True,
                                      env=env).stdout.strip()

            def commit(text: str) -> None:
                for name in gr.HOOK_FILES:
                    (repo / "scripts" / name).write_text(f"{name} {text}\n")
                git("add", "-A")
                git("commit", "-q", "-m", text)

            git("init", "-q", "-b", "main")
            (repo / "scripts").mkdir()
            commit("v1")
            git("checkout", "-q", "-b", "side")
            commit("side")  # a release from this branch installed it
            git("checkout", "-q", "main")
            commit("v2")
            git("merge", "-q", "-s", "ours", "--no-edit", "side")  # the merge keeps main's version
            history = json.loads(gr.hook_history(repo, git("rev-parse", "HEAD")))
            for name in gr.HOOK_FILES:
                self.assertEqual(set(history[name]), {gr.sha256(f"{name} {v}\n".encode()) for v in ("v1", "side", "v2")})

    def test_hygiene_archive_holds_the_tree_and_the_binary(self) -> None:
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
        with tempfile.TemporaryDirectory() as tmp:
            reclaim = Path(tmp) / "reclaim"
            reclaim.write_bytes(b"\x7fELF")
            raw = gr.hygiene_archive(ROOT, head, reclaim)
            self.assertEqual(raw, gr.hygiene_archive(ROOT, head, reclaim))  # reproducible bytes
            with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                names = set(tar.getnames())
                setup = tar.getmember("glaeda/scripts/glaeda-mini-setup")
                self.assertTrue(setup.mode & 0o111)
                self.assertEqual(tar.extractfile("bin/glaeda-worktree-reclaim").read(), b"\x7fELF")
                self.assertEqual(tar.extractfile(gr.ANCESTRY).read().decode().split()[0], head)
                history = json.loads(tar.extractfile(gr.HOOK_HISTORY).read())
                hook_now = gr.sha256((ROOT / "scripts/glaeda-cmux-runner-hook").read_bytes())
                self.assertIn(hook_now, history["glaeda-cmux-runner-hook"])  # the tree is committed in CI
            self.assertIn("glaeda/scripts/glaeda-update", names)
            self.assertIn("glaeda/ops/systemd/glaeda-update.timer", names)
            # glaeda-update's own extraction accepts it
            archive = Path(tmp) / "a.tar.gz"
            archive.write_bytes(raw)
            gu.safe_extract(archive, Path(tmp) / "out")
            self.assertTrue((Path(tmp) / "out/glaeda/scripts/glaeda_release.py").is_file())


if __name__ == "__main__":
    unittest.main()
