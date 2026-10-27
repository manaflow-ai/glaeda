#!/usr/bin/env python3
"""Contract tests for scripts/glaeda-cmux-runner and scripts/glaeda-cmux-runner-hook.

Runs on Linux CI. gh, curl, launchctl and the runner's config.sh are fakes that log every argv
and environment flag to a JSON-lines file, so the tests can prove the registration token never
reaches an argv, a file, or the output.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import plistlib
import re
import shlex
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "glaeda-cmux-runner-hook"
WORKER_STEP_EXIT = "runner-worker-step-exit "  # the Runner.Worker stand-in's last stdout line (HookTest.take)
# Where the hook looks for leftover processes in canonical roots (settle_root): nowhere real, so no test run
# stops a process in this machine's own /private/tmp/cmux-ci. test_taking_a_root_stops_its_leftovers sets its own.
NO_ROOTS = "/nonexistent/glaeda-test-canonical-roots"


def load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, os.fspath(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


cr = load("glaeda_cmux_runner", ROOT / "scripts" / "glaeda-cmux-runner")
cr.DARWIN_REQUIRED = False
cr.ONLINE_WAIT_S = 0
# The test machine's own macOS never leaks into label assertions; tests that want an OS label patch it.
REAL_MACOS_MAJOR = cr.macos_major
cr.macos_major = lambda: None
# The pressure gate reads this machine's real iostat/sysctl values. Pin CPU pressure out of reach here and in the
# hooks the tests start; pressure tests patch the thresholds and samples themselves.
os.environ["GLAEDA_RUNNER_GATE_LOAD_PAUSE"] = os.environ["GLAEDA_RUNNER_GATE_LOAD_RESUME"] = "1000000"
os.environ["GLAEDA_RUNNER_GATE_CPU_PAUSE"] = os.environ["GLAEDA_RUNNER_GATE_CPU_RESUME"] = "1000000"
# Likewise the console check reads this Mac's real session, which a locked test host would turn into refusals: no
# ioreg here or in the hooks the tests start (unreadable changes nothing); console tests point it at a fake.
os.environ["GLAEDA_RUNNER_IOREG"] = "/nonexistent/ioreg"
# The missing-Rust check reads this host's real ~/.cargo/bin: off in this process (and what it starts with its env);
# RustToolchainMissing tests turn it on against a fake bin dir. The hooks run_hook starts get a fake HOME and PATH.
os.environ["GLAEDA_RUNNER_TOOLCHAIN_GATE"] = "0"
hook = load("glaeda_cmux_runner_hook", HOOK)
# The gate evicts under its disk floor with whatever glaeda-disk it finds: in this process never this machine's own
# (~/.local/bin); a test that wants one passes it explicitly or patches disk_pressure_run.
hook.find_disk = lambda explicit: explicit if explicit and os.path.isfile(explicit) else None
REAL_LEVEL = hook.thermal_pressure_level  # GateTest patches the module attribute
setup = load("glaeda_mini_setup_for_runner", ROOT / "scripts" / "glaeda-mini-setup")
setup.DARWIN_REQUIRED = False

REG_TOKEN = "AREGTOKENSECRET0123456789"
REMOVE_TOKEN = "AREMOVETOKENSECRET9876543"
VERSION = "2.399.0"

FAKE_LOG_HEADER = r'''#!/usr/bin/env python3
import json, os, sys
state = os.environ["FAKE_STATE"]
def log(tool, argv):
    with open(os.path.join(state, "log.jsonl"), "a") as f:
        f.write(json.dumps({"tool": tool, "argv": argv,
                            "envToken": os.environ.get("ACTIONS_RUNNER_INPUT_TOKEN")}) + "\n")
def runners():
    try:
        return json.load(open(os.path.join(state, "runners.json")))
    except OSError:
        return {"runners": []}
def save(doc):
    json.dump(doc, open(os.path.join(state, "runners.json"), "w"))
'''

FAKE_GH = FAKE_LOG_HEADER + r'''
argv = sys.argv[1:]
log("gh", argv)
if argv[:2] == ["auth", "status"]:
    sys.exit(0)
if argv[0] != "api":
    sys.exit(2)
if "-X" in argv:
    i = argv.index("-X"); method, endpoint = argv[i + 1], argv[i + 2]
else:
    method, endpoint = "GET", argv[1]
if endpoint.startswith("repos/actions/runner/releases"):
    if os.path.exists(os.path.join(state, "fail-release")):
        sys.exit(1)
    sys.stdout.write(open(os.path.join(state, "release.json")).read())
elif endpoint.endswith("/registration-token") and method == "POST":
    print(os.environ["FAKE_REG_TOKEN"])
elif endpoint.endswith("/remove-token") and method == "POST":
    if os.path.exists(os.path.join(state, "fail-remove-token")):
        sys.exit(1)
    print(os.environ["FAKE_REMOVE_TOKEN"])
elif "/actions/runners" in endpoint and method == "GET":
    if os.path.exists(os.path.join(state, "fail-list")):
        sys.exit(1)
    print(json.dumps(runners()))
elif "/actions/runners/" in endpoint and method == "DELETE":
    if os.path.exists(os.path.join(state, "fail-delete")):
        sys.exit(1)
    rid = int(endpoint.rsplit("/", 1)[1])
    save({"runners": [r for r in runners()["runners"] if r["id"] != rid]})
else:
    sys.exit(3)
'''

FAKE_CURL = FAKE_LOG_HEADER + r'''
argv = sys.argv[1:]
log("curl", argv)
if "-o" not in argv:  # the gh-free path reads the public release API
    if "--config" in argv:  # an authenticated read: note whether stdin carried the header
        open(os.path.join(state, "curl-config"), "w").write(sys.stdin.read())
    if os.path.exists(os.path.join(state, "fail-release")):
        sys.exit(22)
    sys.stdout.write(open(os.path.join(state, "release.json")).read()); sys.exit(0)
dest = argv[argv.index("-o") + 1]
open(dest, "wb").write(open(os.path.join(state, "runner.tar.gz"), "rb").read())
'''

FAKE_LAUNCHCTL = FAKE_LOG_HEADER + r'''
argv = sys.argv[1:]
log("launchctl", argv)
# launchd's disabled overrides live in disabled.json (labels); bootstrap refuses a disabled one, as launchd does.
path = os.path.join(state, "disabled.json")
disabled = json.load(open(path)) if os.path.exists(path) else []
if argv[0] == "print-disabled":
    extra = open(os.path.join(state, "print-disabled-extra")).read() if os.path.exists(os.path.join(state, "print-disabled-extra")) else ""
    print("disabled services = {\n" + extra + "".join(f'\t"{l}" => disabled\n' for l in disabled) + "}"); sys.exit(0)
if argv[0] in ("enable", "disable"):
    label = argv[1].rsplit("/", 1)[1]
    disabled = [l for l in disabled if l != label] + ([label] if argv[0] == "disable" else [])
    json.dump(disabled, open(path, "w")); sys.exit(0)
if argv[0] == "bootstrap" and os.path.basename(argv[2])[:-len(".plist")] in disabled:
    print("Bootstrap failed: 5: Input/output error"); sys.exit(5)
# With loaded.json present, the fake tracks which labels are loaded; otherwise print always says not loaded.
lpath = os.path.join(state, "loaded.json")
if os.path.exists(lpath):
    loaded = json.load(open(lpath))
    if argv[0] == "print":
        sys.exit(0 if argv[1].rsplit("/", 1)[1] in loaded else 113)  # launchctl's "Could not find service"
    if argv[0] == "bootstrap":
        loaded.append(os.path.basename(argv[2])[:-len(".plist")])
    if argv[0] == "bootout":
        loaded = [l for l in loaded if l != argv[1].rsplit("/", 1)[1]]
    json.dump(loaded, open(lpath, "w"))
sys.exit(113 if argv[0] == "print" else 0)
'''

FAKE_CONFIG = FAKE_LOG_HEADER + r'''
argv = sys.argv[1:]
log("config.sh", argv)
here = os.path.dirname(os.path.abspath(__file__))
token = os.environ.get("ACTIONS_RUNNER_INPUT_TOKEN")
if argv[:1] == ["remove"]:
    if token != os.environ["FAKE_REMOVE_TOKEN"] or os.path.exists(os.path.join(state, "fail-config-remove")):
        print("remove failed"); sys.exit(1)
    name = json.load(open(os.path.join(here, ".runner")))["agentName"]
    save({"runners": [r for r in runners()["runners"] if r["name"] != name]})
    os.remove(os.path.join(here, ".runner"))
    sys.exit(0)
# The real config.sh runs env.sh first, which overwrites .path with the PATH config.sh runs under.
open(os.path.join(here, ".path"), "w").write(os.environ.get("PATH", "") + "\n")
if token != os.environ["FAKE_REG_TOKEN"]:
    print("bad token " + str(token)); sys.exit(1)
name = argv[argv.index("--name") + 1]
labels = ([] if "--no-default-labels" in argv else ["self-hosted", "macOS", "ARM64"]) + argv[argv.index("--labels") + 1].split(",")
doc = runners()
if "--replace" in argv:
    doc["runners"] = [r for r in doc["runners"] if r["name"] != name]
elif any(r["name"] == name for r in doc["runners"]):
    print("A runner exists with the same name"); sys.exit(1)
count_path = os.path.join(state, "registrations")
count = int(open(count_path).read()) if os.path.exists(count_path) else 0
open(count_path, "w").write(str(count + 1))
rid = 4242 + count
json.dump({"agentId": rid, "agentName": name, "gitHubUrl": argv[argv.index("--url") + 1]},
          open(os.path.join(here, ".runner"), "w"))
for extra in (".credentials", ".credentials_rsaparams"):
    open(os.path.join(here, extra), "w").write("{}")
doc["runners"].append({"id": rid, "name": name, "status": "online", "busy": False,
                       "labels": [{"name": l} for l in labels]})
save(doc)
print("registered with token " + token)  # a leaky runner must still not leak it through us
'''


def make_executable(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


def wait_for_path(path: Path, seconds: float = 10) -> bool:
    """job-completed starts glaeda-disk detached: what the pass writes appears after the hook returns."""
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return path.exists()


def event(tmp: Path, name: str, payload: dict) -> Path:
    path = tmp / f"{name}.json"
    path.write_text(json.dumps(payload))
    return path


CMUX = {"full_name": "manaflow-ai/cmux", "fork": False}
FORK = {"full_name": "someone/cmux", "fork": True}
TEAMLEADER_FORK = {"full_name": "teamleaderleo/cmux", "fork": True}
SAMPLE_EVENTS = {
    # name: (GITHUB_EVENT_NAME, payload, admitted)
    "same-repo-pr": ("pull_request", {"repository": CMUX, "pull_request": {
        "head": {"repo": CMUX}, "base": {"repo": CMUX}}}, True),
    "push": ("push", {"repository": CMUX, "ref": "refs/heads/main"}, True),
    "workflow-dispatch": ("workflow_dispatch", {"repository": CMUX, "inputs": {}}, True),
    "merge-group": ("merge_group", {"repository": CMUX, "merge_group": {"head_ref": "gh-readonly-queue/main/x"}}, True),
    "fork-pr": ("pull_request", {"repository": CMUX, "pull_request": {
        "head": {"repo": FORK}, "base": {"repo": CMUX}}}, False),
    "teamleaderleo-fork-pr": ("pull_request", {"repository": CMUX, "pull_request": {
        "head": {"repo": TEAMLEADER_FORK}, "base": {"repo": CMUX}}}, True),
    "teamleaderleo-fork-wrong-base": ("pull_request", {"repository": CMUX, "pull_request": {
        "head": {"repo": TEAMLEADER_FORK},
        "base": {"repo": {"full_name": "manaflow-ai/other", "fork": False}}}}, False),
    "cross-repo-pr-not-flagged-fork": ("pull_request", {"repository": CMUX, "pull_request": {
        "head": {"repo": {"full_name": "other/cmux", "fork": False}}, "base": {"repo": CMUX}}}, False),
    "deleted-fork-pr": ("pull_request", {"repository": CMUX, "pull_request": {
        "head": {"repo": None}, "base": {"repo": CMUX}}}, False),
    "pull-request-target-same-repo": ("pull_request_target", {"repository": CMUX, "pull_request": {
        "head": {"repo": CMUX}, "base": {"repo": CMUX}}}, False),
    "fork-pr-review": ("pull_request_review", {"repository": CMUX, "pull_request": {
        "head": {"repo": FORK}, "base": {"repo": CMUX}}}, False),
    "pr-event-without-payload": ("pull_request", {"repository": CMUX}, False),
    "workflow-run-from-fork": ("workflow_run", {"repository": CMUX, "workflow_run": {
        "head_repository": FORK, "event": "pull_request"}}, False),
    "workflow-run-same-repo": ("workflow_run", {"repository": CMUX, "workflow_run": {
        "head_repository": CMUX, "event": "push"}}, True),
    "other-repository": ("push", {"repository": {"full_name": "evil/cmux"}}, False),
    "issue-comment-on-pr": ("issue_comment", {"repository": CMUX, "issue": {"pull_request": {"url": "x"}}}, False),
    "check-run": ("check_run", {"repository": CMUX, "check_run": {}}, False),
    "schedule": ("schedule", {"repository": CMUX}, True),
}


class DrainPinTest(unittest.TestCase):
    def test_the_hook_is_pinned_for_glaeda_disk_drain(self) -> None:
        """A change to the hook must append its sha256 to glaeda-disk's CMUX_NEXT_DRAIN_HOOK_SHA256, or drain
        refuses every runner that installs it (#1550, #1561). Running this suite on a checkout pins it; under CI
        a missing pin fails here, next to the hook's own tests, with the command to run."""
        sys.path.insert(0, os.fspath(ROOT / "scripts"))
        import glaeda_hook_pin
        self.assertIsNone(glaeda_hook_pin.ensure(check=glaeda_hook_pin.under_ci()))


class HookTest(unittest.TestCase):
    """Runs the hook script itself, as the runner would, against sample event files."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_hook(self, phase: str, event_name: str | None, event_path: Path | None,
                 *extra: str, repo: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
        environ = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GLAEDA_RUNNER_TELEMETRY": "0",
                   "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(self.dir / "fleet"),
                   # the xcrun shim the toolchain check runs: the fake node's python3, never this host's real one
                   "GLAEDA_RUNNER_SHIM_PYTHON": "python3",
                   # and the fake node's xcrun for the Xcode probe, which runs xcrun by absolute path
                   "GLAEDA_RUNNER_XCRUN": os.fspath(self.dir / "jobpath" / "xcrun")}
        if event_name is not None:
            environ["GITHUB_EVENT_NAME"] = event_name
        if event_path is not None:
            environ["GITHUB_EVENT_PATH"] = os.fspath(event_path)
        if repo:
            environ["GITHUB_REPOSITORY"] = repo
        environ.update(env or {})
        return subprocess.run([sys.executable, os.fspath(HOOK), phase, *extra], env=environ,
                              capture_output=True, text=True, timeout=60, check=False)

    def test_sample_events(self) -> None:
        for name, (event_name, payload, admitted) in SAMPLE_EVENTS.items():
            with self.subTest(name):
                path = event(self.dir, name, payload)
                repo = (payload.get("repository") or {}).get("full_name")
                result = self.run_hook("job-started", event_name, path, "--allowed-repo", "manaflow-ai/cmux",
                                       "--no-disk", repo=repo)
                self.assertEqual(result.returncode == 0, admitted, result.stdout + result.stderr)
                self.assertIn("admitted" if admitted else "refused", result.stdout)

    def test_a_sparse_workspace_is_reset_before_the_checkout(self) -> None:
        """An earlier job's sparse checkout (sparse-checkout: scripts/ci) left the reused workspace sparse;
        actions/checkout's `sparse-checkout disable` then `checkout --force` failed with "not uptodate; will
        not remove from working tree" twice on cmuxs-mac-mini-6-glaeda-2 (manaflow-ai/cmux runs 38301254053,
        38308823659). job-started resets such a workspace to its object store, so checkout rebuilds it whole.
        A workspace that is not sparse is left alone."""
        git_env = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GIT_AUTHOR_NAME": "t",
                   "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

        def git(ws: Path, *args: str) -> str:
            return subprocess.run(["git", "-C", os.fspath(ws), *args], env=git_env, capture_output=True,
                                  text=True, check=True).stdout.strip()

        def workspace(name: str) -> Path:
            ws = self.dir / name / "_work" / "cmux" / "cmux"
            (ws / "scripts" / "ci").mkdir(parents=True)
            (ws / "Packages").mkdir()
            (ws / "scripts" / "ci" / "a.sh").write_text("a\n")
            (ws / "Packages" / "B.swift").write_text("b\n")
            git(ws, "init", "-q")
            git(ws, "add", "-A")
            git(ws, "commit", "-qm", "one")
            return ws

        path = event(self.dir, "push", {"repository": CMUX})
        sparse = workspace("actions-runner-glaeda-2")
        git(sparse, "sparse-checkout", "set", "scripts/ci")
        self.assertFalse((sparse / "Packages" / "B.swift").exists())
        (sparse / "scripts" / "ci" / "leftover.log").write_text("x\n")
        result = self.run_hook("job-started", "push", path, "--allowed-repo", "manaflow-ai/cmux", "--no-disk",
                               repo="manaflow-ai/cmux", env={"GITHUB_WORKSPACE": os.fspath(sparse)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("sparse", result.stdout)
        self.assertEqual(sorted(p.name for p in sparse.iterdir()), [".git"])
        self.assertNotEqual(git(sparse, "config", "--bool", "--default", "false", "core.sparseCheckout"), "true")
        git(sparse, "reset", "-q", "--hard", "HEAD")  # what actions/checkout runs next
        self.assertEqual((sparse / "Packages" / "B.swift").read_text(), "b\n")
        self.assertEqual(git(sparse, "status", "--porcelain"), "")

        full = workspace("actions-runner-glaeda-3")
        (full / ".build").mkdir()
        result = self.run_hook("job-started", "push", path, "--allowed-repo", "manaflow-ai/cmux", "--no-disk",
                               repo="manaflow-ai/cmux", env={"GITHUB_WORKSPACE": os.fspath(full)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("sparse", result.stdout)
        self.assertTrue((full / ".build").is_dir() and (full / "Packages" / "B.swift").is_file())

    def test_job_started_asks_glaeda_disk_only_about_a_workspace_past_the_bloat_minimum(self) -> None:
        """job-started runs glaeda-disk --own-runner-checkout (from /, in its own session, the workspace in its
        environment) only when the workspace's packs pass the 2 GiB bloat minimum, so an ordinary job pays one
        directory listing. A timeout kills its whole process group; nothing ever fails the job."""
        ws = self.dir / "actions-runner-glaeda-11" / "_work" / "cmux" / "cmux"
        pack = ws / ".git" / "objects" / "pack"
        pack.mkdir(parents=True)
        with (pack / "pack-1.pack").open("wb") as f:
            f.truncate(int(1.9 * 1024**3))
        runs = []

        class Proc:
            pid = 4242

            def __init__(self, argv, **kw):
                runs.append((argv, kw.get("cwd"), kw.get("start_new_session")))

            def communicate(self, timeout=None):
                return "measuring\nremoved it\n", ""
        disk = self.dir / "glaeda-disk"
        disk.write_text("")
        with mock.patch.object(hook.subprocess, "Popen", Proc), mock.patch.object(hook.os, "killpg"):
            self.assertEqual(hook.evict_own_checkout(os.fspath(ws), os.fspath(disk)), "")
            self.assertEqual(hook.evict_own_checkout(None, os.fspath(disk)), "")
            self.assertEqual(hook.evict_own_checkout(os.fspath(self.dir / "none"), os.fspath(disk)), "")
            self.assertEqual(runs, [])
            with (pack / "pack-2.pack").open("wb") as f:
                f.truncate(int(0.2 * 1024**3))
            self.assertEqual(hook.evict_own_checkout(os.fspath(ws), os.fspath(disk)), "removed it")
        self.assertEqual(runs, [([sys.executable, os.fspath(disk), "--own-runner-checkout"], "/", True)])

        class Slow(Proc):
            calls = 0

            def communicate(self, timeout=None):
                Slow.calls += 1
                if Slow.calls == 1:
                    raise subprocess.TimeoutExpired("glaeda-disk", timeout)
                return "", ""
        killed = []
        with mock.patch.object(hook.subprocess, "Popen", Slow), \
                mock.patch.object(hook.os, "killpg", side_effect=lambda pid, sig: killed.append((pid, sig))):
            self.assertIn("timed out", hook.evict_own_checkout(os.fspath(ws), os.fspath(disk), timeout=1))
        self.assertEqual(killed, [(4242, hook.signal.SIGKILL)])
        with mock.patch.object(hook.subprocess, "Popen", side_effect=OSError("gone")):
            self.assertEqual(hook.evict_own_checkout(os.fspath(ws), os.fspath(disk)), "")

    def test_a_workspace_a_failed_checkout_left_is_reset_before_the_next(self) -> None:
        """glaeda-2 kept failing cmux-tui checkouts after the sparse job had gone (op-restore-sqlite: three
        runs on 2026-10-12, missing scripts/install-zig-ci.sh, "not uptodate"): a failed checkout leaves
        skip-worktree entries with no sparse setting, or tracked files that differ from HEAD. job-started
        resets those too."""
        git_env = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GIT_AUTHOR_NAME": "t",
                   "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

        def git(ws: Path, *args: str) -> str:
            return subprocess.run(["git", "-C", os.fspath(ws), *args], env=git_env, capture_output=True,
                                  text=True, check=True).stdout.strip()

        def workspace(name: str) -> Path:
            ws = self.dir / name / "_work" / "cmux" / "cmux"
            (ws / "scripts").mkdir(parents=True)
            (ws / "scripts" / "install-zig-ci.sh").write_text("z\n")
            (ws / "scripts" / "other.sh").write_text("o\n")
            git(ws, "init", "-q")
            git(ws, "add", "-A")
            git(ws, "commit", "-qm", "one")
            return ws

        path = event(self.dir, "push", {"repository": CMUX})
        skipped = workspace("actions-runner-glaeda-2")
        git(skipped, "update-index", "--skip-worktree", "scripts/install-zig-ci.sh")
        (skipped / "scripts" / "install-zig-ci.sh").unlink()
        missing = workspace("actions-runner-glaeda-3")
        (missing / "scripts" / "install-zig-ci.sh").unlink()
        for ws in (skipped, missing):
            with self.subTest(ws.parts[-4]):
                result = self.run_hook("job-started", "push", path, "--allowed-repo", "manaflow-ai/cmux",
                                       "--no-disk", repo="manaflow-ai/cmux", env={"GITHUB_WORKSPACE": os.fspath(ws)})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("reset the", result.stdout)
                self.assertEqual(sorted(p.name for p in ws.iterdir()), [".git"])
                git(ws, "reset", "-q", "--hard", "HEAD")  # what actions/checkout runs next
                self.assertEqual((ws / "scripts" / "install-zig-ci.sh").read_text(), "z\n")
                self.assertEqual(git(ws, "ls-files", "-v", "scripts/install-zig-ci.sh"), "H scripts/install-zig-ci.sh")

    def test_fails_closed_without_event(self) -> None:
        self.assertEqual(self.run_hook("job-started", None, None, "--no-disk").returncode, 1)
        self.assertEqual(self.run_hook("job-started", "push", self.dir / "missing.json", "--no-disk").returncode, 1)
        bad = self.dir / "bad.json"
        bad.write_text("{not json")
        self.assertEqual(self.run_hook("job-started", "push", bad, "--no-disk").returncode, 1)

    def test_org_scope_uses_owner(self) -> None:
        path = event(self.dir, "push", {"repository": CMUX})
        self.assertEqual(self.run_hook("job-started", "push", path, "--allowed-owner", "manaflow-ai",
                                       "--no-disk", repo="manaflow-ai/cmux").returncode, 0)
        self.assertEqual(self.run_hook("job-started", "push", path, "--allowed-owner", "manaflow-ai",
                                       "--no-disk", repo="other/cmux").returncode, 1)

    def test_trusted_ref_admits_only_that_branchs_own_jobs(self) -> None:
        repo = {**CMUX, "default_branch": "main"}
        other = {"full_name": "manaflow-ai/scratch", "fork": False, "default_branch": "main"}
        main = {"ref": "refs/heads/main", "repository": repo}
        pr = {"number": 1, "head": {"repo": {**CMUX, "fork": False}}, "base": {"repo": CMUX}}
        cases = {
            "push to main": ("push", "refs/heads/main", main, "manaflow-ai/cmux", True),
            "schedule": ("schedule", "refs/heads/main", {"repository": repo}, "manaflow-ai/cmux", True),
            "dispatch on main (inputs can name any ref)": (
                "workflow_dispatch", "refs/heads/main", {"ref": "refs/heads/main", "repository": repo,
                                                         "inputs": {"ref": "refs/pull/1/merge"}}, "manaflow-ai/cmux", False),
            "push to a branch": ("push", "refs/heads/pr-branch", {**main, "ref": "refs/heads/pr-branch"},
                                 "manaflow-ai/cmux", False),
            "push, ref env and payload differ": ("push", "refs/heads/main", {**main, "ref": "refs/heads/x"},
                                                 "manaflow-ai/cmux", False),
            "another org repo's main": ("push", "refs/heads/main", {**main, "repository": other},
                                        "manaflow-ai/scratch", False),
            "repo env and payload differ": ("push", "refs/heads/main", {**main, "repository": other},
                                            "manaflow-ai/cmux", False),
            "schedule on another default branch": ("schedule", "refs/heads/main",
                                                   {"repository": {**repo, "default_branch": "dev"}},
                                                   "manaflow-ai/cmux", False),
            "push carrying a pull_request": ("push", "refs/heads/main", {**main, "pull_request": pr},
                                             "manaflow-ai/cmux", False),
            "same-repo pull request": ("pull_request", "refs/pull/1/merge", {"pull_request": pr, "repository": repo},
                                       "manaflow-ai/cmux", False),
            "merge queue": ("merge_group", "refs/heads/gh-readonly-queue/main/pr-1", {"repository": repo},
                            "manaflow-ai/cmux", False),
            "no ref": ("push", None, main, "manaflow-ai/cmux", False),
        }
        for name, (event_name, ref, payload, repository, admitted) in cases.items():
            with self.subTest(name):
                path = event(self.dir, "".join(c if c.isalnum() else "-" for c in name), payload)
                result = self.run_hook("job-started", event_name, path, "--allowed-owner", "manaflow-ai",
                                       "--trusted-ref", "refs/heads/main", "--trusted-repo", "manaflow-ai/cmux",
                                       "--no-disk", repo=repository, env={"GITHUB_REF": ref} if ref else None)
                self.assertEqual(result.returncode == 0, admitted, result.stdout + result.stderr)
                if not admitted:
                    self.assertIn("refused: ", result.stdout)
        path = event(self.dir, "trusted-ref-alone", main)
        self.assertEqual(self.run_hook("job-started", "push", path, "--trusted-ref", "refs/heads/main", "--no-disk",
                                       repo="manaflow-ai/cmux", env={"GITHUB_REF": "refs/heads/main"}).returncode, 1,
                         "--trusted-ref without --trusted-repo fails closed")
        path = event(self.dir, "plain-pr", {"pull_request": pr, "repository": CMUX})
        self.assertEqual(self.run_hook("job-started", "pull_request", path, "--allowed-owner", "manaflow-ai",
                                       "--no-disk", repo="manaflow-ai/cmux").returncode, 0,
                         "without --trusted-ref a same-repo PR is admitted as before")

    def test_trusted_next_nightly_is_an_exact_opt_in(self) -> None:
        ref = "refs/heads/nightly-next"
        payload = {"ref": ref, "repository": {**CMUX, "default_branch": "main"}}
        args = {"event_name": "push", "ref": ref, "repository": "manaflow-ai/cmux", "event": payload,
                "trusted_ref": "refs/heads/main", "trusted_repo": "manaflow-ai/cmux",
                "trusted_next_nightly": True, "workflow_ref": f"manaflow-ai/cmux/.github/workflows/nightly.yml@{ref}",
                "job": "build-nightly-app", "run_attempt": "1"}
        self.assertIsNone(hook.trusted_refusal(**args))
        cases = [
            {"trusted_next_nightly": False}, {"event_name": "schedule"}, {"event_name": "workflow_dispatch"},
            {"ref": "refs/heads/feat-cmux-next"}, {"repository": "manaflow-ai/other"},
            {"event": {**payload, "ref": "refs/heads/main"}},
            {"event": {**payload, "repository": {"full_name": "manaflow-ai/other"}}},
            {"workflow_ref": None}, {"workflow_ref": "manaflow-ai/cmux/.github/workflows/nightly.yml@refs/heads/main"},
            {"workflow_ref": f"manaflow-ai/cmux/.github/workflows/seed-derived-data.yml@{ref}"},
            {"workflow_ref": f"other/cmux/.github/workflows/nightly.yml@{ref}"},
            {"job": "seed"}, {"job": None}, {"run_attempt": "2"}, {"run_attempt": None},
            {"trusted_ref": "refs/heads/feat-cmux-next"}, {"trusted_repo": "manaflow-ai/other"},
            *[{"event": {**payload, key: {}}} for key in ("pull_request", "workflow_run", "merge_group")],
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertIn("untrusted:", hook.trusted_refusal(**{**args, **overrides}))
        for event_name in ("push", "schedule"):
            self.assertIsNone(hook.trusted_refusal(**{**args, "event_name": event_name,
                "ref": "refs/heads/main", "event": {**payload, "ref": "refs/heads/main"}}))
        path = event(self.dir, "next-nightly", payload)
        environ = {"GITHUB_REF": ref, "GITHUB_WORKFLOW_REF": args["workflow_ref"],
                   "GITHUB_JOB": args["job"], "GITHUB_RUN_ATTEMPT": "1"}
        for trusted in (("--trusted-ref", "refs/heads/main", "--trusted-repo", "manaflow-ai/cmux"), ()):
            result = self.run_hook("job-started", "push", path, "--allowed-owner", "manaflow-ai",
                                   "--trusted-next-nightly", "--no-disk", *trusted,
                                   repo="manaflow-ai/cmux", env=environ)
            self.assertEqual(result.returncode == 0, bool(trusted), result.stdout + result.stderr)

    def test_dev_compile_runner_is_exact_job_and_event(self) -> None:
        workflow = "manaflow-ai/cmux/.github/workflows/cmux-next.yml@refs/heads/feat-cmux-next"
        push = {"ref": "refs/heads/feat-cmux-next", "repository": CMUX}
        args = {"event_name": "push", "ref": "refs/heads/feat-cmux-next", "repository": "manaflow-ai/cmux",
                "event": push, "workflow_ref": workflow, "job": "cmux-scheme-compile"}
        self.assertIsNone(hook.dev_compile_refusal(**args))
        dispatch = {"repository": CMUX, "inputs": {"dev_daily": "true"}}
        self.assertIsNone(hook.dev_compile_refusal(**{**args, "event_name": "workflow_dispatch", "event": dispatch}))
        # the push's Release compile, nightly-next's promote gate, is a feed compile too; a dispatch's is not
        self.assertIsNone(hook.dev_compile_refusal(**{**args, "job": "release-compile"}))
        self.assertIn("release-compile only on a feat-cmux-next push", hook.dev_compile_refusal(
            **{**args, "job": "release-compile", "event_name": "workflow_dispatch", "event": dispatch}))
        self.assertIsNotNone(hook.dev_compile_refusal(**{**args, "job": "release-compile-fast"}))
        for overrides in (
                {"job": "other"}, {"workflow_ref": workflow.replace("cmux-next.yml", "ci.yml")},
                {"event": {"ref": "refs/heads/feat-cmux-next", "repository": {"full_name": "other/cmux"}}},
                {"workflow_ref": workflow.replace("feat-cmux-next", "main")},
                {"ref": "refs/heads/main"}, {"event_name": "schedule"},
                {"event_name": "workflow_dispatch", "ref": "refs/heads/main",
                 "event": {"repository": CMUX, "inputs": {"dev_daily": "true"}}},
                {"event": {"repository": CMUX, "inputs": {"dev_daily": "false"}},
                 "event_name": "workflow_dispatch"},
                {"repository": "other/cmux"}):
            with self.subTest(overrides=overrides):
                self.assertIsNotNone(hook.dev_compile_refusal(**{**args, **overrides}))

    def test_job_tiers_are_nightly_jobs_then_dev_compiles(self) -> None:
        """FEED_FIRST: every nightly.yml job on a nightly-next or main push, schedule or dispatch is a nightly job, and
        so is a feat-cmux-next push's release-compile (nightly-next's promote gate); the DEV compiles are DEV; nothing
        a pull request runs is either, whatever its environment says."""
        for branch in ("nightly-next", "main"):
            ref = f"refs/heads/{branch}"
            args = {"event_name": "push", "ref": ref, "repository": "manaflow-ai/cmux",
                    "event": {"ref": ref, "repository": CMUX},
                    "workflow_ref": f"manaflow-ai/cmux/.github/workflows/nightly.yml@{ref}", "job": "build-nightly-app"}
            for job in ("build-nightly-app", "promote-nightly-next", "build-sign-notarize-nightly",
                        "verify-signed-nightly", "verify-final-dmg-nightly"):
                with self.subTest(branch=branch, job=job):
                    self.assertEqual(hook.job_tier(**{**args, "job": job}), hook.TIER_NIGHTLY)
            self.assertEqual(hook.job_tier(**{**args, "event_name": "schedule", "event": {"repository": CMUX}}),
                             hook.TIER_NIGHTLY)
            self.assertEqual(hook.job_tier(**{**args, "event_name": "workflow_dispatch",
                                              "event": {"repository": CMUX}}), hook.TIER_NIGHTLY)
            for overrides in (
                    {"job": ""}, {"event_name": "pull_request"}, {"event_name": "pull_request_target"},
                    {"repository": "other/cmux"}, {"event": {"ref": "refs/heads/other", "repository": CMUX}},
                    {"workflow_ref": args["workflow_ref"].replace("nightly.yml", "ci.yml")},
                    {"workflow_ref": "manaflow-ai/cmux/.github/workflows/nightly.yml@refs/pull/1/merge"},
                    {"ref": "refs/pull/1/merge"}, {"ref": "refs/heads/rc/1"}):
                with self.subTest(branch=branch, overrides=overrides):
                    self.assertEqual(hook.job_tier(**{**args, **overrides}), "")
        dev = {"event_name": "push", "ref": "refs/heads/feat-cmux-next", "repository": "manaflow-ai/cmux",
               "event": {"ref": "refs/heads/feat-cmux-next", "repository": CMUX},
               "workflow_ref": "manaflow-ai/cmux/.github/workflows/cmux-next.yml@refs/heads/feat-cmux-next",
               "job": "cmux-scheme-compile"}
        self.assertEqual(hook.job_tier(**dev), hook.TIER_DEV)
        self.assertEqual(hook.job_tier(**{**dev, "job": "release-compile"}), hook.TIER_NIGHTLY)
        daily = {**dev, "event_name": "workflow_dispatch",
                 "event": {"repository": CMUX, "inputs": {"dev_daily": "true"}}}
        self.assertEqual(hook.job_tier(**daily), hook.TIER_DEV)
        self.assertEqual(hook.job_tier(**{**dev, "job": "swift-test"}), "")
        self.assertEqual(hook.job_tier(**{**dev, "event_name": "pull_request"}), "")
        nightly = {**dev, "ref": "refs/heads/nightly-next",
                   "event": {"ref": "refs/heads/nightly-next", "repository": CMUX},
                   "workflow_ref": "manaflow-ai/cmux/.github/workflows/nightly.yml@refs/heads/nightly-next",
                   "job": "build-nightly-app"}
        for key in hook.UNTRUSTED_PAYLOAD_KEYS:  # a payload that carries a pull request is never a feed's
            for args in (dev, {**dev, "job": "release-compile"}, nightly):
                with self.subTest(key=key, job=args["job"]):
                    self.assertEqual(hook.job_tier(**{**args, "event": {**args["event"], key: {"number": 1}}}), "")

    def test_dev_compile_instance_gate_leaves_ordinary_admission_unchanged(self) -> None:
        self.fleet()
        push = event(self.dir, "dev-push", {"repository": CMUX, "ref": "refs/heads/feat-cmux-next"})
        env = {"GITHUB_REF": "refs/heads/feat-cmux-next", "GITHUB_JOB": "cmux-scheme-compile",
               "GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/cmux-next.yml@refs/heads/feat-cmux-next"}
        args = ("--allowed-repo", "manaflow-ai/cmux", "--no-disk", "--watch-pid", str(os.getpid()),
                "--state-dir", os.fspath(self.dir / "state"))
        try:
            admitted = self.run_hook("job-started", "push", push, *args, "--dev-compile-runner",
                                     repo="manaflow-ai/cmux", env=env)
            self.assertEqual(admitted.returncode, 0, admitted.stdout + admitted.stderr)
            self.finish("")
            ordinary = {**env, "GITHUB_JOB": "claude-wrapper"}
            refused = self.run_hook("job-started", "push", push, *args, "--dev-compile-runner",
                                    repo="manaflow-ai/cmux", env=ordinary)
            self.assertEqual(refused.returncode, 1, refused.stdout)
            self.assertIn("admits only cmux-scheme-compile", refused.stdout)
            neighbor = self.run_hook("job-started", "push", push, *args,
                                     repo="manaflow-ai/cmux", env=ordinary)
            self.assertEqual(neighbor.returncode, 0, neighbor.stdout + neighbor.stderr)
        finally:
            self.finish("")

    def test_a_waiting_nightly_job_holds_every_new_job_but_no_running_jobs_step(self) -> None:
        """No new PR job starts on a mini where a nightly job waits: the fast tier on its own tokens (no units)
        too. A ci-step lease inside a running job (new_job False) is not held, so it cannot deadlock the nightly
        job waiting for that job's units."""
        capacity = self.dir / "capacity"
        waiters = [{"pid": os.getpid() + 1, "since": time.time(), "wants": ["units"],
                    "who": "n2 build-nightly-app (run 9)", "tier": hook.TIER_NIGHTLY}]
        with mock.patch.object(hook, "admission_waiters", return_value=waiters):
            fast = set(hook.FAST_TOKENS)
            self.assertIn("is a nightly job and goes first", hook.fair_turn(capacity, fast, time.time(), "", False, True))
            self.assertIsNone(hook.fair_turn(capacity, fast, time.time(), "", False, False), "a ci-step lease")
            self.assertIn("goes first", hook.fair_turn(capacity, {"units"}, time.time(), hook.TIER_DEV, True, True))
            self.assertIsNone(hook.fair_turn(capacity, fast, time.time() + 1, hook.TIER_NIGHTLY, False, True),
                              "a later nightly job that takes no unit does not wait on another nightly job")

    def test_the_nightly_slot_admits_only_nightly_jobs(self) -> None:
        self.assertIsNone(hook.nightly_slot_refusal(hook.TIER_NIGHTLY))
        for tier in (hook.TIER_DEV, ""):
            self.assertIn("admits only nightly jobs", hook.nightly_slot_refusal(tier))
        self.fleet()
        push = event(self.dir, "nightly-push", {"repository": CMUX, "ref": "refs/heads/nightly-next"})
        env = {"GITHUB_REF": "refs/heads/nightly-next", "GITHUB_JOB": "build-nightly-app",
               "GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/nightly.yml@refs/heads/nightly-next"}
        args = ("--allowed-repo", "manaflow-ai/cmux", "--no-disk", "--watch-pid", str(os.getpid()),
                "--state-dir", os.fspath(self.dir / "state"))
        try:
            admitted = self.run_hook("job-started", "push", push, *args, "--nightly-runner",
                                     repo="manaflow-ai/cmux", env=env)
            self.assertEqual(admitted.returncode, 0, admitted.stdout + admitted.stderr)
            self.finish("")
            dev_push = event(self.dir, "dev-push", {"repository": CMUX, "ref": "refs/heads/feat-cmux-next"})
            ordinary = {**env, "GITHUB_REF": "refs/heads/nightly-next", "GITHUB_JOB": "swift-test",
                        "GITHUB_WORKFLOW_REF": f"{CMUX}/.github/workflows/ci.yml@refs/heads/nightly-next"}
            for name, payload, job_env in (
                    ("another workflow's job", push, ordinary),
                    ("a feat-cmux-next DEV compile", dev_push,
                     {"GITHUB_REF": "refs/heads/feat-cmux-next", "GITHUB_JOB": "cmux-scheme-compile",
                      "GITHUB_WORKFLOW_REF": f"{CMUX}/.github/workflows/cmux-next.yml@refs/heads/feat-cmux-next"})):
                with self.subTest(name):
                    refused = self.run_hook("job-started", "push", payload, *args, "--nightly-runner",
                                            repo="manaflow-ai/cmux", env=job_env)
                    self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                    self.assertIn("the nightly slot admits only nightly jobs", refused.stdout)
                    neighbor = self.run_hook("job-started", "push", payload, *args,
                                             repo="manaflow-ai/cmux", env=job_env)
                    self.assertEqual(neighbor.returncode, 0, neighbor.stdout + neighbor.stderr)
                    self.finish("")
        finally:
            self.finish("")

    def test_disk_pressure_never_fails_and_is_bounded(self) -> None:
        marker = self.dir / "disk-ran"
        slow = make_executable(self.dir / "glaeda-disk-slow", "import time, pathlib, sys\n"
                               f"pathlib.Path({os.fspath(marker)!r}).write_text(' '.join(sys.argv[1:]))\n"
                               "time.sleep(20)\n")
        broken = make_executable(self.dir / "glaeda-disk-broken", "import sys\nsys.exit(3)\n")
        push = event(self.dir, "push", {"repository": CMUX})
        start = time.monotonic()
        result = self.run_hook("job-started", "push", push, "--allowed-repo", "manaflow-ai/cmux",
                               "--disk", os.fspath(slow), repo="manaflow-ai/cmux",
                               env={"GLAEDA_RUNNER_DISK_TIMEOUT": "1"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("glaeda-disk started in the background", result.stdout)  # the queued pass, never waited on
        self.assertTrue(wait_for_path(marker))
        self.assertEqual(marker.read_text(), "--pressure --apply --top 0")
        for phase in ("job-started", "job-completed"):
            result = self.run_hook(phase, "push", push, "--allowed-repo", "manaflow-ai/cmux",
                                   "--disk", os.fspath(broken), repo="manaflow-ai/cmux")
            self.assertEqual(result.returncode, 0, result.stdout)
        # job-completed never fails, even with no event at all
        self.assertEqual(self.run_hook("job-completed", None, None, "--disk", os.fspath(broken)).returncode, 0)

    def test_refused_job_does_not_touch_disk(self) -> None:
        marker = self.dir / "disk-ran"
        disk = make_executable(self.dir / "glaeda-disk", f"import pathlib\npathlib.Path({os.fspath(marker)!r}).touch()\n")
        name, payload, _ = SAMPLE_EVENTS["fork-pr"]
        result = self.run_hook("job-started", name, event(self.dir, "fork", payload), "--allowed-repo",
                               "manaflow-ai/cmux", "--disk", os.fspath(disk), repo="manaflow-ai/cmux")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(marker.exists())

    def test_a_refused_job_skips_the_disk_pass_when_it_completes(self) -> None:
        """A refusal fails the job in about a second; glaeda-disk in job-completed kept its runner 30 s more."""
        marker = self.dir / "disk-ran"
        disk = make_executable(self.dir / "glaeda-disk", f"import pathlib\npathlib.Path({os.fspath(marker)!r}).touch()\n")
        state = os.fspath(self.dir / "state")
        name, payload, _ = SAMPLE_EVENTS["fork-pr"]
        result = self.run_hook("job-started", name, event(self.dir, "fork", payload), "--allowed-repo",
                               "manaflow-ai/cmux", "--disk", os.fspath(disk), "--state-dir", state,
                               "--watch-pid", "4242", repo="manaflow-ai/cmux")
        self.assertEqual(result.returncode, 1)
        other = self.run_hook("job-completed", None, None, "--disk", os.fspath(disk), "--state-dir", state,
                              "--watch-pid", "4343")
        self.assertEqual(other.returncode, 0)
        self.assertTrue(wait_for_path(marker), "another runner's job on the mini still runs its disk pass")
        marker.unlink()
        done = self.run_hook("job-completed", None, None, "--disk", os.fspath(disk), "--state-dir", state,
                             "--watch-pid", "4242")
        self.assertEqual(done.returncode, 0)
        self.assertFalse(marker.exists(), done.stdout)
        self.assertIn("refused in job-started; no disk pass", done.stdout)
        again = self.run_hook("job-completed", None, None, "--disk", os.fspath(disk), "--state-dir", state,
                              "--watch-pid", "4242")
        self.assertEqual(again.returncode, 0)
        self.assertTrue(wait_for_path(marker), "the refusal's note is used once")

    def test_job_completed_disk_pass_runs_detached_and_bounded(self) -> None:
        """job-completed held its runner up to DISK_TIMEOUT_S (120 s) on glaeda-disk: "Complete runner" took 121-124 s
        on main's macOS jobs, and dependent jobs waited for it. The pass now runs detached, still bounded, by its own
        bound: nothing waits on it, and cut at DISK_TIMEOUT_S every pass on cmux14 under the floor (192-685 s whole) was
        killed holding evict.lock while the LaunchAgent's pass skipped as busy for over an hour."""
        marker = self.dir / "disk-pass"
        slow = make_executable(self.dir / "glaeda-disk-slow", "import os, pathlib, sys, time\n"
                               f"pathlib.Path({os.fspath(marker)!r}).write_text(' '.join([str(os.getpid()), "
                               "str(os.getppid()), str(os.getsid(0)), os.environ.get('RUNNER_TRACKING_ID', '-'), "
                               "*sys.argv[1:]]))\n"
                               "time.sleep(60)\n")
        start = time.monotonic()
        result = self.run_hook("job-completed", None, None, "--disk", os.fspath(slow),
                               "--state-dir", os.fspath(self.dir / "state"),
                               env={"GLAEDA_RUNNER_DISK_TIMEOUT": "1", hook.BACKGROUND_DISK_TIMEOUT_ENV: "8",
                                    "RUNNER_TRACKING_ID": "github_job"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(time.monotonic() - start, 15, "job-completed waited on glaeda-disk")
        self.assertIn("glaeda-disk started in the background", result.stdout)
        self.assertTrue(wait_for_path(marker), "the detached pass never ran")
        pid, bound, sid, tracking, *argv = marker.read_text().split()
        environ = Path(f"/proc/{bound}/environ")  # the bounding process's own environment block
        if environ.exists():
            self.assertNotIn(b"RUNNER_TRACKING_ID=", environ.read_bytes())
        elif sys.platform == "darwin":  # what the runner's orphan cleanup reads on a mini
            shown = subprocess.run(["ps", "-E", "-ww", "-o", "command=", "-p", bound], capture_output=True,
                                   text=True, check=False).stdout
            self.assertIn("python", shown.lower())
            self.assertNotIn("RUNNER_TRACKING_ID=", shown)
        self.assertEqual(argv, ["--pressure", "--apply", "--top", "0"])
        self.assertEqual(tracking, "-", "the runner's orphan cleanup would kill a pass carrying RUNNER_TRACKING_ID")
        self.assertNotEqual(int(sid), os.getsid(0), "the pass must leave the runner's session")
        self.assertTrue(hook.pid_alive(int(pid)), "the pass outlives job-completed")
        time.sleep(max(0.0, start + 3 - time.monotonic()))
        self.assertTrue(hook.pid_alive(int(pid)), "the detached pass was cut at the hooks' GLAEDA_RUNNER_DISK_TIMEOUT")
        deadline = start + 20  # bounded: killed at its own 8 s bound, not left for 60 s
        while hook.pid_alive(int(pid)) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(hook.pid_alive(int(pid)), "the detached pass ran past its timeout")

    def test_job_started_triggers_the_queued_pass_and_never_waits_on_it(self) -> None:
        """job-started ran its own glaeda-disk before every job: 10-20 s of measuring that then skipped as busy."""
        marker = self.dir / "disk-pass"
        slow = make_executable(self.dir / "glaeda-disk-slow", "import pathlib, time\n"
                               f"pathlib.Path({os.fspath(marker)!r}).touch()\ntime.sleep(30)\n")
        self.fleet()
        push = event(self.dir, "push", {"repository": CMUX})
        start = time.monotonic()
        result = self.run_hook("job-started", "push", push, "--allowed-repo", "manaflow-ai/cmux", "--disk",
                               os.fspath(slow), "--state-dir", os.fspath(self.dir / "state"),
                               "--watch-pid", str(os.getpid()), repo="manaflow-ai/cmux")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("glaeda-disk started in the background", result.stdout + result.stderr)
        self.assertLess(time.monotonic() - start, 20, "job-started waited on glaeda-disk")
        self.assertTrue(wait_for_path(marker))

    def test_job_completed_disk_pass_outlasts_a_whole_pass(self) -> None:
        """By default the detached pass gets longer than the slowest whole pass measured (685 s on cmux14)."""
        self.assertGreater(hook.BACKGROUND_DISK_TIMEOUT_S, 685)
        quick = make_executable(self.dir / "glaeda-disk-quick", "import sys\n")
        result = self.run_hook("job-completed", None, None, "--disk", os.fspath(quick),
                               "--state-dir", os.fspath(self.dir / "state"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"glaeda-disk started in the background (bounded by {hook.BACKGROUND_DISK_TIMEOUT_S:g} s)",
                      result.stdout)

    # ------------------------------------------------------------ host lock, reservation, disk floor

    def fleet(self) -> Path:
        fleet = self.dir / "fleet"
        fleet.mkdir(exist_ok=True)
        (fleet / "host.lock").touch()
        return fleet

    def started(self, *extra: str, watch: int | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
        push = event(self.dir, "push", {"repository": CMUX})
        # --gui-wait 0 unless a test passes its own (the last one wins): a cmux job waits for capacity with no
        # limit by default (CAPACITY_WAIT), which the tests of refusals themselves must not sit through
        args = ["--allowed-repo", "manaflow-ai/cmux", "--no-disk", "--state-dir", os.fspath(self.dir / "state"),
                "--watch-pid", str(watch or os.getpid()), "--gui-wait", "0", *extra]
        return self.run_hook("job-started", "push", push, *args, repo="manaflow-ai/cmux", env=env)

    def lock_free(self) -> bool:
        import fcntl
        fd = os.open(self.fleet() / "host.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    def test_reservation_markers(self) -> None:
        fleet = self.fleet()
        now = int(time.time())
        v1 = {"schema": "glaeda-reservation/v1", "owner": "fleet-session", "purpose": "chromium campaign",
              "since": now - 60, "until": now + 3600}
        cases = {
            "active": (v1, False, "host reserved by fleet-session for chromium campaign until"),
            "expired": ({**v1, "until": now - 1}, True, "admitted"),
            "iso-string-until": ({**v1, "until": "2099-01-01T00:00:00Z"}, False, "until is not integer"),
            "float-until": ({**v1, "until": now + 3600.5}, False, "until is not integer"),
            "bool-until": ({**v1, "until": True}, False, "until is not integer"),
            "millisecond-until": ({**v1, "until": now * 1000}, False, "until is outside"),
            "negative-since": ({**v1, "since": -1}, False, "since is outside"),
            "missing-since": ({k: v for k, v in v1.items() if k != "since"}, False, "since is not integer"),
            "wrong-schema": ({**v1, "schema": "other/v1"}, False, "schema is not"),
            "not-json": ("{nope", False, "not JSON"),
        }
        for name, (doc, admitted, text) in cases.items():
            with self.subTest(name):
                (fleet / "reservation.json").write_text(doc if isinstance(doc, str) else json.dumps(doc))
                result = self.started()
                self.assertEqual(result.returncode == 0, admitted, result.stdout)
                self.assertIn(text, result.stdout)
                self.run_hook("job-completed", None, None, "--no-disk", "--state-dir", os.fspath(self.dir / "state"))
        (fleet / "reservation.json").unlink()
        self.assertEqual(self.started().returncode, 0)
        self.run_hook("job-completed", None, None, "--no-disk", "--state-dir", os.fspath(self.dir / "state"))

    def test_a_drain_admits_the_job_a_listener_already_took(self) -> None:
        fleet = self.fleet()
        now = int(time.time())
        drain = {"schema": "glaeda-reservation/v1", "owner": "glaeda-mini-health",
                 "purpose": "drain for a Tailscale restart: tailscale_no_route since 2026-01-01T00:00:00Z",
                 "since": now - 60, "until": now + 3600}
        cases = {
            "drain": (drain, True, "admitting the job this runner already took"),
            "other-owner": ({**drain, "owner": "fleet-session"}, False, "host reserved by fleet-session"),
            "not-a-drain": ({**drain, "purpose": "restart now"}, False, "host reserved by glaeda-mini-health"),
            "bare-drain-prefix": ({**drain, "purpose": "drain for "}, False, "host reserved by glaeda-mini-health"),
            "other-drain": ({**drain, "purpose": "drain for unrelated work"}, False,
                            "host reserved by glaeda-mini-health"),
            "expired-drain": ({**drain, "until": now - 1}, True, "admitted"),
            "invalid-drain": ({**drain, "until": "soon"}, False, "until is not integer"),
        }
        for name, (doc, admitted, text) in cases.items():
            with self.subTest(name):
                (fleet / "reservation.json").write_text(json.dumps(doc))
                result = self.started()
                self.assertEqual(result.returncode == 0, admitted, result.stdout)
                self.assertIn(text, result.stdout)
                self.run_hook("job-completed", None, None, "--no-disk", "--state-dir", os.fspath(self.dir / "state"))
        (fleet / "reservation.json").unlink()

    def test_a_drain_still_holds_the_listener(self) -> None:
        marker = self.dir / "reservation.json"
        now = int(time.time())
        marker.write_text(json.dumps({"schema": "glaeda-reservation/v1", "owner": "glaeda-mini-health",
                                      "purpose": "drain for a Tailscale restart: tailscale_no_route since x",
                                      "since": now - 5,
                                      "until": now + 600}))
        why = hook.host_held(os.fspath(self.dir / "no-host.lock"), os.fspath(marker), time.time())
        self.assertIn("host reserved by glaeda-mini-health for drain", why or "")
        self.assertIsNotNone(hook.reservation_refusal(os.fspath(marker), time.time()))
        self.assertIsNone(hook.reservation_refusal(os.fspath(marker), time.time(), taken_job=True))

    def test_marker_without_the_parser_refuses(self) -> None:
        fleet = self.fleet()
        (fleet / "reservation.json").write_text("{}")
        lonely = self.dir / "lonely"
        lonely.mkdir()
        (lonely / "glaeda-cmux-runner-hook").write_bytes(HOOK.read_bytes())
        push = event(self.dir, "push", {"repository": CMUX})
        result = subprocess.run([sys.executable, os.fspath(lonely / "glaeda-cmux-runner-hook"), "job-started",
                                 "--allowed-repo", "manaflow-ai/cmux", "--no-disk"], capture_output=True, text=True,
                                timeout=30, env={"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir),
                                                 "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(fleet), "GITHUB_EVENT_NAME": "push",
                                                 "GITHUB_EVENT_PATH": os.fspath(push),
                                                 "GITHUB_REPOSITORY": "manaflow-ai/cmux"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("glaeda_reservation.py is missing", result.stdout)

    def test_disk_floor_refuses(self) -> None:
        self.fleet()
        result = self.started("--min-free-gib", "999999999", env={hook.DISK_JOB_PEAK_ENV: "10000"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("GiB required", result.stdout)
        self.assertNotIn("disk floor", result.stdout, "--no-disk: no eviction either")
        self.assertTrue(self.lock_free())

    def test_the_gui_and_session_runners_refuse_under_the_floor_a_cmux_next_tour_needs(self) -> None:
        """job-started holds the runners that take cmux-next tours to cmux's tour-requirements.json floor (vendored
        as CMUX_TOUR_REQUIREMENTS), above a lower --min-free-gib, and leaves the other runners on theirs."""
        self.fleet()
        tour = hook.CMUX_TOUR_REQUIREMENTS["min_free_gib"]
        push = event(self.dir, "push", {"repository": CMUX})
        env = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GLAEDA_RUNNER_TELEMETRY": "0",
               "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(self.dir / "fleet"),
               "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": os.fspath(push), "GITHUB_REPOSITORY": "manaflow-ai/cmux"}
        for role, floor in (([], min(tour - 10, hook.DISK_JOB_PEAK_GIB)), (["--gui-runner"], tour),
                            (["--session-runner"], tour)):
            with self.subTest(role=role):
                floors: list[float] = []
                with mock.patch.dict(os.environ, env, clear=True), \
                        mock.patch.object(hook, "job_owner", return_value=None), \
                        mock.patch.object(hook, "start_runner_parent_watchdog"), \
                        mock.patch.object(hook, "disk_refusal",
                                          side_effect=lambda _path, gib: floors.append(gib) or "1.0 GiB free"), \
                        contextlib.redirect_stdout(io.StringIO()):
                    code = hook.main(["job-started", "--allowed-repo", "manaflow-ai/cmux", "--no-disk",
                                      "--reservation", os.fspath(self.dir / "fleet" / "reservation.json"),
                                      "--host-lock", os.fspath(self.dir / "fleet" / "host.lock"),
                                      "--min-free-gib", f"{tour - 10:g}", "--state-dir", os.fspath(self.dir / "state"),
                                      "--watch-pid", str(os.getpid()), "--gui-wait", "0", *role])
                self.assertEqual(code, 1)
                self.assertEqual(floors, [floor])

    def test_disk_floor_evicts_before_refusing(self) -> None:
        self.fleet()
        marker = self.dir / "disk-ran"
        disk = make_executable(self.dir / "glaeda-disk-fake", "import pathlib, sys\n"
                               f"pathlib.Path({os.fspath(marker)!r}).write_text(' '.join(sys.argv[1:]))\n")
        push = event(self.dir, "push", {"repository": CMUX})
        result = self.run_hook("job-started", "push", push, "--allowed-repo", "manaflow-ai/cmux", "--disk",
                               os.fspath(disk), "--min-free-gib", "999999999", "--state-dir",
                               os.fspath(self.dir / "state"), "--watch-pid", str(os.getpid()), repo="manaflow-ai/cmux",
                               env={hook.DISK_JOB_PEAK_ENV: "10000"})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertRegex(result.stdout, r"under the 1e\+09 GiB disk floor: glaeda-disk exited 0, freed -?[0-9.]+ GiB\n")
        self.assertEqual(marker.read_text(), "--pressure --apply --top 0 --floor-gib 1e+09",
                         "told the floor, so it does not wait for the job that holds this runner")
        self.assertLess(result.stdout.index("disk floor"), result.stdout.index("refused"), "evicted, then refused")
        self.assertIn("GiB required", result.stdout)
        self.assertTrue(self.lock_free())
        # the runner hooks already carry this floor, which glaeda-disk reads itself: no --floor-gib, so a
        # glaeda-disk from before it still runs
        own = self.dir / "actions-runner" / hook.RUNNER_HOOK_SCRIPT
        own.parent.mkdir(parents=True)
        own.write_text("exec python3 /hook job-started --min-free-gib 999999999\n")
        result = self.run_hook("job-started", "push", push, "--allowed-repo", "manaflow-ai/cmux", "--disk",
                               os.fspath(disk), "--min-free-gib", "999999999", "--state-dir",
                               os.fspath(self.dir / "state"), "--watch-pid", str(os.getpid()), repo="manaflow-ai/cmux",
                               env={hook.DISK_JOB_PEAK_ENV: "10000"})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(marker.read_text(), "--pressure --apply --top 0")

    def hold_evict_lock(self, seconds: float) -> subprocess.Popen:
        """A glaeda-disk --apply as job-completed's detached pass leaves it: holding evict.lock under this HOME."""
        lock = self.dir / ".cache" / "glaeda-disk" / "evict.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time\n"
                                   f"handle = open({os.fspath(lock)!r}, 'a')\n"
                                   "fcntl.flock(handle, fcntl.LOCK_EX)\nprint('held', flush=True)\n"
                                   f"time.sleep({seconds})\n"], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), "held")
        return holder

    def test_floor_eviction_waits_for_a_running_pass_then_runs_its_own(self) -> None:
        """job-completed's pass now runs detached, so the runner's next job-started can find it still holding
        evict.lock; a pass of its own would skip as busy and the job be refused while space is being freed."""
        self.fleet()
        marker = self.dir / "disk-ran"
        disk = make_executable(self.dir / "glaeda-disk-fake", "import pathlib, sys, time\n"
                               f"pathlib.Path({os.fspath(marker)!r}).write_text(str(time.time()))\n")
        self.hold_evict_lock(3)
        released = time.time() + 2  # the holder sleeps 3 s after it printed; allow a second of slack
        push = event(self.dir, "push", {"repository": CMUX})
        result = self.run_hook("job-started", "push", push, "--allowed-repo", "manaflow-ai/cmux", "--disk",
                               os.fspath(disk), "--min-free-gib", "999999999", "--state-dir",
                               os.fspath(self.dir / "state"), "--watch-pid", str(os.getpid()), repo="manaflow-ai/cmux",
                               env={hook.DISK_JOB_PEAK_ENV: "10000"})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertRegex(result.stdout, r"disk floor: waited [0-9]+ s for a running glaeda-disk pass; "
                                        r"glaeda-disk exited 0, freed")
        self.assertGreater(float(marker.read_text()), released, "its own pass ran only once the lock was free")
        self.assertIn("GiB required", result.stdout)

    def test_floor_eviction_skips_its_own_pass_when_the_running_one_freed_enough(self) -> None:
        self.hold_evict_lock(1)
        with mock.patch.dict(os.environ, {"HOME": os.fspath(self.dir)}), \
                mock.patch.object(hook, "disk_refusal", return_value=None) as refusal, \
                mock.patch.object(hook, "disk_pressure_run") as pressure:
            note = hook.evict(os.fspath(self.dir), 100, None)
        pressure.assert_not_called()
        refusal.assert_called_once_with(os.fspath(self.dir), 100)
        self.assertRegex(note, r"under the 100 GiB disk floor: waited [0-9]+ s for a running glaeda-disk pass, freed")

    def test_floor_eviction_waits_for_a_running_pass_at_most_its_budget(self) -> None:
        self.hold_evict_lock(60)
        start = time.monotonic()
        with mock.patch.dict(os.environ, {"HOME": os.fspath(self.dir)}), \
                mock.patch.object(hook, "EVICT_TIMEOUT_S", 1), \
                mock.patch.object(hook, "disk_pressure_run", return_value=("glaeda-disk exited 0", False)) as pressure:
            note = hook.evict(os.fspath(self.dir), 999999999, None)
        self.assertLess(time.monotonic() - start, 10)
        self.assertRegex(note, r"waited [0-9]+ s for a running glaeda-disk pass; the eviction budget is spent")
        pressure.assert_not_called()  # no run past the budget

    def test_job_started_eviction_is_bounded_by_its_own_timeout(self) -> None:
        with mock.patch.object(hook, "disk_argv", return_value=["glaeda-disk"]), \
                mock.patch.object(hook.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
                mock.patch.dict(os.environ):
            os.environ.pop("GLAEDA_RUNNER_DISK_TIMEOUT", None)
            self.assertIn("glaeda-disk exited 0", hook.evict(os.fspath(self.dir), 100, None))
        self.assertAlmostEqual(run.call_args.kwargs["timeout"], hook.EVICT_TIMEOUT_S, delta=5)
        self.assertEqual(hook.EVICT_TIMEOUT_S, 300, "longer than a typical queued pass (125-534 s, median ~250)")

    def test_floor_eviction_waits_again_while_its_own_pass_skips_as_busy(self) -> None:
        """cmuxs-mac-mini-6, 05:18Z: job-started waited out its budget behind job-completed's queued pass, then its
        own pass skipped as busy (the queue's next pass took evict.lock first), and the nightly job was refused at
        23.8 GiB while that pass freed 16 GiB. A pass that skips as busy is no eviction: it waits for that pass."""
        answers = iter([b"another eviction running; skipped (pid=1)\n", b"another eviction running; skipped\n",
                        b"eviction summary: ...\n"])
        held = iter([True, False, True, False])  # waited for a pass, then free; then a pass held it again
        frees = iter([25.0, 25.0, 25.0, 25.0, 31.0])

        with mock.patch.object(hook, "disk_argv", return_value=["glaeda-disk"]), \
                mock.patch.object(hook.subprocess, "run",
                                  side_effect=lambda *a, **k: mock.Mock(returncode=0, stdout=next(answers))) as run, \
                mock.patch.object(hook, "wait_for_running_pass", side_effect=lambda _d: next(held, False)), \
                mock.patch.object(hook, "free_gib", side_effect=lambda _p: next(frees, 31.0)), \
                mock.patch.object(hook, "EVICT_BUSY_PAUSE_S", 0.0):
            note = hook.evict(os.fspath(self.dir), 30, None)
        self.assertEqual(run.call_count, 3, "retried while busy, until a pass of its own ran")
        self.assertRegex(note, r"^under the 30 GiB disk floor: waited [0-9]+ s for a running glaeda-disk pass; "
                               r"its own pass skipped as busy 2 times; glaeda-disk exited 0, freed [0-9.-]+ GiB$")

    def test_the_eviction_budget_bounds_every_glaeda_disk_run_even_with_a_timeout_override(self) -> None:
        """Review: GLAEDA_RUNNER_DISK_TIMEOUT replaced the remaining budget, so a 299 s lock wait plus a 120 s run took
        419 s. The override can only shorten a run; job-started shares one deadline between its evictions."""
        with mock.patch.object(hook, "disk_argv", return_value=["glaeda-disk"]), \
                mock.patch.object(hook.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=b"")) as run, \
                mock.patch.object(hook, "wait_for_running_pass", return_value=False), \
                mock.patch.dict(os.environ, {"GLAEDA_RUNNER_DISK_TIMEOUT": "120"}):
            hook.evict(os.fspath(self.dir), 30, None, time.monotonic() + 5)
            self.assertLessEqual(run.call_args.kwargs["timeout"], 5)
            run.reset_mock()
            note = hook.evict(os.fspath(self.dir), 30, None, time.monotonic() + 0.5)
            run.assert_not_called()
            self.assertIn("the eviction budget is spent", note)
            os.environ["GLAEDA_RUNNER_DISK_TIMEOUT"] = "3"
            hook.evict(os.fspath(self.dir), 30, None, time.monotonic() + 60)
            self.assertLessEqual(run.call_args.kwargs["timeout"], 3, "an override still shortens a run")

    def test_one_job_at_a_time_is_admitted_under_the_runner_floor(self) -> None:
        """Review: two jobs that each see 25 GiB would both pass a 20 GiB admission floor and could together write
        past the disk. Under the runner floor a live marker of another job refuses the next; a gone job's marker is
        cleared."""
        capacity = self.dir / "capacity"
        first, second = (subprocess.Popen(["/bin/sleep", "60"]) for _ in range(2))
        for proc in (first, second):
            self.addCleanup(proc.wait)
            self.addCleanup(proc.kill)
        self.assertIsNone(hook.disk_low_turn(capacity, first.pid))
        self.assertIsNone(hook.disk_low_turn(capacity, first.pid), "its own marker is no other job")
        why = hook.disk_low_turn(capacity, second.pid)
        self.assertEqual(why, f"another job admitted under the disk floor still runs here (pid {first.pid})")
        (capacity / f"{hook.DISK_LOW}-{first.pid}").write_text("ps:Thu Jan  1 00:00:00 1970\n")
        self.assertIsNone(hook.disk_low_turn(capacity, second.pid), "a reused pid holds nothing")
        self.assertFalse((capacity / f"{hook.DISK_LOW}-{first.pid}").exists())
        second.kill()
        second.wait()
        self.assertIsNone(hook.disk_low_turn(capacity, first.pid), "the job ended: the next one's turn")

    def test_the_disk_ledger_refuses_on_unknown_ownership_and_never_blocks(self) -> None:
        """Review: an unreadable marker, or a live pid whose identity cannot be read (a timed-out ps), let a second job
        in; and the ledger's flock could block job-started without bound. Ambiguity refuses; the lock is bounded."""
        capacity = self.dir / "capacity"
        first, second = (subprocess.Popen(["/bin/sleep", "60"]) for _ in range(2))
        for proc in (first, second):
            self.addCleanup(proc.wait)
            self.addCleanup(proc.kill)
        self.assertIsNone(hook.disk_low_turn(capacity, first.pid))
        real = hook.process_identity
        with mock.patch.object(hook, "process_identity",
                               side_effect=lambda pid: None if pid == first.pid else real(pid)):
            why = hook.disk_low_turn(capacity, second.pid)
        self.assertIn(f"pid {first.pid}, identity unknown", why or "")
        self.assertTrue((capacity / f"{hook.DISK_LOW}-{first.pid}").exists(), "never removed on an unknown identity")
        (capacity / f"{hook.DISK_LOW}-{first.pid}").write_text("")
        self.assertIn("identity unknown", hook.disk_low_turn(capacity, second.pid) or "", "an empty marker refuses")
        (capacity / f"{hook.DISK_LOW}-{first.pid}").unlink()
        held = os.open(capacity / f"{hook.DISK_LOW}.lock", os.O_RDONLY)
        self.addCleanup(os.close, held)
        fcntl.flock(held, fcntl.LOCK_EX)
        begin = time.monotonic()
        with mock.patch.object(hook, "ADMISSION_WAIT_S", 0.3):
            self.assertEqual(hook.disk_low_turn(capacity, second.pid), "another admission holds the disk ledger")
        self.assertLess(time.monotonic() - begin, 5)

    def test_an_unpinned_job_never_takes_the_disk_ledgers_turn(self) -> None:
        """Review: with no pinned JobOwner, job-started guessed the parent pid and could leave a live marker before it
        refused for the owner. Without an owner it refuses under the runner floor and writes nothing."""
        self.fleet()
        push = event(self.dir, "push", {"repository": CMUX})
        env = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GLAEDA_RUNNER_TELEMETRY": "0",
               "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(self.dir / "fleet"),
               "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": os.fspath(push), "GITHUB_REPOSITORY": "manaflow-ai/cmux"}
        with mock.patch.object(hook, "free_gib", return_value=25.0), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(hook, "job_owner", return_value=None), \
                mock.patch.object(hook, "start_runner_parent_watchdog"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = hook.main(["job-started", "--allowed-repo", "manaflow-ai/cmux", "--no-disk",
                              "--reservation", os.fspath(self.dir / "fleet" / "reservation.json"),
                              "--host-lock", os.fspath(self.dir / "fleet" / "host.lock"), "--min-free-gib", "30",
                              "--capacity-dir", os.fspath(self.dir / "capacity"),
                              "--state-dir", os.fspath(self.dir / "state"), "--watch-pid", str(os.getpid()),
                              "--gui-wait", "0"])
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("is not pinned, so it cannot take the disk ledger's turn", out.getvalue())
        self.assertEqual(list((self.dir / "capacity").glob(f"{hook.DISK_LOW}-*")), [])

    def test_job_started_admits_under_the_runner_floor_only_one_job_at_a_time(self) -> None:
        self.fleet()
        args = ("--min-free-gib", "30", "--capacity-dir", os.fspath(self.dir / "capacity"))
        holder = subprocess.Popen(["/bin/sleep", "60"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        with mock.patch.object(hook, "free_gib", return_value=25.0):
            (self.dir / "capacity").mkdir(parents=True, exist_ok=True)
            self.assertIsNone(hook.disk_low_turn(self.dir / "capacity", holder.pid))
            push = event(self.dir, "push", {"repository": CMUX})
            env = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GLAEDA_RUNNER_TELEMETRY": "0",
                   "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(self.dir / "fleet"),
                   "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": os.fspath(push),
                   "GITHUB_REPOSITORY": "manaflow-ai/cmux"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(hook, "start_runner_parent_watchdog"), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                code = hook.main(["job-started", "--allowed-repo", "manaflow-ai/cmux", "--no-disk",
                                  "--reservation", os.fspath(self.dir / "fleet" / "reservation.json"),
                                  "--host-lock", os.fspath(self.dir / "fleet" / "host.lock"), *args,
                                  "--state-dir", os.fspath(self.dir / "state"), "--watch-pid", str(os.getpid()),
                                  "--gui-wait", "0"])
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn(f"25.0 GiB free, 30 GiB required: another job admitted under the disk floor still runs here "
                      f"(pid {holder.pid})", out.getvalue())

    def test_the_admission_floor_is_one_jobs_measured_disk_growth(self) -> None:
        """The runner floor (the manifest's 30 GiB) decides when to evict; a job is refused only under what one job
        has been measured to write (DISK_JOB_PEAK_GIB, max 18.7 GiB), or a tour runner's own floor."""
        self.assertEqual(hook.DISK_JOB_PEAK_GIB, 20.0)
        with mock.patch.dict(os.environ):
            os.environ.pop(hook.DISK_JOB_PEAK_ENV, None)
            self.assertEqual(hook.admit_floor_gib(30, False), 20.0)
            self.assertEqual(hook.admit_floor_gib(12, False), 12.0, "a lower runner floor stands")
            self.assertEqual(hook.admit_floor_gib(0, False), 0.0, "no floor: none")
            self.assertEqual(hook.admit_floor_gib(40, True), 40.0, "a tour runner keeps cmux's floor")
        for value, want in (("25", 25.0), ("0", 0.0), ("-1", 20.0), ("10001", 20.0), ("nan", 20.0), ("x", 20.0)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {hook.DISK_JOB_PEAK_ENV: value}):
                self.assertEqual(hook.job_disk_gib(), want)

    def test_a_completed_line_records_the_lowest_free_disk_the_job_saw(self) -> None:
        samples = hook.JobSamples({"job": "build-nightly-app"}, 8, time.time() - 60)
        samples.disk_free_start = 40.0
        split = (1.0, 0.0, 0.0, {}, {})
        for free in (40.0, 31.5, 22.25, 26.0, float("nan")):
            samples.add(None, split, 10.0, free=free)
        samples.add(None, split, 10.0, free=None)
        samples.disk_free_end = 26.0
        record = samples.summary(time.time())
        self.assertEqual((record["disk_free_start_gib"], record["disk_free_end_gib"], record["disk_free_min_gib"]),
                         (40.0, 26.0, 22.2))
        empty = hook.JobSamples({}, 8, time.time()).summary(time.time())
        self.assertIsNone(empty["disk_free_min_gib"])

    def prejob(self, frees: list[float], active: bool = True, wait_s: float = 2.0, floor: float = 30.0):
        """prejob_evict with free space read from frees (the last repeats), a pass running while active."""
        reads = iter(frees)
        last = [frees[0]]

        def free(_path: str) -> float:
            last[0] = next(reads, last[0])
            return last[0]

        with mock.patch.object(hook, "free_gib", side_effect=free), \
                mock.patch.object(hook, "disk_pressure_detached", return_value="glaeda-disk started") as started, \
                mock.patch.object(hook, "pass_active", return_value=active), \
                mock.patch.object(hook, "PREJOB_POLL_S", 0.05), mock.patch.dict(os.environ):
            os.environ.pop(hook.PREJOB_MARGIN_ENV, None)
            begin = time.monotonic()
            note = hook.prejob_evict(os.fspath(self.dir), floor, None, wait_s=wait_s)
            return note, started, time.monotonic() - begin

    def test_prejob_eviction_runs_before_the_job_and_waits_for_the_target(self) -> None:
        """Above the floor but under the floor plus PREJOB_MARGIN_GIB, the queued pass starts before the job and the
        job waits until free space reaches the target."""
        self.assertEqual(hook.PREJOB_MARGIN_GIB, 20.0)
        self.assertEqual(hook.prejob_target_gib(30), 50.0)
        self.assertEqual(hook.prejob_target_gib(0), 0.0, "a runner with no floor has no target")
        note, started, _ = self.prejob([35.0, 35.0, 35.0, 42.0, 51.0])
        started.assert_called_once_with(None)
        self.assertRegex(note, r"^under the 50 GiB pre-job target \(30 GiB floor \+ 20\): glaeda-disk started; "
                               r"waited [0-9]+ s, freed 16\.0 GiB, target reached$")

    def test_prejob_eviction_is_bounded_and_admits_above_the_floor(self) -> None:
        note, started, took = self.prejob([35.0], wait_s=0.5)
        started.assert_called_once()
        self.assertLess(took, 5, "bounded by wait_s")
        self.assertIn("target not reached; admitted above the floor", note)

    def test_prejob_eviction_stops_waiting_once_no_pass_runs(self) -> None:
        note, _, took = self.prejob([35.0], active=False, wait_s=30)
        self.assertLess(took, 5, "no pass is freeing space: the job does not wait out the bound")
        self.assertIn("target not reached", note)

    def test_prejob_eviction_does_nothing_at_the_target_under_the_floor_or_without_one(self) -> None:
        for frees, floor in (([50.0], 30.0), ([80.0], 30.0), ([29.0], 30.0), ([1.0], 0.0)):
            with self.subTest(frees=frees, floor=floor):
                note, started, _ = self.prejob(frees, floor=floor)
                self.assertIsNone(note)
                started.assert_not_called()

    def test_a_trigger_no_pass_has_served_yet_counts_as_an_active_pass(self) -> None:
        """disk_pressure_detached returns before its detached child takes the pass lock; the pending trigger it wrote
        first keeps the pre-job wait going until a pass served it."""
        with mock.patch.dict(os.environ, {"HOME": os.fspath(self.dir)}):
            state = hook.disk_state_dir()
            state.mkdir(parents=True, exist_ok=True)
            self.assertFalse(hook.pass_active())
            for name in (hook.DISK_PASS_PENDING, f"{hook.DISK_PASS_PENDING}.claimed"):
                (state / name).touch()
                self.assertTrue(hook.pass_active(), name)
                (state / name).unlink()
            self.assertFalse(hook.pass_active())

    def test_the_floor_is_checked_again_after_the_prejob_wait(self) -> None:
        """Running jobs can write the disk below the floor while this one waits: it then evicts and is refused, as
        under the floor before the wait."""
        self.fleet()
        frees = iter([35.0, 35.0, 35.0, 15.0])  # the three floor checks, then the one after the wait
        last = [35.0]

        def free(_path):
            last[0] = next(frees, last[0])
            return last[0]
        push = event(self.dir, "push", {"repository": CMUX})
        env = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GLAEDA_RUNNER_TELEMETRY": "0",
               "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(self.dir / "fleet"),
               "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": os.fspath(push), "GITHUB_REPOSITORY": "manaflow-ai/cmux"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(hook, "free_gib", side_effect=free), \
                mock.patch.object(hook, "prejob_evict", return_value="under the 50 GiB pre-job target: waited 30 s"), \
                mock.patch.object(hook, "evict", return_value="under the 30 GiB disk floor: glaeda-disk exited 0") as ev, \
                mock.patch.object(hook, "start_runner_parent_watchdog"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = hook.main(["job-started", "--allowed-repo", "manaflow-ai/cmux",
                              "--reservation", os.fspath(self.dir / "fleet" / "reservation.json"),
                              "--host-lock", os.fspath(self.dir / "fleet" / "host.lock"), "--min-free-gib", "30",
                              "--state-dir", os.fspath(self.dir / "state"), "--watch-pid", str(os.getpid())])
        self.assertEqual(code, 1, out.getvalue())
        ev.assert_called_once()
        text = out.getvalue()
        self.assertLess(text.index("pre-job target"), text.index("disk floor"))
        self.assertIn("15.0 GiB free, 20 GiB required", text, "refused under one job's measured disk growth")
        self.assertTrue(self.lock_free())

    def test_prejob_margin_override_is_bounded(self) -> None:
        for value, want in (("5", 5.0), ("0", 0.0), ("-1", 20.0), ("501", 20.0), ("nan", 20.0), ("x", 20.0)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {hook.PREJOB_MARGIN_ENV: value}):
                self.assertEqual(hook.prejob_margin_gib(), want)

    def test_job_started_evicts_to_the_prejob_target_before_admitting(self) -> None:
        """The whole hook, with the real detached pass: free space between the floor and the target starts the queued
        pass before admission, and the job waits while that pass (here a 2 s fake glaeda-disk) runs, through its
        startup, so the pass has finished by the time the job is admitted."""
        self.fleet()
        marker = self.dir / "disk-ran"
        disk = make_executable(self.dir / "glaeda-disk-fake", "import pathlib, sys, time\n"
                               "time.sleep(2)\n"
                               f"pathlib.Path({os.fspath(marker)!r}).write_text(' '.join(sys.argv[1:]))\n")
        floor = max(1.0, hook.free_gib(os.fspath(self.dir / "fleet")) - 5)  # above the floor, under floor + 20
        push = event(self.dir, "push", {"repository": CMUX})
        result = self.run_hook("job-started", "push", push, "--allowed-repo", "manaflow-ai/cmux", "--disk",
                               os.fspath(disk), "--min-free-gib", f"{floor:.1f}", "--state-dir",
                               os.fspath(self.dir / "state"), "--watch-pid", str(os.getpid()), repo="manaflow-ai/cmux")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"under the [0-9.]+ GiB pre-job target \([0-9.]+ GiB floor \+ 20\): "
                                        r"glaeda-disk started in the background \(bounded by [0-9]+ s\); waited ([2-9]|[1-9][0-9]) s")
        self.assertLess(result.stdout.index("pre-job target"), result.stdout.index("admitted"), "evicted, then admitted")
        self.assertTrue(marker.read_text().startswith("--pressure --apply"), "the pass ran before admission")
        self.run_hook("job-completed", None, None, "--no-disk", "--state-dir", os.fspath(self.dir / "state"))

    def test_lock_held_by_another_build_refuses_fast(self) -> None:
        fleet = self.fleet()
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import fcntl,os,sys,time\n"
                                   f"fd=os.open({os.fspath(fleet / 'host.lock')!r},os.O_RDWR)\n"
                                   "fcntl.flock(fd,fcntl.LOCK_EX)\nprint('held',flush=True)\ntime.sleep(30)\n"],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            start = time.monotonic()
            result = self.started()
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("another build holds the fleet host lock", result.stdout)
            self.assertLess(time.monotonic() - start, 10)
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()

    def test_job_holds_the_lock_until_completed(self) -> None:
        self.fleet()
        start = time.monotonic()
        result = self.started()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("holding the fleet host lock", result.stdout)
        self.assertLess(time.monotonic() - start, 10, "the holder must not keep the hook's output open")
        self.assertFalse(self.lock_free())
        done = self.run_hook("job-completed", None, None, "--no-disk", "--state-dir", os.fspath(self.dir / "state"))
        self.assertIn("released the fleet host lock", done.stdout)
        self.assertTrue(self.lock_free())

    def test_lock_frees_itself_when_the_job_dies(self) -> None:
        self.fleet()
        worker = subprocess.Popen(["/bin/sleep", "30"])
        try:
            self.assertEqual(self.started(watch=worker.pid).returncode, 0)
            self.assertFalse(self.lock_free())
        finally:
            worker.kill()
            worker.wait()
        deadline = time.monotonic() + 10
        while not self.lock_free() and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertTrue(self.lock_free())

    def test_holder_does_not_follow_a_reused_watcher_pid(self) -> None:
        with mock.patch.object(hook, "pid_alive", return_value=True), \
                mock.patch.object(hook, "process_identity", return_value="ps:new"):
            self.assertFalse(hook.process_alive(73305, "ps:old"))

    def test_stale_holder_file_is_not_success(self) -> None:
        self.fleet()
        state = self.dir / "state"
        state.mkdir()
        stale = hook.holder_file(state)  # keyed by RUNNER_NAME, which CI's own runner sets
        stale.write_text(f"{os.getpid()}\n")  # a live pid, but not a holder
        with mock.patch.object(hook.os, "fork", side_effect=OSError("no fork")):
            held, note = hook.take_host_lock(os.fspath(self.dir / "fleet/host.lock"), os.getpid(), state)
        self.assertFalse(held)
        self.assertIn("cannot start the lock holder", note)
        self.assertFalse(stale.exists())
        self.assertTrue(self.lock_free())

    def test_huge_until_is_described_not_raised(self) -> None:
        reservation = load("glaeda_reservation_under_test", ROOT / "scripts" / "glaeda_reservation.py")
        text = reservation.describe({"owner": "o", "purpose": "p", "since": 0, "until": 10**30})
        self.assertIn("not a representable time", text)

    # ------------------------------------------------------------ weighted capacity

    def job(self, name: str, runner: str, units: int = 4, watch: int | None = None, *extra: str,
            env: dict | None = None) -> subprocess.CompletedProcess:
        return self.started("--capacity-units", str(units), "--capacity-dir", os.fspath(self.dir / "capacity"), *extra,
                            watch=watch, env={"GITHUB_JOB": name, "RUNNER_NAME": runner, **(env or {})})

    def finish(self, runner: str) -> str:
        return self.run_hook("job-completed", None, None, "--no-disk", "--state-dir", os.fspath(self.dir / "state"),
                             env={"RUNNER_NAME": runner}).stdout

    def shared_lock_blocks_exclusive(self) -> bool:
        return not self.lock_free()

    def test_fleet_gateway_clients_wait_as_light_and_leave_compile_tokens_free(self) -> None:
        """release-compile-fast and the other compile-check gateway clients only wait on the Linux pool: they take
        light units and no persistent-dd or root, so a compile on the same mini is admitted beside them."""
        self.fleet()
        next_yml = {"GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/cmux-next.yml@refs/heads/feat-cmux-next"}
        try:
            for n, name in enumerate(("release-compile-fast", "swift-canary-fleet", "app-compile-fleet")):
                waiter = self.job(name, f"w{n}", 14, env=next_yml)
                self.assertEqual(waiter.returncode, 0, waiter.stdout + waiter.stderr)
                self.assertIn(f"2/14 units for {name} (light", waiter.stdout)
                self.assertNotIn("persistent-dd", waiter.stdout)
            compile_ = self.job("macos-compile-admission", "c0", 14)
            self.assertEqual(compile_.returncode, 0, compile_.stdout + compile_.stderr)
            self.assertIn("persistent-dd", compile_.stdout)
        finally:
            for r in ("w0", "w1", "w2", "c0"):
                self.finish(r)
        # Only cmux-next.yml's jobs: the same id in another workflow is still an unknown job, a compile.
        self.assertEqual(hook.job_class("release-compile-fast", "manaflow-ai/cmux", workflow="other.yml")[0], "compile")
        self.assertEqual(hook.job_class("release-compile-fast", "manaflow-ai/cmux", workflow="cmux-next.yml")[0], "light")

    def test_capacity_admits_by_weight_and_refuses_fast_when_full(self) -> None:
        self.fleet()
        try:
            first = self.job("macos-compile-admission", "r0", 9)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIn("holding 5/9 units+persistent-dd+root-1 for macos-compile-admission (compile", first.stdout)
            second = self.job("macos-compile-admission", "r1", 9)
            self.assertEqual(second.returncode, 1)
            self.assertIn("refused: capacity: the persistent-dd token is taken", second.stdout)
            self.assertIn("2/9 units for swift-package-tests (light", self.job("swift-package-tests", "r2", 9).stdout)
            self.assertEqual(self.job("cli-pipe-regressions", "r3", 9).returncode, 0)
            start = time.monotonic()
            full = self.job("swift-package-tests", "r4", 9)
            self.assertLess(time.monotonic() - start, 10, "a full mini refuses, it never waits")
            self.assertEqual(full.returncode, 1)
            self.assertIn("refused: capacity: 0 of 9 units free", full.stdout)
            self.assertTrue(self.shared_lock_blocks_exclusive(), "with-host-lock's LOCK_EX must wait for our jobs")
            self.assertIn("released", self.finish("r0"))
            self.assertIn("persistent-dd", self.job("macos-compile-admission", "r5", 9).stdout)
        finally:
            for runner in ("r0", "r2", "r3", "r5"):
                self.finish(runner)
        self.assertTrue(self.lock_free(), "every holder let go")

    # ------------------------------------------------------------ the fast-tier runner

    FAST_TIER = {"GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/cmux-next.yml@refs/heads/feat-cmux-next"}

    def take_units(self, n: int) -> list[int]:
        """Hold the first n general unit files, as n units of other jobs would."""
        capacity = self.dir / "capacity"
        capacity.mkdir(parents=True, exist_ok=True)
        fds = [hook.lock_file(capacity / f"unit-{slot}", fcntl.LOCK_EX) for slot in range(n)]
        self.assertNotIn(None, fds)
        for fd in fds:
            self.addCleanup(os.close, fd)
        return fds

    def test_dev_compile_wait_holds_admissions_only_while_waiting(self) -> None:
        self.fleet()
        self.take_units(12)
        capacity = self.dir / "capacity"
        waiter = None
        try:
            ordinary = self.job("swift-package-tests", "normal", 14)
            self.assertEqual(ordinary.returncode, 0, ordinary.stdout + ordinary.stderr)
            self.assertIn("holding 2/14 units", ordinary.stdout, "no cores are kept for an idle DEV instance")
            self.finish("normal")
            push = event(self.dir, "dev-wait", {"repository": CMUX, "ref": "refs/heads/feat-cmux-next"})
            environ = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GLAEDA_RUNNER_TELEMETRY": "0",
                       "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(self.dir / "fleet"),
                       "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": os.fspath(push),
                       "GITHUB_REF": "refs/heads/feat-cmux-next", "GITHUB_REPOSITORY": "manaflow-ai/cmux",
                       "GITHUB_JOB": "cmux-scheme-compile", "RUNNER_NAME": "dev",
                       "GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/cmux-next.yml@refs/heads/feat-cmux-next"}
            waiter = subprocess.Popen([sys.executable, os.fspath(HOOK), "job-started", "--allowed-repo",
                                       "manaflow-ai/cmux", "--no-disk", "--dev-compile-runner", "--state-dir",
                                       os.fspath(self.dir / "state"), "--watch-pid", str(os.getpid()),
                                       "--capacity-units", "14", "--capacity-dir", os.fspath(capacity),
                                       "--instance", "1", "--gui-wait", "6"], env=environ,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            marker = capacity / f"{hook.ADMIT_WANT}{waiter.pid}"
            self.assertTrue(wait_for_path(marker), "the DEV hook waits for shared units")
            late = self.job("swift-package-tests", "late", 14)
            self.assertEqual(late.returncode, 1, late.stdout)
            self.assertIn("dev cmux-scheme-compile has waited longer here for units", late.stdout)
            out, _ = waiter.communicate(timeout=15)
            self.assertEqual(waiter.returncode, 1, out)
            self.assertIn("refused: capacity:", out)
            self.assertFalse(marker.exists(), "the refusal releases its admission priority")
            after = self.job("swift-package-tests", "after", 14)
            self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        finally:
            if waiter is not None:
                if waiter.poll() is None:
                    waiter.kill()
                waiter.communicate()
            for runner in ("normal", "late", "dev", "after"):
                self.finish(runner)

    def test_a_fast_tier_job_on_the_fast_runner_runs_while_every_general_unit_is_taken(self) -> None:
        """cmux-next's fast-tier queued a median 38.5 min (p90 73) behind the side lane for the 5 shared units.
        On the fast runner (--fast-runner) it takes the two fast tokens instead, outside those units, and goes
        ahead of a job that waits here for general units: it needs none."""
        self.fleet()
        self.take_units(5)
        capacity = self.dir / "capacity"
        (capacity / f"{hook.ADMIT_WANT}{os.getpid()}").write_text(
            json.dumps({"since": time.time() - 30, "wants": ["units"], "who": "r9 swift-test (run 1)"}) + "\n")
        try:
            fast = self.job("fast-tier", "f0", 5, None, "--fast-runner", env=self.FAST_TIER)
            self.assertEqual(fast.returncode, 0, fast.stdout + fast.stderr)
            self.assertIn("holding 0/5 units+fast-0+fast-1 for fast-tier (fast", fast.stdout)
            second = self.job("fast-tier", "f1", 5, None, "--fast-runner", env=self.FAST_TIER)
            self.assertEqual(second.returncode, 1, second.stdout)
            self.assertIn("refused: capacity: the fast-0 token is taken", second.stdout)
        finally:
            self.finish("f0")
        self.assertTrue(hook.probe(capacity / "fast-0.token") and hook.probe(capacity / "fast-1.token"))

    def hold_gui_reserve(self, token: str = "gui") -> int:
        """Hold <token>.reserve as an idle gui runner's listener gate does (Gate.reserve)."""
        capacity = self.dir / "capacity"
        capacity.mkdir(parents=True, exist_ok=True)
        fd = hook.lock_file(capacity / f"{token}{hook.GUI_RESERVE}", fcntl.LOCK_EX)
        self.assertIsNotNone(fd)
        return fd

    def test_an_idle_gui_runner_keeps_one_unit_from_every_other_job(self) -> None:
        """cmux-next's swift test queued 1.5 to 3 h on the gui label (2026-10-21): side and root jobs filled each
        mini's units, so the gui runners' gates held them off as full. While an idle gui runner's gate holds its
        reserve, every other job leaves one unit free, and the gui runner's own job takes it, even past a side job
        waiting for units. A mini without a gui runner (no reserve held) admits as before."""
        self.fleet()
        self.take_units(12)
        reserve = self.hold_gui_reserve()
        unlocked = self.fake_ioreg(False)
        try:
            side = self.job("swift-package-tests", "s0", 14)
            self.assertEqual(side.returncode, 1, side.stdout)
            self.assertIn("refused: capacity: 2 of 14 units free, swift-package-tests (light) needs 2 and 1 is kept "
                          "for an idle reserving runner", side.stdout)
            (self.dir / "capacity" / f"{hook.ADMIT_WANT}{os.getppid()}").write_text(
                json.dumps({"since": time.time() - 30, "wants": ["units"], "who": "s9 daemon-test (run 1)"}) + "\n")
            gui = self.job("app-host-unit-tests", "g0", 14, None, "--gui-runner", env=unlocked)
            self.assertEqual(gui.returncode, 0, gui.stdout + gui.stderr)
            self.assertIn("holding 1/14 units+gui+root-1 for app-host-unit-tests (gui-step", gui.stdout)
            os.close(reserve)  # the gui runner is busy now: its gate lets the reserve go
            (self.dir / "capacity" / f"{hook.ADMIT_WANT}{os.getppid()}").unlink()
            after = self.job("swift-package-tests", "s1", 14)
            self.assertIn("refused: capacity: 1 of 14 units free, swift-package-tests (light) needs 2", after.stdout)
            self.assertNotIn("kept for an idle reserving runner", after.stdout, "no reserve held: nothing kept")
            self.finish("g0")
            self.assertEqual(self.job("swift-package-tests", "s1", 14).returncode, 0, "2 free, no reserve: admitted")
        finally:
            for runner in ("g0", "s1"):
                self.finish(runner)

    def test_an_idle_freshness_slot_keeps_a_feed_compiles_units_from_pr_jobs(self) -> None:
        """2026-10-26 23:57Z: a feat-cmux-next push's Release compile (nightly-next's promote gate) queued 30+ min on
        the shared side label behind 21 PR jobs, while both DEV slot runners were held off as full by the PR jobs
        beside them. While the slot's gate holds fresh.reserve, every other job on the mini leaves
        FRESH_RESERVE_UNITS free, and the slot's own feed compile takes them, past a PR job waiting for units."""
        self.fleet()
        self.take_units(10)
        reserve = self.hold_gui_reserve(hook.FRESH_RESERVE_TOKEN)
        self.addCleanup(os.close, reserve)
        try:
            side = self.job("swift-package-tests", "s0", 14)
            self.assertEqual(side.returncode, 1, side.stdout)
            self.assertIn("refused: capacity: 4 of 14 units free, swift-package-tests (light) needs 2 and 3 are kept "
                          "for an idle reserving runner", side.stdout)
            (self.dir / "capacity" / f"{hook.ADMIT_WANT}{os.getppid()}").write_text(
                json.dumps({"since": time.time() - 30, "wants": ["units"], "who": "s9 daemon-test (run 1)"}) + "\n")
            ref = "refs/heads/feat-cmux-next"
            push = event(self.dir, "fresh-push", {"repository": CMUX, "ref": ref})
            fresh = self.run_hook(
                "job-started", "push", push, "--allowed-repo", "manaflow-ai/cmux", "--no-disk", "--state-dir",
                os.fspath(self.dir / "state"), "--watch-pid", str(os.getpid()), "--gui-wait", "0",
                "--capacity-units", "14", "--capacity-dir", os.fspath(self.dir / "capacity"), "--dev-compile-runner",
                repo="manaflow-ai/cmux",
                env={"GITHUB_JOB": "release-compile", "RUNNER_NAME": "d0", "GITHUB_REF": ref,
                     "GITHUB_WORKFLOW_REF": f"manaflow-ai/cmux/.github/workflows/cmux-next.yml@{ref}"})
            self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
            self.assertIn("holding 3/14 units for release-compile (isolated", fresh.stdout)
        finally:
            (self.dir / "capacity" / f"{hook.ADMIT_WANT}{os.getppid()}").unlink(missing_ok=True)
            self.finish("d0")

    def test_a_freshness_slots_reserve_lent_to_its_job_is_whole_again_when_that_job_ends(self) -> None:
        """2026-10-27 05:06-05:07Z: the slot's gate let its reserve go once its job was admitted and took it again
        only on its next idle poll, so PR jobs waiting for units took the job's 4 units the moment job-completed let
        them go: the slot found the mini full (1 of 14 free) and held its listener off, and a DEV compile its
        listener took at once was refused behind an older PR waiter (run 40452488452). Lent to its job, the reserve
        keeps nothing while that job holds its units and is whole the moment the job's holder exits."""
        self.fleet()
        self.take_units(10)
        reserve_fd = self.hold_gui_reserve(hook.FRESH_RESERVE_TOKEN)
        self.addCleanup(os.close, reserve_fd)
        reserve = self.dir / "capacity" / f"{hook.FRESH_RESERVE_TOKEN}{hook.GUI_RESERVE}"
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, os, sys, time\n"
                                   "fd = os.open(sys.argv[1], os.O_RDONLY | os.O_CREAT, 0o644)\n"
                                   "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(60)\n",
                                   os.fspath(hook.lent_file(reserve))], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), "held")  # the slot's job's holder lends the reserve
        waiter = self.dir / "capacity" / f"{hook.ADMIT_WANT}{os.getppid()}"
        try:
            side = self.job("swift-package-tests", "s0", 14)
            self.assertEqual(side.returncode, 0, side.stdout + side.stderr)
            self.assertNotIn("kept for an idle reserving runner", side.stdout, "lent to a running job: nothing kept")
            self.finish("s0")
            holder.kill()
            holder.wait()
            again = self.job("swift-package-tests", "s1", 14)
            self.assertEqual(again.returncode, 1, again.stdout)
            self.assertIn(f"{hook.FRESH_RESERVE_UNITS} are kept for an idle reserving runner", again.stdout,
                          "its job ended: kept at once")
            waiter.write_text(json.dumps({"since": time.time() - 300, "wants": ["units"],
                                          "who": "s9 release-compile (run 40450610811)"}) + "\n")
            ref = "refs/heads/feat-cmux-next"
            dispatch = event(self.dir, "dev-daily", {"repository": CMUX, "ref": ref,
                                                     "inputs": {"dev_daily": "true"}})
            fresh = self.run_hook(
                "job-started", "workflow_dispatch", dispatch, "--allowed-repo", "manaflow-ai/cmux", "--no-disk",
                "--state-dir", os.fspath(self.dir / "state"), "--watch-pid", str(os.getpid()), "--gui-wait", "0",
                "--capacity-units", "14", "--capacity-dir", os.fspath(self.dir / "capacity"), "--dev-compile-runner",
                repo="manaflow-ai/cmux",
                env={"GITHUB_JOB": "cmux-scheme-compile", "RUNNER_NAME": "d0", "GITHUB_REF": ref,
                     "GITHUB_WORKFLOW_REF": f"manaflow-ai/cmux/.github/workflows/cmux-next.yml@{ref}"})
            self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
            self.assertIn("for cmux-scheme-compile (isolated", fresh.stdout, "its own reserve: no PR waiter ahead")
            self.assertTrue(hook.reserve_lent(reserve), "its admission holder lends the reserve while the job runs")
            self.finish("d0")
            self.assertFalse(hook.reserve_lent(reserve), "the job's units went back: so did the lend")
        finally:
            waiter.unlink(missing_ok=True)
            for runner in ("s0", "s1", "d0"):
                self.finish(runner)

    def slot_job(self, runner: str = "d0") -> subprocess.CompletedProcess:
        ref = "refs/heads/feat-cmux-next"
        push = event(self.dir, f"fresh-{runner}", {"repository": CMUX, "ref": ref})
        return self.run_hook(
            "job-started", "push", push, "--allowed-repo", "manaflow-ai/cmux", "--no-disk", "--state-dir",
            os.fspath(self.dir / "state"), "--watch-pid", str(os.getpid()), "--gui-wait", "0",
            "--capacity-units", "14", "--capacity-dir", os.fspath(self.dir / "capacity"), "--dev-compile-runner",
            repo="manaflow-ai/cmux",
            env={"GITHUB_JOB": "release-compile", "RUNNER_NAME": runner, "GITHUB_REF": ref,
                 "GITHUB_WORKFLOW_REF": f"manaflow-ai/cmux/.github/workflows/cmux-next.yml@{ref}"})

    def test_the_slots_holder_lends_first_lets_the_lend_go_first_and_never_releases_units_early(self) -> None:
        """Review: a lend held while the holder released units early (the sampler's dynamic release) kept the reserve
        at 0 while those units went to other jobs; and a holder that let its units go before its lend let a reader
        count them spare beside a kept-at-0 reserve. The slot's job's holder ignores release requests and lets its
        lend go before its units."""
        self.fleet()
        reserve_fd = self.hold_gui_reserve(hook.FRESH_RESERVE_TOKEN)
        self.addCleanup(os.close, reserve_fd)
        capacity = self.dir / "capacity"
        reserve = capacity / f"{hook.FRESH_RESERVE_TOKEN}{hook.GUI_RESERVE}"
        try:
            fresh = self.slot_job()
            self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
            self.assertTrue(hook.reserve_lent(reserve))
            units = hook.FRESH_RESERVE_UNITS
            taken = [slot for slot in range(14) if not hook.probe(capacity / f"unit-{slot}")]
            self.assertEqual(len(taken), units)
            main = hook.holder_file(self.dir / "state", "d0")
            main.with_name(main.name + ".release").write_text(json.dumps({"keep_units": 1}))
            time.sleep(hook.HOLDER_POLL_S * 3)
            self.assertEqual([slot for slot in range(14) if not hook.probe(capacity / f"unit-{slot}")], taken,
                             "a lent reserve's job keeps every unit it was admitted with")
        finally:
            self.finish("d0")
        self.assertFalse(hook.reserve_lent(reserve))
        self.assertTrue(all(hook.probe(capacity / f"unit-{slot}") for slot in range(14)))

    def test_the_slots_next_job_waits_briefly_for_the_previous_holders_lend(self) -> None:
        """Review: an old holder still owning the lend made the next slot job admit unlent and with early release on.
        The admission waits LEND_WAIT_S for the lend, and a slot job never releases units early either way."""
        self.fleet()
        reserve_fd = self.hold_gui_reserve(hook.FRESH_RESERVE_TOKEN)
        self.addCleanup(os.close, reserve_fd)
        reserve = self.dir / "capacity" / f"{hook.FRESH_RESERVE_TOKEN}{hook.GUI_RESERVE}"
        old = subprocess.Popen([sys.executable, "-c", "import fcntl, os, sys, time\n"
                                "fd = os.open(sys.argv[1], os.O_RDONLY | os.O_CREAT, 0o644)\n"
                                "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(1.5)\n",
                                os.fspath(hook.lent_file(reserve))], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (old.kill(), old.wait(), old.stdout.close()))
        self.assertEqual(old.stdout.readline().strip(), "held")
        try:
            fresh = self.slot_job()
            self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
            self.assertTrue(hook.reserve_lent(reserve), "the old holder let go: this job holds the lend")
        finally:
            self.finish("d0")

    def test_an_admission_rereads_the_reserve_after_its_unit_scan(self) -> None:
        """Review: a PR admission read the reserve as lent (keeping 0), the slot's job ended during its unit scan,
        and it took units the whole reserve needed. The larger of the looks before and after the scan counts."""
        self.fleet()
        self.take_units(10)
        reserve_fd = self.hold_gui_reserve(hook.FRESH_RESERVE_TOKEN)
        self.addCleanup(os.close, reserve_fd)
        reserve = self.dir / "capacity" / f"{hook.FRESH_RESERVE_TOKEN}{hook.GUI_RESERVE}"
        lend = hook.lock_file(hook.lent_file(reserve), fcntl.LOCK_EX)
        real = hook.gui_reserved
        looks = []

        def ends_mid_scan(*args, **kwargs):
            looks.append(1)
            if len(looks) == 2:  # the job ends between the two looks
                os.close(lend)
            return real(*args, **kwargs)

        taken: dict = {}
        with mock.patch.object(hook, "gui_reserved", side_effect=ends_mid_scan), \
                mock.patch.object(hook, "fair_turn", return_value=None), \
                mock.patch.object(hook, "hold", return_value=(True, "held")):
            ok, why = hook.take_capacity(os.fspath(self.dir / "fleet" / "host.lock"), self.dir / "capacity", 14,
                                         "swift-package-tests", os.getpid(), self.dir / "state", taken_out=taken)
        self.assertFalse(ok, why)
        self.assertIn(f"{hook.FRESH_RESERVE_UNITS} are kept for an idle reserving runner", why)

    def test_a_waiter_marker_with_a_since_past_float_range_is_skipped(self) -> None:
        """A marker is written by a job on this mini, so it is untrusted: a since too large for a float raised
        OverflowError out of admission_waiters (and every gate and admission that reads it)."""
        capacity = self.dir / "capacity"
        capacity.mkdir(parents=True, exist_ok=True)
        (capacity / f"{hook.ADMIT_WANT}{os.getppid()}").write_text('{"since": 1' + "0" * 400 + ', "wants": ["units"]}\n')
        self.assertEqual(hook.admission_waiters(capacity), [])
        self.assertIsNone(hook.fair_turn(capacity, {"units"}, time.time()))

    def nightly_job(self, ref: str, runner: str, job: str = "build-nightly-app") -> subprocess.CompletedProcess:
        push = event(self.dir, f"nightly-{runner}", {"repository": CMUX, "ref": ref})
        return self.run_hook(
            "job-started", "push", push, "--allowed-repo", "manaflow-ai/cmux", "--no-disk", "--state-dir",
            os.fspath(self.dir / "state"), "--watch-pid", str(os.getpid()), "--gui-wait", "0",
            "--capacity-units", "14", "--capacity-dir", os.fspath(self.dir / "capacity"), "--instance", "4",
            repo="manaflow-ai/cmux",
            env={"GITHUB_JOB": job, "RUNNER_NAME": runner, "GITHUB_REF": ref,
                 "GITHUB_WORKFLOW_REF": f"manaflow-ai/cmux/.github/workflows/nightly.yml@{ref}"})

    def test_a_waiting_nightly_job_is_admitted_next_and_keeps_no_reserve(self) -> None:
        """2026-10-27 04:27Z: a nightly-next build-nightly-app (isolated, then 4 units) waited 13+ min on a side runner
        behind PR jobs that reached the mini first, then, the oldest waiter, with 3 of 14 units free while an idle
        DEV slot (4) and an idle gui runner (1) kept their reserves. A nightly job goes ahead of every waiter, a PR
        job past FEED_JUMP_S too, and keeps no reserve; the same job from a pull request does neither."""
        self.fleet()
        self.take_units(8)
        for token in (hook.FRESH_RESERVE_TOKEN, "gui"):  # the live mini: an idle DEV slot and an idle gui runner
            self.addCleanup(os.close, self.hold_gui_reserve(token))
        marker = self.dir / "capacity" / f"{hook.ADMIT_WANT}{os.getppid()}"
        marker.write_text(json.dumps({"since": time.time() - hook.FEED_JUMP_S - 60, "wants": ["units"],
                                      "who": "s9 fast-tier (run 1)"}) + "\n")
        try:
            pr = self.nightly_job("refs/pull/1/merge", "p0")
            self.assertEqual(pr.returncode, 1, pr.stdout)
            self.assertIn("s9 fast-tier (run 1) has waited longer here for units", pr.stdout)
            marker.unlink()
            pr = self.nightly_job("refs/pull/1/merge", "p0")
            self.assertEqual(pr.returncode, 1, pr.stdout)
            self.assertIn("6 of 14 units free, build-nightly-app (isolated) needs 3 and 4 are kept", pr.stdout)
            marker.write_text(json.dumps({"since": time.time() - hook.FEED_JUMP_S - 60, "wants": ["units"],
                                          "who": "s9 fast-tier (run 1)"}) + "\n")
            nightly = self.nightly_job("refs/heads/nightly-next", "n0")
            self.assertEqual(nightly.returncode, 0, nightly.stdout + nightly.stderr)
            self.assertIn("a nightly job: it goes ahead of every other job waiting here", nightly.stdout)
            self.assertIn("holding 3/14 units for build-nightly-app (isolated", nightly.stdout)
        finally:
            marker.unlink(missing_ok=True)
            self.finish("n0")

    def test_no_other_job_starts_while_a_nightly_job_waits(self) -> None:
        """A PR job that arrives while a nightly job waits here does not start, though its units are free, and the
        listener gate holds every runner whose next job takes units, the gui runner and the freshness slot too.
        Nothing running is touched."""
        self.fleet()
        marker = self.dir / "capacity" / f"{hook.ADMIT_WANT}{os.getppid()}"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"since": time.time(), "wants": ["units"], "who": "n9 build-nightly-app (run 1)",
                                      "tier": hook.TIER_NIGHTLY}) + "\n")
        try:
            pr = self.job("swift-package-tests", "s0", 14)
            self.assertEqual(pr.returncode, 1, pr.stdout)
            self.assertIn("n9 build-nightly-app (run 1) is a nightly job and goes first here for units", pr.stdout)
            for scope in (hook.RunnerScope(units=14), hook.RunnerScope(units=14, gui=True),
                          hook.RunnerScope(units=14, fresh=True)):
                with self.subTest(scope=scope):
                    self.assertIn("n9 build-nightly-app (run 1) waits here for units",
                                  hook.mini_full(self.dir / "capacity", 14, scope) or "")
        finally:
            marker.unlink(missing_ok=True)
            self.finish("s0")

    def test_a_waiter_behind_another_names_the_units_it_needs(self) -> None:
        """A waiter refused by fair_turn names every kind it needs, units too, so a newcomer that fits cannot take
        its units in the poll after the waiter ahead of it is admitted (live 2026-10-27: such waiters named [])."""
        capacity = self.dir / "capacity"
        capacity.mkdir(parents=True, exist_ok=True)
        (capacity / f"{hook.ADMIT_WANT}{os.getppid()}").write_text(
            json.dumps({"since": time.time() - 60, "wants": ["units"], "who": "s9 fast-tier (run 1)"}))
        taken: dict = {}
        held, note = hook.take_capacity(os.fspath(self.dir / "no-fleet" / "host.lock"), capacity, 14,
                                        "generated-files", os.getpid(), self.dir / "state", taken_out=taken,
                                        since=time.time() - 5, klass="isolated")
        self.assertFalse(held)
        self.assertIn("has waited longer here for units", note)
        self.assertEqual(taken["wants"], ["units"])

    def test_a_gui_runner_job_costlier_than_the_reserve_still_waits_its_turn(self) -> None:
        """The reserve is one unit: a gui-label job that costs more (cmux-next's swift test, isolated at 4) takes the
        reserved unit too, but waits behind an older job for the rest (fair_turn), so no lane jumps another for
        units it never kept. A reserve never makes a job unfittable: one as large as the mini ignores it."""
        self.fleet()
        reserve = self.hold_gui_reserve()
        self.addCleanup(os.close, reserve)
        swift = {"GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/cmux-next.yml@refs/heads/feat-cmux-next"}
        marker = self.dir / "capacity" / f"{hook.ADMIT_WANT}{os.getppid()}"
        marker.write_text(json.dumps({"since": time.time() - 30, "wants": ["units"], "who": "s9 daemon-test (run 1)"})
                          + "\n")
        env = {**self.fake_ioreg(False), **swift}
        waits = self.job("swift-test", "g0", 14, None, "--gui-runner", env=env)
        self.assertEqual(waits.returncode, 1, waits.stdout)
        self.assertIn("s9 daemon-test (run 1) has waited longer here for units", waits.stdout)
        marker.unlink()
        fds = self.take_units(14)
        fast = self.job("fast-tier", "f0", 14, None, "--fast-runner", env=self.FAST_TIER)
        self.assertEqual(fast.returncode, 0, fast.stdout + fast.stderr)
        self.assertIn("holding 0/14 units+fast-0+fast-1 for fast-tier (fast", fast.stdout,
                      "the fast tier takes no unit, so it keeps none for the gui runner")
        self.finish("f0")
        for fd in fds:
            fcntl.flock(fd, fcntl.LOCK_UN)
        whole = self.job("fast-tier", "s0", 5, None, env=self.FAST_TIER)
        self.assertEqual(whole.returncode, 0, whole.stdout + whole.stderr)
        self.assertIn("holding 5/5 units for fast-tier (isolated", whole.stdout)
        self.finish("s0")
        try:
            self.take_units(10)
            ok = self.job("swift-test", "g0", 14, None, "--gui-runner", env=env)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            self.assertIn("holding 4/14 units for swift-test (isolated", ok.stdout, "its own reserve is its to take")
        finally:
            self.finish("g0")

    def test_a_fast_tier_job_on_a_side_runner_still_costs_its_general_units(self) -> None:
        self.fleet()
        try:
            side = self.job("fast-tier", "s0", 14, None, env=self.FAST_TIER)
            self.assertEqual(side.returncode, 0, side.stdout + side.stderr)
            self.assertIn("holding 5/14 units for fast-tier (isolated", side.stdout)
            self.assertTrue(hook.probe(self.dir / "capacity/fast-0.token"))
        finally:
            self.finish("s0")
        self.take_units(10)
        full = self.job("fast-tier", "s1", 14, None, env=self.FAST_TIER)
        self.assertEqual(full.returncode, 1, full.stdout)
        self.assertIn("refused: capacity: 4 of 14 units free, fast-tier (isolated) needs 5", full.stdout)

    def test_the_fast_runner_refuses_every_other_job(self) -> None:
        """The fast runner keeps listening on a full mini (it needs no general units), so another job that
        reached it would wait there for units with no limit and idle the runner. It is refused instead, before
        taking anything, and the fast tokens stay free."""
        self.fleet()
        capacity = self.dir / "capacity"
        cases = (("swift-package-tests", {}),
                 ("release-compile", self.FAST_TIER),
                 ("fast-tier", {"GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/other.yml@refs/heads/main"}))
        for index, (job, env) in enumerate(cases):
            with self.subTest(job):
                runner = f"n{index}"
                try:
                    started = self.job(job, runner, 5, None, "--fast-runner", env=env)
                    self.assertEqual(started.returncode, 1, started.stdout + started.stderr)
                    self.assertIn("refused: the fast runner takes only the fast tier", started.stdout)
                    self.assertTrue(hook.probe(capacity / "fast-0.token") and hook.probe(capacity / "fast-1.token"))
                finally:
                    self.finish(runner)

    def test_job_log_records_started_refused_and_completed(self) -> None:
        self.fleet()
        log = self.dir / "Library/Logs/glaeda-cmux-jobs.jsonl"
        gh = {"GLAEDA_RUNNER_TELEMETRY": "1", "GITHUB_RUN_ID": "987", "GITHUB_RUN_ATTEMPT": "2",
              "GITHUB_WORKFLOW": "CI", "GITHUB_REF": "refs/pull/5/merge", "GITHUB_HEAD_REF": "feat-x",
              "GITHUB_SHA": "a" * 40}
        try:
            first = self.job("macos-compile-admission", "e0", 14, None, "--instance", "1", env=gh)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            second = self.job("macos-compile-admission", "e1", 14, None, env=gh)
            self.assertEqual(second.returncode, 1, second.stdout)
        finally:
            self.run_hook("job-completed", None, None, "--no-disk", "--state-dir", os.fspath(self.dir / "state"),
                          env={"RUNNER_NAME": "e0", "GLAEDA_RUNNER_TELEMETRY": "1"})
        records = [json.loads(line) for line in log.read_text().splitlines()]
        by_event = {r["event"]: r for r in records}
        self.assertEqual(sorted(by_event), ["completed", "refused", "started"])
        started, refused, completed = by_event["started"], by_event["refused"], by_event["completed"]
        for record in records:
            self.assertEqual(record["schema"], "glaeda-cmux-job/v1")
            self.assertEqual((record["run_id"], record["run_attempt"], record["workflow"], record["ref"],
                              record["head_ref"], record["sha"], record["repository"], record["job"]),
                             ("987", "2", "CI", "refs/pull/5/merge", "feat-x", "a" * 40, "manaflow-ai/cmux",
                              "macos-compile-admission"))
            self.assertIsInstance(record["at"], int)
            self.assertIn("decision", record)
        self.assertEqual((started["runner"], started["units"], started["roots"], started["gui"],
                          started["instance"]), ("e0", 5, ["root-1"], False, 1))
        self.assertIn("holding 5/14 units+persistent-dd+root-1", started["decision"])
        self.assertIsInstance(started["wait_s"], float)
        self.assertEqual((refused["runner"], refused["units"], refused["roots"], refused["gui"]), ("e1", 0, [], False))
        self.assertIn("capacity: the persistent-dd token is taken", refused["decision"])
        self.assertIsInstance(refused["wait_s"], float)
        self.assertEqual((completed["runner"], completed["roots"], completed["units"]), ("e0", ["root-1"], 5))
        self.assertNotIn("roots_admitted", completed)
        self.assertIn("verdict", completed)

    def test_early_refusal_logs_without_a_wait(self) -> None:
        pr = event(self.dir, "pr", {"repository": {"full_name": "someone/fork"}})
        result = self.run_hook("job-started", "pull_request_target", pr, "--allowed-repo", "manaflow-ai/cmux",
                               "--no-disk", env={"GLAEDA_RUNNER_TELEMETRY": "1", "RUNNER_NAME": "e2"})
        self.assertEqual(result.returncode, 1)
        [record] = [json.loads(line) for line in
                    (self.dir / "Library/Logs/glaeda-cmux-jobs.jsonl").read_text().splitlines()]
        self.assertEqual((record["event"], record["wait_s"], record["repository"]), ("refused", None, "someone/fork"))

    def take(self, root: str, runner: str, *extra: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return self.step(["take-root", "--root", root, "--canonical-roots", "2", *extra], runner, env)

    def take_gui(self, runner: str, *extra: str) -> subprocess.CompletedProcess:
        return self.step(["take-gui", *extra], runner)

    def runner_worker(self) -> Path:
        """A real parent process named Runner.Worker (a copied /bin/sh is killed on macOS): it runs /bin/sh with
        its arguments, reports the exit status and stays up until its stdin closes."""
        worker = self.dir / "Runner.Worker"
        if not worker.exists():
            source = self.dir / "worker.c"
            source.write_text("#include <fcntl.h>\n#include <stdio.h>\n#include <sys/wait.h>\n#include <unistd.h>\n"
                              "int main(int c, char **v) {\n"
                              "  pid_t p = fork();\n"
                              "  if (p == 0) { dup2(open(\"/dev/null\", O_RDONLY), 0); execv(\"/bin/sh\", v); _exit(127); }\n"
                              "  int s = 0; waitpid(p, &s, 0);\n"
                              f"  printf(\"{WORKER_STEP_EXIT}%d\\n\", WIFEXITED(s) ? WEXITSTATUS(s) : 1);\n"
                              "  fflush(stdout); close(1); close(2);\n"
                              "  char b; while (read(0, &b, 1) > 0) {}\n"  # up until the test closes stdin
                              "  return 0; }\n")
            cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
            if cc is None or subprocess.run([cc, "-o", os.fspath(worker), os.fspath(source)],
                                            capture_output=True).returncode != 0:
                self.skipTest("no C compiler to build a Runner.Worker stand-in")
        return worker

    def step(self, argv: list[str], runner: str, env: dict | None = None) -> subprocess.CompletedProcess:
        """Run a hook phase as a job step would: under a process named Runner.Worker (its ancestor).
        A real Runner.Worker outlives the step and the root holder watches it, so the stand-in reports
        the step's exit status and then stays up until the test ends; one that exited with the step
        would let the holder release the root within HOLDER_POLL_S."""
        worker = self.runner_worker()
        cmd = " ".join(shlex.quote(a) for a in [sys.executable, os.fspath(HOOK), *argv,
                                                "--capacity-dir", os.fspath(self.dir / "capacity"),
                                                "--state-dir", os.fspath(self.dir / "state")])
        environ = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GLAEDA_RUNNER_TELEMETRY": "0", "RUNNER_NAME": runner,
                   "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(self.dir / "fleet"), **(env or {})}
        proc = subprocess.Popen([os.fspath(worker), "-c", cmd], env=environ, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.end_worker, proc)
        timer = threading.Timer(60, proc.kill)
        timer.start()
        try:
            stdout, stderr = proc.stdout.read(), proc.stderr.read()  # EOF once the step is done
        finally:
            timer.cancel()
        stdout, marker, status = stdout.rpartition(WORKER_STEP_EXIT)
        self.assertTrue(marker, f"the Runner.Worker stand-in did not report the step: {stderr}")
        return subprocess.CompletedProcess(proc.args, int(status), stdout, stderr)

    @staticmethod
    def end_worker(proc: subprocess.Popen) -> None:
        """End a Runner.Worker stand-in, as the job ending would."""
        with contextlib.suppress(OSError):
            proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()

    def test_two_roots_consumers_take_the_producers_root_in_their_step(self) -> None:
        self.fleet()
        two = ("--canonical-roots", "2", "--compile-slots", "2")
        try:
            compile_ = self.job("macos-compile-admission", "p0", 8, None, *two)
            self.assertIn("persistent-dd+root-1", compile_.stdout)
            consumer = self.job("cli-product-tests", "c0", 8, None, *two)
            self.assertEqual(consumer.returncode, 0, consumer.stdout)
            self.assertNotIn("root-", consumer.stdout.split("holding", 1)[1], "a consumer takes no root at job start")
            busy = self.take("/private/tmp/cmux-ci", "c0", "--wait", "1")
            self.assertEqual(busy.returncode, 1, busy.stderr)
            self.assertIn("still in use", busy.stderr)
            got = self.take("/private/tmp/cmux-ci-2", "c0")
            self.assertEqual((got.returncode, got.stdout.strip()), (0, "/private/tmp/cmux-ci-2"), got.stderr)
            self.assertEqual(self.take("2", "c0").returncode, 0, "a re-take of a root this job holds is a no-op")
            second = self.take("/private/tmp/cmux-ci", "c0", "--wait", "0")
            self.assertEqual(second.returncode, 2, "a second, different root could deadlock against another job")
            self.assertIn("already holds root-2", second.stderr)
            other = self.take("/private/tmp/cmux-ci-2", "c1", "--wait", "1")
            self.assertEqual(other.returncode, 1, "another job cannot take a held root")
            self.assertIn("canonical root holder(s) released", self.finish("c0"))
            self.assertEqual(self.take("/private/tmp/cmux-ci-2", "c1").returncode, 0, "released with the job")
            self.finish("c1")
            again = self.take("1", "p0")
            self.assertEqual(again.returncode, 0, "the producer re-taking its own root is a no-op")
            self.assertFalse((self.dir / "state" / "host-lock-holder-p0-root-1.pid").exists())
        finally:
            for runner in ("p0", "c0", "c1"):
                self.finish(runner)
        self.assertTrue(self.lock_free())

    def test_taking_a_root_stops_its_leftovers(self) -> None:
        # manaflow-ai/cmux 36312829569: a cancelled compile's processes outlived the runner's tree kill and its
        # job, still writing /private/tmp/cmux-ci/src when a test consumer took root-1 and ran rm -rf on it
        self.fleet()
        parent = Path(os.path.realpath(self.dir)) / "roots"  # lsof reports resolved paths
        env = {"GLAEDA_CANONICAL_ROOT_PARENT": os.fspath(parent)}
        two = ("--canonical-roots", "2", "--compile-slots", "2")
        (self.dir / "capacity").mkdir()
        for name in ("cmux-ci/src", "cmux-ci-2/derived", "cmux-ci-20"):
            (parent / name).mkdir(parents=True)

        def sleeper(*words: str) -> list[str]:  # one process, whose arguments stay what ps shows
            return [sys.executable, "-c", "import time; time.sleep(60)", *words]

        def leftover(argv: list[str], cwd: Path) -> int:
            """An orphan, like a killed step's compilers: its parent exits at once, so it is under no Runner.Worker
            even when this test runs in a GitHub Actions job."""
            started = subprocess.run([sys.executable, "-c", "import subprocess, sys; print(subprocess.Popen("
                                      "sys.argv[1:], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                                      "stderr=subprocess.DEVNULL, start_new_session=True).pid)", *argv],
                                     cwd=cwd, capture_output=True, text=True, timeout=30, check=True)
            pid = int(started.stdout)
            self.addCleanup(end, pid)
            return pid

        def end(pid: int) -> None:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)

        def alive(pid: int, gone_within: float = 0.0) -> bool:
            deadline = time.monotonic() + gone_within
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return False
                except PermissionError:
                    return True
                if time.monotonic() >= deadline:
                    return True
                time.sleep(0.05)  # a stopped orphan is reaped by its new parent, not by us

        live_path = os.fspath(parent / "cmux-ci" / "derived" / "live.o")
        # a running step of a live job that still names root 1 (a switcher that let it go keeps its arguments)
        live = subprocess.Popen([os.fspath(self.runner_worker()), "-c", shlex.join(["exec", *sleeper(live_path)])],
                                cwd=self.dir, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(live.wait)
        self.addCleanup(live.stdin.close)
        self.addCleanup(lambda: os.killpg(live.pid, signal.SIGKILL) if live.poll() is None else None)
        working = leftover(sleeper(), parent / "cmux-ci" / "src")  # a compiler working in root 1
        naming = leftover(sleeper(os.fspath(parent / "cmux-ci-2" / "derived" / "x.o")), self.dir)
        neighbour = leftover(sleeper(), parent / "cmux-ci-20")  # a name that only starts like root 2
        bystander = leftover(sleeper(os.fspath(parent / "cmux-ci")), self.dir)  # the root itself
        try:
            got = self.take("/private/tmp/cmux-ci", "c0", env=env)
            self.assertEqual((got.returncode, got.stdout.strip()), (0, "/private/tmp/cmux-ci"), got.stderr)
            self.assertIn("stopped 1 leftover process(es)", got.stderr)
            self.assertFalse(alive(working, 5), "the process working in root 1 is stopped before the step goes on")
            self.assertTrue(alive(naming), "root 2 is not this job's")
            compile_ = self.job("macos-compile-admission", "p1", 8, None, *two, "--instance", "1", env=env)
            self.assertIn("persistent-dd+root-2", compile_.stdout)
            self.assertIn("stopped 1 leftover process(es)", compile_.stdout)
            self.assertFalse(alive(naming, 5), "admission settles the root it takes as well")
            for pid in (neighbour, bystander):
                self.assertTrue(alive(pid), "neither another root's name nor the bare root path is inside it")
            self.assertIsNone(live.poll(), "a live job's step is never a leftover")
            found = subprocess.run(["/usr/bin/pgrep", "-f", live_path], capture_output=True, text=True).stdout
            self.assertTrue(found.strip(), "nor is the process it runs")
            quiet = self.take("2", "c1", "--wait", "0", env=env)
            self.assertEqual(quiet.returncode, 1, "root 2 stays p1's")
        finally:
            for runner in ("c0", "p1", "c1"):
                self.finish(runner)

    def test_one_root_consumer_restores_a_product_from_a_second_root(self) -> None:
        # manaflow-ai/cmux 36295033926: a product compiled at /private/tmp/cmux-ci-2 on a two-root mini, restored
        # on cmux7s (one root), whose consumer holds root-1 from admission; take-root refused it (exit 2)
        self.fleet()
        one = ("--canonical-roots", "1")
        try:
            consumer = self.job("tests-build-and-lag", "g0", 4, None, *one)
            self.assertIn("root-1", consumer.stdout)
            got = self.step(["take-root", "--root", "/private/tmp/cmux-ci-2", *one], "g0")
            self.assertEqual((got.returncode, got.stdout.strip()), (0, "/private/tmp/cmux-ci-2"), got.stderr)
            self.assertEqual((self.dir / "state" / "host-lock-holder-g0.roots").read_text().split(),
                             ["root-1", "root-2"], "it keeps root-1 and records root-2 beside it")
            self.assertEqual(self.step(["take-root", "--root", "1", *one], "g0").returncode, 0)
            self.assertEqual(self.step(["take-root", "--root", "2", *one], "g0").returncode, 0, "a re-take is a no-op")
            # a job holding no root waits for root-2's token, so two jobs never own its alias at once
            waited = self.step(["take-root", "--root", "2", "--wait", "1", *one], "x0")
            self.assertEqual(waited.returncode, 1, waited.stderr)
            self.assertIn("still in use", waited.stderr)
            self.finish("g0")
            self.assertEqual(self.step(["take-root", "--root", "2", *one], "x0").returncode, 0, "released with g0")
            late = self.job("tests-build-and-lag", "g1", 4, None, *one)
            self.assertIn("root-1", late.stdout)
            blocked = self.step(["take-root", "--root", "2", "--wait", "1", *one], "g1")
            self.assertEqual(blocked.returncode, 1, "and a root-1 holder waits for x0 in turn")
        finally:
            for runner in ("g0", "x0", "g1"):
                self.finish(runner)
        self.assertTrue(self.lock_free())

    def test_two_roots_unknown_jobs_are_pinned_to_root_one(self) -> None:
        self.fleet()
        two = ("--canonical-roots", "2", "--compile-slots", "2")
        try:
            self.assertIn("root-1", self.job("macos-compile-admission", "u0", 14, None, *two).stdout)
            pinned = self.job("app-host-test-rerun", "u1", 14, None, *two)
            self.assertEqual(pinned.returncode, 1, "an unknown job uses /private/tmp/cmux-ci itself")
            self.assertIn("canonical root token is taken", pinned.stdout)
            self.assertIn("root-2", self.job("macos-compile-admission", "u2", 14, None, *two).stdout)
        finally:
            for runner in ("u0", "u1", "u2"):
                self.finish(runner)

    def test_switch_swaps_a_producers_root_without_holding_two(self) -> None:
        # test-e2e's build reuses a product compiled at another root: it lets its own root go, then takes that one
        self.fleet()
        e2e = {"GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/test-e2e.yml@refs/heads/main"}
        two = ("--canonical-roots", "2", "--compile-slots", "2")
        state = self.dir / "state"
        try:
            build = self.job("build", "e0", 8, None, *two, env=e2e)
            self.assertIn("units+root-1 for build (compile-gui", build.stdout)
            self.assertTrue((state / "host-lock-holder-e0-root-1.pid").exists(), "the root has a holder of its own")
            refused = self.take("2", "e0", "--wait", "0")
            self.assertEqual(refused.returncode, 2, "without --switch a second root is still refused")
            self.assertIn("--switch", refused.stderr)
            # root 2 busy: a switch that times out keeps the old root, never leaving the job without one
            self.assertEqual(self.take("2", "b0").returncode, 0)
            late = self.take("2", "e0", "--switch", "--wait", "1")
            self.assertEqual(late.returncode, 1, late.stderr)
            self.assertEqual(self.take("1", "x0", "--wait", "0").returncode, 1, "e0 still holds root 1")
            self.assertEqual((state / "host-lock-holder-e0.roots").read_text().split(), ["root-1"])
            self.finish("b0")
            env_file = self.dir / "switch_env"
            switched = self.take("/private/tmp/cmux-ci-2", "e0", "--switch", env={"GITHUB_ENV": os.fspath(env_file)})
            self.assertEqual((switched.returncode, switched.stdout.strip()), (0, "/private/tmp/cmux-ci-2"),
                             switched.stderr)
            self.assertIn("let go of root-1", switched.stderr)
            self.assertEqual(env_file.read_text(), "CMUX_CI_CANONICAL_ROOT=/private/tmp/cmux-ci-2\n")
            self.assertEqual((state / "host-lock-holder-e0.roots").read_text().split(), ["root-2"])
            self.assertEqual(self.take("1", "c0", "--wait", "5").returncode, 0, "root 1 is free again")
            full = self.job("swift-package-tests", "g0", 2, None, *two)  # e0 still holds unit-0 and unit-1
            self.assertIn("0 of 2 units free", full.stdout, "the admission holder keeps its units")
            self.assertEqual(self.take("2", "e0").returncode, 0, "a re-take of the new root is a no-op")
            self.assertIn("canonical root holder(s) released", self.finish("e0"))
            self.assertEqual(self.take("2", "c1", "--wait", "5").returncode, 0, "released with the job")
            self.finish("c0")
            self.finish("c1")
            compile_ = self.job("macos-compile-admission", "p0", 8, None, *two)
            self.assertIn("persistent-dd+root-1", compile_.stdout)
            self.assertEqual(self.take("2", "p0", "--switch").returncode, 2,
                             "a root in the admission holder cannot be let go")
            (state / "host-lock-holder-p0-root-1.pid").write_text("999999\n")  # a killed holder's leftover
            self.assertEqual(self.take("2", "p0", "--switch").returncode, 2, "a dead holder lets nothing go")
        finally:
            for runner in ("e0", "c0", "c1", "g0", "p0", "b0", "x0"):
                self.finish(runner)
        self.assertFalse(list((self.dir / "capacity").glob("*.want-*")), "no waiter marker outlives its wait")
        self.assertTrue(self.lock_free())

    def test_take_gui_holds_the_token_for_the_rest_of_the_job(self) -> None:
        self.fleet()
        two = ("--canonical-roots", "2", "--compile-slots", "2")
        state = self.dir / "state"
        try:
            self.assertEqual(self.job("tests-build-and-lag", "g0", 8, None, *two).returncode, 0)
            self.assertTrue((state / "host-lock-holder-g0.gui").exists())
            self.assertEqual(self.take_gui("g0").returncode, 0, "a job holding gui from admission: a no-op")
            self.assertEqual(self.job("macos-compile-admission", "c0", 8, None, *two).returncode, 0)
            waited = time.monotonic()
            busy = self.take_gui("c0", "--wait", "2")
            self.assertEqual(busy.returncode, 1, busy.stderr)
            self.assertIn("still taken after 2 s", busy.stderr)
            self.assertGreaterEqual(time.monotonic() - waited, 2)
            self.finish("g0")
            self.assertEqual(self.take_gui("c0", "--wait", "5").returncode, 0)
            self.assertTrue((state / "host-lock-holder-c0-gui.pid").exists())
            refused = self.job("tests-build-and-lag", "g1", 8, None, *two, "--gui-wait", "0")
            self.assertIn("the gui token is taken", refused.stdout)
            self.assertIn("the gui token holder released", self.finish("c0"))
            self.assertFalse((state / "host-lock-holder-c0.gui").exists())
            self.assertIn("+gui", self.job("tests-build-and-lag", "g2", 8, None, *two).stdout, "released with the job")
            outside = self.run_hook("take-gui", None, None, "--capacity-dir", os.fspath(self.dir / "capacity"),
                                    "--state-dir", os.fspath(state))
            self.assertEqual(outside.returncode, 2)
        finally:
            for runner in ("g0", "c0", "g1", "g2"):
                self.finish(runner)
        self.assertTrue(self.lock_free())

    def test_a_locked_console_leaves_the_frame_pacing_bench_to_skip_itself(self) -> None:
        # The bench runs on the side-lane mini or nowhere (no rescue), and its take-gui step skips its console steps
        # when take-gui gives way. So a locked console admits it without the console (light, no gui token) and
        # take-gui gives way, where a refusal would only fail the nightly.
        self.fleet()
        state = self.dir / "state"
        bench = {"GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/cmux-next-frame-pacing.yml@refs/heads/main"}
        locked = {**self.fake_ioreg(True), **bench}
        try:
            admitted = self.job("bench", "b0", 8, None, env=locked)
            self.assertEqual(admitted.returncode, 0, admitted.stdout)
            self.assertIn("for bench (light", admitted.stdout)
            self.assertIn("console: the console session (cmux) is screen-locked", admitted.stdout)
            self.assertFalse((state / "host-lock-holder-b0.gui").exists(), "no gui token taken")
            gave = self.step(["take-gui", "--wait", "5"], "b0", env=locked)
            self.assertEqual(gave.returncode, hook.TAKE_GUI_GAVE_WAY, gave.stderr)
            self.assertIn("take-gui: console: the console session (cmux) is screen-locked", gave.stderr)
            self.finish("b0")
            refused = self.job("app-host-unit-tests", "g0", 8, None, env=locked)
            self.assertEqual(refused.returncode, 1, "other console jobs are still refused for a rescue elsewhere")
            unlocked = self.job("bench", "b1", 8, None, env={**self.fake_ioreg(False), **bench})
            self.assertEqual(unlocked.returncode, 0, unlocked.stdout)
            self.assertIn("for bench (gui-step", unlocked.stdout)
        finally:
            for runner in ("b0", "g0", "b1"):
                self.finish(runner)
        self.assertTrue(self.lock_free())

    def fake_ioreg(self, locked: bool | None) -> dict[str, str]:
        """GLAEDA_RUNNER_IOREG pointing at a script that prints a console session: locked, unlocked, or (None) at
        the login window with no user."""
        users = ('"IOConsoleUsers" = ()' if locked is None else
                 '"IOConsoleUsers" = ({"kCGSSessionOnConsoleKey"=Yes,"kCGSessionLoginDoneKey"=Yes,'
                 f'"kCGSSessionUserNameKey"="cmux"{',"CGSSessionScreenIsLocked"=Yes' if locked else ""}}})')
        script = self.dir / f"ioreg-{locked}"
        script.write_text(f"#!/bin/sh\ncat <<'X'\n+-o Root\n    {{\n      \"IOConsoleLocked\" = No\n      {users}\n    }}\nX\n")
        script.chmod(0o755)
        return {"GLAEDA_RUNNER_IOREG": os.fspath(script)}

    def test_a_locked_console_refuses_gui_jobs_before_taking_capacity(self) -> None:
        self.fleet()
        state = self.dir / "state"
        locked = self.fake_ioreg(True)
        try:
            for name in ("app-host-unit-tests", "tests-build-and-lag"):
                refused = self.job(name, "g0", 8, None, env=locked)
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn("refused: console: the console session (cmux) is screen-locked", refused.stdout)
                self.assertFalse((state / "host-lock-holder-g0.gui").exists(), "no gui token taken")
            nobody = self.job("app-host-unit-tests", "g0", 8, None, env=self.fake_ioreg(None))
            self.assertIn("refused: console: no user is logged in at the console", nobody.stdout)
            # the gui runner's label brings only console jobs, whatever their id
            side = self.job("swift-package-tests", "g0", 8, None, "--gui-runner", env=locked)
            self.assertIn("refused: console:", side.stdout)
            # and a session runner's only session-only ones, which need the logged-in session too
            session = self.job("daemon-test", "s0", 8, None, "--session-runner", env=locked)
            self.assertIn("refused: console:", session.stdout)
            off = self.job("tests-build-and-lag", "g1", 8, None, env={**locked, "GLAEDA_RUNNER_CONSOLE_GATE": "0"})
            self.assertEqual(off.returncode, 0, "the kill switch turns the check off")
            self.finish("g1")
            compile_ = self.job("macos-compile-admission", "c0", 8, None, env=locked)
            self.assertEqual(compile_.returncode, 0, "a compile needs no console session")
            self.assertEqual(self.job("swift-package-tests", "l0", 8, None, env=locked).returncode, 0)
            gave = self.step(["take-gui", "--wait", "5"], "c0", env=locked)
            self.assertEqual(gave.returncode, hook.TAKE_GUI_GAVE_WAY, "the build leaves its tests to the test job")
            self.assertIn("take-gui: console: the console session (cmux) is screen-locked", gave.stderr)
            self.assertFalse((state / "host-lock-holder-c0-gui.pid").exists())
            self.finish("c0")  # its root
            admitted = self.job("tests-build-and-lag", "g2", 8, None, env=self.fake_ioreg(False))
            self.assertEqual(admitted.returncode, 0, admitted.stdout)
            self.assertIn("+gui", admitted.stdout)
        finally:
            for runner in ("g0", "g1", "g2", "c0", "l0"):
                self.finish(runner)
        self.assertTrue(self.lock_free())

    def test_take_gui_gives_way_to_a_gui_job_waiting_for_its_root(self) -> None:
        # the opposite lock orders: a build holds root 1 and wants gui; a gui job holds gui and wants root 1
        self.fleet()
        two = ("--canonical-roots", "2", "--compile-slots", "2")
        results: dict[str, subprocess.CompletedProcess] = {}
        try:
            self.assertIn("root-1", self.job("macos-compile-admission", "c0", 8, None, *two).stdout)
            self.assertEqual(self.job("tests-build-and-lag", "g0", 8, None, *two).returncode, 0)
            waiter = threading.Thread(target=lambda: results.setdefault("g0", self.take("1", "g0", "--wait", "40")))
            waiter.start()
            deadline = time.monotonic() + 15
            while not list((self.dir / "capacity").glob("root-1.want-*")) and time.monotonic() < deadline:
                time.sleep(0.2)
            self.assertEqual([m.read_text() for m in (self.dir / "capacity").glob("root-1.want-*")], ["gui\n"])
            waited = time.monotonic()
            gave = self.take_gui("c0", "--wait", "30")
            self.assertEqual(gave.returncode, hook.TAKE_GUI_GAVE_WAY, gave.stderr)
            self.assertIn("waiting for root-1, which this job holds; giving way", gave.stderr)
            self.assertLess(time.monotonic() - waited, 15, "it gives way at once, not after its wait")
            self.finish("c0")
            waiter.join(60)
            self.assertEqual(results["g0"].returncode, 0, results["g0"].stderr)
            # a gui holder not waiting for this job's root is no cycle: take-gui keeps waiting for the token
            self.assertIn("root-2", self.job("macos-compile-admission", "c1", 8, None, *two).stdout)
            self.assertEqual(self.take_gui("c1", "--wait", "1").returncode, 1)
            # a marker its waiter stopped refreshing is stale, even if its pid is alive (recycled or foreign)
            stale = self.dir / "capacity" / f"root-2.want-{os.getpid()}"
            stale.write_text("gui\n")
            os.utime(stale, (time.time() - 60, time.time() - 60))
            self.assertEqual(self.take_gui("c1", "--wait", "1").returncode, 1, "no give-way to a stale marker")
            self.assertFalse(stale.exists(), "and it is swept")
        finally:
            for runner in ("c0", "g0", "c1"):
                self.finish(runner)
        self.assertFalse(list((self.dir / "capacity").glob("*.want-*")))
        self.assertTrue(self.lock_free())

    def test_capacity_runs_tours_side_by_side(self) -> None:
        """Tours on one mini (its session runners) each hold their own tag (cmux's media_tag_lock.py) and capture
        only their own app's window, so they run side by side as isolated jobs: units only, no tour, root,
        persistent-dd or gui token."""
        self.fleet()
        tour = {"GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/cmux-next-media.yml@refs/heads/main"}
        two = ("--canonical-roots", "2", "--compile-slots", "2")
        try:
            for runner in ("t0", "t1", "t2", "t3"):
                done = self.job("tour", runner, 8, None, *two, "--gui-wait", "0", env=tour)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertNotIn("tour token", done.stdout)
            self.assertFalse((self.dir / "capacity/tour.token").exists())
            self.assertEqual(self.job("macos-compile-admission", "c0", 8, None, *two, "--gui-wait", "0").returncode, 1,
                             "four tours take all 8 units, so a compile waits for units")
        finally:
            for runner in ("t0", "t1", "t2", "t3", "c0"):
                self.finish(runner)
        self.assertTrue(self.lock_free())

    def test_capacity_units_are_cores_and_a_job_costs_its_measured_cores(self) -> None:
        """A std mini's 14 units are its 14 cores. Each job takes the cores its p75 says it uses (CLASS_COST, else
        JOB_CORES), and a job that does not fit in the cores left waits for them."""
        self.fleet()
        two = ("--canonical-roots", "2", "--compile-slots", "2")

        def run(job: str, runner: str, workflow: str) -> subprocess.CompletedProcess:
            ref = f"manaflow-ai/cmux/.github/workflows/{workflow}@refs/heads/main"
            return self.job(job, runner, 14, None, *two, env={"GITHUB_WORKFLOW_REF": ref})

        try:
            for job, runner, workflow, holding in (
                    ("macos-compile-admission", "r0", "ci.yml", "5/14 units+persistent-dd+root-1"),
                    ("generated-files", "s0", "cmux-next.yml", "6/14 units"),
                    ("tour", "s1", "cmux-next-media.yml", "2/14 units")):
                done = run(job, runner, workflow)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn(f"holding {holding} for {job} (", done.stdout)
            waits = run("swift-test", "s2", "cmux-next.yml")
            self.assertEqual(waits.returncode, 1, "13 of 14 cores are taken: a 4-core job waits (refused at --gui-wait 0)")
            self.assertIn("capacity: 1 of 14 units free, swift-test (isolated) needs 4", waits.stdout)
            small = run("claude-wrapper", "s3", "ci.yml")
            self.assertIn("holding 1/14 units for claude-wrapper (light", small.stdout, "a 1-core job takes the last core")
            self.assertIn("0 of 14 units free", run("remote-daemon-macos-tests", "s4", "ci.yml").stdout)
            self.finish("s0")
            self.assertIn("holding 4/14 units for swift-test (isolated", run("swift-test", "s5", "cmux-next.yml").stdout,
                          "the 6 cores generated-files gave back fit it")
        finally:
            for runner in ("r0", "s0", "s1", "s3", "s5"):
                self.finish(runner)
        self.assertTrue(self.lock_free())

    def test_capacity_a_job_cost_override_keeps_its_class_tokens(self) -> None:
        """cli-regressions is an unknown job (a compile, pinned to root 1) that measures 0.16 cores: it costs 1 core
        but still holds persistent-dd and the root, so a compile still waits for those, not for cores."""
        self.fleet()
        ci = {"GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/ci.yml@refs/heads/main"}
        try:
            done = self.job("cli-regressions", "r0", 14, None, env=ci)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("holding 1/14 units+persistent-dd+root-1 for cli-regressions (compile", done.stdout)
            compile_ = self.job("macos-compile-admission", "r1", 14, None, env=ci)
            self.assertEqual(compile_.returncode, 1)
            self.assertIn("the persistent-dd token is taken", compile_.stdout)
        finally:
            for runner in ("r0", "r1"):
                self.finish(runner)

    def test_take_root_refuses_bad_roots_and_non_jobs(self) -> None:
        for bad in ("/tmp/elsewhere", "/private/tmp/cmux-ci-1", "0", "cmux-ci-2x", "02", "/private/tmp/cmux-ci-02"):
            with self.subTest(bad=bad):
                self.assertEqual(self.take(bad, "x0").returncode, 2)
        for bad in ("100", "/private/tmp/cmux-ci-100"):
            with self.subTest(bad=bad):
                self.assertEqual(self.take(bad, "x0").returncode, 2)
        nameless = self.run_hook("take-root", None, None, "--root", "1", "--state-dir", os.fspath(self.dir / "state"))
        self.assertIn("RUNNER_NAME is not set", nameless.stderr)
        if os.environ.get("GITHUB_ACTIONS"):  # CI itself runs under a real Runner.Worker, so it is "inside a job"
            return
        outside = self.run_hook("take-root", None, None, "--root", "1", "--capacity-dir",
                                os.fspath(self.dir / "capacity"), "--state-dir", os.fspath(self.dir / "state"),
                                env={"RUNNER_NAME": "x0"})
        self.assertEqual(outside.returncode, 2)
        self.assertIn("not inside a runner job", outside.stderr)


    def test_job_started_links_the_root_shim_into_the_fleet_bin(self) -> None:
        fleet = self.fleet()
        (fleet / "bin").mkdir()
        hooks = self.dir / "hooks"
        hooks.mkdir()
        shutil.copy(HOOK, hooks / "glaeda-cmux-runner-hook")
        (hooks / "glaeda-canonical-root").write_text("#!/bin/sh\n")
        environ = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GLAEDA_RUNNER_TELEMETRY": "0", "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(fleet),
                   "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": os.fspath(event(self.dir, "push", {"repository": CMUX})),
                   "GITHUB_REPOSITORY": "manaflow-ai/cmux", "GITHUB_JOB": "swift-package-tests", "RUNNER_NAME": "s0"}
        run = subprocess.run([sys.executable, os.fspath(hooks / "glaeda-cmux-runner-hook"), "job-started",
                              "--allowed-repo", "manaflow-ai/cmux", "--no-disk", "--capacity-units", "4",
                              "--capacity-dir", os.fspath(self.dir / "capacity"), "--state-dir", os.fspath(self.dir / "state"),
                              "--watch-pid", str(os.getpid())], env=environ, capture_output=True, text=True, timeout=30)
        try:
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            link = fleet / "bin" / "glaeda-canonical-root"
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), os.fspath((hooks / "glaeda-canonical-root").resolve()))
        finally:
            self.finish("s0")

    def test_capacity_one_root_job_per_canonical_root(self) -> None:
        # a consumer's rm -rf of <root>/src must never meet a compile or another consumer in that root
        self.fleet()
        env_file, temp = self.dir / "github_env", self.dir / "runner_temp"
        temp.mkdir()
        try:
            compile_ = self.job("macos-compile-admission", "k0", 14, None,
                                env={"GITHUB_ENV": os.fspath(env_file), "RUNNER_TEMP": os.fspath(temp)})
            self.assertIn("CMUX_CI_CANONICAL_ROOT=/private/tmp/cmux-ci via GITHUB_ENV+RUNNER_TEMP", compile_.stdout)
            self.assertEqual(env_file.read_text(), "CMUX_CI_CANONICAL_ROOT=/private/tmp/cmux-ci\n")
            self.assertEqual((temp / "glaeda-canonical-root").read_text(), "/private/tmp/cmux-ci\n")
            for n, consumer in enumerate(("cli-product-tests", "tests-build-and-lag", "app-host-unit-tests")):
                refused = self.job(consumer, f"k{n + 1}", 14)
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn("refused: capacity: the canonical root token is taken", refused.stdout)
            self.assertEqual(self.job("swift-package-tests", "k4", 14).returncode, 0, "light jobs take no root")
            self.finish("k0")
            self.assertIn("root-1", self.job("cli-product-tests", "k5", 14).stdout)
            two = self.job("macos-compile-admission", "k6", 14, None, "--canonical-roots", "2", "--compile-slots", "2",
                           env={"GITHUB_ENV": os.fspath(env_file)})
            self.assertIn("persistent-dd+root-2", two.stdout)
            self.assertIn("CMUX_CI_CANONICAL_ROOT=/private/tmp/cmux-ci-2 via GITHUB_ENV", two.stdout)
            self.assertTrue(env_file.read_text().endswith("CMUX_CI_CANONICAL_ROOT=/private/tmp/cmux-ci-2\n"))
        finally:
            for runner in ("k0", "k4", "k5", "k6"):
                self.finish(runner)
        self.assertTrue(self.lock_free())

    def test_two_roots_each_runner_prefers_its_own_root(self) -> None:
        # warm labels (cmux#14396) name one tree, so a root runner keeps compiling in its own root when it can
        self.fleet()
        two = ("--canonical-roots", "2", "--compile-slots", "2")
        try:
            zero = self.job("macos-compile-admission", "r0", 14, None, *two, "--instance", "0")
            one = self.job("macos-compile-admission", "r1", 14, None, *two, "--instance", "1")
            self.assertIn("root-1", zero.stdout)
            self.assertIn("root-2", one.stdout)
            self.finish("r0")
            self.finish("r1")
            # instance 1 first this time: it still gets root 2, and instance 0 still gets root 1
            self.assertIn("root-2", self.job("macos-compile-admission", "r1", 14, None, *two, "--instance", "1").stdout)
            self.assertIn("root-1", self.job("macos-compile-admission", "r0", 14, None, *two, "--instance", "0").stdout)
            self.finish("r0")
            self.finish("r1")
            # root 1 held (as a consumer's take-root holds it): instance 0 falls back to root 2
            held = os.open(self.dir / "capacity" / "root-1.token", os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(held, fcntl.LOCK_EX)
                fallback = self.job("macos-compile-admission", "r0", 14, None, *two, "--instance", "0")
                self.assertEqual(fallback.returncode, 0, fallback.stdout)
                self.assertIn("root-2", fallback.stdout)
            finally:
                os.close(held)
        finally:
            for runner in ("r0", "r1"):
                self.finish(runner)
        self.assertTrue(self.lock_free())

    def test_two_roots_admission_takes_the_root_warm_for_its_pull_request(self) -> None:
        # the picker sends admission to a runner whose mini is warm for it (cmux warm affinity), but the root
        # follows the token: the hook gives it the root whose kept build is stamped with its key
        fleet = self.fleet()
        two = ("--canonical-roots", "2", "--compile-slots", "2")
        base = "0123456789abcdef0123456789abcdef01234567"

        def stamp(k: int, merged_onto: str, pr: int, fingerprint: str = "fp-owned-rec1") -> None:
            # cmux owned_build_state.py keep's stamp (cmux#14717, #14718)
            store = fleet / "ci" if k == 1 else fleet / "ci" / f"cmux-ci-{k}"
            (store / "derived-data").mkdir(parents=True, exist_ok=True)
            (store / "stamp.json").write_text(json.dumps({"fingerprint": fingerprint, "merged_onto": merged_onto,
                                                          "pr": pr}))

        def admit(runner: str, instance: str, pr: dict) -> subprocess.CompletedProcess:
            payload = {"repository": CMUX, "pull_request": {"head": {"repo": CMUX}, "base": {"repo": CMUX}, **pr}}
            path = event(self.dir, f"pr-{runner}", payload)
            return self.run_hook("job-started", "pull_request", path, "--allowed-repo", "manaflow-ai/cmux",
                                 "--no-disk", "--state-dir", os.fspath(self.dir / "state"),
                                 "--watch-pid", str(os.getpid()), "--capacity-units", "8",
                                 "--capacity-dir", os.fspath(self.dir / "capacity"), *two, "--instance", instance,
                                 repo="manaflow-ai/cmux",
                                 env={"GITHUB_JOB": "macos-compile-admission", "RUNNER_NAME": runner})

        try:
            stamp(1, "f" * 40, 7)
            stamp(2, base, 42)
            # instance 0 owns root 1, but root 2 is kept for this merge base
            warm = admit("r0", "0", {"base": {"repo": CMUX, "sha": base}, "number": 7})
            self.assertEqual(warm.returncode, 0, warm.stdout + warm.stderr)
            self.assertIn("persistent-dd+root-2, warm for 0123456789ab", warm.stdout)
            self.finish("r0")
            # a re-push onto a new base: the root kept for the same pull request
            again = admit("r1", "1", {"base": {"repo": CMUX, "sha": "e" * 40}, "number": 7})
            self.assertIn("persistent-dd+root-1, warm for pr-7", again.stdout)
            self.finish("r1")
            # the merge base beats the pull request, and a warm root that is held falls back to the other
            held = os.open(self.dir / "capacity" / "root-2.token", os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(held, fcntl.LOCK_EX)
                busy = admit("r0", "0", {"base": {"repo": CMUX, "sha": base}, "number": 7})
                self.assertIn("persistent-dd+root-1 for", busy.stdout)
                self.assertNotIn("warm for", busy.stdout)
            finally:
                os.close(held)
            self.finish("r0")
            # no match, a stamp mid-keep (no fingerprint), or no pull request: the runner's own root
            stamp(2, base, 42, fingerprint="")
            for runner, instance, pr in (("r1", "1", {"base": {"repo": CMUX, "sha": "d" * 40}, "number": 9}),
                                         ("r0", "0", {"base": {"repo": CMUX, "sha": base}})):
                plain = admit(runner, instance, pr)
                self.assertIn(f"root-{int(instance) + 1} for", plain.stdout)
                self.assertNotIn("warm for", plain.stdout)
                self.finish(runner)
        finally:
            for runner in ("r0", "r1"):
                self.finish(runner)
        self.assertTrue(self.lock_free())

    def test_root_warm_keys_never_trust_a_stamp_blindly(self) -> None:
        # any job on the mini can write the stamp: a FIFO, junk, a huge file, another state version or a
        # missing DerivedData reads as not warm, and never blocks or raises
        base, ci = "0123456789abcdef0123456789abcdef01234567", self.dir / "ci"
        (ci / "derived-data").mkdir(parents=True)
        good = {"fingerprint": "fp-owned-rec1", "merged_onto": base.upper(), "pr": 3}
        (ci / "stamp.json").write_text(json.dumps(good))
        self.assertEqual(hook.root_warm_keys(1, os.fspath(self.dir / "ci")), ["0123456789ab", "pr-3"])
        for bad in ({**good, "fingerprint": "fp-owned-rec0"}, {**good, "fingerprint": ""}, [good],
                    {**good, "merged_onto": "HEAD", "pr": True}, "[" * 100000):
            (ci / "stamp.json").write_text(bad if isinstance(bad, str) else json.dumps(bad))
            self.assertIn(hook.root_warm_keys(1, os.fspath(ci)), ([], ), bad if isinstance(bad, dict) else "junk")
        (ci / "stamp.json").write_text(json.dumps(good))
        os.rmdir(ci / "derived-data")
        self.assertEqual(hook.root_warm_keys(1, os.fspath(ci)), [])
        (ci / "derived-data").mkdir()
        (ci / "stamp.json").unlink()
        os.mkfifo(ci / "stamp.json")
        start = time.monotonic()
        self.assertEqual(hook.root_warm_keys(1, os.fspath(ci)), [])
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(hook.root_warm_keys(2, os.fspath(ci)), [])  # no cmux-ci-2 at all

    def test_warm_root_costs_rank_roots_by_predicted_compile(self) -> None:
        ci = self.dir / "ci"
        mirror = self.dir / "work"  # the commits; the hook reads a bare copy, as the prefetch keeps it
        mirror.mkdir(parents=True)
        git = ["git", "-C", os.fspath(mirror), "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run([*git, "init", "-q"], check=True)
        (mirror / "Sources").mkdir()
        (mirror / "Sources" / "A.swift").write_text("a")
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "0"], check=True)
        old = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        for n in range(3):
            (mirror / "Sources" / f"M{n}.swift").write_text("m")
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "1"], check=True)
        new = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        (ci / ".prefetch").mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", "--bare", os.fspath(mirror), os.fspath(ci / ".prefetch" / "cmux.git")],
                       check=True)

        def stamp(k: int, onto: str, pr: int, files: list[str], **extra) -> None:
            store = ci if k == 1 else ci / f"cmux-ci-{k}"
            (store / "derived-data").mkdir(parents=True, exist_ok=True)
            (store / "stamp.json").write_text(json.dumps({"fingerprint": "fp-owned-rec1", "merged_onto": onto,
                                                          "pr": pr, "pr_app_swift_files": files, **extra}))

        # root 1 kept pull request 5 (8 files) three main files back; root 2 kept pull request 6 (1 file) on the base
        stamp(1, old, 5, [f"Sources/P{n}.swift" for n in range(8)], pr_package_interface=False)
        stamp(2, new, 6, ["Sources/Q.swift"], pr_package_interface=False)
        order, predicted = hook.warm_root_costs([0, 1], new, 7, os.fspath(ci))
        self.assertEqual(order, [1, 0])
        self.assertEqual(predicted["root-1"], {"seconds": 266.5, "tier": "far", "app_swift_files": 11})
        self.assertEqual(predicted["root-2"], {"seconds": 140.0, "tier": "near", "app_swift_files": 1})
        # A re-push of pull request 5: its own files are not undone, so both roots are near; the fewer files win
        # (root 2's 1 against root 1's 3 main files), and on an equal count the runner's own root.
        order, predicted = hook.warm_root_costs([0, 1], new, 5, os.fspath(ci))
        self.assertEqual((order, predicted["root-1"]["tier"], predicted["root-1"]["app_swift_files"]), ([1, 0], "near", 3))
        stamp(2, new, 6, [f"Sources/Q{n}.swift" for n in range(3)], pr_package_interface=False)
        self.assertEqual(hook.warm_root_costs([0, 1], new, 5, os.fspath(ci))[0], [0, 1])
        self.assertEqual(hook.warm_root_costs([1, 0], new, 5, os.fspath(ci))[0], [1, 0])
        # A list the stamp cut short may hide a package change: a rebuild unless the stamp says no interface changed.
        stamp(2, new, 6, ["Sources/Q.swift"], pr_app_swift_total=500)
        self.assertEqual(hook.warm_root_costs([0, 1], new, 7, os.fspath(ci))[1]["root-2"]["tier"], "rebuild")
        stamp(2, new, 6, ["Sources/Q.swift"], pr_package_interface=False)
        # The fitted model beside the stamps replaces the default; a package interface change or a hot file rebuilds.
        (ci / hook.WARM_MODEL).write_text(json.dumps({"near_app_swift_files": 5, "hot_files": ["Sources/Q.swift"],
                                                      "tiers": {"near": {"p50": 100}, "far": {"p50": 200},
                                                                "rebuild": {"p50": 300}}}))
        order, predicted = hook.warm_root_costs([0, 1], new, 7, os.fspath(ci))
        self.assertEqual((order, predicted["root-2"]["tier"], predicted["root-1"]["seconds"]), ([0, 1], "rebuild", 200.0))
        # Parity with cmux warm_distance.predict(): a root starts from its kept build, so a tier's kept cell costs
        # it once the cell has 5 compiles; a sparse or bad cell, or a model without tiers_by_start, costs the tier.
        def cells(near_kept: dict) -> None:
            (ci / hook.WARM_MODEL).write_text(json.dumps({
                "near_app_swift_files": 5, "hot_files": ["Sources/Q.swift"],
                "tiers": {"near": {"p50": 100}, "far": {"p50": 200}, "rebuild": {"p50": 300}},
                "tiers_by_start": {"near": {"kept": near_kept, "seed": {"n": 50, "p50": 150}},
                                   "far": {"kept": {"n": 5, "p50": 120.5}, "seed": {"n": 9, "p50": 250}},
                                   "rebuild": {"kept": {"n": 4, "p50": 250}}}}))

        cells({"n": 5, "p50": 95})
        model = hook.read_warm_model(os.fspath(ci))
        self.assertEqual(model["kept"], {"near": 95.0, "far": 120.5})  # rebuild's cell has 4 compiles
        self.assertEqual(model["tiers"], {"near": 100.0, "far": 200.0, "rebuild": 300.0})
        order, predicted = hook.warm_root_costs([0, 1], new, 7, os.fspath(ci))
        self.assertEqual((predicted["root-1"]["seconds"], predicted["root-2"]["seconds"]), (120.5, 300.0))
        self.assertEqual(order, [0, 1])
        order, predicted = hook.warm_root_costs([0, 1], new, 5, os.fspath(ci))  # both near on a re-push
        self.assertEqual(predicted["root-1"], {"seconds": 95.0, "tier": "near", "app_swift_files": 3})
        for bad in ({"n": 4, "p50": 95}, {"n": 5, "p50": -1}, {"n": 5, "p50": True}, {"n": True, "p50": 95},
                    {"n": 5}, {"n": "5", "p50": 95}, [], None):
            cells(bad)
            model = hook.read_warm_model(os.fspath(ci))
            self.assertEqual((model["kept"].get("near"), model["kept"]["far"], model["tiers"]["near"]),
                             (None, 120.5, 100.0), bad)
        self.assertEqual(hook.read_warm_model(os.fspath(self.dir / "nowhere"))["kept"], {})
        (ci / hook.WARM_MODEL).write_text(json.dumps({"near_app_swift_files": 5, "hot_files": ["Sources/Q.swift"],
                                                      "tiers": {"near": {"p50": 100}, "far": {"p50": 200},
                                                                "rebuild": {"p50": 300}}}))
        self.assertEqual(hook.read_warm_model(os.fspath(ci))["kept"], {})
        for junk in ("[" * 1000, '{"tiers": {"near": {"p50": NaN}, "far": {"p50": 1}, "rebuild": {"p50": 2}}}',
                     '{"tiers": {"near": {"p50": -1}, "far": {"p50": 1}, "rebuild": {"p50": 2}}}'):
            (ci / hook.WARM_MODEL).write_text(junk)
            self.assertEqual(hook.read_warm_model(os.fspath(ci)), hook.WARM_DEFAULT_MODEL, junk)
        # A root with no kept build goes last, after a rebuild.
        (ci / "cmux-ci-2" / "stamp.json").unlink()
        order, predicted = hook.warm_root_costs([1, 0], new, 7, os.fspath(ci))
        self.assertEqual((order, predicted["root-2"]["tier"]), ([0, 1], "cold"))
        stamp(2, new, 6, ["Sources/Q.swift"], pr_package_interface=False)
        # A base the mirror lacks, a stamp of an older cmux, or no base: fall back to the exact keys.
        self.assertEqual(hook.warm_root_costs([0, 1], "e" * 40, 7, os.fspath(ci)), ([], {}))
        self.assertEqual(hook.warm_root_costs([0, 1], "", 7, os.fspath(ci)), ([], {}))
        self.assertEqual(hook.warm_root_costs([0, 1], new, 7, os.fspath(self.dir / "nowhere")), ([], {}))
        (ci / "stamp.json").write_text(json.dumps({"fingerprint": "fp-owned-rec1", "merged_onto": old, "pr": 5}))
        order, predicted = hook.warm_root_costs([0, 1], new, 7, os.fspath(ci))
        self.assertEqual(predicted["root-1"]["tier"], "unknown")
        self.assertEqual(hook.job_warm_target({"pull_request": {"base": {"sha": new.upper()}, "number": 5,
                                                                "changed_files": 12}}), (new, 5, 12))
        self.assertEqual(hook.job_warm_target({"pull_request": {"number": True, "changed_files": "9"}}), ("", None, 0))

    def test_warm_root_costs_count_the_job_and_find_its_parked_build(self) -> None:
        ci = self.dir / "ci"
        work = self.dir / "work"
        work.mkdir()
        git = ["git", "-C", os.fspath(work), "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run([*git, "init", "-q"], check=True)
        (work / "A.swift").write_text("a")
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "0"], check=True)
        base = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        (ci / ".prefetch").mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", "--bare", os.fspath(work), os.fspath(ci / ".prefetch" / "cmux.git")],
                       check=True)

        def stamp(store: Path, pr: int) -> None:
            (store / "derived-data").mkdir(parents=True, exist_ok=True)
            (store / "stamp.json").write_text(json.dumps({"fingerprint": "fp-owned-rec1", "merged_onto": base, "pr": pr,
                                                          "pr_app_swift_files": ["Sources/X.swift"],
                                                          "pr_package_interface": False}))

        stamp(ci, 5)
        stamp(ci / "cmux-ci-2", 6)
        # The job's own 40 files make every other pull request's build far; its own build (root 1) stays near.
        order, predicted = hook.warm_root_costs([1, 0], base, 5, os.fspath(ci), job_files=40)
        self.assertEqual((order, predicted["root-1"]["tier"], predicted["root-2"]["tier"]), ([0, 1], "near", "far"))
        self.assertEqual(predicted["root-2"]["app_swift_files"], 41)
        # A build of pull request 7 parked on root 2 counts as its own build there.
        stamp(ci / "cmux-ci-2" / "pr-builds" / "pr-7", 7)
        order, predicted = hook.warm_root_costs([0, 1], base, 7, os.fspath(ci), job_files=40)
        self.assertEqual((order, predicted["root-2"]["tier"]), ([1, 0], "near"))
        # A parked stamp naming another pull request is not taken for this one.
        stamp(ci / "cmux-ci-2" / "pr-builds" / "pr-8", 9)
        self.assertEqual(hook.warm_root_costs([0, 1], base, 8, os.fspath(ci), job_files=40)[1]["root-2"]["tier"], "far")
        # Over a main build admission adopts from the parked one, so it counts there too.
        (ci / "cmux-ci-2" / "stamp.json").write_text(json.dumps({"fingerprint": "fp-owned-rec1", "merged_onto": base}))
        self.assertEqual(hook.root_stamp(2, os.fspath(ci), 7).get("pr"), 7)
        # An expired slot is never unparked either.
        activity = self.dir / "Library/Logs/glaeda-cmux-jobs.jsonl"
        activity.parent.mkdir(parents=True, exist_ok=True)
        activity.write_text("".join(json.dumps({"event": "started", "at": time.time()}) + "\n" for _ in range(
            hook.WARM_PR_BUILD_MAX_JOBS)))
        old_job_log = hook.JOB_LOG
        hook.JOB_LOG = activity
        self.addCleanup(setattr, hook, "JOB_LOG", old_job_log)
        (ci / "cmux-ci-2" / "stamp.json").write_text(json.dumps({"fingerprint": "fp-owned-rec1", "merged_onto": base,
                                                                "pr": 6}))
        self.assertEqual(hook.root_stamp(2, os.fspath(ci), 7).get("pr"), 6)
        os.utime(ci / "cmux-ci-2" / "pr-builds" / "pr-7", (1, 1))
        self.assertEqual(hook.root_stamp(2, os.fspath(ci), 7).get("pr"), 6)

    def test_job_warm_keys_come_from_the_pull_request(self) -> None:
        sha = "ABCDEF0123456789abcdef0123456789abcdef01"
        self.assertEqual(hook.job_warm_keys({"pull_request": {"base": {"sha": sha}, "number": 5}}),
                         ("abcdef012345", "pr-5"))
        for payload in (None, {}, {"ref": "refs/heads/main"}, {"pull_request": {"base": {"sha": "x"}, "number": "5"}},
                        {"pull_request": {"number": True}}, {"pull_request": []}):
            self.assertEqual(hook.job_warm_keys(payload), (), payload)

    def test_capacity_ios_jobs_take_no_root_or_gui_token(self) -> None:
        self.fleet()
        ios = {"GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/test-ios.yml@refs/heads/main"}
        try:
            self.assertIn("persistent-dd+root-1", self.job("macos-compile-admission", "i0", 12).stdout)
            for n, job in enumerate(("ios-simulator-build", "mobile-core-package")):
                result = self.job(job, f"i{n + 1}", 12, None, env=ios)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn(f"holding 3/12 units for {job} (isolated", result.stdout)
            sim = self.job("ios-simulator", "i3", 12)
            self.assertIn("1/12 units+simulator for ios-simulator (simulator", sim.stdout)
            waited = time.monotonic()
            second = self.job("screenshots", "i4", 12, None, "--gui-wait", "2")
            self.assertEqual(second.returncode, 1, "one simulator job per mini")
            self.assertIn("the simulator token is taken", second.stdout)
            self.assertGreaterEqual(time.monotonic() - waited, 2, "it waits for the token before it is refused")
            self.finish("i4")
            # the holder finishes within the wait: the waiting simulator job is admitted instead of refused
            threading.Timer(1.0, self.finish, args=("i3",)).start()
            later = self.job("screenshots", "i4", 12, None, "--gui-wait", "30")
            self.assertEqual(later.returncode, 0, later.stdout)
            self.assertIn("units+simulator for screenshots (simulator", later.stdout)
            self.assertIn("(validate is compile)", self.job("validate", "i5", 12).stdout,
                          "validate is unknown to the hook (compile class): it stays on Blacksmith")
        finally:
            for runner in ("i0", "i1", "i2", "i3", "i4", "i5"):
                self.finish(runner)

    def test_capacity_compile_slots(self) -> None:
        self.fleet()
        slots = ("--compile-slots", "2", "--canonical-roots", "2")
        try:
            first = self.started("--capacity-units", "10", "--capacity-dir", os.fspath(self.dir / "capacity"), *slots,
                                 env={"GITHUB_JOB": "macos-compile-admission", "RUNNER_NAME": "c0"})
            self.assertIn("5/10 units+persistent-dd+root-1 for", first.stdout)
            second = self.started("--capacity-units", "10", "--capacity-dir", os.fspath(self.dir / "capacity"), *slots,
                                  env={"GITHUB_JOB": "macos-compile-admission", "RUNNER_NAME": "c1"})
            self.assertIn("5/10 units+persistent-dd-1+root-2 for", second.stdout)
            third = self.started("--capacity-units", "15", "--capacity-dir", os.fspath(self.dir / "capacity"), *slots,
                                 env={"GITHUB_JOB": "macos-compile-admission", "RUNNER_NAME": "c2"})
            self.assertIn("refused: capacity: all 2 persistent-dd tokens are taken", third.stdout)
        finally:
            for runner in ("c0", "c1"):
                self.finish(runner)

    def test_capacity_gui_token_and_unknown_jobs(self) -> None:
        self.fleet()
        try:
            self.assertEqual(self.job("tests-build-and-lag", "g0").returncode, 0)
            other = self.job("tests-build-and-lag", "g1", 4, None, "--gui-wait", "0")
            self.assertIn("refused: capacity: the gui token is taken", other.stdout)
            ci = {"GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/ci.yml@refs/heads/main"}
            for n, (lane, cores) in enumerate((("cli-pipe-regressions", 2), ("remote-daemon-macos-tests", 1),
                                               ("claude-wrapper", 1))):
                side = self.job(lane, f"s{n}", 8, None, env=ci)
                self.assertIn(f"{cores}/8 units for {lane} (light", side.stdout, "light, or its JOB_CORES")
                self.finish(f"s{n}")
            self.assertIn("canonical root token is taken", self.job("future-unlisted-job", "u0").stdout)
            self.finish("g0")
            unknown = self.job("future-unlisted-job", "u0")
            self.assertIn("persistent-dd+root-1 for future-unlisted-job (compile", unknown.stdout)
        finally:
            for runner in ("g0", "u0"):
                self.finish(runner)

    def test_capacity_a_root_runner_listens_only_while_the_mini_fits_any_root_job(self) -> None:
        self.fleet()
        capacity = self.dir / "capacity"
        root = hook.RunnerScope(units=4, compile_slots=2, roots=2, root=True)
        side = hook.RunnerScope(units=4, compile_slots=2, roots=2, root=False)
        slots = ("--compile-slots", "2", "--canonical-roots", "2")
        try:
            first = self.job("macos-compile-admission", "c0", 10, None, *slots, "--instance", "0")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIsNone(hook.mini_full(capacity, 10, root), "5 units, pdd-1 and root-2 are free")
            shard = self.job("tests-build-and-lag", "g1", 10, None, *slots, "--instance", "1", "--gui-wait", "0")
            self.assertEqual(shard.returncode, 0, shard.stdout + shard.stderr)
            # 4 cores left: the next compile (5) would wait, so root runners stop; a light side job still fits
            self.assertEqual(hook.mini_full(capacity, 10, root),
                             "only 4 of 10 capacity units on this mini are free; a compile needs 5")
            self.assertIsNone(hook.mini_full(capacity, 10, side))
            self.assertIn("4 of 10 units free", self.job("macos-compile-admission", "c2", 10, None, *slots).stdout)
            self.finish("c0")
            # 9 units free while the shard holds the gui token: a compile fits, and a shard waits for the token
            self.assertIsNone(hook.mini_full(capacity, 10, root))
            self.assertIsNone(hook.mini_full(capacity, 10, side))
            self.finish("g1")
            self.assertIsNone(hook.mini_full(capacity, 10, root))
            for n in range(2):  # both compile slots: every root and persistent-dd token is taken
                both = self.job("macos-compile-admission", f"c{n}", 15, None, *slots, "--instance", str(n))
                self.assertEqual(both.returncode, 0, both.stdout + both.stderr)
            self.assertEqual(hook.mini_full(capacity, 15, root), "all 2 persistent-dd tokens on this mini are taken")
            self.assertIsNone(hook.mini_full(capacity, 15, side))
        finally:
            for runner in ("c0", "c1", "c2", "g1"):
                self.finish(runner)

    def idle_warm(self, *held: str) -> subprocess.Popen:
        """A stand-in glaeda-idle-warm: holds HELD under the capacity dir, names them in the holder file, and
        exits on SIGTERM, as the real one does."""
        capacity = self.dir / "capacity"
        capacity.mkdir(exist_ok=True)
        fake = make_executable(self.dir / "glaeda-idle-warm", f"""import fcntl, json, os, pathlib, signal, sys, time
capacity = pathlib.Path({os.fspath(capacity)!r})
fds = []
host = os.open(capacity.parent / "fleet" / "host.lock", os.O_RDONLY | os.O_CREAT)
fcntl.flock(host, fcntl.LOCK_SH | fcntl.LOCK_NB)  # as the real one does, like a job
for name in sys.argv[1:]:
    fd = os.open(capacity / (name if name.startswith("unit-") else name + ".token"), os.O_RDONLY | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fds.append(fd)
(capacity / "idle-warm.json").write_text(json.dumps({{"pid": os.getpid(), "held": sys.argv[1:]}}))
signal.signal(signal.SIGTERM, lambda *_: os._exit(143))
print("held", flush=True)
time.sleep(60)
""")
        warm = subprocess.Popen([sys.executable, os.fspath(fake), *held], stdout=subprocess.PIPE, text=True)
        self.assertEqual(warm.stdout.readline().strip(), "held")
        self.addCleanup(warm.kill)
        # launchd reaps the real one at once; a zombie would still look alive to the hook
        threading.Thread(target=warm.wait, daemon=True).start()
        return warm

    def test_capacity_a_job_stops_the_idle_catch_up_and_takes_its_locks(self) -> None:
        self.fleet()
        capacity = self.dir / "capacity"
        root = hook.RunnerScope(units=4, compile_slots=1, roots=1, root=True)
        warm = self.idle_warm("root-1", "persistent-dd", "unit-0", "unit-1")
        try:
            # what the catch-up holds counts as free, so the gate keeps the root runner listening
            self.assertIsNone(hook.mini_full(capacity, 10, root))
            first = self.job("macos-compile-admission", "y0", 10)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIn("holding 5/10 units+persistent-dd+root-1", first.stdout)
            self.assertIn(f"stopped the idle catch-up (pid {warm.pid})", first.stdout)
            # SIGTERM, or SIGKILL once WARM_YIELD_S passed (a loaded machine may not run its handler in time)
            self.assertIn(warm.wait(timeout=10), (143, -signal.SIGKILL))
            # the job's own locks are not the catch-up's: the gate sees them as taken
            self.assertEqual(hook.mini_full(capacity, 10, root), "the persistent-dd token is taken")
        finally:
            self.finish("y0")

    def test_the_listener_gate_never_holds_a_runner_off_for_the_idle_catch_up(self) -> None:
        fleet = self.fleet()
        capacity = self.dir / "capacity"
        warm = self.idle_warm("root-1", "persistent-dd", "unit-0", "unit-1")
        # it holds the host lock shared, like a job: never a fleet build waiting for the host
        self.assertEqual(hook.lock_waiters(os.fspath(fleet / "host.lock"), set()), [])
        runner = self.dir / "actions-runner-glaeda"
        (runner / "glaeda-hooks").mkdir(parents=True)
        gate = hook.Gate(runner, os.fspath(fleet / "host.lock"), os.fspath(self.dir / "none.json"),
                         self.dir / "state", capacity_dir=capacity)
        self.assertIsNone(gate.shed(None))
        # a load or thermal hold stops the catch-up instead of holding the runner off
        self.assertIsNone(gate.shed("the mini is saturated"))
        self.assertIn(warm.wait(timeout=10), (143, -signal.SIGKILL))
        self.assertEqual(gate.shed("the mini is saturated"), "the mini is saturated", "no catch-up left to stop")
        # a fleet build waiting for the host lock would wait out the catch-up's shared hold: stop it
        again = self.idle_warm("root-1", "persistent-dd")
        gate.yield_to_fleet()
        self.assertIn(again.wait(timeout=10), (143, -signal.SIGKILL))

    def test_capacity_yielding_the_idle_catch_up_also_ends_its_build(self) -> None:
        capacity = self.dir / "capacity"
        capacity.mkdir()
        leader = make_executable(self.dir / "glaeda-idle-warm", f"""import json, os, pathlib, signal, subprocess, time
build = subprocess.Popen(["/bin/sleep", "60"])  # joins our process group
pathlib.Path({os.fspath(capacity)!r}, "idle-warm.json").write_text(json.dumps({{"pid": os.getpid(), "held": []}}))
signal.signal(signal.SIGTERM, lambda *_: os._exit(143))  # exits without ending its build
print(build.pid, flush=True)
time.sleep(60)
""")
        proc = subprocess.Popen([sys.executable, os.fspath(leader)], stdout=subprocess.PIPE, text=True,
                                start_new_session=True)
        self.addCleanup(proc.kill)
        orphan = int(proc.stdout.readline())
        self.addCleanup(lambda: subprocess.run(["/bin/kill", "-9", str(orphan)], capture_output=True))
        threading.Thread(target=proc.wait, daemon=True).start()
        self.assertIn("stopped the idle catch-up", hook.yield_idle_warm(capacity))
        deadline = time.monotonic() + 5
        state = "running"
        while time.monotonic() < deadline:
            state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(orphan)], capture_output=True,
                                   text=True).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(0.1)
        self.assertTrue(not state or state.startswith("Z"), f"the build still runs: {state!r}")

    def idle_fuzzer(self, capacity: Path) -> tuple[subprocess.Popen, int]:
        """A stand-in for the idle fuzzer lane: glaeda-idle-warm naming itself in FUZZ_HOLDER, with an app in its
        process group; (the lane, the app's pid)."""
        leader = make_executable(self.dir / "fuzz" / "glaeda-idle-warm", f"""import json, os, pathlib, signal, subprocess, time
os.setpgid(0, 0)
app = subprocess.Popen(["/bin/sleep", "60"])  # joins our process group
pathlib.Path({os.fspath(capacity)!r}, "idle-fuzz.json").write_text(json.dumps({{"pid": os.getpid(), "held": [], "fuzz": True}}))
signal.signal(signal.SIGTERM, lambda *_: os.killpg(0, signal.SIGKILL))
print(app.pid, flush=True)
time.sleep(60)
""")
        proc = subprocess.Popen([sys.executable, os.fspath(leader), "--apply", "--fuzz"], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        app = int(proc.stdout.readline())
        self.addCleanup(lambda: subprocess.run(["/bin/kill", "-9", str(app)], capture_output=True))
        threading.Thread(target=proc.wait, daemon=True).start()  # launchd reaps the lane on a mini
        return proc, app

    def test_capacity_only_a_gui_job_stops_the_idle_fuzzer(self) -> None:
        """The fuzzer runs beside compiles; a job that takes the gui token (at admission or take-gui) stops it and its
        app before it holds the token."""
        self.fleet()
        capacity = self.dir / "capacity"
        capacity.mkdir()
        (self.dir / "fuzz").mkdir()
        two = ("--canonical-roots", "2", "--compile-slots", "2")
        try:
            lane, app = self.idle_fuzzer(capacity)
            self.assertIsNone(hook.idle_warm(capacity), "the fuzz lane is never taken for a catch-up")
            compile_job = self.job("macos-compile-admission", "f0", 8, None, *two)
            self.assertEqual(compile_job.returncode, 0, compile_job.stdout + compile_job.stderr)
            self.assertNotIn("idle fuzzer", compile_job.stdout)
            self.assertTrue(hook.pid_alive(app), "a compile leaves the fuzzer running")
            gui = self.job("tests-build-and-lag", "f1", 8, None, *two)
            self.assertEqual(gui.returncode, 0, gui.stdout + gui.stderr)
            self.assertIn(f"stopped the idle fuzzer (pid {lane.pid})", gui.stdout)
            self.assertIn("+gui", gui.stdout)
            self.assertFalse(hook.pid_alive(app), "its app went with it")
            self.finish("f1")
            # take-gui: a job that asks for the token from its step
            lane, app = self.idle_fuzzer(capacity)
            taken = self.take_gui("f0", "--wait", "5")
            self.assertEqual(taken.returncode, 0, taken.stderr)
            self.assertIn(f"stopped the idle fuzzer (pid {lane.pid})", taken.stderr)
            self.assertFalse(hook.pid_alive(app))
        finally:
            for runner in ("f0", "f1"):
                self.finish(runner)

    def test_capacity_a_stale_idle_catch_up_file_signals_nothing(self) -> None:
        self.fleet()
        capacity = self.dir / "capacity"
        capacity.mkdir()
        other = subprocess.Popen(["/bin/sleep", "60"])
        self.addCleanup(other.kill)
        (capacity / "idle-warm.json").write_text(json.dumps({"pid": other.pid, "held": ["root-1", "persistent-dd"]}))
        try:
            self.assertIsNone(hook.idle_warm(capacity), "a reused pid that is not glaeda-idle-warm")
            first = self.job("macos-compile-admission", "y1")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertNotIn("idle catch-up", first.stdout)
            self.assertIsNone(other.poll(), "never signalled")
            for junk in ("{", "[]", json.dumps({"pid": True, "held": []}), json.dumps({"pid": 1, "held": []})):
                (capacity / "idle-warm.json").write_text(junk)
                self.assertIsNone(hook.idle_warm(capacity))
        finally:
            self.finish("y1")

    def test_capacity_a_full_mini_is_seen_by_the_listener_gate(self) -> None:
        self.fleet()
        capacity = self.dir / "capacity"
        runners = [f"f{n}" for n in range(5)]
        try:
            self.assertEqual(self.job("swift-package-tests", "f0", 8).returncode, 0)
            self.assertIsNone(hook.mini_full(capacity, 8), "6 of 8 units are still free")
            for runner in runners[1:4]:
                self.assertEqual(self.job("swift-package-tests", runner, 8).returncode, 0)
            self.assertEqual(hook.mini_full(capacity, 8), "all 8 capacity units on this mini are taken")
            # the probe took and gave back nothing: the holders keep every unit, and a job is still refused
            refused = self.job("swift-package-tests", "f4", 8)
            self.assertIn("refused: capacity: 0 of 8 units free", refused.stdout)
            self.finish("f2")
            self.assertIsNone(hook.mini_full(capacity, 8))
        finally:
            for runner in runners:
                self.finish(runner)

    def fake_process(self, name: str, target: str, *args: str) -> int:
        """Start `self.dir/bin/NAME args` (a symlink to target, so ps shows that path) detached from this
        process, so launchd or init reaps it as it would the real daemon. Returns its pid."""
        link = self.dir / "bin" / name
        link.parent.mkdir(exist_ok=True)
        if not link.is_symlink():
            link.symlink_to(target)
        out = subprocess.run(["/bin/sh", "-c", '"$@" </dev/null >/dev/null 2>&1 & echo $!', "sh", os.fspath(link), *args],
                             capture_output=True, text=True, timeout=10, check=True).stdout
        pid = int(out.strip())

        def stop() -> None:
            with contextlib.suppress(OSError):
                os.kill(pid, 9)
        self.addCleanup(stop)
        return pid

    def own_tests(self) -> tuple[str, str]:
        """Only this test's fake xctest counts as a running test: real ones and other tests' stay out of it."""
        return ("--xctest-pattern", re.escape(os.fspath(self.dir / "bin")) + r"/xctest(?:\s|$)")

    def wait_gone(self, pid: int, seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not hook.pid_alive(pid):
                return True
            time.sleep(0.05)
        return False

    def test_gui_token_jobs_recycle_testmanagerd_when_no_test_runs(self) -> None:
        self.fleet()
        program = os.fspath(self.dir / "bin" / "testmanagerd")
        recycle = ("--recycle-testmanagerd", "--testmanagerd-program", program, *self.own_tests())
        try:
            daemon = self.fake_process("testmanagerd", "/bin/sleep", "60")
            light = self.job("swift-package-tests", "t0", 4, None, *recycle)
            self.assertEqual(light.returncode, 0, light.stdout)
            self.assertNotIn("testmanagerd", light.stdout, "a job without the gui token leaves it")
            self.assertTrue(hook.pid_alive(daemon))
            # a test in flight on the mini (another runner's xctest) keeps it
            xctest = self.fake_process("xctest", "/bin/sleep", "60")
            kept = self.job("cli-product-tests", "t1", 4, None, *recycle)
            self.assertEqual(kept.returncode, 0, kept.stdout)
            self.assertIn("testmanagerd: kept (a test is running on this mini)", kept.stdout)
            self.assertTrue(hook.pid_alive(daemon))
            self.finish("t1")
            os.kill(xctest, 9)
            self.assertTrue(self.wait_gone(xctest))
            shard = self.job("tests-build-and-lag", "t2", 4, None, *recycle)
            self.assertEqual(shard.returncode, 0, shard.stdout)
            self.assertRegex(shard.stdout, rf"testmanagerd: (stopped|killed) pid {daemon}\b")  # killed: a slow exit
            self.assertTrue(self.wait_gone(daemon))
            self.finish("t2")
            # none running: launchd starts one at the next test
            none = self.job("tests-build-and-lag", "t3", 4, None, *recycle)
            self.assertIn("testmanagerd: not running", none.stdout)
            self.finish("t3")
            # a wedged daemon that ignores SIGTERM is killed
            ready = self.dir / "ready"
            (self.dir / "bin" / "testmanagerd").unlink()
            wedged = self.fake_process("testmanagerd", "/bin/sh", "-c",
                                       f"trap '' TERM; : > {shlex.quote(os.fspath(ready))}; while :; do sleep 1; done")
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            killed = self.job("cli-product-tests", "t4", 4, None, *recycle)
            self.assertEqual(killed.returncode, 0, killed.stdout)
            self.assertIn(f"testmanagerd: killed pid {wedged} (it ignored SIGTERM)", killed.stdout)
            self.assertTrue(self.wait_gone(wedged))
        finally:
            for runner in ("t0", "t1", "t2", "t3", "t4"):
                self.finish(runner)

    def test_take_gui_recycles_testmanagerd_for_the_tests(self) -> None:
        self.fleet()
        e2e = {"GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/test-e2e.yml@refs/heads/main"}
        program = os.fspath(self.dir / "bin" / "testmanagerd")
        try:
            build = self.job("build", "e0", 8, None, "--recycle-testmanagerd", "--testmanagerd-program", program,
                             *self.own_tests(), env=e2e)
            self.assertEqual(build.returncode, 0, build.stdout)
            self.assertNotIn("testmanagerd", build.stdout, "compile-gui takes the gui token later")
            daemon = self.fake_process("testmanagerd", "/bin/sleep", "60")
            took = self.take_gui("e0", "--recycle-testmanagerd", "--testmanagerd-program", program, *self.own_tests())
            self.assertEqual(took.returncode, 0, took.stderr)
            self.assertEqual(took.stdout.strip(), "")
            self.assertRegex(took.stderr, rf"take-gui: testmanagerd: (stopped|killed) pid {daemon}\b")
            self.assertTrue(self.wait_gone(daemon))
        finally:
            self.finish("e0")

    def test_a_testmanagerd_that_outlives_sigkill_refuses_the_job(self) -> None:
        self.assertIsNone(hook.XCTEST_RUNNING.search("/usr/bin/xcodebuild build -scheme cmux"))
        self.assertIsNone(hook.XCTEST_RUNNING.search("/x/Debug/cmuxCLITests.xctest"))
        for busy in ("/Applications/Xcode_26.6.app/Contents/Developer/usr/bin/xctest /x/a.xctest",
                     "/usr/bin/xcodebuild test-without-building -xctestrun a", "xcodebuild -scheme cmux test"):
            self.assertIsNotNone(hook.XCTEST_RUNNING.search(busy), busy)
        daemon, root = (4242, os.getuid(), "Ss", hook.TESTMANAGERD), (4243, 0, "Ss", hook.TESTMANAGERD)

        def recycle(*tables, error=None):
            """recycle_testmanagerd against ps tables in turn (the last repeats), with os.kill recorded."""
            seq = list(tables)
            ps = mock.Mock(side_effect=error or (lambda: seq.pop(0) if len(seq) > 1 else seq[0]))
            with mock.patch.object(hook, "_ps_table", ps), mock.patch.object(hook.os, "kill") as kill, \
                    mock.patch.object(hook, "TESTMANAGERD_TERM_S", 0.3), mock.patch.object(hook, "TESTMANAGERD_KILL_S", 0.3):
                return hook.recycle_testmanagerd(), [c.args for c in kill.call_args_list]

        term, kill = hook.signal.SIGTERM, hook.signal.SIGKILL
        note, kills = recycle([daemon, root])
        self.assertEqual(note, f"{hook.TESTMANAGERD_STUCK}: pid 4242 outlived SIGKILL")
        self.assertEqual(kills, [(4242, term), (4242, kill)], "only this user's daemon")
        # a zombie is gone: an impostor whose parent never reaps it cannot make the mini refuse every job
        self.assertEqual(recycle([daemon], [(4242, os.getuid(), "Z", hook.TESTMANAGERD)])[0],
                         "testmanagerd: stopped pid 4242")
        self.assertEqual(recycle([daemon], [(4242, os.getuid(), "SE", hook.TESTMANAGERD)])[0],
                         "testmanagerd: stopped pid 4242", "exiting (macOS ps E) counts as gone")
        # a test that starts while it ignores SIGTERM keeps it: no SIGKILL under a live run
        note, kills = recycle([daemon], [daemon, (7, os.getuid(), "S", "/x/usr/bin/xctest /x/a.xctest")])
        self.assertEqual(note, "testmanagerd: kept pid 4242 (ignored SIGTERM; a test started)")
        self.assertEqual(kills, [(4242, term)])
        self.assertEqual(recycle([], error=ValueError("bad bytes"))[0], "testmanagerd: not checked (ValueError)")
        self.assertEqual(recycle(None)[0], "testmanagerd: not checked (ps failed)")
        # ps failing after the signal is no proof it is stuck: never SIGKILL or refuse on a guess
        note, kills = recycle([daemon], None)
        self.assertEqual((note, kills), ("testmanagerd: sent SIGTERM to pid 4242 (not confirmed: ps failed)", [(4242, term)]))
        note = f"{hook.TESTMANAGERD_STUCK}: pid 4242 outlived SIGKILL"
        # job-started refuses and lets its capacity go: the hook itself, with only the daemon's answer stubbed
        self.fleet()
        stub = ("import importlib.machinery,importlib.util,sys\n"
                f"loader=importlib.machinery.SourceFileLoader('h',{os.fspath(HOOK)!r})\n"
                "spec=importlib.util.spec_from_loader('h',loader);h=importlib.util.module_from_spec(spec)\n"
                "loader.exec_module(h)\n"
                f"h.recycle_testmanagerd=lambda *a: {note!r}\n"
                "sys.exit(h.main(sys.argv[1:]))\n")
        push = event(self.dir, "push", {"repository": CMUX})
        environ = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GLAEDA_RUNNER_TELEMETRY": "0",
                   "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(self.dir / "fleet"), "GITHUB_EVENT_NAME": "push",
                   "GITHUB_EVENT_PATH": os.fspath(push), "GITHUB_REPOSITORY": "manaflow-ai/cmux",
                   "GITHUB_JOB": "tests-build-and-lag", "RUNNER_NAME": "s0"}
        refused = subprocess.run([sys.executable, "-c", stub, "job-started", "--allowed-repo", "manaflow-ai/cmux",
                                      "--no-disk", "--state-dir", os.fspath(self.dir / "state"),
                                      "--watch-pid", str(os.getpid()), "--capacity-units", "4",
                                      "--capacity-dir", os.fspath(self.dir / "capacity"), "--recycle-testmanagerd"],
                                     env=environ, capture_output=True, text=True, timeout=30, check=False)
        try:
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn(f"refused: {note}", refused.stdout)
            # no job-completed for s0 yet: the refusal itself let the gui token and root go
            again = self.job("tests-build-and-lag", "s1", 4, None, "--gui-wait", "0")
            self.assertEqual(again.returncode, 0, "the refused job let the gui token and root go: " + again.stdout)
        finally:
            self.finish("s0")
            self.finish("s1")

    def test_capacity_product_job_holds_the_gui_token(self) -> None:
        # cli-product-tests runs XCTest through the same testmanagerd as the gui jobs: one at a time per mini
        self.fleet()
        try:
            product = self.job("cli-product-tests", "p0")
            self.assertEqual(product.returncode, 0, product.stdout)
            self.assertIn("units+gui+root-1 for cli-product-tests (product", product.stdout)
            self.finish("p0")
            self.assertEqual(self.job("tests-build-and-lag", "p1").returncode, 0)
            refused = self.job("cli-product-tests", "p2", 4, None, "--gui-wait", "0")
            self.assertIn("refused: capacity: the gui token is taken", refused.stdout)
        finally:
            for runner in ("p0", "p1", "p2"):
                self.finish(runner)

    def test_capacity_gui_job_waits_for_the_gui_token(self) -> None:
        self.fleet()
        try:
            self.assertEqual(self.job("tests-build-and-lag", "w0").returncode, 0)
            waited = time.monotonic()
            busy = self.job("tests-build-and-lag", "w1", 4, None, "--gui-wait", "3")
            self.assertEqual(busy.returncode, 1, busy.stdout)
            self.assertIn("refused: capacity: the gui token is taken", busy.stdout)
            self.assertGreaterEqual(time.monotonic() - waited, 3, "a gui job waits for the token before refusal")
            # the holder finishes while the next one waits: it is admitted, not refused
            release = threading.Timer(2.0, self.finish, args=("w0",))
            release.start()
            try:
                admitted = self.job("tests-build-and-lag", "w2", 4, None, "--gui-wait", "30")
            finally:
                release.join()
            self.assertEqual(admitted.returncode, 0, admitted.stdout)
            self.assertIn("+gui", admitted.stdout)
            # the wait never overshoots its deadline by a poll interval
            waited = time.monotonic()
            self.job("tests-build-and-lag", "w5", 4, None, "--gui-wait", "1")
            self.assertLess(time.monotonic() - waited, 4.5, "the last try starts by the deadline")
            # a units refusal waits the same --gui-wait before it refuses (CAPACITY_WAIT)
            self.finish("w2")
            self.assertEqual(self.job("macos-compile-admission", "w3", 4).returncode, 0)
            waited = time.monotonic()
            full = self.job("cli-product-tests", "w4", 4, None, "--gui-wait", "2")
            self.assertIn("refused: capacity:", full.stdout)
            self.assertNotIn("gui token", full.stdout)
            self.assertGreaterEqual(time.monotonic() - waited, 2, "a units refusal waits too")
            self.assertLess(time.monotonic() - waited, 20, "then refuses")
        finally:
            for runner in ("w0", "w1", "w2", "w3", "w4", "w5"):
                self.finish(runner)

    def test_capacity_a_cmux_job_waits_for_a_root_with_no_limit_and_in_turn(self) -> None:
        # cmux PR #15160 (2026-09-28): a compile handed to a root runner while both roots were held was refused
        # after 240 s, and a newer compile took the root that freed meanwhile. It now waits, with no limit, says
        # for what and who holds it, and a later compile waits behind it.
        self.fleet()
        capacity = self.dir / "capacity"
        slots = ("--compile-slots", "2", "--canonical-roots", "2")
        root = hook.RunnerScope(units=15, compile_slots=2, roots=2, root=True)
        side = hook.RunnerScope(units=15, compile_slots=2, roots=2, root=False)
        waiter = None
        try:
            for n in range(2):
                held = self.job("macos-compile-admission", f"h{n}", 15, None, *slots, "--instance", str(n),
                                env={"GITHUB_RUN_ID": f"10{n}"})
                self.assertEqual(held.returncode, 0, held.stdout + held.stderr)
            push = event(self.dir, "push", {"repository": CMUX})
            environ = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GLAEDA_RUNNER_TELEMETRY": "0",
                       "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(self.dir / "fleet"),
                       "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": os.fspath(push),
                       "GITHUB_REPOSITORY": "manaflow-ai/cmux", "GITHUB_JOB": "macos-compile-admission",
                       "RUNNER_NAME": "w0", "GITHUB_RUN_ID": "200"}
            # no --gui-wait: the default has no limit
            waiter = subprocess.Popen([sys.executable, os.fspath(HOOK), "job-started", "--allowed-repo",
                                       "manaflow-ai/cmux", "--no-disk", "--state-dir", os.fspath(self.dir / "state"),
                                       "--watch-pid", str(os.getpid()), "--capacity-units", "15", "--capacity-dir",
                                       os.fspath(capacity), *slots, "--instance", "1"],
                                      env=environ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            deadline = time.monotonic() + 20
            while not list(capacity.glob("admit.want-*")) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertTrue(list(capacity.glob("admit.want-*")), "the waiter leaves its marker")
            self.assertIsNone(waiter.poll(), "it waits instead of refusing")
            # the root runners stop listening while it waits for a root; a side runner's light job still fits
            self.assertEqual(hook.mini_full(capacity, 15, root),
                             "w0 macos-compile-admission (run 200) waits here for persistent-dd")
            self.assertIsNone(hook.mini_full(capacity, 15, side))
            # a newer compile waits behind the older waiter (--gui-wait 0 refuses at once), a light job does not
            late = self.job("macos-compile-admission", "n0", 15, None, *slots, "--instance", "0")
            self.assertEqual(late.returncode, 1, late.stdout)
            self.assertIn("w0 macos-compile-admission (run 200) has waited longer here for persistent-dd",
                          late.stdout)
            self.assertEqual(self.job("swift-package-tests", "l0", 15).returncode, 0, "a light job needs no root")
            self.finish("h0")
            out, _ = waiter.communicate(timeout=30)
            self.assertEqual(waiter.returncode, 0, out)
            self.assertRegex(out, r"waiting \(\d+ s so far\): all 2 persistent-dd tokens are taken "
                                  r"\(macos-compile-admission is compile\)")
            if sys.platform == "darwin":  # the holders come from lsof and ps -E, macOS here
                self.assertIn("held by persistent-dd: h0 macos-compile-admission (run 100); "
                              "persistent-dd-1: h1 macos-compile-admission (run 101)", out)
            self.assertIn("admitted:", out)
            self.assertIn("persistent-dd+root-1", out)
            self.assertEqual(list(capacity.glob("admit.want-*")), [], "the marker goes with the wait")
            self.assertIsNone(hook.mini_full(capacity, 15, side))
        finally:
            if waiter is not None and waiter.poll() is None:
                waiter.kill()
                waiter.communicate()
            for runner in ("h0", "h1", "w0", "n0", "l0"):
                self.finish(runner)

    def test_capacity_waiter_markers_are_ordered_and_expire(self) -> None:
        capacity = self.dir / "capacity"
        capacity.mkdir()
        now = time.time()
        for pid, since, wants in ((os.getppid(), now - 5, ["root"]), (1, now - 9, ["units"])):
            (capacity / f"admit.want-{pid}").write_text(json.dumps({"since": since, "wants": wants, "who": str(pid)}))
        (capacity / "admit.want-999999").write_text(json.dumps({"since": now - 20, "wants": ["root"]}))
        stale = capacity / f"admit.want-{os.getpid() + 0}"
        stale.write_text(json.dumps({"since": now - 30, "wants": ["root"]}))
        old = now - hook.ADMIT_FRESH_S - 5
        os.utime(stale, (old, old))
        (capacity / "admit.want-junk").write_text("{}")
        waiters = hook.admission_waiters(capacity)
        self.assertEqual([w["who"] for w in waiters], ["1", str(os.getppid())], "oldest first, live and fresh only")
        self.assertFalse((capacity / "admit.want-999999").exists(), "a dead waiter's marker is removed")
        self.assertFalse(stale.exists(), "a stale marker is removed")
        self.assertIn("has waited longer here for units", hook.fair_turn(capacity, {"units"}, now))
        self.assertIsNone(hook.fair_turn(capacity, {"simulator"}, now), "no shared kind: no turn to wait for")
        self.assertIsNone(hook.fair_turn(capacity, {"root", "units"}, now - 60), "an older admission goes first")

    def test_tiers_order_the_admission_queue(self) -> None:
        """FEED_FIRST: a nightly job goes ahead of every waiter however long it has waited, and while one waits no
        admission of another tier takes a unit, not even one its own gate's reserve covers. A DEV compile goes ahead
        of a PR job that has waited less than FEED_JUMP_S. Within a tier, arrival order."""
        capacity = self.dir / "capacity"
        capacity.mkdir()
        now, bound = time.time(), hook.FEED_JUMP_S
        marker = capacity / f"{hook.ADMIT_WANT}{os.getppid()}"

        def waiting(since: float, tier: object = None, wants: tuple[str, ...] = ("units",)) -> None:
            want: dict[str, object] = {"since": since, "wants": list(wants), "who": "w0 job (run 1)"}
            if tier is not None:
                want["tier"] = tier
            marker.write_text(json.dumps(want))

        nightly, dev = hook.TIER_NIGHTLY, hook.TIER_DEV
        waiting(now - bound - 600)  # a PR job long past the bound
        self.assertIsNone(hook.fair_turn(capacity, {"units"}, now, nightly, True), "a nightly job goes first")
        self.assertIn("has waited longer here for units", hook.fair_turn(capacity, {"units"}, now, dev, True))
        waiting(now - 60)  # a PR job, a minute in
        self.assertIsNone(hook.fair_turn(capacity, {"units"}, now, dev, True), "a DEV compile goes first")
        self.assertIn("has waited longer here for units", hook.fair_turn(capacity, {"units"}, now))
        waiting(now - 5, nightly, ())  # a nightly job that came later and waits for no unit (a token, say)
        for tier, since in (("", now - bound - 600), (dev, now - 60), ("", now - 60)):
            with self.subTest(tier=tier, since=since):
                self.assertIn("is a nightly job and goes first here for units",
                              hook.fair_turn(capacity, set(), since, tier, True))
                self.assertIsNone(hook.fair_turn(capacity, set(), since, tier, False), "no unit taken: no wait")
        waiting(now - 5, nightly)
        self.assertIsNone(hook.fair_turn(capacity, {"units"}, now - 60, nightly, True), "nightly: arrival order")
        fits = (capacity, 14, 1, 1, os.fspath(self.dir / "no-fleet" / "host.lock"),
                os.fspath(self.dir / "no-reservation"), self.dir / "state", now)
        waiting(now - 5, None, ("root",))  # a PR job waiting for a root leaves unit-only ci-steps fitting
        self.assertEqual(hook.step_fits(*fits), list(hook.STEP_CLASSES))
        waiting(now - 5, nightly, ("root",))  # an older nightly job waiting for a token holds a later one's units too
        self.assertIn("has waited longer here for units",
                      hook.fair_turn(capacity, {"persistent-dd"}, now, nightly, True))
        self.assertEqual(hook.step_fits(*fits), [], "no ci-step fits while a nightly job waits")
        waiting(now - 5, nightly)
        self.assertIn("has waited longer", hook.fair_turn(capacity, {"units"}, now, nightly, True))
        waiting(now - 5, dev)  # a DEV compile that came later
        self.assertIn("is a DEV compile and goes first here for units", hook.fair_turn(capacity, {"units"}, now - 60))
        self.assertIsNone(hook.fair_turn(capacity, {"units"}, now - bound - 10), "past the bound: arrival order")
        waiting(now - 5, "boss")  # only the known tiers count
        self.assertEqual([w["tier"] for w in hook.admission_waiters(capacity)], [""])
        for since in (True, "inf", "nan", float("inf")):  # a since that would break the total order is no waiter
            with self.subTest(since=since):
                marker.write_text(json.dumps({"since": since, "wants": ["units"]}) if not isinstance(since, float)
                                  else '{"since": Infinity, "wants": ["units"]}')
                self.assertEqual(hook.admission_waiters(capacity), [])
        for name in ("0", "-5", str(2 ** 64)):  # nor a pid no process has
            with self.subTest(pid=name):
                bad = capacity / f"{hook.ADMIT_WANT}{name}"
                bad.write_text(json.dumps({"since": now, "wants": ["units"]}))
                self.assertEqual([w["pid"] for w in hook.admission_waiters(capacity)], [])
                bad.unlink()
        waiting(now - 5, "boss")
        self.assertIsNone(hook.fair_turn(capacity, {"units"}, now - 60))
        # one total order: of any two waiters, exactly one is ahead
        tiers = ("", dev, nightly)
        for a in tiers:
            for b in tiers:
                for age_a, age_b in ((60, 5), (bound + 60, 5), (bound + 60, bound + 5), (5, 60), (5, bound + 60)):
                    ra = hook.admission_rank(a, now - age_a, 10, now)
                    rb = hook.admission_rank(b, now - age_b, 11, now)
                    self.assertNotEqual(ra < rb, rb < ra, (a, age_a, b, age_b))

    def test_a_waiting_job_marks_its_tier(self) -> None:
        capacity = self.dir / "capacity"
        capacity.mkdir()
        marker = capacity / f"{hook.ADMIT_WANT}{os.getppid()}"
        hook.write_want(marker, 1.0, ["units"])
        self.assertNotIn("tier", json.loads(marker.read_text()))
        hook.write_want(marker, 1.0, ["units"], hook.TIER_NIGHTLY)
        self.assertEqual(json.loads(marker.read_text())["tier"], hook.TIER_NIGHTLY)
        self.assertEqual([w["tier"] for w in hook.admission_waiters(capacity)], [hook.TIER_NIGHTLY])

    def test_capacity_runners_wait_for_units(self) -> None:
        # a 4-core side lane waits for units instead of refusing, on a side runner (instance past
        # --canonical-roots) and on a root runner alike
        self.fleet()
        release = None
        try:
            self.assertEqual(self.job("macos-compile-admission", "u0", 9).returncode, 0)
            self.assertEqual(self.job("claude-wrapper", "u1", 9).returncode, 0)
            waited = time.monotonic()
            root = self.job("release-build", "u2", 9, None, "--gui-wait", "2")
            self.assertEqual(root.returncode, 1, root.stdout)
            self.assertIn("refused: capacity: 2 of 9 units free", root.stdout)
            self.assertGreaterEqual(time.monotonic() - waited, 2, "a root runner waits for units too")
            release = threading.Timer(2.0, self.finish, args=("u1",))
            release.start()
            side = self.job("release-build", "u3", 9, None, "--instance", "1", "--gui-wait", "30")
            release.join()
            self.assertEqual(side.returncode, 0, side.stdout)
            self.assertIn("holding 4/9 units for release-build (isolated", side.stdout)
            # the wait ends at its deadline
            waited = time.monotonic()
            late = self.job("release-build", "u4", 9, None, "--instance", "1", "--gui-wait", "2")
            self.assertEqual(late.returncode, 1, late.stdout)
            self.assertGreaterEqual(time.monotonic() - waited, 2)
            self.assertLess(time.monotonic() - waited, 20, "a 2 s wait, then a refusal")
        finally:
            if release is not None:
                release.cancel()
            for runner in ("u0", "u1", "u2", "u3", "u4"):
                self.finish(runner)

    def owner_gone_checks(self, out: str, elapsed: float) -> None:
        """A job whose Runner.Worker went away mid-wait: the wait ends at once, nothing is held, and the hook never
        watches launchd (cmux10s 2026-10-20: "the watched process 1 has no stable identity", a refused-1 note)."""
        self.assertNotIn("no stable identity", out)
        self.assertNotIn("watching pid 1)", out)
        self.assertLess(elapsed, 20, "a dead owner ends the wait; it never sits out --gui-wait")
        state = self.dir / "state"
        self.assertEqual(sorted(p.name for p in state.glob("refused-*")), [], "no refusal note for a gone job")
        self.assertFalse(hook.holder_alive(state / "host-lock-holder-u2.pid"), out)

    def test_capacity_a_worker_that_dies_mid_wait_is_abandoned_not_refused(self) -> None:
        # The job's Runner.Worker (here a sleep the hook is told to watch) exits while its hook waits for units,
        # as a cancelled job's Worker does. The hook lets go of the wait instead of refusing on a later parent.
        self.fleet()
        worker = subprocess.Popen(["/bin/sleep", "60"])
        try:
            self.assertEqual(self.job("macos-compile-admission", "u0", 9).returncode, 0)
            self.assertEqual(self.job("claude-wrapper", "u1", 9).returncode, 0)
            # the hook's own output says when it is queued; only then does its Worker go (reaped: no zombie)
            began = time.monotonic()
            env = {"GITHUB_JOB": "release-build", "RUNNER_NAME": "u2"}
            push = event(self.dir, "push", {"repository": CMUX})
            hook_run = subprocess.Popen(
                [sys.executable, os.fspath(HOOK), "job-started", "--allowed-repo", "manaflow-ai/cmux", "--no-disk",
                 "--state-dir", os.fspath(self.dir / "state"), "--watch-pid", str(worker.pid),
                 "--capacity-units", "9", "--capacity-dir", os.fspath(self.dir / "capacity"), "--gui-wait", "60"],
                env={"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GLAEDA_RUNNER_TELEMETRY": "0",
                     "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(self.dir / "fleet"),
                     "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": os.fspath(push),
                     "GITHUB_REPOSITORY": "manaflow-ai/cmux", **env},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            lines = []
            for line in hook_run.stdout:
                lines.append(line)
                if "waiting (" in line and worker.poll() is None:
                    worker.kill()
                    worker.wait()
            hook_run.wait(timeout=60)
            elapsed = time.monotonic() - began
            out = "".join(lines)
            gone = hook_run
            self.assertNotEqual(gone.returncode, 0, out)
            self.assertIn("waiting (", out, "it was waiting for units when its Worker went")
            self.assertTrue("Runner.Worker is gone" in out or "orphaned job-started hook" in out, out)
            self.owner_gone_checks(out, elapsed)
        finally:
            worker.kill()
            worker.wait()
            for runner in ("u0", "u1", "u2"):
                self.finish(runner)

    def test_capacity_a_reparented_hook_never_watches_launchd(self) -> None:
        # No --watch-pid: the hook pins its real parent at start. The parent exits mid-wait and the hook is
        # reparented (to launchd on a mini); it must not then take os.getppid() as the job.
        self.fleet()
        push = event(self.dir, "push", {"repository": CMUX})
        environ = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GLAEDA_RUNNER_TELEMETRY": "0",
                   "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(self.dir / "fleet"),
                   "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": os.fspath(push),
                   "GITHUB_REPOSITORY": "manaflow-ai/cmux", "GITHUB_JOB": "release-build", "RUNNER_NAME": "u2"}
        log = self.dir / "orphan.out"
        args = [sys.executable, os.fspath(HOOK), "job-started", "--allowed-repo", "manaflow-ai/cmux", "--no-disk",
                "--state-dir", os.fspath(self.dir / "state"), "--capacity-units", "9",
                "--capacity-dir", os.fspath(self.dir / "capacity"), "--gui-wait", "60"]
        try:
            self.assertEqual(self.job("macos-compile-admission", "u0", 9).returncode, 0)
            self.assertEqual(self.job("claude-wrapper", "u1", 9).returncode, 0)
            # the parent starts the hook as a child (no exec), waits until it is queued, then exits
            script = ('"$@" >"$LOG" 2>&1 & echo $! >"$LOG.pid"; '
                      'for _ in $(seq 100); do grep -q "waiting (" "$LOG" 2>/dev/null && break; sleep 0.1; done; exit 0')
            began = time.monotonic()
            parent = subprocess.run(["/bin/bash", "-c", script, "parent", *args], env={**environ, "LOG": os.fspath(log)},
                                    timeout=30, check=False)
            self.assertEqual(parent.returncode, 0)
            child = int(Path(f"{log}.pid").read_text())
            deadline = time.monotonic() + 30
            while hook.pid_alive(child) and time.monotonic() < deadline:
                time.sleep(0.2)
            elapsed = time.monotonic() - began
            out = log.read_text()
            self.assertFalse(hook.pid_alive(child), "the orphaned hook outlived its parent: " + out)
            self.assertIn("waiting (", out)
            self.owner_gone_checks(out, elapsed)
        finally:
            for runner in ("u0", "u1", "u2"):
                self.finish(runner)

    def test_hold_never_hands_locks_to_a_holder_watching_nobody(self) -> None:
        state = self.dir / "state"
        for watch in (0, 1):  # launchd, or no pid at all, is never a job
            self.assertIsNone(hook.JobOwner.of(watch))
            r, w = os.pipe()
            os.close(r)
            ok, why = hook.hold([w], watch, state)
            self.assertFalse(ok)
            self.assertIn("no stable identity", why)
            with self.assertRaises(OSError, msg="the fd went back"):
                os.fstat(w)
        dead = subprocess.Popen(["/bin/sleep", "60"])
        owner = hook.JobOwner.of(dead.pid)
        self.assertIsNotNone(owner)
        dead.kill()
        dead.wait()
        r, w = os.pipe()
        os.close(r)
        ok, why = hook.hold([w], owner, state)
        self.assertFalse(ok)
        self.assertIn(hook.OWNER_GONE, why)
        with self.assertRaises(OSError):
            os.fstat(w)
        self.assertFalse(hook.holder_file(state).exists(), "no holder was started")
        # a pinned owner survives the hook being reparented: job_owner never falls back to launchd
        with mock.patch.object(hook, "runner_worker_pid", return_value=None), \
                mock.patch.object(hook.os, "getppid", return_value=1):
            self.assertIsNone(hook.job_owner())
        me = hook.job_owner(os.getpid())
        self.assertEqual(me.pid, os.getpid())
        self.assertTrue(me.alive())
        self.assertFalse(me.gone())
        with mock.patch.object(hook, "process_identity", return_value=None):
            self.assertFalse(me.gone(), "an unreadable start token is unknown, never a gone owner")
        with mock.patch.object(hook, "process_identity", return_value="ps:reused"):
            self.assertTrue(me.gone(), "a reused pid is another process")

    def test_capacity_e2e_build_takes_the_gui_token_for_its_tests_only(self) -> None:
        # test-e2e's build compiles, then takes the gui token itself (take-gui) before its console-session tests
        self.fleet()
        e2e = {"GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/test-e2e.yml@refs/heads/main"}
        two = ("--canonical-roots", "2", "--compile-slots", "2")
        env_file = self.dir / "github_env"
        state = self.dir / "state"
        try:
            build = self.job("build", "e0", 12, None, *two, env={**e2e, "GITHUB_ENV": os.fspath(env_file)})
            self.assertEqual(build.returncode, 0, build.stdout)
            self.assertIn("holding 4/12 units+root-1 for build (compile-gui", build.stdout)
            self.assertNotIn("gui+", build.stdout)
            self.assertNotIn("persistent-dd", build.stdout)
            self.assertFalse((state / "host-lock-holder-e0.gui").exists())
            self.assertEqual(env_file.read_text(), "CMUX_CI_CANONICAL_ROOT=/private/tmp/cmux-ci\n")
            # its own restore step re-takes the root it holds: a no-op
            self.assertEqual(self.take("/private/tmp/cmux-ci", "e0").returncode, 0)
            # while it compiles, a GUI job runs beside it, and so does a compile admission in the other root
            shard = self.job("tests-build-and-lag", "g0", 12, None, *two, "--gui-wait", "0")
            self.assertIn("+gui for tests-build-and-lag (gui", shard.stdout)
            self.assertIn("persistent-dd+root-2", self.job("macos-compile-admission", "e1", 12, None, *two).stdout)
            # the build's tests wait for that shard's token, then take it
            self.assertEqual(self.take_gui("e0", "--wait", "1").returncode, 1, "the shard still holds it")
            self.finish("g0")
            self.assertEqual(self.take_gui("e0", "--wait", "5").returncode, 0)
            refused = self.job("tests-build-and-lag", "g1", 12, None, *two, "--gui-wait", "0")
            self.assertIn("refused: capacity: the gui token is taken", refused.stdout)
            self.assertIn("the gui token holder released", self.finish("e0"))
            # with root 1 free, a build on the second root runner still takes it: root 1's seeds and caches
            # are the ones main publishes, and it keeps no per-root state to prefer its own root for
            first = self.job("build", "e3", 12, None, *two, "--instance", "1", env=e2e)
            self.assertIn("units+root-1 for build (compile-gui", first.stdout)
            self.finish("e3")
            # the fallback test job is a consumer: gui from its take-gui step, then the producer's root from its
            # restore step
            test = self.job("test", "e2", 12, None, *two, env=e2e)
            self.assertEqual(test.returncode, 0, test.stdout)
            self.assertIn("holding 1/12 units for test (gui-step", test.stdout)
            self.assertEqual(self.take_gui("e2").returncode, 0)
            self.assertEqual(self.take("/private/tmp/cmux-ci-2", "e2", "--wait", "0").returncode, 1,
                             "the compile admission still holds root 2")
            self.assertEqual(self.take("/private/tmp/cmux-ci", "e2").returncode, 0)
        finally:
            for runner in ("e0", "e1", "e2", "e3", "g0", "g1"):
                self.finish(runner)
        self.assertTrue(self.lock_free())

    def test_capacity_shard_takes_the_gui_token_before_its_restore(self) -> None:
        # an app-host shard (gui-step) fetches its product without the gui token and takes it with take-gui in
        # the step before its restore takes the producer's root: gui, then root, the order gui jobs take them in
        self.fleet()
        two = ("--canonical-roots", "2", "--compile-slots", "2")
        state = self.dir / "state"
        try:
            shard = self.job("app-host-unit-tests", "s0", 8, None, *two, "--gui-wait", "0")
            self.assertEqual(shard.returncode, 0, shard.stdout)
            self.assertIn("holding 1/8 units for app-host-unit-tests (gui-step", shard.stdout)
            self.assertFalse((state / "host-lock-holder-s0.gui").exists())
            # while the shard fetches, another GUI job takes the console session
            other = self.job("tests-build-and-lag", "g0", 8, None, *two, "--gui-wait", "0")
            self.assertIn("+gui for tests-build-and-lag (gui", other.stdout)
            self.assertEqual(self.take_gui("s0", "--wait", "1").returncode, 1, "the GUI job still holds it")
            self.finish("g0")
            self.assertEqual(self.take_gui("s0", "--wait", "5").returncode, 0)
            self.assertTrue((state / "host-lock-holder-s0.gui").exists())
            self.assertEqual(self.take_gui("s0").returncode, 0, "a second take-gui is a no-op")
            self.assertEqual(self.take("/private/tmp/cmux-ci", "s0").returncode, 0)
            refused = self.job("tests-build-and-lag", "g1", 8, None, *two, "--gui-wait", "0")
            self.assertIn("refused: capacity: the gui token is taken", refused.stdout)
            self.assertIn("the gui token holder released", self.finish("s0"))
            # a one-root mini gives the shard gui and its root at job start, as for a gui job
            one = self.job("app-host-unit-tests", "s1", 8, None, "--gui-wait", "0")
            self.assertIn("holding 1/8 units+gui+root-1 for app-host-unit-tests (gui-step", one.stdout)
            self.assertEqual(self.take_gui("s1").returncode, 0, "its step's take-gui is a no-op")
        finally:
            for runner in ("s0", "s1", "g0", "g1"):
                self.finish(runner)
        self.assertTrue(self.lock_free())

    def test_capacity_gui_step_custom_token_is_taken_only_in_its_step(self) -> None:
        """A VM desktop token must remain free until gui-step's take-gui step, too."""
        self.fleet()
        two = ("--canonical-roots", "2", "--compile-slots", "2", "--gui-token", "gui-vm")
        try:
            shard = self.job("app-host-unit-tests", "v0", 8, None, *two, "--gui-wait", "0")
            self.assertEqual(shard.returncode, 0, shard.stdout)
            self.assertIn("holding 1/8 units for app-host-unit-tests (gui-step", shard.stdout)
            self.assertNotIn("gui-vm", shard.stdout)
            self.assertEqual(self.take_gui("v0", "--gui-token", "gui-vm", "--wait", "1").returncode, 0)
        finally:
            self.finish("v0")
        self.assertTrue(self.lock_free())

    def test_capacity_e2e_test_job_takes_the_gui_token_in_its_step(self) -> None:
        # test-e2e's `test` runs when the build could not get the gui token: at job start it takes neither, so a
        # busy token is waited for in its take-gui step instead of refusing the job after GUI_WAIT_S
        self.fleet()
        e2e = {"GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/test-e2e.yml@refs/heads/main"}
        two = ("--canonical-roots", "2", "--compile-slots", "2")
        try:
            shard = self.job("tests-build-and-lag", "g0", 8, None, *two, "--gui-wait", "0")
            self.assertIn("+gui for tests-build-and-lag (gui", shard.stdout)
            test = self.job("test", "t0", 8, None, *two, "--gui-wait", "0", env=e2e)
            self.assertEqual(test.returncode, 0, test.stdout)
            self.assertIn("holding 1/8 units for test (gui-step", test.stdout)
            self.assertEqual(self.take_gui("t0", "--wait", "1").returncode, 1, "the GUI job still holds it")
            self.finish("g0")
            self.assertEqual(self.take_gui("t0", "--wait", "5").returncode, 0)
        finally:
            for runner in ("g0", "t0"):
                self.finish(runner)
        self.assertTrue(self.lock_free())

    def test_capacity_wait_continues_when_the_refusal_turns_to_units(self) -> None:
        self.fleet()
        release = None
        ci = {"GITHUB_WORKFLOW_REF": "manaflow-ai/cmux/.github/workflows/ci.yml@refs/heads/main"}  # 1 core each
        try:
            self.assertEqual(self.job("tests-build-and-lag", "x0", 4).returncode, 0)
            self.assertEqual(self.job("claude-wrapper", "x1", 4, None, env=ci).returncode, 0)
            self.assertEqual(self.job("claude-wrapper", "x2", 4, None, env=ci).returncode, 0)
            self.assertEqual(self.job("claude-wrapper", "x5", 4, None, env=ci).returncode, 0)
            # the gui holder leaves, but the units it frees go to a light job first: the refusal turns to units
            def swap() -> None:
                self.finish("x0")
                self.job("claude-wrapper", "x3", 4, None, env=ci)
            release = threading.Timer(2.0, swap)
            release.start()
            waited = time.monotonic()
            result = self.job("tests-build-and-lag", "x4", 4, None, "--gui-wait", "5")
            release.join()
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("units free", result.stdout)
            self.assertGreaterEqual(time.monotonic() - waited, 5, "a units refusal after a gui one keeps waiting")
            self.assertLess(time.monotonic() - waited, 20, "until the deadline")
        finally:
            if release is not None:
                release.cancel()
            for runner in ("x0", "x1", "x2", "x3", "x4", "x5"):
                self.finish(runner)

    def test_job_class_keys_on_workflow_file_and_job_id(self) -> None:
        wf = hook.workflow_file
        self.assertEqual(wf("manaflow-ai/cmux/.github/workflows/cmux-tui.yml@refs/pull/1/merge"), "cmux-tui.yml")
        self.assertEqual(wf("manaflow-ai/cmux/.github/workflows/test-e2e.yml@refs/heads/main"), "test-e2e.yml")
        for bad in (None, "", "cmux-tui.yml", "manaflow-ai/cmux/cmux-tui.yml@main"):
            self.assertEqual(wf(bad), "")
        home = "manaflow-ai/cmux"
        for job in ("lint", "test", "build"):
            self.assertEqual(hook.job_class(job, home, home, "cmux-tui.yml"), ("isolated", False))
            # the same id in a workflow the table does not name keeps the unknown-job default: compile, pinned
            self.assertEqual(hook.job_class(job, home, home, "other.yml"), ("compile", True))
            self.assertEqual(hook.job_class(job, home, home), ("compile", True))
        # test-e2e runs its tests in the console session: build compiles and tests, test is the fallback
        self.assertEqual(hook.job_class("build", home, home, "test-e2e.yml"), ("compile-gui", False))
        self.assertEqual(hook.job_class("test", home, home, "test-e2e.yml"), ("gui-step", False))
        self.assertEqual(hook.job_class("lint", home, home, "test-e2e.yml"), ("compile", True))
        self.assertEqual(hook.job_class("build", "someone/else", home, "test-e2e.yml"), ("isolated", False))
        # cmux-next's fast tier and Swift canary build SwiftPM packages (and cargo tests) in the workspace only:
        # no kept DerivedData or canonical root, so no persistent-dd wait behind a compile (hq#1915: the fast
        # tier waited 11.4 min for the token for a 52 s run, cmux run 38352638705)
        for job in ("fast-tier", "swift-canary"):
            self.assertEqual(hook.job_class(job, home, home, "cmux-next.yml"), ("isolated", False))
        self.assertEqual(hook.CLASS_COST["compile-gui"], (4, ("root",)),
                         "no persistent-dd: the E2E build never writes compile admission's kept DerivedData")
        self.assertNotIn("compile-gui", hook.ROOT_CONSUMERS, "a producer takes a token-chosen root")
        for klass in {*hook.JOB_CLASSES.values(), *hook.WORKFLOW_JOB_CLASSES.values()}:
            self.assertIn(klass, hook.CLASS_COST)
        # a seed job holds the root its matrix names itself (take-root), so no token picks one for it
        self.assertEqual(hook.job_class("seed", home, home, "seed-derived-data.yml"), ("seed", False))
        self.assertEqual(hook.CLASS_COST["seed"], (5, ()))
        self.assertEqual(hook.job_class("seed", home, home, "other.yml"), ("compile", True))
        # the nightly app build compiles in its own workspace: no root a seed on the trusted mini waits for
        self.assertEqual(hook.job_class("build-nightly-app", home, home, "nightly.yml"), ("isolated", False))
        self.assertEqual(hook.job_class("build-nightly-app", home, home, "other.yml"), ("compile", True))
        # the nightly Ghostty helper runs Zig in its workspace: light (no root), sized by its measured cores
        self.assertEqual(hook.job_class("build-nightly-ghostty-cli-helper", home, home, "nightly.yml"), ("light", False))
        self.assertEqual(hook.job_units("light", "build-nightly-ghostty-cli-helper", home, home, "nightly.yml"), 4)
        self.assertEqual(hook.job_class("rerun", home, home, "app-host-test-rerun.yml"), ("gui", False))
        self.assertEqual(hook.job_class("cdp-browser-smoke", home, home, "cmux-tui.yml"), ("isolated", False))
        # cmux-tui's macOS relay lane only runs chatmux-relay Rust tests in the workspace: no root or GUI token.
        self.assertEqual(hook.job_class("macos-relay", home, home, "cmux-tui.yml"), ("isolated", False))
        self.assertEqual(hook.job_class("build", home, home, "reload-build.yml"), ("isolated", False))
        # cmux-next's side-lane jobs build in their workspace or $RUNNER_TEMP: no root, no kept DerivedData
        self.assertEqual(hook.job_class("swift-test", home, home, "cmux-next.yml"), ("isolated", False))
        self.assertEqual(hook.job_class("release-compile", home, home, "cmux-next.yml"), ("isolated", False))
        # the Release compiles cost their measured p75 cores, not the class's 4
        for workflow, job in (("cmux-next.yml", "release-compile"), ("nightly.yml", "build-nightly-app"),
                              ("cmux-next.yml", "cmux-scheme-compile")):
            with self.subTest(job=job):
                self.assertEqual(hook.job_units(hook.job_class(job, home, home, workflow)[0], job, home, home,
                                                workflow), 3)
        self.assertEqual(hook.job_class("cmux-scheme-compile", home, home, "cmux-next.yml"), ("isolated", False))
        # cmux-next's nightly frame-pacing bench: reload-build's `build`, called from it (GITHUB_WORKFLOW_REF names
        # the caller), builds in its workspace; `bench` takes the gui token itself (take-gui) and draws in the console
        self.assertEqual(hook.job_class("build", home, home, "cmux-next-frame-pacing.yml"), ("isolated", False))
        self.assertEqual(hook.job_class("bench", home, home, "cmux-next-frame-pacing.yml"), ("gui-step", False))
        self.assertEqual(hook.job_class("bench", home, home, "other.yml"), ("compile", True))
        # cmux-next's session-only jobs on the gui runner build SwiftPM in the workspace (owned_spm_scratch keeps
        # .build per workspace path) and run swift test in the logged-in session: no root, kept DerivedData or token
        for workflow, job in (("cmux-next.yml", "daemon-test"), ("cmux-next.yml", "generated-files"),
                              ("cmux-next-pr-autofix.yml", "action-exports"),
                              ("cmux-next-base-autofix.yml", "action-exports")):
            self.assertEqual(hook.job_class(job, home, home, workflow), ("isolated", False), (workflow, job))
            self.assertEqual(hook.job_class(job, home, home, "other.yml"), ("compile", True), job)
        # cmux-next media's tour holds its own tag in the workflow and captures only its app's window: isolated,
        # as many per mini as units allow
        self.assertEqual(hook.job_class("tour", home, home, "cmux-next-media.yml"), ("isolated", False))
        self.assertNotIn("tour", hook.CLASS_COST)
        self.assertEqual(hook.job_class("tour", home, home, "other.yml"), ("compile", True))
        self.assertEqual(hook.job_class("release-compile", home, home, "other.yml"), ("compile", True))
        self.assertEqual(hook.job_class("build", home, home, "nightly.yml"), ("compile", True))
        self.assertEqual(hook.job_class("rerun", home, home, "other.yml"), ("compile", True))
        self.assertEqual(hook.job_class("release-build", home, home, "ci.yml"), ("isolated", False))
        self.assertEqual(hook.job_class("macos-compile-admission", home, home, "ci.yml"), ("compile", False))
        self.assertEqual(hook.job_class("lint", "someone/else", home, "cmux-tui.yml"), ("isolated", False),
                         "a guest keeps the guest rule")
        # JOB_CORES: a job's own measured cores where they round to another number than its class's
        for key, cores in hook.JOB_CORES.items():
            repo, (workflow, job) = (home, key) if len(key) == 2 else (key[0], key[1:])
            klass = hook.job_class(job, repo, home, workflow)[0]
            self.assertNotEqual(cores, hook.CLASS_COST[klass][0], f"{key} is no different from its class")
            self.assertEqual(hook.job_units(klass, job, repo, home, workflow), cores, key)
        hq = "manaflow-ai/cmuxterm-hq"
        self.assertEqual(hook.job_units("isolated", "test", hq, home, "build-fleet-go.yml"), 1, "a guest's own cost")
        self.assertEqual(hook.job_units("isolated", "test", "MANAFLOW-AI/cmuxterm-hq", home, "build-fleet-go.yml"), 1)
        self.assertEqual(hook.job_units("isolated", "test", hq, home, "other.yml"), 4, "else its class's")
        self.assertEqual(hook.job_units("isolated", "generated-files", hq, home, "cmux-next.yml"), 4,
                         "a home-repo entry never costs a guest")
        self.assertEqual(hook.job_units("compile", "macos", home, home, "cmux-tui.yml"), 6)
        self.assertEqual(hook.job_units("compile", "macos", home, home, "other.yml"), 5)

    def test_capacity_guest_repo_jobs_never_take_cmux_roots(self) -> None:
        self.fleet()
        env = {"GITHUB_REPOSITORY": "manaflow-ai/newapp"}

        def guest(name: str, runner: str) -> subprocess.CompletedProcess:
            return self.job(name, runner, 20, None, "--allowed-repo", "manaflow-ai/newapp", env=env)
        try:
            self.assertIn("persistent-dd+root-1", self.job("macos-compile-admission", "h0", 20,
                                                           env={"GITHUB_REPOSITORY": "manaflow-ai/cmux"}).stdout)
            build = guest("build", "n0")
            self.assertEqual(build.returncode, 0, build.stdout)
            self.assertIn("holding 4/20 units for build (isolated", build.stdout)
            # a guest id that cmux also uses gets the guest cost, not cmux's compile class
            same = guest("macos-compile-admission", "n1")
            self.assertIn("(isolated", same.stdout)
            self.assertNotIn("root-", same.stdout)
            self.assertIn("2/20 units for lint-light (light", guest("lint-light", "n2").stdout)
            self.assertIn("+simulator for ui-tests-sim (simulator", guest("ui-tests-sim", "n3").stdout)
            waited = time.monotonic()
            busy = self.job("ios-simulator", "n4", 20, None, "--allowed-repo", "manaflow-ai/newapp",
                            "--guest-wait", "3", env=env)
            self.assertIn("simulator token is taken", busy.stdout)
            self.assertGreaterEqual(time.monotonic() - waited, 3, "a guest waits for room before it is refused")
        finally:
            for runner in ("h0", "n0", "n1", "n2", "n3", "n4"):
                self.finish(runner)

    # ------------------------------------------------------------ ci-step admission (cmuxterm-hq#794)

    def ci_step(self, phase: str, *extra: str, units: int = 7, env: dict | None = None) -> tuple[int, dict]:
        """Run admit, release or fits as the build worker does: one JSON line on stdout. 7 units: a light step (2),
        an isolated one (4) and one more."""
        result = self.run_hook(phase, None, None, "--capacity-units", str(units),
                               "--capacity-dir", os.fspath(self.dir / "capacity"),
                               "--state-dir", os.fspath(self.dir / "state"), *extra, env=env)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, result.stdout + result.stderr)
        return result.returncode, json.loads(lines[0])

    def ci_worker(self) -> subprocess.Popen:
        """A stand-in for the build worker whose pid a step's holder watches."""
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(worker.wait)
        self.addCleanup(worker.kill)
        return worker

    def fits(self, units: int = 7) -> list[str]:
        status, result = self.ci_step("fits", units=units)
        self.assertEqual((status, result["schema"], result["unknown"]), (0, "glaeda-ci-step-fits/v1", False))
        return result["fits"]

    def test_ci_step_admission_shares_the_runners_ledger(self) -> None:
        self.fleet()
        worker = self.ci_worker()
        watch = ("--watch-pid", str(worker.pid))
        try:
            self.assertEqual(self.fits(), ["light", "isolated"])
            status, first = self.ci_step("admit", "--class", "light", "--job-key", "r1-100-1", *watch)
            self.assertEqual(status, 0, first)
            self.assertEqual((first["schema"], first["admitted"], first["units"], first["units_total"],
                              first["tokens"]), ("glaeda-ci-step-admission/v1", True, 2, 7, []))
            self.assertIsInstance(first["holder_pid"], int)
            self.assertTrue(self.shared_lock_blocks_exclusive(), "a dev build's exclusive host lock waits for steps")
            self.assertEqual(self.fits(), ["light", "isolated"], "five of seven units are free")
            status, second = self.ci_step("admit", "--class", "isolated", "--job-key", "r1-101-1", *watch)
            self.assertEqual((status, second["units"]), (0, 4), second)
            self.assertEqual(self.fits(), [], "one unit is left: too few for any step")
            start = time.monotonic()
            status, full = self.ci_step("admit", "--class", "light", "--job-key", "r1-102-1", *watch)
            self.assertLess(time.monotonic() - start, 25, "a full mini refuses, it never waits")
            self.assertEqual((status, full["admitted"]), (1, False))
            self.assertIn("capacity: 1 of 7 units free", full["reason"])
            runner = self.job("swift-package-tests", "r0", 7)
            self.assertEqual(runner.returncode, 1, "a runner job sees the units the steps hold")
            self.assertIn("refused: capacity: 1 of 7 units free", runner.stdout)
            status, released = self.ci_step("release", "--job-key", "r1-100-1")
            self.assertEqual((status, released["released"]), (0, "released the fleet host lock"))
            self.assertEqual(self.fits(), ["light"], "three units are free again: too few for an isolated step")
        finally:
            for key in ("r1-100-1", "r1-101-1", "r1-102-1"):
                self.ci_step("release", "--job-key", key)
        self.assertTrue(self.lock_free(), "every step holder let go")

    def test_ci_steps_leave_an_idle_gui_runner_its_unit(self) -> None:
        """A ci-step is never the gui runner's job: fits and admit leave the reserved unit free as runner jobs do."""
        self.fleet()
        worker = self.ci_worker()
        self.take_units(2)
        self.assertEqual(self.fits(6), ["light", "isolated"])
        capacity = self.dir / "capacity"
        reserve = hook.lock_file(capacity / f"gui{hook.GUI_RESERVE}", fcntl.LOCK_EX)
        try:
            self.assertEqual(self.fits(6), ["light"], "4 free: an isolated step's 4 would take the kept unit")
            status, refused = self.ci_step("admit", "--class", "isolated", "--job-key", "r1-1-1",
                                           "--watch-pid", str(worker.pid), units=6)
            self.assertEqual(status, 1, refused)
            self.assertIn("capacity: 4 of 6 units free, ci-step r1-1-1 (isolated) needs 4 and 1 is kept for an idle "
                          "reserving runner", refused["reason"])
        finally:
            os.close(reserve)
        self.assertEqual(self.fits(6), ["light", "isolated"], "the reserve is gone with its gate")

    def test_ci_step_holder_ends_with_the_worker(self) -> None:
        self.fleet()
        worker = self.ci_worker()
        status, admitted = self.ci_step("admit", "--class", "light", "--job-key", "gone-1-1",
                                        "--watch-pid", str(worker.pid))
        self.assertEqual(status, 0, admitted)
        self.assertFalse(self.lock_free())
        worker.kill()
        worker.wait()
        deadline = time.monotonic() + 10
        while not self.lock_free() and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertTrue(self.lock_free(), "a crashed worker strands no capacity")
        status, again = self.ci_step("admit", "--class", "light", "--job-key", "gone-1-1",
                                     "--watch-pid", str(worker.pid))
        self.assertEqual(status, 1)
        self.assertIn("is not running", again["reason"])

    def test_ci_step_takes_nothing_while_the_rust_toolchain_is_missing(self) -> None:
        """2026-10-26: ~/.cargo/bin/rustup was deleted on the runner minis (rustc and cargo link to it). fits kept
        advertising light and isolated, so the controller kept leasing steps that could not run cargo. Now a fleet
        node (a --require-eligible runner) with a missing toolchain fits nothing, says why, and admit refuses."""
        self.fleet()
        hooks = self.dir / "actions-runner-glaeda" / "glaeda-hooks"
        hooks.mkdir(parents=True)
        (hooks / "job-started.sh").write_text("#!/bin/bash\nexec /usr/bin/python3 /hook job-started --require-eligible "
                                              "--fleet-class m4pro-48 --instance 0\n")
        bin_dir = self.dir / ".cargo" / "bin"
        bin_dir.mkdir(parents=True)
        for tool in ("rustc", "cargo"):
            (bin_dir / tool).symlink_to("rustup")
        # The worker's own PATH finds a rust elsewhere; the jobs' (the runner agent's WORKLOAD_PATH) is what counts.
        elsewhere = self.dir / "worker-path"
        elsewhere.mkdir()
        for tool in ("rustup", "rustc", "cargo"):
            make_executable(elsewhere / tool, "#!/bin/sh\nexit 0\n")
        workload = f"{bin_dir}:{self.dir / 'no-rust-here'}"
        env = {"PATH": os.fspath(elsewhere), "GLAEDA_RUNNER_WORKLOAD_PATH": workload}
        (hooks.parent / ".path").write_text(f"{elsewhere}\n")  # nothing reads .path: it never decides
        worker = self.ci_worker()
        watch = ("--watch-pid", str(worker.pid))
        status, fits = self.ci_step("fits", env=env)
        self.assertEqual((status, fits["fits"], fits["unknown"]), (0, [], False))
        self.assertIn(f"Rust toolchain missing: rustup is not installed (not on the job PATH {workload}, not in "
                      f"{bin_dir}); {bin_dir / 'rustc'} (a link to rustup", fits["refusal"])
        status, refused = self.ci_step("admit", "--class", "light", "--job-key", "rust-1-1", *watch, env=env)
        self.assertEqual((status, refused["admitted"]), (1, False))
        self.assertIn("Rust toolchain missing", refused["reason"])
        self.assertNotIn("units", refused, "a refused step holds no units")
        make_executable(bin_dir / "rustup", "#!/bin/sh\nexit 0\n")
        status, fits = self.ci_step("fits", env=env)
        self.assertEqual((fits["fits"], "refusal" in fits), (["light", "isolated"], False), "back as soon as it runs")
        try:
            status, admitted = self.ci_step("admit", "--class", "light", "--job-key", "rust-2-1", *watch, env=env)
            self.assertEqual(status, 0, admitted)
        finally:
            self.ci_step("release", "--job-key", "rust-2-1", env=env)
        (bin_dir / "rustup").unlink()
        (hooks / "job-started.sh").write_text("#!/bin/bash\nexec /usr/bin/python3 /hook job-started --instance 0\n")
        self.assertEqual(self.ci_step("fits", env=env)[1]["fits"], ["light", "isolated"],
                         "a mini with no fleet build runner needs no Rust for its steps")

    def test_ci_step_refusals_are_typed_and_never_wait(self) -> None:
        fleet = self.fleet()
        worker = self.ci_worker()
        watch = ("--watch-pid", str(worker.pid))
        try:
            status, first = self.ci_step("admit", "--class", "isolated", "--job-key", "iso-1-1", *watch)
            self.assertEqual((status, first["units"]), (0, 4), first)
            self.assertEqual(self.fits(), ["light"], "three units left: too few for an isolated step")
            status, second = self.ci_step("admit", "--class", "isolated", "--job-key", "iso-2-1", *watch)
            self.assertEqual(status, 1)
            self.assertIn("capacity: 3 of 7 units free", second["reason"])
            status, again = self.ci_step("admit", "--class", "light", "--job-key", "iso-1-1", *watch)
            self.assertEqual(status, 1, "a key holds one admission")
            self.assertIn("already admitted", again["reason"])
            status, prefixed = self.ci_step("admit", "--class", "light", "--job-key", "iso-1-1-gui", *watch)
            self.assertEqual(status, 0, "a key that extends another is its own step")
            self.ci_step("release", "--job-key", "iso-1-1-gui")
            self.assertEqual(self.fits(), ["light"], "releasing one key leaves the other's holder")
        finally:
            self.ci_step("release", "--job-key", "iso-1-1")
        for bad in (("--class", "compile", "--job-key", "k-1-1"), ("--class", "simulator", "--job-key", "k-1-1"),
                    ("--class", "light", "--job-key", "../k"), ("--class", "light")):
            with self.subTest(bad=bad):
                result = self.run_hook("admit", None, None, "--capacity-units", "3", *bad, *watch)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import fcntl,os,time\n"
                                   f"fd=os.open({os.fspath(fleet / 'host.lock')!r},os.O_RDWR)\n"
                                   "fcntl.flock(fd,fcntl.LOCK_EX)\nprint('held',flush=True)\ntime.sleep(30)\n"],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.assertEqual(self.fits(), [], "nothing fits while a dev build holds the host")
            status, refused = self.ci_step("admit", "--class", "light", "--job-key", "dev-1-1", *watch)
            self.assertEqual(status, 1)
            self.assertIn("capacity: a fleet build holds the host lock", refused["reason"])
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()

    @unittest.skipUnless(os.path.exists(hook.LSOF), "needs lsof")
    def test_ci_step_fits_nothing_while_a_fleet_build_waits(self) -> None:
        fleet = self.fleet()
        worker = self.ci_worker()
        status, first = self.ci_step("admit", "--class", "light", "--job-key", "wait-1-1", "--watch-pid", str(worker.pid))
        self.assertEqual(status, 0, first)
        waiter = subprocess.Popen([sys.executable, "-c",
                                   "import fcntl,os\n"
                                   f"fd=os.open({os.fspath(fleet / 'host.lock')!r},os.O_RDWR)\n"
                                   "print('waiting',flush=True)\nfcntl.flock(fd,fcntl.LOCK_EX)\n"],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(waiter.stdout.readline().strip(), "waiting")
            time.sleep(0.5)
            self.assertEqual(self.fits(), [], "fits agrees with admit, which refuses while a fleet build waits")
            status, refused = self.ci_step("admit", "--class", "light", "--job-key", "wait-2-1",
                                           "--watch-pid", str(worker.pid))
            self.assertEqual(status, 1)
            self.assertIn("a fleet build is waiting for the host", refused["reason"])
            self.ci_step("release", "--job-key", "wait-1-1")
            self.assertEqual(waiter.wait(timeout=10), 0, "the build worker gets the host once the step lets go")
        finally:
            waiter.kill()
            waiter.wait()
            waiter.stdout.close()
            self.ci_step("release", "--job-key", "wait-1-1")

    def test_ci_step_fits_is_unknown_while_an_admission_runs(self) -> None:
        self.fleet()
        capacity = self.dir / "capacity"
        capacity.mkdir()
        fd = os.open(capacity / "admission.lock", os.O_RDONLY | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            status, result = self.ci_step("fits")
            self.assertEqual((status, result["fits"], result["unknown"]), (0, [], True))
        finally:
            os.close(fd)
        self.assertEqual(self.fits(), ["light", "isolated"])

    def test_capacity_refuses_while_a_fleet_build_holds_the_host(self) -> None:
        fleet = self.fleet()
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import fcntl,os,time\n"
                                   f"fd=os.open({os.fspath(fleet / 'host.lock')!r},os.O_RDWR)\n"
                                   "fcntl.flock(fd,fcntl.LOCK_EX)\nprint('held',flush=True)\ntime.sleep(30)\n"],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            result = self.job("cli-product-tests", "w0")
            self.assertEqual(result.returncode, 1)
            self.assertIn("refused: capacity: a fleet build holds the host lock", result.stdout)
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()

    @unittest.skipUnless(os.path.exists(hook.LSOF), "needs lsof")
    def test_capacity_preempts_a_yielding_fleet_build(self) -> None:
        """A fleet build that wrote <host lock>.yield (cmuxterm-hq's catch-up fill) gives way: the job
        writes .preempted, SIGTERMs it and is admitted once the lock is free. Like the fill, it passes the
        lock fd to a build in a new session, which lsof lists too."""
        fleet = self.fleet()
        lock = fleet / "host.lock"
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import fcntl,os,signal,subprocess,sys,time\n"
                                   f"fd=os.open({os.fspath(lock)!r},os.O_RDWR)\n"
                                   "fcntl.flock(fd,fcntl.LOCK_EX)\n"
                                   "build=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)'],"
                                   "pass_fds=(fd,),start_new_session=True,stdout=subprocess.DEVNULL)\n"
                                   "def stop(*a):\n os.killpg(build.pid,signal.SIGTERM); build.wait(); sys.exit(0)\n"
                                   "signal.signal(signal.SIGTERM, stop)\n"
                                   "print('held',flush=True)\ntime.sleep(300)\n"],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            (fleet / "host.lock.yield").write_text(f"{holder.pid}\n")
            result = self.job("cli-product-tests", "w0")
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn(f"preempted the yielding fleet build (pid {holder.pid})", result.stdout)
            self.assertEqual(holder.wait(timeout=10), 0)
            note = json.loads((fleet / "host.lock.preempted").read_text())
            self.assertEqual((note["by"], note["pid"]), ("cli-product-tests", holder.pid))
        finally:
            self.finish("w0")
            with contextlib.suppress(OSError):
                holder.kill()
            holder.wait()
            holder.stdout.close()

    @unittest.skipUnless(os.path.exists(hook.LSOF), "needs lsof")
    def test_capacity_never_preempts_a_fill_waiting_behind_another_build(self) -> None:
        """The fill marks itself yielding while it waits; if another fleet build holds the lock, the job is
        refused at once and the waiting fill is left alone."""
        fleet = self.fleet()
        lock = fleet / "host.lock"
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import fcntl,os,time\n"
                                   f"fd=os.open({os.fspath(lock)!r},os.O_RDWR)\n"
                                   "fcntl.flock(fd,fcntl.LOCK_EX)\nprint('held',flush=True)\ntime.sleep(300)\n"],
                                  stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), "held")  # before the waiter, which must not win
        waiter = subprocess.Popen([sys.executable, "-c",
                                   "import fcntl,os,time\n"
                                   f"fd=os.open({os.fspath(lock)!r},os.O_RDWR)\n"
                                   "print('open',flush=True)\nfcntl.flock(fd,fcntl.LOCK_EX)\ntime.sleep(300)\n"],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(waiter.stdout.readline().strip(), "open")
            (fleet / "host.lock.yield").write_text(f"{waiter.pid}\n")
            result = self.job("cli-product-tests", "w0")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("refused: capacity: a fleet build holds the host lock", result.stdout)
            self.assertIsNone(waiter.poll(), f"the waiting fill was not signalled ({waiter.returncode})")
            self.assertFalse((fleet / "host.lock.preempted").exists())
        finally:
            for proc in (holder, waiter):
                proc.kill()
                proc.wait()
                proc.stdout.close()

    def test_capacity_never_signals_a_marker_pid_without_the_lock(self) -> None:
        """A stale or planted .yield naming a process that does not have the lock open changes nothing."""
        fleet = self.fleet()
        lock = fleet / "host.lock"
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import fcntl,os,time\n"
                                   f"fd=os.open({os.fspath(lock)!r},os.O_RDWR)\n"
                                   "fcntl.flock(fd,fcntl.LOCK_EX)\nprint('held',flush=True)\ntime.sleep(30)\n"],
                                  stdout=subprocess.PIPE, text=True)
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            (fleet / "host.lock.yield").write_text(f"{bystander.pid}\n")
            result = self.job("cli-product-tests", "w0")
            self.assertEqual(result.returncode, 1)
            self.assertIn("refused: capacity: a fleet build holds the host lock", result.stdout)
            self.assertIsNone(bystander.poll(), "the bystander was not signalled")
            self.assertFalse((fleet / "host.lock.preempted").exists())
        finally:
            for proc in (holder, bystander):
                proc.kill()
                proc.wait()
            holder.stdout.close()

    def test_capacity_fleet_drain_refuses_without_entering_admission_wait(self) -> None:
        fleet = self.fleet()
        markers = Path(str(fleet / "host.lock") + ".waiters")
        markers.mkdir()
        drain = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            # A reserved worker waits for Runner.Worker to exit. Waiting inside its
            # job-started child would prevent that exit and deadlock both schedulers.
            for job in ("swift-package-tests", "macos-compile-admission"):
                with self.subTest(job=job):
                    (markers / f"{drain.pid}-refresh-lend_drain-test").touch()
                    result = self.job(job, "draining", 4, None, "--gui-wait", "2")
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("a fleet build is waiting for the host", result.stdout)
                    self.assertNotIn("waiting (", result.stdout)
                    self.assertFalse(list((self.dir / "capacity").glob("admit.want-*")))
                    self.assertTrue(self.lock_free())
        finally:
            drain.kill()
            drain.wait()

    @unittest.skipUnless(os.path.exists(hook.LSOF), "needs lsof")
    def test_capacity_stops_admitting_while_a_fleet_build_waits(self) -> None:
        fleet = self.fleet()
        try:
            self.assertEqual(self.job("swift-package-tests", "l0", 8).returncode, 0)
            waiter = subprocess.Popen([sys.executable, "-c",
                                       "import fcntl,os\n"
                                       f"fd=os.open({os.fspath(fleet / 'host.lock')!r},os.O_RDWR)\n"
                                       "print('waiting',flush=True)\nfcntl.flock(fd,fcntl.LOCK_EX)\n"],
                                      stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(waiter.stdout.readline().strip(), "waiting")
                time.sleep(0.5)
                result = self.job("swift-package-tests", "l1", 8)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("refused: a fleet build is waiting for the host", result.stdout)
                self.finish("l0")
                self.assertEqual(waiter.wait(timeout=10), 0, "the build worker gets the host once our job ends")
            finally:
                waiter.kill()
                waiter.wait()
                waiter.stdout.close()
        finally:
            self.finish("l0")

    def test_capacity_a_releasing_holder_is_not_a_waiter(self) -> None:
        self.fleet()
        state = self.dir / "state"
        state.mkdir()
        (state / "host-lock-holder-gone.pid.releasing").write_text("4242\n")
        (state / "host-lock-holder-live.pid").write_text("4343\n")
        self.assertEqual(hook.holder_pids(state), {os.getpid(), 4242, 4343})
        try:  # job-completed on one runner while another admits: never "a fleet build is waiting"
            self.assertEqual(self.job("swift-package-tests", "a0", 8).returncode, 0)
            for n in range(3):
                self.finish("a0")
                self.assertEqual(self.job("swift-package-tests", "a0", 8).returncode, 0)
                result = self.job("swift-package-tests", f"b{n}", 8)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.finish(f"b{n}")
        finally:
            self.finish("a0")
        self.assertTrue(self.lock_free())
        self.assertEqual(sorted(p.name for p in state.glob("*.releasing")), ["host-lock-holder-gone.pid.releasing"])

    def test_capacity_toolchain_gate_requires_gh(self) -> None:
        self.fleet()
        self.node(gh=False)
        result = self.eligible_start()
        self.assertEqual(result.returncode, 1)
        self.assertIn("gh is not on the job PATH", result.stdout)
        self.assertTrue(self.lock_free())

    def test_no_fleet_lock_file_admits(self) -> None:
        result = self.started()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("no fleet host lock", result.stdout)

    # ------------------------------------------------------------ eligibility gate

    COMMIT = "3809eed51fdd9f1464452f2cd37dbf3148a831fd"
    TOOLCHAIN = {"rustcVersion": "rustc 1.98.1 (48a229cea 2026-09-01)", "cargoVersion": "cargo 1.98.1 (797e8a9bc 2026-08-05)",
                 "zigVersion": "0.16.0", "xcodeVersion": "26.6", "xcodeBuild": "17F113", "macosSdkVersion": "26.5"}

    def node(self, state: str = "eligible", routing: bool = True, receipt_sha: str = "sha256:aa",
             enrolled_sha: str = "sha256:aa", tools: dict | None = None, generation: bool = True,
             valid: bool = True, role: str = "cmux_macos_native_build", receipt_toolchain: dict | None = None,
             hang: str | None = None, default: str = "1.98.1-aarch64-apple-darwin",
             installed: tuple = ("1.88.0-aarch64-apple-darwin", "1.98.1-aarch64-apple-darwin"),
             python: str = "3.13", gh: bool = True, sdk_path: str | None = None,
             swiftc: str | None = None, python_runs: bool = True, unfound: tuple = (),
             slow: str | None = None) -> None:
        """A fake Glaeda node under HOME (self.dir): enrollment, class receipt, staged CLI, toolchain. xcrun answers
        --show-sdk-path with sdk_path and `swiftc --version` with swiftc, and fails both when they are None."""
        config = self.dir / ".config/glaeda/cmux-fleet"
        (config / "class-acceptance").mkdir(parents=True, exist_ok=True)
        (config / "acceptance").mkdir(exist_ok=True)
        (config / "enrollment.json").write_text(json.dumps({"state": state, "classAcceptanceSha256": enrolled_sha}))
        (config / "acceptance/cmux_macos_native_build.json").write_text("{}")
        (config / "class-acceptance/m4pro-48.json").write_text(json.dumps(
            {"receiptSha256": receipt_sha, "glaedaCandidate": {"commit": self.COMMIT},
             "toolchain": self.TOOLCHAIN if receipt_toolchain is None else receipt_toolchain}))
        gen = self.dir / "Projects/glaeda-generations" / self.COMMIT[:12] / "scripts"
        if generation:
            gen.mkdir(parents=True, exist_ok=True)
            status = {"schema": "glaeda-cmux-fleet-node-status/v1", "state": state, "routingCandidateEligible": routing,
                      "roles": [{"role": role, "eligible": routing,
                                 "reason": "ok" if routing else "acceptance_missing_or_rejected"}]}
            # slow: "validate" or "status" sleeps in that probe, as a Python start on a mini at load 275 does
            (gen / "cmux_fleet.py").write_text(
                "import json, time\n"
                f"def validate_class_acceptance(doc):\n    if {slow == 'validate'!r}:\n        time.sleep(30)\n"
                f"    if not {valid!r}:\n        raise ValueError('digest')\n"
                "    return doc\n"
                f"if __name__ == '__main__':\n    if {slow == 'status'!r}:\n        time.sleep(30)\n"
                f"    print(json.dumps({status!r}))\n")
        cargo = self.dir / "jobpath"
        cargo.mkdir(parents=True, exist_ok=True)
        have = {**self.TOOLCHAIN, **(tools or {})}
        (self.dir / "rustup-default").write_text(default)
        (self.dir / "rustup-installed").write_text("\n".join(installed))
        versions = {"1.98.1-aarch64-apple-darwin": self.TOOLCHAIN["rustcVersion"],
                    "1.88.0-aarch64-apple-darwin": "rustc 1.88.0 (6b00bc388 2025-06-23)"}
        (self.dir / "rustc-by-toolchain.json").write_text(json.dumps(versions))
        state = os.fspath(self.dir)
        make_executable(cargo / "rustup", f"""#!{sys.executable}
import json, sys
state = {state!r}
default = open(state + "/rustup-default").read().strip()
installed = open(state + "/rustup-installed").read().split()
args = sys.argv[1:]
if args[:2] == ["toolchain", "list"]:
    for name in installed:
        print(name + (" (active, default)" if name == default else ""))
elif args == ["default"]:
    print(default + " (default)")
elif args == ["show"]:
    print("Default host: aarch64-apple-darwin")
elif args[:1] == ["default"] and args[1] in installed:
    open(state + "/rustup-default", "w").write(args[1])
    print("info: default toolchain set to " + args[1])
elif args[:1] == ["run"]:
    print(json.load(open(state + "/rustc-by-toolchain.json")).get(args[1] + "-aarch64-apple-darwin", ""))
else:
    sys.exit(1)
""")
        rustc = have["rustcVersion"] if (tools or {}).get("rustcVersion") else None
        make_executable(cargo / "rustc", f"#!{sys.executable}\nimport json\n"
                        + (f"print({rustc!r})\n" if rustc else
                           f"import os\nprint(json.load(open({state!r} + '/rustc-by-toolchain.json'))"
                           f"[os.environ.get('RUSTUP_TOOLCHAIN') or open({state!r} + '/rustup-default').read().strip()])\n"))
        make_executable(cargo / "python3", f"#!/bin/sh\necho {python}\n" if python_runs else
                        "#!/bin/sh\necho \"xcode-select: Failed to locate 'python3', requesting installation of "
                        "command line developer tools.\" >&2\nexit 72\n")
        if gh:
            make_executable(cargo / "gh", "#!/bin/sh\necho 'gh version 2.101.0 (2026-09-15)'\n")
        else:  # shadows a gh the host has on /usr/bin (GitHub's hosted images do)
            make_executable(cargo / "gh", "#!/bin/sh\nexit 127\n")
        outputs = {"cargo": have["cargoVersion"], "zig": have["zigVersion"],
                   "xcrun": have["macosSdkVersion"],
                   "xcodebuild": f"Xcode {have['xcodeVersion']}\nBuild version {have['xcodeBuild']}"}
        for name, text in outputs.items():
            if text is None:
                (cargo / name).unlink(missing_ok=True)
                continue
            body = "sleep 60\n" if name == hang else f"cat <<'OUT'\n{text}\nOUT\n"
            if name == "xcrun" and name != hang:
                found = f"echo {shlex.quote(sdk_path)}" if sdk_path is not None else "exit 1"
                swift = f"echo {shlex.quote(swiftc)}" if swiftc is not None else "exit 1"
                missing = "".join(f"--find\\ {tool}) echo 'xcrun: error: unable to find utility \"{tool}\"' >&2; "
                                  "exit 72 ;;\n" for tool in unfound)
                body = (f'case "$*" in\n{missing}--find\\ *) echo /usr/bin/found ;;\n*--show-sdk-path*) {found} ;;\n'
                        f"*swiftc*) {swift} ;;\n*) cat <<'OUT'\n{text}\nOUT\n;;\nesac\n")
            make_executable(cargo / name, f"#!/bin/sh\n{body}")

    def eligible_start(self) -> subprocess.CompletedProcess:
        # The fake toolchain is on the job's PATH, which is the runner's PATH the hook inherits.
        return self.started("--require-eligible", "--fleet-class", "m4pro-48", "--toolchain-xcode",
                            "/Applications/Xcode_26.6.app", env={"PATH": f"{self.dir / 'jobpath'}:/usr/bin:/bin"})

    def done(self) -> None:
        self.run_hook("job-completed", None, None, "--no-disk", "--state-dir", os.fspath(self.dir / "state"))

    def test_the_receipts_source_node_is_admitted_without_adopting_it(self) -> None:
        self.fleet()
        self.node()
        config = self.dir / ".config/glaeda/cmux-fleet"
        receipt = json.loads((config / "class-acceptance/m4pro-48.json").read_text())
        receipt.update(fleetClass="m4pro-48", acceptingNodeId="cmux-mac-001", acceptingEnrollmentGeneration=2, glaedaGeneration="sha256:g",
                       toolchainGeneration="sha256:t", cmuxToolchainIdentity="sha256:i", cmuxSemanticResultSha256="sha256:s")
        (config / "class-acceptance/m4pro-48.json").write_text(json.dumps(receipt))
        enrollment = {"state": "eligible", "nodeId": "cmux-mac-001", "enrollmentGeneration": 2,
                      "glaedaGeneration": "sha256:g", "supportedToolchainGenerations": ["sha256:t"]}
        acceptance = {"nodeId": "cmux-mac-001", "enrollmentGeneration": 2, "toolchainGeneration": "sha256:t",
                      "cmuxToolchainIdentity": "sha256:i", "cmuxSemanticResultSha256": "sha256:s"}
        cases = {"source": ({}, {}, 0), "other-node": ({"nodeId": "cmux-mac-002"}, {}, 1),
                 "stale-generation": ({"enrollmentGeneration": 1}, {}, 1),
                 "other-toolchain": ({"supportedToolchainGenerations": ["sha256:x"]}, {}, 1),
                 "other-result": ({}, {"cmuxSemanticResultSha256": "sha256:z"}, 1),
                 "references-another-receipt": ({"classAcceptanceSha256": "sha256:bb"}, {}, 1),
                 "generations-not-a-list": ({"supportedToolchainGenerations": "sha256:t"}, {}, 1)}
        for name, (enroll_over, accept_over, code) in cases.items():
            with self.subTest(name):
                (config / "enrollment.json").write_text(json.dumps({**enrollment, **enroll_over}))
                (config / "acceptance/cmux_macos_native_build.json").write_text(json.dumps({**acceptance, **accept_over}))
                result = self.eligible_start()
                self.done()
                self.assertEqual(result.returncode, code, result.stdout)
                if code:
                    self.assertIn("does not reference the m4pro-48 class receipt", result.stdout)
        (config / "enrollment.json").write_text(json.dumps(enrollment))
        (config / "acceptance/cmux_macos_native_build.json").write_text(json.dumps(acceptance))
        (config / "class-acceptance/m4pro-48.json").write_text(json.dumps({**receipt, "fleetClass": "m4-16"}))
        self.assertEqual(self.eligible_start().returncode, 1, "a receipt for another class never admits")
        self.done()

    def test_eligible_node_on_its_toolchain_is_admitted(self) -> None:
        self.fleet()
        self.node()
        result = self.eligible_start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.done()

    def test_eligible_node_is_admitted_to_capacity(self) -> None:
        # every fleet member passes --require-eligible and --capacity-units together; the capacity wait's marker
        # once shadowed the eligibility want and crashed toolchain_refusal after admission
        self.fleet()
        self.node()
        result = self.started("--require-eligible", "--fleet-class", "m4pro-48", "--toolchain-xcode",
                              "/Applications/Xcode_26.6.app", "--capacity-units", "4", "--capacity-dir",
                              os.fspath(self.dir / "capacity"),
                              env={"PATH": f"{self.dir / 'jobpath'}:/usr/bin:/bin", "GITHUB_JOB": "swift-package-tests",
                                   "RUNNER_NAME": "e0"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("admitted:", result.stdout)
        self.run_hook("job-completed", None, None, "--no-disk", "--state-dir", os.fspath(self.dir / "state"),
                      env={"RUNNER_NAME": "e0"})

    def test_ineligible_nodes_refuse(self) -> None:
        self.fleet()
        cases = {
            "enrolling": (dict(state="enrolling", routing=False), "state enrolling"),
            "routing-false": (dict(routing=False), "acceptance_missing_or_rejected"),
            "other-receipt": (dict(enrolled_sha="sha256:bb"), "does not reference the m4pro-48 class receipt"),
            "rustc-drift": (dict(tools={"rustcVersion": "rustc 1.99.0"}), "rustcVersion 'rustc 1.99.0'"),
            "xcode-drift": (dict(tools={"xcodeBuild": "17F999"}), "xcodeBuild '17F999' != '17F113'"),
            "zig-missing": (dict(tools={"zigVersion": None}), "zigVersion '' != '0.16.0'"),
            "no-generation": (dict(generation=False), "no node status from generation 3809eed51fdd"),
            "receipt-invalid": (dict(valid=False), "class receipt does not validate"),
            "wrong-role": (dict(role="some_other_role"), "some_other_role: ok"),
            "receipt-without-toolchain": (dict(receipt_toolchain={}), "records no rustcVersion"),
            "receipt-unknown-zig": (dict(receipt_toolchain={**self.TOOLCHAIN, "zigVersion": "unknown"}),
                                    "records no zigVersion"),
        }
        for name, (kwargs, text) in cases.items():
            with self.subTest(name):
                import shutil as _sh
                _sh.rmtree(self.dir / "Projects", ignore_errors=True)
                self.node(**kwargs)
                result = self.eligible_start()
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("refused: node not eligible", result.stdout)
                self.assertIn(text, result.stdout)
                self.assertTrue(self.lock_free(), "a refused job never takes the host lock")

    def test_drifted_rustup_default_is_realigned_under_the_lock(self) -> None:
        self.fleet()
        self.node(default="1.88.0-aarch64-apple-darwin")  # another fleet job flipped the global default
        result = self.eligible_start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("set the rustup default to 1.98.1-aarch64-apple-darwin (was 1.88.0-aarch64-apple-darwin)",
                      result.stdout)
        self.assertEqual((self.dir / "rustup-default").read_text(), "1.98.1-aarch64-apple-darwin")
        self.assertFalse(self.lock_free())
        self.done()

    def test_check_is_read_only_and_predicts_the_alignment(self) -> None:
        self.node(default="1.88.0-aarch64-apple-darwin")
        push = event(self.dir, "push", {"repository": CMUX})
        result = self.run_hook("check", "push", push, "--fleet-class", "m4pro-48", "--toolchain-xcode",
                               "/Applications/Xcode_26.6.app", env={"PATH": f"{self.dir / 'jobpath'}:/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("eligible (would set the rustup default to 1.98.1-aarch64-apple-darwin)", result.stdout)
        self.assertEqual((self.dir / "rustup-default").read_text(), "1.88.0-aarch64-apple-darwin")
        self.assertFalse((self.dir / "state").exists())

    def test_old_python_on_the_job_path_refuses(self) -> None:
        self.fleet()
        self.node(python="3.9")
        result = self.eligible_start()
        self.assertEqual(result.returncode, 1)
        self.assertIn("python3 on the job PATH is 3.9", result.stdout)
        self.assertTrue(self.lock_free())

    def test_matching_stable_default_is_left_alone(self) -> None:
        self.fleet()
        self.node(default="stable-aarch64-apple-darwin", installed=("stable-aarch64-apple-darwin",))
        json_path = self.dir / "rustc-by-toolchain.json"
        versions = json.loads(json_path.read_text())
        versions["stable-aarch64-apple-darwin"] = self.TOOLCHAIN["rustcVersion"]
        json_path.write_text(json.dumps(versions))
        result = self.eligible_start()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("set the rustup default", result.stdout)
        self.assertEqual((self.dir / "rustup-default").read_text(), "stable-aarch64-apple-darwin")
        self.done()

    def test_toolchain_refusal_after_the_lock_releases_it(self) -> None:
        self.fleet()
        self.node(default="1.88.0-aarch64-apple-darwin", installed=("1.88.0-aarch64-apple-darwin",))
        result = self.eligible_start()
        self.assertEqual(result.returncode, 1)
        self.assertIn("rust toolchain 1.98.1-aarch64-apple-darwin is not installed", result.stdout)
        self.assertTrue(self.lock_free())
        self.assertEqual((self.dir / "rustup-default").read_text(), "1.88.0-aarch64-apple-darwin")

    def test_diff_sidecar_pin_is_checked(self) -> None:
        self.fleet()
        receipt = {**self.TOOLCHAIN, "diffRustToolchain": "1.88.0",
                   "diffRustcVersion": "rustc 1.88.0 (6b00bc388 2025-06-23)"}
        self.node(receipt_toolchain=receipt)
        self.assertEqual(self.eligible_start().returncode, 0)
        self.done()
        self.node(receipt_toolchain={**receipt, "diffRustcVersion": "rustc 1.88.1 (x)"})
        result = self.eligible_start()
        self.assertEqual(result.returncode, 1)
        self.assertIn("diffRustcVersion", result.stdout)
        self.assertTrue(self.lock_free())

    def test_a_timed_out_eligibility_probe_is_named_as_a_timeout_not_an_invalid_receipt(self) -> None:
        """On a mini too loaded to start Python within ELIGIBILITY_BUDGET_S the validator or node status gives no
        answer. node_answer flags it, so the health agent raises no node_ineligible; the refusal names the timeout."""
        import shutil as _sh
        for slow, text in (("validate", "m4pro-48 class receipt validator timed out after 1 s"),
                           ("status", "node status from generation 3809eed51fdd timed out after 1 s")):
            with self.subTest(slow):
                _sh.rmtree(self.dir / "Projects", ignore_errors=True)
                self.node(slow=slow)
                with mock.patch.dict(os.environ, {"HOME": os.fspath(self.dir), "XDG_CONFIG_HOME": ""}), \
                        mock.patch.object(hook, "ELIGIBILITY_BUDGET_S", 1.0):
                    start = time.monotonic()
                    why, want, timed_out = hook.node_answer("m4pro-48")
                    self.assertLess(time.monotonic() - start, 10)
                    self.assertEqual(hook.node_refusal("m4pro-48"), (why, want))
                self.assertTrue(timed_out)
                self.assertIn(text, why)
                self.assertNotIn("does not validate", why)
                self.assertEqual(want, {})
        for kwargs, timed_out in ((dict(valid=False), False), ({}, False)):
            with self.subTest(**kwargs):
                _sh.rmtree(self.dir / "Projects", ignore_errors=True)
                self.node(**kwargs)
                with mock.patch.dict(os.environ, {"HOME": os.fspath(self.dir), "XDG_CONFIG_HOME": ""}):
                    why, _want, got = hook.node_answer("m4pro-48")
                self.assertEqual(got, timed_out, why)
                self.assertEqual(why is None, not kwargs, why)

    def test_hung_tool_refuses_within_the_budget(self) -> None:
        self.fleet()
        self.node(hang="xcodebuild")
        with mock.patch.dict(os.environ, {}):
            start = time.monotonic()
            result = self.started("--require-eligible", "--fleet-class", "m4pro-48",
                                  env={"PATH": f"{self.dir / 'jobpath'}:/usr/bin:/bin"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("xcodeVersion '' != '26.6'", result.stdout)
        self.assertLess(time.monotonic() - start, 30)

    def test_no_enrollment_refuses(self) -> None:
        self.fleet()
        result = self.eligible_start()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no Glaeda enrollment", result.stdout)

    # ------------------------------------------------------------ the pool Xcode's toolchain (cmux21, 2026-10-21)

    def xcode_start(self, **env: str) -> subprocess.CompletedProcess:
        xcode = fake_xcode(self.dir)
        return self.started("--require-eligible", "--fleet-class", "m4pro-48", "--toolchain-xcode", os.fspath(xcode),
                            env={"PATH": f"{self.dir / 'jobpath'}:/usr/bin:/bin",
                                 "GLAEDA_RUNNER_XCODE_PROBE_TTL_S": "0", **env})

    def test_a_host_whose_xcrun_resolves_the_command_line_tools_refuses(self) -> None:
        """cmux21 (macOS 27): Command Line Tools 27 made xcrun resolve their MacOSX27 SDK and Swift 6.4 even with
        DEVELOPER_DIR on Xcode_26.6, so cmux-next's fast tier failed "this SDK is not supported by the compiler"
        (jobs 119519488456, 119553662726). job-started refuses such a host before any step runs."""
        self.fleet()
        xcode = fake_xcode(self.dir)
        good = os.fspath(xcode / "Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk")
        self.node(sdk_path=good, swiftc=SWIFT_633)
        github_env = self.dir / "github-env"
        result = self.xcode_start(GITHUB_ENV=os.fspath(github_env))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"DEVELOPER_DIR={xcode / 'Contents/Developer'}\n", github_env.read_text(),
                      "every later step of the job runs on the pool Xcode")
        self.done()
        clt = fake_clt(self.dir)
        self.node(sdk_path=os.fspath(clt), swiftc=SWIFT_64)
        result = self.xcode_start()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(f"refused: toolchain: xcrun resolves {clt} outside Xcode_26.6", result.stdout)
        self.assertIn("Swift 6.4", result.stdout)
        self.assertTrue(self.lock_free(), "a refused job never keeps the host lock")

    def test_a_python3_or_a_cmux_tool_that_xcrun_cannot_run_refuses(self) -> None:
        """cmux21, after the Command Line Tools were moved aside: under a stale SDKROOT the /usr/bin/python3 shim
        failed ("xcode-select: Failed to locate 'python3'", exit 72), and app-compile, fast-tier and web-bundles
        failed as infra. job-started refuses when python3 does not run or xcrun cannot find a tool cmux uses."""
        self.fleet()
        xcode = fake_xcode(self.dir)
        good = os.fspath(xcode / "Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk")
        self.node(sdk_path=good, swiftc=SWIFT_633, unfound=("git", "make"))
        result = self.xcode_start()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("refused: toolchain: xcrun --find git fails (exit 72: xcrun: error: unable to find utility",
                      result.stdout)
        self.assertIn("xcrun --find make fails", result.stdout)
        self.assertTrue(self.lock_free())
        self.node(sdk_path=good, swiftc=SWIFT_633, python_runs=False)
        result = self.started("--require-eligible", "--fleet-class", "m4pro-48", "--toolchain-xcode",
                              os.fspath(xcode), env={"PATH": f"{self.dir / 'jobpath'}:/usr/bin:/bin",
                                                     "GLAEDA_RUNNER_XCODE_PROBE_TTL_S": "0"})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("refused: node not eligible (python3 on the job PATH is missing", result.stdout,
                      "the job PATH's python3 is checked first, as before")

    def test_a_toolchain_probe_that_answers_nothing_does_not_refuse(self) -> None:
        # a loaded mini's xcrun that times out is no verdict: the exact-string drift check still runs
        self.fleet()
        self.node()  # xcrun fails --show-sdk-path and swiftc
        result = self.xcode_start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.done()

    def test_a_listener_env_that_carries_a_foreign_sdkroot_refuses(self) -> None:
        """The runner gate ran under the /usr/bin/python3 xcrun shim, which exported SDKROOT, and passed it to
        Runner.Listener, so every job inherited the Command Line Tools SDK (and, once that was moved aside, the
        job-started hook itself failed to start: job 119553581129)."""
        self.fleet()
        xcode = fake_xcode(self.dir)
        self.node(sdk_path=os.fspath(xcode / "Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"),
                  swiftc=SWIFT_633)
        result = self.xcode_start(SDKROOT="/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("refused: toolchain: the runner's environment sets "
                      "SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk", result.stdout)
        inside = os.fspath(xcode / "Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk")
        result = self.xcode_start(SDKROOT=inside)
        self.assertEqual(result.returncode, 0, "an SDKROOT inside the pool Xcode is the pool's SDK\n" + result.stdout)
        self.done()

    def test_decide_is_pure_table(self) -> None:
        for name, (event_name, payload, admitted) in SAMPLE_EVENTS.items():
            with self.subTest(name):
                ok, _ = hook.decide(event_name, payload, (payload.get("repository") or {}).get("full_name"),
                                    ["manaflow-ai/cmux"], None)
                self.assertEqual(ok, admitted)


SWIFT_633 = ("swift-driver version: 1.127.14.1 Apple Swift version 6.3.3 (swiftlang-6.3.3.1.2 clang-1700.6.3.2)\n"
             "Target: arm64-apple-macosx27.0")
SWIFT_64 = ("swift-driver version: 1.140.2 Apple Swift version 6.4 (swiftlang-6.4.0.10.4 clang-1800.2.6.1)\n"
            "Target: arm64-apple-macosx27.0")


def fake_xcode(base: Path, swift: str = SWIFT_633, sdk: str = "26.5") -> Path:
    """BASE/Xcode_26.6.app with a MacOSX SDK (SDKSettings.json Version SDK) and the default toolchain's swiftc."""
    app = base / "Xcode_26.6.app"
    developer = app / "Contents/Developer"
    sdks = developer / "Platforms/MacOSX.platform/Developer/SDKs"
    (sdks / f"MacOSX{sdk}.sdk").mkdir(parents=True, exist_ok=True)
    (sdks / f"MacOSX{sdk}.sdk/SDKSettings.json").write_text(json.dumps({"Version": sdk}))
    if not (sdks / "MacOSX.sdk").exists():
        (sdks / "MacOSX.sdk").symlink_to(f"MacOSX{sdk}.sdk")
    swiftc = developer / "Toolchains/XcodeDefault.xctoolchain/usr/bin/swiftc"
    swiftc.parent.mkdir(parents=True, exist_ok=True)
    make_executable(swiftc, f"#!/bin/sh\ncat <<'OUT'\n{swift}\nOUT\n")
    return app


def fake_clt(base: Path, sdk: str = "27.0") -> Path:
    """A Command Line Tools MacOSX SDK under BASE (SDKSettings.json Version SDK)."""
    path = base / "CommandLineTools/SDKs/MacOSX.sdk"
    path.mkdir(parents=True, exist_ok=True)
    (path / "SDKSettings.json").write_text(json.dumps({"Version": sdk}))
    return path


class MirrorBorrowTest(unittest.TestCase):
    """A cmux runner checkout borrows the mini's git mirror at job-completed and drops its local copies; at
    job-started a borrower whose mirror is gone or was rebuilt loses its .git, so checkout clones it whole."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        self.env = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.dir), "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@example.com"}
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        src = self.dir / "src"
        src.mkdir()
        self.git(src, "init", "-q", "-b", "main")
        for n in range(3):
            (src / f"f{n}.bin").write_bytes(os.urandom(200_000))
            self.git(src, "add", "-A")
            self.git(src, "commit", "-qm", f"c{n}")
        self.mirror = self.dir / "fleet" / "git" / "cmux.git"
        self.mirror.parent.mkdir(parents=True)
        self.git(self.dir, "clone", "-q", "--bare", "--no-local", os.fspath(src), os.fspath(self.mirror))
        (self.mirror / "glaeda-verified").write_text("now\n")
        self.ws = self.dir / "actions-runner-glaeda-2" / "_work" / "cmux" / "cmux"
        self.ws.parent.mkdir(parents=True)
        self.git(self.dir, "clone", "-q", "--no-local", os.fspath(src), os.fspath(self.ws))
        self.git(self.ws, "remote", "set-url", "origin", "https://github.com/manaflow-ai/cmux")
        self.git(self.ws, "repack", "-a", "-d", "-q")
        (self.ws / "local.txt").write_text("only here\n")
        self.git(self.ws, "add", "-A")
        self.git(self.ws, "commit", "-qm", "pull request commit")  # an object the mirror lacks

    def tearDown(self) -> None:
        self.patch.stop()
        self.tmp.cleanup()

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", os.fspath(cwd), *args], env=self.env, capture_output=True, text=True,
                              check=True).stdout.strip()

    def borrow(self) -> str:
        with mock.patch.object(hook, "BORROW_REPACK_MIN", 0):
            return hook.borrow_mirror(os.fspath(self.ws), self.mirror)

    def test_a_checkout_borrows_the_mirror_and_keeps_only_what_the_mirror_lacks(self) -> None:
        before = hook.pack_bytes(self.ws / ".git")
        note = self.borrow()
        self.assertIn("borrows", note)
        self.assertIn("repacked against the mirror", note)
        self.assertEqual(hook.alternates_of(self.ws / ".git"), [os.fspath(self.mirror / "objects")])
        self.assertLess(hook.pack_bytes(self.ws / ".git"), before / 10)
        self.git(self.ws, "fsck", "--no-dangling")  # every object still reads, through the mirror
        self.assertEqual(self.git(self.ws, "show", "HEAD:local.txt"), "only here")
        self.assertEqual(self.git(self.ws, "status", "--porcelain"), "")
        self.assertEqual(hook.guard_borrowed_checkout(os.fspath(self.ws), self.mirror), "")
        self.assertTrue((self.ws / ".git").is_dir())
        self.assertNotIn("borrows", self.borrow())  # idempotent: the second pass only repacks what is new

    def test_a_borrower_whose_mirror_was_rebuilt_or_removed_loses_its_git(self) -> None:
        self.borrow()
        rebuilt = self.mirror.with_name("cmux.git.new")
        shutil.copytree(self.mirror, rebuilt)
        shutil.rmtree(self.mirror)
        rebuilt.rename(self.mirror)  # what a reseed does: a new directory at the same path
        self.assertIn("gone or was rebuilt", hook.guard_borrowed_checkout(os.fspath(self.ws), self.mirror))
        self.assertFalse((self.ws / ".git").exists())
        self.assertTrue((self.ws / "local.txt").exists())  # only .git goes; checkout clones into the workspace

    def test_a_missing_mirror_resets_the_borrower(self) -> None:
        self.borrow()
        shutil.rmtree(self.mirror)
        self.assertIn("gone or was rebuilt", hook.guard_borrowed_checkout(os.fspath(self.ws), self.mirror))
        self.assertFalse((self.ws / ".git").exists())

    def test_nothing_borrows_an_unproven_mirror_or_from_another_repository(self) -> None:
        (self.mirror / "glaeda-verified").unlink()
        self.assertEqual(self.borrow(), "")
        (self.mirror / "glaeda-verified").write_text("now\n")
        (self.mirror / "glaeda-repair-pending").write_text("x\n")
        self.assertEqual(self.borrow(), "")
        (self.mirror / "glaeda-repair-pending").unlink()
        self.git(self.ws, "remote", "set-url", "origin", "https://github.com/manaflow-ai/other")
        self.assertEqual(self.borrow(), "")
        self.assertEqual(hook.alternates_of(self.ws / ".git"), [])
        outside = self.dir / "elsewhere" / "cmux"
        shutil.copytree(self.ws, outside)
        self.assertEqual(hook.borrow_mirror(os.fspath(outside), self.mirror), "")
        self.assertEqual(hook.borrow_mirror(None, self.mirror), "")
        self.assertEqual(hook.guard_borrowed_checkout(os.fspath(self.ws), self.mirror), "")  # not a borrower

    def test_a_repack_past_its_time_is_killed_and_leaves_the_checkout_readable(self) -> None:
        real = subprocess.Popen

        class Slow:
            pid = 4242
            returncode = None

            def communicate(self, timeout=None):
                if timeout and timeout < 5:
                    raise subprocess.TimeoutExpired("git repack", timeout)
                return "", ""

        def popen(argv, *a, **kw):  # subprocess.run (the origin read) still runs for real
            return Slow() if "repack" in argv else real(argv, *a, **kw)
        killed = []
        with mock.patch.object(hook, "BORROW_REPACK_MIN", 0), mock.patch.object(hook.subprocess, "Popen", popen), \
                mock.patch.object(hook.os, "killpg", lambda pid, sig: killed.append(pid)):
            note = hook.borrow_mirror(os.fspath(self.ws), self.mirror, timeout=1)
        self.assertIn("repack timed out", note)
        self.assertEqual(killed, [4242])
        self.git(self.ws, "fsck", "--no-dangling")


class DiskPassCoalesceTest(unittest.TestCase):
    """job-completed's disk passes: one at a time per host, triggers coalesced into one follow-up, none started above
    glaeda-disk's target. On cmux14, 2889 of 2986 passes (2026-10-24..26) skipped as eviction_busy: every job started
    a pass while one (11-28 min) still held evict.lock."""

    def setUp(self) -> None:
        self.dir = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HOME": os.fspath(self.dir)})
        env.start()
        self.addCleanup(env.stop)
        self.state = hook.disk_state_dir()
        self.state.mkdir(parents=True)
        self.lock, self.pending = self.state / hook.DISK_PASS_LOCK, self.state / hook.DISK_PASS_PENDING
        self.log = hook.disk_run_log()
        self.runs = self.dir / "runs"
        self.overlap = self.dir / "overlap"

    def fake_disk(self, seconds: float = 0.0, triggers_on_first: int = 0, busy_first: bool = False,
                  trigger_every: bool = False) -> Path:
        """A glaeda-disk that records each run, notes any overlap with another, and on its first run can stand in
        for triggers arriving mid-pass (writes the pending trigger as a hook would) or skip as eviction_busy."""
        return make_executable(self.dir / "glaeda-disk", "import os, pathlib, time\n"
                               f"runs, overlap = pathlib.Path({os.fspath(self.runs)!r}), "
                               f"pathlib.Path({os.fspath(self.overlap)!r})\n"
                               f"running = pathlib.Path({os.fspath(self.dir / 'running')!r})\n"
                               "try:\n    os.close(os.open(running, os.O_CREAT | os.O_EXCL))\n"
                               "except FileExistsError:\n    overlap.touch()\n"
                               "n = len(runs.read_text().splitlines()) if runs.exists() else 0\n"
                               "with runs.open('a') as f:\n    f.write(f'{time.monotonic()}\\n')\n"
                               f"if n == 0:\n    for _ in range({triggers_on_first}):\n"
                               f"        pathlib.Path({os.fspath(self.pending)!r}).touch()\n"
                               f"if n == 0 and {busy_first!r}:\n"
                               "    print('another eviction running; skipped (pid 1)')\n"
                               f"if {trigger_every!r}:\n    pathlib.Path({os.fspath(self.pending)!r}).touch()\n"
                               f"time.sleep({seconds})\n"
                               "running.unlink()\n")

    def bound(self, disk: Path, timeout: float = 30, lifetime: float = 120, lock: Path | None = None) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-c", hook.DISK_BOUND, f"{timeout:g}", f"{lifetime:g}",
                                 os.fspath(lock or self.lock), os.fspath(self.pending), os.fspath(hook.evict_lock_path()),
                                 sys.executable, os.fspath(disk), "--pressure", "--apply", "--top", "0"],
                                env={**os.environ, "HOME": os.fspath(self.dir)})

    def waiting(self) -> bool:
        """A trigger no pass has served: pending, or claimed by a pass that did not run."""
        return self.pending.exists() or Path(f"{self.pending}.claimed").exists()

    def run_count(self) -> int:
        return len(self.runs.read_text().splitlines()) if self.runs.exists() else 0

    def logged(self) -> list[str]:
        return [json.loads(line).get("skipped") for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def row(self, targets: dict | None = None, mode: str = "pressure", skipped: str | None = None,
            age_s: float = 60, default: bool = True, floor: float = 30.0, config: str | None = None) -> str:
        at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - age_s))
        config = config or hook.disk_config_sha256(os.fspath(self.dir / "glaeda-disk"))
        return json.dumps({"at": at, "mode": mode, "targets": targets, "default_targets": default,
                           "config_sha256": config, "floor_gib": floor, "skipped": skipped}) + "\n"

    def targets(self) -> dict | None:
        return hook.disk_targets(config=hook.disk_config_sha256(os.fspath(self.dir / "glaeda-disk")))

    def write_target(self, gib: float) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.write_text(self.row({os.fspath(self.dir): max(1, int(gib * 1024**3))}))

    def test_the_bound_is_spared_as_glaedas_own(self) -> None:
        self.assertIn("glaeda-disk-bound", hook.DISK_BOUND.splitlines()[0])  # leftover_spared reads it

    def test_targets_are_glaeda_disks_newest_own_default_pressure_line(self) -> None:
        self.fake_disk()
        self.assertIsNone(self.targets())
        self.log.parent.mkdir(parents=True, exist_ok=True)
        gib, home = 1024**3, os.fspath(self.dir)
        self.log.write_text(self.row({home: 100 * gib}) + self.row({home: 170 * gib, "/tmp": 2 * gib})
                            + self.row({home: 90 * gib}, mode="idle")
                            + self.row({home: 50 * gib}, skipped=hook.HOOK_SKIP_ABOVE_TARGET)
                            + self.row({home: 50 * gib}, skipped=hook.HOOK_SKIP_COALESCED))
        self.assertEqual(self.targets(), {home: 170 * gib, "/tmp": 2 * gib})
        refused = {"stale": self.row({home: 100 * gib}, age_s=hook.DISK_TARGET_MAX_AGE_S + 60),
                   "custom --target": self.row({home: 100 * gib}, default=False),
                   "lower floor": self.row({home: 100 * gib}, floor=10.0),
                   "another glaeda-disk or policy": self.row({home: 100 * gib}, config="0" * 64),
                   "no targets": self.row(None),
                   "newest malformed": self.row({home: 100 * gib}) + "{not json\n",
                   "newest has no targets": self.row({home: 100 * gib}) + self.row({})}
        with mock.patch.object(hook, "hooks_floor_gib", return_value=30.0):
            for why, text in refused.items():
                with self.subTest(why):
                    self.log.write_text(text)
                    self.assertIsNone(self.targets(), "it must run the pass, never skip on this")
        self.log.write_text(self.row({home: 100 * gib}))
        policy = self.dir / ".config/glaeda/disk-policy.json"
        policy.parent.mkdir(parents=True)
        policy.write_text('{"target": "50%"}')
        self.assertIsNone(self.targets(), "a policy change since the line must run the pass")

    def test_any_filesystem_short_of_its_target_runs_the_pass(self) -> None:
        home = os.fspath(self.dir)
        self.assertEqual(hook.disk_short({home: 1}), "")
        self.assertIn(f"on {home}", hook.disk_short({home: 1, "/": 1, home + "/.": 10**18}) or "")
        self.assertIsNone(hook.disk_short({home + "/missing": 1}))

    def test_a_trigger_above_target_is_skipped_without_a_pass(self) -> None:
        disk = self.fake_disk()
        self.write_target(0.001)
        note = hook.disk_pressure_detached(os.fspath(disk))
        self.assertIn("every filesystem at or above its target", note)
        self.assertFalse(self.pending.exists())
        time.sleep(0.5)
        self.assertEqual(self.run_count(), 0)
        self.assertEqual(self.logged()[-1], hook.HOOK_SKIP_ABOVE_TARGET)

    def test_triggers_while_a_pass_runs_are_coalesced_not_dropped(self) -> None:
        disk = self.fake_disk()
        self.write_target(10**9)  # never reached
        with self.lock.open("a") as held:  # a running pass's bounding process
            fcntl.flock(held, fcntl.LOCK_EX)
            for _ in range(5):
                self.assertIn("coalesced", hook.disk_pressure_detached(os.fspath(disk)))
            self.assertTrue(self.pending.exists(), "the trigger must wait for the running pass")
            self.assertEqual(self.logged().count(hook.HOOK_SKIP_COALESCED), 5)
            self.assertEqual(self.run_count(), 0)
        # what the holder does once its pass ends: serve the pending trigger with one pass, not five
        self.assertEqual(self.bound(disk).wait(30), 0)
        self.assertEqual(self.run_count(), 1)
        self.assertFalse(self.waiting())

    def test_triggers_during_a_pass_get_exactly_one_follow_up(self) -> None:
        disk = self.fake_disk(triggers_on_first=3)
        self.pending.touch()
        self.assertEqual(self.bound(disk).wait(30), 0)
        self.assertEqual(self.run_count(), 2)
        self.assertFalse(self.waiting())

    def test_a_second_bound_never_overlaps_the_first(self) -> None:
        disk = self.fake_disk(seconds=4, triggers_on_first=1)
        self.pending.touch()
        first = self.bound(disk)
        self.assertTrue(wait_for_path(self.runs))
        self.pending.touch()
        start = time.monotonic()
        self.assertEqual(self.bound(disk).wait(30), 0)
        self.assertLess(time.monotonic() - start, 3.5, "the second bound waited instead of leaving the pass to the first")
        self.assertEqual(first.wait(30), 0)
        self.assertFalse(self.overlap.exists(), "two passes ran at once")
        self.assertEqual(self.run_count(), 2)
        self.assertFalse(self.waiting())

    def test_a_trigger_after_the_holder_lets_go_is_served(self) -> None:
        """The holder checks the trigger again after it unlocks: a bound that lost the lock race to it must not
        leave its trigger behind."""
        disk = self.fake_disk()
        self.write_target(10**9)
        self.pending.touch()
        self.assertEqual(self.bound(disk).wait(30), 0)
        self.assertEqual(self.run_count(), 1)
        self.pending.touch()  # a trigger right after; the next start (or the hook's own fork) serves it
        note = hook.disk_pressure_detached(os.fspath(disk))
        self.assertIn("started in the background", note)
        deadline = time.monotonic() + 10
        while self.run_count() < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.run_count(), 2)

    def test_a_pass_skipped_as_busy_keeps_its_trigger(self) -> None:
        disk = self.fake_disk(busy_first=True)
        self.pending.touch()
        self.assertEqual(self.bound(disk).wait(30), 0)
        self.assertEqual(self.run_count(), 2, "the trigger a busy pass did not serve was dropped")
        self.assertFalse(self.waiting())

    def test_the_bound_never_runs_a_pass_without_its_lock(self) -> None:
        disk = self.fake_disk()
        self.pending.touch()
        self.assertEqual(self.bound(disk, lock=self.dir / "missing" / "hook-pass.lock").wait(30), 0)
        self.assertEqual(self.run_count(), 0)
        self.assertTrue(self.pending.exists(), "the trigger stays for a bound that can lock")

    def test_the_bound_has_one_lifetime_and_leaves_the_trigger_after_it(self) -> None:
        disk = self.fake_disk(seconds=0.5, trigger_every=True)  # a trigger during every pass, without end
        self.pending.touch()
        start = time.monotonic()
        self.assertEqual(self.bound(disk, timeout=10, lifetime=3).wait(30), 0)
        self.assertLess(time.monotonic() - start, 10)
        self.assertGreaterEqual(self.run_count(), 2)
        self.assertTrue(self.waiting(), "the next trigger's bound serves what this one left")
        self.assertFalse(self.overlap.exists())

    def test_a_glaeda_disk_that_cannot_start_keeps_its_trigger(self) -> None:
        self.pending.touch()
        bound = subprocess.Popen([sys.executable, "-c", hook.DISK_BOUND, "30", "2", os.fspath(self.lock),
                                  os.fspath(self.pending), os.fspath(hook.evict_lock_path()),
                                  os.fspath(self.dir / "no-such-python"), "glaeda-disk", "--pressure"])
        self.assertEqual(bound.wait(30), 0)
        self.assertTrue(self.waiting())

    def test_an_infinite_or_huge_timeout_stays_bounded(self) -> None:
        disk = self.fake_disk()
        running = mock.patch.object(hook, "disk_pass_running", return_value=False)  # the first call's bound may run
        running.start()
        self.addCleanup(running.stop)
        with mock.patch.dict(os.environ, {hook.BACKGROUND_DISK_TIMEOUT_ENV: "inf"}):
            note = hook.disk_pressure_detached(os.fspath(disk))
        self.assertIn(f"bounded by {hook.BACKGROUND_DISK_TIMEOUT_S:g} s", note)
        with mock.patch.dict(os.environ, {hook.BACKGROUND_DISK_TIMEOUT_ENV: "1e308"}):
            note = hook.disk_pressure_detached(os.fspath(disk))
        self.assertIn(f"bounded by {hook.BACKGROUND_DISK_TIMEOUT_MAX_S:g} s", note)

    def test_a_claimed_trigger_left_by_a_bound_is_served_by_the_next(self) -> None:
        disk = self.fake_disk()
        Path(f"{self.pending}.claimed").touch()
        self.assertEqual(self.bound(disk).wait(30), 0)
        self.assertEqual(self.run_count(), 1)
        self.assertFalse(self.waiting())

    def test_skip_lines_have_every_key_of_glaedas_run_line(self) -> None:
        hook.disk_skip_line(hook.HOOK_SKIP_COALESCED, 12.0, None)
        row = json.loads(self.log.read_text().splitlines()[-1])
        for key in ("at", "host", "mode", "off", "free_gib_before", "free_gib_after", "target_gib", "target_bytes",
                    "targets", "default_targets", "config_sha256", "caller", "evict_owner", "floor_gib",
                    "reclaimed_gib", "reached", "blocked",
                    "duration_s", "skipped", "cmux_next_ci_over_gib"):
            self.assertIn(key, row)

    def test_the_bound_marks_its_pass_as_the_queues_own(self) -> None:
        """glaeda-disk folds any other pressure run into the queue while the bound holds its lock; the bound's own
        pass must not be folded into itself."""
        seen = self.dir / "env"
        disk = make_executable(self.dir / "glaeda-disk", "import os, pathlib\n"
                               f"pathlib.Path({os.fspath(seen)!r}).write_text(os.environ.get('GLAEDA_DISK_HOOK_PASS', '-')"
                               " + ' ' + os.environ.get('GLAEDA_DISK_CALLER', '-'))\n")
        self.pending.touch()
        self.assertEqual(self.bound(disk).wait(30), 0)
        self.assertEqual(seen.read_text(), "1 runner hook pass")

    def test_a_pass_deferred_for_ci_keeps_its_trigger(self) -> None:
        disk = make_executable(self.dir / "glaeda-disk", "print('deferred: a CI job is running (x); pressure deletes "
                                                         "wait unless free drops below 30')\n")
        self.pending.touch()
        self.assertEqual(self.bound(disk, lifetime=2).wait(30), 0)
        self.assertTrue(self.waiting())

    def test_a_pass_that_exits_nonzero_keeps_its_trigger(self) -> None:
        disk = make_executable(self.dir / "glaeda-disk", "import sys\nsys.exit(1)\n")
        self.pending.touch()
        self.assertEqual(self.bound(disk, lifetime=2).wait(30), 0)
        self.assertTrue(self.waiting())

    def test_floor_evictions_are_never_queued(self) -> None:
        env = hook.floor_disk_env("runner hook floor eviction")
        self.assertEqual((env["GLAEDA_DISK_HOOK_PASS"], env["GLAEDA_DISK_CALLER"]), ("1", "runner hook floor eviction"))

    def test_a_pass_that_could_not_start_keeps_its_trigger(self) -> None:
        disk = self.fake_disk()
        self.pending.touch()
        with hook.evict_lock_path().open("a") as held:  # another eviction past this bound's whole lifetime
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.bound(disk, timeout=1, lifetime=2.5).wait(30), 0)
        self.assertEqual(self.run_count(), 0)
        self.assertTrue(self.waiting())

    def test_a_pass_waits_for_another_evict_lock_holder(self) -> None:
        """A pass started while the gate's or the LaunchAgent's glaeda-disk holds evict.lock would only skip as
        eviction_busy; it waits for that one within its bound instead."""
        disk = self.fake_disk()
        self.pending.touch()
        evict = hook.evict_lock_path()
        with evict.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            proc = self.bound(disk)
            time.sleep(1.5)
            self.assertEqual(self.run_count(), 0)
            released = time.monotonic()
        self.assertEqual(proc.wait(30), 0)
        self.assertEqual(self.run_count(), 1)
        self.assertGreaterEqual(float(self.runs.read_text().split()[0]), released - 0.01)


class XcodeToolchainTest(unittest.TestCase):
    """The pool Xcode's toolchain: the listener's env, and the check that a host's xcrun resolves inside it."""

    XCODE = "/Applications/Xcode_26.6.app"
    CLT_SDK = "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"

    def setUp(self) -> None:
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_listener_env_drops_sdkroot_and_toolchains_and_pins_the_pool_xcode(self) -> None:
        environ = {"PATH": "/opt/homebrew/bin:/usr/bin", "SDKROOT": self.CLT_SDK, "TOOLCHAINS": "swift",
                   "DEVELOPER_DIR": "/Library/Developer/CommandLineTools", "HOME": "/Users/cmux"}
        self.assertEqual(hook.listener_env(environ, self.XCODE),
                         {"PATH": "/opt/homebrew/bin:/usr/bin", "HOME": "/Users/cmux",
                          "DEVELOPER_DIR": "/Applications/Xcode_26.6.app/Contents/Developer"})
        self.assertEqual(hook.listener_env(environ, None),
                         {"PATH": "/opt/homebrew/bin:/usr/bin", "HOME": "/Users/cmux",
                          "DEVELOPER_DIR": "/Library/Developer/CommandLineTools"},
                         "a runner without a pool Xcode keeps its DEVELOPER_DIR")
        self.assertIn("SDKROOT", environ, "a copy: the caller's mapping is left alone")

    def test_swift_versions_are_read_from_swiftc(self) -> None:
        self.assertEqual(hook.swift_version(SWIFT_633), "6.3.3")
        self.assertEqual(hook.swift_version(SWIFT_64), "6.4")
        self.assertEqual(hook.swift_version("Apple Swift version 6.3 (swift-6.3-RELEASE)"), "6.3")
        self.assertIsNone(hook.swift_version(""))
        self.assertIsNone(hook.swift_version("26.5"))

    def test_mismatch_is_decided_from_the_facts_alone(self) -> None:
        xcode_sdk = f"{self.XCODE}/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
        good = {"sdkPath": xcode_sdk, "swift": "6.3.3", "xcodeSwift": "6.3.3", "sdkVersion": "26.5",
                "xcodeSdkVersion": "26.5"}
        self.assertIsNone(hook.xcode_mismatch(self.XCODE, good))
        cmux21 = {"sdkPath": self.CLT_SDK, "swift": "6.4", "xcodeSwift": "6.3.3", "sdkVersion": "27.0",
                  "xcodeSdkVersion": "26.5"}
        why = hook.xcode_mismatch(self.XCODE, cmux21)
        self.assertTrue(why.startswith(f"toolchain: xcrun resolves {self.CLT_SDK} outside Xcode_26.6"), why)
        self.assertIn("xcrun swiftc is Swift 6.4, Xcode_26.6's is 6.3.3", why)
        self.assertIn("SDK 27.0 is newer than Xcode_26.6's SDK 26.5", why)
        cases = {
            "swift only": ({"swift": "6.4"}, "xcrun swiftc is Swift 6.4"),
            "newer sdk only": ({"sdkVersion": "27.0"}, "SDK 27.0 is newer"),
            "a point release of the same sdk": ({"sdkVersion": "26.6"}, None),
            "an older sdk inside xcode": ({"sdkVersion": "26.2"}, None),
            "a sibling Xcode's sdk": ({"sdkPath": xcode_sdk.replace("Xcode_26.6", "Xcode_26.6-beta")}, "outside"),
            "no sdk path (a timeout)": ({"sdkPath": ""}, None),
            "not a path": ({"sdkPath": "26.5"}, None),
            "no swift version": ({"swift": None}, None),
            "no xcode swift version": ({"xcodeSwift": None, "swift": "6.4"}, None),
            "unreadable sdk settings": ({"sdkVersion": None}, None),
            "a tool xcrun cannot find": ({"broken": ["xcrun --find git fails (exit 72: unable to find utility)"]},
                                         "toolchain: xcrun --find git fails (exit 72"),
            "a python3 that does not run": ({"broken": ["/usr/bin/python3 does not run (exit 72: xcode-select)"]},
                                            "/usr/bin/python3 does not run"),
            "nothing broken": ({"broken": []}, None),
        }
        for name, (change, want) in cases.items():
            with self.subTest(name):
                got = hook.xcode_mismatch(self.XCODE, {**good, **change})
                if want is None:
                    self.assertIsNone(got)
                else:
                    self.assertIn(want, got or "")

    def test_a_foreign_sdkroot_or_toolchains_in_an_env_is_named(self) -> None:
        inside = f"{self.XCODE}/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
        self.assertIsNone(hook.leaked_toolchain_env({"PATH": "/usr/bin"}, self.XCODE))
        self.assertIsNone(hook.leaked_toolchain_env({"SDKROOT": inside}, self.XCODE))
        self.assertEqual(hook.leaked_toolchain_env({"SDKROOT": self.CLT_SDK}, self.XCODE),
                         f"toolchain: the runner's environment sets SDKROOT={self.CLT_SDK}, outside Xcode_26.6")
        self.assertIn("TOOLCHAINS=swift", hook.leaked_toolchain_env({"TOOLCHAINS": "swift"}, self.XCODE))
        self.assertIn("SDKROOT=", hook.leaked_toolchain_env({"SDKROOT": inside}, None),
                      "without a pool Xcode any SDKROOT path is foreign")
        for name in ("macosx", "macosx26.5", "iphonesimulator"):
            with self.subTest(name):
                self.assertIsNone(hook.leaked_toolchain_env({"SDKROOT": name}, self.XCODE),
                                  "an SDK name resolves through DEVELOPER_DIR: only a path is a leak")

    def test_the_xcrun_shims_own_sdkroot_is_not_a_leak(self) -> None:
        """/usr/bin/python3 execs Xcode's (or the Command Line Tools') python with SDKROOT set to what xcrun resolved
        for it: under that interpreter SDKROOT says nothing about the listener's env, and the clean-env probe
        (xcode_refusal) judges what xcrun resolves instead."""
        self.assertTrue(hook.under_xcrun_shim("/Applications/Xcode_26.3.app/Contents/Developer/usr/bin/python3"))
        self.assertTrue(hook.under_xcrun_shim("/Library/Developer/CommandLineTools/usr/bin/python3"))
        self.assertFalse(hook.under_xcrun_shim("/opt/homebrew/bin/python3"))
        self.assertFalse(hook.under_xcrun_shim(os.path.expanduser("~/.local/python-3.13.15/bin/python3")))

    def probe_env(self, sdk_path: str | None, swiftc: str | None, hang: bool = False,
                  unfound: tuple = (), shim_runs: bool = True) -> dict[str, str]:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        found = "sleep 30" if hang else f"echo {shlex.quote(sdk_path)}" if sdk_path is not None else "exit 1"
        swift = f"cat <<'OUT'\n{swiftc}\nOUT\n" if swiftc is not None else "exit 1"
        missing = "".join(f"--find\\ {tool}) echo 'xcrun: error: unable to find utility' >&2; exit 72 ;;\n"
                          for tool in unfound)
        make_executable(bin_dir / "xcrun", f'#!/bin/sh\nenv > {self.tmp / "xcrun.env"}\n'
                        f'echo "$*" >> {self.tmp / "xcrun.calls"}\n'
                        f'case "$*" in\n{missing}--find\\ *) echo /usr/bin/found ;;\n'
                        f'*--show-sdk-path*) {found} ;;\n*swiftc*) {swift} ;;\n*) exit 1 ;;\nesac\n')
        make_executable(bin_dir / "python3", "#!/bin/sh\nexit 0\n")
        make_executable(self.tmp / "shim-python3", "#!/bin/sh\nexit 0\n" if shim_runs else
                        "#!/bin/sh\necho \"xcode-select: Failed to locate 'python3'\" >&2\nexit 72\n")
        for name, value in (("SHIM_PYTHON", os.fspath(self.tmp / "shim-python3")), ("XCRUN", os.fspath(bin_dir / "xcrun"))):
            patch = mock.patch.object(hook, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        return {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": os.fspath(self.tmp), "SDKROOT": self.CLT_SDK,
                "TOOLCHAINS": "swift"}

    def test_the_probe_runs_xcrun_in_the_clean_env_and_refuses_a_definite_mismatch(self) -> None:
        xcode = fake_xcode(self.tmp)
        sdk = xcode / "Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
        cache = self.tmp / "cache.json"
        env = self.probe_env(os.fspath(sdk), SWIFT_633)
        self.assertEqual(hook.xcode_facts(os.fspath(xcode), env, time.monotonic() + 10),
                         {"sdkPath": os.fspath(sdk), "swift": "6.3.3", "xcodeSwift": "6.3.3", "sdkVersion": "26.5",
                          "xcodeSdkVersion": "26.5", "broken": []}, "the fake answers every probe: a pass is a real pass")
        self.assertIsNone(hook.xcode_refusal(os.fspath(xcode), env, cache=cache))
        seen = dict(line.split("=", 1) for line in (self.tmp / "xcrun.env").read_text().splitlines() if "=" in line)
        self.assertNotIn("SDKROOT", seen)
        self.assertNotIn("TOOLCHAINS", seen)
        self.assertEqual(seen["DEVELOPER_DIR"], os.fspath(xcode / "Contents/Developer"))
        clt = fake_clt(self.tmp)
        env = self.probe_env(os.fspath(clt), SWIFT_64)
        self.assertIsNone(hook.xcode_refusal(os.fspath(xcode), env, cache=cache),
                          "a pass is reused for XCODE_PROBE_TTL_S")
        why = hook.xcode_refusal(os.fspath(xcode), env, cache=cache, ttl=0)
        self.assertIn(f"xcrun resolves {clt} outside Xcode_26.6", why)
        self.assertIn("SDK 27.0 is newer than Xcode_26.6's SDK 26.5", why)
        cache.unlink()
        self.assertIsNotNone(hook.xcode_refusal(os.fspath(xcode), env, cache=cache))
        self.assertFalse(cache.exists(), "a refusal is never cached: every look and recheck probes again")

    def test_one_probe_batch_refuses_a_tool_xcrun_cannot_find_or_a_python3_that_does_not_run(self) -> None:
        xcode = fake_xcode(self.tmp)
        sdk = os.fspath(xcode / "Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk")
        cache = self.tmp / "cache.json"
        env = self.probe_env(sdk, SWIFT_633, unfound=("clang", "ld"))
        why = hook.xcode_refusal(os.fspath(xcode), env, cache=cache)
        self.assertIn("xcrun --find clang fails (exit 72: xcrun: error: unable to find utility)", why)
        self.assertIn("xcrun --find ld fails", why)
        self.assertNotIn("--find git", why)
        calls = (self.tmp / "xcrun.calls").read_text().splitlines()
        self.assertEqual(sorted(c.split()[1] for c in calls if c.startswith("--find")), sorted(hook.CMUX_TOOLS))
        self.assertEqual(set(hook.CMUX_TOOLS),
                         {"swift", "swiftc", "clang", "git", "make", "ld", "xcodebuild", "python3"})
        self.assertFalse(cache.exists())
        env = self.probe_env(sdk, SWIFT_633, shim_runs=False)
        why = hook.xcode_refusal(os.fspath(xcode), env, cache=cache)
        self.assertEqual(why, f"toolchain: {self.tmp / 'shim-python3'} does not run "
                              "(exit 72: xcode-select: Failed to locate 'python3')")
        env = self.probe_env(sdk, SWIFT_633)
        self.assertIsNone(hook.xcode_refusal(os.fspath(xcode), env, cache=cache))
        self.assertTrue(cache.exists(), "a pass is cached")

    def test_the_probe_is_one_batch_whose_partial_answers_still_count(self) -> None:
        """The probes run as one shell batch under one deadline. A batch killed at its deadline keeps what it
        already saw: a definite failure before the hang refuses, and the probes it never reached are no answer."""
        xcode = fake_xcode(self.tmp)
        env = self.probe_env(None, SWIFT_633, hang=True, unfound=("git",))
        with mock.patch.object(hook.subprocess, "run", side_effect=AssertionError("one batch, not serial probes")):
            start = time.monotonic()
            facts = hook.xcode_facts(os.fspath(xcode), env, time.monotonic() + 2.0)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(facts["sdkPath"], "", "the hung probe is no answer")
        self.assertIn("xcrun --find git fails (exit 72: xcrun: error: unable to find utility)", facts["broken"])
        self.assertEqual(facts["xcodeSwift"], "6.3.3")

    def test_a_probe_that_hangs_answers_within_its_budget_and_does_not_refuse(self) -> None:
        xcode = fake_xcode(self.tmp)
        env = self.probe_env(None, None, hang=True)
        start = time.monotonic()
        self.assertIsNone(hook.xcode_refusal(os.fspath(xcode), env, cache=self.tmp / "cache.json",
                                             deadline=time.monotonic() + 1.0))
        self.assertLess(time.monotonic() - start, 10)
        self.assertFalse((self.tmp / "cache.json").exists(), "no answer is never cached as a pass")

    def test_xcrun_runs_by_absolute_path_so_a_path_without_it_refuses_nothing(self) -> None:
        """The Xcode probe runs XCRUN (/usr/bin/xcrun), never PATH's: a PATH without xcrun would exit 127 for every
        `xcrun --find` and refuse a healthy host."""
        self.assertEqual(hook.XCRUN, "/usr/bin/xcrun")
        xcode = fake_xcode(self.tmp)
        sdk = os.fspath(xcode / "Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk")
        env = self.probe_env(sdk, SWIFT_633)
        (self.tmp / "bin" / "xcrun").rename(self.tmp / "xcrun")
        with mock.patch.object(hook, "XCRUN", os.fspath(self.tmp / "xcrun")):
            facts = hook.xcode_facts(os.fspath(xcode), env, time.monotonic() + 10)
        self.assertEqual((facts["sdkPath"], facts["swift"], facts["broken"]), (sdk, "6.3.3", []))

    def test_the_batch_leaves_no_temp_file_even_when_killed_at_its_deadline(self) -> None:
        xcode = fake_xcode(self.tmp)
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        for hang in (False, True):
            with self.subTest(hang=hang), mock.patch.object(hook.tempfile, "tempdir", os.fspath(scratch)):
                env = self.probe_env(None if hang else "/x", SWIFT_633, hang=hang)
                hook.xcode_facts(os.fspath(xcode), env, time.monotonic() + (1.0 if hang else 10))
                self.assertEqual(list(scratch.iterdir()), [])

    def test_only_a_pass_that_saw_the_sdk_and_both_swifts_is_cached(self) -> None:
        xcode = fake_xcode(self.tmp)
        sdk = os.fspath(xcode / "Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk")
        cache = self.tmp / "cache.json"
        for name, (sdk_path, swiftc) in {"no sdk path": (None, SWIFT_633), "no xcrun swiftc": (sdk, None)}.items():
            with self.subTest(name):
                self.assertIsNone(hook.xcode_refusal(os.fspath(xcode), self.probe_env(sdk_path, swiftc), cache=cache))
                self.assertFalse(cache.exists())
        own = xcode / "Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/swiftc"
        own.rename(own.with_name("swiftc.gone"))
        self.assertIsNone(hook.xcode_refusal(os.fspath(xcode), self.probe_env(sdk, SWIFT_633), cache=cache))
        self.assertFalse(cache.exists(), "the Xcode's own swiftc did not answer")
        own.with_name("swiftc.gone").rename(own)
        self.assertIsNone(hook.xcode_refusal(os.fspath(xcode), self.probe_env(sdk, SWIFT_633), cache=cache))
        self.assertTrue(cache.exists())

    def test_the_xcode_probe_runs_after_the_receipt_probes_on_its_own_budget(self) -> None:
        """Slow Xcode probes on a loaded mini must not starve xcodebuild -version or xcrun --show-sdk-version into a
        '' != want drift refusal: the receipt probes run first on ELIGIBILITY_BUDGET_S, then the Xcode probe on
        XCODE_PROBE_BUDGET_S. Its refusal still wins over drift, as the clearer reason."""
        want = {"rustcVersion": "rustc 1.98.1", "cargoVersion": "cargo 1.98.1", "zigVersion": "0.16.0",
                "xcodeVersion": "26.6", "xcodeBuild": "17F113", "macosSdkVersion": "26.5"}
        answers = {"rustc": "rustc 1.98.1", "cargo": "cargo 1.98.1", "zig": "0.16.0", "python3": "3.13",
                   "gh": "gh version 2", "xcodebuild": "Xcode 26.6\nBuild version 17F113", "xcrun": "26.5"}
        order: list[str] = []

        def probe(argv, env, cwd, deadline):
            order.append(argv[0])
            return answers.get(argv[0], "")

        budgets: list[float] = []

        def refusal(xcode_app, env, deadline=None):
            order.append("xcode_refusal")
            budgets.append(deadline - time.monotonic())
            return "toolchain: xcrun resolves /x outside Xcode_26.6"

        with mock.patch.object(hook, "_probe", side_effect=probe), \
                mock.patch.object(hook, "xcode_refusal", side_effect=refusal):
            why, _ = hook.toolchain_refusal("m4pro-48", want, self.XCODE, align=False)
            self.assertEqual(why, "toolchain: xcrun resolves /x outside Xcode_26.6")
            self.assertEqual(order[-1], "xcode_refusal")
            self.assertIn("xcodebuild", order)
            self.assertGreater(budgets[0], hook.XCODE_PROBE_BUDGET_S - 2)
            answers["xcodebuild"] = ""
            order.clear()
            why, _ = hook.toolchain_refusal("m4pro-48", want, self.XCODE, align=False)
            self.assertEqual(why, "toolchain: xcrun resolves /x outside Xcode_26.6", "the clearer reason wins")

    def test_the_runner_xcode_is_the_job_started_hooks_toolchain_xcode(self) -> None:
        runner = self.tmp / "actions-runner-glaeda-1"
        (runner / "glaeda-hooks").mkdir(parents=True)
        self.assertIsNone(hook.runner_xcode(runner))
        script = runner / hook.RUNNER_HOOK_SCRIPT
        script.write_text("#!/bin/bash\nexec /usr/bin/python3 /hook job-started --allowed-owner manaflow-ai "
                          "--require-eligible --fleet-class m6-16 --toolchain-xcode '/Applications/Xcode 26.app'\n")
        self.assertEqual(hook.runner_xcode(runner), "/Applications/Xcode 26.app")
        script.write_text("#!/bin/bash\nexec /usr/bin/python3 /hook job-started --allowed-owner manaflow-ai\n")
        self.assertIsNone(hook.runner_xcode(runner))

    def test_an_adopted_listener_is_stale_unless_it_was_started_clean(self) -> None:
        dev = f"{self.XCODE}/Contents/Developer"
        self.assertIsNone(hook.adopted_env_stale("clean", {"SDKROOT": self.CLT_SDK}, self.XCODE),
                          "a gate that started it with the clean env says so across the reload")
        self.assertEqual(hook.adopted_env_stale("SDKROOT was set", {}, self.XCODE), "SDKROOT was set")
        self.assertIsNone(hook.adopted_env_stale(None, {"DEVELOPER_DIR": dev}, self.XCODE))
        self.assertIn("SDKROOT=", hook.adopted_env_stale(None, {"SDKROOT": self.CLT_SDK, "DEVELOPER_DIR": dev},
                                                        self.XCODE))
        self.assertIn("DEVELOPER_DIR", hook.adopted_env_stale(None, {}, self.XCODE),
                      "a listener from before the gate pinned DEVELOPER_DIR")
        self.assertIsNone(hook.adopted_env_stale(None, {}, None))


FAKE_LISTENER = """#!/usr/bin/env python3
import signal, sys, time
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
print("Listening for Jobs", flush=True)
while True:
    time.sleep(0.05)
"""


# The newest Runner_*.log on cmuxs-mac-mini-5 when the gate stopped its listener at 2026-09-28 13:53:03Z (trimmed to
# the lines the gate reads): a job ended, the listener acknowledged the next runner request at 13:53:01, and the
# gate's SIGINT cancelled its acquirejob POST. GitHub had the job on that runner and failed it 10 min later with
# "The self-hosted runner lost communication with the server".
MID_ACQUIRE_DIAG = """\
[2026-09-28 13:51:52Z INFO Terminal] WRITE LINE: 2026-09-28 13:51:52Z: Listening for Jobs
[2026-09-28 13:51:53Z INFO BrokerMessageListener] Acknowledging runner request '930d9f84-513b-5f03-96b5-0138d88db265'.
[2026-09-28 13:51:54Z INFO JobDispatcher] Job request 0 for plan 7d33e0be-ef63-4567-9fe3-daa350b05775 job 930d9f84-513b-5f03-96b5-0138d88db265 received.
[2026-09-28 13:51:55Z INFO ProcessInvokerWrapper]   File name: '/Users/cmux/actions-runner-glaeda-1/bin/Runner.Worker'
[2026-09-28 13:52:59Z INFO JobDispatcher] Worker finished for job 930d9f84-513b-5f03-96b5-0138d88db265. Code: 102
[2026-09-28 13:52:59Z INFO JobDispatcher] finish job request for job 930d9f84-513b-5f03-96b5-0138d88db265 with result: Failed
[2026-09-28 13:52:59Z INFO BrokerMessageListener] Received job status event. JobState: Online
[2026-09-28 13:52:59Z INFO BrokerMessageListener] Get messages has been cancelled using local token source. Continue to get messages with new status.
[2026-09-28 13:53:01Z INFO BrokerMessageListener] Acknowledging runner request 'eeb83692-303d-516a-b3bf-e6e6f93c1d90'.
[2026-09-28 13:53:02Z INFO GitHubActionsService] AAD Correlation ID for this token request: Unknown
"""
ACQUIRE_FAILED = ("[2026-09-28 13:53:13Z ERR  Runner] Caught exception from acquiring job message: "
                  "System.Net.Http.HttpRequestException: 503\n   at GitHub.Runner.Listener.Runner.RunAsync()\n")


class LeakedAppTest(unittest.TestCase):
    def test_direct_child_scan_ignores_its_ps_probe(self) -> None:
        result = mock.Mock(stdout="101 42 ps\n102 42 Python\n", returncode=0)
        with mock.patch.object(hook.subprocess, "run", return_value=result):
            self.assertEqual(hook.direct_child_pids(42), {102})

    def test_orphan_hook_requires_dead_runner_and_no_children(self) -> None:
        with mock.patch.object(hook, "pid_alive", return_value=False):
            self.assertTrue(hook.orphan_hook_should_exit(42, "ps:start", set()))
            self.assertFalse(hook.orphan_hook_should_exit(42, "ps:start", {99}))
            self.assertTrue(hook.orphan_hook_should_exit(42, "ps:start", set(), parent_pid=1))
        with mock.patch.object(hook, "pid_alive", return_value=True), \
                mock.patch.object(hook, "process_identity", return_value=None):
            self.assertFalse(hook.orphan_hook_should_exit(42, "ps:start", set()))
        with mock.patch.object(hook, "pid_alive", return_value=True), \
                mock.patch.object(hook, "process_identity", return_value="ps:new"):
            self.assertTrue(hook.orphan_hook_should_exit(42, "ps:start", set(), parent_pid=99))
        with mock.patch.object(hook, "pid_alive", return_value=True), \
                mock.patch.object(hook, "process_identity", return_value="ps:new"):
            self.assertTrue(hook.orphan_hook_should_exit(42, "ps:start", set()))
        with mock.patch.object(hook, "pid_alive", return_value=True), \
                mock.patch.object(hook, "process_identity", return_value="ps:start"):
            self.assertFalse(hook.orphan_hook_should_exit(42, "ps:start", set()))

    def test_orphaned_test_app_is_selected_but_live_job_app_is_protected(self) -> None:
        app = "/Users/cmux/actions-runner-glaeda/_work/_temp/cmux-derived-data-tests-123-1-shard-2/Build/Products/Debug/cmux DEV.app/Contents/MacOS/cmux DEV"
        table = {
            10: (1, app),
            20: (30, app),
            30: (1, "/Users/cmux/actions-runner-glaeda/bin/Runner.Worker spawnclient"),
        }
        self.assertEqual([pid for pid, _ in hook.leaked_test_app_pids(table)], [10])
        self.assertEqual([pid for pid, _ in hook.leaked_test_app_pids(table, {30})], [10, 20])

    def test_reparented_app_is_protected_by_live_temp_tree_owner(self) -> None:
        root = "/Users/cmux/actions-runner-glaeda/_work/_temp/cmux-derived-data-tests-123-1-shard-2"
        app = root + "/Build/Products/Debug/cmux DEV.app/Contents/MacOS/cmux DEV"
        table = {
            10: (1, app),
            40: (1, "/usr/bin/xcodebuild -derivedDataPath " + root + "/Build test-without-building"),
            50: (1, "/Users/cmux/actions-runner-glaeda/bin/Runner.Worker spawnclient"),
        }
        self.assertEqual(hook.leaked_test_app_pids(table), [])

    def test_orphaned_media_app_is_selected_only_when_no_other_runner_is_live(self) -> None:
        app = "/Users/cmux/Library/Developer/Xcode/DerivedData/cmux-ci-media-pr-20473-head/Build/Products/Debug/cmux DEV.app/Contents/Resources/bin/cmux-tui --headless --session cmux-chief"
        table = {
            10: (1, app),
            30: (1, "/Users/cmux/actions-runner-glaeda-2/bin/Runner.Worker spawnclient"),
        }
        self.assertEqual(hook.leaked_media_app_pids(table), [])
        self.assertEqual([pid for pid, _ in hook.leaked_media_app_pids(table, {30})], [10])

    def test_media_app_with_live_runner_ancestor_is_protected(self) -> None:
        app = "/Users/cmux/Library/Developer/Xcode/DerivedData/cmux-ci-media-feat-cmux-next-base/Build/Products/Debug/cmux DEV.app/Contents/Resources/bin/cmux-tui --headless"
        table = {
            10: (20, app),
            20: (1, "/Users/cmux/actions-runner-glaeda/bin/Runner.Worker spawnclient"),
        }
        self.assertEqual(hook.leaked_media_app_pids(table), [])

    MEDIA = ("/Users/cmux/Library/Developer/Xcode/DerivedData/cmux-ci-media-pr-20473-head/"
             "Build/Products/Debug/cmux DEV ci-media-pr-20473-head.app/Contents/Resources/bin/cmux-tui "
             "--headless --session cmux-chief-1234")

    def test_actual_tagged_media_and_screen_detection_are_selected(self) -> None:
        detector = self.MEDIA.split("/Contents/", 1)[0] + "/Contents/Resources/bin/cmux-agent-screen-detection"
        table = {10: (1, self.MEDIA), 11: (10, detector)}
        self.assertEqual([pid for pid, _ in hook.leaked_media_app_pids(table)], [10, 11])
        bad = self.MEDIA.replace("cmux DEV ci-media-pr-20473-head.app", "cmux DEV ci-media-other.app")
        self.assertEqual(hook.leaked_media_app_pids({10: (1, bad)}), [])

    def test_cleanup_ends_detached_media_app(self) -> None:
        pid, table = 123, {123: (1, self.MEDIA)}
        with mock.patch.object(hook, "processes", side_effect=[table, table]), \
             mock.patch.object(hook, "process_identity", return_value="ps:start"), \
             mock.patch.object(hook, "process_alive", return_value=False), \
             mock.patch.object(hook.os, "kill") as kill:
            notes = hook._cleanup_leaked_media_apps_locked()
        kill.assert_called_once_with(pid, signal.SIGTERM)
        self.assertTrue(notes and "media cleanup: ended pid" in notes[0], notes)

    def test_media_cleanup_preserves_reused_pid_or_changed_command(self) -> None:
        for changed_command, identity in ((self.MEDIA, "ps:new"), (self.MEDIA + " --new", "ps:old")):
            with self.subTest(command=changed_command, identity=identity), \
                 mock.patch.object(hook, "processes", side_effect=[{123: (1, self.MEDIA)}, {123: (1, changed_command)}]), \
                 mock.patch.object(hook, "process_identity", side_effect=["ps:old", identity]), \
                 mock.patch.object(hook.os, "kill") as kill:
                self.assertEqual(hook._cleanup_leaked_media_apps_locked(), [])
            kill.assert_not_called()

    def test_media_cleanup_obeys_host_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host.lock"
            path.touch()
            with path.open("r+") as held, mock.patch.object(hook, "HOST_LOCK", str(path)), \
                 mock.patch.object(hook, "_cleanup_leaked_media_apps_locked") as clean:
                fcntl.flock(held, fcntl.LOCK_SH | fcntl.LOCK_NB)
                self.assertEqual(hook.cleanup_leaked_media_apps(), [])
                clean.assert_not_called()
                fcntl.flock(held, fcntl.LOCK_UN)
                clean.return_value = ["receipt"]
                self.assertEqual(hook.cleanup_leaked_media_apps(), ["receipt"])


class JobLeftoverTest(unittest.TestCase):
    WORK = "/Users/cmux/actions-runner-glaeda-3/_work"

    def test_detached_daemon_and_its_hosts_from_this_runner_are_leftovers(self) -> None:
        daemon = self.WORK + "/cmux/cmux/cmux-tui/target/hosted/tree/abc/cmux-tui --headless --session cnd-bd-1"
        table = {
            10: (1, daemon),
            11: (10, "cmux-tui __terminal-host --bootstrap-stdio"),
            12: (11, "/bin/zsh"),
            20: (1, "/Users/cmux/actions-runner-glaeda-2/_work/cmux/cmux/cmux-tui/target/x/cmux-tui --headless"),
            30: (1, "/Users/cmux/Applications/cmux.app/Contents/Resources/bin/cmux-tui --headless --session cmux-app"),
            31: (30, "cmux-tui __terminal-host --bootstrap-stdio"),
            40: (1, "'" + self.WORK + "/_temp/cmux-e2e-products-1/Build/Products/Debug/cmux DEV.app/Contents/MacOS/cmux DEV'"),
        }
        self.assertEqual([pid for pid, _ in hook.work_leftover_pids(table, self.WORK, set())], [10, 11, 12, 40])

    def test_hook_and_its_ancestors_are_never_leftovers(self) -> None:
        table = {
            50: (1, "/Users/cmux/actions-runner-glaeda-3/bin/Runner.Worker spawnclient 1 2"),
            51: (50, "/bin/bash " + self.WORK + "/_temp/hook.sh"),
            52: (51, self.WORK + "/cmux/cmux/scripts/something"),
        }
        self.assertEqual(hook.work_leftover_pids(table, self.WORK, {50, 51, 52}), [])

    def test_cleanup_ends_a_detached_process_running_from_work(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        work = tmp / "actions-runner-t" / "_work"
        # Detached like a leaked daemon (reparented to launchd, never this test's child), with its command
        # naming a binary under _work. A copied /bin/sleep would not run, so argv0 carries the path.
        out = subprocess.run(["/bin/bash", "-c", f"(exec -a '{work}/bin/daemon' /bin/sleep 60) >/dev/null 2>&1 & echo $!"],
                             capture_output=True, text=True, check=True).stdout
        pid = int(out.strip())
        deadline = time.monotonic() + 5
        while str(work) not in hook.processes().get(pid, (0, ""))[1] and time.monotonic() < deadline:
            time.sleep(0.05)

        def kill_if_left() -> None:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        self.addCleanup(kill_if_left)
        with mock.patch.object(hook, "process_cwds", return_value=None), \
             mock.patch.object(hook, "git_repair_protected", return_value=set()):
            notes = hook.cleanup_job_leftovers(str(work))
        self.assertFalse(hook.pid_alive(pid))
        self.assertTrue(notes and "ended 1 leftover" in notes[0], notes)
        self.assertTrue(notes[0].endswith(": daemon"), notes)  # the executable's name, never its arguments

    def test_orphan_working_in_work_from_a_copied_binary_is_a_leftover(self) -> None:
        # cmuxs-mac-mini-6, 2026-10-21: copies of cmux-tui outside _work, cwd still in this runner's checkout
        table = {
            60: (1, "/var/folders/hq/x/T/cnd-bin-59647/cmux-tui --headless --session cnd-crash-1"),
            61: (60, "cmux-tui __terminal-host --bootstrap-stdio"),
            62: (1, "/bin/sh /tmp/slr-ABD/cmux-tui remote connect ssh://drill --headless"),
            63: (62, "sleep 5"),
            64: (700, "/bin/sh /tmp/slr-X/cmux-tui remote connect"),  # still has a parent: not an orphan
            65: (1, "/tmp/other/cmux-tui --headless"),  # another runner's checkout
        }
        cwds = {60: self.WORK + "/cmux/cmux/Packages/macOS/CmuxNext", 62: self.WORK + "/cmux", 64: self.WORK,
                65: "/Users/cmux/actions-runner-glaeda-33/_work/cmux", 61: "/"}
        found = hook.work_leftover_pids(table, self.WORK, set(), cwds)
        self.assertEqual([pid for pid, _ in found], [60, 61, 62, 63])

    def test_runner_glaeda_and_git_maintenance_processes_are_spared(self) -> None:
        table = {
            70: (1, "/Users/cmux/actions-runner-glaeda-3/bin/Runner.Listener run"),
            71: (1, "/bin/bash /Users/cmux/actions-runner-glaeda-3/glaeda-hooks/listen.sh"),
            72: (1, "/usr/bin/python3 /Users/cmux/actions-runner-glaeda-3/glaeda-hooks/glaeda-cmux-runner-hook listen"),
            73: (1, "/usr/bin/git -C . fetch --refetch origin"),
            74: (1, "/Library/Developer/CommandLineTools/usr/libexec/git-core/git-pack-objects --revs"),
            75: (1, "/usr/bin/git status"),
        }
        cwds = {pid: self.WORK + "/cmux" for pid in table}
        self.assertEqual([pid for pid, _ in hook.work_leftover_pids(table, self.WORK, set(), cwds)], [75])
        self.assertTrue(hook.leftover_spared(""))

    def test_orphans_only_drops_command_roots_that_still_have_a_parent(self) -> None:
        table = {80: (1, self.WORK + "/cmux/a"), 81: (90, self.WORK + "/cmux/b"), 90: (1, "/sbin/launchd-ish")}
        self.assertEqual([pid for pid, _ in hook.work_leftover_pids(table, self.WORK, set(), orphans_only=True)], [80])
        self.assertEqual([pid for pid, _ in hook.work_leftover_pids(table, self.WORK, set())], [80, 81])

    def test_git_repair_protected_lists_pids_and_descendants(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        listed = tmp / "protected"
        listed.write_text("90\tmachine-hygiene repack, and every child\nnot-a-pid\n999\tgone\n")
        table = {90: (1, "/bin/sh repack.sh"), 91: (90, "git repack -a"), 92: (1, "other")}
        self.assertEqual(hook.git_repair_protected(table, listed), {90, 91})
        self.assertEqual(hook.git_repair_protected(table, tmp / "missing"), set())
        self.assertIsNone(hook.git_repair_protected(table, tmp))  # a directory: unreadable, protects everything
        cwds = {90: self.WORK + "/cmux", 91: self.WORK + "/cmux"}
        self.assertEqual(hook.work_leftover_pids(table, self.WORK, {90, 91}, cwds), [])

    def test_job_leftovers_end_nothing_when_the_protected_list_is_unreadable(self) -> None:
        table = {10: (1, self.WORK + "/cmux/daemon")}
        with mock.patch.object(hook, "processes", return_value=table), \
             mock.patch.object(hook, "git_repair_protected", return_value=None), \
             mock.patch.object(hook.os, "kill") as kill:
            self.assertEqual(hook.cleanup_job_leftovers(self.WORK), [])
        kill.assert_not_called()


    def test_sessions_with_a_terminal_and_spared_trees_are_never_leftovers(self) -> None:
        table = {
            100: (1, "tmux new -s agent"), 101: (100, "/bin/zsh"), 102: (101, "/usr/bin/python3 x.py"),
            110: (1, "/usr/local/bin/claude --resume"), 111: (110, "node helper.js"),
            120: (1, "/var/folders/x/T/cnd-bin-1/cmux-tui --headless"),  # has a terminal: someone's session
            130: (1, "/var/folders/x/T/cnd-bin-2/cmux-tui --headless"), 131: (130, "tmux -L inner"),
            132: (131, "/bin/zsh"), 133: (130, "/bin/zsh -l"), 134: (133, "vim notes"), 135: (130, "cat"),
        }
        cwds = {pid: self.WORK + "/cmux" for pid in (100, 110, 120, 130)}
        found = hook.work_leftover_pids(table, self.WORK, set(), cwds, ttys={120, 133})
        self.assertEqual([pid for pid, _ in found], [130, 135])  # 131's tmux, 133's terminal and all under them stay

    def test_glaedas_detached_disk_bound_is_spared(self) -> None:
        bound = f"{sys.executable} -c {hook.DISK_BOUND} 120 {sys.executable} /Users/cmux/.local/bin/glaeda-disk --pressure"
        self.assertTrue(hook.leftover_spared(bound))
        self.assertTrue(hook.leftover_spared("/usr/bin/python3 /Users/cmux/.local/bin/glaeda-disk --pressure --apply"))
        self.assertFalse(hook.leftover_spared("/Users/cmux/actions-runner-glaeda-3/_work/cmux/x/cmux-tui --headless"))

    def test_cwd_rule_matches_the_resolved_work_path(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "real" / "_work").mkdir(parents=True)
        (tmp / "link").symlink_to(tmp / "real")
        work = os.fspath(tmp / "link" / "_work")
        resolved = os.path.realpath(work)
        table = {140: (1, "/tmp/x/cmux-tui --headless")}
        self.assertEqual([pid for pid, _ in hook.work_leftover_pids(table, work, set(), {140: resolved + "/cmux"})],
                         [140])

    def test_work_dir_comes_from_runner_temp_only(self) -> None:
        with mock.patch.dict(os.environ, {"RUNNER_TEMP": self.WORK + "/_temp"}):
            self.assertEqual(hook.job_work_dir(), self.WORK)
        for temp in ("", "/tmp", "/Users/cmux/_work/_temp", self.WORK + "/_temp/x"):
            with mock.patch.dict(os.environ, {"RUNNER_TEMP": temp}):
                self.assertIsNone(hook.job_work_dir(), temp)


class IdleLeftoverSweepTest(unittest.TestCase):
    RUNNER = Path("/Users/cmux/actions-runner-glaeda-11")
    WORK = "/Users/cmux/actions-runner-glaeda-11/_work"
    ORPHAN = "/var/folders/hq/x/T/cnd-bin-1/cmux-tui --headless --session cnd-crash-1"

    def setUp(self) -> None:
        self.alive: set[int] = set()

        def kill(pid: int, sig: int) -> None:
            if sig in (signal.SIGTERM, signal.SIGKILL):
                self.alive.discard(pid)
        for patcher in (mock.patch.object(hook.os, "kill", side_effect=kill),
                        mock.patch.object(hook, "pid_alive", side_effect=lambda pid: pid in self.alive),
                        mock.patch.object(hook.os, "geteuid", return_value=501),
                        mock.patch.object(hook, "git_repair_protected", return_value=set()),
                        mock.patch.object(hook, "tty_pids", return_value=set()),
                        mock.patch.object(hook, "LEFTOVER_TERM_S", 0.0)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def table(self, worker_of: str | None = None) -> dict[int, tuple[int, str]]:
        table = {60: (1, self.ORPHAN), 61: (60, "cmux-tui __terminal-host"),
                 62: (1, "/tmp/x/daemon"), 99: (1, "/usr/bin/python3 glaeda-cmux-runner-hook listen")}
        if worker_of:
            table[50] = (40, f"/Users/cmux/{worker_of}/bin/Runner.Worker spawnclient 1 2")
        return table

    def cwds(self) -> dict[int, str]:
        return {60: self.WORK + "/cmux/cmux/Packages/macOS/CmuxNext", 61: "/",
                62: "/Users/cmux/actions-runner-glaeda-2/_work/cmux"}

    def test_ends_this_runners_orphans_only(self) -> None:
        self.alive = {60, 61, 62}
        with mock.patch.object(hook, "processes", side_effect=lambda: self.table("actions-runner-glaeda-2")), \
             mock.patch.object(hook, "process_cwds", return_value=self.cwds()):
            notes = hook.sweep_idle_leftovers(self.RUNNER)
        self.assertEqual(self.alive, {62})  # another runner's orphan, mid-job or not, is never this sweep's
        self.assertEqual(len(notes), 2, notes)
        self.assertIn("pid 60 (cmux-tui, cwd _work/cmux/cmux/Packages/macOS/CmuxNext)", notes[0])
        self.assertNotIn("--session", " ".join(notes))  # never arguments

    def test_a_live_worker_of_this_runner_vetoes_the_sweep(self) -> None:
        self.alive = {60, 61}
        with mock.patch.object(hook, "processes", side_effect=lambda: self.table("actions-runner-glaeda-11")), \
             mock.patch.object(hook, "process_cwds", return_value=self.cwds()):
            self.assertEqual(hook.sweep_idle_leftovers(self.RUNNER), [])
        self.assertEqual(self.alive, {60, 61})

    def test_a_worker_appearing_before_the_signal_vetoes_it(self) -> None:
        self.alive = {60, 61}
        tables = iter([self.table(), self.table("actions-runner-glaeda-11")])
        with mock.patch.object(hook, "processes", side_effect=lambda: next(tables, self.table("actions-runner-glaeda-11"))), \
             mock.patch.object(hook, "process_cwds", return_value=self.cwds()):
            self.assertEqual(hook.sweep_idle_leftovers(self.RUNNER), [])
        self.assertEqual(self.alive, {60, 61})

    def test_missing_evidence_fails_closed(self) -> None:
        self.alive = {60, 61}
        with mock.patch.object(hook, "processes", side_effect=lambda: self.table()), \
             mock.patch.object(hook, "process_cwds", return_value=None):
            self.assertEqual(hook.sweep_idle_leftovers(self.RUNNER), [])
        with mock.patch.object(hook, "processes", side_effect=lambda: self.table()), \
             mock.patch.object(hook, "process_cwds", return_value=self.cwds()), \
             mock.patch.object(hook, "git_repair_protected", return_value=None):
            self.assertEqual(hook.sweep_idle_leftovers(self.RUNNER), [])
        with mock.patch.object(hook, "processes", side_effect=subprocess.TimeoutExpired("ps", 30)):
            with self.assertRaises(subprocess.TimeoutExpired):  # Gate.sweep logs it and tries later
                hook.sweep_idle_leftovers(self.RUNNER)
        self.assertEqual(self.alive, {60, 61})

    def test_no_terminal_evidence_or_a_terminal_ends_nothing(self) -> None:
        self.alive = {60, 61}
        for ttys in (None, {60}):
            with mock.patch.object(hook, "processes", side_effect=lambda: self.table()), \
                 mock.patch.object(hook, "process_cwds", return_value=self.cwds()), \
                 mock.patch.object(hook, "tty_pids", return_value=ttys):
                self.assertEqual(hook.sweep_idle_leftovers(self.RUNNER), [])
        self.assertEqual(self.alive, {60, 61})

    def test_root_never_sweeps(self) -> None:
        self.alive = {60, 61}
        with mock.patch.object(hook.os, "geteuid", return_value=0), \
             mock.patch.object(hook, "processes", side_effect=lambda: self.table()), \
             mock.patch.object(hook, "process_cwds", return_value=self.cwds()):
            self.assertEqual(hook.sweep_idle_leftovers(self.RUNNER), [])
        self.assertEqual(self.alive, {60, 61})

    def test_gate_sweeps_only_idle_and_at_most_every_sweep_interval(self) -> None:
        gate = object.__new__(hook.Gate)
        gate.runner_dir = self.RUNNER
        gate.swept_at = time.monotonic() - hook.GATE_SWEEP_S - 1
        with mock.patch.object(hook.Gate, "busy", return_value=True), \
             mock.patch.object(hook, "sweep_idle_leftovers") as sweep:
            gate.sweep()
        sweep.assert_not_called()
        gate.swept_at = time.monotonic() - hook.GATE_SWEEP_S - 1
        with mock.patch.object(hook.Gate, "busy", return_value=False), \
             mock.patch.object(hook, "sweep_idle_leftovers", return_value=["ended leftover pid 60"]) as sweep, \
             mock.patch.object(hook, "gate_log") as log:
            gate.sweep()
            gate.sweep()  # within the interval: no second sweep
        sweep.assert_called_once_with(self.RUNNER)
        log.assert_called_once_with("ended leftover pid 60")
        gate.swept_at = time.monotonic() - hook.GATE_SWEEP_S - 1
        with mock.patch.object(hook.Gate, "busy", return_value=False), \
             mock.patch.object(hook, "sweep_idle_leftovers", side_effect=OSError("boom")), \
             mock.patch.object(hook, "gate_log") as log:
            gate.sweep()  # never raises
        self.assertIn("leftover sweep failed", log.call_args[0][0])


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        sweep = mock.patch.object(hook, "sweep_idle_leftovers", return_value=[])  # IdleLeftoverSweepTest covers it
        sweep.start()
        self.addCleanup(sweep.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lock = self.tmp / "host.lock"
        self.lock.touch()
        self.state = self.tmp / "state"
        self.runner = self.tmp / "actions-runner-glaeda"
        (self.runner / "bin").mkdir(parents=True)
        listener = self.runner / "bin/Runner.Listener"
        listener.write_text(FAKE_LISTENER)
        listener.chmod(0o755)
        # The real layers: run.sh runs run-helper.sh, which runs the listener and passes its code back.
        helper = self.runner / "run-helper.sh"
        helper.write_text(f"#!/bin/bash\n{sys.executable} {listener}\nexit $?\n")
        helper.chmod(0o755)
        run_sh = self.runner / "run.sh"
        run_sh.write_text(f"#!/bin/bash\n{helper}\nexit 0\n")
        run_sh.chmod(0o755)
        self.addCleanup(self.reap)
        # A cool mini unless a test says otherwise (the real reading depends on the machine running the test).
        self.level: int | None = 0
        patcher = mock.patch.object(hook, "thermal_pressure_level", side_effect=lambda: self.level)
        patcher.start()
        self.addCleanup(patcher.stop)

    def reap(self) -> None:
        with contextlib.suppress(Exception):
            table = hook.processes()
        for pid, (_, command) in (locals().get("table") or {}).items():
            if os.fspath(self.runner) in command:
                with contextlib.suppress(OSError):
                    os.kill(pid, 9)

    def listeners(self) -> list[int]:
        return [pid for pid, (_, cmd) in hook.processes().items()
                if os.fspath(self.runner / "bin/Runner.Listener") in cmd]

    def held(self) -> str | None:
        return hook.host_held(os.fspath(self.lock), os.fspath(self.tmp / "none.json"), time.time())

    def hold(self, mode: int) -> int:
        fd = os.open(self.lock, os.O_RDONLY)
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        return fd

    def opener(self, name: str) -> subprocess.Popen:
        """A process with the host lock open, whose argv carries `name`."""
        script = self.tmp / name
        script.write_text(f"import time\nf = open({os.fspath(self.lock)!r})\nprint('open', flush=True)\n"
                          "time.sleep(60)\n")
        proc = subprocess.Popen([sys.executable, os.fspath(script)], stdout=subprocess.PIPE)
        self.addCleanup(proc.kill)
        proc.stdout.readline()
        return proc

    def blocked_opener(self, name: str) -> subprocess.Popen:
        """A process blocked acquiring LOCK_EX after opening the host lock."""
        script = self.tmp / name
        script.write_text(f"import fcntl, time\nf = open({os.fspath(self.lock)!r})\nprint('open', flush=True)\n"
                          "fcntl.flock(f, fcntl.LOCK_EX)\ntime.sleep(60)\n")
        proc = subprocess.Popen([sys.executable, os.fspath(script)], stdout=subprocess.PIPE)
        self.addCleanup(proc.kill)
        proc.stdout.readline()
        return proc

    def test_registered_fleet_waiter_is_seen_without_a_kernel_wait_channel(self) -> None:
        waiters = Path(str(self.lock) + ".waiters")
        waiters.mkdir()
        (waiters / "4242-refresh-build-1").touch()
        with mock.patch.object(hook, "_lock_processes", return_value=([], "")), \
                mock.patch.object(hook, "pid_alive", return_value=True):
            self.assertEqual(hook.lock_waiters(os.fspath(self.lock), set()), [4242])

    def test_waiter_markers_ignore_dead_stale_own_and_symlink_entries(self) -> None:
        waiters = Path(str(self.lock) + ".waiters")
        waiters.mkdir()
        for name in ("4242-refresh-build-1", "4243-blocked", "4244-refresh-stale-1", "4245-own"):
            (waiters / name).touch()
        old = time.time() - 60
        os.utime(waiters / "4244-refresh-stale-1", (old, old))
        os.utime(waiters / "4243-blocked", (old, old))
        (waiters / "4246-symlink").symlink_to(waiters / "4243-blocked")
        with mock.patch.object(hook, "_lock_processes", return_value=([], "")), \
                mock.patch.object(hook, "pid_alive", side_effect=lambda pid: pid != 4242):
            self.assertEqual(hook.lock_waiters(os.fspath(self.lock), {4245}), [4243])

    def waited(self, t0: float, start: float, until: float, pids=(77,), notes: list[str] | None = None,
               step: float = 60, identity=lambda pid: f"start-{pid}") -> list[int]:
        """Gate readings every `step` seconds from start to until (as a polling gate takes them); the last one."""
        t, out = start, []
        while True:
            out = hook.bounded_waiters(self.state, list(pids), now=t0 + t,
                                       log=notes.append if notes is not None else None, identity=identity)
            if t >= until:
                return out
            t = min(until, t + step)

    def test_a_waiter_that_never_gets_the_host_is_yielded_to_for_a_bounded_time(self) -> None:
        hold, waive = hook.GATE_WAITER_MAX_HOLD_S, hook.GATE_WAITER_WAIVE_S
        notes: list[str] = []
        t0 = 1_000_000.0
        self.assertEqual(self.waited(t0, 0, hold - 60, notes=notes), [77], "yielded to within the hold")
        self.assertEqual(notes, [])
        self.assertEqual(self.waited(t0, hold, hold + 1, notes=notes), [], "past the hold the gate listens anyway")
        self.assertEqual(len(notes), 1)
        self.assertIn("pid 77", notes[0])
        self.assertIn("without getting it", notes[0])
        self.assertEqual(self.waited(t0, hold + 60, hold + waive - 60, notes=notes), [])
        self.assertEqual(len(notes), 1, "one note per waived stretch, not one per poll")
        self.assertEqual(self.waited(t0, hold + waive, hold + waive + 1, notes=notes), [77],
                         "then honoured again, so a slow build still gets a turn")
        self.assertEqual(self.waited(t0, hold + waive + 60, 2 * hold + waive + 1, notes=notes), [])
        self.assertEqual(len(notes), 2)
        # another pid on the same host has its own span
        self.assertEqual(self.waited(t0, 2 * hold + waive + 2, 2 * hold + waive + 2, (77, 88)), [88])

    def test_replay_a_worker_waiting_for_over_an_hour_is_bounded(self) -> None:
        # 2026-10-26, a std mini: the fleet worker (one pid) waited for host.lock from 22:19Z, taking it now and then
        # only to hand its build back for disk (2 to 12 min a try) and wait again. Every gate held its listener off
        # for over an hour (until at least 23:28Z). Replayed at the gate's 30 s waiter cadence, with the lock taken
        # for 2 min at 22:47Z and 23:13Z as the worker did: the listeners must come back within the hold.
        t0 = 5_000_000.0  # 22:19:00Z
        notes: list[str] = []
        hold = hook.GATE_WAITER_MAX_HOLD_S
        self.assertLessEqual(hold, 10 * 60, "the policy bound is at most 10 min")
        resumed = None
        for t in range(0, 69 * 60, 30):  # to 23:28Z
            if 28 * 60 <= t < 30 * 60 or 54 * 60 <= t < 56 * 60:
                continue  # the worker holds the lock: the gate reads "holds the host lock", no waiter reading
            owed = hook.bounded_waiters(self.state, [44], now=t0 + t, log=notes.append, identity=lambda pid: "w")
            if not owed and resumed is None:
                resumed = t
        self.assertIsNotNone(resumed)
        self.assertLessEqual(resumed, hold + 30, "listening again within one reading of the hold")
        self.assertTrue(notes and "pid 44" in notes[0], notes)

    def test_a_waiter_that_got_the_host_starts_a_new_wait(self) -> None:
        hold, gap = hook.GATE_WAITER_MAX_HOLD_S, hook.GATE_WAITER_GAP_S
        t0 = 2_000_000.0
        self.assertEqual(self.waited(t0, 0, hold - 120), [77])
        # unseen past the gap (it held the host for a build): the next wait starts afresh
        self.assertEqual(self.waited(t0, hold - 120 + gap, hold - 120 + gap + hold - 60), [77])
        # unseen for less than the gap (a hand-back's eviction under the lock): the same wait goes on
        self.assertEqual(self.waited(t0, hold - 120 + gap + hold - 60 + gap - 60,
                                     hold - 120 + gap + hold - 60 + gap - 60), [])

    def test_a_reused_pid_or_a_reboot_starts_its_own_wait(self) -> None:
        hold = hook.GATE_WAITER_MAX_HOLD_S
        t0 = 3_000_000.0
        self.assertEqual(self.waited(t0, 0, hold + 1), [])  # pid 77's wait is past the hold
        self.assertEqual(self.waited(t0, hold + 30, hold + 30, identity=lambda pid: "another-start"), [77],
                         "a new process with the same pid is a new waiter")
        self.assertEqual(self.waited(t0, hold + 60, hold + 60, identity=lambda pid: None), [77],
                         "an unreadable start honours the waiter")
        # the monotonic clock restarts at boot: a reading before the last sighting is a new span
        self.assertEqual(self.waited(0.0, 5, 5), [77])

    def test_an_unwritable_waiter_record_honours_every_waiter(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / hook.WAITER_SPANS_FILE).write_text("not json")
        self.assertEqual(self.waited(1.0, 0, 0), [77], "a torn record starts afresh")
        blocked = self.tmp / "file-not-dir"
        blocked.touch()
        notes: list[str] = []
        for t in (0.0, 10 * hook.GATE_WAITER_MAX_HOLD_S):
            self.assertEqual(hook.bounded_waiters(blocked / "state", [77], now=t, log=notes.append,
                                                  identity=lambda pid: "w"), [77], "no record: nothing to bound by")
        self.assertEqual(notes, [], "no 'listening anyway' note while still holding")

    def test_a_locked_waiter_record_honours_every_waiter_and_logs_nothing(self) -> None:
        self.assertEqual(self.waited(1.0, 0, hook.GATE_WAITER_MAX_HOLD_S + 1), [])
        fd = os.open(self.state / (Path(hook.WAITER_SPANS_FILE).stem + ".lock"), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        notes: list[str] = []
        with mock.patch.object(hook, "WAITER_SPANS_LOCK_S", 0.1):
            self.assertEqual(self.waited(1.0, hook.GATE_WAITER_MAX_HOLD_S + 60, hook.GATE_WAITER_MAX_HOLD_S + 60,
                                         notes=notes), [77])
        self.assertEqual(notes, [])

    def test_concurrent_waiter_readers_keep_each_others_spans(self) -> None:
        # Two gates read while a third holds the record's lock; the later-scheduled one sampled an older time.
        t0 = 4_000_000.0
        hold = hook.GATE_WAITER_MAX_HOLD_S
        self.waited(t0, 0, 0, (77,))
        fd = os.open(self.state / (Path(hook.WAITER_SPANS_FILE).stem + ".lock"), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        readers = [threading.Thread(target=self.waited, args=(t0, 90, 90, (88,))),
                   threading.Thread(target=self.waited, args=(t0, 60, 60, (77,)))]
        for reader in readers:
            reader.start()
        time.sleep(0.3)  # both block on the lock
        fcntl.flock(fd, fcntl.LOCK_UN)
        for reader in readers:
            reader.join(timeout=10)
        spans = json.loads((self.state / hook.WAITER_SPANS_FILE).read_text())
        self.assertEqual(sorted(spans), ["77:start-77", "88:start-88"], "neither update lost the other's span")
        self.assertEqual(spans["77:start-77"]["since"], t0, "77's wait kept its start")
        self.assertEqual(self.waited(t0, hold + 1, hold + 1, (77,)), [], "and is bounded from that start")

    def test_a_stale_waiter_reading_neither_restarts_nor_rewinds_a_span(self) -> None:
        t0 = 6_000_000.0
        hold = hook.GATE_WAITER_MAX_HOLD_S
        self.waited(t0, 0, 120)
        self.assertEqual(self.waited(t0, 90, 90), [77])  # a reading sampled before the last sighting
        spans = json.loads((self.state / hook.WAITER_SPANS_FILE).read_text())
        self.assertEqual(spans["77:start-77"], {"since": t0, "seen": t0 + 120})
        self.assertEqual(self.waited(t0, 150, hold + 1), [])
        # bounded_waiters reads the clock itself only once it holds the lock
        with mock.patch.object(hook.time, "monotonic", return_value=t0 + hold + 2):
            self.assertEqual(hook.bounded_waiters(self.state, [77], identity=lambda pid: "start-77"), [])

    def test_the_gate_stops_yielding_to_a_waiter_past_the_hold(self) -> None:
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        with mock.patch.object(hook, "lock_waiters", return_value=[77]), \
                mock.patch.object(hook, "process_identity", return_value="w"):
            self.assertEqual(sorted(hook.host_waiters(os.fspath(self.lock), self.state)), [77])
            spans = json.loads((self.state / hook.WAITER_SPANS_FILE).read_text())
            spans["77:w"]["since"] -= hook.GATE_WAITER_MAX_HOLD_S + 1
            (self.state / hook.WAITER_SPANS_FILE).write_text(json.dumps(spans))
            gate.waiting = {77}
            with contextlib.redirect_stdout(io.StringIO()) as out:
                why = gate.confirmed()
            self.assertNotEqual(why, "a fleet build is waiting for the host (pid 77)")
            self.assertIn("listening anyway", out.getvalue())
            self.assertEqual(gate.waiting, set())

    def test_lock_waiter_parser_counts_only_a_blocked_exclusive_lock(self) -> None:
        process_table = """\
101  hrtimer_nanosleep worker-current
102  pipe_read          /bin/cat /Users/Shared/cmux-build-fleet/host.lock
201  locks_lock_inode_wait /usr/local/bin/with-host-lock host.lock
202  lockf:             /usr/local/bin/with-host-lock host.lock (Darwin flock waiter)
301  hrtimer_nanosleep /usr/local/bin/with-host-lock host.lock (exclusive holder)
"""
        self.assertEqual(hook._blocked_lock_pids(process_table, {102, 201, 202, 301}), {201, 202})

    def test_a_free_host_or_our_shared_jobs_leave_the_listener_on(self) -> None:
        self.assertIsNone(self.held())
        self.hold(fcntl.LOCK_SH)  # a PR job's holder
        self.assertIsNone(self.held())

    def test_an_exclusive_holder_claims_the_host(self) -> None:
        self.hold(fcntl.LOCK_EX)
        self.assertEqual(self.held(), "a fleet build holds the host lock")

    @unittest.skipUnless(os.path.exists(hook.LSOF), "needs lsof")
    def test_a_yielding_holder_leaves_the_listener_on(self) -> None:
        self.hold(fcntl.LOCK_EX)
        (self.tmp / "host.lock.yield").write_text(f"{os.getpid()}\n")
        self.addCleanup(hook._gate_yield_verdict.clear)
        self.assertIsNone(self.held(), "a job preempts it, so the gate keeps listening")
        self.opener("fill-recipe.py")  # a child of this process with the lock open: the yielding build's own
        hook._gate_yield_verdict.clear()
        self.assertIsNone(self.held())
        worker = self.blocked_opener("with-host-lock.py")
        worker_pid = worker.pid
        with mock.patch.object(hook, "yielding_family", return_value={os.getpid()}):
            hook._gate_yield_verdict.clear()  # another fleet build on the lock: the marker yields nothing
            self.assertEqual(self.held(), "a fleet build holds the host lock")
            self.assertIn(worker_pid, hook.host_waiters(os.fspath(self.lock), self.tmp / "state"))
        self.assertEqual(self.held(), "a fleet build holds the host lock", "kept for GATE_WAITER_EVERY_S")
        hook._gate_yield_verdict.clear()
        self.assertIsNone(self.held(), "the opener is this process's child, so part of the yielding build")
        (self.tmp / "host.lock.yield").write_text("999999\n")  # no such process: a stale marker
        self.assertEqual(self.held(), "a fleet build holds the host lock")
        (self.tmp / "host.lock.yield").write_text("x" * 100)
        self.assertEqual(self.held(), "a fleet build holds the host lock")

    def test_a_reservation_claims_the_host(self) -> None:
        with mock.patch.object(hook, "reservation_refusal", return_value="host reserved by leo"):
            self.assertEqual(self.held(), "host reserved by leo")

    def test_real_lsof_sees_a_fleet_waiter_but_not_this_hook_in_flight(self) -> None:
        self.hold(fcntl.LOCK_SH)  # keep the build's LOCK_EX blocked while lsof and ps observe it
        fleet = self.blocked_opener("with-host-lock.py")
        ours = self.opener("glaeda-cmux-runner-hook-job-started.py")  # another runner's admission
        self.assertEqual(hook.host_waiters(os.fspath(self.lock), self.state), {fleet.pid})
        real = subprocess.run
        def gone(cmd, **kw):  # the fleet pid exits between lsof and ps: ps lists only the hook's own pid
            if cmd[0] == "/bin/ps":
                cmd = [*cmd[:-1], str(ours.pid)]
            return real(cmd, **kw)
        with mock.patch.object(hook.subprocess, "run", side_effect=gone):
            self.assertEqual(hook.host_waiters(os.fspath(self.lock), self.state), set())
        fleet.kill(); fleet.wait()
        self.assertEqual(hook.host_waiters(os.fspath(self.lock), self.state), set())
        self.assertIsNone(ours.poll())

    def gate(self, claims: list, busy: list | None = None):
        gate = hook.Gate(self.runner, os.fspath(self.lock), "", self.state)
        gate.child = mock.Mock(pid=1)
        gate.child.poll.return_value = None
        seq = iter(claims)
        gate.claimed = lambda: next(seq)
        gate.confirmed = lambda: "held"
        gate.reload = lambda: None
        busy_seq = iter(busy or [])
        gate.busy = lambda: next(busy_seq, False)
        stops: list[str] = []
        gate.stop = lambda why: (stops.append(why), setattr(gate, "stop_deadline", time.monotonic() + 60),
                                 setattr(gate, "held", why))
        return gate, stops

    def test_a_listener_just_started_keeps_its_first_poll(self) -> None:
        # 2026-10-12 03:46Z, cmux14: the root runner's listener created its broker session at 03:46:41 and the
        # gate stopped it at 03:46:44 (another runner's job made it wait for root). Its first message poll was
        # cancelled with no job seen, but GitHub had already given it the queued root job, which then failed at
        # 03:49:28: "The job was not acquired by Runner of type self-hosted even after multiple attempts".
        full = "all 5 capacity units on this mini are taken"
        gate, stops = self.gate([full] * 8)
        with mock.patch.object(hook.subprocess, "Popen", return_value=gate.child):
            gate.start()
        for _ in range(3):
            gate.step()
        self.assertEqual(stops, [], "a new listener's first poll may already carry a job: it is not stopped")
        gate.listening_since -= getattr(hook, "GATE_FIRST_POLL_S", 30.0)
        gate.step()
        self.assertEqual(stops, ["held"], "once its first poll has had time to deliver, the hold stops it")

    def test_a_listener_keeps_a_whole_broker_long_poll(self) -> None:
        # 2026-10-21, cmux7s: the gate started both root listeners at 14:59:10 (sessions at 14:59:11) and stopped
        # them at 14:59:40, the moment the first-poll hold ran out, for a fleet waiter ("swift-test waits here
        # for units"). GitHub had queued mobile-core-package (run 39621070398) at 14:59:36, 25 s into that first
        # message poll, and handed it to one of those sessions; the SIGINT cancelled the poll before the message
        # came, and the job failed at 15:04 "not acquired by Runner of type self-hosted". The broker holds a
        # message poll open for up to 50 s, so a job given to a new session can arrive any time in that span.
        full = "cmux7s-mac-mini-glaeda-4 swift-test (run 39589203114) waits here for units"
        gate, stops = self.gate([full] * 8)
        with mock.patch.object(hook.subprocess, "Popen", return_value=gate.child):
            gate.start()
        gate.listening_since -= 30.0  # 14:59:40
        for _ in range(3):
            gate.step()
        self.assertEqual(stops, [], "30 s into its first poll a listener may still be getting a job")
        gate.listening_since -= hook.GATE_FIRST_POLL_S  # past the whole first poll
        gate.step()
        self.assertEqual(stops, ["held"])
        self.assertGreater(hook.GATE_FIRST_POLL_S, hook.BROKER_POLL_S)

    def units(self, held: int) -> Path:
        """A capacity ledger an admission has used, with its first `held` units taken."""
        capacity = Path(tempfile.mkdtemp(dir=self.tmp))
        (capacity / "admission.lock").touch()  # every admission creates it
        for slot in range(held):
            fd = os.open(capacity / f"unit-{slot}", os.O_RDONLY | os.O_CREAT, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.addCleanup(os.close, fd)
        return capacity

    def runner_hook(self, units: int | None) -> None:
        """This runner's job-started hook as glaeda-cmux-runner writes it, with or without --capacity-units."""
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(exist_ok=True)
        scope = "--allowed-owner manaflow-ai" + (f" --capacity-units {units}" if units is not None else "")
        script.write_text(f"#!/bin/bash\nexec /usr/bin/python3 /hook job-started {scope}\n")

    def full_gate(self, capacity: Path):
        return hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state,
                         capacity_dir=capacity)

    def test_a_mini_with_every_unit_taken_claims_the_host(self) -> None:
        self.assertIsNone(hook.mini_full(None, 2))
        self.assertIsNone(hook.mini_full(self.units(2), 0), "a single-runner mini has no units")
        self.assertIsNone(hook.mini_full(self.units(2), 3))
        self.assertIsNone(hook.mini_full(self.tmp / "unused", 2), "no admission yet: nothing is taken")
        capacity = self.units(2)
        self.assertEqual(hook.mini_full(capacity, 2), "all 2 capacity units on this mini are taken")
        gate = self.full_gate(capacity)
        self.assertIsNone(gate.claimed(), "a runner without a hook script never counts as full")
        self.runner_hook(None)
        self.assertIsNone(gate.claimed(), "nor one whose hook takes no units")
        self.runner_hook(2)
        self.assertEqual(hook.runner_units(self.runner), 2)
        self.assertEqual(gate.claimed(), "all 2 capacity units on this mini are taken")
        self.assertEqual(gate.confirmed(), "all 2 capacity units on this mini are taken")
        # a re-apply that raised this runner's units: the new free units count on the next look (a root runner's
        # next job may be a 5-core compile)
        self.runner_hook(7)
        self.assertIsNone(gate.claimed())
        self.runner_hook(2)
        self.hold(fcntl.LOCK_EX)
        self.assertEqual(gate.claimed(), "a fleet build holds the host lock", "the fleet's claim comes first")

    def test_a_saturated_mini_claims_the_host_with_hysteresis(self) -> None:
        pause, resume = 2.0, 1.5
        for name, value in (("GATE_LOAD_PAUSE", pause), ("GATE_LOAD_RESUME", resume),
                            ("GATE_CPU_PAUSE", 80.0), ("GATE_CPU_RESUME", 70.0), ("GATE_CPU_EVERY_S", 0.0)):
            patcher = mock.patch.object(hook, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertIsNone(hook.mini_saturated(False, load=pause * 14 - 0.1, cores=14))
        why = hook.mini_saturated(False, load=pause * 14, cores=14)
        self.assertTrue(why and why.startswith(hook.SATURATED), why)
        self.assertEqual(why, hook.mini_saturated(False, load=pause * 14 + 30, cores=14), "stable text, logged once")
        self.assertIsNotNone(hook.mini_saturated(True, load=resume * 14, cores=14))
        self.assertIsNone(hook.mini_saturated(True, load=resume * 14 - 0.1, cores=14))
        self.assertIsNone(hook.mini_saturated(False, load=99, cores=0), "no core count: never saturated")
        with mock.patch.object(hook.os, "getloadavg", side_effect=OSError):
            self.assertIsNone(hook.mini_saturated(False), "an unreadable load never saturates")

        busy = [hook.GATE_CPU_PAUSE]
        patcher = mock.patch.object(hook, "host_io", side_effect=lambda: (0.0, busy[0]))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(hook, "memory_pressure_level", return_value=1)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Full and saturated: the CPU reason wins, so the resume mark applies.
        self.runner_hook(2)
        gate = self.full_gate(self.units(2))
        self.assertTrue(gate.claimed().startswith(hook.SATURATED))
        self.assertTrue(gate.confirmed().startswith(hook.SATURATED))
        busy[0] = (hook.GATE_CPU_PAUSE + hook.GATE_CPU_RESUME) / 2
        self.assertTrue(gate.claimed().startswith(hook.SATURATED), "between the marks a held gate stays held")
        busy[0] = hook.GATE_CPU_RESUME - 1
        self.assertEqual(gate.claimed(), "all 2 capacity units on this mini are taken", "then the full reason")
        self.hold(fcntl.LOCK_EX)
        busy[0] = hook.GATE_CPU_PAUSE
        self.assertEqual(gate.claimed(), "a fleet build holds the host lock", "the fleet's claim comes first")

    def test_the_listener_gate_pauses_at_85_and_resumes_below_80_by_default(self) -> None:
        """With units that are cores, admission stops near the core count, and the gate is the margin for jobs
        above their estimate: by default an idle listener stops at 85% sampled CPU and listens again below 80%.
        The hook is loaded fresh without the GLAEDA_RUNNER_GATE_CPU_* overrides this module sets."""
        clean = {k: v for k, v in os.environ.items() if not k.startswith("GLAEDA_RUNNER_GATE_CPU_")}
        with mock.patch.dict(os.environ, clean, clear=True):
            fresh = load("glaeda_cmux_runner_hook_gate_defaults", HOOK)
        self.addCleanup(sys.modules.pop, "glaeda_cmux_runner_hook_gate_defaults", None)
        self.assertEqual((fresh.GATE_CPU_PAUSE, fresh.GATE_CPU_RESUME), (85.0, 80.0))
        busy = [0.0]
        gate = fresh.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        with mock.patch.object(fresh, "GATE_CPU_EVERY_S", 0.0), \
                mock.patch.object(fresh, "host_io", side_effect=lambda: (0.0, busy[0])), \
                mock.patch.object(fresh, "memory_pressure_level", return_value=1), \
                mock.patch.object(fresh, "thermal_pressure_level", return_value=0):
            for cpu, held in ((84.9, False), (85.0, True), (82.0, True), (80.0, True), (79.9, False), (84.0, False),
                              (90.0, True)):
                busy[0] = cpu
                why = gate.claimed()
                self.assertEqual(why is not None, held, f"{cpu}% CPU: {why}")
                if held:
                    self.assertTrue(why.startswith(fresh.SATURATED), why)
                    self.assertIn("listening resumes below 80% CPU", why)

    def test_host_pressure_gate_uses_cpu_and_memory_not_load_average(self) -> None:
        with mock.patch.object(hook, "GATE_CPU_PAUSE", 80.0), mock.patch.object(hook, "GATE_CPU_RESUME", 70.0):
            self.assertIsNone(hook.host_saturated(False, cpu_busy=59.0, memory_pressure=1))
            self.assertTrue(hook.host_saturated(False, cpu_busy=80.0, memory_pressure=1).startswith(hook.SATURATED))
            self.assertTrue(hook.host_saturated(False, cpu_busy=20.0, memory_pressure=2).startswith(hook.SATURATED))
            self.assertIsNotNone(hook.host_saturated(True, cpu_busy=70.0, memory_pressure=1))
            self.assertIsNone(hook.host_saturated(True, cpu_busy=69.9, memory_pressure=1))
            self.assertIsNone(hook.host_saturated(False, cpu_busy=None, memory_pressure=None))

    def test_gate_lines_carry_a_utc_stamp_readers_still_match(self) -> None:
        out = io.StringIO()
        with mock.patch.object(hook.time, "gmtime", return_value=time.gmtime(1790379071)), \
                contextlib.redirect_stdout(out):
            hook.gate_log("holding the listener off: all 4 capacity units on this mini are taken")
        line = out.getvalue()
        self.assertEqual(line, "2026-09-25 23:31:11Z: glaeda-cmux-runner-gate: holding the listener off: "
                               "all 4 capacity units on this mini are taken\n")
        # glaeda-cmux-runner reads the held state by substring, so the stamp keeps it working
        self.assertIn(cr.GATE_HELD, line)

    def test_a_cpu_hold_ends_after_the_limit_until_pressure_falls(self) -> None:
        for name, value in (("GATE_CPU_PAUSE", 80.0), ("GATE_CPU_RESUME", 70.0), ("GATE_CPU_EVERY_S", 0.0),
                            ("GATE_LOAD_MAX_HOLD_S", 60.0)):
            patcher = mock.patch.object(hook, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        busy, clock = [80.0], [1000.0]
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        with mock.patch.object(hook, "host_io", side_effect=lambda: (0.0, busy[0])), \
                mock.patch.object(hook, "memory_pressure_level", return_value=1), \
                mock.patch.object(hook.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(hook, "gate_log") as log:
            self.assertIsNotNone(gate.claimed())
            clock[0] += 59
            self.assertIsNotNone(gate.claimed())
            clock[0] += 2
            self.assertIsNone(gate.claimed(), "past the limit the mini listens anyway")
            self.assertIn("longer than any compile", log.call_args[0][0])
            clock[0] += 600
            self.assertIsNone(gate.claimed(), "still waived while the load stays up")
            busy[0] = 60.0
            self.assertIsNone(gate.claimed())
            busy[0] = 80.0
            self.assertIsNotNone(gate.claimed(), "a fresh overload holds again")
            # A fleet claim in between restarts the clock: the limit times a load pause, not the fleet's.
            clock[0] += 50
            fd = self.hold(fcntl.LOCK_EX)
            self.assertEqual(gate.claimed(), "a fleet build holds the host lock")
            fcntl.flock(fd, fcntl.LOCK_UN)
            clock[0] += 50
            self.assertIsNotNone(gate.claimed(), "the 100 s since the first look are not all load hold")
            self.assertIsNotNone(gate.claimed())

    def test_a_saturated_mini_stops_an_idle_listener_through_step(self) -> None:
        for name, value in (("GATE_CPU_PAUSE", 80.0), ("GATE_CPU_RESUME", 70.0), ("GATE_CPU_EVERY_S", 0.0)):
            patcher = mock.patch.object(hook, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        busy = [80.0]
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        with mock.patch.object(hook, "host_io", side_effect=lambda: (0.0, busy[0])), \
                mock.patch.object(hook, "memory_pressure_level", return_value=1), \
                mock.patch.object(gate, "start") as start, mock.patch.object(gate, "stop") as stop, \
                mock.patch.object(gate, "busy", return_value=False), mock.patch.object(gate, "reload"):
            gate.child = mock.Mock(poll=mock.Mock(return_value=None))
            gate.step()
            gate.step()
            stop.assert_called_once()
            self.assertTrue(stop.call_args[0][0].startswith(hook.SATURATED))
            gate.child, gate.held = None, stop.call_args[0][0]
            busy[0] = 75.0  # below pause, above resume: stays off
            for _ in range(3):
                gate.step()
            start.assert_not_called()
            busy[0] = 60.0
            for _ in range(hook.GATE_CONFIRM):
                gate.step()
            start.assert_called_once()

    def test_the_job_record_class_uses_the_workflow_like_admission(self) -> None:
        payload = self.tmp / "event.json"
        payload.write_text(json.dumps({"workflow": ".github/workflows/test-e2e.yml"}))
        env = {"GITHUB_JOB": "build", "GITHUB_EVENT_PATH": os.fspath(payload)}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("GITHUB_WORKFLOW_REF", None)
            self.assertEqual(hook.job_workflow(), "test-e2e.yml", "a dispatch payload names its workflow")
            self.assertEqual(hook.job_meta("manaflow-ai/cmux", hook.DEFAULT_HOME_REPO)["class"], "compile-gui")
            os.environ["GITHUB_WORKFLOW_REF"] = "manaflow-ai/cmux/.github/workflows/cmux-tui.yml@refs/heads/main"
            self.assertEqual(hook.job_meta("manaflow-ai/cmux", hook.DEFAULT_HOME_REPO)["class"], "isolated")
        payload.write_text(json.dumps({"pull_request": {}}))
        with mock.patch.dict(os.environ, env):
            os.environ.pop("GITHUB_WORKFLOW_REF", None)
            self.assertEqual(hook.job_workflow(), "")

    def test_dev_compile_capacity_wait_is_bounded_for_push_and_daily(self) -> None:
        self.assertEqual(hook.DEV_COMPILE_CAPACITY_WAIT_S, 60.0)
        push = {"repository": CMUX, "ref": "refs/heads/feat-cmux-next"}
        self.assertEqual(
            hook.dev_compile_capacity_wait("manaflow-ai/cmux", hook.DEFAULT_HOME_REPO, "push",
                                           "refs/heads/feat-cmux-next", push, "cmux-scheme-compile",
                                           "cmux-next.yml"),
            hook.DEV_COMPILE_CAPACITY_WAIT_S)
        daily = {"repository": CMUX, "inputs": {"dev_daily": "true"}}
        self.assertEqual(
            hook.dev_compile_capacity_wait("manaflow-ai/cmux", hook.DEFAULT_HOME_REPO, "workflow_dispatch",
                                           "refs/heads/feat-cmux-next", daily, "cmux-scheme-compile",
                                           "cmux-next.yml"),
            hook.DEV_COMPILE_CAPACITY_WAIT_S)

    def test_dev_daily_publisher_jobs_wait_a_bounded_time(self) -> None:
        """cmux-next-dev-daily.yml's Mac jobs have 15 and 20 minute timeouts that count the wait in runner setup
        (run 40466210697 timed out after 19 min there), so each is refused after the bound, whatever its event."""
        for job in ("verify", "tip-dmg-build", "anything-new"):
            for event_name in ("workflow_dispatch", "push"):
                with self.subTest(job=job, event=event_name):
                    self.assertEqual(hook.dev_compile_capacity_wait(
                        "manaflow-ai/cmux", hook.DEFAULT_HOME_REPO, event_name, "refs/heads/main",
                        {"repository": CMUX}, job, "cmux-next-dev-daily.yml"), hook.DEV_COMPILE_CAPACITY_WAIT_S)
        self.assertIsNone(hook.dev_compile_capacity_wait(
            "someone/else", hook.DEFAULT_HOME_REPO, "workflow_dispatch", "refs/heads/main", {}, "verify",
            "cmux-next-dev-daily.yml"))

    def test_the_dev_bound_holds_over_a_longer_explicit_wait(self) -> None:
        bound = hook.DEV_COMPILE_CAPACITY_WAIT_S
        self.assertEqual(hook.bounded_wait(None, bound), bound)
        self.assertEqual(hook.bounded_wait(bound * 10, bound), bound)
        self.assertEqual(hook.bounded_wait(5.0, bound), 5.0)
        self.assertEqual(hook.bounded_wait(bound * 10, None), bound * 10)
        self.assertIsNone(hook.bounded_wait(None, None))
        # The worst case (the last try's admission.lock wait) stays far inside the DEV daily's 15-minute verify timeout.
        self.assertLessEqual(hook.DEV_BOUND_WORST_S, 2 * 60)

    def test_every_job_on_the_freshness_slot_waits_a_bounded_time(self) -> None:
        """A push's release-compile on the slot is not a DEV compile (dev_compile_capacity_wait is None), but the
        slot still bounds its wait, so a mini that fills while it waits refuses it back to placement. An explicit
        longer wait is clamped and a shorter one kept; off the slot, waits are unchanged."""
        bound = hook.DEV_COMPILE_CAPACITY_WAIT_S
        self.assertEqual(hook.slot_capacity_wait(None, True), bound)
        self.assertEqual(hook.slot_capacity_wait(bound * 2, True), bound)
        self.assertEqual(hook.slot_capacity_wait(5.0, True), 5.0)
        self.assertIsNone(hook.slot_capacity_wait(None, False))
        self.assertEqual(hook.slot_capacity_wait(bound * 2, False), bound * 2)

    def test_the_nightly_next_app_build_waits_a_bounded_time_on_attempt_1(self) -> None:
        """NEXT's nightly app build is refused after NIGHTLY_CAPACITY_WAIT_S on attempt 1, so the owned-pool rescue's
        re-run (attempt 2, the picker's Blacksmith label) builds it within minutes. Main's nightly, other nightly
        jobs, later attempts and non-nightly tiers keep their waits."""
        bound = hook.NIGHTLY_CAPACITY_WAIT_S
        self.assertEqual(bound, 300.0)
        # the refusal must come within cmux owned_pool_rescue.py REFUSAL_SECONDS (360 s), or the rescue reads it as an
        # ordinary failure and never re-runs it off the fleet
        self.assertLess(hook.NIGHTLY_BOUND_WORST_S, 360)
        args = (hook.TIER_NIGHTLY, "push", "refs/heads/nightly-next", "build-nightly-app", "nightly.yml")
        self.assertEqual(hook.nightly_capacity_wait(*args, "1"), bound)
        for attempt in ("2", "", None, " 1", "01"):  # only an exact attempt 1; anything else keeps its wait
            with self.subTest(attempt=attempt):
                self.assertIsNone(hook.nightly_capacity_wait(*args, attempt))
        for index, value in ((0, hook.TIER_DEV), (0, ""), (1, "workflow_dispatch"), (1, "schedule"), (1, None),
                             (2, "refs/heads/main"), (3, "build-sign-notarize-nightly"), (4, "cmux-next.yml")):
            changed = list(args)
            changed[index] = value
            with self.subTest(changed=changed):
                self.assertIsNone(hook.nightly_capacity_wait(*changed, "1"))
        # composes with an explicit wait the way the DEV bound does
        self.assertEqual(hook.bounded_wait(None, hook.nightly_capacity_wait(*args, "1")), bound)
        self.assertEqual(hook.bounded_wait(5.0, hook.nightly_capacity_wait(*args, "1")), 5.0)
        # job_tier() is the gate: a pull request's copy of nightly.yml is no nightly job, so it is never bounded here
        ref = "refs/heads/nightly-next"
        tier = hook.job_tier("push", ref, "manaflow-ai/cmux", {"ref": ref, "repository": CMUX},
                             f"manaflow-ai/cmux/.github/workflows/nightly.yml@{ref}", "build-nightly-app")
        self.assertEqual(hook.nightly_capacity_wait(tier, "push", ref, "build-nightly-app", "nightly.yml", "1"), bound)
        untrusted = hook.job_tier("pull_request", "refs/pull/1/merge", "manaflow-ai/cmux", {"repository": CMUX},
                                  "manaflow-ai/cmux/.github/workflows/nightly.yml@refs/pull/1/merge",
                                  "build-nightly-app")
        self.assertIsNone(hook.nightly_capacity_wait(untrusted, "push", ref, "build-nightly-app", "nightly.yml", "1"))

    def test_other_capacity_waits_remain_unbounded(self) -> None:
        event = {"repository": CMUX, "ref": "refs/heads/feat-cmux-next"}
        for job, workflow, event_name in (("cmux-scheme-compile", "cmux-next.yml", "pull_request"),
                                          ("generated-files", "cmux-next.yml", "push"),
                                          ("cmux-scheme-compile", "other.yml", "push")):
            with self.subTest(job=job, workflow=workflow, event=event_name):
                self.assertIsNone(hook.dev_compile_capacity_wait(
                    "manaflow-ai/cmux", hook.DEFAULT_HOME_REPO, event_name,
                    "refs/heads/feat-cmux-next", event, job, workflow))

    def test_runner_scope_reads_the_hook_and_the_runner_directory(self) -> None:
        self.assertEqual(hook.runner_scope(self.runner), hook.RunnerScope(), "no hook script: no units, no root")
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(exist_ok=True)
        script.write_text("exec python3 /hook job-started --capacity-units 5 --compile-slots 2 "
                          "--canonical-roots 2 --instance 1\n")
        self.assertEqual(hook.runner_scope(self.runner), hook.RunnerScope(5, 2, 2, True))
        script.write_text("exec python3 /hook job-started --capacity-units 4 --canonical-roots 2 --instance 3\n")
        self.assertEqual(hook.runner_scope(self.runner), hook.RunnerScope(4, 1, 2, False))
        # one root: the hook passes no --instance, so the directory suffix names it (actions-runner-glaeda-K)
        script.write_text("exec python3 /hook job-started --capacity-units 4\n")
        self.assertEqual(hook.runner_scope(self.runner), hook.RunnerScope(4, 1, 1, True))
        side = self.tmp / "actions-runner-glaeda-2"
        (side / "glaeda-hooks").mkdir(parents=True)
        (side / hook.RUNNER_HOOK_SCRIPT).write_text(script.read_text())
        self.assertEqual(hook.runner_scope(side), hook.RunnerScope(4, 1, 1, False))
        self.assertEqual(hook.runner_units(side), 4)
        script.write_text("exec python3 /hook job-started --capacity-units 5 --compile-slots 2 "
                          "--canonical-roots 2 --instance 4 --gui-runner\n")
        self.assertEqual(hook.runner_scope(self.runner), hook.RunnerScope(5, 2, 2, False, True))
        script.write_text("exec python3 /hook job-started --capacity-units 5 --instance 0 --gui-runner\n")
        self.assertEqual(hook.runner_scope(self.runner), hook.RunnerScope(5, 1, 1, False, True),
                         "a gui runner is never a root runner, whatever its instance")
        script.write_text("exec python3 /hook job-started --capacity-units 5 --instance 0 --gui-runner --gui-token gui-vm\n")
        self.assertEqual(hook.runner_scope(self.runner), hook.RunnerScope(5, 1, 1, False, True, 0, "gui-vm"))

    def test_a_gui_runner_holds_while_the_console_is_locked(self) -> None:
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("exec python3 /hook job-started --capacity-units 5 --instance 2 --gui-runner\n")
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        why = "console: the console session (cmux) is screen-locked"
        with mock.patch.object(hook, "console_refusal", return_value=why) as read:
            self.assertEqual(gate.claimed(), why)
            self.assertEqual(gate.claimed(), why)
            self.assertEqual(read.call_count, 1, "read at most every GATE_CONSOLE_EVERY_S")
            self.assertEqual(gate.confirmed(), why)
            self.assertEqual(read.call_count, 2, "the look right before a stop is fresh")
        with mock.patch.object(hook, "console_refusal", return_value=None):
            self.assertEqual(gate.claimed(), why, "the cached reading holds until the next read")
            self.assertIsNone(gate.confirmed())
        script.write_text("exec python3 /hook job-started --capacity-units 5 --instance 3 --session-runner\n")
        with mock.patch.object(hook, "console_refusal", return_value=why):
            self.assertEqual(hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"),
                                       self.state).locked(fresh=True), why, "a session runner's jobs need the session")
        script.write_text("exec python3 /hook job-started --capacity-units 5 --instance 0\n")
        with mock.patch.object(hook, "console_refusal", return_value=why) as read:
            self.assertIsNone(hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"),
                                        self.state).confirmed(), "a root runner's compiles need no console")
            read.assert_not_called()

    def test_a_require_eligible_runner_holds_while_its_node_would_be_refused(self) -> None:
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("exec /usr/bin/python3 /hook job-started --allowed-owner manaflow-ai --require-eligible "
                          "--fleet-class m6-16 --toolchain-xcode /Applications/Xcode_26.6.app --instance 1\n")
        self.assertEqual(hook.eligibility_scope(self.runner), ("m6-16", "/Applications/Xcode_26.6.app"))
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        for name in ("saturated", "hot"):  # this host's own load and heat are not what this test is about
            patch = mock.patch.object(gate, name, return_value=None)
            patch.start()
            self.addCleanup(patch.stop)
        why = "node not eligible (rust toolchain 1.99.1-aarch64-apple-darwin is not installed)"

        def settle() -> None:
            if gate.eligible_look is not None:
                gate.eligible_look.join(5)

        with mock.patch.object(hook, "eligibility_refusal", return_value=why) as look:
            self.assertIsNone(gate.claimed(), "the first poll starts the look")
            settle()
            self.assertIsNone(gate.claimed(), "a first refusal holds only once a recheck agrees")
            settle()
            self.assertEqual(look.call_count, 1, "the recheck waits GATE_ELIGIBLE_RECHECK_S")
            gate.eligible_at -= hook.GATE_ELIGIBLE_RECHECK_S
            gate.claimed()
            settle()
            self.assertEqual(gate.claimed(), f"ineligible: {why}")
            self.assertEqual(gate.confirmed(), f"ineligible: {why}")
            self.assertEqual(look.call_count, 2, "looked at most every GATE_ELIGIBLE_EVERY_S once held")
            look.assert_called_with("m6-16", "/Applications/Xcode_26.6.app")
        with mock.patch.dict(os.environ, {hook.GATE_INELIGIBLE_ENV: why}):
            reloaded = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
            self.assertNotIn(hook.GATE_INELIGIBLE_ENV, os.environ)
        self.assertEqual(reloaded.eligible_why, why, "a reload keeps the held answer")
        with mock.patch.object(hook, "eligibility_refusal", return_value="node not eligible (state x)"):
            fresh = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
            fresh.ineligible()
            fresh.eligible_look.join(5)
            fresh.eligible_at -= hook.GATE_ELIGIBLE_RECHECK_S
            with mock.patch.object(hook, "eligibility_refusal", return_value=None):
                fresh.ineligible()
                fresh.eligible_look.join(5)
            self.assertIsNone(fresh.ineligible(), "a refusal the recheck does not repeat never holds")
        script.write_text("exec /usr/bin/python3 /hook job-started --require-eligible --fleet-class m6-16 "
                          "--toolchain-xcode '/Applications/Xcode 26.app'\n")
        self.assertEqual(hook.eligibility_scope(self.runner), ("m6-16", "/Applications/Xcode 26.app"))
        script.write_text("exec /usr/bin/python3 /hook job-started --allowed-owner manaflow-ai --require-eligible "
                          "--fleet-class m6-16 --toolchain-xcode /Applications/Xcode_26.6.app --instance 1\n")
        gate.eligible_at -= hook.GATE_ELIGIBLE_EVERY_S
        with mock.patch.object(hook, "eligibility_refusal", return_value=None):
            gate.claimed()
            settle()
            self.assertIsNone(gate.claimed(), "eligible again: listening again")
        gate.eligible_at -= hook.GATE_ELIGIBLE_EVERY_S
        with mock.patch.object(hook, "eligibility_refusal", side_effect=RuntimeError("probe")):
            gate.claimed()
            settle()
            self.assertIsNone(gate.claimed(), "a look that crashes never holds the runner")
        script.write_text("exec /usr/bin/python3 /hook job-started --allowed-owner manaflow-ai --fleet-class m6-16 "
                          "--instance 1\n")
        self.assertIsNone(hook.eligibility_scope(self.runner))
        with mock.patch.object(hook, "eligibility_refusal", return_value=why) as look:
            gate.eligible_at -= hook.GATE_ELIGIBLE_EVERY_S
            self.assertIsNone(gate.claimed(), "no --require-eligible: never held for eligibility")
            look.assert_not_called()

    def rust_bin(self) -> Path:
        """A rustup bin dir as the minis had it on 2026-10-26: rustc and cargo link to a rustup that is gone. The gate's
        own PATH (run.sh's, so the jobs') reaches it."""
        bin_dir = self.tmp / "cargo" / "bin"
        bin_dir.mkdir(parents=True)
        for tool in ("rustc", "cargo"):
            (bin_dir / tool).symlink_to("rustup")
        for patch in (mock.patch.object(hook, "TOOLCHAIN_GATE", True),
                      mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{self.tmp / 'no-rust-here'}",
                                                   "CARGO_HOME": os.fspath(bin_dir.parent)})):
            patch.start()
            self.addCleanup(patch.stop)
        return bin_dir

    def test_toolchain_missing_names_each_tool_and_why(self) -> None:
        bin_dir = self.rust_bin()
        cargo_home = os.fspath(bin_dir.parent)
        self.assertEqual(hook.toolchain_missing({"PATH": f"{bin_dir}:/nonexistent", "CARGO_HOME": cargo_home}),
                         f"node not eligible (Rust toolchain missing: rustup is not installed (not on the job PATH "
                         f"{bin_dir}:/nonexistent, not in {bin_dir}); {bin_dir / 'rustc'} (a link to rustup, which is "
                         f"gone); {bin_dir / 'cargo'} (a link to rustup, which is gone))")
        make_executable(bin_dir / "rustup", "#!/bin/sh\nexit 0\n")
        self.assertIsNone(hook.toolchain_missing({"PATH": os.fspath(bin_dir)}), "the links resolve again")
        self.assertEqual(hook.toolchain_missing({"PATH": "/nonexistent", "CARGO_HOME": cargo_home}),
                         "node not eligible (Rust toolchain missing: " + "; ".join(
                             f"{tool} is not on the job PATH /nonexistent ({bin_dir / tool} is there)"
                             for tool in hook.RUST_TOOLS) + ")")
        (bin_dir / "rustup").unlink()
        brew = self.tmp / "brew"  # the minis' jobs run Homebrew's rustup proxies (/opt/homebrew/bin first)
        brew.mkdir()
        for tool in hook.RUST_TOOLS:
            make_executable(brew / tool, "#!/bin/sh\nexit 0\n")
        self.assertIsNone(hook.toolchain_missing({"PATH": f"{brew}:/usr/bin", "CARGO_HOME": cargo_home}),
                          "a toolchain the job PATH finds elsewhere runs, whatever ~/.cargo/bin holds")
        (brew / "rustc").unlink()
        (brew / "rustc").symlink_to(self.tmp / "Cellar" / "rustup" / "bin" / "rustc")
        self.assertIn(f"{brew / 'rustc'} (a link to", hook.toolchain_missing({"PATH": os.fspath(brew)}) or "",
                      "a dangling link on the PATH is named first")
        with mock.patch.object(hook, "TOOLCHAIN_GATE", False):
            self.assertIsNone(hook.toolchain_missing())

    def test_the_workload_path_is_the_runner_agents(self) -> None:
        self.assertEqual(hook.WORKLOAD_DEFAULT, cr.WORKLOAD_PATH)
        self.assertEqual(hook.workload_env()["PATH"], hook.WORKLOAD_PATH)

    def test_a_missing_rust_toolchain_holds_a_require_eligible_runner_at_once(self) -> None:
        """2026-10-26: with ~/.cargo/bin/rustup gone, a listening runner took every job its label brought until the
        gate's next eligibility look (up to GATE_ELIGIBLE_EVERY_S) and its recheck (GATE_ELIGIBLE_RECHECK_S) agreed.
        A missing binary is no slow probe: the gate holds on the poll that sees it, and lets go on the one after
        it is back. The PATH is the gate's own, which run.sh and every job inherit, never the runner's .path."""
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("exec /usr/bin/python3 /hook job-started --require-eligible --fleet-class m4pro-48 "
                          "--toolchain-xcode /Applications/Xcode_26.6.app --instance 1\n")
        bin_dir = self.rust_bin()
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        for name in ("saturated", "hot"):
            patch = mock.patch.object(gate, name, return_value=None)
            patch.start()
            self.addCleanup(patch.stop)
        gate.eligible_at = time.monotonic()  # a look just ran and said eligible: the next is 300 s away
        with mock.patch.object(hook, "eligibility_refusal", return_value=None) as look:
            why = gate.claimed()
            self.assertEqual(why, f"ineligible: {hook.toolchain_missing()}")
            self.assertIn("rustup is not installed", why)
            self.assertEqual(gate.confirmed(), why, "the look right before a stop holds too")
            look.assert_not_called()
            make_executable(bin_dir / "rustup", "#!/bin/sh\nexit 0\n")
            self.assertIsNone(gate.claimed(), "listening again on the next poll")
            # every mini's .path drifted to this on re-registration; nothing on a glaeda runner reads it
            (self.runner / ".path").write_text("/usr/bin:/bin:/usr/sbin:/sbin\n")
            self.assertIsNone(gate.claimed(), "a .path without the toolchain never holds a runner whose PATH has it")
        script.write_text("exec /usr/bin/python3 /hook job-started --fleet-class m4pro-48 --instance 1\n")
        (bin_dir / "rustup").unlink()
        self.assertIsNone(gate.claimed(), "no --require-eligible: never held for the toolchain")

    def test_a_missing_toolchain_never_outlives_its_return(self) -> None:
        """The threaded look's toolchain_refusal also refuses a missing toolchain, but the per-poll check owns that
        answer: a look that meets it changes nothing (a held refusal stands, none is stored), so a runner listens
        again on the first poll after rustup is back, unless something else holds it; a reload never carries it."""
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("exec /usr/bin/python3 /hook job-started --require-eligible --fleet-class m4pro-48\n")
        bin_dir = self.rust_bin()
        why = f"node not eligible ({hook.TOOLCHAIN_MISSING}: rustup is not installed)"
        with mock.patch.dict(os.environ, {hook.GATE_INELIGIBLE_ENV: why}):
            gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        self.assertIsNone(gate.eligible_why, "a reload drops a carried missing-toolchain answer")
        for name in ("saturated", "hot"):
            patch = mock.patch.object(gate, name, return_value=None)
            patch.start()
            self.addCleanup(patch.stop)
        release = threading.Event()

        def slow_look(*_args):
            release.wait(5)  # the look is still running when rustup comes back
            return why

        with mock.patch.object(hook, "eligibility_refusal", side_effect=slow_look):
            self.assertIn(hook.TOOLCHAIN_MISSING, gate.claimed() or "")
            gate.eligible_at -= hook.GATE_ELIGIBLE_EVERY_S
            make_executable(bin_dir / "rustup", "#!/bin/sh\nexit 0\n")
            self.assertIsNone(gate.claimed(), "listening again on the first poll after it is back")
            self.assertTrue(gate.eligible_look.is_alive())
            release.set()
            gate.eligible_look.join(5)
            self.assertIsNone(gate.eligible_why, "the look's late answer is never stored, so it cannot hold 300 s")
            self.assertIsNone(gate.eligible_new, "no recheck queued for it either")
            self.assertIsNone(gate.claimed())
        held = "node not eligible (state x, build: stale receipt)"
        gate.eligible_why = held  # another refusal was already holding the runner
        gate.eligible_at -= hook.GATE_ELIGIBLE_EVERY_S
        with mock.patch.object(hook, "eligibility_refusal", return_value=why):
            gate.claimed()
            gate.eligible_look.join(5)
        self.assertEqual(gate.eligible_why, held, "a look that meets a missing toolchain keeps the held refusal")
        self.assertEqual(gate.claimed(), f"ineligible: {held}")

    def test_the_listener_starts_without_sdkroot_and_on_the_pool_xcode(self) -> None:
        """cmux21: the gate ran under the /usr/bin/python3 xcrun shim, which put SDKROOT in its env, and run.sh
        inherited it, so every job built against the Command Line Tools SDK."""
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("exec /usr/bin/python3 /hook job-started --require-eligible --fleet-class m6-16 "
                          "--toolchain-xcode /Applications/Xcode_26.6.app --instance 1\n")
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        leaked = {"SDKROOT": "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk", "TOOLCHAINS": "swift"}
        with mock.patch.dict(os.environ, leaked), mock.patch.object(hook.subprocess, "Popen") as popen:
            gate.start()
        env = popen.call_args.kwargs["env"]
        self.assertNotIn("SDKROOT", env)
        self.assertNotIn("TOOLCHAINS", env)
        self.assertEqual(env["DEVELOPER_DIR"], "/Applications/Xcode_26.6.app/Contents/Developer")
        self.assertEqual(env["PATH"], os.environ["PATH"])
        self.assertIsNone(gate.stale_env)

    def test_an_adopted_listener_with_a_stale_env_is_restarted_once_idle(self) -> None:
        """A gate that reloads from a hook before the clean env adopts a listener that still carries SDKROOT: the
        stale env holds it like an ineligible node (stopped only while idle, by the usual stop), and the gate
        starts a clean one. A gate that started its listener clean says so across a reload."""
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("exec /usr/bin/python3 /hook job-started --allowed-owner manaflow-ai --instance 1 "
                          "--toolchain-xcode /Applications/Xcode_26.6.app\n")
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        gate.child = mock.Mock(pid=4242)
        gate.stale_env = "the runner's environment sets SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
        self.assertEqual(gate.ineligible(), f"listener env: {gate.stale_env}; restarting it on the pool Xcode's env")
        gate.child = None
        self.assertIsNone(gate.ineligible(), "a stopped listener is no longer stale: the next start() is clean")
        with mock.patch.object(hook.subprocess, "Popen"):
            gate.start()
        self.assertIsNone(gate.stale_env)
        self.assertIsNone(gate.ineligible())

    def test_every_runner_holds_while_free_disk_is_under_its_floor(self) -> None:
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        script.write_text("exec python3 /hook job-started --allowed-owner manaflow-ai --capacity-units 4 --instance 0\n")
        self.assertIsNone(gate.claimed(), "no --min-free-gib: no floor")
        script.write_text("exec python3 /hook job-started --allowed-owner manaflow-ai --min-free-gib 999999999.5 "
                          "--trusted-ref refs/heads/main --trusted-repo manaflow-ai/cmux\n")
        self.assertEqual(hook.runner_scope(self.runner).min_free_gib, 999999999.5)
        why = gate.claimed()
        self.assertIsNotNone(why)
        self.assertRegex(why, r"^disk: [0-9.]+ GiB free, 1e\+09 GiB required$")
        self.assertEqual(gate.confirmed(), why, "the look right before a stop sees it too")
        script.write_text("exec python3 /hook job-started --allowed-owner manaflow-ai --min-free-gib 1\n")
        self.assertIsNone(gate.claimed(), "read on every poll: a re-apply that lowers the floor counts at once")
        self.assertEqual(hook.min_free_gib(" --min-free-gib 100 --min-free-gib 1e+03 "), 1000.0, "the last one wins")
        self.assertEqual(hook.min_free_gib(" --min-free-gib=150\n"), 150.0)
        self.assertEqual(hook.min_free_gib(" --min-free-gib x1 "), 0.0)
        self.assertEqual(hook.min_free_gib(" --min-free-gib 1e "), 0.0, "not a number: no floor")
        self.assertEqual(hook.min_free_gib(" --min-free-gib 1e999 "), 0.0, "not finite: no floor")
        disk = importlib.machinery.SourceFileLoader("glaeda_disk_re", os.fspath(HOOK.parent / "glaeda-disk"))
        spec = importlib.util.spec_from_loader("glaeda_disk_re", disk)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"glaeda_disk_re": module}):  # its dataclasses look the module up
            disk.exec_module(module)
        self.assertEqual(module.MIN_FREE_GIB_RE, hook.MIN_FREE_GIB_RE, "glaeda-disk reads the floor the same way")

    def test_the_gui_and_session_runners_hold_the_floor_a_cmux_next_tour_needs(self) -> None:
        """2026-10-21: a gui runner took a "Tour the app" job at 27 GiB free, over the fleet's 30 GiB floor, and
        reload.sh refused it under cmux's scripts/cmux-next/tour-requirements.json floor (40). The hook holds the
        runners that take tours to that floor (CMUX_TOUR_REQUIREMENTS, vendored) itself, so glaeda-update's hook
        refresh rolls it out to every mini without re-applying a runner or changing its wrappers."""
        tour = hook.CMUX_TOUR_REQUIREMENTS["min_free_gib"]
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        base = f"exec python3 /hook job-started --min-free-gib {tour - 10:g} --capacity-units 10 --instance 4"
        for role, floor in (("", tour - 10), (" --gui-runner", tour), (" --session-runner", tour),
                            (" --fast-runner", tour - 10)):
            with self.subTest(role=role):
                script.write_text(base + role + "\n")
                self.assertEqual(hook.runner_scope(self.runner).min_free_gib, floor)
        script.write_text(f"exec python3 /hook job-started --min-free-gib {tour + 10:g} --gui-runner\n")
        self.assertEqual(hook.runner_scope(self.runner).min_free_gib, tour + 10, "a higher fleet floor stays")
        script.write_text("exec python3 /hook job-started --gui-runner\n")
        self.assertEqual(hook.runner_scope(self.runner).min_free_gib, 0, "a runner with no floor keeps none")
        script.write_text(base + " --session-runner\n")
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        usage = lambda _path: mock.Mock(free=int((tour - 5) * 1024**3))  # noqa: E731
        with mock.patch.object(hook.shutil, "disk_usage", side_effect=usage), \
                mock.patch.object(gate, "evict_in_background"):
            self.assertEqual(gate.low_disk(), f"disk: {tour - 5:.1f} GiB free, {tour:g} GiB required",
                             "between the fleet floor and the tour's, the session runner is held")

    def test_a_disk_hold_resumes_above_the_floor_and_logs_a_long_hold_once(self) -> None:
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("exec python3 /hook job-started --min-free-gib 100\n")
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        free = {"gib": 99.0}
        usage = lambda _path: mock.Mock(free=int(free["gib"] * 1024**3))  # noqa: E731
        logs: list[str] = []
        clock = {"t": 1000.0}
        with mock.patch.object(hook.shutil, "disk_usage", side_effect=usage), \
                mock.patch.object(hook, "gate_log", side_effect=logs.append), \
                mock.patch.object(hook.time, "monotonic", side_effect=lambda: clock["t"]):
            # listening (a child runs): only the floor itself counts, so a dip that recovers never stops it
            gate.child = mock.Mock(pid=1)
            why = gate.low_disk()
            self.assertIn("100 GiB required", why)
            free["gib"] = 101.0
            self.assertIsNone(gate.low_disk(), "a listening runner back above the floor is not stopped")
            # held off for disk: it resumes only 2 GiB above the floor
            gate.child, gate.held = None, why
            self.assertIn("102 GiB required", gate.low_disk(), "a hold resumes only 2 GiB above the floor")
            clock["t"] += hook.GATE_DISK_NOTE_S
            gate.low_disk()
            gate.low_disk()
            held = [line for line in logs if line.startswith("held for free disk")]
            self.assertEqual(len(held), 1, "a long hold is logged once")
            self.assertIn("glaeda-disk --top 25", held[0])
            free["gib"] = 102.0
            self.assertIsNone(gate.low_disk())
            # held for something else: no margin, and the disk timer does not run
            gate.held = "a fleet build holds the host lock"
            free["gib"] = 101.0
            self.assertIsNone(gate.low_disk())
            self.assertIsNone(gate.disk_since)

    def test_the_first_idle_reading_under_the_disk_floor_stops_the_listener(self) -> None:
        """cmux9s, 2026-09-30: a job ended under the 30 GiB floor, the listener took the next one 2 s later, and
        job-started refused it; the gate had waited for a second idle poll, behind a capacity hold."""
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("exec python3 /hook job-started --min-free-gib 30 --capacity-units 5\n")
        free = {"gib": 50.0}
        usage = lambda _path: mock.Mock(free=int(free["gib"] * 1024**3))  # noqa: E731
        full = "all 5 capacity units on this mini are taken"
        with mock.patch.object(hook.shutil, "disk_usage", side_effect=usage):
            gate, stops = self.gate([full] * 3)
            gate.step()
            self.assertEqual(stops, [], "any other hold still waits for GATE_CONFIRM idle polls")
            gate.step()
            self.assertEqual(stops, ["held"])

            free["gib"] = 21.9
            gate, stops = self.gate([full] * 3, busy=[True])
            gate.worker_pids = lambda: [4242]
            gate.step()
            self.assertEqual(stops, [], "a runner with a job is never stopped")
            self.assertEqual(gate.watching, (4242,), "under the floor the gate watches the job's worker")
            gate.step()  # the worker exited
            self.assertEqual(stops, ["disk: 21.9 GiB free, 30 GiB required"], "at once, ahead of the capacity hold")

            free["gib"] = 31.0
            gate, stops = self.gate([None], busy=[True])
            gate.worker_pids = lambda: [4242]
            gate.step()
            self.assertEqual(gate.watching, (), "above the floor a job is polled as usual")
            gate.step()
            self.assertEqual(stops, [], "and a runner above its floor keeps listening")

    def test_a_fleet_waiter_seen_during_a_job_stops_the_listener_as_the_job_ends(self) -> None:
        """cmux11s, 2026-10-06: a fleet build waited 13 minutes while three runners kept taking queued jobs and
        refusing each one. The gate deferred while a job ran and the next job came before two idle polls."""
        self.state.mkdir(parents=True, exist_ok=True)
        hook.refused_note(self.state, 4242).touch()  # job-started refused the job
        gate, stops = self.gate([None] * 3, busy=[True])
        gate.worker_pids = lambda: [4242]
        with mock.patch.object(hook, "host_waiters", return_value={77}):
            gate.step()
            self.assertEqual(stops, [], "a runner with a job is never stopped")
            self.assertEqual(gate.watching, (4242,), "a refused job's worker is watched")
            gate.step()  # the worker exited
        self.assertEqual(stops, ["a fleet build is waiting for the host (pid 77)"], "at once, not two idle polls")

        # Any other job is polled as usual, and its runner stops on the first idle poll.
        gate, stops = self.gate([None] * 3, busy=[True, True])
        gate.worker_pids = lambda: [4343]
        with mock.patch.object(hook, "host_waiters", return_value={77}):
            gate.step(); gate.step()
            self.assertEqual((stops, gate.watching), ([], ()))
            gate.step()  # the job ended
        self.assertEqual(stops, ["a fleet build is waiting for the host (pid 77)"])

    def reserve(self, gate) -> str:
        """An active reservation of this host, as glaeda-mini-fleet reserve writes it; returns the gate's reason."""
        marker = self.tmp / "reservation.json"
        now = int(time.time())
        marker.write_text(json.dumps({"schema": "glaeda-reservation/v1", "owner": "leo@big-red+soak-0123456789ab",
                                      "purpose": "readiness soak", "since": now, "until": now + 3600}))
        gate.reservation = os.fspath(marker)
        return hook.host_held(os.fspath(self.lock), gate.reservation, time.time())

    def test_a_reservation_made_during_a_job_stops_the_listener_as_the_job_ends(self) -> None:
        """cmux10s, 2026-10-14: a soak reserved the mini at 06:01Z while three runners had jobs. Each took the next
        queued job within 2 s of the last one ending, before two idle polls, and job-started refused it: 14 PR jobs
        failed in two minutes. A reservation seen during a job now stops the listener as that job ends."""
        self.state.mkdir(parents=True, exist_ok=True)
        hook.refused_note(self.state, 4242).touch()  # job-started refused the job
        gate, stops = self.gate([None] * 3, busy=[True])
        reserved = self.reserve(gate)
        self.assertTrue(reserved and reserved.startswith("host reserved by"), reserved)
        gate.worker_pids = lambda: [4242]
        with mock.patch.object(hook, "host_waiters", return_value=set()):
            gate.step()
            self.assertEqual(stops, [], "a runner with a job is never stopped")
            self.assertEqual(gate.watching, (4242,), "a refused job's worker is watched")
            gate.step()  # the worker exited
        self.assertEqual(stops, [reserved], "at once, not two idle polls")

        # A job the reservation did not refuse (it started first) ends the same way.
        gate, stops = self.gate([None] * 3, busy=[True, True])
        reserved = self.reserve(gate)
        gate.worker_pids = lambda: [4343]
        with mock.patch.object(hook, "host_waiters", return_value=set()):
            gate.step(); gate.step()
            self.assertEqual((stops, gate.watching), ([], ()))
            gate.step()  # the job ended
        self.assertEqual(stops, [reserved])

    def test_a_reservation_released_before_the_job_ends_keeps_the_listener(self) -> None:
        gate, stops = self.gate([None] * 3, busy=[True])
        self.reserve(gate)
        gate.worker_pids = lambda: [4242]
        with mock.patch.object(hook, "host_waiters", return_value=set()):
            gate.step()
            Path(gate.reservation).unlink()
            gate.step()
        self.assertEqual(stops, [])

    def test_a_waiter_gone_by_the_end_of_the_job_keeps_the_listener(self) -> None:
        gate, stops = self.gate([None] * 3, busy=[True])
        gate.worker_pids = lambda: [4242]
        with mock.patch.object(hook, "host_waiters", side_effect=[{77}, set()]):
            gate.step()
            gate.step()
        self.assertEqual(stops, [])

    def test_a_busy_runner_asks_lsof_at_most_every_thirty_seconds(self) -> None:
        gate, stops = self.gate([None] * 3, busy=[True, True, True])
        gate.worker_pids = lambda: [4242]
        with mock.patch.object(hook, "host_waiters", return_value=set()) as lsof:
            gate.step(); gate.step()
            self.assertEqual(lsof.call_count, 1)
            gate.waiting_at -= hook.GATE_WAITER_EVERY_S
            gate.step()
            self.assertEqual(lsof.call_count, 2)
        self.assertEqual((stops, gate.watching), ([], ()))
        gate, stops = self.gate([None] * 3, busy=[True])
        gate.waiters = False
        gate.worker_pids = lambda: [4242]
        with mock.patch.object(hook, "host_waiters", return_value={77}) as lsof:
            gate.step(); gate.step()
            lsof.assert_not_called()
        self.assertEqual(stops, [])

    def test_a_waiter_refusal_on_the_mini_brings_the_waiter_reading_forward(self) -> None:
        """cmux-austin-mini-0, 2026-10-27: job-started refused a job at 17:48:12 for a fleet waiter, after 766 s in
        the capacity wait; the gate had read no waiter during the job (its 30 s cadence), so the runner took a
        nightly verify at 17:49:29 and refused it too, and stopped only at 17:49:50. Now job-started's refusal notes
        it in the mini's state dir, and every gate reads the waiters at its next poll."""
        self.state.mkdir(parents=True, exist_ok=True)
        gate, stops = self.gate([None] * 3, busy=[True, True])
        gate.worker_pids = lambda: [4242]
        with mock.patch.object(hook, "host_waiters", side_effect=[set(), {77}, {77}]) as lsof:
            gate.step()  # the job waits for capacity; no waiter yet
            self.assertEqual(lsof.call_count, 1)
            hook.refused_note(self.state, 4242).touch()
            hook.note_waiter_refusal(self.state, "a fleet build is waiting for the host (pid 77)")
            gate.step()  # within GATE_WAITER_EVERY_S, but the note makes it look now
            self.assertEqual(lsof.call_count, 2)
            self.assertEqual(gate.watching, (4242,), "the refused job's worker is watched")
            gate.step()  # the worker exited: still_waiting reads again and stops
        self.assertEqual(stops, ["a fleet build is waiting for the host (pid 77)"])

    def test_the_waiter_note_counts_once_and_only_for_waiter_refusals(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        hook.note_waiter_refusal(self.state, "capacity: 1 of 10 units free")
        hook.note_waiter_refusal(self.state, "host reserved by someone")
        self.assertIsNone(hook.waiter_seen_stamp(self.state))
        gate = hook.Gate(self.runner, os.fspath(self.lock), "", self.state)
        gate.waiting_at = time.monotonic()
        self.assertFalse(gate.waiters_due())
        hook.note_waiter_refusal(self.state, "a fleet build is waiting for the host (pid 77)")
        self.assertIsNotNone(hook.waiter_seen_stamp(self.state))
        self.assertTrue(gate.waiters_due(), "a new refusal brings the reading forward")
        self.assertFalse(gate.waiters_due(), "once per refusal, then the cadence again")
        later = hook.Gate(self.runner, os.fspath(self.lock), "", self.state)
        later.waiting_at = time.monotonic()
        self.assertFalse(later.waiters_due(), "a gate started after the note does not take it as new")
        hook.note_waiter_refusal(self.state, "a fleet build is waiting for the host (pid 78)")
        self.assertTrue(later.waiters_due())
        self.assertTrue(gate.waiters_due())

    def test_job_started_notes_a_waiter_refusal_in_the_state_dir(self) -> None:
        # take_capacity's waiter refusal and the gate's reason share one prefix, which the note keys on.
        self.assertTrue(f"{hook.WAITER_REFUSAL} (pid 1)".startswith("a fleet build is waiting for the host"))
        source = HOOK.read_text()
        self.assertIn("note_waiter_refusal(args.state_dir, why)", source)
        self.assertIn('return False, f"{WAITER_REFUSAL} (pid {waiting[0]})"', source)

    def test_a_job_under_the_disk_floor_wakes_the_gate_when_its_worker_exits(self) -> None:
        import threading
        gate = hook.Gate(self.runner, os.fspath(self.lock), "", self.state)
        gate.child = mock.Mock(pid=1)
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(worker.wait)
        self.addCleanup(worker.kill)
        gate.watching = (worker.pid,)
        # the listener reaps its worker as it exits; here the test does, as the parent
        reaper = threading.Timer(0.3, lambda: (worker.kill(), worker.wait()))
        reaper.start()
        self.addCleanup(reaper.cancel)
        with mock.patch.object(hook, "GATE_POLL_S", 30.0):
            started = time.monotonic()
            gate.nap()
            self.assertLess(time.monotonic() - started, 5.0, "woken by the worker's exit, not after GATE_POLL_S")
            self.assertEqual(gate.watching, ())
            self.assertGreater(gate.fast_until, time.monotonic())
            started = time.monotonic()
            gate.nap()
            self.assertLess(time.monotonic() - started, 1.0, "then it polls every GATE_WATCH_S")
            gate.stop_deadline = time.monotonic() + 60
            with mock.patch.object(hook.time, "sleep") as sleep:
                gate.nap()
            sleep.assert_called_once()
            self.assertGreater(sleep.call_args[0][0], 29.0, "a stop under way needs no fast polls")
        with mock.patch.object(hook, "GATE_POLL_S", 0.2):
            gate.stop_deadline, gate.fast_until = None, 0.0
            started = time.monotonic()
            gate.nap()
            self.assertGreaterEqual(time.monotonic() - started, 0.19, "nothing watched: the plain poll")

    def test_the_gate_evicts_in_the_background_before_it_listens_again(self) -> None:
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("exec python3 /hook job-started --min-free-gib 100\n")
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        free = {"gib": 99.0}
        usage = lambda _path: mock.Mock(free=int(free["gib"] * 1024**3))  # noqa: E731
        procs: list[mock.Mock] = []

        def popen(argv: list[str], **_: object) -> mock.Mock:
            proc = mock.Mock(argv=argv)
            proc.poll.return_value = None
            procs.append(proc)
            return proc

        logs: list[str] = []
        clock = {"t": 1000.0}
        with mock.patch.object(hook.shutil, "disk_usage", side_effect=usage), \
                mock.patch.object(hook, "disk_argv", side_effect=lambda _e, floor: ["glaeda-disk", f"{floor:g}"]), \
                mock.patch.object(hook.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(hook, "pass_active", return_value=False), \
                mock.patch.object(hook, "gate_log", side_effect=logs.append), \
                mock.patch.object(hook.time, "monotonic", side_effect=lambda: clock["t"]):
            gate.child = mock.Mock(pid=1)
            self.assertIn("99.0 GiB free, 100 GiB required", gate.low_disk(), "short while the eviction runs")
            self.assertEqual([p.argv for p in procs], [["glaeda-disk", "100"]], "told the runner's floor, detached")
            clock["t"] += 30
            self.assertIsNotNone(gate.low_disk())
            self.assertEqual(len(procs), 1, "one eviction at a time; the poll does not wait on it")
            procs[0].poll.return_value, free["gib"] = 0, 130.0
            self.assertIsNone(gate.low_disk(), "the eviction made room: listening again")
            self.assertEqual(logs, ["under the 100 GiB disk floor: glaeda-disk exited 0, freed 31.0 GiB"])
            # short again soon after: hold, but no new eviction until GATE_DISK_EVICT_EVERY_S has passed
            free["gib"] = 99.0
            why = gate.low_disk()
            gate.child, gate.held = None, why
            self.assertIn("102 GiB required", gate.low_disk(), "a held runner still resumes only 2 GiB above")
            self.assertEqual(len(procs), 1)
            clock["t"] += hook.GATE_DISK_EVICT_EVERY_S
            gate.low_disk()
            self.assertEqual(len(procs), 2, "retried every GATE_DISK_EVICT_EVERY_S")
            clock["t"] += hook.DISK_TIMEOUT_S
            self.assertIsNotNone(gate.low_disk())
            procs[1].kill.assert_called_once()
            self.assertEqual(logs[-1], "under the 100 GiB disk floor: glaeda-disk timed out, freed 0.0 GiB")
            self.assertIsNone(gate.evicting)

    def test_the_gate_waits_for_a_running_pass_instead_of_racing_it(self) -> None:
        """Under the floor while the queued pass (or any evict.lock holder) runs, the gate's own glaeda-disk would
        only skip as eviction_busy: it leaves one trigger for that pass, waits, and runs its own only once no pass
        runs or after GATE_DISK_EVICT_EVERY_S."""
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("exec python3 /hook job-started --min-free-gib 100\n")
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        usage = lambda _path: mock.Mock(free=int(99 * 1024**3))  # noqa: E731
        procs: list[mock.Mock] = []

        def popen(argv: list[str], **_: object) -> mock.Mock:
            proc = mock.Mock(argv=argv)
            proc.poll.return_value = None
            procs.append(proc)
            return proc

        logs: list[str] = []
        clock = {"t": 1000.0}
        active = {"on": True}
        with mock.patch.object(hook.shutil, "disk_usage", side_effect=usage), \
                mock.patch.object(hook, "disk_argv", side_effect=lambda _e, floor: ["glaeda-disk", f"{floor:g}"]), \
                mock.patch.object(hook.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(hook, "pass_active", side_effect=lambda: active["on"]), \
                mock.patch.object(hook, "disk_pressure_detached", return_value="coalesced") as trigger, \
                mock.patch.object(hook, "gate_log", side_effect=logs.append), \
                mock.patch.object(hook.time, "monotonic", side_effect=lambda: clock["t"]):
            gate.child = mock.Mock(pid=1)
            for _ in range(5):
                self.assertIsNotNone(gate.low_disk())
                clock["t"] += 30
            self.assertEqual(procs, [], "no run of its own while a pass runs")
            trigger.assert_called_once()
            self.assertEqual(logs, ["under the 100 GiB disk floor: a glaeda-disk pass is running; waiting for it "
                                    "(coalesced)"])
            active["on"] = False
            gate.low_disk()
            self.assertEqual([p.argv for p in procs], [["glaeda-disk", "100"]], "the pass ended short: its own run")
            procs[0].poll.return_value = 0
            gate.low_disk()
            # a pass that never ends (a stale trigger): waited at most GATE_DISK_EVICT_EVERY_S, then its own run
            clock["t"] += hook.GATE_DISK_EVICT_EVERY_S
            active["on"] = True
            gate.low_disk()
            self.assertEqual(len(procs), 1)
            clock["t"] += hook.GATE_DISK_EVICT_EVERY_S
            gate.low_disk()
            self.assertEqual(len(procs), 2, "a wait past GATE_DISK_EVICT_EVERY_S runs its own")

    def test_a_root_runner_holds_while_a_token_its_jobs_need_is_taken(self) -> None:
        root = hook.RunnerScope(4, 2, 2, True)
        side = hook.RunnerScope(4, 2, 2, False)
        capacity = self.units(0)
        self.assertIsNone(hook.mini_full(capacity, 4, root))
        gui = hook.lock_file(capacity / "gui.token", fcntl.LOCK_EX)
        self.assertIsNone(hook.mini_full(capacity, 4, root), "a compile needs no gui token; a shard waits for it")
        os.close(gui)
        cases = (("persistent-dd.token", "persistent-dd-1.token"), ("root-1.token", "root-2.token"))
        whys = ("all 2 persistent-dd tokens on this mini are taken", "all 2 canonical roots on this mini are taken")
        for names, why in zip(cases, whys):
            fds = [hook.lock_file(capacity / name, fcntl.LOCK_EX) for name in names]
            self.assertEqual(hook.mini_full(capacity, 4, root), why)
            self.assertIsNone(hook.mini_full(capacity, 4, side), "side runners take no root job")
            fcntl.flock(fds[-1], fcntl.LOCK_UN)
            if len(fds) > 1:
                self.assertIsNone(hook.mini_full(capacity, 4, root), "one free copy is enough")
            for fd in fds:
                os.close(fd)
        self.assertEqual(hook.mini_full(self.units(6), 10, root),
                         "only 4 of 10 capacity units on this mini are free; a compile needs 5")
        self.assertIsNone(hook.mini_full(self.units(6), 10, side))
        self.assertIsNone(hook.mini_full(self.units(5), 10, root), "5 cores free fit a compile")
        self.assertEqual(hook.mini_full(self.units(1), 2, root),
                         "only 1 of 2 capacity units on this mini are free; a compile needs 2")
        self.assertEqual(hook.mini_full(self.units(1), 1, root), "all 1 capacity units on this mini are taken")
        self.assertIsNone(hook.mini_full(self.units(0), 1, root), "a 1-unit mini needs just its unit")
        # the gate reads the scope from the runner's own hook script
        self.runner_hook(10)
        gate = self.full_gate(self.units(6))
        self.assertEqual(gate.claimed(), "only 4 of 10 capacity units on this mini are free; a compile needs 5")

    def test_a_gui_runner_holds_while_the_gui_token_or_every_root_is_taken(self) -> None:
        gui = hook.RunnerScope(5, 2, 2, False, True)
        capacity = self.units(0)
        self.assertIsNone(hook.mini_full(capacity, 5, gui))
        held = hook.lock_file(capacity / "gui.token", fcntl.LOCK_EX)
        self.assertEqual(hook.mini_full(capacity, 5, gui), "the gui token is taken")
        os.close(held)
        dd = [hook.lock_file(capacity / name, fcntl.LOCK_EX) for name in ("persistent-dd.token", "persistent-dd-1.token")]
        self.assertIsNone(hook.mini_full(capacity, 5, gui), "a GUI job needs no persistent-dd token")
        roots = [hook.lock_file(capacity / name, fcntl.LOCK_EX) for name in ("root-1.token", "root-2.token")]
        self.assertEqual(hook.mini_full(capacity, 5, gui), "all 2 canonical roots on this mini are taken")
        os.close(roots.pop())
        self.assertIsNone(hook.mini_full(capacity, 5, gui), "one free root is enough")
        for fd in [*roots, *dd]:
            os.close(fd)
        self.assertIsNone(hook.mini_full(self.units(4), 5, gui), "a GUI job needs one unit, not a compile's two")
        self.assertEqual(hook.mini_full(self.units(5), 5, gui), "all 5 capacity units on this mini are taken")
        one = hook.RunnerScope(5, 1, 1, False, True)
        capacity = self.units(0)
        held = hook.lock_file(capacity / "root-1.token", fcntl.LOCK_EX)
        self.assertEqual(hook.mini_full(capacity, 5, one), "the canonical root is taken")
        os.close(held)

    def test_a_session_runner_holds_only_for_units(self) -> None:
        """A session runner (guiRunners 2: the gui-role runner beside the console one) takes only session-only jobs
        (daemon tests, generated files, action exports, the tour): no gui token or root, so a console job holding
        them is no reason to idle it. The console gui runner keeps its gate."""
        session = hook.RunnerScope(5, 2, 2, False, False, session=True)
        console = hook.RunnerScope(5, 2, 2, False, True)
        capacity = self.units(0)
        held = hook.lock_file(capacity / "gui.token", fcntl.LOCK_EX)
        roots = [hook.lock_file(capacity / name, fcntl.LOCK_EX) for name in ("root-1.token", "root-2.token")]
        self.assertIsNone(hook.mini_full(capacity, 5, session), "neither the token nor the roots hold it")
        self.assertEqual(hook.mini_full(capacity, 5, console), "the gui token is taken")
        (capacity / f"{hook.ADMIT_WANT}{os.getpid()}").write_text(
            json.dumps({"since": time.time(), "wants": ["gui"], "who": "g1 app-host-unit-tests (run 3)"}) + "\n")
        self.assertIsNone(hook.mini_full(capacity, 5, session), "a console job waiting is the console runner's business")
        (capacity / f"{hook.ADMIT_WANT}{os.getpid()}").unlink()
        self.assertEqual(hook.mini_full(capacity, 5, console), "the gui token is taken", "only its own gate")
        for fd in [held, *roots]:
            os.close(fd)
        self.assertEqual(hook.mini_full(self.units(5), 5, session), "all 5 capacity units on this mini are taken")

    def test_an_idle_gui_runners_reserve_holds_the_other_runners_a_unit_early(self) -> None:
        """While an idle gui runner's gate holds <gui token>.reserve, every other runner (root, side, session) counts
        one unit fewer free, so none of them listens for a job that would take the gui runner's unit. The gui
        runner counts the unit as its own, and a job waiting here for units is no reason to hold it: its next job
        takes the reserved unit, not theirs. The fast runner takes no general unit either way."""
        root = hook.RunnerScope(10, 2, 2, True)
        side = hook.RunnerScope(10, 2, 2)
        session = hook.RunnerScope(10, 2, 2, False, False, session=True)
        gui = hook.RunnerScope(10, 2, 2, False, True)
        fast = hook.RunnerScope(10, 2, 2, False, False, fast=True)
        capacity = self.units(9)
        for scope in (side, session, gui):
            self.assertIsNone(hook.mini_full(capacity, 10, scope), "no reserve: one free unit is room")
        reserve = hook.lock_file(capacity / f"gui{hook.GUI_RESERVE}", fcntl.LOCK_EX)
        self.addCleanup(os.close, reserve)
        for scope in (side, session):
            self.assertEqual(hook.mini_full(capacity, 10, scope), "only 1 of 10 capacity units on this mini are "
                             "free; its next job needs 1 and 1 is kept for an idle reserving runner")
        self.assertIsNone(hook.mini_full(capacity, 10, gui), "the reserved unit is the gui runner's own")
        self.assertIsNone(hook.mini_full(capacity, 10, fast))

        def reserved(held: int) -> Path:
            ledger = self.units(held)
            self.addCleanup(os.close, hook.lock_file(ledger / f"gui{hook.GUI_RESERVE}", fcntl.LOCK_EX))
            return ledger

        self.assertEqual(hook.mini_full(reserved(5), 10, root), "only 5 of 10 capacity units on this mini are "
                         "free; a compile needs 5 and 1 is kept for an idle reserving runner")
        self.assertIsNone(hook.mini_full(reserved(4), 10, root), "6 free: a compile and the kept unit")
        self.assertIsNone(hook.mini_full(reserved(0), 5, root), "a reserve never makes a compile unfittable")
        self.assertIsNone(hook.mini_full(self.units(5), 10, root), "no gui runner on the mini: nothing kept")
        (capacity / f"{hook.ADMIT_WANT}{os.getpid()}").write_text(
            json.dumps({"since": time.time(), "wants": ["units"], "who": "s1 daemon-test (run 4)"}) + "\n")
        self.assertIsNone(hook.mini_full(capacity, 10, gui), "a job waiting for units does not hold the gui runner")
        (capacity / f"{hook.ADMIT_WANT}{os.getpid()}").write_text(
            json.dumps({"since": time.time(), "wants": ["gui"], "who": "r0 app-host-unit-tests (run 4)"}) + "\n")
        self.assertEqual(hook.mini_full(capacity, 10, gui), "r0 app-host-unit-tests (run 4) waits here for gui")
        (capacity / f"{hook.ADMIT_WANT}{os.getpid()}").unlink()
        self.assertEqual(hook.mini_full(self.units(10), 10, gui), "all 10 capacity units on this mini are taken")

    def test_an_idle_freshness_slots_reserve_holds_the_other_runners_off_its_compile_room(self) -> None:
        """The freshness slot (--dev-compile-runner) reserves FRESH_RESERVE_UNITS, one feed compile: every other
        runner counts that many fewer free, so the PR side runners on its mini stop listening before they would
        take its room. The slot counts the units as its own and listens only with room for its compile."""
        side = hook.RunnerScope(10, 2, 2)
        fresh = hook.RunnerScope(10, 2, 2, fresh=True)
        self.assertEqual(hook.reserve_token(fresh), hook.FRESH_RESERVE_TOKEN)
        self.assertIsNone(hook.reserve_token(side))

        def reserved(held: int) -> Path:
            ledger = self.units(held)
            self.addCleanup(os.close, hook.lock_file(ledger / f"{hook.FRESH_RESERVE_TOKEN}{hook.GUI_RESERVE}",
                                                     fcntl.LOCK_EX))
            return ledger

        self.assertEqual(hook.FRESH_RESERVE_UNITS, 3, "the feed compiles' measured cost (JOB_CORES)")
        self.assertIsNone(hook.mini_full(reserved(6), 10, side), "4 free: its job and the slot's 3")
        self.assertEqual(hook.mini_full(reserved(7), 10, side), "only 3 of 10 capacity units on this mini are "
                         "free; its next job needs 1 and 3 are kept for an idle reserving runner")
        self.assertIsNone(hook.mini_full(reserved(7), 10, fresh), "the reserved units are the slot's own")
        self.assertEqual(hook.mini_full(self.units(8), 10, fresh), "only 2 of 10 capacity units on this mini are "
                         "free; its next job needs 3")
        self.assertIsNone(hook.mini_full(self.units(9), 10, side), "no slot on the mini: nothing kept")
        ledger = reserved(7)
        (ledger / f"{hook.ADMIT_WANT}{os.getpid()}").write_text(
            json.dumps({"since": time.time(), "wants": ["units"], "who": "s1 daemon-test (run 4)"}) + "\n")
        self.assertIsNone(hook.mini_full(ledger, 10, fresh), "a PR job waiting for units does not hold the slot")
        self.assertIsNotNone(hook.mini_full(ledger, 10, side))

    def test_the_nightly_slot_listens_on_a_full_mini_and_its_waiting_job_holds_every_other_runner(self) -> None:
        """NIGHTLY_SLOT: the nightly slot's gate never holds it for units, a waiter or CPU saturation, so GitHub
        hands it the queued nightly job at once; that job, waiting here, holds every other runner on the mini."""
        capacity = self.units(10)
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(exist_ok=True)
        script.write_text("#!/bin/bash\nexec /usr/bin/python3 /hook job-started --capacity-units 10 --instance 2"
                          " --canonical-roots 2 --nightly-runner --test-keychain\n")
        scope = hook.runner_scope(self.runner)
        self.assertTrue(scope.nightly)
        self.assertFalse(scope.root or scope.fresh or scope.gui or scope.session or scope.fast or scope.dedicated)
        self.assertIsNone(hook.reserve_token(scope), "it keeps no reserve: a nightly job takes the next free units")
        gate = self.full_gate(capacity)
        self.assertEqual(hook.mini_full(capacity, 10, hook.RunnerScope(10, 2, 2)),
                         "all 10 capacity units on this mini are taken")
        self.assertIsNone(gate.full(), "every unit taken: the nightly slot listens anyway")
        self.assertIsNone(gate.full(fresh=True))
        (capacity / f"{hook.ADMIT_WANT}{os.getpid()}").write_text(json.dumps(
            {"since": time.time() - 600, "wants": ["units"], "who": "s3 swift-test (run 7)"}) + "\n")
        self.assertIsNone(gate.full(), "an older PR waiter does not hold it: its job goes first")
        with mock.patch.object(hook, "host_io", return_value=(None, 99.0)), \
                mock.patch.object(hook, "memory_pressure_level", return_value=None):
            self.assertIsNone(gate.saturated(), "a busy CPU does not hold it, as it does not hold the freshness slot")
        free = self.units(0)
        (free / f"{hook.ADMIT_WANT}{os.getpid()}").write_text(json.dumps(
            {"since": time.time(), "wants": ["units"], "who": "n2 build-nightly-app (run 9)",
             "tier": hook.TIER_NIGHTLY}) + "\n")
        for other in (hook.RunnerScope(10, 2, 2), hook.RunnerScope(10, 2, 2, root=True),
                      hook.RunnerScope(10, 2, 2, gui=True), hook.RunnerScope(10, 2, 2, fresh=True),
                      hook.RunnerScope(10, 2, 2, session=True), hook.RunnerScope(10, 2, 2, fast=True)):
            with self.subTest(other=other):
                self.assertEqual(hook.mini_full(free, 10, other), "n2 build-nightly-app (run 9) waits here for units",
                                 "no new job starts on a mini where a nightly job waits")

    def test_a_freshness_slots_gate_holds_fresh_reserve_while_idle(self) -> None:
        capacity = self.units(0)
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(exist_ok=True)
        script.write_text("#!/bin/bash\nexec /usr/bin/python3 /hook job-started --capacity-units 10 --instance 1"
                          " --dev-compile-runner\n")
        self.assertTrue(hook.runner_scope(self.runner).fresh)
        gate = self.full_gate(capacity)
        self.addCleanup(gate.reserve, False)
        reserve = capacity / f"{hook.FRESH_RESERVE_TOKEN}{hook.GUI_RESERVE}"
        with mock.patch.object(gate, "start"), mock.patch.object(gate, "stop"), \
                mock.patch.object(gate, "reload"), mock.patch.object(gate, "saturated", return_value=None), \
                mock.patch.object(gate, "busy", return_value=False):
            gate.step()
            self.assertFalse(hook.probe(reserve), "idle: the slot holds its reserve")
            self.assertEqual(hook.gui_reserved(capacity), hook.FRESH_RESERVE_UNITS)
            self.assertEqual(hook.gui_reserved(capacity, hook.FRESH_RESERVE_TOKEN), 0, "its own job takes it")
        # A job arrives: the reserve stays whole until that job holds its admission, then is lent to it: it keeps
        # nothing while the job holds its units, and is whole again the moment the job's holder lets them go, before
        # any gate poll (2026-10-27 05:07:50Z: PR waiters took the units in the seconds before the next idle poll).
        gate.child = mock.Mock(poll=mock.Mock(return_value=None))
        (self.runner / ".runner").write_text("\ufeff" + json.dumps({"agentName": "mini-glaeda-1"}), encoding="utf-8")
        holder_proc = subprocess.Popen([sys.executable, "-c", "import fcntl, os, sys, time\n"
                                   "fd = os.open(sys.argv[1], os.O_RDONLY | os.O_CREAT, 0o644)\n"
                                   "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\nsys.stdin.read()\n",
                                   os.fspath(hook.lent_file(reserve))], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder_proc.kill(), holder_proc.wait(), holder_proc.stdout.close(),
                                 holder_proc.stdin.close()))
        with mock.patch.object(gate, "start"), mock.patch.object(gate, "stop"), \
                mock.patch.object(gate, "reload"), mock.patch.object(gate, "saturated", return_value=None), \
                mock.patch.object(gate, "busy", return_value=True):
            gate.step()
            self.assertFalse(hook.probe(reserve), "a job not yet admitted: its units wait for it")
            self.assertEqual(hook.gui_reserved(capacity), hook.FRESH_RESERVE_UNITS)
            self.state.mkdir(parents=True, exist_ok=True)
            holder = hook.holder_file(self.state, "mini-glaeda-1")
            holder.write_text(f"{holder_proc.pid}\n")
            self.assertEqual(holder_proc.stdout.readline().strip(), "held")  # its admission holder took the lend
            gate.step()
            self.assertFalse(hook.probe(reserve), "its job holds its units: the reserve is kept, lent to that job")
            self.assertTrue(hook.reserve_lent(reserve))
            self.assertEqual(hook.gui_reserved(capacity), 0, "lent: its job's own units are the reserve")
            gate.step()
            self.assertEqual(hook.gui_reserved(capacity), 0, "still lent on the next poll")
            holder_proc.stdin.close()
            holder_proc.wait()
            self.assertEqual(hook.gui_reserved(capacity), hook.FRESH_RESERVE_UNITS,
                             "the job let its units go: the reserve is whole at once, with no gate poll")
            holder.unlink()
        # The next job of the slot's own listener, taken before any idle poll, finds its reserve whole (take_capacity
        # in_reserve): an older PR job waiting for units does not go ahead of it.
        self.assertFalse(hook.probe(reserve))
        with mock.patch.object(gate, "start"), mock.patch.object(gate, "stop"), \
                mock.patch.object(gate, "reload"), mock.patch.object(gate, "saturated", return_value=None), \
                mock.patch.object(gate, "busy", return_value=False):
            gate.step()
            self.assertFalse(hook.probe(reserve), "idle again: still held")
        # Review: the gate once read "admitted, not lent" (a holder letting go, or one that took no lend) as a reason to
        # drop the reserve, reopening the gap. It keeps the reserve through any job of its own; unlent, the reserve still
        # counts beside the job, the safe side.
        gate.child = mock.Mock(poll=mock.Mock(return_value=None))
        holder.write_text(f"{os.getpid()}\n")
        with mock.patch.object(gate, "start"), mock.patch.object(gate, "stop"), \
                mock.patch.object(gate, "reload"), mock.patch.object(gate, "saturated", return_value=None), \
                mock.patch.object(gate, "busy", return_value=True):
            gate.step()
            self.assertFalse(hook.probe(reserve), "admitted without a lend: still held")
            self.assertEqual(hook.gui_reserved(capacity), hook.FRESH_RESERVE_UNITS, "and counted beside the job")
        holder.unlink()
        gate.reserve(False)
        self.assertTrue(hook.probe(reserve))

    def test_a_job_arriving_during_the_idle_look_keeps_the_reserve(self) -> None:
        """Review: a job taken after busy() read false but before claimed() finished made claimed() see the runner's
        own job (its gui token) and drop the reserve, which then stayed dropped through the job."""
        capacity = self.units(0)
        reserve = capacity / f"gui{hook.GUI_RESERVE}"
        gate = self.gui_gate(capacity)
        busy = [False]
        with mock.patch.object(gate, "start"), mock.patch.object(gate, "stop"), \
                mock.patch.object(gate, "reload"), mock.patch.object(gate, "saturated", return_value=None), \
                mock.patch.object(hook, "console_refusal", return_value=None), \
                mock.patch.object(gate, "busy", side_effect=lambda: busy[0]):
            gate.step()
            self.assertFalse(hook.probe(reserve), "idle: held")
            gate.child = mock.Mock(poll=mock.Mock(return_value=None))
            real = gate.claimed

            def job_arrives():
                busy[0] = True  # the listener took a job during the look
                token = hook.lock_file(capacity / "gui.token", fcntl.LOCK_EX)
                self.addCleanup(os.close, token)
                return real()

            with mock.patch.object(gate, "claimed", side_effect=job_arrives):
                busy[0] = False
                gate.step()
            self.assertFalse(hook.probe(reserve), "its own job arrived meanwhile: the reserve stays")
            gate.step()
            self.assertFalse(hook.probe(reserve), "and through the job")

    def test_a_job_arriving_during_the_confirming_look_keeps_the_reserve(self) -> None:
        """Review (round 6): the reserve was let go on claimed()'s answer, before confirmed(); a job taken during the
        confirming look then skipped the stop and never got the reserve back. It is let go only after the last look,
        and only with no job of its own."""
        capacity = self.units(0)
        reserve = capacity / f"gui{hook.GUI_RESERVE}"
        gate = self.gui_gate(capacity)
        busy = [False]
        with mock.patch.object(gate, "start"), mock.patch.object(gate, "stop") as stop, \
                mock.patch.object(gate, "reload"), mock.patch.object(gate, "saturated", return_value=None), \
                mock.patch.object(gate, "first_poll", return_value=False), \
                mock.patch.object(hook, "console_refusal", return_value=None), \
                mock.patch.object(gate, "busy", side_effect=lambda: busy[0]):
            gate.step()
            self.assertFalse(hook.probe(reserve), "idle: held")
            gate.child = mock.Mock(poll=mock.Mock(return_value=None))

            def job_arrives():
                busy[0] = True  # the listener took a job during the confirming look
                return "held for the fleet"

            gate.streak = hook.GATE_CONFIRM
            with mock.patch.object(gate, "claimed", return_value="held for the fleet"), \
                    mock.patch.object(gate, "confirmed", side_effect=job_arrives):
                gate.step()
            stop.assert_not_called()
            self.assertFalse(hook.probe(reserve), "its own job arrived during confirmed(): the reserve stays")
            gate.step()
            self.assertFalse(hook.probe(reserve), "and through the job")
            gate.reserve(False)

    def test_a_job_beating_the_last_busy_check_gets_the_reserve_back(self) -> None:
        """A job that arrives after the idle look's last busy() read finds the reserve let go; its first busy poll
        takes it back, so the job's end does not reopen the gap. Review (round 7): the retake reads only the job, so a
        reload between the drop and that poll loses nothing."""
        capacity = self.units(0)
        reserve = capacity / f"gui{hook.GUI_RESERVE}"
        gate = self.gui_gate(capacity)
        busy = [False]
        with mock.patch.object(gate, "start"), mock.patch.object(gate, "stop"), \
                mock.patch.object(gate, "reload"), mock.patch.object(gate, "saturated", return_value=None), \
                mock.patch.object(hook, "console_refusal", return_value=None), \
                mock.patch.object(gate, "busy", side_effect=lambda: busy[0]):
            gate.step()
            self.assertFalse(hook.probe(reserve), "idle: held")
            gate.child = mock.Mock(poll=mock.Mock(return_value=None))
            with mock.patch.object(gate, "claimed", return_value="held for the fleet"):
                gate.step()
            self.assertTrue(hook.probe(reserve), "a hold with no job: let go")
        child = gate.child
        gate.reserve(False)
        gate = self.gui_gate(capacity)  # a reload: a new gate with nothing carried over
        self.addCleanup(gate.reserve, False)
        gate.child = child
        with mock.patch.object(gate, "start"), mock.patch.object(gate, "stop"), \
                mock.patch.object(gate, "reload"), mock.patch.object(gate, "saturated", return_value=None), \
                mock.patch.object(hook, "console_refusal", return_value=None), \
                mock.patch.object(gate, "busy", return_value=True):
            gate.step()
            self.assertFalse(hook.probe(reserve), "the job that beat the check gets the reserve back, reload or not")

    def test_a_lend_is_a_flock_so_a_file_or_a_pid_lends_nothing(self) -> None:
        """Review: a .lent naming a pid could be satisfied by a reused pid or written by any job. The lend is the
        admission holder's flock on <token>.reserve.lent: the file's contents mean nothing, and the lend dies with the
        process that holds it."""
        capacity = self.units(0)
        reserve = capacity / f"{hook.FRESH_RESERVE_TOKEN}{hook.GUI_RESERVE}"
        fd = hook.lock_file(reserve, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        hook.lent_file(reserve).write_text(f"{os.getpid()} {hook.process_identity(os.getpid())}\n")
        self.assertFalse(hook.reserve_lent(reserve), "contents lend nothing")
        self.assertEqual(hook.gui_reserved(capacity), hook.FRESH_RESERVE_UNITS)
        lend = hook.lock_file(hook.lent_file(reserve), fcntl.LOCK_EX)
        self.assertTrue(hook.reserve_lent(reserve))
        self.assertEqual(hook.gui_reserved(capacity), 0)
        os.close(lend)
        self.assertEqual(hook.gui_reserved(capacity), hook.FRESH_RESERVE_UNITS, "the holder is gone: whole again")

    def test_a_reload_hands_the_reserve_flock_to_the_new_gate(self) -> None:
        """The reserve flock is the gate's fd; a reload's exec used to close it, so admissions saw the reserve free
        until the new gate's first poll. Now the fd is inherited (GATE_RESERVE_FD_ENV) and adopted only when it is
        open on this runner's own reserve."""
        capacity = self.units(0)
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(exist_ok=True)
        script.write_text("#!/bin/bash\nexec /usr/bin/python3 /hook job-started --capacity-units 10 --instance 1"
                          " --dev-compile-runner\n")
        reserve = capacity / f"{hook.FRESH_RESERVE_TOKEN}{hook.GUI_RESERVE}"
        fd = hook.lock_file(reserve, fcntl.LOCK_EX)
        os.set_inheritable(fd, True)
        gate = self.full_gate(capacity)
        self.addCleanup(gate.reserve, False)
        gate.adopt_reserve(str(fd))
        self.assertEqual(gate.reserve_fd, fd)
        self.assertFalse(os.get_inheritable(fd), "never leaked into run.sh")
        self.assertFalse(hook.probe(reserve), "still held")
        other = os.open(capacity / "other", os.O_RDONLY | os.O_CREAT, 0o644)
        second = self.full_gate(capacity)
        second.adopt_reserve(str(other))
        self.assertIsNone(second.reserve_fd, "an fd on another file is not a reserve")
        with self.assertRaises(OSError):
            os.fstat(other)
        for junk in (None, "", "x", "2"):
            second.adopt_reserve(junk)
            self.assertIsNone(second.reserve_fd)

    def gui_gate(self, capacity: Path, gui: bool = True) -> hook.Gate:
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(exist_ok=True)
        script.write_text("#!/bin/bash\nexec /usr/bin/python3 /hook job-started --capacity-units 10 --instance 3"
                          + (" --gui-runner" if gui else "") + "\n")
        gate = self.full_gate(capacity)
        self.addCleanup(gate.reserve, False)
        return gate

    def test_an_idle_gui_runners_gate_holds_its_reserve_and_lets_it_go_with_a_job(self) -> None:
        """The reserve is a flock, so it lives exactly as long as the gate wants it: held while the gui runner has
        no job and nothing but units holds it, and through a new job's admission (else a job waiting here for units
        takes the unit in the seconds before job-started runs); let go once that job holds its units, or while the
        gui token, a locked console, a fleet build or heat keeps it off (its unit would only sit idle); and gone
        with the gate's process."""
        capacity = self.units(0)
        reserve = capacity / f"gui{hook.GUI_RESERVE}"
        gate = self.gui_gate(capacity)
        console = [None]
        with mock.patch.object(gate, "start"), mock.patch.object(gate, "stop"), \
                mock.patch.object(gate, "reload"), mock.patch.object(gate, "saturated", return_value=None), \
                mock.patch.object(hook, "console_refusal", side_effect=lambda: console[0]), \
                mock.patch.object(gate, "busy", return_value=False) as busy:
            gate.step()  # no listener: free, so it starts one
            self.assertFalse(hook.probe(reserve), "idle: the reserve is held")
            gate.child = mock.Mock(poll=mock.Mock(return_value=None))
            (self.runner / ".runner").write_text("\ufeff" + json.dumps({"agentName": "mini-glaeda-3"}),
                                                 encoding="utf-8")
            busy.return_value = True
            gate.step()
            self.assertFalse(hook.probe(reserve), "a job not yet admitted: the reserved unit waits for it")
            self.state.mkdir(parents=True, exist_ok=True)
            holder = hook.holder_file(self.state, "mini-glaeda-3")
            holder.write_text(f"{os.getpid()}\n")
            lend = hook.lock_file(hook.lent_file(reserve), fcntl.LOCK_EX)  # what its admission holder takes
            gate.step()
            self.assertFalse(hook.probe(reserve), "its job holds its units: the reserve is kept, lent to it")
            self.assertEqual(hook.gui_reserved(capacity), 0)
            os.close(lend)
            holder.unlink()
            busy.return_value = False
            gate.step()
            self.assertFalse(hook.probe(reserve), "idle again")
            fds = [hook.lock_file(capacity / f"unit-{slot}", fcntl.LOCK_EX) for slot in range(10)]
            gate.step()
            self.assertFalse(hook.probe(reserve), "held off as full: still the gui lane's unit")
            token = hook.lock_file(capacity / "gui.token", fcntl.LOCK_EX)
            while fds:
                os.close(fds.pop())
            gate.step()
            self.assertTrue(hook.probe(reserve), "held off for the gui token: it could take no job")
            os.close(token)
            gate.step()
            self.assertFalse(hook.probe(reserve))
            console[0], gate.console_at = "console: the console session (cmux) is screen-locked", -1e9
            gate.step()
            self.assertTrue(hook.probe(reserve), "a gui runner that cannot take a job keeps no unit")
        capacity = self.units(0)
        other = self.gui_gate(capacity, gui=False)
        with mock.patch.object(other, "start"), mock.patch.object(other, "reload"), \
                mock.patch.object(other, "saturated", return_value=None), \
                mock.patch.object(other, "busy", return_value=False):
            other.step()
        self.assertEqual(list(capacity.glob(f"*{hook.GUI_RESERVE}")), [], "only a gui runner reserves")

    def test_a_gui_runner_listens_through_cpu_saturation_but_not_memory_pressure(self) -> None:
        """The CPU hold kept the gui runners off while side and root jobs kept their minis busy, so the gui label
        queued for hours. A gui runner's jobs are admitted by units like any other, and the gui token keeps them
        one per mini, so the CPU mark no longer holds it; memory pressure still does."""
        for name, value in (("GATE_CPU_PAUSE", 80.0), ("GATE_CPU_RESUME", 70.0), ("GATE_CPU_EVERY_S", 0.0)):
            patcher = mock.patch.object(hook, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        pressure = [1]
        with mock.patch.object(hook, "host_io", return_value=(0.0, 99.0)), \
                mock.patch.object(hook, "memory_pressure_level", side_effect=lambda: pressure[0]), \
                mock.patch.object(hook, "console_refusal", return_value=None):
            gui = self.gui_gate(self.units(0))
            self.assertIsNone(gui.claimed(), "99% CPU: the gui runner listens")
            pressure[0] = 2
            why = gui.claimed()
            self.assertTrue(why and why.startswith(hook.SATURATED) and "memory pressure level 2" in why, why)
            self.assertNotIn("CPU busy", why)
            pressure[0] = 1
            side = self.gui_gate(self.units(0), gui=False)
            self.assertTrue((side.claimed() or "").startswith(hook.SATURATED), "every other runner keeps the CPU hold")
            script = self.runner / hook.RUNNER_HOOK_SCRIPT
            script.write_text("#!/bin/bash\nexec /usr/bin/python3 /hook job-started --capacity-units 10 --instance 1"
                              " --dev-compile-runner\n")
            fresh = self.full_gate(self.units(0))
            self.addCleanup(fresh.reserve, False)
            self.assertIsNone(fresh.claimed(), "99% CPU: the freshness slot listens for the feed compiles")
            pressure[0] = 2
            self.assertTrue((fresh.claimed() or "").startswith(hook.SATURATED), "memory pressure still holds it")

    def test_runner_scope_reads_the_session_runner_flag(self) -> None:
        script = self.runner / hook.RUNNER_HOOK_SCRIPT
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("exec python3 /hook job-started --capacity-units 5 --canonical-roots 2 --instance 0 "
                          "--session-runner\n")
        self.assertEqual(hook.runner_scope(self.runner), hook.RunnerScope(5, 1, 2, False, False, session=True),
                         "a session runner is never a root runner, whatever its instance")
        script.write_text("exec python3 /hook job-started --capacity-units 5 --instance 4 --session-runner "
                          "--fast-runner\n")
        self.assertEqual((hook.runner_scope(self.runner).session, hook.runner_scope(self.runner).fast), (False, True))

    def test_the_fast_runner_listens_only_while_the_fast_tokens_are_free(self) -> None:
        fast = hook.RunnerScope(5, 2, 2, False, False, fast=True)
        self.assertIsNone(hook.mini_full(self.units(5), 5, fast), "the fast runner takes no general unit")
        capacity = self.units(0)
        for name in ("fast-0.token", "fast-1.token"):
            held = hook.lock_file(capacity / name, fcntl.LOCK_EX)
            self.assertEqual(hook.mini_full(capacity, 5, fast), f"the {name[:-6]} token is taken")
            os.close(held)
        self.assertIsNone(hook.mini_full(capacity, 5, fast))
        (capacity / f"{hook.ADMIT_WANT}{os.getpid()}").write_text(
            json.dumps({"since": time.time(), "wants": ["fast-0"], "who": "f1 fast-tier (run 2)"}) + "\n")
        self.assertEqual(hook.mini_full(capacity, 5, fast), "f1 fast-tier (run 2) waits here for fast-0")
        (capacity / f"{hook.ADMIT_WANT}{os.getpid()}").write_text(
            json.dumps({"since": time.time(), "wants": ["units"], "who": "r9 swift-test (run 1)"}) + "\n")
        self.assertIsNone(hook.mini_full(capacity, 5, fast), "a job waiting for general units is not its business")
        # and no other runner counts the fast tokens
        held = [hook.lock_file(capacity / name, fcntl.LOCK_EX) for name in ("fast-0.token", "fast-1.token")]
        (capacity / f"{hook.ADMIT_WANT}{os.getpid()}").unlink()
        for scope in (hook.RunnerScope(5, 2, 2, True), hook.RunnerScope(5, 2, 2), hook.RunnerScope(5, 2, 2, False, True)):
            self.assertIsNone(hook.mini_full(capacity, 5, scope), scope)
        for fd in held:
            os.close(fd)

    def test_the_full_probe_never_competes_with_an_admission(self) -> None:
        capacity = self.units(1)
        admission = os.open(capacity / "admission.lock", os.O_RDONLY)
        self.addCleanup(os.close, admission)
        fcntl.flock(admission, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with mock.patch.object(hook, "lock_file", side_effect=AssertionError("probed during an admission")):
            self.assertIs(hook.mini_full(capacity, 2), hook.UNKNOWN)
        fcntl.flock(admission, fcntl.LOCK_UN)
        self.assertIsNone(hook.mini_full(capacity, 2))
        # and the probe leaves the free unit free
        fd = hook.lock_file(capacity / "unit-1", fcntl.LOCK_EX)
        self.assertIsNotNone(fd)
        os.close(fd)

    def test_a_look_an_admission_blocks_keeps_the_last_answer(self) -> None:
        capacity = self.units(2)
        self.runner_hook(2)
        gate = self.full_gate(capacity)
        self.assertEqual(gate.claimed(), "all 2 capacity units on this mini are taken")
        # a full mini's admissions keep retrying: a blocked look must not read as free and restart a listener
        with mock.patch.object(hook, "mini_full", return_value=hook.UNKNOWN):
            self.assertEqual(gate.claimed(), "all 2 capacity units on this mini are taken")
            self.assertIsNone(gate.confirmed(), "the look right before a stop needs a reading of its own")
        with mock.patch.object(hook, "mini_full", return_value=None):
            self.assertIsNone(gate.claimed())
        with mock.patch.object(hook, "mini_full", return_value=hook.UNKNOWN):
            self.assertIsNone(gate.claimed(), "and a free mini stays free")

    def test_a_stopped_listener_stays_off_while_blocked_looks_hide_a_full_mini(self) -> None:
        capacity = self.units(2)
        self.runner_hook(2)
        gate = self.full_gate(capacity)
        gate.reload = lambda: None
        gate.child, gate.held, gate.full_why = None, "all 2 capacity units on this mini are taken", \
            "all 2 capacity units on this mini are taken"
        starts: list[int] = []
        gate.start = lambda: starts.append(1)
        with mock.patch.object(hook, "mini_full", return_value=hook.UNKNOWN):
            for _ in range(4):
                self.assertIsNone(gate.step())
        self.assertEqual(starts, [])
        with mock.patch.object(hook, "mini_full", return_value=None):
            for _ in range(2):
                gate.step()
        self.assertEqual(starts, [1], "two free looks restart the listener")

    def test_an_idle_listener_stops_only_after_the_claim_holds_for_two_polls(self) -> None:
        gate, stops = self.gate(["held", None, "held", "held"])
        for _ in range(3):
            self.assertIsNone(gate.step())
        self.assertEqual(stops, [])
        gate.step()
        self.assertEqual(stops, ["held"])

    def test_a_busy_runner_is_never_stopped_and_its_polls_do_not_count(self) -> None:
        gate, stops = self.gate(["held", "held"], busy=[True, True, True, False, False])
        for _ in range(4):
            gate.step()
        self.assertEqual(stops, [])  # three busy polls, then one idle claim: not two in a row yet
        gate.step()
        self.assertEqual(stops, ["held"])

    def test_the_fresh_look_before_a_stop_can_call_it_off(self) -> None:
        gate, stops = self.gate(["held", "held"])
        gate.confirmed = lambda: None
        gate.step(); gate.step()
        self.assertEqual(stops, [])

    def test_a_waiter_must_still_wait_in_a_second_reading(self) -> None:
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        gate.waiting = {77}
        with mock.patch.object(hook, "host_waiters", return_value={88}):
            self.assertIsNone(gate.confirmed())  # 77 was a job-started in flight; 88 is new, not yet confirmed
        gate.waiting = {77}
        with mock.patch.object(hook, "host_waiters", return_value={77, 88}):
            self.assertEqual(gate.confirmed(), "a fleet build is waiting for the host (pid 77)")

    def test_a_throttled_mini_holds_until_it_has_cooled(self) -> None:
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        self.level = 2
        self.assertIsNone(gate.claimed())  # heavy under load is normal
        self.level = 4
        gate.thermal_at -= hook.GATE_THERMAL_EVERY_S
        self.assertEqual(gate.claimed(), "the mini is throttled (thermal pressure level 4)")
        self.assertEqual(gate.confirmed(), "the mini is throttled (thermal pressure level 4)")
        self.level = 2
        gate.thermal_at -= hook.GATE_THERMAL_EVERY_S
        self.assertEqual(gate.claimed(), "the mini is throttled (thermal pressure level 2)")  # not cool yet
        self.level = 1
        self.assertIsNotNone(gate.claimed())  # read at most every thirty seconds
        gate.thermal_at -= hook.GATE_THERMAL_EVERY_S
        self.assertIsNone(gate.claimed())

    def test_an_unreadable_level_or_the_switch_off_never_holds(self) -> None:
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        self.level = None
        self.assertIsNone(gate.claimed())
        self.level = 4
        with mock.patch.object(hook, "GATE_THERMAL_HOLD", 0):
            self.assertIsNone(gate.confirmed())

    def test_a_hold_at_level_one_does_not_flap_at_a_steady_level(self) -> None:
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        self.level = 1
        with mock.patch.object(hook, "GATE_THERMAL_HOLD", 1):
            self.assertIsNotNone(gate.claimed())
            gate.thermal_at -= hook.GATE_THERMAL_EVERY_S
            self.assertIsNotNone(gate.claimed())  # still held at the same level
            self.level = 0
            gate.thermal_at -= hook.GATE_THERMAL_EVERY_S
            self.assertIsNone(gate.claimed())

    def test_a_bad_hold_setting_falls_back_instead_of_breaking_the_hook(self) -> None:
        out = subprocess.run([sys.executable, "-c", f"import runpy; m = runpy.run_path({os.fspath(HOOK)!r}, "
                              "run_name='x'); print(m['GATE_THERMAL_HOLD'])"],
                             env={**os.environ, "GLAEDA_RUNNER_THERMAL_HOLD": "hot"}, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "3", out.stderr)

    def test_a_hot_idle_listener_stops_and_a_busy_one_keeps_its_job(self) -> None:
        self.level = 3
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        stops: list[str] = []
        gate.stop = stops.append  # type: ignore[method-assign]
        gate.child = mock.Mock(poll=mock.Mock(return_value=None))
        gate.busy = lambda: True  # type: ignore[method-assign]
        gate.step(); gate.step(); gate.step()
        self.assertEqual(stops, [])
        gate.busy = lambda: False  # type: ignore[method-assign]
        gate.step(); gate.step()
        self.assertEqual(stops, ["the mini is throttled (thermal pressure level 3)"])

    def test_the_level_parser_reads_notifyutil(self) -> None:
        run = mock.Mock(return_value=mock.Mock(stdout="com.apple.system.thermalpressurelevel 4\n"))
        with mock.patch.object(hook.subprocess, "run", run):
            self.assertEqual(REAL_LEVEL(), 4)
            run.return_value = mock.Mock(stdout="")
            self.assertIsNone(REAL_LEVEL())

    def test_the_gate_asks_lsof_at_most_every_thirty_seconds(self) -> None:
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        with mock.patch.object(hook, "host_waiters", return_value={77}) as lsof:
            self.assertEqual(gate.claimed(), "a fleet build is waiting for the host (pid 77)")
            gate.claimed()
            self.assertEqual(lsof.call_count, 1)
            gate.waiting_at -= hook.GATE_WAITER_EVERY_S
            lsof.return_value = set()
            self.assertIsNone(gate.claimed())
        no_waiters = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state,
                               waiters=False)
        with mock.patch.object(hook, "host_waiters", return_value={77}) as lsof:
            self.assertIsNone(no_waiters.claimed())
            lsof.assert_not_called()

    def test_a_stopped_listener_restarts_after_two_free_polls_and_a_runner_exit_still_exits(self) -> None:
        gate, stops = self.gate(["held", "held", "held", None, None, None])
        gate.step(); gate.step()
        self.assertEqual(stops, ["held"])
        gate.child.poll.return_value = 0  # the listener exited because the gate asked
        with mock.patch.object(hook.subprocess, "Popen") as popen:
            self.assertIsNone(gate.step())  # the listener is gone
            self.assertIsNone(gate.child)
            gate.step()  # still held
            gate.step()  # free once
            popen.assert_not_called()
            gate.step()  # free twice
            popen.assert_not_called()  # but not yet free for GATE_RESTART_FREE_S
            gate.free_since -= hook.GATE_RESTART_FREE_S
            gate.step()
            popen.assert_called_once()
        gate.child = mock.Mock()
        gate.child.poll.return_value = 3  # the runner exited on its own: launchd decides
        self.assertEqual(gate.step(), 3)

    def test_a_stopped_listener_waits_out_a_whole_poll_of_free_host_before_restarting(self) -> None:
        # 2026-10-21, cmux7s roots: started at 14:10:14, 14:50:14 and 14:59:10 after two free polls, each stopped
        # 30 s later by a hold that came back. Every stop of a listener can lose a job GitHub just handed it, so
        # after a stop the host must stay free a whole broker poll before the listener starts again, and a hold
        # inside that span starts the count over.
        gate, stops = self.gate(["held", "held", "held", None, None, "held", None, None, None])
        gate.step(); gate.step()
        gate.child.poll.return_value = 0
        with mock.patch.object(hook.subprocess, "Popen") as popen:
            gate.step()  # the listener is gone
            gate.step()  # still held
            gate.step(); gate.step()  # free twice
            gate.free_since -= hook.GATE_RESTART_FREE_S - 5
            gate.step()  # held again: the free span starts over
            self.assertIsNone(gate.free_since)
            gate.step(); gate.step()
            popen.assert_not_called()
            gate.free_since -= hook.GATE_RESTART_FREE_S
            gate.step()
            popen.assert_called_once()
        self.assertGreaterEqual(hook.GATE_RESTART_FREE_S, hook.BROKER_POLL_S)

    def test_a_gate_held_from_its_start_starts_after_two_free_polls(self) -> None:
        # The free-hold is for a listener the gate stopped; a gate that starts held (boot, a full mini) has stopped
        # nothing, so two free polls still start it.
        gate, _ = self.gate(["held", None, None])
        gate.child = None
        with mock.patch.object(hook.subprocess, "Popen") as popen:
            gate.step(); gate.step(); gate.step()
            popen.assert_called_once()

    def test_a_job_that_slipped_in_is_never_killed_at_the_deadline(self) -> None:
        gate, _ = self.gate([], busy=[True, False])
        gate.stop_deadline = time.monotonic() - 1
        with mock.patch.object(gate, "kill") as kill:
            gate.step()
            kill.assert_not_called()
            gate.step()
            kill.assert_called_once()

    def test_sigterm_stops_a_running_listener_and_ends_a_held_gate(self) -> None:
        gate, stops = self.gate([])
        gate.terminate()
        gate.step()
        self.assertEqual(stops, ["the runner agent is stopping"])
        gate.child.poll.return_value = 0
        self.assertEqual(gate.step(), 0)
        held, _ = self.gate([None, None])
        held.child = None
        held.terminate()
        self.assertEqual(held.step(), 0)

    def test_sigterm_during_the_look_never_starts_a_listener(self) -> None:
        gate, _ = self.gate([])
        gate.child = None
        gate.claimed = lambda: (gate.terminate(), None)[1]
        with mock.patch.object(hook.subprocess, "Popen") as popen:
            self.assertEqual(gate.step(), 0)
            popen.assert_not_called()

    def test_a_gate_bug_hands_the_runner_back_and_never_kills_its_job(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
        gate = hook.Gate(self.runner, os.fspath(self.lock), "", self.state)
        gate.child = child
        self.assertEqual(gate.unexpected(RuntimeError("boom")), 0)  # waited for the child instead
        gate.child = None
        self.assertEqual(gate.unexpected(RuntimeError("boom")), hook.GATE_FALLBACK)
        with mock.patch.object(hook.Gate, "step", side_effect=OSError("ps failed")) as step, \
                mock.patch.object(hook, "GATE_POLL_S", 0):
            self.assertEqual(hook.listen(self.runner, os.fspath(self.lock), "", self.state, True), hook.GATE_FALLBACK)
        self.assertEqual(step.call_count, hook.GATE_MAX_FAILURES)  # a transient failure keeps gating
        flaky = iter([OSError("ps timed out"), 7])
        def step_once(_self):
            item = next(flaky)
            if isinstance(item, Exception):
                raise item
            return item
        with mock.patch.object(hook.Gate, "step", step_once), mock.patch.object(hook, "GATE_POLL_S", 0):
            self.assertEqual(hook.listen(self.runner, os.fspath(self.lock), "", self.state, True), 7)

    def test_a_broken_gate_still_ends_its_runner_when_the_agent_stops(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.kill)
        gate = hook.Gate(self.runner, os.fspath(self.lock), "", self.state)
        gate.child = child
        gate.terminating = True
        with mock.patch.object(gate, "stop", side_effect=OSError("ps failed")):
            self.assertIsNotNone(gate.unexpected(RuntimeError("boom")))  # terminated, not waited on forever

    def test_a_stop_with_no_listener_ends_the_whole_runner_tree(self) -> None:
        (self.runner / "run-helper.sh").write_text("#!/bin/bash\nsleep 30\n")  # between listeners
        gate = hook.Gate(self.runner, os.fspath(self.lock), "", self.state)
        gate.start()
        deadline = time.monotonic() + 10
        while not [p for p, (_, c) in hook.processes().items() if "sleep 30" in c and p in hook.descendants(gate.child.pid, hook.processes())]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        gate.stop("held")
        self.assertIsNotNone(gate.child.wait(timeout=10))
        time.sleep(0.2)
        self.assertEqual([p for p, (_, c) in hook.processes().items() if os.fspath(self.runner) in c], [])

    def test_the_gate_reloads_a_changed_hook_and_keeps_its_runner(self) -> None:
        gate = hook.Gate(self.runner, os.fspath(self.lock), "", self.state)
        gate.child = mock.Mock(pid=4321)
        gate.source = (0, 0, 0)
        # reload() blocks SIGTERM/SIGINT for the exec'd gate to unblock. With execv mocked that block would
        # stay on this test process, and every later child would inherit it (a SIGTERM'd holder never dies).
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, mask)
        with mock.patch.object(hook.os, "execv") as execv, mock.patch.object(hook.sys, "argv", [os.fspath(HOOK), "listen"]):
            gate.reload()
        self.assertIn(signal.SIGTERM, signal.pthread_sigmask(signal.SIG_SETMASK, mask), "blocked across the exec")
        args = execv.call_args[0][1]
        self.assertEqual(args[1:], [os.fspath(HOOK), "listen", "--adopt=4321"])
        gate.source = (0, 0, 0)
        with mock.patch.object(hook.subprocess, "run", return_value=mock.Mock(returncode=2)) as check, \
                mock.patch.object(hook.os, "execv") as execv, \
                mock.patch.object(hook.sys, "argv", [os.fspath(HOOK), "listen", "--no-waiters"]):
            gate.reload()
            execv.assert_not_called()  # a hook that rejects this exact argv is never exec'd
        self.assertEqual(check.call_args[0][0][1:], [os.fspath(HOOK), "listen", "--no-waiters", "--adopt=4321",
                                                     "--parse-only"])

    def test_a_reload_keeps_the_listeners_first_poll(self) -> None:
        # An --apply that lands inside a listener's first poll must not hand the re-executed gate a listener it
        # may stop at once (GATE_FIRST_POLL_S). The start goes in the environment, so argv stays one an older hook
        # (a rollback) accepts.
        gate = hook.Gate(self.runner, os.fspath(self.lock), "", self.state)
        gate.child = mock.Mock(pid=4321)
        gate.source = (0, 0, 0)
        gate.listening_since = time.monotonic() - 5.0
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, mask)
        self.addCleanup(os.environ.pop, hook.GATE_LISTENING_SINCE_ENV, None)
        handed: list[str | None] = []
        with mock.patch.object(hook.os, "execv",
                               side_effect=lambda *_: handed.append(os.environ.get(hook.GATE_LISTENING_SINCE_ENV))), \
                mock.patch.object(hook.sys, "argv", [os.fspath(HOOK), "listen"]):
            gate.reload()
        self.assertEqual(handed, [repr(gate.listening_since)])
        seen: list[float | None] = []
        def step(new: hook.Gate) -> int:
            seen.append(new.listening_since)
            return 0
        with mock.patch.object(hook.Gate, "step", step), mock.patch.object(hook, "Adopted", return_value=mock.Mock()):
            self.assertEqual(hook.listen(self.runner, os.fspath(self.lock), "", self.state, True, adopt=4321), 0)
        self.assertEqual(seen, [gate.listening_since], "the adopted listener keeps its first poll")
        self.assertNotIn(hook.GATE_LISTENING_SINCE_ENV, os.environ, "read once; run.sh's children never see it")

    def test_an_ios_sim_or_session_runner_listens_only_with_room_for_its_costliest_job(self) -> None:
        self.assertNotIn("--fit-units", self.listen_sh(sys.executable).read_text())
        ios_sim = self.listen_sh(sys.executable, ["glaeda-mini", "glaeda-side-std-xcode-26.6", "glaeda-ios-sim"]).read_text()
        self.assertIn(f"listen --runner-dir {self.runner} --fit-units 3 &", ios_sim)
        session = self.listen_sh(sys.executable, ["glaeda-mini", "glaeda-session-std-xcode-26.6"], session=True).read_text()
        self.assertIn(f"listen --runner-dir {self.runner} --fit-units 6 &", session)

        def cost(workflow: str, job: str) -> int:
            return hook.job_units(hook.job_class(job, "manaflow-ai/cmux", workflow=workflow)[0], job,
                                  "manaflow-ai/cmux", workflow=workflow)

        self.assertEqual(max(cost("test-ios.yml", job) for job in
                             ("mobile-core-package", "ios-simulator-build", "ios-simulator", "screenshots")),
                         cr.IOS_SIM_FIT_UNITS, "the costliest job glaeda-ios-sim brings")
        self.assertEqual(max(cost(*key) for key in (
            ("cmux-next.yml", "daemon-test"), ("cmux-next.yml", "generated-files"),
            ("cmux-next-pr-autofix.yml", "action-exports"), ("cmux-next-base-autofix.yml", "action-exports"),
            ("cmux-next-media.yml", "tour"))), cr.SESSION_FIT_UNITS, "the costliest job a session runner takes")
        argv = ["listen", "--runner-dir", os.fspath(self.runner), "--fit-units", "3", "--parse-only"]
        self.assertEqual(subprocess.run([sys.executable, os.fspath(HOOK), *argv], capture_output=True).returncode, 0)
        side = hook.RunnerScope(14, 2, 2, False)
        self.assertEqual(hook.mini_full(self.units(12), 14, side, 3),
                         "only 2 of 14 capacity units on this mini are free; its next job needs 3")
        self.assertIsNone(hook.mini_full(self.units(12), 14, side), "a light-only side runner still fits a light job")
        self.assertIsNone(hook.mini_full(self.units(11), 14, side, 3))
        self.runner_hook(14)
        (self.runner / hook.RUNNER_HOOK_SCRIPT).write_text(
            "exec python3 /hook job-started --capacity-units 14 --canonical-roots 2 --instance 3\n")
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state,
                         capacity_dir=self.units(12), fit_units=3)
        self.assertEqual(gate.claimed(), "only 2 of 14 capacity units on this mini are free; its next job needs 3")
        # a gate started from an older listen.sh (no flag in its argv) reads the flag from the current one
        old = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state,
                        capacity_dir=self.units(12))
        self.assertIsNone(old.claimed())
        (self.runner / hook.LISTEN_SCRIPT).write_text(ios_sim)
        self.assertEqual(hook.listen_fit_units(self.runner), 3)
        self.assertEqual(old.claimed(), "only 2 of 14 capacity units on this mini are free; its next job needs 3")
        # a session runner with 5 cores free stays off: generated-files or action exports would wait here for a sixth
        (self.runner / hook.RUNNER_HOOK_SCRIPT).write_text(
            "exec python3 /hook job-started --capacity-units 14 --canonical-roots 2 --instance 5 --session-runner\n")
        (self.runner / hook.LISTEN_SCRIPT).write_text(session)
        five = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state,
                         capacity_dir=self.units(9))
        self.assertEqual(five.claimed(), "only 5 of 14 capacity units on this mini are free; its next job needs 6")
        self.assertIsNone(hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state,
                                    capacity_dir=self.units(8)).claimed(), "with 6 free it listens")

    def test_parse_only_accepts_the_gate_argv_and_an_old_hook_would_not(self) -> None:
        argv = ["listen", "--runner-dir", os.fspath(self.runner), "--no-waiters", "--adopt=1", "--parse-only"]
        self.assertEqual(subprocess.run([sys.executable, os.fspath(HOOK), *argv], capture_output=True).returncode, 0)
        old = self.tmp / "old-hook"
        old.write_text(HOOK.read_text().replace('p.add_argument("--parse-only"', 'p.add_argument("--renamed"'))
        self.assertEqual(subprocess.run([sys.executable, os.fspath(old), *argv], capture_output=True).returncode, 2)

    def run_listen(self, *extra: str) -> tuple[subprocess.Popen, Path]:
        # The hook runs in its own process, so the setUp patch of the thermal level does not reach it.
        # No first-poll hold (GATE_FIRST_POLL_S) and no free span after a stop (GATE_RESTART_FREE_S): these tests
        # stop a listener the moment a claim confirms and restart it once two polls are free.
        env = {**os.environ, "GLAEDA_RUNNER_GATE_POLL_S": "0.1", "GLAEDA_RUNNER_THERMAL_HOLD": "0",
               "GLAEDA_RUNNER_GATE_FIRST_POLL_S": "0", "GLAEDA_RUNNER_GATE_RESTART_FREE_S": "0"}
        log = self.tmp / "runner.log"
        with log.open("wb") as out:
            proc = subprocess.Popen([*extra] if extra else
                                    [sys.executable, os.fspath(HOOK), "listen", "--runner-dir", os.fspath(self.runner),
                                     "--host-lock", os.fspath(self.lock), "--reservation",
                                     os.fspath(self.tmp / "none.json"), "--state-dir", os.fspath(self.state)],
                                    stdout=out, stderr=subprocess.STDOUT, env=env)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc, log

    def wait_for(self, log: Path, text: str, count: int = 1) -> None:
        deadline = time.monotonic() + 15
        while log.read_text().count(text) < count:
            self.assertLess(time.monotonic(), deadline, log.read_text())
            time.sleep(0.05)

    def diag(self, text: str, name: str = "Runner_20260928-135151-utc.log") -> Path:
        diag = self.runner / "_diag"
        diag.mkdir(exist_ok=True)
        (diag / name).write_text(text)
        return diag / name

    def started_gate(self) -> hook.Gate:
        """A real gate over the fake listener, held for a full mini, past its first poll (GATE_FIRST_POLL_S)."""
        patcher = mock.patch.object(hook, "GATE_FIRST_POLL_S", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        gate = hook.Gate(self.runner, os.fspath(self.lock), "", self.state)
        gate.reload = lambda: None  # type: ignore[method-assign]
        gate.claimed = lambda: "all 2 canonical roots on this mini are taken"  # type: ignore[method-assign]
        gate.confirmed = gate.claimed  # type: ignore[method-assign]
        gate.start()
        self.addCleanup(gate.kill)
        deadline = time.monotonic() + 10
        while not self.listeners():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        return gate

    def assert_listening(self) -> None:
        listeners = self.listeners()
        self.assertEqual(len(listeners), 1, "the listener was signalled")
        stat = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(listeners[0])], capture_output=True, text=True)
        self.assertFalse(stat.stdout.strip().startswith("T"), "the listener was left frozen")

    def test_an_idle_stop_never_cancels_a_job_the_listener_is_taking(self) -> None:
        # mini-5 2026-09-28: a stop between the listener's acknowledge and its acquirejob orphaned a cmux job
        log = self.diag(MID_ACQUIRE_DIAG)
        gate = self.started_gate()
        for _ in range(4):
            self.assertIsNone(gate.step())
        self.assert_listening()
        self.assertIsNone(gate.stop_deadline)
        log.write_text(log.read_text() + ACQUIRE_FAILED)  # the acquisition ended: the listener is idle again
        gate.step(); gate.step()
        self.assertIsNotNone(gate.stop_deadline)
        self.assertIsNotNone(gate.child.wait(timeout=10))

    def test_the_frozen_last_look_keeps_a_listener_that_acknowledged_after_the_poll(self) -> None:
        gate = self.started_gate()
        log = self.diag(MID_ACQUIRE_DIAG)  # the acknowledge lands after step()'s busy look, before the signal
        self.assertFalse(gate.stop("all 2 canonical roots on this mini are taken"))
        self.assert_listening()
        self.assertIsNone(gate.stop_deadline)
        log.write_text(log.read_text() + ACQUIRE_FAILED)
        self.assertTrue(gate.stop("all 2 canonical roots on this mini are taken"))
        self.assertIsNotNone(gate.child.wait(timeout=10))

    def test_the_agent_stopping_waits_out_an_acquisition_but_not_a_running_job(self) -> None:
        log = self.diag(MID_ACQUIRE_DIAG)
        gate = self.started_gate()
        gate.terminate()
        for _ in range(3):
            self.assertIsNone(gate.step())
        self.assert_listening()
        self.assertIsNone(gate.stop_deadline)
        # dispatched with a Worker running: launchd is stopping the agent, so the job is cancelled as before
        log.write_text(log.read_text() + "[2026-09-28 13:53:04Z INFO JobDispatcher] Job request 0 for plan p job "
                       "eeb83692-303d-516a-b3bf-e6e6f93c1d90 received.\n")
        with mock.patch.object(hook.Gate, "worker", return_value=True):
            gate.step()
        self.assertIsNotNone(gate.stop_deadline)
        self.assertIsNotNone(gate.child.wait(timeout=10))

    def test_the_listener_request_reads_the_newest_runner_diag(self) -> None:
        self.assertIsNone(hook.listener_request(self.runner), "no _diag yet")
        self.diag(MID_ACQUIRE_DIAG)
        self.assertEqual(hook.listener_request(self.runner), "eeb83692-303d-516a-b3bf-e6e6f93c1d90")
        self.diag("", "Worker_20260928-135500-utc.log")  # a job's own log is not the listener's
        self.assertEqual(hook.listener_request(self.runner), "eeb83692-303d-516a-b3bf-e6e6f93c1d90")
        for end in (ACQUIRE_FAILED,
                    "[x INFO Runner] Skipping message Job. 409 already acquired\n",
                    "[x INFO JobDispatcher] Job request 0 for plan p job eeb83692 received.\n"
                    "[x INFO JobDispatcher] Worker finished for job eeb83692. Code: 100\n",
                    "[x INFO JobDispatcher] Job request 0 for plan p job eeb83692 received.\n"
                    "[x INFO JobDispatcher] Stop renew job request for job eeb83692.\n",  # cancelled before its Worker
                    "[x INFO JobDispatcher] Job request 0 for plan p job eeb83692 received.\n"
                    "[x INFO JobDispatcher] Unable to renew job request for job eeb83692 for the first time, stop "
                    "dispatching job to worker.\n",
                    "[x INFO Listener] Runner execution has finished with return code 0\n"):
            self.diag(MID_ACQUIRE_DIAG + end)
            self.assertIsNone(hook.listener_request(self.runner), end)
        self.diag(MID_ACQUIRE_DIAG + "[x INFO JobDispatcher] Job request 0 for plan p job eeb83692 received.\n")
        self.assertEqual(hook.listener_request(self.runner), "eeb83692", "dispatched, its Worker not yet done")
        self.diag(MID_ACQUIRE_DIAG + ACQUIRE_FAILED)
        self.diag("[x INFO BrokerMessageListener] Acknowledging runner request 'next'.\n",
                  "Runner_20260928-140000-utc.log")  # a later listener's file is the one it reads
        self.assertEqual(hook.listener_request(self.runner), "next")

    def test_listen_stops_and_restarts_a_real_listener(self) -> None:
        proc, log = self.run_listen()
        self.wait_for(log, "Listening for Jobs")
        self.assertEqual(len(self.listeners()), 1)
        fd = os.open(self.lock, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.wait_for(log, "holding the listener off: a fleet build holds the host lock")
        self.assertEqual(self.listeners(), [])
        self.assertIn("stopping the idle listener: a fleet build holds the host lock", log.read_text())
        self.assertIsNone(proc.poll())
        os.close(fd)
        self.wait_for(log, "the host is free again")
        self.wait_for(log, "Listening for Jobs", 2)
        proc.terminate()
        self.assertEqual(proc.wait(timeout=15), 0)
        self.assertEqual(self.listeners(), [])

    def test_listen_leaves_a_runner_with_a_job_alone(self) -> None:
        worker = self.runner / "bin/Runner.Worker"
        worker.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n")
        worker.chmod(0o755)
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        gate.child = subprocess.Popen([sys.executable, "-c", f"import subprocess,sys,time; subprocess.run([sys.executable, {os.fspath(worker)!r}]); time.sleep(60)"])
        self.addCleanup(gate.child.kill)
        deadline = time.monotonic() + 10
        while not gate.busy():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        self.hold(fcntl.LOCK_EX)
        with mock.patch.object(gate, "stop") as stop:
            for _ in range(4):
                gate.step()
            stop.assert_not_called()
            for pid in hook.descendants(gate.child.pid, hook.processes()):
                os.kill(pid, 9)
            deadline = time.monotonic() + 10
            while gate.busy():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            gate.step(); gate.step()
            stop.assert_called_once_with("a fleet build holds the host lock")

    def listen_sh(self, python: str, labels: list[str] | None = None, session: bool = False,
                  home: Path | None = None) -> Path:
        ctx = mock.Mock(python=python, hook_dir=HOOK.parent, runner_dir=self.runner, org=None, repo="manaflow-ai/cmux",
                        min_free_gib=0, member=None, capacity_units=4, labels=labels or ["glaeda-mini"],
                        session_runner=session, **({"home": home} if home else {}))
        script = self.tmp / "listen.sh"
        script.write_bytes(cr.hook_wrappers(ctx)["listen.sh"])
        script.chmod(0o755)
        return script

    def test_listen_sh_clears_sdkroot_and_pins_the_pool_xcode(self) -> None:
        text = self.listen_sh(sys.executable).read_text()
        self.assertIn("unset SDKROOT TOOLCHAINS\n", text)
        self.assertNotIn("DEVELOPER_DIR", text, "a runner with no pool Xcode keeps the host's")
        ctx = mock.Mock(python=sys.executable, hook_dir=HOOK.parent, runner_dir=self.runner, org="manaflow-ai",
                        repo=None, min_free_gib=0, capacity_units=4, labels=["glaeda-mini"], session_runner=False,
                        member={"hardware": "m6-16", "xcodeApps": ["/Applications/Xcode_26.6.app"]})
        text = cr.hook_wrappers(ctx)["listen.sh"].decode()
        self.assertIn("export DEVELOPER_DIR=/Applications/Xcode_26.6.app/Contents/Developer\n", text)
        self.assertLess(text.index("unset SDKROOT"), text.index("listen --runner-dir"))
        self.assertLess(text.index("unset SDKROOT"), text.index("exec ./run.sh"), "the fallback run.sh is clean too")

    def test_hooks_prefer_a_python_that_is_not_the_xcrun_shim(self) -> None:
        # a stand-in shim, so the test means the same on a Mac (where /usr/bin/python3 is the shim) and on Linux CI
        brew, shim = self.tmp / "brew-python3", self.tmp / "shim-python3"
        make_executable(brew, "#!/bin/sh\n")
        make_executable(shim, "#!/bin/sh\n")
        with mock.patch.object(cr, "XCRUN_SHIM_PYTHON", os.path.realpath(shim)):
            with mock.patch.object(cr.shutil, "which", return_value=os.fspath(shim)), \
                    mock.patch.object(cr, "HOOK_PYTHONS", (os.fspath(brew), os.fspath(shim))):
                self.assertEqual(cr.hook_python(), os.fspath(brew))
            with mock.patch.object(cr.shutil, "which", return_value="/opt/custom/bin/python3"):
                self.assertEqual(cr.hook_python(), "/opt/custom/bin/python3", "a python3 on PATH that is not the shim")
            with mock.patch.object(cr.shutil, "which", return_value=None), \
                    mock.patch.object(cr, "HOOK_PYTHONS", ("/nonexistent/python3", os.fspath(shim))):
                self.assertEqual(cr.hook_python(), os.path.realpath(shim), "the shim only when nothing else runs")

    def test_listen_sh_runs_the_runner_without_a_gate_that_cannot_start(self) -> None:
        (self.runner / "run.sh").write_text(f"#!/bin/bash\necho plain run.sh > {self.tmp / 'ran'}\nexit 0\n")
        result = subprocess.run([os.fspath(self.listen_sh("/nonexistent/python3"))], capture_output=True, text=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("running the runner without it", result.stdout)
        self.assertEqual((self.tmp / "ran").read_text(), "plain run.sh\n")

    def test_listen_sh_leaves_a_deleted_registration_to_the_gate(self) -> None:
        (self.runner / "run.sh").write_text(f"#!/bin/bash\necho plain run.sh > {self.tmp / 'ran'}\nexit 0\n")
        record = self.tmp / ".local/state/glaeda/cmux-runner/registration-actions-runner-glaeda.json"
        record.parent.mkdir(parents=True)
        record.write_text('{"cause": "registration_deleted"}')
        result = subprocess.run([os.fspath(self.listen_sh("/nonexistent/python3", home=self.tmp))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("deleted this runner's registration", result.stdout)
        self.assertFalse((self.tmp / "ran").exists(), "a runner GitHub deleted is never retried without the backoff")
        (self.runner / ".runner").write_text("{}")
        later = time.time() + 60
        os.utime(self.runner / ".runner", (later, later))  # re-registered since the record
        result = subprocess.run([os.fspath(self.listen_sh("/nonexistent/python3", home=self.tmp))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.tmp / "ran").read_text(), "plain run.sh\n")

    def deleted(self, name: str = "mini-a-glaeda") -> None:
        """The listener's _diag trace and .runner as a deleted registration leaves them."""
        (self.runner / ".runner").write_text("\ufeff" + json.dumps({"agentName": name}), encoding="utf-8")
        (self.runner / "_diag").mkdir(exist_ok=True)
        (self.runner / "_diag/Runner_20261024-010000-utc.log").write_text(
            "[2026-10-24 01:00:01Z ERR  Runner] GitHub.DistributedTask.WebApi.TaskAgentNotFoundException: "
            "Registration was not found\n")

    def test_a_deleted_registration_is_recorded_and_retried_only_after_the_backoff(self) -> None:
        # 2026-10-24 01:00Z: 36 org runners lost their registrations while the gate held them offline; each
        # listen.sh then restarted its listener about 33k times, and nothing said why.
        gate, _ = self.gate([None] * 20)
        with mock.patch.object(hook.subprocess, "Popen", return_value=gate.child) as popen, \
                mock.patch.object(hook, "processes", return_value={}):
            gate.start()
            self.deleted()
            gate.child.poll.return_value = 1
            self.assertIsNone(gate.step(), "the gate keeps the runner instead of handing launchd a restart loop")
            record = json.loads(hook.registration_file(self.state, self.runner).read_text())
            self.assertEqual((record["cause"], record["runner"], record["exits"]),
                             ("registration_deleted", "mini-a-glaeda", 1))
            self.assertAlmostEqual(record["retry_after"], time.time() + hook.GATE_REGISTRATION_RETRY_S, delta=5)
            self.assertGreaterEqual(hook.GATE_REGISTRATION_RETRY_S, 600)
            for _ in range(3):
                gate.step()
            self.assertEqual(popen.call_count, 1, "no restart inside the backoff")
            fresh = hook.Gate(self.runner, os.fspath(self.lock), "", self.state)
            self.assertTrue(fresh.registration_wait(), "a restarted gate keeps the backoff")
            gate.registration["retry_after"] = time.time() - 1
            gate.step()
            self.assertEqual(popen.call_count, 2, "after the backoff it tries once more")
            gate.child.poll.return_value = 1
            gate.step()
            self.assertEqual(json.loads(hook.registration_file(self.state, self.runner).read_text())["exits"], 2)

    def test_a_reconfigured_runner_listens_at_once_and_a_listener_that_stays_up_clears_the_record(self) -> None:
        gate, _ = self.gate([None] * 20)
        with mock.patch.object(hook.subprocess, "Popen", return_value=gate.child) as popen, \
                mock.patch.object(hook, "processes", return_value={}):
            gate.start()
            self.deleted()
            gate.child.poll.return_value = 1
            gate.step()
            os.utime(self.runner / ".runner", (time.time() + 60, time.time() + 60))  # config.sh re-registered it
            gate.child = mock.Mock(pid=1)
            gate.child.poll.return_value = None
            popen.return_value = gate.child
            gate.child = None
            gate.step()
            self.assertEqual(popen.call_count, 2)
            gate.step()
            self.assertTrue(hook.registration_file(self.state, self.runner).exists(), "kept until it stays up")
            gate.listening_since -= hook.GATE_REGISTRATION_OK_S
            gate.step()
            self.assertFalse(hook.registration_file(self.state, self.runner).exists())
            self.assertIsNone(gate.registration)

    def test_a_job_left_behind_keeps_the_exit_with_launchd(self) -> None:
        gate, _ = self.gate([None] * 4)
        with mock.patch.object(hook.subprocess, "Popen", return_value=gate.child):
            gate.start()
            self.deleted()
            gate.child.poll.return_value = 1
            worker = os.fspath(self.runner / "bin/Runner.Worker")
            with mock.patch.object(hook, "processes", return_value={77: (1, f"{worker} spawnclient 3 4")}):
                self.assertEqual(gate.step(), 1, "an orphaned Worker still runs: no backoff beside its job")
        self.assertFalse(hook.registration_file(self.state, self.runner).exists())

    def test_a_self_updated_runners_worker_is_its_job(self) -> None:
        # a runner that updated itself spawns bin.<version>/Runner.Worker (bin is a symlink to it)
        gate, _ = self.gate([None] * 4)
        with mock.patch.object(hook.subprocess, "Popen", return_value=gate.child):
            gate.start()
            gate.child.poll.return_value = 1
            worker = os.fspath(self.runner / "bin.2.338.0/Runner.Worker")
            with mock.patch.object(hook, "processes", return_value={77: (1, f"{worker} spawnclient 148 151")}):
                self.assertTrue(gate.job_left())
            with mock.patch.object(hook, "processes", return_value={77: (1, f"{self.runner}-2/bin.2.338.0/Runner.Worker")}):
                self.assertFalse(gate.job_left(), "another instance's Worker is not this runner's job")

    def test_an_adopted_listener_never_reads_a_diag_log_from_before_the_reload(self) -> None:
        # a reload's exec adopts run.sh: an exit after it must not match an older start's deletion trace
        self.deleted()
        old = time.time() - 600
        os.utime(self.runner / "_diag/Runner_20261024-010000-utc.log", (old, old))
        seen = []
        def step(gate):
            seen.append(gate)
            return 0
        with mock.patch.object(hook.Gate, "step", step), mock.patch.object(hook.signal, "signal"), \
                mock.patch.object(hook.signal, "pthread_sigmask"), mock.patch.dict(os.environ, {}):
            hook.listen(self.runner, os.fspath(self.lock), "", self.state, False, adopt=os.getpid())
        [gate] = seen
        self.assertGreater(gate.started_wall, old)
        self.assertIsNone(hook.registration_deleted(self.runner, gate.started_wall))
        self.assertIsNotNone(hook.registration_deleted(self.runner, 0.0), "only the since bound keeps it out")

    def test_any_other_exit_still_goes_to_launchd(self) -> None:
        gate, _ = self.gate([None] * 4)
        with mock.patch.object(hook.subprocess, "Popen", return_value=gate.child):
            gate.start()
            (self.runner / "_diag").mkdir(exist_ok=True)
            (self.runner / "_diag/Runner_1.log").write_text("Runner execution has finished\n")
            gate.child.poll.return_value = 2
            self.assertEqual(gate.step(), 2)
        self.assertFalse(hook.registration_file(self.state, self.runner).exists())

    def test_listen_sh_never_starts_a_second_runner_beside_a_live_one(self) -> None:
        script = self.listen_sh(sys.executable)
        proc, log = self.run_listen(os.fspath(script))
        self.wait_for(log, "Listening for Jobs")
        gate = [pid for pid, (_, cmd) in hook.processes().items()
                if "listen --runner-dir" in cmd and os.fspath(self.runner) in cmd and "listen.sh" not in cmd]
        self.assertEqual(len(gate), 1, gate)
        os.kill(gate[0], 9)  # jetsam, or a re-exec into a hook that cannot run this gate
        self.assertEqual(proc.wait(timeout=30), 1)  # launchd restarts a fresh gate
        self.assertIn("with its runner running", log.read_text())
        self.assertNotIn("running the runner without it", log.read_text())
        self.assertEqual(log.read_text().count("Listening for Jobs"), 1)
        self.assertEqual(self.listeners(), [])

    def test_listen_sh_fallback_never_stops_a_listener_taking_a_job(self) -> None:
        # the fallback asks the hook (exit 3: idle) with the listener frozen, so a request it took keeps it
        script = self.listen_sh(sys.executable)
        self.assertIn("busy --runner-dir", script.read_text())
        proc, log = self.run_listen(os.fspath(script))
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        self.wait_for(log, "Listening for Jobs")
        diag = self.diag(MID_ACQUIRE_DIAG, "Runner_29990101-000000-utc.log")  # the newest diag
        gate = [pid for pid, (_, cmd) in hook.processes().items()
                if "listen --runner-dir" in cmd and os.fspath(self.runner) in cmd and "listen.sh" not in cmd]
        os.kill(gate[0], 9)
        self.wait_for(log, "with its runner running")
        time.sleep(7)  # past one pass of the fallback's loop
        self.assertIsNone(proc.poll(), "taking a job: the fallback keeps the listener")
        self.assertTrue(self.listeners())
        diag.write_text(diag.read_text() + ACQUIRE_FAILED)  # the acquisition ended: idle
        self.assertEqual(proc.wait(timeout=30), 1)
        self.assertEqual(self.listeners(), [])

    def test_a_request_left_by_a_listener_that_is_gone_is_no_job_for_the_gate(self) -> None:
        self.diag(MID_ACQUIRE_DIAG)
        gate = hook.Gate(self.runner, os.fspath(self.lock), os.fspath(self.tmp / "none.json"), self.state)
        self.assertFalse(gate.busy(), "no listener, no Worker: a stale acknowledge holds nothing")
        with mock.patch.object(gate, "listening", return_value=True):
            self.assertTrue(gate.busy(), "with its listener alive it is a job being taken")

    def test_listen_sh_forwards_sigterm_and_leaves_nothing_behind(self) -> None:
        script = self.listen_sh(sys.executable)
        proc, log = self.run_listen(os.fspath(script))
        self.wait_for(log, "Listening for Jobs")
        self.assertIn("--runner-dir", script.read_text())
        self.assertNotIn("--no-waiters", script.read_text())
        proc.terminate()
        self.assertEqual(proc.wait(timeout=15), 0)
        time.sleep(0.3)
        self.assertEqual(self.listeners(), [])
        self.assertNotIn("without it", log.read_text())


class RunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.home = base / "operator"
        self.home.mkdir()
        self.state = base / "fake"
        self.state.mkdir()
        self.bin = base / "bin"
        self.bin.mkdir()
        self.gh = make_executable(self.bin / "gh", FAKE_GH)
        self.curl = make_executable(self.bin / "curl", FAKE_CURL)
        self.launchctl = make_executable(self.bin / "launchctl", FAKE_LAUNCHCTL)
        self.build_release()
        self.patches = [
            mock.patch.dict(os.environ, {"HOME": os.fspath(self.home), "FAKE_STATE": os.fspath(self.state),
                                         "FAKE_REG_TOKEN": REG_TOKEN, "FAKE_REMOVE_TOKEN": REMOVE_TOKEN}),
            mock.patch.object(cr, "CURL", os.fspath(self.curl)),
            mock.patch.object(cr, "LAUNCHCTL", os.fspath(self.launchctl)),
            mock.patch.object(cr, "default_name", lambda: "mini-test-glaeda"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def build_release(self, sha_override: str | None = None) -> None:
        stage = self.state / "stage"
        stage.mkdir(exist_ok=True)
        make_executable(stage / "config.sh", FAKE_CONFIG)
        make_executable(stage / "run.sh", "#!/bin/bash\nexit 0\n")
        tarball = self.state / "runner.tar.gz"
        with tarfile.open(tarball, "w:gz") as tar:
            for name in ("config.sh", "run.sh"):
                tar.add(stage / name, arcname=name)
        sha = sha_override or hashlib.sha256(tarball.read_bytes()).hexdigest()
        asset = f"actions-runner-osx-arm64-{VERSION}.tar.gz"
        (self.state / "release.json").write_text(json.dumps({
            "tag_name": f"v{VERSION}",
            "body": f"notes\n<!-- BEGIN SHA osx-arm64 -->{sha}<!-- END SHA osx-arm64 -->\n",
            "assets": [{"name": asset, "browser_download_url": f"https://github.com/actions/runner/releases/download/v{VERSION}/{asset}"}],
        }))

    def invoke(self, *args: str, expect: int = 0, via_setup: bool = False, gh: bool = True,
               stdin: str = "") -> dict:
        out = io.StringIO()
        argv = ["--output", "json", *(["--gh", os.fspath(self.gh)] if gh else []), "--python", sys.executable, *args]
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            code = setup.main(["--runner", *argv]) if via_setup else cr.main(argv)
        self.assertEqual(code, expect, out.getvalue()[-3000:])
        self.last_output = out.getvalue()
        return json.loads(out.getvalue())

    def log(self) -> list[dict]:
        path = self.state / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def tree(self) -> dict[str, bytes | None]:
        return {os.fspath(p.relative_to(self.home)): (p.read_bytes() if p.is_file() else None)
                for p in sorted(self.home.rglob("*"))}

    def by_kind(self, receipt: dict) -> dict[str, dict]:
        return {a["kind"] + (":" + Path(a["path"]).name if a["kind"] == "hook" else ""): a for a in receipt["actions"]}

    # ------------------------------------------------------------ plan

    def test_plan_is_side_effect_free_and_prints_routing(self) -> None:
        receipt = self.invoke()
        self.assertFalse(receipt["applied"])
        self.assertEqual(self.tree(), {})
        for entry in self.log():
            self.assertEqual(entry["tool"], "gh", entry)
            self.assertNotIn("-X", entry["argv"], entry)
        kinds = self.by_kind(receipt)
        self.assertEqual(kinds["download"]["state"], "create")
        self.assertEqual(kinds["download"]["version"], VERSION)
        self.assertEqual(kinds["register"]["labels"], ["self-hosted", "macOS", "ARM64", "glaeda-mini"])
        self.assertFalse(kinds["register"]["ephemeral"])
        self.assertIn("gh variable set MACOS_RUNNER_15 --body glaeda-mini --repo manaflow-ai/cmux",
                      receipt["routing"]["route"])
        self.assertIn("gh variable delete MACOS_RUNNER_15 --repo manaflow-ai/cmux", receipt["routing"]["rollback"])
        human = io.StringIO()
        with contextlib.redirect_stdout(human):
            cr.main(["--gh", os.fspath(self.gh), "--python", sys.executable])
        self.assertIn("gh variable set MACOS_RUNNER_26 --body glaeda-mini --repo manaflow-ai/cmux", human.getvalue())
        self.assertEqual(self.tree(), {})

    def test_mini_setup_runner_flag_delegates(self) -> None:
        receipt = self.invoke(via_setup=True)
        self.assertEqual(receipt["schema"], "glaeda-cmux-runner/v1")
        self.assertEqual(self.tree(), {})

    # ------------------------------------------------------------ apply

    # ------------------------------------------------------------ hook refresh (glaeda-update's rollout)

    OLDER_HOOK = HOOK.read_text() + "\n# an older hook\n"

    def hook_history(self, *hooks: str) -> None:
        """A release's hook-history.json beside this copy, naming these hook versions (and today's module)."""
        path = self.state / "hook-history.json"
        path.write_text(json.dumps({
            cr.HOOK_NAME: [hashlib.sha256(h.encode()).hexdigest() for h in hooks],
            cr.RESERVATION_NAME: [hashlib.sha256((ROOT / "scripts" / cr.RESERVATION_NAME).read_bytes()).hexdigest()]}))
        patch = mock.patch.object(cr, "HOOK_HISTORY", path)
        patch.start()
        self.addCleanup(patch.stop)

    def installed_with_an_older_hook(self, *apply_args: str) -> Path:
        self.hook_history(self.OLDER_HOOK)
        self.invoke("--apply", "--labels", "cmux-extra", *apply_args)
        hooks = self.home / "actions-runner-glaeda" / cr.HOOK_DIR
        (hooks / cr.HOOK_NAME).write_text(self.OLDER_HOOK)
        return hooks

    def test_refresh_hooks_installs_only_the_hook_files(self) -> None:
        self.hook_history(self.OLDER_HOOK)
        self.invoke("--apply", "--labels", "cmux-extra")
        hooks = self.home / "actions-runner-glaeda" / cr.HOOK_DIR
        as_applied, calls = self.tree(), len(self.log())
        (hooks / cr.HOOK_NAME).write_text(self.OLDER_HOOK)
        (hooks / cr.RESERVATION_NAME).chmod(0o600)
        plan = self.invoke("--refresh-hooks")
        self.assertEqual([(r["state"], sorted(r["files"])) for r in plan["runners"]],
                         [("update", sorted([cr.HOOK_NAME, cr.RESERVATION_NAME]))])
        self.assertEqual((hooks / cr.HOOK_NAME).read_text(), self.OLDER_HOOK, "a plan writes nothing")
        done = self.invoke("--refresh-hooks", "--apply")
        self.assertEqual([r["state"] for r in done["runners"]], ["updated"])
        self.assertEqual(done["hookSha256"], cr.sha256_bytes(HOOK.read_bytes()))
        # the hook and module are this copy's again; wrappers, plist, receipt are as --apply left them
        self.assertEqual({k: v for k, v in self.tree().items() if not k.endswith("runner.lock")},
                         {k: v for k, v in as_applied.items() if not k.endswith("runner.lock")})
        self.assertEqual(os.stat(hooks / cr.HOOK_NAME).st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(hooks / cr.RESERVATION_NAME).st_mode & 0o777, 0o644)
        self.assertEqual(len(self.log()), calls, "no gh, launchctl or config.sh")
        self.assertEqual([r["state"] for r in self.invoke("--refresh-hooks", "--apply")["runners"]], ["unchanged"])

    def test_refresh_hooks_never_downgrades_a_hook_it_does_not_know(self) -> None:
        hooks = self.installed_with_an_older_hook()
        newer = HOOK.read_text() + "\n# from main, newer than this release\n"
        (hooks / cr.HOOK_NAME).write_text(newer)
        kept = self.invoke("--refresh-hooks", "--apply")["runners"][0]
        self.assertEqual(kept["state"], "kept")
        self.assertEqual((hooks / cr.HOOK_NAME).read_text(), newer)
        # a copy with no release history beside it (a checkout) refuses outright
        with mock.patch.object(cr, "HOOK_HISTORY", self.state / "absent.json"):
            self.assertIn("error", self.invoke("--refresh-hooks", "--apply", expect=1))

    def test_refresh_hooks_swaps_a_busy_runner_in_the_gap_between_two_jobs(self) -> None:
        """2026-10-21: four runners (cmux10s and cmux11s glaeda-11, cmux12s glaeda-3 and -4) ran jobs back to back and
        had one at every 10-minute refresh, so they kept a hook two releases old for hours: each run looked once and
        deferred. A deferred runner is now watched for REFRESH_GAP_WAIT_S and swapped in its first gap between jobs."""
        hooks = self.installed_with_an_older_hook()
        looks = {"n": 0}

        def busy_for_three_looks(_runner_dir):
            looks["n"] += 1
            return looks["n"] <= 3  # the run's first look and two more polls see a job; then the gap

        with mock.patch.object(cr, "worker_running", side_effect=busy_for_three_looks), \
                mock.patch.object(cr.time, "sleep"):
            done = self.invoke("--refresh-hooks", "--apply")["runners"][0]
        self.assertEqual(done["state"], "updated", done)
        self.assertEqual((hooks / cr.HOOK_NAME).read_bytes(), HOOK.read_bytes())

    def test_a_busy_runner_gets_its_hook_staged_and_job_completed_installs_it_between_jobs(self) -> None:
        """A runner with a job at every refresh still goes idle between jobs, and that is when its old gate runs. So
        a refresh that finds a job stages the checked new hook beside the old one (.<name>.next, with a record of the
        hook it replaces), and the job's own job-completed installs it: the next job-started runs the new file whole,
        and the gate sees the change and reloads."""
        hooks = self.installed_with_an_older_hook()
        older = (hooks / cr.HOOK_NAME).read_bytes()
        (hooks / cr.RESERVATION_NAME).chmod(0o600)  # the module is stale too: it is staged and installed with the hook
        clock = {"t": 0.0}
        with mock.patch.object(cr, "worker_running", return_value=True), \
                mock.patch.object(cr.time, "sleep", side_effect=lambda s: clock.__setitem__("t", clock["t"] + s)), \
                mock.patch.object(cr.time, "monotonic", side_effect=lambda: clock["t"]):
            deferred = self.invoke("--refresh-hooks", "--apply")["runners"][0]
        self.assertEqual(sorted(deferred["files"]), sorted([cr.HOOK_NAME, cr.RESERVATION_NAME]))
        self.assertTrue((hooks / f".{cr.RESERVATION_NAME}{cr.NEXT_SUFFIX}").is_file())
        self.assertIn("module", json.loads((hooks / cr.NEXT_RECORD).read_text()))
        self.assertEqual(deferred["state"], "deferred")
        self.assertTrue(deferred.get("staged"), deferred)
        self.assertEqual((hooks / cr.HOOK_NAME).read_bytes(), older, "never swapped under a running job")
        staged = hooks / f".{cr.HOOK_NAME}{cr.NEXT_SUFFIX}"
        self.assertEqual(staged.read_bytes(), HOOK.read_bytes())
        self.assertEqual(os.stat(staged).st_mode & 0o777, 0o755)
        self.assertEqual((cr.NEXT_SUFFIX, cr.NEXT_RECORD), (hook.NEXT_SUFFIX, hook.NEXT_RECORD), "one name on both sides")
        # job-completed, at the end of that job
        note = hook.install_staged_hook(hooks)
        self.assertIn("installed", note)
        self.assertEqual((hooks / cr.HOOK_NAME).read_bytes(), HOOK.read_bytes())
        self.assertEqual(os.stat(hooks / cr.HOOK_NAME).st_mode & 0o777, 0o755)
        self.assertFalse(staged.exists())
        self.assertFalse((hooks / cr.NEXT_RECORD).exists())
        self.assertEqual(os.stat(hooks / cr.RESERVATION_NAME).st_mode & 0o777, 0o644, "the staged module went in too")
        self.assertFalse((hooks / f".{cr.RESERVATION_NAME}{cr.NEXT_SUFFIX}").exists())
        self.assertIsNone(hook.install_staged_hook(hooks), "nothing staged: nothing to do")
        self.assertEqual(self.invoke("--refresh-hooks", "--apply")["runners"][0]["state"], "unchanged")

    def test_the_gap_wait_is_one_budget_per_run_however_many_runners_are_busy(self) -> None:
        """glaeda-update runs --refresh-hooks every 10 minutes: busy runners share one REFRESH_GAP_WAIT_S, watched
        together, so a mini with N busy runners never stretches a run to N x 240 s."""
        hooks = self.installed_with_an_older_hook()
        self.invoke("--apply", "--labels", "cmux-extra", "--instance", "1")
        self.invoke("--apply", "--labels", "cmux-extra", "--instance", "2")
        for k in (1, 2):
            (self.home / f"actions-runner-glaeda-{k}" / cr.HOOK_DIR / cr.HOOK_NAME).write_text(self.OLDER_HOOK)
        clock = {"t": 0.0}
        # only the gap poll moves the clock: a subprocess wait's own short sleeps (the --parse-only checks) do not
        poll = lambda s: clock.__setitem__("t", clock["t"] + s) if s == cr.REFRESH_GAP_POLL_S else None  # noqa: E731
        with mock.patch.object(cr, "worker_running", return_value=True), \
                mock.patch.object(cr.time, "sleep", side_effect=poll), \
                mock.patch.object(cr.time, "monotonic", side_effect=lambda: clock["t"]):
            done = self.invoke("--refresh-hooks", "--apply")
        self.assertEqual([r["state"] for r in done["runners"]], ["deferred"] * 3)
        self.assertGreaterEqual(clock["t"], cr.REFRESH_GAP_WAIT_S)
        self.assertLess(clock["t"], cr.REFRESH_GAP_WAIT_S + 2 * cr.REFRESH_GAP_POLL_S, "one budget, not one per runner")
        self.assertTrue((hooks / f".{cr.HOOK_NAME}{cr.NEXT_SUFFIX}").is_file())

    def test_a_staged_hook_never_replaces_a_hook_that_changed_since(self) -> None:
        hooks = self.installed_with_an_older_hook()
        with mock.patch.object(cr, "worker_running", side_effect=[True] + [False] * 8), \
                mock.patch.object(cr, "REFRESH_GAP_WAIT_S", 0):
            self.assertTrue(self.invoke("--refresh-hooks", "--apply")["runners"][0].get("staged"))
        staged = hooks / f".{cr.HOOK_NAME}{cr.NEXT_SUFFIX}"
        self.assertTrue(staged.is_file())
        # an --apply from a newer copy wrote another hook in the meantime: job-completed drops the stale stage
        newer = HOOK.read_text() + "\n# from main, newer than this release\n"
        (hooks / cr.HOOK_NAME).write_text(newer)
        self.assertIn("changed since it was staged", hook.install_staged_hook(hooks))
        self.assertEqual((hooks / cr.HOOK_NAME).read_text(), newer)
        self.assertFalse(staged.exists())
        self.assertFalse((hooks / cr.NEXT_RECORD).exists())
        # a refresh that finds the runner idle installs directly and clears any stage
        (hooks / cr.HOOK_NAME).write_text(self.OLDER_HOOK)
        with mock.patch.object(cr, "worker_running", return_value=True), mock.patch.object(cr, "REFRESH_GAP_WAIT_S", 0):
            self.invoke("--refresh-hooks", "--apply")
        self.assertTrue(staged.is_file())
        self.assertEqual(self.invoke("--refresh-hooks", "--apply")["runners"][0]["state"], "updated")
        self.assertFalse(staged.exists())
        self.assertFalse((hooks / cr.NEXT_RECORD).exists())

    def test_refresh_hooks_waits_for_a_job_and_refuses_what_it_cannot_prove(self) -> None:
        hooks = self.installed_with_an_older_hook()
        older = (hooks / cr.HOOK_NAME).read_bytes()
        clock = {"t": 0.0}

        def tick(seconds: float) -> None:
            clock["t"] += seconds

        with mock.patch.object(cr, "worker_running", return_value=True), \
                mock.patch.object(cr.time, "sleep", side_effect=tick), \
                mock.patch.object(cr.time, "monotonic", side_effect=lambda: clock["t"]):
            deferred = self.invoke("--refresh-hooks", "--apply")["runners"][0]
        self.assertEqual(deferred["state"], "deferred")
        self.assertGreaterEqual(clock["t"], cr.REFRESH_GAP_WAIT_S, "a runner busy the whole window is left for a later run")
        self.assertEqual((hooks / cr.HOOK_NAME).read_bytes(), older, "never swapped under a running job")
        # a job that starts during the checks: still deferred, nothing written (no gap wait: one look only)
        with mock.patch.object(cr, "worker_running", side_effect=[False, True]), \
                mock.patch.object(cr, "REFRESH_GAP_WAIT_S", 0):
            self.assertEqual(self.invoke("--refresh-hooks", "--apply")["runners"][0]["state"], "deferred")
        self.assertEqual((hooks / cr.HOOK_NAME).read_bytes(), older)
        runner = self.home / "actions-runner-glaeda"
        with mock.patch.object(cr.subprocess, "run", side_effect=OSError("no ps")):  # ps cannot tell: busy
            self.assertTrue(cr.worker_running(runner))
        def table(*commands: str):
            return mock.patch.object(cr.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, "".join(c + "\n" for c in commands), ""))
        with table(f"{runner}/bin.2.338.0/Runner.Worker spawnclient 148 151"):  # a self-updated runner's job
            self.assertTrue(cr.worker_running(runner))
        with table(f"{runner}/bin/Runner.Worker spawnclient 1 2"):
            self.assertTrue(cr.worker_running(runner))
        with table(f"{runner}-2/bin.2.338.0/Runner.Worker spawnclient 1 2", f"{runner}/bin/Runner.Listener run"):
            self.assertFalse(cr.worker_running(runner), "another instance's job, or a bare listener, is not this one's")
            (runner / "_diag").mkdir(exist_ok=True)
            (runner / "_diag/Runner_20261027-021255-utc.log").write_text(
                "[2026-10-27 02:18:14Z INFO BrokerMessageListener] Acknowledging runner request 'abc-123'.\n")
            self.assertTrue(cr.worker_running(runner), "a request the listener took is a job before its Worker starts")
        with table(f"{runner}-2/bin/Runner.Listener run"):
            self.assertFalse(cr.worker_running(runner), "a request left by a listener that is gone holds nothing")
        (runner / "_diag/Runner_20261027-021255-utc.log").unlink()
        with mock.patch.object(cr, "hook_module", side_effect=OSError("no hook")), table():
            self.assertTrue(cr.worker_running(runner), "a copy without its hook cannot tell: busy")
        # an --apply holds the lock: a later run refreshes
        with cr.runner_lock(self.home, wait=True):
            self.assertIn("lock", self.invoke("--refresh-hooks", "--apply", expect=1)["error"])
        # a wrapper passing a flag the new hook does not know: a swap would fail every job-started
        started = hooks / "job-started.sh"
        wrapper = started.read_text()
        started.write_text(wrapper.replace(" job-started ", " job-started --flag-from-a-newer-hook "))
        blocked = self.invoke("--refresh-hooks", "--apply", expect=1)["runners"][0]
        self.assertEqual(blocked["state"], "blocked")
        self.assertIn("rejects job-started.sh", blocked["note"])
        self.assertEqual((hooks / cr.HOOK_NAME).read_bytes(), older)
        started.write_text(wrapper)
        # a check that times out on a loaded host is no verdict: deferred to a later run, never blocked
        real_run = subprocess.run

        def slow(argv, *args, **kwargs):
            if "--parse-only" in argv:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return real_run(argv, *args, **kwargs)

        with mock.patch.object(cr.subprocess, "run", side_effect=slow):
            slowed = self.invoke("--refresh-hooks", "--apply")["runners"][0]
        self.assertEqual(slowed["state"], "deferred")
        self.assertTrue(slowed["note"].startswith(cr.HOOK_CHECK_SLOW), slowed["note"])
        self.assertEqual((hooks / cr.HOOK_NAME).read_bytes(), older)
        # a directory whose marker no longer matches the receipt is not this command's
        marker = self.home / "actions-runner-glaeda" / cr.MARKER
        install_id = marker.read_text()
        marker.write_text("someone-else\n")
        self.assertEqual(self.invoke("--refresh-hooks", "--apply")["runners"][0]["state"], "skipped")
        marker.write_text(install_id)
        # a hook that is a link, not the file --apply wrote
        (hooks / cr.HOOK_NAME).unlink()
        (hooks / cr.HOOK_NAME).symlink_to(HOOK)
        self.assertEqual(self.invoke("--refresh-hooks", "--apply")["runners"][0]["state"], "skipped")
        self.assertTrue((hooks / cr.HOOK_NAME).is_symlink())
        (hooks / cr.HOOK_NAME).unlink()
        (hooks / cr.HOOK_NAME).write_bytes(older)
        (hooks / cr.HOOK_NAME).chmod(0o755)
        (hooks / f".{cr.HOOK_NAME}.abc{cr.STAGE_SUFFIX}").write_text("a killed refresh's stage")
        live = hooks / f".job-started.sh{cr.STAGE_SUFFIX}"  # an older copy's lockless --apply, mid-write
        live.write_text("an older --apply's stage")
        self.assertEqual(self.invoke("--refresh-hooks", "--apply")["runners"][0]["state"], "updated")
        self.assertEqual((hooks / cr.HOOK_NAME).read_bytes(), HOOK.read_bytes())
        self.assertEqual([p.name for p in hooks.iterdir() if p.name.endswith(".tmp")], [live.name],
                         "its own stages are gone; an older --apply's fixed stage is not its to remove")

    def test_refresh_hooks_checks_the_canonical_root_calls_too(self) -> None:
        hooks = self.installed_with_an_older_hook("--capacity-units", "4")
        calls = dict((w, argv) for w, argv in cr.hook_calls(hooks) if w == "glaeda-canonical-root")
        self.assertTrue(calls, "take-root and take-gui are checked")
        takes = [argv[2] for w, argv in cr.hook_calls(hooks) if w == "glaeda-canonical-root"]
        self.assertEqual(sorted(takes), ["take-gui", "take-root"])
        root = hooks / "glaeda-canonical-root"
        root.write_text(root.read_text().replace("take-root --root", "take-root --flag-from-a-newer-hook --root"))
        blocked = self.invoke("--refresh-hooks", "--apply", expect=1)["runners"][0]
        self.assertIn("rejects glaeda-canonical-root", blocked["note"])

    def test_refresh_hooks_reads_every_instance_receipt_and_nothing_else(self) -> None:
        self.assertEqual(self.invoke("--refresh-hooks", "--apply")["runners"], [], "no runner here: nothing")
        self.hook_history("# older\n")
        self.invoke("--apply", "--labels", "cmux-extra")
        self.invoke("--apply", "--labels", "cmux-extra", "--instance", "2")
        for name in ("actions-runner-glaeda", "actions-runner-glaeda-2"):
            (self.home / name / cr.HOOK_DIR / cr.HOOK_NAME).write_text("# older\n")
        done = self.invoke("--refresh-hooks", "--apply")
        self.assertEqual(sorted((Path(r["runnerDir"]).name, r["state"]) for r in done["runners"]),
                         [("actions-runner-glaeda", "updated"), ("actions-runner-glaeda-2", "updated")])

    def test_apply_installs_registers_and_is_idempotent(self) -> None:
        receipt = self.invoke("--apply", "--labels", "cmux-extra")
        runner = self.home / "actions-runner-glaeda"
        self.assertTrue((runner / ".runner").is_file())
        self.assertTrue((runner / ".glaeda-cmux-runner").is_file())
        kinds = self.by_kind(receipt)
        self.assertEqual(kinds["verify"]["state"], "ok", kinds["verify"])
        self.assertEqual(kinds["verify"]["runner"]["id"], 4242)
        config = [e for e in self.log() if e["tool"] == "config.sh"][0]
        self.assertIn("--unattended", config["argv"])
        self.assertNotIn("--ephemeral", config["argv"])
        self.assertEqual(config["argv"][config["argv"].index("--labels") + 1], "glaeda-mini,cmux-extra")
        self.assertEqual(config["argv"][config["argv"].index("--url") + 1], "https://github.com/manaflow-ai/cmux")
        plist = plistlib.loads((self.home / "Library/LaunchAgents/com.teamleaderleo.glaeda.cmux-runner.plist").read_bytes())
        self.assertEqual(plist["ProgramArguments"], [os.fspath(runner / "glaeda-hooks/listen.sh")])
        self.assertIs(plist["KeepAlive"], True)
        self.assertEqual(plist["ThrottleInterval"], 10)
        listen = runner / "glaeda-hooks/listen.sh"
        self.assertTrue(os.access(listen, os.X_OK))
        self.assertIn(f"listen --runner-dir {runner} --no-waiters &", listen.read_text())  # no capacity units
        self.assertIn("exec ./run.sh", listen.read_text())
        env = plist["EnvironmentVariables"]
        started = Path(env["ACTIONS_RUNNER_HOOK_JOB_STARTED"])
        self.assertTrue(os.access(started, os.X_OK))
        self.assertTrue(os.access(env["ACTIONS_RUNNER_HOOK_JOB_COMPLETED"], os.X_OK))
        self.assertIn("--allowed-repo manaflow-ai/cmux", started.read_text())
        receipt_path = self.home / ".local/state/glaeda/cmux-runner/receipt.json"
        self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(receipt_path.read_text())["registration"]["runnerId"], 4242)
        self.assertFalse(any(e["tool"] == "launchctl" for e in self.log()), "sandbox HOME never calls launchctl")

        before = self.tree()
        calls = len(self.log())
        second = self.invoke("--apply", "--labels", "cmux-extra")
        states = {a["state"] for a in second["actions"] if a["kind"] != "verify"}
        self.assertEqual(states, {"unchanged"})
        self.assertFalse(any(e["tool"] in {"curl", "config.sh"} or "-X" in e["argv"] for e in self.log()[calls:]))
        after = self.tree()
        for tree in (before, after):
            tree.pop(".local/state/glaeda/cmux-runner/receipt.json")
        self.assertEqual(before, after)

    def test_installed_wrapper_hooks_allow_only_teamleaderleo_fork_prs(self) -> None:
        self.invoke("--apply")
        hooks = self.home / "actions-runner-glaeda/glaeda-hooks"
        for name, (event_name, payload, admitted) in SAMPLE_EVENTS.items():
            with self.subTest(name):
                path = event(self.state, name, payload)
                result = subprocess.run(["/bin/bash", os.fspath(hooks / "job-started.sh")], capture_output=True,
                                        text=True, timeout=60, check=False, env={
                                            "PATH": "/usr/bin:/bin", "HOME": os.fspath(self.home),
                                            "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(self.home / "fleet"),
                                            "GITHUB_EVENT_NAME": event_name, "GITHUB_EVENT_PATH": os.fspath(path),
                                            "GITHUB_REPOSITORY": (payload.get("repository") or {}).get("full_name", "")})
                self.assertEqual(result.returncode == 0, admitted, result.stdout + result.stderr)
                done = subprocess.run(["/bin/bash", os.fspath(hooks / "job-completed.sh")], capture_output=True,
                                      text=True, timeout=60, check=False, env={"PATH": "/usr/bin:/bin",
                                                                               "HOME": os.fspath(self.home)})
                self.assertEqual(done.returncode, 0)

    def test_installed_wrapper_holds_the_lock_for_its_real_parent(self) -> None:
        self.invoke("--apply")
        hooks = self.home / "actions-runner-glaeda/glaeda-hooks"
        fleet = self.home / "fleet"
        fleet.mkdir()
        (fleet / "host.lock").touch()
        env = {"PATH": "/usr/bin:/bin", "HOME": os.fspath(self.home), "GLAEDA_CANONICAL_ROOT_PARENT": NO_ROOTS, "GLAEDA_FLEET_DIR": os.fspath(fleet),
               "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": os.fspath(event(self.state, "push", SAMPLE_EVENTS["push"][1])),
               "GITHUB_REPOSITORY": "manaflow-ai/cmux"}
        # No --watch-pid: the wrapper execs python, so the holder watches this test process, which lives on.
        started = subprocess.run(["/bin/bash", os.fspath(hooks / "job-started.sh")], capture_output=True,
                                 text=True, timeout=60, check=False, env=env)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertIn(f"watching pid {os.getpid()}", started.stdout)
        time.sleep(3)  # longer than one holder poll: a holder watching a dead parent would have let go
        import fcntl
        fd = os.open(fleet / "host.lock", os.O_RDONLY)
        try:
            with self.assertRaises(OSError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        subprocess.run(["/bin/bash", os.fspath(hooks / "job-completed.sh")], capture_output=True, timeout=60,
                       check=False, env=env)
        fd = os.open(fleet / "host.lock", os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_missing_interpreter_fails_closed(self) -> None:
        self.invoke("--apply", "--python", "/nonexistent/python3")
        hooks = self.home / "actions-runner-glaeda/glaeda-hooks"
        path = event(self.state, "push", SAMPLE_EVENTS["push"][1])
        started = subprocess.run(["/bin/bash", os.fspath(hooks / "job-started.sh")], capture_output=True, timeout=30,
                                 check=False, env={"PATH": "/usr/bin:/bin", "GITHUB_EVENT_NAME": "push",
                                                   "GITHUB_EVENT_PATH": os.fspath(path)})
        self.assertNotEqual(started.returncode, 0)
        done = subprocess.run(["/bin/bash", os.fspath(hooks / "job-completed.sh")], capture_output=True, timeout=30,
                              check=False, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(done.returncode, 0)

    def test_token_never_in_argv_files_or_output(self) -> None:
        self.invoke("--apply")
        self.invoke("--uninstall", "--apply")
        entries = self.log()
        self.assertTrue(any(e["tool"] == "config.sh" and e["envToken"] == REG_TOKEN for e in entries))
        self.assertTrue(any(e["tool"] == "config.sh" and e["envToken"] == REMOVE_TOKEN for e in entries))
        for entry in entries:
            joined = " ".join(entry["argv"])
            self.assertNotIn(REG_TOKEN, joined)
            self.assertNotIn(REMOVE_TOKEN, joined)
            if entry["tool"] != "config.sh":
                self.assertIsNone(entry["envToken"], entry)
        self.assertNotIn(REG_TOKEN, self.last_output)
        for path in list(self.home.rglob("*")):
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(REG_TOKEN.encode(), data, path)
                self.assertNotIn(REMOVE_TOKEN.encode(), data, path)
        self.assertNotIn("ACTIONS_RUNNER_INPUT_TOKEN", os.environ)

    def test_token_scrubbed_from_config_output_on_failure(self) -> None:
        # config.sh rejects this token and echoes it back; the receipt and output must not carry it.
        out = io.StringIO()
        with mock.patch.object(cr, "read_token", lambda gh, ep: REG_TOKEN + "X"), contextlib.redirect_stdout(out):
            code = cr.main(["--apply", "--output", "json", "--gh", os.fspath(self.gh), "--python", sys.executable])
        self.assertEqual(code, 1)
        self.assertNotIn(REG_TOKEN + "X", out.getvalue())
        receipt = json.loads(out.getvalue())
        self.assertEqual(self.by_kind(receipt)["register"]["state"], "failed")
        self.assertEqual(self.by_kind(receipt)["agent"]["state"], "skipped")

    def test_sha_mismatch_refuses_install(self) -> None:
        self.build_release(sha_override="0" * 64)
        receipt = self.invoke("--apply", expect=1)
        self.assertEqual(self.by_kind(receipt)["download"]["state"], "failed")
        self.assertIn("SHA-256 mismatch", self.by_kind(receipt)["download"]["note"])
        self.assertFalse((self.home / "actions-runner-glaeda").exists())
        self.assertFalse(any(e["tool"] == "config.sh" or "-X" in e["argv"] for e in self.log()))

    def test_existing_runner_name_blocks_without_replace(self) -> None:
        (self.state / "runners.json").write_text(json.dumps({"runners": [
            {"id": 7, "name": "mini-test-glaeda", "status": "offline", "labels": []}]}))
        receipt = self.invoke("--apply", expect=1)
        self.assertEqual(self.by_kind(receipt)["register"]["state"], "blocked")
        self.assertFalse(receipt["ready"])
        self.assertFalse((self.home / "actions-runner-glaeda").exists())

    def test_launchctl_is_managed_for_the_real_home(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            receipt = self.invoke("--apply")
            self.assertTrue(self.by_kind(receipt)["agent"]["bootstrapped"])
            self.invoke("--uninstall", "--apply")
        verbs = [e["argv"][0] for e in self.log() if e["tool"] == "launchctl"]
        self.assertIn("bootstrap", verbs)
        self.assertIn("bootout", verbs)
        self.assertLess(verbs.index("bootstrap"), verbs.index("bootout"))

    def test_a_runner_held_for_a_repair_stays_disabled(self) -> None:
        held = self.home / ".local/state/glaeda/mini-fleet/runner-held"
        held.mkdir(parents=True)
        (held / "actions-runner-glaeda").touch()
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--apply")
        verbs = [e["argv"][0] for e in self.log() if e["tool"] == "launchctl"]
        self.assertNotIn("enable", verbs)

    def test_a_disabled_label_is_enabled_before_bootstrap(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        label = "com.teamleaderleo.glaeda.cmux-runner"
        (self.state / "disabled.json").write_text(json.dumps([label]))  # a hand `launchctl disable`, no hold mark
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            plan = self.invoke()
            self.assertTrue(self.by_kind(plan)["agent"]["disabled"])
            self.assertIn("enables it before bootstrap", self.by_kind(plan)["agent"]["note"])
            receipt = self.invoke("--apply")
        agent = self.by_kind(receipt)["agent"]
        self.assertTrue(agent["bootstrapped"] and agent["wasDisabled"] and agent["enabled"], agent)
        self.assertTrue(receipt["ready"], receipt["blocking"])
        verbs = [e["argv"][0] for e in self.log() if e["tool"] == "launchctl" and e["argv"][0] != "print"]
        self.assertLess(verbs.index("enable"), verbs.index("bootstrap"), verbs)
        self.assertIn(["enable", f"gui/{os.getuid()}/{label}"], [e["argv"] for e in self.log() if e["tool"] == "launchctl"])
        on_disk = json.loads((self.home / ".local/state/glaeda/cmux-runner/receipt.json").read_text())
        self.assertTrue(self.by_kind(on_disk)["agent"]["wasDisabled"])

    def test_an_enabled_label_is_bootstrapped_without_enable(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            receipt = self.invoke("--apply")
        self.assertNotIn("wasDisabled", self.by_kind(receipt)["agent"])
        self.assertNotIn("enable", [e["argv"][0] for e in self.log() if e["tool"] == "launchctl"])

    def test_a_held_runner_stays_stopped_until_released(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        label = "com.teamleaderleo.glaeda.cmux-runner"
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--apply")
            held = self.home / ".local/state/glaeda/mini-fleet/runner-held/actions-runner-glaeda"
            held.parent.mkdir(parents=True)
            held.write_text("")  # runner_hold: mark, disable, bootout
            (self.state / "disabled.json").write_text(json.dumps([label]))
            (self.state / "log.jsonl").unlink()
            receipt = self.invoke("--apply")
            agent = self.by_kind(receipt)["agent"]
            self.assertTrue(agent["held"], agent)
            self.assertIn("runner_release", agent["note"])
            self.assertNotIn("bootstrapped", agent)
            self.assertTrue(receipt["ready"], receipt["blocking"])
            self.assertIn("held", self.by_kind(receipt)["verify"]["note"])
            verbs = [e["argv"][0] for e in self.log() if e["tool"] == "launchctl"]
            self.assertNotIn("enable", verbs)
            self.assertNotIn("bootstrap", verbs)
            self.assertEqual(json.loads((self.state / "disabled.json").read_text()), [label])
            held.unlink()  # runner_release: enable, bootstrap, drop the mark; or a later --apply once released
            receipt = self.invoke("--apply")
        self.assertTrue(self.by_kind(receipt)["agent"]["bootstrapped"])

    def test_relabel_never_starts_a_held_runner(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--apply", "--labels", "ram48")
            held = self.home / ".local/state/glaeda/mini-fleet/runner-held/actions-runner-glaeda"
            held.parent.mkdir(parents=True)
            held.write_text("")
            (self.state / "log.jsonl").unlink()
            with mock.patch.object(cr, "xcode_present", return_value=True):
                receipt = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                                      "--name", "mini-test-glaeda")
        self.assertEqual(self.by_kind(receipt)["register"]["state"], "updated")
        self.assertNotIn("bootstrap", [e["argv"][0] for e in self.log() if e["tool"] == "launchctl"])

    def test_print_disabled_is_read_per_exact_label(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        (self.state / "disabled.json").write_text(json.dumps(["com.teamleaderleo.glaeda.cmux-runner.1"]))
        (self.state / "print-disabled-extra").write_text(
            '\t"com.teamleaderleo.glaeda.cmux-runner" => enabled\n\t"com.teamleaderleo.glaeda.cmux-runner.2" => true\n')
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            receipt = self.invoke("--apply")  # instance 0: its own entry says enabled; .1 is only a prefix match
            self.assertFalse(self.by_kind(receipt)["agent"]["disabled"])
            self.assertNotIn("enable", [e["argv"][0] for e in self.log() if e["tool"] == "launchctl"])
            ctx = cr.Context(cr.parser().parse_args(["--instance", "2"]))
            self.assertTrue(cr.launchctl_disabled(ctx), "older macOS prints => true")
            ctx = cr.Context(cr.parser().parse_args(["--instance", "1"]))
            self.assertTrue(cr.launchctl_disabled(ctx))
            self.assertEqual(cr.held_marker(ctx).name, "actions-runner-glaeda-1")  # runner_hold's basename

    def test_relabel_of_a_loaded_held_runner_leaves_it_stopped_and_says_so(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        label = "com.teamleaderleo.glaeda.cmux-runner"
        (self.state / "loaded.json").write_text("[]")
        (self.state / "disabled.json").write_text(json.dumps([label]))
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            first = self.invoke("--apply", "--labels", "ram48")  # a hand disable: enabled, then started
            self.assertTrue(self.by_kind(first)["agent"]["wasDisabled"])
            self.assertEqual(json.loads((self.state / "loaded.json").read_text()), [label])
            held = self.home / ".local/state/glaeda/mini-fleet/runner-held/actions-runner-glaeda"
            held.parent.mkdir(parents=True)
            held.write_text("")  # marked held, but still loaded (a hold whose bootout did not take)
            (self.state / "log.jsonl").unlink()
            with mock.patch.object(cr, "xcode_present", return_value=True):
                receipt = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                                      "--name", "mini-test-glaeda")
        agent = self.by_kind(receipt)["agent"]
        self.assertEqual(self.by_kind(receipt)["register"]["state"], "updated")
        self.assertFalse(agent["loaded"], agent)
        self.assertIn("runner_release", agent["note"])
        self.assertIn("held", self.by_kind(receipt)["verify"]["note"])
        self.assertEqual(json.loads((self.state / "loaded.json").read_text()), [])
        verbs = [e["argv"][0] for e in self.log() if e["tool"] == "launchctl"]
        self.assertNotIn("bootstrap", verbs)
        self.assertNotIn("enable", verbs)

    def test_relabel_records_the_enable_on_the_agent_step(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        label = "com.teamleaderleo.glaeda.cmux-runner"
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--apply", "--labels", "ram48")
            (self.state / "disabled.json").write_text(json.dumps([label]))
            with mock.patch.object(cr, "xcode_present", return_value=True):
                receipt = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                                      "--name", "mini-test-glaeda")
        agent = self.by_kind(receipt)["agent"]
        self.assertTrue(agent["wasDisabled"] and agent["enabled"], agent)
        self.assertTrue(receipt["ready"], receipt["blocking"])

    def test_a_failed_enable_fails_the_agent_step(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        (self.state / "disabled.json").write_text(json.dumps(["com.teamleaderleo.glaeda.cmux-runner"]))
        make_executable(self.launchctl, FAKE_LAUNCHCTL.replace(
            'if argv[0] in ("enable", "disable"):', 'if argv[0] == "enable":\n    print("denied"); sys.exit(1)\nif argv[0] in ("enable", "disable"):'))
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            receipt = self.invoke("--apply", expect=1)
        agent = self.by_kind(receipt)["agent"]
        self.assertEqual(agent["state"], "failed")
        self.assertIn("launchctl enable failed: denied", agent["note"])
        self.assertNotIn("bootstrap", [e["argv"][0] for e in self.log() if e["tool"] == "launchctl"])

    def test_uninstall_drops_the_hold_mark(self) -> None:
        self.invoke("--apply")
        held = self.home / ".local/state/glaeda/mini-fleet/runner-held/actions-runner-glaeda"
        held.parent.mkdir(parents=True)
        held.write_text("")
        receipt = self.invoke("--uninstall", "--apply")
        self.assertTrue(self.by_kind(receipt)["agent"]["heldMarkRemoved"])
        self.assertFalse(held.exists())

    # ------------------------------------------------------------ uninstall

    def test_uninstall_plan_is_side_effect_free_then_apply_removes_owned(self) -> None:
        self.invoke("--apply", "--name", "custom-mini")
        before, calls = self.tree(), len(self.log())
        plan = self.invoke("--uninstall")  # no --name: the receipt decides what to remove
        self.assertEqual(before, self.tree())
        self.assertFalse(any("-X" in e["argv"] or e["tool"] == "config.sh" for e in self.log()[calls:]))
        states = {a["kind"]: a["state"] for a in plan["actions"]}
        self.assertEqual(states, {"agent": "remove", "deregister": "remove", "dir": "remove"})
        self.invoke("--uninstall", "--apply")
        self.assertFalse((self.home / "actions-runner-glaeda").exists())
        self.assertFalse((self.home / "Library/LaunchAgents/com.teamleaderleo.glaeda.cmux-runner.plist").exists())
        self.assertEqual(json.loads((self.state / "runners.json").read_text())["runners"], [])
        remove = [e for e in self.log() if e["tool"] == "config.sh" and e["argv"][:1] == ["remove"]]
        self.assertEqual(len(remove), 1)
        state_dir = self.home / ".local/state/glaeda/cmux-runner"
        self.assertTrue((state_dir / "uninstall-receipt.json").is_file())
        self.assertFalse((state_dir / "receipt.json").exists())

    def test_uninstall_without_receipt_removes_nothing(self) -> None:
        runner = self.home / "actions-runner-glaeda"
        runner.mkdir()
        (runner / "config.sh").write_text("#!/bin/sh\n")
        before = self.tree()
        receipt = self.invoke("--uninstall", "--apply")
        self.assertTrue(any("nothing is removed" in a.get("note", "") for a in receipt["actions"]))
        after = {k: v for k, v in self.tree().items() if not k.startswith(".local")}
        self.assertEqual(before, after)
        self.assertTrue((self.home / ".local/state/glaeda/cmux-runner/uninstall-receipt.json").is_file())
        self.assertFalse(any(e["tool"] == "config.sh" or "-X" in e["argv"] for e in self.log()))

    def test_preexisting_runner_dir_is_never_adopted(self) -> None:
        runner = self.home / "actions-runner-glaeda"
        runner.mkdir()
        (runner / "keep.txt").write_text("operator data")
        receipt = self.invoke("--apply", expect=1)
        self.assertEqual(self.by_kind(receipt)["dir"]["state"], "blocked")
        self.assertEqual(sorted(p.name for p in runner.iterdir()), ["keep.txt"])
        self.assertFalse(any(e["tool"] in {"curl", "config.sh"} for e in self.log()))
        self.invoke("--uninstall", "--apply")
        self.assertTrue((runner / "keep.txt").exists())

    def test_marker_mismatch_keeps_dir_and_registration(self) -> None:
        self.invoke("--apply")
        runner = self.home / "actions-runner-glaeda"
        (runner / ".glaeda-cmux-runner").write_text("someone-else\n")
        plan = self.invoke("--uninstall", "--apply")
        states = {a["kind"]: a["state"] for a in plan["actions"]}
        self.assertEqual(states["dir"], "kept")
        self.assertEqual(states["deregister"], "kept")
        self.assertTrue((runner / ".runner").exists())
        self.assertFalse(any(e["tool"] == "config.sh" and e["argv"][:1] == ["remove"] for e in self.log()))

    def test_operator_edited_plist_is_kept(self) -> None:
        self.invoke("--apply")
        plist = self.home / "Library/LaunchAgents/com.teamleaderleo.glaeda.cmux-runner.plist"
        plist.write_bytes(plist.read_bytes() + b"\n<!-- operator edit -->\n")
        plan = self.invoke("--uninstall", "--apply")
        self.assertEqual({a["kind"]: a["state"] for a in plan["actions"]}["agent"], "kept")
        self.assertTrue(plist.exists())

    def test_failed_deregistration_keeps_dir_and_receipt(self) -> None:
        self.invoke("--apply")
        (self.state / "fail-config-remove").touch()
        (self.state / "fail-delete").touch()
        receipt = self.invoke("--uninstall", "--apply", expect=1)
        states = {a["kind"]: a["state"] for a in receipt["actions"]}
        self.assertEqual(states["deregister"], "failed")
        self.assertEqual(states["dir"], "kept")
        self.assertTrue((self.home / "actions-runner-glaeda/.runner").exists())
        self.assertTrue((self.home / ".local/state/glaeda/cmux-runner/receipt.json").exists())
        (self.state / "fail-config-remove").unlink()
        (self.state / "fail-delete").unlink()
        # The plist was already removed by the failed run; the retry still deregisters and cleans up.
        retry = self.invoke("--uninstall", "--apply")
        self.assertEqual({a["kind"]: a["state"] for a in retry["actions"]}["dir"], "remove")
        self.assertFalse((self.home / "actions-runner-glaeda").exists())

    def test_api_fallback_never_deletes_another_machines_runner(self) -> None:
        self.invoke("--apply")
        # the name was re-registered elsewhere with --replace: same name, new id
        (self.state / "runners.json").write_text(json.dumps({"runners": [
            {"id": 9999, "name": "mini-test-glaeda", "status": "online", "labels": []}]}))
        (self.state / "fail-config-remove").touch()
        receipt = self.invoke("--uninstall", "--apply", expect=1)
        dereg = next(a for a in receipt["actions"] if a["kind"] == "deregister")
        self.assertEqual(dereg["state"], "failed")
        self.assertIn("not deleted", dereg["note"])
        self.assertFalse(any(e["tool"] == "gh" and "DELETE" in e["argv"] for e in self.log()))
        self.assertTrue((self.home / "actions-runner-glaeda").is_dir())
        self.assertTrue((self.home / ".local/state/glaeda/cmux-runner/receipt.json").is_file())

    def test_a_second_install_elsewhere_is_blocked_and_keeps_the_receipt(self) -> None:
        self.invoke("--apply")
        receipt_path = self.home / ".local/state/glaeda/cmux-runner/receipt.json"
        before = receipt_path.read_bytes()
        (self.home / "elsewhere").mkdir()
        for args in (("--runner-dir", os.fspath(self.home / "elsewhere")),
                     ("--runner-dir", os.fspath(self.home / "fresh")),
                     ("--name", "other-glaeda"), ("--repo", "teamleaderleo/cmux")):
            with self.subTest(args=args):
                blocked = self.invoke("--apply", *args, expect=1)
                self.assertEqual([a["state"] for a in blocked["actions"]], ["blocked"])
                self.assertEqual(receipt_path.read_bytes(), before)
                self.assertFalse((self.home / "fresh").exists())
        removed = self.invoke("--uninstall", "--apply")
        self.assertTrue(all(a["applied"] for a in removed["actions"] if a["state"] == "remove"))
        self.assertFalse((self.home / "actions-runner-glaeda").exists())

    def test_rerun_after_failed_unpack_recovers(self) -> None:
        self.invoke("--apply")
        runner = self.home / "actions-runner-glaeda"
        for name in ("config.sh", ".runner"):
            (runner / name).unlink()
        (self.state / "runners.json").write_text(json.dumps({"runners": []}))
        receipt = self.invoke("--apply")
        self.assertTrue(receipt["ready"], receipt["blocking"])
        self.assertTrue((runner / ".runner").is_file())

    def test_registration_finished_after_timeout_is_adopted_then_removed(self) -> None:
        # config.sh outlived our timeout: .runner exists but the receipt recorded no registration
        with mock.patch.object(cr, "run_with_token", side_effect=lambda argv, token, cwd, timeout=900: (
                subprocess.run(argv, env={**os.environ, "ACTIONS_RUNNER_INPUT_TOKEN": token}, cwd=cwd,
                               capture_output=True, check=False), (127, "timed out after 900 seconds"))[1]):
            first = self.invoke("--apply", expect=1)
        self.assertEqual(self.by_kind(first)["register"]["state"], "failed")
        runner = self.home / "actions-runner-glaeda"
        self.assertTrue((runner / ".runner").is_file())
        second = self.invoke("--apply")
        self.assertTrue(second["ready"], second["blocking"])
        self.assertIn("adopted", self.by_kind(second)["register"]["note"])
        receipt = json.loads((self.home / ".local/state/glaeda/cmux-runner/receipt.json").read_text())
        self.assertEqual(receipt["registration"]["runnerId"], 4242)
        removed = self.invoke("--uninstall", "--apply")
        self.assertTrue(next(a for a in removed["actions"] if a["kind"] == "deregister")["applied"])
        self.assertEqual(json.loads((self.state / "runners.json").read_text())["runners"], [])
        self.assertFalse(runner.exists())

    def test_uninstall_deregisters_an_unadopted_interrupted_registration(self) -> None:
        with mock.patch.object(cr, "run_with_token", side_effect=lambda argv, token, cwd, timeout=900: (
                subprocess.run(argv, env={**os.environ, "ACTIONS_RUNNER_INPUT_TOKEN": token}, cwd=cwd,
                               capture_output=True, check=False), (127, "timed out"))[1]):
            self.invoke("--apply", expect=1)
        removed = self.invoke("--uninstall", "--apply")
        self.assertTrue(next(a for a in removed["actions"] if a["kind"] == "deregister")["applied"])
        self.assertEqual(json.loads((self.state / "runners.json").read_text())["runners"], [])

    def test_interrupted_registration_uninstall_uses_the_runners_own_name_and_scope(self) -> None:
        with mock.patch.object(cr, "run_with_token", side_effect=lambda argv, token, cwd, timeout=900: (
                subprocess.run(argv, env={**os.environ, "ACTIONS_RUNNER_INPUT_TOKEN": token}, cwd=cwd,
                               capture_output=True, check=False), (124, "timed out"))[1]):
            self.invoke("--apply", "--repo", "teamleaderleo/cmux", "--name", "custom-glaeda", expect=1)
        removed = self.invoke("--uninstall", "--apply")
        self.assertTrue(next(a for a in removed["actions"] if a["kind"] == "deregister")["applied"])
        tokens = [e["argv"] for e in self.log() if e["tool"] == "gh" and "-X" in e["argv"]]
        self.assertIn("repos/teamleaderleo/cmux/actions/runners/remove-token", tokens[-1])
        self.assertEqual(json.loads((self.state / "runners.json").read_text())["runners"], [])

    def test_timeout_kills_the_whole_process_group(self) -> None:
        marker = self.home / "grandchild-survived"
        script = make_executable(self.home / "slow-config.sh",
                                 "#!/bin/bash\n(sleep 3; touch " + os.fspath(marker) + ") &\nsleep 30\n")
        start = time.monotonic()
        code, out = cr.run_with_token([os.fspath(script)], REG_TOKEN, self.home, timeout=1)
        self.assertEqual(code, 124)
        self.assertLess(time.monotonic() - start, 10)
        self.assertNotIn(REG_TOKEN, out)
        time.sleep(4)
        self.assertFalse(marker.exists())

    def manifest(self) -> str:
        path = self.state / "mini-fleet.json"
        path.write_text(json.dumps(MANIFEST))
        return os.fspath(path)

    def config_argvs(self) -> list[list[str]]:
        return [e["argv"] for e in self.log() if e["tool"] == "config.sh" and e["argv"][:1] != ["remove"]]

    def test_manifest_install_derives_labels_and_name(self) -> None:
        with mock.patch.object(cr, "xcode_present", return_value=True):
            receipt = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std")
        argv = self.config_argvs()[0]
        self.assertEqual(argv[argv.index("--name") + 1], "mini-std-glaeda")
        self.assertEqual(argv[argv.index("--labels") + 1], "glaeda-mini,glaeda-class-std,glaeda-dedicated,xcode-26.6,glaeda-std-xcode-26.6,"
                                                         "glaeda-root-std-xcode-26.6,glaeda-runner-mini-std-glaeda")
        self.assertEqual(receipt["member"]["class"], "std")
        self.assertEqual(self.by_kind(receipt)["verify"]["state"], "ok")
        # a plain re-run keeps the registered labels and never asks for a relabel
        again = self.invoke("--apply")
        self.assertEqual(self.by_kind(again)["register"]["state"], "unchanged")
        self.assertEqual(len(self.config_argvs()), 1)

    def test_manifest_disk_floor_is_baked_into_the_hook(self) -> None:
        manifest = json.loads(json.dumps(MANIFEST))
        manifest["defaults"]["disk"] = {"min_free_gib": 100}
        path = self.state / "floor.json"
        path.write_text(json.dumps(manifest))
        with mock.patch.object(cr, "xcode_present", return_value=True):
            self.invoke("--apply", "--manifest", os.fspath(path), "--member", "mini-std")
        hooks = self.home / "actions-runner-glaeda/glaeda-hooks"
        self.assertIn("--min-free-gib 100", (hooks / "job-started.sh").read_text())
        self.assertIn("--require-eligible --fleet-class m4pro-48 --toolchain-xcode /Applications/Xcode_26.6.app",
                      (hooks / "job-started.sh").read_text())
        self.assertTrue((hooks / "glaeda_reservation.py").is_file())
        shim = (hooks / "glaeda-canonical-root").read_text()
        self.assertIn("take-root --root", shim)
        self.assertIn("take-gui --capacity-dir", shim)
        for bad in ([], ["take"], ["nope"]):
            self.assertEqual(subprocess.run(["/bin/sh", os.fspath(hooks / "glaeda-canonical-root"), *bad],
                                            capture_output=True).returncode, 2, bad)
        self.assertIn("--canonical-roots 1 --capacity-dir", shim)
        self.assertIn("--state-dir", shim)
        self.assertTrue(os.access(hooks / "glaeda-canonical-root", os.X_OK))
        self.assertNotIn("--compile-slots", (hooks / "job-started.sh").read_text())  # 1 is the hook's default
        # one root: no --canonical-roots, but --instance still tells the gate a root runner from a side one
        self.assertNotIn("--canonical-roots", (hooks / "job-started.sh").read_text())
        self.assertIn("--capacity-units 14 --instance 0\n", (hooks / "job-started.sh").read_text())
        manifest["defaults"]["runner"] = {"classes": {"std": {"compileSlots": 2, "canonicalRoots": 2}}}
        path.write_text(json.dumps(manifest))
        with mock.patch.object(cr, "xcode_present", return_value=True):
            self.invoke("--apply", "--manifest", os.fspath(path), "--member", "mini-std")
        self.assertIn("--capacity-units 14 --compile-slots 2 --canonical-roots 2 --instance 0",
                      (hooks / "job-started.sh").read_text())  # instance 0 prefers root 1
        self.assertNotIn("--trusted-ref", (hooks / "job-started.sh").read_text())
        self.assertIn("--test-keychain --recycle-testmanagerd", (hooks / "job-started.sh").read_text())
        self.assertIn("--recycle-testmanagerd \"$@\"", (hooks / "glaeda-canonical-root").read_text(), "take-gui too")
        manifest["hosts"]["mini-std"].setdefault("overrides", {})["runner"] = {
            "trustedRef": "refs/heads/main", "trustedRepo": "manaflow-ai/cmux", "trustedNextNightly": True}
        manifest["hosts"]["mini-std"]["roles"].remove("dev-builds")
        path.write_text(json.dumps(manifest))
        with mock.patch.object(cr, "xcode_present", return_value=True):
            self.invoke("--apply", "--manifest", os.fspath(path), "--member", "mini-std")
        self.assertIn("--trusted-ref refs/heads/main --trusted-repo manaflow-ai/cmux", (hooks / "job-started.sh").read_text())
        self.assertIn("--trusted-next-nightly", (hooks / "job-started.sh").read_text())
        self.assertNotIn("--test-keychain", (hooks / "job-started.sh").read_text(), "a secret-holding host keeps its keychains")
        self.assertNotIn("--recycle-testmanagerd", (hooks / "job-started.sh").read_text() +
                         (hooks / "glaeda-canonical-root").read_text(), "a trusted-ref host runs no PR XCTest")

    def test_the_last_instance_is_the_gui_runner_with_only_the_gui_pool_label(self) -> None:
        manifest = json.loads(json.dumps(MANIFEST))
        manifest["hosts"]["mini-std"]["roles"].append("ios-simulators")
        manifest["hosts"]["mini-std"]["overrides"] = {"ios_simulator": {"runtimes": ["23F77"]}}
        manifest["defaults"]["runner"] = {"classes": {"std": {"runners": 5, "capacityUnits": 10, "compileSlots": 2,
                                                              "canonicalRoots": 2, "guiRunners": 1}}}
        path = self.state / "gui.json"
        path.write_text(json.dumps(manifest))
        with mock.patch.object(cr, "xcode_present", return_value=True), \
                mock.patch.object(cr, "ios_sim_present", return_value=True), \
                mock.patch.object(cr, "macos_major", return_value="27"):
            for instance in ("0", "2", "4"):
                self.invoke("--apply", "--manifest", os.fspath(path), "--member", "mini-std", "--instance", instance)
        labels = [argv[argv.index("--labels") + 1].split(",") for argv in self.config_argvs()]
        prefixes = ("glaeda-std", "glaeda-root", "glaeda-side", "glaeda-gui", "glaeda-session")
        self.assertEqual([[x for x in l if x.startswith(prefixes)] for l in labels],
                         [["glaeda-std-xcode-26.6", "glaeda-root-std-xcode-26.6"],
                          ["glaeda-std-xcode-26.6", "glaeda-side-std-xcode-26.6",
                           "glaeda-side-light-xcode-26.6"],
                          ["glaeda-gui-std-xcode-26.6", "glaeda-session-std-xcode-26.6"]],
                         "the gui runner carries neither the pool label nor a root or side one, and the session label")
        self.assertIn("glaeda-ios-sim", labels[0])
        self.assertNotIn("glaeda-ios-sim", labels[2], "no iOS job ever takes the gui runner")
        self.assertEqual([l.count("glaeda-macos-27") for l in labels], [1, 1, 1], "every runner, gui included, names its OS")
        self.assertFalse([x for x in labels[2] if x.startswith("glaeda-runner-")])
        started = (self.home / "actions-runner-glaeda-4/glaeda-hooks/job-started.sh").read_text()
        self.assertIn("--canonical-roots 2 --instance 4 --gui-runner --gui-token gui\n", started)
        self.assertNotIn("--gui-runner", (self.home / "actions-runner-glaeda-2/glaeda-hooks/job-started.sh").read_text())
        self.assertEqual(hook.runner_scope(self.home / "actions-runner-glaeda-4"), hook.RunnerScope(10, 2, 2, False, True))

    def test_gui_runners_two_adds_a_session_runner_beside_the_console_one(self) -> None:
        base = {"runners": 6, "capacityUnits": 10, "compileSlots": 2, "canonicalRoots": 2}
        labels, hooks = self.fast_labels({**base, "guiRunners": 2}, "gui2")
        roles = lambda l: [x for x in l if x.startswith(("glaeda-std", "glaeda-root", "glaeda-side", "glaeda-gui",
                                                          "glaeda-session"))]
        self.assertEqual([roles(l) for l in labels],
                         [["glaeda-std-xcode-26.6", "glaeda-root-std-xcode-26.6"]] * 2
                         + [["glaeda-std-xcode-26.6", "glaeda-side-std-xcode-26.6", "glaeda-side-light-xcode-26.6"]] * 2
                         + [["glaeda-session-std-xcode-26.6"], ["glaeda-gui-std-xcode-26.6"]],
                         "only the last carries the gui label, so no console job lands where its token is held, and "
                         "with a session runner beside it the console runner leaves session jobs to that one")
        self.assertEqual(["--gui-runner" in h for h in hooks], [False] * 5 + [True])
        self.assertEqual(["--session-runner" in h for h in hooks], [False] * 4 + [True, False])
        self.assertFalse([l for l in labels[4] if l == "glaeda-ios-sim" or l.startswith("glaeda-runner-")])
        scopes = [hook.runner_scope(self.fast_home / f"actions-runner-glaeda-{k}") for k in (4, 5)]
        self.assertEqual([(s.gui, s.session, s.root) for s in scopes], [(False, True, False), (True, False, False)])
        # a plain re-run keeps each role
        with mock.patch.dict(os.environ, {"HOME": os.fspath(self.fast_home)}), \
                mock.patch.object(cr, "xcode_present", return_value=True):
            for instance in ("4", "5"):
                self.invoke("--apply", "--instance", instance)
        again = [hook.runner_scope(self.fast_home / f"actions-runner-glaeda-{k}") for k in (4, 5)]
        self.assertEqual(again, scopes)

    def fast_labels(self, classes: dict, name: str, *, dev_compile: bool = False,
                    ios_sim: bool = False, nightly_slot: bool = False) -> tuple[list[list[str]], list[str]]:
        """Apply every instance of mini-std under these runner classes: (labels per instance, job-started.sh texts)."""
        manifest = json.loads(json.dumps(MANIFEST))
        manifest["defaults"]["runner"] = {"classes": {"std": classes}}
        if dev_compile:
            manifest["hosts"]["mini-std"].setdefault("overrides", {})["runner"] = {"devCompile": True}
        if nightly_slot:
            manifest["hosts"]["mini-std"].setdefault("overrides", {}).setdefault("runner", {})["nightlySlot"] = True
        if ios_sim:
            manifest["hosts"]["mini-std"]["roles"].append("ios-simulators")
            manifest["defaults"]["ios_simulator"] = {"runtimes": ["23F77"]}
        path = self.state / f"{name}.json"
        path.write_text(json.dumps(manifest))
        home = self.home.parent / f"operator-{name}"  # each fleet shape installs into a fresh home
        home.mkdir()
        labels: list[list[str]] = []
        with mock.patch.dict(os.environ, {"HOME": os.fspath(home)}), \
                mock.patch.object(cr, "xcode_present", return_value=True):
            for instance in range(classes["runners"]):
                receipt = self.invoke("--apply", "--manifest", os.fspath(path), "--member", "mini-std",
                                      "--instance", str(instance), "--replace")
                [register] = [a for a in receipt["actions"] if a.get("kind") == "register"]
                labels.append(register["labels"])
        hooks = [(home / f"actions-runner-glaeda{'-' + str(k) if k else ''}/glaeda-hooks/job-started.sh").read_text()
                 .replace(os.fspath(home), "HOME") for k in range(classes["runners"])]
        self.fast_home = home
        return labels, hooks

    def test_dev_compile_uses_one_existing_side_instance_and_preserves_neighbors(self) -> None:
        shape = {"runners": 6, "capacityUnits": 14, "compileSlots": 2, "canonicalRoots": 2,
                 "guiRunners": 1, "fastRunners": 1}
        with mock.patch.object(cr, "ios_sim_present", return_value=True):
            normal, normal_hooks = self.fast_labels(shape, "normal-dev-baseline", ios_sim=True)
            dev, dev_hooks = self.fast_labels(shape, "dev-instance", dev_compile=True, ios_sim=True)
        for instance in (0, 1, 3, 4, 5):
            self.assertEqual(dev[instance], normal[instance], f"neighbor {instance} keeps every label")
            self.assertEqual(dev_hooks[instance], normal_hooks[instance], f"neighbor {instance} keeps its hook")
        self.assertIn("glaeda-ios-sim", dev[3], "the ordinary side instance still admits iOS jobs")
        self.assertEqual([label for label in dev[2] if label not in cr.BASE_LABELS],
                         ["glaeda-mini", "glaeda-class-std", "glaeda-dedicated", "xcode-26.6",
                          "glaeda-dev-std-xcode-26.6"])
        self.assertEqual(["glaeda-dev-std-xcode-26.6" in labels for labels in dev],
                         [False, False, True, False, False, False])
        self.assertEqual(sum("glaeda-std-xcode-26.6" in labels for labels in dev), 3,
                         "two roots and one side instance count in the normal pool")
        self.assertEqual(["--dev-compile-runner" in text for text in dev_hooks],
                         [False, False, True, False, False, False])
        for text in dev_hooks:
            self.assertIn("--capacity-units 14 --compile-slots 2 --canonical-roots 2", text)
        self.assertNotIn("--test-keychain", dev_hooks[2])
        self.assertNotIn("--recycle-testmanagerd", dev_hooks[2])
        with mock.patch.dict(os.environ, {"HOME": os.fspath(self.fast_home)}):
            for instance in range(shape["runners"]):
                self.invoke("--apply", "--instance", str(instance))
        for instance, original in enumerate(dev_hooks):
            suffix = f"-{instance}" if instance else ""
            again = (self.fast_home / f"actions-runner-glaeda{suffix}/glaeda-hooks/job-started.sh").read_text()
            self.assertEqual(again.replace(os.fspath(self.fast_home), "HOME"), original)

    def test_the_nightly_slot_takes_the_side_instance_after_the_freshness_slot(self) -> None:
        shape = {"runners": 6, "capacityUnits": 14, "compileSlots": 2, "canonicalRoots": 2,
                 "guiRunners": 1, "fastRunners": 1}
        with mock.patch.object(cr, "ios_sim_present", return_value=True):
            dev, dev_hooks = self.fast_labels(shape, "nightly-dev-baseline", dev_compile=True, ios_sim=True)
            both, both_hooks = self.fast_labels(shape, "nightly-and-dev", dev_compile=True, ios_sim=True,
                                                nightly_slot=True)
        for instance in (0, 1, 2, 4, 5):
            self.assertEqual(both[instance], dev[instance], f"neighbor {instance} keeps every label")
            self.assertEqual(both_hooks[instance], dev_hooks[instance], f"neighbor {instance} keeps its hook")
        self.assertEqual([label for label in both[3] if label not in cr.BASE_LABELS],
                         ["glaeda-mini", "glaeda-class-std", "glaeda-dedicated", "xcode-26.6",
                          "glaeda-nightly-std-xcode-26.6"], "no pool, side or iOS label: no PR job reaches it")
        self.assertEqual(["--nightly-runner" in text for text in both_hooks],
                         [False, False, False, True, False, False])
        self.assertIn("--test-keychain", both_hooks[3])
        self.assertNotIn("--dev-compile-runner", both_hooks[3])
        with mock.patch.object(cr, "ios_sim_present", return_value=True):
            alone, _ = self.fast_labels(shape, "nightly-alone", ios_sim=True, nightly_slot=True)
        self.assertEqual(["glaeda-nightly-std-xcode-26.6" in labels for labels in alone],
                         [False, False, True, False, False, False], "without a freshness slot: the first side instance")
        self.assertTrue(hook.runner_scope(self.fast_home / "actions-runner-glaeda-2").nightly)
        self.assertFalse(hook.runner_scope(self.fast_home / "actions-runner-glaeda-3").nightly)

    def test_dev_compile_install_refuses_without_existing_side_instance(self) -> None:
        manifest = copy.deepcopy(MANIFEST)
        manifest["defaults"]["runner"] = {"devCompile": True, "classes": {"std": {"runners": 1}}}
        path = self.state / "dev-no-side.json"
        path.write_text(json.dumps(manifest))
        err = io.StringIO()
        with mock.patch.object(cr, "xcode_present", return_value=True), contextlib.redirect_stderr(err):
            self.assertEqual(cr.main(["--apply", "--manifest", os.fspath(path), "--member", "mini-std"]), 2)
        self.assertIn("needs an existing side instance", err.getvalue())
        self.assertEqual(self.tree(), {}, "invalid routing fails before installing or relabeling a runner")

    def test_fast_runners_one_makes_the_last_instance_the_fast_runner(self) -> None:
        base = {"runners": 5, "capacityUnits": 10, "compileSlots": 2, "canonicalRoots": 2}
        today, today_hooks = self.fast_labels(base, "today")
        roles = lambda l: [x for x in l if x.startswith(("glaeda-std", "glaeda-root", "glaeda-side", "glaeda-gui",
                                                          "glaeda-fast"))]
        self.assertEqual([roles(l) for l in today],
                         [["glaeda-std-xcode-26.6", "glaeda-root-std-xcode-26.6"]] * 2
                         + [["glaeda-std-xcode-26.6", "glaeda-side-std-xcode-26.6", "glaeda-side-light-xcode-26.6"]] * 3)
        zero, zero_hooks = self.fast_labels({**base, "fastRunners": 0}, "zero")
        self.assertEqual((zero, zero_hooks), (today, today_hooks), "fastRunners 0 is today's fleet exactly")
        self.assertFalse([h for h in today_hooks if "--fast-runner" in h])
        fast, fast_hooks = self.fast_labels({**base, "fastRunners": 1}, "fast")
        self.assertEqual(fast[:4], today[:4], "every other runner keeps its labels")
        self.assertEqual(fast_hooks[:4], today_hooks[:4])
        self.assertEqual(roles(fast[4]), ["glaeda-fast-std-xcode-26.6"], "only the fast label, no pool or side one")
        self.assertEqual([l.count("glaeda-fast-std-xcode-26.6") for l in fast], [0, 0, 0, 0, 1])
        self.assertIn("--canonical-roots 2 --instance 4 --fast-runner\n", fast_hooks[4])
        self.assertTrue(hook.runner_scope(self.fast_home / "actions-runner-glaeda-4").fast)
        self.assertFalse(hook.runner_scope(self.fast_home / "actions-runner-glaeda-3").fast)
        # with a gui runner too, the fast runner stays last and the gui runner is the one before it
        both, both_hooks = self.fast_labels({**base, "guiRunners": 1, "fastRunners": 1}, "both")
        self.assertEqual([roles(l) for l in both[3:]], [["glaeda-gui-std-xcode-26.6"], ["glaeda-fast-std-xcode-26.6"]])
        self.assertIn("--gui-runner", both_hooks[3])
        self.assertNotIn("--gui-runner", both_hooks[4])

    def test_a_plain_rerun_keeps_the_fast_and_gui_roles(self) -> None:
        """A re-run without --manifest restores the member from the receipt; the fast runner's and the gui
        runner's hooks must stay theirs, or fast-tier costs two shared units on the fast runner again."""
        base = {"runners": 5, "capacityUnits": 10, "compileSlots": 2, "canonicalRoots": 2}
        self.fast_labels({**base, "guiRunners": 1, "fastRunners": 1}, "rerun")
        home = self.fast_home
        with mock.patch.dict(os.environ, {"HOME": os.fspath(home)}), \
                mock.patch.object(cr, "xcode_present", return_value=True):
            for instance in ("4", "3"):
                self.invoke("--apply", "--instance", instance)
        hooks = {k: (home / f"actions-runner-glaeda-{k}/glaeda-hooks/job-started.sh").read_text() for k in (3, 4)}
        self.assertIn("--instance 4 --fast-runner\n", hooks[4], "the fast runner keeps --fast-runner")
        self.assertIn("--instance 3 --gui-runner", hooks[3], "the gui runner keeps --gui-runner")
        self.assertTrue(hook.runner_scope(home / "actions-runner-glaeda-4").fast)

    def test_test_keychain_is_created_unlocked_first_and_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "Library/Keychains").mkdir(parents=True)
            path = os.fspath(home / "Library/Keychains" / hook.TEST_KEYCHAIN)
            login = os.fspath(home / "Library/Keychains/login.keychain-db")
            state = {"list": [login], "default": login}
            calls: list[list[str]] = []

            def fake(argv: list[str], **_: object) -> subprocess.CompletedProcess:
                calls.append(argv[1:])
                verb, rest = argv[1], argv[2:]
                out = ""
                if verb == "create-keychain":
                    Path(rest[-1]).write_bytes(b"")
                elif verb == "list-keychains" and "-s" in rest:
                    state["list"] = rest[rest.index("-s") + 1:]
                elif verb == "list-keychains":
                    out = "".join(f'    "{k}"\n' for k in state["list"])
                elif verb == "default-keychain" and "-s" in rest:
                    state["default"] = rest[-1]
                elif verb == "default-keychain":
                    out = f'    "{state["default"]}"\n'
                return subprocess.CompletedProcess(argv, 0, out, "")

            with mock.patch.object(hook.subprocess, "run", side_effect=fake):
                self.assertIn("unlocked and default", hook.ensure_test_keychain(home))
                self.assertEqual(state, {"list": [path, login], "default": path})
                self.assertIn(["create-keychain", "-p", "", path], calls)
                self.assertIn(["set-keychain-settings", path], calls, "no lock timeout, no lock on sleep")
                calls.clear()
                self.assertIn("unlocked and default", hook.ensure_test_keychain(home))
                self.assertEqual([c[0] for c in calls],
                                 ["set-keychain-settings", "unlock-keychain", "list-keychains", "default-keychain"],
                                 "idempotent: nothing recreated or reordered")
                self.assertFalse(any(login in c for c in calls if c[0] != "list-keychains"),
                                 "the login keychain is never changed")
            state.update({"list": [login, path], "default": login})  # present but not first: reordered
            with mock.patch.object(hook.subprocess, "run", side_effect=fake):
                self.assertIn("unlocked and default", hook.ensure_test_keychain(home))
                self.assertEqual(state, {"list": [path, login], "default": path})

            def failing(verb: str):
                def run(argv: list[str], **kw: object) -> subprocess.CompletedProcess:
                    done = fake(argv, **kw)
                    return (subprocess.CompletedProcess(argv, 1, "", "no") if argv[1] == verb and "-s" in argv
                            else done)
                return run
            for verb, why in (("list-keychains", "search list"), ("default-keychain", "default")):
                state.update({"list": [login], "default": login})
                with self.subTest(verb), mock.patch.object(hook.subprocess, "run", side_effect=failing(verb)):
                    self.assertIn(f"setting the {why} failed", hook.ensure_test_keychain(home))
            with mock.patch.object(hook.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 51, "", "locked")):
                self.assertEqual(hook.ensure_test_keychain(home), "test keychain: unlock failed")
            with mock.patch.object(hook.subprocess, "run", side_effect=subprocess.TimeoutExpired("security", 15)):
                self.assertEqual(hook.ensure_test_keychain(home), "test keychain: TimeoutExpired")

    def test_test_keychain_is_unlocked_in_the_gui_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plist = home / "Library/LaunchAgents" / f"{hook.GUI_UNLOCK_LABEL}.plist"
            service = f"gui/501/{hook.GUI_UNLOCK_LABEL}"
            loaded: set[str] = set()
            calls: list[list[str]] = []

            def fake(argv: list[str], **_: object) -> subprocess.CompletedProcess:
                calls.append(argv[1:])
                verb = argv[1]
                if verb == "bootstrap":
                    loaded.add(service)
                    return subprocess.CompletedProcess(argv, 0, "", "")
                code = 0 if argv[2] in loaded else 113
                if verb == "bootout":
                    loaded.discard(argv[2])
                return subprocess.CompletedProcess(argv, code, "", "")

            with mock.patch.object(hook.subprocess, "run", side_effect=fake):
                self.assertEqual(hook.unlock_test_keychain_in_gui(home, 501), "test keychain: GUI session unlock started")
                agent = plistlib.loads(plist.read_bytes())
                self.assertEqual(agent["LimitLoadToSessionType"], "Aqua", "the unlock must run in the desktop's session")
                self.assertTrue(agent["RunAtLoad"], "every login unlocks it again")
                keychain = os.fspath(home / "Library/Keychains" / hook.TEST_KEYCHAIN)
                self.assertEqual(agent["ProgramArguments"], ["/bin/sh", "-c", hook.GUI_UNLOCK_SCRIPT, "sh", keychain])
                self.assertEqual(calls, [["bootout", service], ["kickstart", service],
                                         ["bootstrap", "gui/501", os.fspath(plist)]])
                calls.clear()
                self.assertEqual(hook.unlock_test_keychain_in_gui(home, 501), "test keychain: GUI session unlock started")
                self.assertEqual(calls, [["kickstart", service]], "loaded: a kickstart reruns the unlock")
                calls.clear()
                plist.write_bytes(plistlib.dumps({"Label": hook.GUI_UNLOCK_LABEL, "ProgramArguments": ["old"]}))
                self.assertEqual(hook.unlock_test_keychain_in_gui(home, 501), "test keychain: GUI session unlock started")
                self.assertEqual(calls[0], ["bootout", service], "a changed agent is reloaded, not rerun as it was")
                self.assertIn(service, loaded)
            with mock.patch.object(hook.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 5, "", "no gui")):
                self.assertEqual(hook.unlock_test_keychain_in_gui(home, 501), "test keychain: no GUI session to unlock it in")
            with mock.patch.object(hook.subprocess, "run", side_effect=subprocess.TimeoutExpired("launchctl", 15)):
                self.assertEqual(hook.unlock_test_keychain_in_gui(home, 501), "test keychain: GUI unlock TimeoutExpired")

    def test_diagnostics_reporter_queues_are_emptied_before_it_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            panics, diagnostics, reports, home = (root / "PanicReporter", root / "DiagnosticsReporter",
                                                  root / "DiagnosticReports", root / "home")
            for folder in (panics, diagnostics, reports, home):
                folder.mkdir()
            summary = reports / ".contents.panic"
            summary.write_text("summary")
            full = reports / "panic-full-2026-09-25.panic"
            full.write_text("full")
            victim = home / "keep.txt"
            victim.write_text("user file")
            os.symlink(summary, panics / "current.panic")
            # A planted entry naming a file outside the reports directory: only the entry goes.
            os.symlink(f"{reports}/.contents.x/../../home/keep.txt", panics / "escape")
            os.symlink(victim, panics / "other")
            (diagnostics / "crash").mkdir()
            (diagnostics / "crash" / "report").write_text("x")
            os.symlink(home, diagnostics / "linked-dir")
            calls: list[list[str]] = []

            def fake(argv: list[str], **_: object) -> subprocess.CompletedProcess:
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, "", "")

            with mock.patch.object(hook.subprocess, "run", side_effect=fake):
                note = hook.dequeue_diagnostics_reporter((panics, diagnostics, root / "missing"), reports, 501)
            self.assertEqual(note, "dequeued 5 Diagnostics Reporter entries; closed Diagnostics Reporter")
            self.assertEqual(list(panics.iterdir()) + list(diagnostics.iterdir()), [])
            self.assertFalse(summary.exists(), "the unanswered summary goes with its entry")
            self.assertEqual((full.read_text(), victim.read_text()), ("full", "user file"))
            self.assertTrue(home.is_dir(), "a symlinked entry is removed, never followed")
            self.assertEqual(calls, [["/usr/bin/pkill", "-KILL", "-u", "501", "-f",
                                      "/Diagnostics Reporter.app/Contents/MacOS/"]],
                             "closed only after the queues are empty")
            with mock.patch.object(hook.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 1, "", "")):
                self.assertEqual(hook.dequeue_diagnostics_reporter((panics, diagnostics), reports, 501), "")
                # Another runner instance removed the entry first: gone, not a failure.
                (panics / "raced").write_text("x")
                with mock.patch.object(hook.Path, "unlink", side_effect=FileNotFoundError):
                    self.assertEqual(hook.dequeue_diagnostics_reporter((panics,), reports, 501), "")

    def test_gui_unlock_script_cancels_every_queued_prompt(self) -> None:
        # Shell-function fakes: each kill finds SecurityAgent while a prompt is left.
        def run(unlock_code: int, prompts: int) -> tuple[int, list[str]]:
            script = hook.GUI_UNLOCK_SCRIPT
            for tool in ("/usr/bin/security", "/usr/bin/pkill", "/usr/bin/id"):
                script = script.replace(tool, "fake_" + tool.rsplit("/", 1)[1])
            fakes = (f'left={prompts}\nfake_security() {{ echo "security $*"; return {unlock_code}; }}\n'
                     'fake_pkill() { echo "pkill $*"; [ "$left" -gt 0 ] || return 1; left=$((left - 1)); }\n'
                     'fake_id() { echo 501; }\nsleep() { :; }\n')
            done = subprocess.run(["/bin/sh", "-c", fakes + script, "sh", "/k"], capture_output=True, text=True,
                                  timeout=10)
            return done.returncode, done.stdout.splitlines()

        kill = "pkill -KILL -x -u 501 SecurityAgent"
        code, lines = run(0, 0)
        self.assertEqual((code, lines), (0, ["security unlock-keychain -p  /k"] + [kill] * 15),
                         "unlock first; with no prompt, 15 quiet checks and done")
        code, lines = run(0, 2)
        self.assertEqual((code, lines[1:]), (0, [kill] * 17),
                         "each queued prompt is killed, and the 15 s wait restarts after it")
        code, lines = run(0, 99)
        self.assertEqual((code, lines.count(kill)), (0, 30), "bounded")
        code, lines = run(1, 2)
        self.assertEqual((code, lines), (1, ["security unlock-keychain -p  /k"]),
                         "still locked: leave the prompts to the next run")

    def test_instances_get_their_own_paths_and_share_the_capacity(self) -> None:
        with mock.patch.object(cr, "xcode_present", return_value=True):
            first = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std")
            third = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std", "--instance", "3")
        names = [argv[argv.index("--name") + 1] for argv in self.config_argvs()]
        self.assertEqual(names, ["mini-std-glaeda", "mini-std-glaeda-3"])
        labels = [argv[argv.index("--labels") + 1].split(",") for argv in self.config_argvs()]
        self.assertEqual([("glaeda-root-std-xcode-26.6" in l, "glaeda-std-xcode-26.6" in l,
                           "glaeda-side-std-xcode-26.6" in l) for l in labels],
                         [(True, True, False), (False, True, True)],
                         "only instance 0 is the root runner of a one-root mini; the rest carry the side label")
        self.assertEqual([[x for x in l if x.startswith("glaeda-runner-")] for l in labels],
                         [["glaeda-runner-mini-std-glaeda"], []],
                         "a root runner carries a label naming only itself; a side runner none")
        self.assertEqual(third["runnerDir"], os.fspath(self.home / "actions-runner-glaeda-3"))
        self.assertEqual(first["runnerDir"], os.fspath(self.home / "actions-runner-glaeda"))
        plist = self.home / "Library/LaunchAgents/com.teamleaderleo.glaeda.cmux-runner.3.plist"
        self.assertEqual(plistlib.loads(plist.read_bytes())["Label"], "com.teamleaderleo.glaeda.cmux-runner.3")
        self.assertTrue((self.home / ".local/state/glaeda/cmux-runner/instance-3/receipt.json").is_file())
        self.assertTrue((self.home / ".local/state/glaeda/cmux-runner/receipt.json").is_file())
        for runner in ("actions-runner-glaeda", "actions-runner-glaeda-3"):
            self.assertIn("--capacity-units 14", (self.home / runner / "glaeda-hooks/job-started.sh").read_text())
        # a plain re-run keeps the member's capacity, gate and floor
        self.invoke("--apply", "--instance", "3")
        wrapper = (self.home / "actions-runner-glaeda-3/glaeda-hooks/job-started.sh").read_text()
        self.assertIn("--capacity-units 14", wrapper)
        self.assertIn("--require-eligible --fleet-class m4pro-48", wrapper)
        err = io.StringIO()
        with mock.patch.object(cr, "xcode_present", return_value=True), contextlib.redirect_stderr(err):
            self.assertEqual(cr.main(["--gh", os.fspath(self.gh), "--manifest", self.manifest(), "--member",
                                      "mini-std", "--instance", "4"]), 2)
        self.assertIn("instance 4 is beyond that", err.getvalue())

    def test_manifest_refusals_and_exclusive_flags(self) -> None:
        for args in (("--manifest", self.manifest(), "--member", "laptop"),
                     ("--manifest", self.manifest(), "--member", "mini-std", "--labels", "x"),
                     ("--manifest", self.manifest()), ("--member", "mini-std")):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cr.main(["--gh", os.fspath(self.gh), *args]), 2)
        self.assertEqual(self.tree(), {})

    def test_relabel_reregisters_in_place_and_keeps_work(self) -> None:
        self.invoke("--apply", "--labels", "ram48")  # the pre-manifest fleet install
        runner = self.home / "actions-runner-glaeda"
        (runner / "_work").mkdir(exist_ok=True)
        (runner / "_work" / "hot").write_text("derived data")
        for stale in (".runner_migrated", ".credentials_migrated"):
            (runner / stale).write_text("{}")
        with mock.patch.object(cr, "xcode_present", return_value=True):
            plan = self.invoke("--manifest", self.manifest(), "--member", "mini-std", "--name", "mini-test-glaeda")
            self.assertEqual(self.by_kind(plan)["register"]["state"], "relabel")
            self.assertEqual(self.by_kind(plan)["register"]["previousLabels"],
                             ["ARM64", "glaeda-mini", "macOS", "ram48", "self-hosted"])
            receipt = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                                  "--name", "mini-test-glaeda")
        reg = self.by_kind(receipt)["register"]
        self.assertEqual(reg["state"], "updated", reg)
        argv = self.config_argvs()[-1]
        self.assertIn("--replace", argv)
        self.assertEqual(argv[argv.index("--labels") + 1], "glaeda-mini,glaeda-class-std,glaeda-dedicated,xcode-26.6,glaeda-std-xcode-26.6,"
                                                         "glaeda-root-std-xcode-26.6,glaeda-runner-mini-test-glaeda")
        runners = json.loads((self.state / "runners.json").read_text())["runners"]
        self.assertEqual([(r["name"], r["id"]) for r in runners], [("mini-test-glaeda", 4243)])
        self.assertEqual((runner / "_work" / "hot").read_text(), "derived data")
        self.assertFalse((runner / ".runner_migrated").exists())
        self.assertFalse((runner / ".credentials_migrated").exists())
        saved = json.loads((self.home / ".local/state/glaeda/cmux-runner/receipt.json").read_text())
        self.assertEqual(saved["registration"]["runnerId"], 4243)
        self.assertIn("glaeda-class-std", saved["registration"]["labels"])
        self.assertEqual(self.by_kind(receipt)["verify"]["state"], "ok")

    def test_reregistration_over_bare_ssh_path_keeps_homebrew_and_cargo_on_runner_path(self) -> None:
        # 2026-10-26: a re-registration over ssh left .path at /usr/bin:/bin:/usr/sbin:/sbin, without Homebrew.
        bare = "/usr/bin:/bin:/usr/sbin:/sbin"
        runner = self.home / "actions-runner-glaeda"
        cargo = os.fspath(self.home / ".cargo/bin")
        local = os.fspath(self.home / ".local/bin")
        with mock.patch.dict(os.environ, {"PATH": bare}):
            self.invoke("--apply", "--labels", "ram48")
            entries = (runner / ".path").read_text().strip().split(":")
            self.assertEqual(entries[:2], ["/opt/homebrew/bin", "/usr/local/bin"])
            self.assertIn(cargo, entries)
            self.assertTrue(set(bare.split(":")) <= set(entries), entries)
            self.assertEqual(len(entries), len(set(entries)), entries)
            # a hand-added entry survives a re-registration (--replace) that runs under the bare PATH again
            (runner / ".path").write_text(f"{local}:{bare}\n")
            with mock.patch.object(cr, "xcode_present", return_value=True):
                receipt = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                                      "--name", "mini-test-glaeda")
        self.assertEqual(self.by_kind(receipt)["register"]["state"], "updated")
        self.assertIn("--replace", self.config_argvs()[-1])
        entries = (runner / ".path").read_text().strip().split(":")
        self.assertEqual(entries[:2], ["/opt/homebrew/bin", "/usr/local/bin"])
        self.assertIn(local, entries)
        self.assertIn(cargo, entries)
        self.assertNotIn("", entries)

    def test_failed_reregistration_still_keeps_homebrew_on_runner_path(self) -> None:
        # env.sh rewrites .path before config.sh rejects the token: the failed relabel must not leave it bare
        bare = "/usr/bin:/bin:/usr/sbin:/sbin"
        runner = self.home / "actions-runner-glaeda"
        local = os.fspath(self.home / ".local/bin")
        with mock.patch.dict(os.environ, {"PATH": bare}):
            self.invoke("--apply", "--labels", "ram48")
            (runner / ".path").write_text(f"{local}:{bare}\n")
            with mock.patch.object(cr, "xcode_present", return_value=True), \
                    mock.patch.object(cr, "read_token", lambda gh, ep: REG_TOKEN + "X"):
                receipt = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                                      "--name", "mini-test-glaeda", expect=1)
        self.assertEqual(self.by_kind(receipt)["register"]["state"], "failed")
        self.assertIn("--replace", self.config_argvs()[-1])
        entries = (runner / ".path").read_text().strip().split(":")
        self.assertEqual(entries[:2], ["/opt/homebrew/bin", "/usr/local/bin"])
        self.assertIn(local, entries)
        self.assertIn(os.fspath(self.home / ".cargo/bin"), entries)

    def test_reregistration_with_a_non_utf8_runner_path_keeps_its_bytes(self) -> None:
        bare = "/usr/bin:/bin:/usr/sbin:/sbin"
        runner = self.home / "actions-runner-glaeda"
        with mock.patch.dict(os.environ, {"PATH": bare}):
            self.invoke("--apply", "--labels", "ram48")
            (runner / ".path").write_bytes(b"/opt/odd\xff/bin:/usr/bin\n")
            with mock.patch.object(cr, "xcode_present", return_value=True):
                receipt = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                                      "--name", "mini-test-glaeda")
        self.assertEqual(self.by_kind(receipt)["register"]["state"], "updated")
        entries = (runner / ".path").read_bytes().strip().split(b":")
        self.assertEqual(entries[:3], [b"/opt/homebrew/bin", b"/usr/local/bin", b"/opt/odd\xff/bin"])
        self.assertEqual(cr.read_runner_path(self.home / "missing"), "")

    def test_runner_path_merges_in_order_without_duplicates(self) -> None:
        home = Path("/Users/cmux")
        self.assertEqual(cr.runner_path(home, "/usr/bin:/bin:/usr/sbin:/sbin"),
                         "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/cmux/.cargo/bin")
        self.assertEqual(cr.runner_path(home, "/Users/cmux/.local/bin:/Users/cmux/.cargo/bin:/usr/bin:/bin",
                                        "/usr/bin:/bin:/usr/sbin:/sbin"),
                         "/opt/homebrew/bin:/usr/local/bin:/Users/cmux/.local/bin:/Users/cmux/.cargo/bin:"
                         "/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertEqual(cr.runner_path(home, ""), "/opt/homebrew/bin:/usr/local/bin:/Users/cmux/.cargo/bin:"
                                                   "/usr/bin:/bin:/usr/sbin:/sbin")

    def human(self, *args: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "stdin", io.StringIO("")):
            cr.main(["--gh", os.fspath(self.gh), "--python", sys.executable, *args])
        return out.getvalue()

    def test_a_fleet_member_gets_no_macos_runner_hint(self) -> None:
        with mock.patch.object(cr, "xcode_present", return_value=True):
            plan = self.invoke("--manifest", self.manifest(), "--member", "mini-std")
            text = self.human("--manifest", self.manifest(), "--member", "mini-std")
        self.assertEqual((plan["routing"]["route"], plan["routing"]["rollback"]), ([], []))
        self.assertIn("pool picker", plan["routing"]["note"])
        self.assertNotIn("gh variable set MACOS_RUNNER", text)
        self.assertIn("routing: fleet member", text)
        self.assertIn("Never set MACOS_RUNNER_*", text)

    def test_a_trusted_member_prints_the_nightly_variables(self) -> None:
        manifest = json.loads(json.dumps(MANIFEST))
        manifest["hosts"]["mini-std"]["overrides"] = {"runner": {"trustedRef": "refs/heads/main",
                                                                 "trustedRepo": "manaflow-ai/cmux"}}
        path = self.state / "trusted.json"
        path.write_text(json.dumps(manifest))
        with mock.patch.object(cr, "xcode_present", return_value=True):
            plan = self.invoke("--manifest", os.fspath(path), "--member", "mini-std")
        self.assertEqual(plan["routing"]["route"], [
            "gh variable set CI_SEED_TRUSTED_POOL --body glaeda-trusted-std-xcode-26.6 --repo manaflow-ai/cmux",
            "gh variable set CI_NIGHTLY_TRUSTED_RUNNER --body glaeda-runner-mini-std-glaeda --repo manaflow-ai/cmux"])
        self.assertEqual(plan["routing"]["rollback"], ["gh variable delete CI_NIGHTLY_TRUSTED_RUNNER --repo manaflow-ai/cmux"])
        self.assertNotIn("MACOS_RUNNER_15 --body", json.dumps(plan["routing"]))

    def test_label_drift_against_the_receipt_and_github_is_reported(self) -> None:
        self.invoke("--apply", "--labels", "ram48")
        with mock.patch.object(cr, "xcode_present", return_value=True):
            plan = self.invoke("--manifest", self.manifest(), "--member", "mini-std", "--name", "mini-test-glaeda")
            text = self.human("--manifest", self.manifest(), "--member", "mini-std", "--name", "mini-test-glaeda")
        drift = self.by_kind(plan)["register"]["drift"]
        self.assertEqual({d["against"] for d in drift}, {"receipt", "github"})
        receipt_drift = next(d for d in drift if d["against"] == "receipt")
        self.assertEqual(receipt_drift["removed"], ["ram48"])
        self.assertIn("glaeda-runner-mini-test-glaeda", receipt_drift["added"])
        self.assertIn("registered labels differ from what this version would register: +glaeda-class-std", text)
        # GitHub lost a label behind the receipt's back: a plain re-run (labels from the receipt) still says so
        runners = json.loads((self.state / "runners.json").read_text())
        runners["runners"][0]["labels"] = [label for label in runners["runners"][0]["labels"] if label["name"] != "ram48"]
        (self.state / "runners.json").write_text(json.dumps(runners))
        again = self.by_kind(self.invoke())["register"]
        self.assertEqual(again["state"], "unchanged")
        self.assertEqual(again["drift"], [{"against": "github", "added": ["ram48"], "removed": []}])
        self.assertIn("registered labels differ", again["note"])
        self.assertTrue(all(e["argv"][:3] != ["api", "-X", "POST"] for e in self.log()[-6:] if e["tool"] == "gh"))

    def test_manifest_relabels_when_github_labels_are_stale_despite_matching_receipt(self) -> None:
        with mock.patch.object(cr, "xcode_present", return_value=True):
            self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std")
            runners = json.loads((self.state / "runners.json").read_text())
            runners["runners"][0]["labels"] = [{"name": "self-hosted"}, {"name": "macOS"},
                                                   {"name": "ARM64"}, {"name": "glaeda-mini"},
                                                   {"name": "glaeda-side-std-xcode-26.6"}]
            (self.state / "runners.json").write_text(json.dumps(runners))
            plan = self.invoke("--manifest", self.manifest(), "--member", "mini-std")
        register = self.by_kind(plan)["register"]
        self.assertEqual(register["state"], "relabel")
        self.assertEqual(register["previousLabels"],
                         ["ARM64", "glaeda-mini", "glaeda-side-std-xcode-26.6", "macOS", "self-hosted"])

    def test_manifest_blocks_when_github_labels_cannot_be_read(self) -> None:
        with mock.patch.object(cr, "xcode_present", return_value=True):
            self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std")
            (self.state / "fail-list").touch()
            plan = self.invoke("--manifest", self.manifest(), "--member", "mini-std")
        register = self.by_kind(plan)["register"]
        self.assertEqual(register["state"], "blocked")
        self.assertIn("receipt labels are not authoritative", register["note"])

    def test_labels_the_fleet_read_spare_a_mini_without_gh_its_re_registration(self) -> None:
        # the minis hold no gh login, so each fleet apply re-registered every idle runner in place whatever its labels
        real_which = cr.which
        with mock.patch.object(cr, "xcode_present", return_value=True):
            first = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std")
            labels = ",".join(first["registration"]["labels"])
            configs = len(self.config_argvs())
            with mock.patch.object(cr, "which", lambda name, extra=(): None if name == "gh" else real_which(name, extra)):
                same = self.invoke("--apply", "--token-stdin", "--manifest", self.manifest(), "--member", "mini-std",
                                   "--github-labels", labels, gh=False, stdin=REG_TOKEN)
                self.assertEqual(self.by_kind(same)["register"]["state"], "unchanged", self.by_kind(same)["register"])
                self.assertEqual(len(self.config_argvs()), configs, "matching labels: no config.sh, no bootout")
                stale = self.invoke("--manifest", self.manifest(), "--member", "mini-std", "--token-stdin",
                                    "--github-labels", "self-hosted,glaeda-mini", gh=False)
        self.assertEqual(self.by_kind(stale)["register"]["state"], "relabel")
        self.assertEqual(self.by_kind(stale)["register"]["previousLabels"], ["glaeda-mini", "self-hosted"])

    def test_an_uninstall_never_stops_or_deregisters_a_runner_with_a_job(self) -> None:
        for skip in (False, True):
            ctx = mock.Mock(skip_launchctl=skip, headless=False, sandbox=True)
            actions = [{"kind": "agent", "state": "remove", "path": "/nonexistent.plist"},
                       {"kind": "deregister", "state": "remove", "name": "x"}]
            with mock.patch.object(cr, "listeners_frozen", return_value=contextlib.nullcontext(0)), \
                    mock.patch.object(cr, "runner_busy", return_value=True), \
                    mock.patch.object(cr, "bootout") as bootout, mock.patch.object(cr, "run_with_token") as config:
                cr.apply_uninstall(ctx, actions)
            bootout.assert_not_called()
            config.assert_not_called()
            self.assertEqual([a["state"] for a in actions], ["failed", "failed"], skip)
        # a plist this install does not own, still loaded in launchd (KeepAlive restarts it): no deregister
        ctx = mock.Mock(skip_launchctl=False, headless=False, sandbox=False, label="x", uid=501)
        actions = [{"kind": "agent", "state": "kept", "path": "/x.plist"}, {"kind": "deregister", "state": "remove"}]
        with mock.patch.object(cr, "listeners_frozen", return_value=contextlib.nullcontext(0)), \
                mock.patch.object(cr, "runner_busy", return_value=False), mock.patch.object(cr, "run", return_value=(0, "")), \
                mock.patch.object(cr, "run_with_token") as config:
            cr.apply_uninstall(ctx, actions)
        config.assert_not_called()
        self.assertEqual(actions[1]["state"], "failed")
        # a plist this install does not own (kept): a live listener refuses the deregister
        ctx = mock.Mock(skip_launchctl=False, headless=False, sandbox=True)
        actions = [{"kind": "agent", "state": "kept", "path": "/x.plist"}, {"kind": "deregister", "state": "remove"}]
        with mock.patch.object(cr, "listeners_frozen", return_value=contextlib.nullcontext(1)), \
                mock.patch.object(cr, "runner_busy", return_value=False), mock.patch.object(cr, "run_with_token") as config:
            cr.apply_uninstall(ctx, actions)
        config.assert_not_called()
        self.assertEqual(actions[1]["state"], "failed")

    def test_fleet_labels_stand_in_for_a_runner_list_this_gh_cannot_read(self) -> None:
        with mock.patch.object(cr, "xcode_present", return_value=True):
            first = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std")
            (self.state / "fail-list").touch()  # gh is logged in but cannot list runners
            same = self.invoke("--apply", "--token-stdin", "--manifest", self.manifest(), "--member", "mini-std",
                               "--github-labels=" + ",".join(first["registration"]["labels"]), stdin=REG_TOKEN,
                               expect=1)  # verify reads the same broken list
        self.assertEqual(self.by_kind(same)["register"]["state"], "unchanged", self.by_kind(same)["register"])
        self.assertEqual(len(self.config_argvs()), 1, "no re-registration")

    def test_manifest_apply_with_token_relabels_when_github_lookup_is_unavailable(self) -> None:
        with mock.patch.object(cr, "xcode_present", return_value=True):
            self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std")
            (self.state / "fail-list").touch()
            receipt = self.invoke("--apply", "--token-stdin", "--manifest", self.manifest(),
                                 "--member", "mini-std", stdin=REG_TOKEN, expect=1)
        register = self.by_kind(receipt)["register"]
        self.assertEqual(register["state"], "updated", register)
        self.assertIn("--replace", self.config_argvs()[-1])
        self.assertEqual(self.by_kind(receipt)["verify"]["state"], "failed")
        self.assertFalse(receipt["ready"])

    def test_apply_never_re_registers_a_self_updated_runner_with_a_job(self) -> None:
        # 2026-10-27 02:09:59Z: a runner that updated itself runs bin.2.338.0/Runner.Worker; a pgrep for
        # bin/Runner.Worker read it as idle and --apply booted it out mid-compile to re-register it
        with mock.patch.object(cr, "xcode_present", return_value=True):
            first = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std")
            (self.state / "fail-list").touch()
            worker = f"{first['runnerDir']}/bin.2.338.0/Runner.Worker spawnclient 148 151"
            real_run = subprocess.run
            def ps(argv, *args, **kwargs):  # the process table of a mini with that job
                if list(argv[:2]) == ["/bin/ps", "-axww"]:
                    return subprocess.CompletedProcess(argv, 0, f"/sbin/launchd\n{worker}\n", "")
                return real_run(argv, *args, **kwargs)
            with mock.patch.object(cr.subprocess, "run", side_effect=ps):
                receipt = self.invoke("--apply", "--token-stdin", "--manifest", self.manifest(),
                                      "--member", "mini-std", stdin=REG_TOKEN, expect=1)
        register = self.by_kind(receipt)["register"]
        self.assertEqual(register["state"], "blocked", register)
        self.assertIn("running a job", register["note"])
        self.assertNotIn("bootout", [e["argv"][0] for e in self.log() if e["tool"] == "launchctl"])

    def test_a_real_self_updated_worker_is_busy_and_its_listener_is_frozen_for_the_stop(self) -> None:
        runner = self.home / "actions-runner-glaeda-7"
        (runner / "_diag").mkdir(parents=True)
        def spawn(argv0: str) -> subprocess.Popen:  # a process ps lists as argv0 (python re-execs, so a sleep)
            proc = subprocess.Popen(["/bin/bash", "-c", 'exec -a "$0" /bin/sleep 60', argv0])
            self.addCleanup(lambda: (proc.kill(), proc.wait()))
            return proc
        self.assertFalse(cr.worker_running(runner))
        listener = spawn(f"{runner}/bin/Runner.Listener")
        spawner = subprocess.Popen(["/bin/bash", "-c", 'exec -a "$0" /bin/sleep 60',  # the gate, which starts listeners
                                    f"/usr/bin/python3 hook listen --runner-dir {runner} --x"])
        self.addCleanup(lambda: (spawner.kill(), spawner.wait()))
        worker = spawn(f"{runner}/bin.2.338.0/Runner.Worker")
        deadline = time.monotonic() + 10
        while not cr.worker_running(runner):
            self.assertLess(time.monotonic(), deadline, "a live bin.2.338.0/Runner.Worker is this runner's job")
            time.sleep(0.05)
        ctx = mock.Mock(runner_dir=runner)
        with mock.patch.object(cr, "bootout") as bootout:
            self.assertFalse(cr.stop_if_idle(ctx))
            bootout.assert_not_called()
            worker.kill()
            worker.wait()
            def frozen(_ctx: object) -> bool:
                for pid in (listener.pid, spawner.pid):
                    state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                                           text=True).stdout.strip()
                    self.assertTrue(state.startswith("T"), f"listener and gate are stopped for the bootout: {state!r}")
                return True
            bootout.side_effect = frozen
            self.assertTrue(cr.stop_if_idle(ctx))
            bootout.assert_called_once()
        state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(listener.pid)], capture_output=True, text=True)
        self.assertFalse(state.stdout.strip().startswith("T"), "and thawed after")

    def test_a_listener_started_between_two_looks_is_frozen_too(self) -> None:
        runner = self.home / "actions-runner-glaeda-6"
        looks = iter([f"1 /usr/bin/python3 hook listen --runner-dir {runner} --x\n",
                      f"1 /usr/bin/python3 hook listen --runner-dir {runner} --x\n2 {runner}/bin/Runner.Listener run\n",
                      f"1 /usr/bin/python3 hook listen --runner-dir {runner} --x\n2 {runner}/bin/Runner.Listener run\n"])
        ps = lambda *a, **k: subprocess.CompletedProcess([], 0, next(looks), "")
        with mock.patch.object(cr.subprocess, "run", side_effect=ps), mock.patch.object(cr.os, "kill") as kill:
            with cr.listeners_frozen(runner) as frozen:
                self.assertEqual(frozen, 2, "the gate, and the listener it started after the first look")
        self.assertEqual([c.args for c in kill.call_args_list],
                         [(1, signal.SIGSTOP), (2, signal.SIGSTOP), (2, signal.SIGCONT), (1, signal.SIGCONT)])
        endless = iter(f"{n} {runner}/bin/Runner.Listener run\n" for n in range(1, 99))
        with mock.patch.object(cr.subprocess, "run", side_effect=lambda *a, **k: subprocess.CompletedProcess(
                [], 0, next(endless), "")), mock.patch.object(cr.os, "kill"):
            with cr.listeners_frozen(runner) as frozen:
                self.assertIsNone(frozen, "a tree that keeps changing is not frozen: stop nothing")

    def test_a_skip_launchctl_stop_refuses_a_live_spawner_without_a_listener(self) -> None:
        runner = self.home / "actions-runner-glaeda-9"
        gate = subprocess.Popen(["/bin/bash", "-c", 'exec -a "$0" /bin/sleep 60',
                                 f"/usr/bin/python3 hook listen --runner-dir {runner} --x"])
        self.addCleanup(lambda: (gate.kill(), gate.wait()))
        time.sleep(0.2)
        stop = mock.Mock(return_value=True)
        with mock.patch.object(cr, "runner_busy", return_value=False):
            self.assertFalse(cr.stop_if_idle(mock.Mock(runner_dir=runner), stop, listener_ok=False),
                             "a gate between listeners starts one the moment it is thawed")
        stop.assert_not_called()

    def test_a_relabel_whose_bootout_did_not_complete_keeps_the_registration(self) -> None:
        runner = self.home / "actions-runner-glaeda-8"
        runner.mkdir()
        (runner / ".runner").write_text("{}")
        ctx = mock.Mock(runner_dir=runner, skip_launchctl=False)
        act: dict = {}
        with mock.patch.object(cr, "listeners_frozen", return_value=contextlib.nullcontext(0)), \
                mock.patch.object(cr, "runner_busy", return_value=False), \
                mock.patch.object(cr, "bootout", return_value=False), mock.patch.object(cr, "register") as register:
            cr.relabel(ctx, act)
        register.assert_not_called()
        self.assertEqual(act["state"], "failed")
        self.assertTrue((runner / ".runner").exists())

    def test_a_bootout_is_done_only_when_launchd_says_the_service_is_gone(self) -> None:
        ctx = mock.Mock(headless=False, uid=501, label="x")
        with mock.patch.object(cr, "run", return_value=(127, "")), mock.patch.object(cr.time, "sleep"):
            self.assertFalse(cr.bootout(ctx, wait_s=0), "a print that failed (timeout, sudo) is no proof")
        with mock.patch.object(cr, "run", side_effect=[(0, ""), (0, ""), (cr.LAUNCHCTL_NOT_FOUND, "")]), \
                mock.patch.object(cr.time, "sleep"):
            self.assertTrue(cr.bootout(ctx))

    def test_listen_sh_patterns_match_the_runner_path_literally(self) -> None:
        self.assertEqual(cr.ere_escape("/Users/a.b/actions-runner[1]"), "/Users/a\\.b/actions-runner\\[1\\]")
        self.assertIn("re=", cr.hook_wrappers(mock.Mock(
            python=sys.executable, hook_dir=self.home / "h", runner_dir=self.home / "actions-runner-glaeda",
            org=None, repo="manaflow-ai/cmux", min_free_gib=0, member=None, capacity_units=4,
            labels=["glaeda-mini"], session_runner=False))["listen.sh"].decode())

    def test_a_stop_that_cannot_freeze_the_listeners_stops_nothing(self) -> None:
        ctx = mock.Mock(runner_dir=self.home / "actions-runner-glaeda")
        with mock.patch.object(cr.subprocess, "run", side_effect=OSError("no ps")), \
                mock.patch.object(cr, "runner_busy", return_value=False), mock.patch.object(cr, "bootout") as bootout:
            self.assertFalse(cr.stop_if_idle(ctx), "an idle reading without the freeze is no proof")
        bootout.assert_not_called()

    def test_a_skip_launchctl_relabel_refuses_a_live_listener(self) -> None:
        runner = self.home / "actions-runner-glaeda-5"
        runner.mkdir()
        (runner / ".runner").write_text("{}")
        ctx = mock.Mock(runner_dir=runner, skip_launchctl=True)
        act: dict = {}
        listener = subprocess.Popen(["/bin/bash", "-c", 'exec -a "$0" /bin/sleep 60', f"{runner}/bin/Runner.Listener"])
        self.addCleanup(lambda: (listener.kill(), listener.wait()))
        time.sleep(0.2)
        with mock.patch.object(cr, "register") as register:
            cr.relabel(ctx, act)
        register.assert_not_called()
        self.assertEqual(act["state"], "failed")
        self.assertTrue((runner / ".runner").exists(), "its registration is kept")

    def test_an_agent_relabel_started_is_never_booted_out_with_a_job(self) -> None:
        ctx = mock.Mock(skip_launchctl=False, agent_started=True, headless=False)
        act = {"kind": "agent", "state": "update", "loaded": False, "path": "/nonexistent.plist"}
        with mock.patch.object(cr, "runner_busy", return_value=True), mock.patch.object(cr, "bootout") as bootout, \
                mock.patch.object(cr, "listeners_frozen", return_value=contextlib.nullcontext()):
            cr.apply_install(ctx, [act], {})
        bootout.assert_not_called()
        self.assertEqual(act["state"], "failed")
        self.assertIn("re-run when idle", act["note"])

    def installed_release(self, labels: list[str], same_files: bool = False) -> Path:
        """An OTA release on this fake HOME whose plan registers `labels`; returns the log of its argv."""
        tag = "r-20260926-abcdefabcdef"
        root = self.home / ".local/state/glaeda/update"
        scripts = root / "generations" / tag / "glaeda/scripts"
        scripts.mkdir(parents=True)
        (root / "state.json").write_text(json.dumps({"current": tag, "previous": None, "quarantined": []}))
        seen = self.state / "release-plan.jsonl"
        if same_files:
            for name in cr.RUNNER_FILES:
                shutil.copy2(cr.SCRIPT_DIR / name, scripts / name)
            return seen
        make_executable(scripts / "glaeda-cmux-runner", f"""#!/usr/bin/env python3
import json, os, sys
with open({os.fspath(seen)!r}, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "guard": os.environ.get("{cr.NO_RELEASE_CHECK}")}}) + "\\n")
print(json.dumps({{"actions": [{{"kind": "register", "labels": {labels!r}}}]}}))
""")
        return seen

    def test_a_copy_older_than_the_installed_release_blocks_and_shows_its_labels(self) -> None:
        seen = self.installed_release(["self-hosted", "macOS", "ARM64", "glaeda-mini", "glaeda-runner-new"])
        old = {"date": "2026-09-24", "source": "a" * 40, "tag": None, "via": "stamp by fleet"}
        with mock.patch.object(cr, "script_source", return_value=old):
            plan = self.invoke("--token-stdin", expect=0)
            self.assertFalse(plan["ready"])
            self.assertIn("scriptCopy", plan["preflight"]["blocking"])
            self.assertEqual(plan["scriptCopy"]["state"], "stale")
            self.assertEqual(plan["scriptCopy"]["labelDrift"],
                             {"against": "release", "added": ["glaeda-runner-new"], "removed": []})
            self.assertIn(".local/state/glaeda/update/generations/r-20260926-abcdefabcdef/glaeda/scripts/glaeda-cmux-runner",
                          plan["preflight"]["checks"]["scriptCopy"]["note"])
            text = self.human("--token-stdin")
            self.assertIn("the installed release would register +glaeda-runner-new against this copy", text)
            # the release's plan ran read-only, with the same arguments, and without re-checking itself
            calls = [json.loads(line) for line in seen.read_text().splitlines()]
            self.assertTrue(all("--apply" not in c["argv"] and c["guard"] == "1" for c in calls), calls)
            self.assertIn("--token-stdin", calls[0]["argv"])
            blocked = self.invoke("--apply", "--token-stdin", stdin=REG_TOKEN + "\n", expect=1)
            self.assertFalse([e for e in self.log() if e["tool"] == "config.sh"], "a stale copy registered")
            self.assertEqual(self.by_kind(blocked)["register"]["state"], "blocked")
            self.assertFalse(any(c["argv"].count("--apply") for c in
                                 (json.loads(line) for line in seen.read_text().splitlines())))
            forced = self.invoke("--apply", "--allow-stale")
            self.assertEqual(forced["preflight"]["checks"]["scriptCopy"]["level"], "warn")
            self.assertEqual(self.by_kind(forced)["verify"]["state"], "ok")

    def test_a_newer_or_identical_copy_is_not_stale(self) -> None:
        self.installed_release(["self-hosted"])
        newer = {"date": "2026-09-27", "source": "b" * 40, "tag": None, "via": "git"}
        with mock.patch.object(cr, "script_source", return_value=newer):
            plan = self.invoke()
        self.assertEqual(plan["scriptCopy"]["state"], "differs")
        self.assertEqual(plan["preflight"]["checks"]["scriptCopy"]["level"], "info")
        self.assertTrue(plan["ready"])
        # a copy of an older release is stale even from the same day
        other = {"date": "2026-09-26", "source": "c" * 40, "tag": "r-20260926-000000000000", "via": "release"}
        with mock.patch.object(cr, "script_source", return_value=other):
            self.assertEqual(self.invoke()["scriptCopy"]["state"], "stale")
        with mock.patch.object(cr, "script_source", return_value=None):  # unknown origin counts as older
            self.assertEqual(self.invoke()["scriptCopy"]["state"], "stale")

    def test_the_release_copy_itself_is_never_checked(self) -> None:
        shutil.rmtree(self.home / ".local", ignore_errors=True)
        self.installed_release([], same_files=True)
        self.assertNotIn("scriptCopy", self.invoke())

    def test_script_source_reads_a_stamp_a_release_or_git(self) -> None:
        base = Path(self.tmp.name)
        staged = base / "glaeda-runner/scripts"
        staged.mkdir(parents=True)
        (staged / cr.SOURCE_STAMP).write_text(json.dumps({"by": "fleet runner relabel", "date": "2026-09-26T10:00:00Z",
                                                          "source": "d" * 40}))
        self.assertEqual(cr.script_source(staged)["date"], "2026-09-26")
        gen = base / "generations/r-20260925-0123456789ab"
        (gen / "glaeda/scripts").mkdir(parents=True)
        (gen / "release.json").write_text(json.dumps({"tag": "r-20260925-0123456789ab", "source": "e" * 40}))
        self.assertEqual(cr.script_source(gen / "glaeda/scripts")["tag"], "r-20260925-0123456789ab")
        self.assertIsNone(cr.script_source(base / "nowhere"))

    def test_relabel_starts_a_stopped_agent_exactly_once(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--apply", "--labels", "ram48")
            with mock.patch.object(cr, "xcode_present", return_value=True):
                receipt = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                                      "--name", "mini-test-glaeda")
        self.assertEqual(self.by_kind(receipt)["register"]["state"], "updated")
        verbs = [e["argv"][0] for e in self.log() if e["tool"] == "launchctl"]
        self.assertEqual(verbs.count("bootstrap"), 2, verbs)  # install, then relabel; never a third

    def test_relabel_with_a_plist_change_restarts_on_the_new_plist(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--apply", "--labels", "ram48")
            plist = self.home / "Library/LaunchAgents/com.teamleaderleo.glaeda.cmux-runner.plist"
            receipt_path = self.home / ".local/state/glaeda/cmux-runner/receipt.json"
            doc = json.loads(receipt_path.read_text())
            data = plist.read_bytes().replace(b"</dict>\n</plist>", b"<key>X</key><string>old</string></dict>\n</plist>")
            plist.write_bytes(data)  # an older plist this install wrote
            for act in doc["actions"]:
                if act.get("kind") == "agent":
                    act["ownedSha256"] = cr.sha256_bytes(data)
            receipt_path.write_text(json.dumps(doc))
            with mock.patch.object(cr, "xcode_present", return_value=True):
                receipt = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                                      "--name", "mini-test-glaeda")
        self.assertEqual(self.by_kind(receipt)["agent"]["state"], "update")
        self.assertTrue(receipt["ready"], receipt["blocking"])
        verbs = [e["argv"][0] for e in self.log() if e["tool"] == "launchctl"
                 and e["argv"][0] not in {"print", "print-disabled", "enable"}]
        self.assertEqual(verbs[-3:], ["bootstrap", "bootout", "bootstrap"], verbs)

    def test_a_plist_change_never_boots_out_a_runner_with_a_job(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))  # not a sandbox home, so launchctl runs
        patch = mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw)
        patch.start()
        self.addCleanup(patch.stop)
        self.invoke("--apply", "--labels", "ram48")
        plist = self.home / "Library/LaunchAgents/com.teamleaderleo.glaeda.cmux-runner.plist"
        receipt_path = self.home / ".local/state/glaeda/cmux-runner/receipt.json"
        doc = json.loads(receipt_path.read_text())
        data = plist.read_bytes().replace(b"</dict>\n</plist>", b"<key>X</key><string>old</string></dict>\n</plist>")
        plist.write_bytes(data)  # an older plist this install wrote, loaded and running a job
        for act in doc["actions"]:
            if act.get("kind") == "agent":
                act["ownedSha256"] = cr.sha256_bytes(data)
        receipt_path.write_text(json.dumps(doc))
        make_executable(self.launchctl, FAKE_LOG_HEADER + "argv = sys.argv[1:]\nlog('launchctl', argv)\nsys.exit(0)\n")
        with mock.patch.object(cr, "runner_busy", return_value=True):
            receipt = self.invoke("--apply", "--labels", "ram48", expect=1)
        agent = self.by_kind(receipt)["agent"]
        self.assertEqual(agent["state"], "failed")
        self.assertIn("re-run when idle", agent["note"])
        self.assertEqual(plist.read_bytes(), data)
        self.assertNotIn("bootout", [e["argv"][0] for e in self.log() if e["tool"] == "launchctl"])

    def test_relabel_without_a_token_blocks_and_changes_nothing(self) -> None:
        self.invoke("--apply", "--labels", "ram48")
        before = self.tree()
        real_which = cr.which
        with mock.patch.object(cr, "which", lambda name, extra=(): None if name == "gh" else real_which(name, extra)), \
                mock.patch.object(cr, "xcode_present", return_value=False):
            blocked = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                                  "--name", "mini-test-glaeda", gh=False, expect=1)
        self.assertIn("--token-stdin", self.by_kind(blocked)["register"]["note"])
        after = self.tree()
        for tree in (before, after):
            tree.pop(".local/state/glaeda/cmux-runner/receipt.json", None)
        self.assertEqual(before, after)

    def test_interrupted_relabel_reregisters_its_own_name(self) -> None:
        self.invoke("--apply", "--labels", "ram48")
        (self.state / "fail-register").touch()
        real = cr.register

        def failing(ctx, act):  # config.sh fails after the local registration was cleared
            act["state"], act["note"] = "failed", "config.sh failed: network"
        with mock.patch.object(cr, "register", failing), mock.patch.object(cr, "xcode_present", return_value=False):
            self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                        "--name", "mini-test-glaeda", expect=1)
        runner = self.home / "actions-runner-glaeda"
        self.assertFalse((runner / ".runner").exists())
        with mock.patch.object(cr, "register", real), mock.patch.object(cr, "xcode_present", return_value=False):
            receipt = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                                  "--name", "mini-test-glaeda")
        self.assertTrue(receipt["ready"], receipt["blocking"])
        self.assertIn("--replace", self.config_argvs()[-1])
        runners = json.loads((self.state / "runners.json").read_text())["runners"]
        self.assertEqual(len(runners), 1)

    def test_config_remove_failure_falls_back_to_api_delete(self) -> None:
        self.invoke("--apply")
        (self.state / "fail-config-remove").touch()
        receipt = self.invoke("--uninstall", "--apply")
        dereg = next(a for a in receipt["actions"] if a["kind"] == "deregister")
        self.assertIn("deleted by id", dereg["note"])
        self.assertTrue(any(e["tool"] == "gh" and "DELETE" in e["argv"] for e in self.log()))
        self.assertFalse((self.home / "actions-runner-glaeda").exists())


    # ------------------------------------------------------------ --token-stdin, no gh on the mini

    def test_token_stdin_without_gh_installs_and_uninstalls(self) -> None:
        real_which = cr.which
        with mock.patch.object(cr, "which", lambda name, extra=(): None if name == "gh" else real_which(name, extra)):
            plan = self.invoke(gh=False)
            self.assertEqual(self.by_kind(plan)["register"]["state"], "blocked")
            preview = self.invoke("--token-stdin", gh=False, stdin=REG_TOKEN + "\n")
            self.assertTrue(preview["ready"], preview["blocking"])
            self.assertEqual(self.by_kind(preview)["register"]["state"], "create")
            self.assertEqual(self.tree(), {})
            for entry in self.log():  # plans only read the public release API
                self.assertEqual(entry["tool"], "curl", entry)
                self.assertNotIn("-o", entry["argv"], entry)
            receipt = self.invoke("--apply", "--token-stdin", gh=False, stdin=REG_TOKEN + "\n")
            kinds = self.by_kind(receipt)
            self.assertTrue(receipt["ready"], receipt["blocking"])
            self.assertEqual(kinds["download"]["version"], VERSION)
            self.assertEqual(kinds["verify"]["state"], "ok", kinds["verify"])
            self.assertEqual(kinds["verify"]["runner"]["id"], 4242)
            self.assertFalse(any(e["tool"] == "gh" for e in self.log()))
            config = [e for e in self.log() if e["tool"] == "config.sh"][0]
            self.assertEqual(config["envToken"], REG_TOKEN)
            for entry in self.log():
                self.assertNotIn(REG_TOKEN, " ".join(entry["argv"]))
            self.assertNotIn(REG_TOKEN, self.last_output)
            for name, data in self.tree().items():
                self.assertNotIn(REG_TOKEN.encode(), data or b"", name)

            again = self.invoke("--apply", gh=False)  # an installed mini re-applies without gh or a token
            self.assertTrue(again["ready"], again["blocking"])
            self.assertEqual({a["state"] for a in again["actions"] if a["kind"] != "verify"}, {"unchanged"})

            before = self.tree()
            stuck = self.invoke("--uninstall", "--apply", gh=False, expect=1)  # no way to deregister: touch nothing
            self.assertFalse(stuck["ready"])
            self.assertTrue(all(a["state"] == "blocked" for a in stuck["actions"]), stuck["actions"])
            after = self.tree()
            after.pop(".local/state/glaeda/cmux-runner/uninstall-receipt.json")
            self.assertEqual(before, after)

            removed = self.invoke("--uninstall", "--apply", "--token-stdin", gh=False, stdin=REMOVE_TOKEN + "\n")
            self.assertTrue(next(a for a in removed["actions"] if a["kind"] == "deregister")["applied"])
            self.assertFalse((self.home / "actions-runner-glaeda").exists())
            self.assertNotIn(REMOVE_TOKEN, self.last_output)

    def test_xcode_app_check_warns_and_never_blocks(self) -> None:
        missing = self.invoke("--xcode-app", os.fspath(self.home / "Xcode_26.6.app"))
        self.assertEqual(missing["preflight"]["checks"]["xcodeApp"]["level"], "warn")
        self.assertNotIn("xcodeApp", missing["blocking"])
        real = self.home / "Real.app"
        real.mkdir()
        (self.home / "Link.app").symlink_to(real)
        linked = self.invoke("--xcode-app", os.fspath(self.home / "Link.app"))
        self.assertIn("symlink", linked["preflight"]["checks"]["xcodeApp"]["value"])
        self.assertNotIn("xcodeApp", self.invoke()["preflight"]["checks"])

    # ------------------------------------------------------------ which runner release a new install unpacks

    def release_reads(self, since: int = 0) -> list[dict]:
        """API reads of actions/runner releases (gh, or curl without -o) since log entry `since`."""
        return [e for e in self.log()[since:] if (e["tool"] == "gh" and any("actions/runner/releases" in a for a in e["argv"]))
                or (e["tool"] == "curl" and "-o" not in e["argv"])]

    def downloads(self, since: int = 0) -> list[dict]:
        return [e for e in self.log()[since:] if e["tool"] == "curl" and "-o" in e["argv"]]

    def download_act(self, *args: str, **kw) -> dict:
        return self.by_kind(self.invoke(*args, **kw))["download"]

    def test_release_resolution_reuses_installed_then_cache_then_operator_then_api_then_pin(self) -> None:
        sha = hashlib.sha256((self.state / "runner.tar.gz").read_bytes()).hexdigest()
        # nothing on this mini and no operator answer: one API read of "latest"
        fresh = self.download_act()
        self.assertEqual((fresh["version"], fresh["resolvedBy"]), (VERSION, "api-latest"))
        self.assertEqual(len(self.release_reads()), 1)
        # the operator's answer (the fleet resolves it once per apply) beats the API
        calls = len(self.log())
        told = self.download_act("--latest-runner-version", "2.401.0", "--latest-runner-sha256", "a" * 64)
        self.assertEqual((told["version"], told["resolvedBy"], told["sha256"]), ("2.401.0", "operator", "a" * 64))
        self.assertIn("/v2.401.0/actions-runner-osx-arm64-2.401.0.tar.gz", told["url"])
        self.assertEqual(self.release_reads(calls), [])

        # instance 0 installs: one download, into the cache, and the receipt records the SHA-256
        self.invoke("--apply")
        self.assertEqual(len(self.downloads()), 1)
        cache = self.home / cr.RUNNER_CACHE / VERSION
        marker = json.loads((cache / cr.RUNNER_CACHE_MARKER).read_text())
        self.assertEqual((marker["version"], marker["sha256"]), (VERSION, sha))
        self.assertTrue((cache / f"actions-runner-osx-arm64-{VERSION}.tar.gz").is_file())
        receipt = json.loads((self.home / ".local/state/glaeda/cmux-runner/receipt.json").read_text())
        self.assertEqual((receipt["runnerVersion"], receipt["runnerSha256"]), (VERSION, sha))

        # instance 1 reuses what instance 0 runs, from the cache, even when the operator names a newer one:
        # no API read and no second download
        calls = len(self.log())
        reused = self.download_act("--instance", "1", "--latest-runner-version", "2.401.0",
                                   "--latest-runner-sha256", "a" * 64)
        self.assertEqual((reused["version"], reused["resolvedBy"], reused.get("reused")), (VERSION, "cache", "installed"))
        done = self.invoke("--apply", "--instance", "1")
        self.assertEqual(self.by_kind(done)["download"]["tarball"], "cache")
        self.assertEqual(self.release_reads(calls), [])
        self.assertEqual(self.downloads(calls), [])
        self.assertTrue((self.home / "actions-runner-glaeda-1" / "config.sh").is_file())

        # a runner that self-updated to a version with no local SHA-256: the newest cached tarball, still no API
        bin_dir = self.home / "actions-runner-glaeda" / "bin.2.400.0"
        bin_dir.mkdir()
        (self.home / "actions-runner-glaeda" / "bin").symlink_to(bin_dir)
        (self.home / "actions-runner-glaeda-1").rename(self.home / "moved-away")  # only instance 0 counts now
        calls = len(self.log())
        newer = self.download_act("--instance", "2")
        self.assertEqual((newer["version"], newer["resolvedBy"]), (VERSION, "cache"))
        # ... unless the operator's answer is that very version: then it is reused with the operator's SHA-256
        same = self.download_act("--instance", "2", "--latest-runner-version", "2.400.0",
                                 "--latest-runner-sha256", "b" * 64)
        self.assertEqual((same["version"], same["resolvedBy"], same.get("reused")), ("2.400.0", "operator", "installed"))
        self.assertEqual(self.release_reads(calls), [])

        # a folder others can write to vouches for nothing
        ctx = cr.Context(cr.parser().parse_args([]))
        self.assertIsNotNone(cr.cached_release(ctx, VERSION))
        cache.chmod(0o777)
        self.assertIsNone(cr.cached_release(ctx, VERSION))
        cache.chmod(0o755)
        # a cached tarball that no longer hashes to its marker is a miss, never trusted
        (cache / f"actions-runner-osx-arm64-{VERSION}.tar.gz").write_bytes(b"truncated")
        self.assertIsNone(cr.cached_release(cr.Context(cr.parser().parse_args([])), VERSION))

    def test_release_falls_back_to_the_pin_when_the_api_fails(self) -> None:
        (self.state / "fail-release").touch()
        act = self.download_act()
        self.assertEqual((act["state"], act["version"], act["resolvedBy"]),
                         ("create", cr.PINNED_RUNNER_VERSION, "pinned"))
        self.assertEqual(act["sha256"], cr.PINNED_RUNNER_SHA256)
        self.assertIn("pinned version", act["note"])
        self.assertEqual(len(self.release_reads()), 1, "the API is read once, then the pin")
        # an explicit --runner-version with its SHA-256 needs no API at all
        calls = len(self.log())
        pinned = self.download_act("--runner-version", "v2.330.0", "--runner-sha256", "c" * 64)
        self.assertEqual((pinned["version"], pinned["resolvedBy"], pinned["sha256"]), ("2.330.0", "flag", "c" * 64))
        self.assertEqual(self.release_reads(calls), [])

    def test_release_read_without_gh_sends_a_token_on_curl_stdin_only(self) -> None:
        secret = "ghp_" + "Z" * 36
        real_which = cr.which
        with mock.patch.object(cr, "which", lambda name, extra=(): None if name == "gh" else real_which(name, extra)), \
                mock.patch.dict(os.environ, {"GH_TOKEN": secret}):
            act = self.download_act(gh=False)
        self.assertEqual((act["version"], act["resolvedBy"]), (VERSION, "api-latest"))
        reads = self.release_reads()
        self.assertEqual(len(reads), 1)
        self.assertIn("--config", reads[0]["argv"])
        self.assertNotIn(secret, json.dumps(self.log()))
        self.assertEqual((self.state / "curl-config").read_text(), f'header = "Authorization: Bearer {secret}"\n')
        self.assertNotIn(secret, self.last_output)

    def test_release_flags_are_validated(self) -> None:
        for args in (("--latest-runner-version", "2.1.0"), ("--latest-runner-sha256", "a" * 64),
                     ("--latest-runner-version", "2.1.0; rm -rf /", "--latest-runner-sha256", "a" * 64),
                     ("--runner-version", "latest"), ("--runner-sha256", "nothex")):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cr.main(["--gh", os.fspath(self.gh), *args]), 2)
        self.assertEqual(self.tree(), {})

    def test_runner_cache_keeps_the_newest_versions(self) -> None:
        ctx = cr.Context(cr.parser().parse_args([]))
        for version in ("2.300.0", "2.310.0", "2.320.0"):
            folder = ctx.runner_cache / version
            folder.mkdir(parents=True)
            (folder / cr.RUNNER_CACHE_MARKER).write_text("{}")
            (folder / cr.runner_asset(version)).write_bytes(b"x")
        (ctx.runner_cache / "2.300.0" / "someone-elses").write_text("keep")
        cr.prune_runner_cache(ctx, "2.399.0")
        self.assertEqual(sorted(p.name for p in ctx.runner_cache.iterdir()), ["2.300.0", "2.320.0"])
        self.assertEqual(sorted(p.name for p in (ctx.runner_cache / "2.300.0").iterdir()), ["someone-elses"],
                         "only the cache's own files are removed")

    def test_token_stdin_apply_needs_a_token(self) -> None:
        for args, stdin in ((("--apply", "--token-stdin"), ""),
                            (("--apply", "--token-stdin"), "not a token; rm -rf /\n")):
            with self.subTest(args=args, stdin=stdin[:8]), contextlib.redirect_stderr(io.StringIO()) as err, \
                    mock.patch.object(sys, "stdin", io.StringIO(stdin)):
                self.assertEqual(cr.main(["--gh", os.fspath(self.gh), *args]), 2)
                self.assertNotIn("rm -rf", err.getvalue())
        self.assertEqual(self.tree(), {})
        self.assertEqual(self.log(), [])


MANIFEST = {
    "defaults": {"xcode": {"apps": [{"path": "/Applications/Xcode_26.6.app", "version": "26.6", "build": "17F113"}]}},
    "hosts": {
        "mini-std": {"class": "std", "availability": "dedicated", "roles": ["dev-builds", "ci-runner"],
                     "hardware": "m4pro-48"},
        "mini-light": {"class": "light", "availability": "opportunistic", "roles": ["ci-runner"], "owner": "x",
                       "hardware": "m4-16"},
        "no-hardware": {"class": "std", "availability": "dedicated", "roles": ["ci-runner"]},
        "mini-no-role": {"class": "std", "availability": "dedicated", "roles": ["dev-builds"]},
        "laptop": {"class": "dev", "availability": "opportunistic", "roles": ["ci-runner"]},
        "borrowed": {"class": "borrowed", "availability": "opportunistic", "roles": ["ci-runner"]},
        "old-shape": {"class": "m4pro-48", "roles": ["ci-runner"]},
        "bad-avail": {"class": "std", "availability": "sometimes", "roles": ["ci-runner"]},
        "override": {"class": "std", "availability": "dedicated", "roles": ["ci-runner"], "hardware": "m4pro-48",
                     "overrides": {"xcode": {"apps": [{"path": "/Applications/Xcode.app", "version": "26.3",
                                                       "build": "17C529"}]}}},
    },
}


NIGHTLY = {"name": "nightly-next", "repo": "manaflow-ai/cmux",
           "labels": ["glaeda-trusted-next-nightly", "glaeda-runner-mini-std-nightly-next"],
           "admit": ["/usr/bin/python3", "~/nightly-builder/nightly_builder.py", "admit"],
           "watch": ["/usr/bin/python3", "~/nightly-builder/nightly_builder.py", "watch", "--loop"],
           "env": {"NIGHTLY_BUILDER_RUNNER_DIR": "{runnerDir}", "PATH": "~/.local/bin"}}


def dedicated_manifest(*entries: dict) -> dict:
    manifest = copy.deepcopy(MANIFEST)
    manifest["hosts"]["mini-std"]["overrides"] = {"runner": {"dedicated": [copy.deepcopy(e) for e in entries]}}
    return manifest


class DedicatedRunnerTest(unittest.TestCase):
    """Leo, 2026-10-27: "why the fuck not manage it with glaeda????????" A builder's runner (the nightly-next app
    build, the iOS internal archive) is a manifest runner.dedicated entry that glaeda-cmux-runner --dedicated installs:
    glaeda's release, .path, hooks, gate and disk floor, with exactly its own labels, scope and admission."""

    setUp, tearDown, build_release = RunnerTest.setUp, RunnerTest.tearDown, RunnerTest.build_release
    invoke, log, by_kind, config_argvs = RunnerTest.invoke, RunnerTest.log, RunnerTest.by_kind, RunnerTest.config_argvs

    def manifest(self, *entries: dict) -> str:
        path = self.state / "mini-fleet.json"
        path.write_text(json.dumps(dedicated_manifest(*(entries or (NIGHTLY,)))))
        return os.fspath(path)

    def test_the_manifest_entry_is_validated(self) -> None:
        good, why = fleet_labels.dedicated_runners(dedicated_manifest(NIGHTLY), "mini-std")
        self.assertIsNone(why)
        self.assertEqual(good[0]["labels"], NIGHTLY["labels"])
        self.assertEqual(fleet_labels.dedicated_runners(MANIFEST, "mini-std"), ([], None))
        for change, needle in (({"labels": ["self-hosted", "x"]}, "leave out"), ({"labels": []}, "non-empty"),
                               ({"org": "manaflow-ai"}, "exactly one of repo"), ({"admit": ["relative.py"]}, "admit"),
                               ({"name": "Bad Name"}, "name"), ({"name": "123"}, "name"),
                               ({"name": "x-glaeda"}, "pool instance"), ({"name": "x-glaeda-2"}, "pool instance"),
                               ({"env": {"lower": "x"}}, "env"),
                               ({"surprise": 1}, "unknown keys"), ({"group": "g"}, "group goes with org"),
                               ({"labels": ["x", "glaeda-std-xcode-26.6".upper()]}, "pool labels"),
                               ({"labels": ["glaeda-std-xcode-26.6"]}, "pool labels")):
            with self.subTest(change):
                _, why = fleet_labels.dedicated_runners(dedicated_manifest({**NIGHTLY, **change}), "mini-std")
                self.assertIn(needle, why or "")
        _, why = fleet_labels.dedicated_runners(dedicated_manifest(NIGHTLY, NIGHTLY), "mini-std")
        self.assertIn("declared twice", why)
        headless = dedicated_manifest(NIGHTLY)
        headless["hosts"]["mini-std"]["gui"] = False
        _, why = fleet_labels.dedicated_runners(headless, "mini-std")
        self.assertIn("gui: false", why or "")

    def test_install_registers_exactly_its_labels_with_its_own_admission_and_watch(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            receipt = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                                  "--dedicated", "nightly-next")
        self.assertTrue(receipt["ready"], receipt["blocking"])
        runner = self.home / "actions-runner-glaeda-nightly-next"
        self.assertEqual(receipt["runnerDir"], os.fspath(runner))
        [argv] = self.config_argvs()
        self.assertIn("--no-default-labels", argv)
        self.assertEqual(argv[argv.index("--labels") + 1], ",".join(NIGHTLY["labels"]))
        self.assertEqual(argv[argv.index("--name") + 1], "mini-std-glaeda-nightly-next")
        self.assertEqual(argv[argv.index("--url") + 1], "https://github.com/manaflow-ai/cmux")
        registered = json.loads((self.state / "runners.json").read_text())["runners"][0]
        self.assertEqual([l["name"] for l in registered["labels"]], NIGHTLY["labels"], "no self-hosted or pool label")
        hooks = runner / cr.HOOK_DIR
        started = (hooks / "job-started.sh").read_text()
        self.assertIn(f"--dedicated-admit {hooks / cr.DEDICATED_ADMIT}", started)
        for absent in ("--capacity-units", "--require-eligible", "--test-keychain", "--trusted-ref"):
            self.assertNotIn(absent, started)
        self.assertIn(f"exec /usr/bin/python3 {self.home}/nightly-builder/nightly_builder.py admit",
                      (hooks / cr.DEDICATED_ADMIT).read_text())
        self.assertEqual(hook.runner_scope(runner).dedicated, True)
        self.assertFalse(hook.runner_scope(runner).root)
        label = "com.teamleaderleo.glaeda.cmux-runner.dedicated.nightly-next"
        agent = plistlib.loads((self.home / "Library/LaunchAgents" / f"{label}.plist").read_bytes())
        self.assertEqual(agent["Label"], label)
        watch = plistlib.loads((self.home / "Library/LaunchAgents" / f"{label}.watch.plist").read_bytes())
        self.assertEqual(watch["ProgramArguments"][1], f"{self.home}/nightly-builder/nightly_builder.py")
        self.assertEqual(watch["EnvironmentVariables"]["NIGHTLY_BUILDER_RUNNER_DIR"], os.fspath(runner))
        watch_path = watch["EnvironmentVariables"]["PATH"].split(":")
        self.assertIn(f"{self.home}/.local/bin", watch_path)
        self.assertIn("/usr/bin", watch_path, "a declared PATH joins the workload PATH, never replaces it")
        self.assertIn(f"NIGHTLY_BUILDER_RUNNER_DIR={runner}", (runner / ".env").read_text().splitlines())
        path = (runner / ".path").read_text().strip().split(":")
        self.assertEqual(path[:3], ["/opt/homebrew/bin", "/usr/local/bin", f"{self.home}/.local/bin"])
        self.assertIn(f"{self.home}/.cargo/bin", path)
        bootstraps = [e["argv"][2] for e in self.log() if e["tool"] == "launchctl" and e["argv"][0] == "bootstrap"]
        self.assertEqual(sorted(Path(b).name for b in bootstraps), [f"{label}.plist", f"{label}.watch.plist"])
        state = self.home / ".local/state/glaeda/cmux-runner/instance-dedicated-nightly-next/receipt.json"
        self.assertEqual(json.loads(state.read_text())["dedicated"]["name"], "nightly-next")
        self.assertEqual(receipt["routing"]["route"], [])
        # a re-run changes nothing and registers nothing new
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            again = self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                                "--dedicated", "nightly-next")
        self.assertEqual(self.by_kind(again)["register"]["state"], "unchanged")
        self.assertEqual(len(self.config_argvs()), 1)
        # the manifest drops the watch loop and an env key: the agent this install wrote and the key both go
        trimmed = {k: v for k, v in NIGHTLY.items() if k != "watch"}
        trimmed["env"] = {"PATH": "~/.local/bin"}
        (self.state / "loaded.json").write_text(json.dumps([label, f"{label}.watch"]))  # both agents run
        env_before = (runner / ".env").read_text()
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw), \
                mock.patch.object(cr, "runner_busy", return_value=True):
            busy = self.invoke("--apply", "--manifest", self.manifest(trimmed), "--member", "mini-std",
                               "--dedicated", "nightly-next", expect=1)
        self.assertEqual(self.by_kind(busy)["register"]["state"], "failed", "never restarted under a job")
        self.assertEqual((runner / ".env").read_text(), env_before)
        self.assertIn(label, json.loads((self.state / "loaded.json").read_text()))
        before = [e for e in self.log() if e["tool"] == "launchctl"]
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--apply", "--manifest", self.manifest(trimmed), "--member", "mini-std",
                        "--dedicated", "nightly-next")
        self.assertFalse((self.home / "Library/LaunchAgents" / f"{label}.watch.plist").exists())
        self.assertNotIn("NIGHTLY_BUILDER_RUNNER_DIR", (runner / ".env").read_text())
        # the env changed under a loaded, idle listener: it is restarted so its jobs see the new env
        tail = [e["argv"] for e in self.log() if e["tool"] == "launchctl"][len(before):]
        self.assertIn(["bootout", f"gui/{os.getuid()}/{label}"], tail)
        self.assertTrue(any(a[0] == "bootstrap" and a[2].endswith(f"{label}.plist") for a in tail), tail)
        self.assertIn(["bootout", f"gui/{os.getuid()}/{label}.watch"],
                      [e["argv"] for e in self.log() if e["tool"] == "launchctl"])
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std",
                        "--dedicated", "nightly-next")
        self.assertTrue((self.home / "Library/LaunchAgents" / f"{label}.watch.plist").exists(), "and comes back")
        # and an uninstall deregisters it and removes both agents
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--uninstall", "--apply", "--manifest", self.manifest(), "--member", "mini-std",
                        "--dedicated", "nightly-next")
        self.assertFalse((self.home / "Library/LaunchAgents" / f"{label}.watch.plist").exists())
        self.assertFalse((self.home / "Library/LaunchAgents" / f"{label}.plist").exists())
        self.assertEqual(json.loads((self.state / "runners.json").read_text())["runners"], [])

    def test_a_dropped_path_entry_leaves_path_and_a_stuck_watch_is_retried(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        runner = self.home / "actions-runner-glaeda-nightly-next"
        label = "com.teamleaderleo.glaeda.cmux-runner.dedicated.nightly-next"
        builder = {**NIGHTLY, "env": {"PATH": "/opt/builder-v1/bin:~/.local/bin"}}
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--apply", "--manifest", self.manifest(builder), "--member", "mini-std",
                        "--dedicated", "nightly-next")
        path = (runner / ".path").read_text().strip().split(":")
        self.assertIn("/opt/builder-v1/bin", path)
        (runner / ".path").write_text(":".join([*path, "/opt/someone-else/bin"]) + "\n")  # not ours: it stays
        # PATH goes from the manifest: the entries it added go, every other entry stays
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--apply", "--manifest", self.manifest({**NIGHTLY, "env": {}}), "--member", "mini-std",
                        "--dedicated", "nightly-next")
        path = (runner / ".path").read_text().strip().split(":")
        self.assertNotIn("/opt/builder-v1/bin", path)
        self.assertNotIn(f"{self.home}/.local/bin", path)
        self.assertIn("/opt/someone-else/bin", path)
        self.assertEqual(path[:2], ["/opt/homebrew/bin", "/usr/local/bin"])
        # a watch update launchd will not unload writes nothing, so the next apply sees the change again
        watch_plist = self.home / "Library/LaunchAgents" / f"{label}.watch.plist"
        old_watch = watch_plist.read_bytes()
        changed = {**NIGHTLY, "watch": [*NIGHTLY["watch"], "--verbose"], "env": {}}
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw), \
                mock.patch.object(cr, "bootout_watch", return_value=False):
            failed = self.invoke("--apply", "--manifest", self.manifest(changed), "--member", "mini-std",
                                 "--dedicated", "nightly-next", expect=1)
        self.assertEqual(next(a for a in failed["actions"] if a["kind"] == "watch")["state"], "failed")
        self.assertEqual(watch_plist.read_bytes(), old_watch, "the old plist stays while the old agent runs")
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            again = self.invoke("--apply", "--manifest", self.manifest(changed), "--member", "mini-std",
                                "--dedicated", "nightly-next")
        self.assertEqual(next(a for a in again["actions"] if a["kind"] == "watch")["state"], "update")
        self.assertIn("--verbose", plistlib.loads(watch_plist.read_bytes())["ProgramArguments"])
        # launchd keeps the watch agent: its plist and ownership stay, and the next apply removes it
        trimmed = {k: v for k, v in NIGHTLY.items() if k != "watch"}
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw), \
                mock.patch.object(cr, "bootout_watch", return_value=False) as stuck:
            failed = self.invoke("--apply", "--manifest", self.manifest(trimmed), "--member", "mini-std",
                                 "--dedicated", "nightly-next", expect=1)
        stuck.assert_called_once()
        watch = next(a for a in failed["actions"] if a["kind"] == "watch")
        self.assertEqual(watch["state"], "failed")
        self.assertTrue((self.home / "Library/LaunchAgents" / f"{label}.watch.plist").exists())
        # bootout_watch itself: True only once launchd says it no longer has the agent
        (self.state / "loaded.json").write_text(json.dumps([f"{label}.watch"]))
        ctx = mock.Mock(uid=os.getuid())
        self.assertTrue(cr.bootout_watch(ctx, f"{label}.watch"))
        make_executable(self.launchctl, FAKE_LAUNCHCTL.replace('if argv[0] == "bootout":',
                                                               'if argv[0] == "bootout" and False:'))
        (self.state / "loaded.json").write_text(json.dumps([f"{label}.watch"]))
        self.assertFalse(cr.bootout_watch(ctx, f"{label}.watch", wait_s=0.3))
        make_executable(self.launchctl, FAKE_LAUNCHCTL)
        (self.state / "loaded.json").unlink()
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            again = self.invoke("--apply", "--manifest", self.manifest(trimmed), "--member", "mini-std",
                                "--dedicated", "nightly-next")
        self.assertEqual(next(a for a in again["actions"] if a["kind"] == "watch")["state"], "remove")
        self.assertFalse((self.home / "Library/LaunchAgents" / f"{label}.watch.plist").exists())

    def test_an_uninstall_stops_when_the_watch_agent_does_not_unload(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        runner = self.home / "actions-runner-glaeda-nightly-next"
        label = "com.teamleaderleo.glaeda.cmux-runner.dedicated.nightly-next"
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std", "--dedicated", "nightly-next")
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw), \
                mock.patch.object(cr, "bootout_watch", return_value=False):
            stuck = self.invoke("--uninstall", "--apply", "--manifest", self.manifest(), "--member", "mini-std",
                                "--dedicated", "nightly-next", expect=1)
        self.assertTrue(all(a["state"] != "remove" or not a.get("applied") for a in stuck["actions"]))
        self.assertTrue((self.home / "Library/LaunchAgents" / f"{label}.watch.plist").exists())
        self.assertTrue((self.home / "Library/LaunchAgents" / f"{label}.plist").exists(), "the runner stays too")
        self.assertTrue((runner / ".runner").is_file(), "still registered")
        self.assertEqual(len(json.loads((self.state / "runners.json").read_text())["runners"]), 1)
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--uninstall", "--apply", "--manifest", self.manifest(), "--member", "mini-std",
                        "--dedicated", "nightly-next")
        self.assertFalse((self.home / "Library/LaunchAgents" / f"{label}.watch.plist").exists())
        self.assertEqual(json.loads((self.state / "runners.json").read_text())["runners"], [])

    def test_keep_paths_are_written_for_glaeda_disk_and_validated(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        runner = self.home / "actions-runner-glaeda-nightly-next"
        kept = {**NIGHTLY, "keep": ["{runnerDir}/_work/cmux/cmux", "~/Library/Caches/cmux-nightly-next/x"]}
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--apply", "--manifest", self.manifest(kept), "--member", "mini-std", "--dedicated", "nightly-next")
        self.assertEqual(json.loads((runner / cr.ROLE_KEEP_MARKER).read_text()),
                         [f"{runner}/_work/cmux/cmux", f"{self.home}/Library/Caches/cmux-nightly-next/x"])
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std", "--dedicated", "nightly-next")
        self.assertFalse((runner / cr.ROLE_KEEP_MARKER).exists(), "dropped from the manifest: kept no longer")
        for bad in (["/abs"], ["~/../x"], ["relative"], "~/x", [""]):
            with self.subTest(bad=bad):
                _, why = fleet_labels.dedicated_runners(dedicated_manifest({**NIGHTLY, "keep": bad}), "mini-std")
                self.assertIn("keep", why or "")

    def test_a_relabel_starts_the_runner_only_under_its_new_admission(self) -> None:
        fake_pw = mock.Mock(pw_dir=os.fspath(self.home))
        wrapper = self.home / "actions-runner-glaeda-nightly-next" / cr.HOOK_DIR / cr.DEDICATED_ADMIT
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw):
            self.invoke("--apply", "--manifest", self.manifest(), "--member", "mini-std", "--dedicated", "nightly-next")
        changed = {**NIGHTLY, "labels": [*NIGHTLY["labels"], "glaeda-extra"],
                   "admit": ["/usr/bin/python3", "~/nightly-builder/nightly_builder.py", "admit", "--strict"]}
        old_wrapper = wrapper.read_text()
        (self.state / "loaded.json").write_text(json.dumps(["com.teamleaderleo.glaeda.cmux-runner.dedicated.nightly-next"]))
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw), \
                mock.patch.object(cr, "runner_busy", return_value=True):
            busy = self.invoke("--apply", "--manifest", self.manifest(changed), "--member", "mini-std",
                               "--dedicated", "nightly-next", expect=1)
        self.assertIn(self.by_kind(busy)["register"]["state"], {"failed", "blocked"})
        self.assertEqual(wrapper.read_text(), old_wrapper, "a runner under a job keeps its old labels and admission")
        # a job that starts between the plan and the stop: relabel fails and writes no hook either
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw), \
                mock.patch.object(cr, "stop_if_idle", return_value=False):
            raced = self.invoke("--apply", "--manifest", self.manifest(changed), "--member", "mini-std",
                                "--dedicated", "nightly-next", expect=1)
        self.assertEqual(self.by_kind(raced)["register"]["state"], "failed")
        self.assertEqual(wrapper.read_text(), old_wrapper, "the stop failed: the old admission stays")
        (self.state / "loaded.json").unlink()
        seen = []
        real_start = cr.start_agent

        def start(ctx, act, path):
            seen.append(wrapper.read_text())
            return real_start(ctx, act, path)
        with mock.patch.object(cr.pwd, "getpwuid", return_value=fake_pw), mock.patch.object(cr, "start_agent", start):
            receipt = self.invoke("--apply", "--manifest", self.manifest(changed), "--member", "mini-std",
                                  "--dedicated", "nightly-next")
        self.assertEqual(self.by_kind(receipt)["register"]["state"], "updated")
        self.assertTrue(seen, "the relabel started the runner again")
        self.assertTrue(all("--strict" in text for text in seen), "never started under the old admission")

    def test_unknown_names_and_mixed_flags_are_refused(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(cr.main(["--manifest", self.manifest(), "--member", "mini-std", "--dedicated", "other"]), 2)
            self.assertEqual(cr.main(["--dedicated", "nightly-next"]), 2)
            self.assertEqual(cr.main(["--manifest", self.manifest(), "--member", "mini-std", "--dedicated",
                                      "nightly-next", "--instance", "1"]), 2)
        self.assertIn("declares no runner.dedicated named other", out.getvalue())


class DedicatedHookTest(unittest.TestCase):
    run_hook = HookTest.run_hook

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_job_started_admits_only_what_the_builders_admission_admits(self) -> None:
        path = event(self.dir, "push", {"repository": CMUX, "ref": "refs/heads/nightly-next"})
        for code, admitted in ((0, True), (1, False)):
            with self.subTest(code=code):
                admit = make_executable(self.dir / f"admit-{code}.sh", f"#!/bin/sh\nexit {code}\n")
                result = self.run_hook("job-started", "push", path, "--allowed-repo", "manaflow-ai/cmux", "--no-disk",
                                       "--dedicated-admit", os.fspath(admit), repo="manaflow-ai/cmux")
                self.assertEqual(result.returncode == 0, admitted, result.stdout + result.stderr)
                self.assertIn("dedicated runner" if admitted else "refused the job (exit 1)", result.stdout)
        # glaeda's own checks still run first: a fork PR never reaches the builder's admission
        fork = event(self.dir, "fork", SAMPLE_EVENTS["fork-pr"][1])
        marker = self.dir / "ran"
        admit = make_executable(self.dir / "admit-mark.sh", f"#!/bin/sh\ntouch {marker}\n")
        result = self.run_hook("job-started", "pull_request", fork, "--allowed-repo", "manaflow-ai/cmux", "--no-disk",
                               "--dedicated-admit", os.fspath(admit), repo="manaflow-ai/cmux")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(marker.exists())

    def test_the_gate_never_holds_a_dedicated_runner_for_the_pool(self) -> None:
        runner = self.dir / "actions-runner-glaeda-nightly-next"
        (runner / cr.HOOK_DIR).mkdir(parents=True)
        (runner / cr.HOOK_DIR / "job-started.sh").write_text(
            "#!/bin/bash\nexec python3 hook job-started --allowed-repo manaflow-ai/cmux --dedicated-admit /x/a.sh\n")
        gate = hook.Gate(runner, os.fspath(self.dir / "host.lock"), os.fspath(self.dir / "reservation.json"),
                         self.dir / "state", waiters=False)
        with mock.patch.object(hook, "host_held", return_value="a fleet build holds the host"), \
                mock.patch.object(hook, "reservation_refusal", return_value="reserved"), \
                mock.patch.object(hook.Gate, "low_disk", return_value=None):
            self.assertIsNone(gate.claimed())
            self.assertIsNone(gate.confirmed())
            self.assertIsNone(gate.still_reserved())
        with mock.patch.object(hook.Gate, "low_disk", return_value="disk: 10 GiB free"):
            self.assertEqual(gate.claimed(), "disk: 10 GiB free")


class ManifestLabelsTest(unittest.TestCase):
    def test_side_labels_widen_to_narrower_classes_only(self) -> None:
        # One rule from the manifest's class order: a side runner carries its own class's side label and every
        # narrower class's, so GitHub hands a starved light queue to idle std side runners; a light runner never
        # carries a std label, and root labels never widen.
        manifest = copy.deepcopy(MANIFEST)
        manifest["hosts"]["mini-light"]["availability"] = "dedicated"
        with mock.patch.object(cr, "xcode_present", return_value=True):
            std, _ = cr.member_labels(manifest, "mini-std")
            light, _ = cr.member_labels(manifest, "mini-light")
        self.assertEqual(light["sidePools"], ["glaeda-side-light-xcode-26.6"])
        self.assertEqual(light["rootPools"], ["glaeda-root-light-xcode-26.6"])
        self.assertFalse([label for label in light["labels"] + light["sidePools"] if "-std-" in label])
        self.assertEqual(std["sidePools"], ["glaeda-side-std-xcode-26.6", "glaeda-side-light-xcode-26.6"])
        self.assertEqual(std["rootPools"], ["glaeda-root-std-xcode-26.6"])
        self.assertEqual(std["pools"], ["glaeda-std-xcode-26.6"], "the pool count stays the member's own class")
        self.assertEqual(fleet_labels.side_classes("xl"), ("xl", "std", "light"))
        self.assertEqual(fleet_labels.side_classes("dev"), ())
        # A runner registered before the widening converges to what member_labels issues now.
        self.assertEqual(fleet_labels.widened_labels(["glaeda-std-xcode-26.6", "glaeda-side-std-xcode-26.6"]),
                         ["glaeda-std-xcode-26.6", "glaeda-side-std-xcode-26.6", "glaeda-side-light-xcode-26.6"])
        self.assertEqual(fleet_labels.widened_labels(std["labels"] + std["sidePools"]), std["labels"] + std["sidePools"])
        once = fleet_labels.widened_labels(["glaeda-aws-side-std-xcode-26.6", "glaeda-side-trusted-std-xcode-26.6"])
        self.assertEqual(once, ["glaeda-aws-side-std-xcode-26.6", "glaeda-side-trusted-std-xcode-26.6",
                                "glaeda-aws-side-light-xcode-26.6", "glaeda-side-trusted-light-xcode-26.6"])
        self.assertEqual(fleet_labels.widened_labels(once), once)
        for unchanged in (["glaeda-side-light-xcode-26.6"], ["glaeda-root-std-xcode-26.6"], ["glaeda-gui-std-xcode-26.6"],
                          ["glaeda-session-std-xcode-26.6"], ["glaeda-std-xcode-26.6"], ["self-hosted", "macOS"],
                          ["glaeda-side-std"], ["x-side-std-xcode-26.6"]):
            self.assertEqual(fleet_labels.widened_labels(unchanged), unchanged)

    def test_runner_capacity_per_class(self) -> None:
        with mock.patch.object(cr, "xcode_present", return_value=True):
            std, _ = cr.member_labels(MANIFEST, "mini-std")
            light, _ = cr.member_labels(MANIFEST, "mini-light")
            self.assertEqual((std["runners"], std["capacityUnits"]), (4, 14), "units are cores: an M4 Pro's 14")
            self.assertEqual((light["runners"], light["capacityUnits"]), (2, 10), "an M4's 10")
            self.assertEqual(fleet_labels.COMPILE_CORES,
                             hook.CLASS_COST["compile"][0], "compileSlots is bounded by the hook's compile cost")
            manifest = json.loads(json.dumps(MANIFEST))
            manifest["defaults"]["runner"] = {"classes": {"std": {"runners": 3, "capacityUnits": 15}}}
            manifest["hosts"]["override"]["overrides"]["runner"] = {"classes": {"std": {"runners": 1}}}
            self.assertEqual(cr.member_labels(manifest, "mini-std")[0]["runners"], 3)
            self.assertEqual(cr.member_labels(manifest, "override")[0]["runners"], 1)
            self.assertEqual(cr.member_labels(manifest, "override")[0]["capacityUnits"], 15)
            self.assertEqual(cr.member_labels(manifest, "mini-std")[0]["compileSlots"], 1)
            self.assertEqual(cr.member_labels(manifest, "mini-std")[0]["canonicalRoots"], 1)
            self.assertEqual(cr.member_labels(manifest, "mini-std")[0]["rootPools"], ["glaeda-root-std-xcode-26.6"])
            # A std side runner also carries the light side label: light jobs spill onto it, never the reverse.
            self.assertEqual(cr.member_labels(manifest, "mini-std")[0]["sidePools"],
                             ["glaeda-side-std-xcode-26.6", "glaeda-side-light-xcode-26.6"])
            self.assertEqual(cr.member_labels(manifest, "mini-std")[0]["rootPools"], ["glaeda-root-std-xcode-26.6"])
            manifest["defaults"]["runner"]["classes"]["std"]["compileSlots"] = 2
            self.assertIn("compileSlots 2 needs canonicalRoots 2", cr.member_labels(manifest, "mini-std")[1])
            manifest["defaults"]["runner"]["classes"]["std"]["canonicalRoots"] = 2
            self.assertEqual(cr.member_labels(manifest, "mini-std")[0]["compileSlots"], 2)
            self.assertEqual(cr.member_labels(manifest, "mini-std")[0]["canonicalRoots"], 2)
            self.assertEqual(cr.member_labels(manifest, "mini-std")[0]["guiPools"], [], "no gui runner by default")
            self.assertEqual(cr.member_labels(manifest, "mini-std")[0]["sessionPools"], [])
            manifest["defaults"]["runner"]["classes"]["std"]["guiRunners"] = 1
            self.assertEqual(cr.member_labels(manifest, "mini-std")[0]["guiPools"], ["glaeda-gui-std-xcode-26.6"])
            manifest["defaults"]["runner"]["classes"]["std"]["canonicalRoots"] = 3  # 3 runners, all roots
            self.assertIn("guiRunners must be 0 to 0", cr.member_labels(manifest, "mini-std")[1])
            # a second gui runner per mini: session-only jobs need a logged-in session, not the one gui token
            manifest["defaults"]["runner"]["classes"]["std"].update(runners=6, canonicalRoots=2, guiRunners=2)
            two = cr.member_labels(manifest, "mini-std")[0]
            self.assertEqual((two["guiRunners"], two["guiPools"], two["sessionPools"]),
                             (2, ["glaeda-gui-std-xcode-26.6"], ["glaeda-session-std-xcode-26.6"]))
            self.assertIn("guiRunners must be 0 to 4", cr.member_labels(
                json.loads(json.dumps(manifest).replace('"guiRunners": 2', '"guiRunners": 5')), "mini-std")[1])
            manifest["defaults"]["runner"]["classes"]["std"]["runners"] = 3
            for bad in (2, -1, True, "1"):
                manifest["defaults"]["runner"]["classes"]["std"].update(canonicalRoots=2, guiRunners=bad)
                self.assertIn("guiRunners must be 0 to 1", cr.member_labels(manifest, "mini-std")[1], bad)
            del manifest["defaults"]["runner"]["classes"]["std"]["guiRunners"]
            manifest["defaults"]["runner"]["classes"]["std"]["canonicalRoots"] = 4  # 3 runners
            self.assertIn("canonicalRoots must be 1 to 3", cr.member_labels(manifest, "mini-std")[1])
            del manifest["defaults"]["runner"]["classes"]["std"]["canonicalRoots"]
            manifest["defaults"]["runner"]["classes"]["std"]["compileSlots"] = 4  # 15 cores hold 3 5-core compiles
            self.assertIn("compileSlots must be 1 to 3 (a compile is 5 units)", cr.member_labels(manifest, "mini-std")[1])
            for bad in ({"runners": 0}, {"runners": True}, {"capacityUnits": 1}, {"capacityUnits": 4}, "four"):
                with self.subTest(bad=bad):
                    manifest["defaults"]["runner"] = {"classes": {"std": bad}}
                    member, why = cr.member_labels(manifest, "mini-std")
                    self.assertIsNone(member)
                    self.assertIn("runner.classes.std", why)

    def test_trusted_ref_moves_the_member_to_its_own_pool(self) -> None:
        with mock.patch.object(cr, "xcode_present", return_value=True):
            manifest = json.loads(json.dumps(MANIFEST))
            trusted = {"trustedRef": "refs/heads/main", "trustedRepo": "manaflow-ai/cmux"}
            manifest["hosts"]["mini-std"].setdefault("overrides", {})["runner"] = dict(trusted)
            member, why = cr.member_labels(manifest, "mini-std")
            self.assertIsNone(why)
            self.assertEqual(member["pools"], ["glaeda-trusted-std-xcode-26.6"])
            self.assertIn("glaeda-trusted", member["labels"])
            self.assertNotIn("glaeda-std-xcode-26.6", member["labels"], "a PR run must never route here")
            self.assertEqual(member["trustedRef"], "refs/heads/main")
            self.assertIsNone(cr.member_labels(MANIFEST, "mini-std")[0]["trustedRef"])
            for bad in ({**trusted, "trustedRef": "main"}, {**trusted, "trustedRef": "refs/pull/1/merge"},
                        {**trusted, "trustedRef": "refs/heads/a b"}, {**trusted, "trustedRef": 7},
                        {"trustedRef": "refs/heads/main"}, {"trustedRepo": "manaflow-ai/cmux"},
                        {**trusted, "trustedRepo": "cmux"}):
                with self.subTest(bad=bad):
                    manifest["hosts"]["mini-std"]["overrides"]["runner"] = bad
                    self.assertIn("trustedRef", cr.member_labels(manifest, "mini-std")[1])

    def test_trusted_next_nightly_manifest_fails_closed(self) -> None:
        trusted = {"trustedRef": "refs/heads/main", "trustedRepo": "manaflow-ai/cmux", "trustedNextNightly": True}
        with mock.patch.object(cr, "xcode_present", return_value=True):
            manifest = json.loads(json.dumps(MANIFEST))
            host = manifest["hosts"]["mini-std"]
            host["roles"].remove("dev-builds")
            host["overrides"] = {"runner": trusted}
            self.assertTrue(cr.member_labels(manifest, "mini-std")[0]["trustedNextNightly"])
            self.assertIn("glaeda-trusted-next-nightly", cr.member_labels(manifest, "mini-std")[0]["labels"])
            self.assertNotIn("glaeda-trusted-next-nightly", cr.member_labels(MANIFEST, "mini-std")[0]["labels"])
            self.assertFalse(cr.member_labels(MANIFEST, "mini-std")[0]["trustedNextNightly"])
            for bad in ({**trusted, "trustedNextNightly": "true"}, {**trusted, "trustedNextNightly": 1},
                        {**trusted, "trustedNextNightly": None}, {"trustedNextNightly": True},
                        {**trusted, "trustedRef": "refs/heads/feat-cmux-next"},
                        {**trusted, "trustedRepo": "manaflow-ai/other"}):
                with self.subTest(bad=bad):
                    host["overrides"]["runner"] = bad
                    self.assertIn("trustedNextNightly", cr.member_labels(manifest, "mini-std")[1])
            host["overrides"]["runner"] = trusted
            host["roles"].append("dev-builds")
            self.assertIn("trustedNextNightly", cr.member_labels(manifest, "mini-std")[1])

    def test_ios_sim_label_needs_the_manifest_and_a_runtime(self) -> None:
        def rt(build: str, available: bool = True, platform: str = "iOS") -> dict:
            return {"platform": platform, "version": "26.5", "buildversion": build, "isAvailable": available}
        with mock.patch.object(cr, "xcode_present", return_value=True):
            manifest = json.loads(json.dumps(MANIFEST))
            self.assertNotIn("glaeda-ios-sim", cr.member_labels(manifest, "mini-std")[0]["labels"])
            manifest["hosts"]["mini-std"]["roles"] = [*manifest["hosts"]["mini-std"]["roles"], "ios-simulators"]
            runtimes = {"runtimes": [rt("23F77")]}
            with mock.patch.object(cr, "run", return_value=(0, "warning: noise\n" + json.dumps(runtimes))):
                self.assertNotIn("glaeda-ios-sim", cr.member_labels(manifest, "mini-std")[0]["labels"],
                                 "no declared runtime, no label")
                manifest["defaults"]["ios_simulator"] = {"runtimes": ["23F77"], "exclusive": True}
                member, _ = cr.member_labels(manifest, "mini-std")
                self.assertIn("glaeda-ios-sim", member["labels"])
                self.assertTrue(member["iosSim"])
            for label, found in (("none", {"runtimes": []}),
                                 ("26.3.1 only", {"runtimes": [rt("23D8133")]}),
                                 ("unavailable", {"runtimes": [rt("23F77", available=False)]}),
                                 ("watchOS", {"runtimes": [rt("23F77", platform="watchOS")]})):
                with self.subTest(label), mock.patch.object(cr, "run", return_value=(0, json.dumps(found))):
                    self.assertNotIn("glaeda-ios-sim", cr.member_labels(manifest, "mini-std")[0]["labels"])
            manifest["defaults"]["ios_simulator"]["runtimes"] = ["23F77", "24A5390f"]
            with mock.patch.object(cr, "run", return_value=(0, json.dumps(runtimes))):
                self.assertNotIn("glaeda-ios-sim", cr.member_labels(manifest, "mini-std")[0]["labels"],
                                 "every declared runtime must be present")
            manifest["defaults"]["ios_simulator"]["runtimes"] = ["23F77"]
            with mock.patch.object(cr, "run", return_value=(72, "xcrun: error")) as run:
                self.assertNotIn("glaeda-ios-sim", cr.member_labels(manifest, "mini-std")[0]["labels"])
                self.assertIn("glaeda-ios-sim", cr.member_labels(manifest, "mini-std", ["glaeda-ios-sim"])[0]["labels"],
                              "a simctl hiccup keeps the label the runner registered with")
                self.assertTrue(run.call_args.kwargs["env"]["DEVELOPER_DIR"].endswith("/Contents/Developer"))
            with mock.patch.object(cr, "run", return_value=(0, json.dumps({"runtimes": []}))):
                self.assertNotIn("glaeda-ios-sim", cr.member_labels(manifest, "mini-std", ["glaeda-ios-sim"])[0]["labels"],
                                 "a clean answer with no runtime drops it")

    def test_member_labels_table(self) -> None:
        with mock.patch.object(cr, "xcode_present", return_value=True):
            std, _ = cr.member_labels(MANIFEST, "mini-std")
            self.assertEqual(std["labels"], ["glaeda-mini", "glaeda-class-std", "glaeda-dedicated", "xcode-26.6",
                                             "glaeda-std-xcode-26.6"])
            light, _ = cr.member_labels(MANIFEST, "mini-light")
            self.assertEqual(light["labels"], ["glaeda-mini", "glaeda-class-light", "glaeda-opportunistic", "xcode-26.6"])
            self.assertEqual(cr.member_labels(MANIFEST, "override")[0]["labels"][-2:],
                             ["xcode-26.3", "glaeda-std-xcode-26.3"])
        with mock.patch.object(cr, "xcode_present", return_value=False):
            self.assertEqual(cr.member_labels(MANIFEST, "mini-std")[0]["labels"],
                             ["glaeda-mini", "glaeda-class-std", "glaeda-dedicated"])
        dev = copy.deepcopy(MANIFEST)
        dev["hosts"]["mini-std"].setdefault("overrides", {})["runner"] = {"devCompile": True}
        with mock.patch.object(cr, "xcode_present", return_value=True):
            summary, why = cr.member_labels(dev, "mini-std")
        self.assertIsNone(why)
        self.assertEqual(summary["pools"], ["glaeda-std-xcode-26.6"])
        self.assertEqual(summary["devPools"], ["glaeda-dev-std-xcode-26.6"])
        self.assertTrue(summary["devCompile"])
        self.assertNotIn("glaeda-dev-std-xcode-26.6", summary["labels"], "DEV labels belong to one instance")
        self.assertEqual(summary["devCompileInstance"], 1)
        aws = copy.deepcopy(MANIFEST)
        aws["hosts"]["mini-std"].setdefault("overrides", {})["runner"] = {"poolPrefix": "aws-"}
        with mock.patch.object(cr, "xcode_present", return_value=True):
            member, why = cr.member_labels(aws, "mini-std")
        self.assertIsNone(why)
        self.assertIn("glaeda-aws-std-xcode-26.6", member["labels"])
        self.assertEqual(member["rootPools"], ["glaeda-aws-root-std-xcode-26.6"])
        self.assertEqual(member["sidePools"], ["glaeda-aws-side-std-xcode-26.6", "glaeda-aws-side-light-xcode-26.6"])
        for prefix in ("root-", "side-", "gui-", "trusted-", "std-", "light-", "xl-", "dev-"):
            invalid = copy.deepcopy(MANIFEST)
            invalid["hosts"]["mini-std"].setdefault("overrides", {})["runner"] = {"poolPrefix": prefix}
            member, why = cr.member_labels(invalid, "mini-std")
            self.assertIsNone(member)
            self.assertIn("poolPrefix", why)
        for name, why in (("mini-no-role", "ci-runner"), ("laptop", "never runs"), ("borrowed", "never runs"),
                          ("old-shape", "m4pro-48"), ("bad-avail", "availability"), ("absent", "not a member"),
                          ("no-hardware", "no hardware class")):
            with self.subTest(name):
                member, reason = cr.member_labels(MANIFEST, name)
                self.assertIsNone(member)
                self.assertIn(why, reason)

    def test_xcode_present_needs_the_exact_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Xcode_26.6.app"
            app.mkdir()
            for out, want in (("Xcode 26.6\nBuild version 17F113", True), ("Xcode 26.6\nBuild version 17F1134", False),
                              ("Xcode 26.6\nBuild version 17F11", False), ("Xcode 26.3\nBuild version 17F113", False)):
                with self.subTest(out=out), mock.patch.object(cr, "run", return_value=(0, out)):
                    got = cr.xcode_present({"path": os.fspath(app), "version": "26.6", "build": "17F113"})
                    self.assertEqual(got, want)
            link = Path(tmp) / "Link.app"
            link.symlink_to(app)
            with mock.patch.object(cr, "run", return_value=(0, "Xcode 26.6\nBuild version 17F113")):
                self.assertFalse(cr.xcode_present({"path": os.fspath(link), "version": "26.6", "build": "17F113"}))

    def test_malformed_manifest_is_refused_not_a_traceback(self) -> None:
        for manifest in ({"hosts": {"m": {"class": "std", "availability": "dedicated", "roles": ["ci-runner"],
                                          "overrides": ["x"]}}},
                         {"defaults": {"xcode": "26.6"},
                          "hosts": {"m": {"class": "std", "availability": "dedicated", "roles": ["ci-runner"]}}},
                         {"hosts": {"m": "std"}}, {"hosts": []}, []):
            with self.subTest(manifest=manifest):
                member, why = cr.member_labels(manifest, "m")
                if member is not None:  # a non-object xcode block just means no Xcode labels
                    self.assertEqual(member["labels"], ["glaeda-mini", "glaeda-class-std", "glaeda-dedicated"])
                else:
                    self.assertTrue(why)


fleet_labels = load("glaeda_fleet_labels_under_test", ROOT / "scripts" / "glaeda_fleet_labels.py")


class FleetLabelsModuleTest(unittest.TestCase):
    """The shared rule glaeda-mini-fleet imports: pin its strings and its declared/conforming views."""

    def test_dev_compile_declares_a_separate_pool_without_adding_capacity(self) -> None:
        manifest = copy.deepcopy(MANIFEST)
        before = fleet_labels.member_labels(manifest, "mini-std", lambda app: True)[0]
        manifest["hosts"]["mini-std"].setdefault("overrides", {})["runner"] = {"devCompile": True}
        after, why = fleet_labels.member_labels(manifest, "mini-std", lambda app: True)
        self.assertIsNone(why)
        for field in ("runners", "capacityUnits", "compileSlots", "canonicalRoots", "pools", "labels"):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(fleet_labels.declared_pools(manifest), {
            "glaeda-dev-std-xcode-26.6": ["mini-std"], "glaeda-std-xcode-26.3": ["override"],
            "glaeda-std-xcode-26.6": ["mini-std"]})
        self.assertNotIn("glaeda-dev-std-xcode-26.6", fleet_labels.declared_pools(
            manifest, lambda member, app: member != "mini-std"))
        self.assertEqual(fleet_labels.widened_labels(after["devPools"]), after["devPools"])
        manifest["hosts"]["mini-std"]["overrides"]["runner"]["poolPrefix"] = "dev-"
        self.assertEqual(fleet_labels.member_labels(manifest, "mini-std", lambda app: True)[0], after,
                         "the DEV namespace never moves neighboring instances out of their normal pool")

    def test_the_nightly_slot_declares_its_own_pool_without_adding_capacity(self) -> None:
        manifest = copy.deepcopy(MANIFEST)
        before = fleet_labels.member_labels(manifest, "mini-std", lambda app: True)[0]
        manifest["hosts"]["mini-std"].setdefault("overrides", {})["runner"] = {"nightlySlot": True}
        after, why = fleet_labels.member_labels(manifest, "mini-std", lambda app: True)
        self.assertIsNone(why)
        for field in ("runners", "capacityUnits", "compileSlots", "canonicalRoots", "pools", "labels", "sidePools"):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after["nightlyPools"], ["glaeda-nightly-std-xcode-26.6"])
        self.assertEqual((after["nightlySlot"], after["nightlyInstance"]), (True, 1))
        self.assertEqual((before["nightlyPools"], before["nightlyInstance"]), ([], None))
        self.assertIn("glaeda-nightly-std-xcode-26.6", fleet_labels.declared_pools(manifest))
        self.assertEqual(fleet_labels.widened_labels(after["nightlyPools"]), after["nightlyPools"])
        manifest["hosts"]["mini-std"]["overrides"]["runner"]["devCompile"] = True
        manifest["defaults"]["runner"] = {"classes": {"std": {"runners": 3}}}
        both, why = fleet_labels.member_labels(manifest, "mini-std", lambda app: True)
        self.assertIsNone(why)
        self.assertEqual((both["devCompileInstance"], both["nightlyInstance"]), (1, 2))
        cases = [({"nightlySlot": value}, {}, "nightlySlot must be boolean") for value in ("true", 1, None)]
        cases += [({"nightlySlot": True, "devCompile": True}, {"runners": 2}, "and the freshness slot"),
                  ({"nightlySlot": True}, {"runners": 2, "canonicalRoots": 2}, "needs an existing side instance"),
                  ({"nightlySlot": True, "poolPrefix": "aws-"}, {}, "default pool namespace"),
                  ({"nightlySlot": True, "trustedRef": "refs/heads/main", "trustedRepo": "manaflow-ai/cmux"}, {},
                   "without trustedRef")]
        for runner, shape, message in cases:
            with self.subTest(runner=runner, shape=shape):
                bad = copy.deepcopy(MANIFEST)
                bad["defaults"]["runner"] = {"classes": {"std": shape}} if shape else {}
                bad["hosts"]["mini-std"].setdefault("overrides", {})["runner"] = runner
                member, why = fleet_labels.member_labels(bad, "mini-std", lambda app: True)
                self.assertIsNone(member)
                self.assertIn(message, why)
        self.assertIn("nightly-", fleet_labels.RESERVED_POOL_PREFIXES)

    def test_dev_compile_malformed_manifest_fails_closed(self) -> None:
        cases = [({"devCompile": value}, {}, "devCompile must be boolean")
                 for value in ("true", 1, None, [])]
        cases += [({"devCompile": True}, shape, "needs an existing side instance") for shape in (
            {"runners": 1}, {"runners": 2, "canonicalRoots": 2},
            {"runners": 3, "canonicalRoots": 1, "guiRunners": 1, "fastRunners": 1})]
        cases += [({"devCompile": True, "poolPrefix": "aws-"}, {}, "devCompile uses poolPrefix"),
                  ({"devCompile": True, "trustedRef": "refs/heads/main", "trustedRepo": "manaflow-ai/cmux"},
                   {}, "dedicated std runner without trustedRef")]
        for runner, shape, message in cases:
            with self.subTest(runner=runner, shape=shape):
                manifest = copy.deepcopy(MANIFEST)
                manifest["defaults"]["runner"] = {"classes": {"std": shape}}
                manifest["hosts"]["mini-std"].setdefault("overrides", {})["runner"] = runner
                summary, why = fleet_labels.member_labels(manifest, "mini-std", lambda app: True)
                self.assertIsNone(summary)
                self.assertIn(message, why)
                self.assertFalse(any("dev-" in label for label in fleet_labels.declared_pools(manifest)))

    def test_pool_label_string(self) -> None:
        self.assertEqual(fleet_labels.pool_label("std", "26.6"), "glaeda-std-xcode-26.6")
        self.assertEqual(fleet_labels.pool_label("light", "26.6"), "glaeda-light-xcode-26.6")
        self.assertEqual(fleet_labels.pool_label("std", "26.3", pool_prefix="aws-"),
                         "glaeda-aws-std-xcode-26.3")
        self.assertEqual(fleet_labels.root_label("glaeda-aws-std-xcode-26.3"),
                         "glaeda-aws-root-std-xcode-26.3")

    def test_fast_runners_field(self) -> None:
        manifest = json.loads(json.dumps(MANIFEST))
        std = {"runners": 5, "capacityUnits": 5, "canonicalRoots": 2}
        manifest["defaults"]["runner"] = {"classes": {"std": std}}
        today = fleet_labels.member_labels(manifest, "mini-std", lambda app: True)[0]
        self.assertEqual((today["fastRunners"], today["fastPools"]), (0, []))
        std["fastRunners"] = 0
        self.assertEqual(fleet_labels.member_labels(manifest, "mini-std", lambda app: True)[0], today)
        std["fastRunners"] = 1
        fast = fleet_labels.member_labels(manifest, "mini-std", lambda app: True)[0]
        self.assertEqual((fast["fastRunners"], fast["fastPools"]), (1, ["glaeda-fast-std-xcode-26.6"]))
        self.assertEqual(fast["pools"], today["pools"], "the picker's pool count is unchanged")
        self.assertEqual(fleet_labels.fast_label("glaeda-aws-std-xcode-26.3"), "glaeda-aws-fast-std-xcode-26.3")
        for bad in (2, -1, True, "1"):
            std["fastRunners"] = bad
            self.assertIn("fastRunners must be 0 or 1", fleet_labels.member_labels(manifest, "mini-std", lambda a: True)[1])
        std.update(fastRunners=1, guiRunners=1, canonicalRoots=4)
        self.assertIn("fastRunners must be 0 or 1", fleet_labels.member_labels(manifest, "mini-std", lambda a: True)[1])
        manifest["hosts"]["mini-std"]["overrides"] = {"runner": {"poolPrefix": "fast-"}}
        std.update(canonicalRoots=2)
        self.assertIn("poolPrefix", fleet_labels.member_labels(manifest, "mini-std", lambda a: True)[1])

    def test_runner_and_module_agree(self) -> None:
        with mock.patch.object(cr, "xcode_present", return_value=True):
            for member in MANIFEST["hosts"]:
                with self.subTest(member):
                    self.assertEqual(cr.member_labels(MANIFEST, member),
                                     fleet_labels.member_labels(MANIFEST, member, lambda app: True))

    def test_declared_and_conforming_pools(self) -> None:
        declared = fleet_labels.declared_pools(MANIFEST)
        self.assertEqual(declared, {"glaeda-std-xcode-26.3": ["override"], "glaeda-std-xcode-26.6": ["mini-std"]})
        conforming = fleet_labels.declared_pools(MANIFEST, lambda member, app: member != "mini-std")
        self.assertEqual(conforming, {"glaeda-std-xcode-26.3": ["override"]})
        self.assertEqual(fleet_labels.declared_pools({"hosts": []}), {})

    def test_macos_label_comes_from_the_observed_major_version(self) -> None:
        self.assertEqual(fleet_labels.macos_label("27"), "glaeda-macos-27")
        summary, _ = fleet_labels.member_labels(MANIFEST, "mini-std", lambda app: True, macos_major="27")
        self.assertIn("glaeda-macos-27", summary["labels"])
        self.assertNotIn("glaeda-macos-27", summary["pools"], "the OS label is never a pool label")
        for major in (None, "", "27.0", "macos-27", "0"):
            summary, _ = fleet_labels.member_labels(MANIFEST, "mini-std", lambda app: True, macos_major=major)
            self.assertFalse([x for x in summary["labels"] if "macos" in x], major)

    def test_runner_reads_sw_vers_and_keeps_its_prior_label_when_sw_vers_fails(self) -> None:
        for out, want in (((0, "27.0.1"), "27"), ((0, "26.5"), "26"), ((0, "15"), "15"),
                          ((1, ""), None), ((127, ""), None), ((0, "garbage"), None)):
            with self.subTest(out), mock.patch.object(cr, "run", return_value=out):
                self.assertEqual(REAL_MACOS_MAJOR(), want)
        with mock.patch.object(cr, "xcode_present", return_value=True):
            with mock.patch.object(cr, "macos_major", return_value="27"):
                self.assertIn("glaeda-macos-27", cr.member_labels(MANIFEST, "mini-std", ["glaeda-macos-26"])[0]["labels"])
            labels = cr.member_labels(MANIFEST, "mini-std", ["glaeda-mini", "glaeda-macos-26"])[0]["labels"]
            self.assertIn("glaeda-macos-26", labels, "a sw_vers hiccup keeps the registered OS label")
            self.assertFalse([x for x in cr.member_labels(MANIFEST, "mini-std")[0]["labels"] if "macos" in x])

    def test_runner_without_the_module_explains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cr, "SCRIPT_DIR", Path(tmp)):
            member, why = cr.member_labels(MANIFEST, "mini-std")
        self.assertIsNone(member)
        self.assertIn("glaeda_fleet_labels.py must sit next to this script", why)


class ConsoleTest(unittest.TestCase):
    ON = '"kCGSSessionOnConsoleKey"=Yes,"kCGSessionLoginDoneKey"=Yes,"kCGSSessionUserNameKey"="cmux"'

    def parse(self, *rows: str) -> object:
        return hook.parse_console_users("\n".join(["+-o Root  <class IORegistryEntry>", "    {", *rows, "    }"]))

    def test_parses_the_console_session(self) -> None:
        on = self.ON
        cases = {
            "unlocked": ((f'      "IOConsoleUsers" = ({{{on},"kCGSSessionIDKey"=257}})',), ("unlocked", "cmux")),
            "screen-locked": ((f'      "IOConsoleUsers" = ({{{on},"CGSSessionScreenIsLocked"=Yes}})',),
                              ("locked", "cmux")),
            "console locked": (('      "IOConsoleLocked" = Yes', f'      "IOConsoleUsers" = ({{{on}}})'),
                               ("locked", "cmux")),
            "login window": (('      "IOConsoleLocked" = Yes',), ("no_user", None)),
            "no sessions": (('      "IOConsoleUsers" = ()',), ("no_user", None)),
            "a switched-out session only": (
                ('      "IOConsoleUsers" = ({"kCGSSessionOnConsoleKey"=No,"kCGSSessionUserNameKey"="cmux"})',),
                ("no_user", None)),
            "login in progress": ((f'      "IOConsoleUsers" = ({{{on.replace("LoginDoneKey\"=Yes", "LoginDoneKey\"=No")}}})',),
                                  ("no_user", None)),
            "the on-console one of two": (
                ('      "IOConsoleUsers" = ({"kCGSSessionOnConsoleKey"=No,"kCGSSessionUserNameKey"="leo"},'
                 f'{{{on},"CGSSessionScreenIsLocked"=Yes}})',), ("locked", "cmux")),
        }
        for name, (rows, want) in cases.items():
            with self.subTest(name):
                self.assertEqual(tuple(self.parse(*rows)), want)
        self.assertIsNone(hook.parse_console_users(""), "not the Root entry: unknown")
        self.assertIsNone(hook.parse_console_users("ioreg: error"))

    def test_refusal_follows_the_state_and_an_unreadable_one_changes_nothing(self) -> None:
        cases = ((hook.ConsoleState("unlocked", "cmux"), None), (None, None),
                 (hook.ConsoleState("locked", "cmux"), "console: the console session (cmux) is screen-locked"),
                 (hook.ConsoleState("no_user"), "console: no user is logged in at the console"))
        for state, want in cases:
            with self.subTest(state), mock.patch.object(hook, "console_state", return_value=state):
                why = hook.console_refusal()
                self.assertEqual(why if why is None else why[:len(want)], want)
        with mock.patch.object(hook, "CONSOLE_GATE", False), \
                mock.patch.object(hook, "console_state", return_value=hook.ConsoleState("locked", "cmux")):
            self.assertIsNone(hook.console_refusal())
        with mock.patch.object(hook, "IOREG", "/nonexistent/ioreg"):
            self.assertIsNone(hook.console_state())


class NoEmDashTest(unittest.TestCase):
    def test_no_em_dashes(self) -> None:
        for path in (ROOT / "scripts/glaeda-cmux-runner", HOOK, Path(__file__), ROOT / "docs/CMUX_MINI_RUNNER.md",
                     ROOT / "scripts/glaeda_fleet_labels.py", ROOT / "scripts/glaeda_reservation.py"):
            self.assertNotIn(chr(0x2014), path.read_text(encoding="utf-8"), path)


class JobTelemetryTest(unittest.TestCase):
    """The per-job host sampler: attribution, verdicts, the bounded log, and the fork/finish handshake."""

    def setUp(self) -> None:
        self.hook = load("glaeda_cmux_runner_hook_telemetry", HOOK)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_classify_splits_this_job_other_jobs_and_outside(self) -> None:
        rows = [
            (100, 1, 1.0, "cmux", "/Users/cmux/actions-runner-glaeda/bin/Runner.Worker"),
            (101, 100, 700.0, "cmux", "/Applications/Xcode.app/usr/bin/swift-frontend"),
            (200, 1, 0.5, "cmux", "/Users/cmux/actions-runner-glaeda-2/bin/Runner.Worker"),
            (201, 200, 300.0, "cmux", "/usr/local/bin/node"),
            (300, 1, 450.0, "cmux", "/opt/homebrew/bin/zig"),
            (301, 1, 50.0, "root", "/usr/libexec/mds_stores"),
            (400, 1, 90.0, "cmux", "/bin/ps"),
        ]
        job, others, outside, by_outside, by_runner = self.hook.classify(rows, 100, skip={400})
        self.assertAlmostEqual(job, 7.01)
        self.assertAlmostEqual(others, 3.005)
        self.assertAlmostEqual(outside, 5.0)
        self.assertEqual(by_runner, {"actions-runner-glaeda-2": 3.005})
        self.assertEqual(set(by_outside), {"zig (cmux)", "mds_stores (root)"})
        self.assertFalse(any("/" in key for key in by_outside), "no paths leave the host")

    def test_job_rss_sums_this_jobs_tree_and_its_test_app(self) -> None:
        rows = [
            (100, 1, 1.0, "cmux", "/Users/cmux/actions-runner-glaeda/bin/Runner.Worker"),
            (101, 100, 0.0, "cmux", "swift-frontend"),
            (102, 101, 0.0, "cmux", "ld"),
            (200, 1, 0.5, "cmux", "/Users/cmux/actions-runner-glaeda-2/bin/Runner.Worker"),
            (201, 200, 300.0, "cmux", "node"),
            (300, 1, 10.0, "cmux", "cmux DEV"),
            (301, 1, 50.0, "root", "mds_stores"),
            (400, 1, 90.0, "cmux", "ps"),
        ]
        rss = {100: 100_000, 101: 2_000_000, 102: 500_000, 200: 90_000, 201: 700_000, 300: 400_000,
               301: 50_000, 400: 1_000}
        # idle processes (0 %cpu) still hold memory, and the reparented test app is the job's
        self.assertEqual(self.hook.job_rss_kib(rows, rss, 100, {400}, {300: 100}), 3_000_000)
        self.assertEqual(self.hook.job_rss_kib(rows, rss, 200, {400}, {300: 100}), 790_000)
        self.assertIsNone(self.hook.job_rss_kib(rows, {}, 100, set()), "no RSS read is unknown, not zero")
        zombies = {pid: 0 for pid in rss}  # a tree of zombies while the Runner.Worker exits
        self.assertIsNone(self.hook.job_rss_kib(rows, zombies, 100, set()), "an all-zero reading is unknown too")
        self.assertEqual(self.hook.job_rss_kib(rows, {**rss, 101: 0}, 100, {400}, {300: 100}), 1_000_000)

    def test_summary_records_the_peak_footprint_beside_rss(self) -> None:
        split = (1.0, 0.0, 0.0, {}, {})
        samples = self.hook.JobSamples({}, 14, 0.0)
        for rss, fp in ((4_194_304, 1_048_576), (3_145_728, 2_097_152), (None, None)):
            samples.add(None, split, 10.0, rss_kib=rss, footprint_kib=fp)
        summary = samples.summary(60.0)
        self.assertEqual((summary["rss_peak_mib"], summary["rss_footprint_peak_mib"]), (4096.0, 2048.0))
        rss_only = self.hook.JobSamples({}, 14, 0.0)
        rss_only.add(None, split, 10.0, rss_kib=1024)  # off Darwin: no footprint, so null and never 0
        self.assertIsNone(rss_only.summary(60.0)["rss_footprint_peak_mib"])

    def test_tree_kib_sums_nonzero_readings_of_the_tree(self) -> None:
        self.assertEqual(self.hook.tree_kib([1, 2, 3], {1: 10, 2: 0, 4: 99}), 10)
        self.assertIsNone(self.hook.tree_kib([1, 2], {1: 0, 2: 0}), "zombies only: unknown, not 0")
        self.assertIsNone(self.hook.tree_kib([1], {}), "nothing readable: unknown, not 0")

    @unittest.skipUnless(sys.platform == "darwin", "proc_pid_rusage is Darwin's")
    def test_phys_footprint_is_read_for_own_processes_only(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import sys; b = bytearray(64 << 20); sys.stdin.read()"],
                                 stdin=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            got: dict[int, int] = {}
            while time.monotonic() < deadline:
                got = self.hook.phys_footprints_kib([os.getpid(), child.pid, 1, 999_999_999])
                if got.get(child.pid, 0) >= 60 << 10:
                    break
                time.sleep(0.1)
            self.assertGreater(got.get(os.getpid(), 0), 0)
            self.assertGreaterEqual(got.get(child.pid, 0), 60 << 10, "the child's 64 MiB of dirty pages")
            self.assertNotIn(999_999_999, got, "no such process")
            if os.geteuid() != 0:
                self.assertNotIn(1, got, "launchd is root's: not readable, left out rather than 0")
        finally:
            child.stdin.close()
            child.wait(10)

    def test_phys_footprint_off_darwin_is_empty(self) -> None:
        with mock.patch.object(self.hook, "_LIBSYSTEM", [None]):
            self.assertEqual(self.hook.phys_footprints_kib([os.getpid()]), {})

    def test_summary_records_the_peak_job_rss_or_null(self) -> None:
        split = (1.0, 0.0, 0.0, {}, {})
        samples = self.hook.JobSamples({}, 14, 0.0)
        for kib in (1_048_576, None, 3_145_728, 2_097_152):
            samples.add(None, split, 10.0, rss_kib=kib)
        self.assertEqual(samples.summary(60.0)["rss_peak_mib"], 3072.0)
        unknown = self.hook.JobSamples({}, 14, 0.0)
        unknown.add(None, split, 10.0)
        self.assertIsNone(unknown.summary(60.0)["rss_peak_mib"])

    def test_ps_rows_reads_rss_from_the_same_ps_pass(self) -> None:
        calls = []

        def fake_run(argv, **_kwargs):
            calls.append(argv[-1])
            out = ("100 Runner.Worker\n101 swift-frontend\n" if argv[-1] == "pid=,ucomm=" else
                   "  100     1   1.0 cmux   102400 /Users/cmux/actions-runner/bin/Runner.Worker\n"
                   "  101   100 700.0 cmux  2048000 /Applications/Xcode.app/usr/bin/swift frontend\n")
            return subprocess.CompletedProcess(argv, 0, out, "")

        with mock.patch.object(self.hook.subprocess, "run", fake_run):
            rss: dict[int, int] = {}
            rows = self.hook.ps_rows(rss)
            plain = self.hook.ps_rows()
        self.assertEqual(rss, {100: 102400, 101: 2048000})
        self.assertEqual(rows, plain)
        self.assertEqual(rows[1], (101, 100, 700.0, "cmux", "swift-frontend"))
        self.assertEqual(calls.count("pid=,ppid=,pcpu=,user=,rss=,comm="), 2, "one ps pass per call, no extra")

    def test_dynamic_release_needs_sustained_quiet_headroom(self) -> None:
        decide = self.hook.dynamic_release_decision
        self.assertIsNone(decide(100, 20, 2, 5, 0.8, 55, 0, 1, False))
        self.assertEqual(decide(200, 120, 2, 5, 0.8, 55, 0, 1, False)[0], 1)
        self.assertIsNone(decide(200, 120, 2, 5, 0.8, 85, 0, 1, False))
        self.assertIsNone(decide(200, 120, 2, 5, 0.8, 55, 1, 1, False))
        self.assertIsNone(decide(200, 120, 2, 5, 0.8, 55, 0, 2, False))
        # units are cores: a 6-core job quiet after warmup gives back one core at a time, down to half its admission
        self.assertEqual(decide(200, 120, 6, 14, 1.0, 55, 0, 1, False, admitted=6), (5, "sustained low job CPU with host headroom"))
        self.assertEqual(decide(200, 120, 4, 14, 1.0, 55, 0, 1, False, admitted=6)[0], 3)
        self.assertIsNone(decide(200, 120, 3, 14, 1.0, 55, 0, 1, False, admitted=6), "3 is half of 6: it keeps them")
        self.assertIsNone(decide(200, 120, 3, 14, 1.0, 55, 0, 1, False, admitted=5), "ceil(5 / 2) is 3")
        self.assertEqual(decide(200, 120, 2, 14, 0.8, 55, 0, 1, False, admitted=2)[0], 1, "a 2-core job still may go to 1")

    def test_reparented_test_app_cpu_belongs_to_exact_live_job(self) -> None:
        first = "/Users/cmux/actions-runner-glaeda"
        second = "/Users/cmux/actions-runner-glaeda-2"
        temp = first + "/_work/_temp/cmux-derived-data-tests-123-1-shard-2"
        stale = first + "/_work/_temp/cmux-derived-data-tests-122-1-shard-2"
        app = "/Build/Products/Debug/cmux DEV.app/Contents/MacOS/cmux DEV"
        table = {
            100: (1, first + "/bin/Runner.Worker"),
            101: (100, "/usr/bin/xcodebuild -derivedDataPath " + temp + "/Build test-without-building"),
            200: (1, second + "/bin/Runner.Worker"),
            201: (200, "/bin/bash " + stale + "/run.sh"),
            300: (1, temp + app),
            301: (300, "/usr/bin/helper"),
            400: (1, stale + app),
            500: (1, "/Applications/cmux.app/Contents/MacOS/cmux"),
        }
        owners = self.hook.telemetry_test_app_owners(table)
        self.assertEqual(owners, {300: 100})
        rows = [(pid, parent, 100.0, "cmux", command if pid in (100, 200) else "cmux DEV")
                for pid, (parent, command) in table.items()]
        own = self.hook.classify(rows, 100, set(), owners)
        peer = self.hook.classify(rows, 200, set(), owners)
        self.assertEqual(own[:3], (4.0, 2.0, 2.0))
        self.assertEqual(peer[:3], (2.0, 4.0, 2.0))
        self.assertEqual(own[3], {"cmux DEV (cmux)": 2.0})

    def test_summary_records_disk_free_at_start_and_end(self) -> None:
        """The disk floor (--min-free-gib) is a judgement call: each job records the free GiB on the disk the floor
        checks at admission and at its end, so a floor can come from what jobs write."""
        samples = self.hook.JobSamples({}, 14, 0.0)
        record = samples.summary(10.0)
        self.assertIsNone(record["disk_free_start_gib"])
        self.assertIsNone(record["disk_free_end_gib"])
        samples.disk_free_start, samples.disk_free_end = 61.234, 48.96
        record = samples.summary(10.0)
        self.assertEqual((record["disk_free_start_gib"], record["disk_free_end_gib"]), (61.2, 49.0))

    def test_summary_flags_outside_cpu_not_its_own_load(self) -> None:
        samples = self.hook.JobSamples({"job": "macos-compile-admission"}, 14, 1000.0)
        for _ in range(6):
            samples.add(30.0, (8.0, 0.0, 5.0, {"zig (cmux)": 5.0}, {}), 10.0)
        record = samples.summary(1060.0)
        self.assertEqual(record["schema"], "glaeda-cmux-job/v1")
        self.assertEqual(record["verdict"], "contended")
        self.assertEqual(record["cores"], {"job": 8.0, "other_runner_jobs": 0.0, "outside": 5.0})
        self.assertEqual(record["top_outside"], [{"process": "zig (cmux)", "core_seconds": 300}])
        self.assertEqual(len(record["reasons"]), 1)
        self.assertIn("zig (cmux)", record["reasons"][0])

        busy = self.hook.JobSamples({}, 14, 0.0)
        busy.add(40.0, (13.0, 0.0, 0.5, {}, {}), 10.0)
        self.assertEqual(busy.summary(10.0)["verdict"], "clear", "a compile's own load is not contention")
        self.assertEqual(busy.summary(10.0)["load"]["mean"], 40.0)

        quiet = self.hook.JobSamples({}, 14, 0.0)
        quiet.add(9.0, (12.0, 1.0, 0.5, {"WindowServer (_windowserver)": 0.5}, {}), 10.0)
        self.assertEqual(quiet.summary(10.0)["verdict"], "clear")

    def test_job_log_keeps_its_newest_half(self) -> None:
        log = self.dir / "Logs" / "jobs.jsonl"
        with mock.patch.object(self.hook, "JOB_LOG_MAX_BYTES", 4096):
            for n in range(200):
                self.hook.append_job_record({"n": n, "pad": "x" * 40}, log)
        lines = log.read_text().splitlines()
        self.assertLessEqual(log.stat().st_size, 4096 + 200)
        self.assertEqual(json.loads(lines[-1])["n"], 199)
        self.assertTrue(all(json.loads(line) for line in lines), "every kept line is whole JSON")

    def test_job_log_cap_is_16_mib_and_trims_under_concurrent_writers(self) -> None:
        self.assertEqual(self.hook.JOB_LOG_MAX_BYTES, 16 * 1024 * 1024)
        log = self.dir / "Logs" / "jobs.jsonl"
        with mock.patch.object(self.hook, "JOB_LOG_MAX_BYTES", 8192):
            def write(w: int) -> None:
                for n in range(150):
                    self.hook.append_job_record({"w": w, "n": n, "pad": "x" * 40}, log)
            threads = [threading.Thread(target=write, args=(w,)) for w in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        lines = log.read_text().splitlines()
        self.assertLessEqual(log.stat().st_size, 8192 + 200)
        records = [json.loads(line) for line in lines]  # every kept line is whole JSON
        self.assertEqual({r["n"] for r in records if r["n"] == 149}, {149}, "the newest lines survive a trim")

    def test_trim_renames_and_leaves_no_temp_file(self) -> None:
        log = self.dir / "Logs" / "jobs.jsonl"
        self.hook.append_job_record({"n": 0}, log)
        before = log.stat().st_ino
        with mock.patch.object(self.hook, "JOB_LOG_MAX_BYTES", 2048):
            for n in range(1, 80):
                self.hook.append_job_record({"n": n, "pad": "x" * 40}, log)
        self.assertNotEqual(log.stat().st_ino, before, "the trim replaces the file, never truncates it")
        self.assertEqual([p.name for p in log.parent.iterdir()], [log.name])
        self.assertEqual(json.loads(log.read_text().splitlines()[-1])["n"], 79)

    def test_job_started_line_skips_a_held_log_lock(self) -> None:
        log = self.dir / "jobs.jsonl"
        log.write_text("")
        with open(log, "a+b") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            start = time.monotonic()
            self.assertFalse(self.hook.append_job_record({"n": 1}, log, wait=False))
            self.assertLess(time.monotonic() - start, 1.0, "the log never delays a job")
            self.hook.job_event("started", {"job": "x"}, log)  # swallowed, not raised
        self.assertEqual(log.read_text(), "")
        self.assertTrue(self.hook.append_job_record({"n": 2}, log, wait=False))

    def test_oversized_record_stays_whole_json(self) -> None:
        log = self.dir / "jobs.jsonl"
        self.hook.append_job_record({"event": "completed", "decision": "d" * 9000, "job": "x"}, log)
        record = json.loads(log.read_text())
        self.assertEqual((record["job"], "decision" in record), ("x", False))

    def test_broken_roots_file_reads_as_none(self) -> None:
        self.hook.roots_file(self.dir).write_bytes(b"\xff\xfe root-1")
        self.assertEqual(self.hook.held_roots(self.dir), [])

    def test_completed_roots_come_from_while_the_worker_lived(self) -> None:
        watched = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (watched.kill(), watched.wait()))
        state = self.dir / "state"
        log = self.dir / "jobs.jsonl"
        state.mkdir()
        self.hook.roots_file(state).write_text("root-1\n")
        self.hook.start_sampler(watched.pid, state, {"job": "x", "roots": ["root-1"]}, log, interval=0.2)
        deadline = time.monotonic() + 10
        while not self.hook.sampler_file(state).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        watched.kill()
        watched.wait()
        self.hook.roots_file(state).write_text("root-2\n")  # the next job on this runner, after the worker died
        self.hook.gui_file(state).write_text("gui\n")
        while (not log.exists() or not log.read_text()) and time.monotonic() < deadline:
            time.sleep(0.1)
        [record] = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual((record["roots"], record["gui"], "roots_admitted" in record), (["root-1"], False, False))

    def test_roots_released_after_the_pid_file_goes_are_not_read_as_empty(self) -> None:
        # finish_sampler removes the pid file, then job-completed removes .roots while the worker still lives
        watched = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (watched.kill(), watched.wait()))
        state = self.dir / "state"
        log = self.dir / "jobs.jsonl"
        state.mkdir()
        self.hook.roots_file(state).write_text("root-1\n")
        self.hook.start_sampler(watched.pid, state, {"job": "x", "roots": ["root-1"]}, log, interval=0.2)
        deadline = time.monotonic() + 10
        while not self.hook.sampler_file(state).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        self.hook.sampler_file(state).unlink()
        self.hook.roots_file(state).unlink()
        while (not log.exists() or not log.read_text()) and time.monotonic() < deadline:
            time.sleep(0.1)
        [record] = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual((record["roots"], "roots_admitted" in record), (["root-1"], False))

    def test_completed_line_has_event_and_final_roots(self) -> None:
        watched = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (watched.kill(), watched.wait()))
        state = self.dir / "state"
        log = self.dir / "jobs.jsonl"
        state.mkdir()
        self.hook.roots_file(state).write_text("root-1\n")
        self.hook.start_sampler(watched.pid, state, {"job": "x", "roots": ["root-1"], "decision": "d"}, log,
                                interval=0.2)
        deadline = time.monotonic() + 10
        while not self.hook.sampler_file(state).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.hook.roots_file(state).write_text("root-2\n")  # take-root --switch mid-job
        time.sleep(2.5)  # a sample takes about a second; the switch is read on the next pass while the job is ours
        self.assertEqual(self.hook.finish_sampler(state), "job telemetry recorded")
        [record] = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual((record["event"], record["roots"], record["roots_admitted"], record["decision"]),
                         ("completed", ["root-2"], ["root-1"], "d"))
        self.assertIsInstance(record["at"], int)

    def test_sampler_writes_one_record_when_finished(self) -> None:
        watched = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (watched.kill(), watched.wait()))
        state = self.dir / "state"
        log = self.dir / "jobs.jsonl"
        note = self.hook.start_sampler(watched.pid, state, {"job": "claude-wrapper", "class": "light"}, log,
                                       interval=0.2)
        self.assertEqual(note, "job telemetry sampling")
        deadline = time.monotonic() + 10
        while not self.hook.sampler_file(state).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.6)
        self.assertEqual(self.hook.finish_sampler(state), "job telemetry recorded")
        records = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["job"], "claude-wrapper")
        self.assertGreaterEqual(records[0]["samples"], 1)
        self.assertIn(records[0]["verdict"], ("clear", "contended"))
        self.assertFalse(self.hook.sampler_file(state).exists())

    def test_completed_record_embeds_bounded_compile_telemetry(self) -> None:
        telemetry = self.dir / self.hook.COMPILE_TELEMETRY_FILE
        telemetry.write_text(json.dumps({
            "schema": self.hook.COMPILE_TELEMETRY_SCHEMA,
            "run_id": "42",
            "run_attempt": "1",
            "cacheable_tasks": 100,
            "hits": 80,
            "misses": 20,
            "hit_rate": 0.8,
            "seed_distance": 3,
            "compile_seconds": 12.5,
            "compile_wall_seconds": 8.25,
            "cache_backend": "fleet",
            "fetch_seconds": 4.0,
            "link_seconds": 1.5,
        }))
        with mock.patch.dict(self.hook.os.environ, {"RUNNER_TEMP": str(self.dir)}, clear=False):
            result = self.hook.compile_telemetry({"run_id": "42", "run_attempt": "1"})
        self.assertEqual(result["cacheable_tasks"], 100)
        self.assertEqual(result["hits"], 80)
        self.assertEqual(result["seed_distance"], 3)
        self.assertEqual(result["compile_wall_seconds"], 8.25)
        self.assertEqual(result["cache_backend"], "fleet")
        self.assertFalse(telemetry.exists())

    def test_completed_record_accepts_legacy_v1_telemetry(self) -> None:
        telemetry = self.dir / self.hook.COMPILE_TELEMETRY_FILE
        telemetry.write_text(json.dumps({
            "schema": self.hook.COMPILE_TELEMETRY_SCHEMA,
            "run_id": "legacy",
            "run_attempt": "1",
            "cacheable_tasks": 1,
            "hits": 1,
            "misses": 0,
            "hit_rate": 1.0,
            "seed_distance": None,
            "compile_seconds": 1.0,
            "fetch_seconds": None,
            "link_seconds": 0.0,
        }))
        with mock.patch.dict(self.hook.os.environ, {"RUNNER_TEMP": str(self.dir)}, clear=False):
            result = self.hook.compile_telemetry({"run_id": "legacy", "run_attempt": "1"})
        self.assertNotIn("cache_backend", result)

    def test_completed_record_rejects_inconsistent_compile_telemetry(self) -> None:
        telemetry = self.dir / self.hook.COMPILE_TELEMETRY_FILE
        telemetry.write_text(json.dumps({
            "schema": self.hook.COMPILE_TELEMETRY_SCHEMA,
            "run_id": "123",
            "run_attempt": "1",
            "cacheable_tasks": 10,
            "hits": 7,
            "misses": 0,
            "hit_rate": 0.7,
            "seed_distance": None,
            "compile_seconds": 12.0,
            "compile_wall_seconds": None,
            "cache_backend": "bogus",
            "fetch_seconds": None,
            "link_seconds": 2.0,
        }))
        meta = {"run_id": "123", "run_attempt": "1"}
        with mock.patch.dict(self.hook.os.environ, {"RUNNER_TEMP": str(self.dir)}, clear=False):
            self.assertIsNone(self.hook.compile_telemetry(meta))
        self.assertFalse(telemetry.exists())

    def test_completed_record_rejects_oversized_numeric_without_raising(self) -> None:
        telemetry = self.dir / self.hook.COMPILE_TELEMETRY_FILE
        telemetry.write_text(json.dumps({
            "schema": self.hook.COMPILE_TELEMETRY_SCHEMA,
            "run_id": "123",
            "run_attempt": "1",
            "cacheable_tasks": 1,
            "hits": 1,
            "misses": 0,
            "hit_rate": 1,
            "seed_distance": None,
            "compile_seconds": 10 ** 400,
            "fetch_seconds": None,
            "link_seconds": 1,
        }))
        with mock.patch.dict(self.hook.os.environ, {"RUNNER_TEMP": str(self.dir)}, clear=False):
            self.assertIsNone(self.hook.compile_telemetry({"run_id": "123", "run_attempt": "1"}))
        self.assertFalse(telemetry.exists())

    def test_sampler_records_when_the_job_dies_first(self) -> None:
        watched = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
        state = self.dir / "state"
        log = self.dir / "jobs.jsonl"
        self.hook.start_sampler(watched.pid, state, {"job": "x"}, log, interval=0.2)
        watched.wait()
        deadline = time.monotonic() + 10
        while (not log.exists() or not log.read_text() or self.hook.sampler_file(state).exists()) \
                and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(len(log.read_text().splitlines()), 1)
        self.assertEqual(self.hook.finish_sampler(state), "no job telemetry sampler")

    def test_host_io_reads_the_second_report(self) -> None:
        out = ("              disk0              disk4       cpu\n"
               "    KB/t  tps  MB/s     KB/t  tps  MB/s  us sy id\n"
               "   17.34 1233 20.88    49.77    0  0.00  19  9 72\n"
               "    9.36 7391 67.59     8.00   10  1.41  46 43 12\n")
        done = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with mock.patch.object(self.hook.subprocess, "run", return_value=done):
            self.assertEqual(self.hook.host_io(), (69.0, 88.0))
        with mock.patch.object(self.hook.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(self.hook.host_io(), (None, None), "Linux has no BSD iostat")
        garbled = subprocess.CompletedProcess([], 0, stdout="usage: iostat\n", stderr="")
        with mock.patch.object(self.hook.subprocess, "run", return_value=garbled):
            self.assertEqual(self.hook.host_io(), (None, None))

    def test_run_queue_counts_running_and_blocked(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="R+\nS\nSs\nU\nU+\nD\nZ\n", stderr="")
        with mock.patch.object(self.hook.subprocess, "run", return_value=done):
            self.assertEqual(self.hook.run_queue(), (1, 3))

    def test_saturation_is_contention_only_when_shared(self) -> None:
        shared = self.hook.JobSamples({}, 14, 0.0)
        shared.add(40.0, (0.3, 6.6, 1.1, {}, {}), 10.0, (20, 0), 120.0, 98.0)
        record = shared.summary(10.0)
        self.assertEqual(record["verdict"], "contended")
        self.assertIn("98% busy", record["reasons"][0])
        self.assertEqual(record["cpu_busy_pct"], 98.0)
        self.assertEqual(record["disk_mb_s"], {"mean": 120.0, "max": 120.0})

        alone = self.hook.JobSamples({}, 14, 0.0)
        alone.add(40.0, (12.0, 0.5, 0.5, {}, {}), 10.0, (20, 0), 120.0, 99.0)
        self.assertEqual(alone.summary(10.0)["verdict"], "clear", "a job saturating the host by itself is clear")

        waiting = self.hook.JobSamples({}, 14, 0.0)
        waiting.add(9.0, (4.0, 0.0, 0.2, {}, {}), 10.0, (2, 6), 900.0, 40.0)
        quiet = self.hook.JobSamples({}, 14, 0.0)
        quiet.add(3.0, (1.0, 0.0, 0.2, {}, {}), 10.0, (2, 3), 5.0, 20.0)
        self.assertEqual(quiet.summary(10.0)["verdict"], "clear", "an idle Mac shows a few U processes")
        record = waiting.summary(10.0)
        self.assertEqual(record["verdict"], "contended")
        self.assertIn("uninterruptible wait (disk or memory), disks 900 MB/s", record["reasons"][0])

        unknown = self.hook.JobSamples({}, 14, 0.0)
        unknown.add(9.0, (4.0, 0.0, 0.2, {}, {}), 10.0)
        record = unknown.summary(10.0)
        self.assertEqual((record["queue"], record["disk_mb_s"], record["cpu_busy_pct"]), (None, None, None))

    def test_sampler_file_never_looks_like_a_lock_holder(self) -> None:
        name = self.hook.sampler_file(self.dir).name
        self.assertFalse(name.startswith(self.hook.HOLDER_FILE))


if __name__ == "__main__":
    unittest.main()
