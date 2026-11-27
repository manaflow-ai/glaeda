#!/usr/bin/env python3
"""Tests for scripts/glaeda-seed-archive against a real glaeda-seed-serve standing in for the seeder."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVE = ROOT / "scripts/glaeda-seed-serve"


def load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, os.fspath(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


archive = load("glaeda_seed_archive", ROOT / "scripts/glaeda-seed-archive")
pm = archive.pm
PREFIX = "admission-derived-data-v1-macOS-ARM64-" + "a" * 32 + "-j14-"
KEYS = [PREFIX + c * 40 for c in "bcdef"]

# Stands in for the archive host's /usr/bin/ssh to the seeder: runs the seeder's forced command locally.
FAKE_SSH = """import json, os, sys
os.environ["SSH_ORIGINAL_COMMAND"] = sys.argv[-1]
os.environ["SSH_CLIENT"] = "172.20.21.158 50000 22"
args = json.loads(os.environ["FAKE_SERVE_ARGS"])
os.execv(sys.executable, [sys.executable, *args])
"""


@unittest.skipUnless(pm.zstd_tool(), "needs zstd")
class ArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.state = base / "seeder/ci"
        (self.state / "seeds").mkdir(parents=True)
        receipt = base / "seeder/receipt.json"
        receipt.write_text(json.dumps({"member": {"trustedRef": "refs/heads/main"}}))
        self.archive = base / "archive"
        fake = base / "ssh"
        fake.write_text(f"#!{sys.executable}\n" + FAKE_SSH)
        fake.chmod(0o755)
        self.env = dict(os.environ)
        os.environ["FAKE_SERVE_ARGS"] = json.dumps([os.fspath(SERVE), "--state", os.fspath(self.state), "--run-dir",
                                                  os.fspath(base / "seeder/run"), "--receipt", os.fspath(receipt)])
        self.saved = (pm.ssh_command, archive.STATE, archive.GRACE, archive.KEEP_NEWEST)
        pm.ssh_command = lambda config, address, request: [os.fspath(fake), request]
        archive.STATE = base / "state"
        self.config = {"user": "cmux", "addresses": ["172.20.21.202"], "identity": base / "k", "known_hosts": base / "kh"}
        self.saved_config = pm.lan_config
        pm.lan_config = lambda path=None: self.config

    def tearDown(self) -> None:
        pm.ssh_command, archive.STATE, archive.GRACE, archive.KEEP_NEWEST = self.saved
        pm.lan_config = self.saved_config
        os.environ.clear()
        os.environ.update(self.env)
        self.tmp.cleanup()

    def keep(self, key: str, mtime: int) -> None:
        seed = self.state / "seeds" / key
        (seed / "Build").mkdir(parents=True)
        (seed / "Build/x").write_bytes(os.urandom(4096))
        (seed / pm.MANIFEST).write_text("{}")
        os.utime(seed, (mtime, mtime))

    def run_archive(self, apply: bool = True, **kwargs) -> dict:
        kwargs.setdefault("min_free", 0)  # the test machine's own free space is not the archive disk's
        return archive.run(apply, self.archive, mounted=True, **kwargs)

    def test_plan_changes_nothing(self):
        self.keep(KEYS[0], 100)
        record = self.run_archive(apply=False)
        self.assertEqual(record["state"], "planned")
        self.assertFalse(self.archive.exists())

    def test_fill_takes_every_kept_seed_newest_first_then_is_a_no_op(self):
        for n, key in enumerate(KEYS[:3]):
            self.keep(key, 100 + n)
        record = self.run_archive()
        self.assertEqual(record["state"], "applied", record)
        self.assertEqual([f["key"] for f in record["fetched"]], [KEYS[2], KEYS[1], KEYS[0]])
        self.assertTrue(all(f["outcome"] == "fetched" for f in record["fetched"]), record)
        self.assertEqual(sorted(p.name for p in self.archive.iterdir()), sorted(k + ".tar.zst" for k in KEYS[:3]))
        again = self.run_archive()
        self.assertEqual((again["missing"], again["fetched"]), (0, []))

    def test_the_archive_serves_what_the_filler_wrote(self):
        self.keep(KEYS[0], 100)
        self.run_archive()
        env = {**os.environ, "SSH_ORIGINAL_COMMAND": f"seed-v1 tar {KEYS[1]} {KEYS[0]}", "SSH_CLIENT": "172.20.21.192 1 22"}
        proc = subprocess.run([sys.executable, os.fspath(SERVE), "--role", "archive", "--archive", os.fspath(self.archive),
                               "--run-dir", os.fspath(self.archive.parent / "run"), "--receipt",
                               os.fspath(self.archive.parent / "no-receipt")], env=env, capture_output=True, timeout=60)
        head, _, body = proc.stdout.partition(b"\n")
        self.assertEqual((proc.returncode, head.decode()), (0, f"glaeda-seed-serve 1 hit {KEYS[0]} tar"))
        names = tarfile.open(fileobj=io.BytesIO(body)).getnames()
        self.assertIn(f"{KEYS[0]}/{pm.MANIFEST}", names)

    def test_prune_drops_the_oldest_over_budget(self):
        for n, key in enumerate(KEYS):
            self.keep(key, 100 + n)
        self.run_archive()
        for n, key in enumerate(KEYS):  # fetched long ago, oldest first
            os.utime(self.archive / (key + ".tar.zst"), (1000 + n, 1000 + n))
        for key in KEYS:  # the seeder dropped them all; only then may prune take them
            shutil.rmtree(self.state / "seeds" / key)
        archive.KEEP_NEWEST = 2
        size = (self.archive / (KEYS[0] + ".tar.zst")).stat().st_size
        record = self.run_archive(budget=size * 2 + size // 2)
        self.assertEqual([d["key"] for d in record["pruned"]], KEYS[:3])
        self.assertEqual(sorted(p.name[:-8] for p in self.archive.iterdir()), sorted(KEYS[3:]))

    def test_a_full_archive_fetches_nothing_and_spares_the_seeders_seeds(self):
        self.keep(KEYS[0], 100)
        self.run_archive()
        self.keep(KEYS[1], 101)
        os.utime(self.archive / (KEYS[0] + ".tar.zst"), (1000, 1000))
        archive.KEEP_NEWEST = 0
        record = self.run_archive(budget=1)
        self.assertTrue(record["full"])
        self.assertEqual((record["fetched"], record["pruned"]), ([], []))  # KEYS[0] is still on the seeder
        self.assertTrue((self.archive / (KEYS[0] + ".tar.zst")).exists())

    def test_prune_keeps_recently_used_seeds(self):
        self.keep(KEYS[0], 100)
        self.keep(KEYS[1], 101)
        self.run_archive()
        archive.KEEP_NEWEST = 0
        record = self.run_archive(budget=0)
        self.assertEqual(record["pruned"], [])  # both touched within GRACE

    def test_check_rejects_entries_outside_the_key_and_missing_manifests(self):
        def pack(names: list[str]) -> Path:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                for name in names:
                    info = tarfile.TarInfo(name)
                    info.size = 1
                    tar.addfile(info, io.BytesIO(b"x"))
            path = self.archive.parent / "t.tar.zst"
            path.write_bytes(subprocess.run([pm.zstd_tool(), "-q", "-c"], input=buf.getvalue(), capture_output=True,
                                            check=True).stdout)
            return path
        key = KEYS[0]
        self.assertEqual(archive.check(pack([f"{key}/{pm.MANIFEST}", f"{key}/._x"]), key), "")
        self.assertIn("outside", archive.check(pack([f"{key}/{pm.MANIFEST}", "../evil"]), key))
        self.assertIn("outside", archive.check(pack([f"{key}/{pm.MANIFEST}", f"{KEYS[1]}/x"]), key))
        self.assertIn("outside", archive.check(pack([f"{key}/{pm.MANIFEST}", f"{key}/../x"]), key))
        self.assertIn("outside", archive.check(pack([f"{key}/{pm.MANIFEST}", "._other"]), key))
        self.assertEqual(archive.check(pack([f"{key}/{pm.MANIFEST}", f"._{key}"]), key), "")
        self.assertEqual(archive.check(pack([f"{key}/x"]), key), "no seed manifest")

    def test_an_untrusted_seeder_is_an_error(self):
        (self.state.parent / "receipt.json").write_text("{}")
        record = self.run_archive()
        self.assertEqual(record["state"], "error")
        self.assertIn("refused", record["reason"])

    def test_a_seed_that_reads_eio_is_quarantined_not_a_crash(self):
        self.keep(KEYS[0], 100)
        self.keep(KEYS[1], 101)
        self.run_archive()
        bad = self.archive / (KEYS[0] + archive.SUFFIX)
        real = archive.os.scandir

        class Entry:
            def __init__(self, entry): self.entry, self.name = entry, entry.name
            def is_file(self, follow_symlinks=True):
                if self.name == bad.name:
                    raise OSError(5, "Input/output error")
                return self.entry.is_file(follow_symlinks=follow_symlinks)
            def stat(self, follow_symlinks=True): return self.entry.stat(follow_symlinks=follow_symlinks)

        class Listing(list):
            def __enter__(self): return self
            def __exit__(self, *exc): return False

        archive.os.scandir = lambda path: Listing(Entry(e) for e in real(path))
        try:
            record = self.run_archive()
        finally:
            archive.os.scandir = real
        self.assertEqual(record["state"], "applied", record)
        [item] = record["unreadable"]
        self.assertEqual((item["key"], item["quarantined"]), (KEYS[0], True))
        self.assertIn("errno 5", item["error"])
        self.assertTrue((self.archive / f".corrupt-{bad.name}").exists())
        self.assertFalse(archive.quiet(record))
        # The good seed is untouched, and the quarantined one is fetched again in the same run.
        self.assertTrue((self.archive / (KEYS[1] + archive.SUFFIX)).exists())
        self.assertEqual([f["outcome"] for f in record["fetched"]], ["fetched"])

    def eio_on_stat(self, bad: set[str], rename_fails: bool = False):
        """Patch the module's scandir so each entry named in BAD raises EIO from stat() (the 2026-11-27 traceback:
        entry.stat(follow_symlinks=False) on a failing X10 Pro), and optionally its quarantine rename too."""
        real_scandir, real_rename = archive.os.scandir, archive.os.rename

        class Entry:
            def __init__(self, entry): self.entry, self.name, self.path = entry, entry.name, entry.path
            def is_file(self, follow_symlinks=True): return self.entry.is_file(follow_symlinks=follow_symlinks)
            def stat(self, follow_symlinks=True):
                if self.name in bad:
                    raise OSError(5, "Input/output error", self.path)
                return self.entry.stat(follow_symlinks=follow_symlinks)

        class Listing(list):
            def __enter__(self): return self
            def __exit__(self, *exc): return False

        def rename(src, dst):
            if rename_fails and Path(src).name in bad:
                raise OSError(5, "Input/output error", src)
            return real_rename(src, dst)

        def restore():
            archive.os.scandir, archive.os.rename = real_scandir, real_rename

        archive.os.scandir = lambda path: Listing(Entry(e) for e in real_scandir(path))
        archive.os.rename = rename
        self.addCleanup(restore)
        return restore

    def main_once(self) -> dict:
        """main(["--apply"]) against the test archive, its log and state kept in the test's tree."""
        real_run, real_log = archive.run, archive.LOG
        archive.run = lambda apply: real_run(apply, self.archive, mounted=True, min_free=0)
        archive.LOG = Path(self.tmp.name) / "log.jsonl"
        out = io.StringIO()
        real_stdout, sys.stdout = sys.stdout, out
        try:
            archive.main(["--apply"])
        finally:
            sys.stdout, archive.run, archive.LOG = real_stdout, real_run, real_log
        return json.loads(out.getvalue())

    def disk_errors_file(self) -> dict:
        return json.loads((archive.STATE.parent / "disk-errors" / archive.DISK_ERRORS_NAME).read_text())

    def test_a_stat_that_reads_eio_is_skipped_counted_and_reported(self):
        # cmux-lawrence 2026-11-27: entry.stat() raised EIO on 19 of 341 seeds and the job restarted every 15 s.
        for i, key in enumerate(KEYS[:3]):
            self.keep(key, 100 + i)
        self.main_once()
        self.assertEqual(self.disk_errors_file()["unreadable"], 0)
        bad = {KEYS[0] + archive.SUFFIX, KEYS[1] + archive.SUFFIX}
        self.eio_on_stat(bad, rename_fails=True)
        record = self.main_once()
        self.assertEqual(record["state"], "applied", record)
        self.assertEqual(sorted(i["key"] for i in record["unreadable"]), sorted(KEYS[:2]))
        self.assertEqual(record["archive_seeds"], 1)  # the readable seed is still counted and served
        report = self.disk_errors_file()
        self.assertEqual(report["schema"], "glaeda-disk-errors/v1")
        self.assertEqual((report["unreadable"], report["still_unreadable"], report["quarantined"]), (2, 2, 0))
        self.assertIn("errno 5", report["examples"][0])
        self.assertEqual(report["root"], os.fspath(self.archive))

    def test_a_quarantined_seed_still_counts_as_a_disk_error_on_later_runs(self):
        self.keep(KEYS[0], 100)
        self.keep(KEYS[1], 101)
        self.main_once()
        restore = self.eio_on_stat({KEYS[0] + archive.SUFFIX})
        first = self.main_once()
        restore()  # the seed fetched again in place of the quarantined one reads fine
        self.assertTrue(first["unreadable"][0]["quarantined"], first)
        self.assertEqual(self.disk_errors_file()["quarantined"], 1)
        again = self.main_once()  # the bad seed now sits aside as .corrupt-*: not re-read, still reported
        self.assertNotIn("unreadable", again)
        report = self.disk_errors_file()
        self.assertEqual((report["unreadable"], report["quarantined"]), (0, 1))

    def test_a_run_that_did_not_list_the_archive_keeps_the_last_count(self):
        self.keep(KEYS[0], 100)
        self.main_once()
        path = archive.STATE.parent / "disk-errors" / archive.DISK_ERRORS_NAME
        path.write_text(json.dumps({"schema": "glaeda-disk-errors/v1", "unreadable": 7, "at": 1}))
        archive.pm.lan_config = lambda path=None: None  # an error record before any listing
        self.main_once()
        self.assertEqual(json.loads(path.read_text())["unreadable"], 7)

    def test_a_quarantine_that_fails_too_is_reported_and_not_fetched_over(self):
        self.keep(KEYS[0], 100)
        unreadable = {KEYS[0]: "OSError errno 5: Input/output error"}
        [item] = archive.quarantine(self.archive, unreadable, True)  # nothing to rename: fails like a bad entry
        self.assertFalse(item["quarantined"])
        self.assertIn("quarantine_error", item)

    def test_an_eio_anywhere_in_a_run_is_an_error_record(self):
        real = archive.run
        archive.run = lambda apply: (_ for _ in ()).throw(OSError(5, "Input/output error", "/Volumes/x"))
        try:
            out = io.StringIO()
            real_stdout, sys.stdout = sys.stdout, out
            try:
                self.assertEqual(archive.main(["--apply"]), 1)
            finally:
                sys.stdout = real_stdout
        finally:
            archive.run = real
        self.assertEqual(json.loads(out.getvalue())["state"], "error")

    def test_the_job_is_a_system_daemon_for_this_user_and_install_migrates_the_agent(self):
        plist = archive.plist(Path("/Users/x/.local/libexec/glaeda-seed-archive"))
        self.assertEqual(plist["UserName"], archive.pwd.getpwuid(os.getuid()).pw_name)
        self.assertIn("HOME", plist["EnvironmentVariables"])
        self.assertTrue(plist["AbandonProcessGroup"])
        base = Path(self.tmp.name)
        calls = base / "calls"
        sudo = base / "sudo"
        sudo.write_text(f'#!/bin/sh\necho "$@" >>{calls}\n[ "$1" = chown ] && exit 0\nexec "$@"\n')
        sudo.chmod(0o755)
        launchctl = base / "launchctl"
        launchctl.write_text(f'#!/bin/sh\necho "launchctl $@" >>{calls}\n[ "$1" = print ] && exit 113\nexit 0\n')
        launchctl.chmod(0o755)
        home = base / "home"
        (home / "Library/LaunchAgents").mkdir(parents=True)
        agent = home / "Library/LaunchAgents" / f"{archive.LABEL}.plist"
        agent.write_text("old")
        saved = (archive.HOME, archive.DAEMON_DIR, archive.SUDO_PREFIX, archive.LAUNCHCTL, archive.STATE)
        archive.HOME, archive.DAEMON_DIR = home, base / "daemons"
        archive.DAEMON_DIR.mkdir()
        archive.SUDO_PREFIX, archive.LAUNCHCTL, archive.STATE = [os.fspath(sudo)], os.fspath(launchctl), base / "st/x"
        try:
            report = {}
            archive.install_daemon(report, b"want")
            self.assertEqual(report["daemon"], "installed", report)
            self.assertFalse(agent.exists())
            self.assertEqual((archive.DAEMON_DIR / f"{archive.LABEL}.plist").read_bytes(), b"want")
            log = calls.read_text()
            self.assertIn(f"launchctl bootout gui/{os.getuid()}/{archive.LABEL}", log)
            self.assertIn(f"launchctl bootstrap system {archive.DAEMON_DIR}", log)
            # No sudo and no loaded daemon: the root commands, never an agent.
            archive.SUDO_PREFIX = ["/usr/bin/false"]
            (archive.DAEMON_DIR / f"{archive.LABEL}.plist").unlink()
            report = {}
            archive.install_daemon(report, b"want")
            self.assertFalse(report["loaded"])
            self.assertIn("no passwordless sudo", report["error"])
            self.assertFalse(agent.exists())
        finally:
            archive.HOME, archive.DAEMON_DIR, archive.SUDO_PREFIX, archive.LAUNCHCTL, archive.STATE = saved

    def test_the_launch_agent_runs_apples_python(self):
        plist = archive.plist(Path("/Users/x/.local/libexec/glaeda-seed-archive"))
        self.assertEqual(plist["ProgramArguments"][:len(archive.LAN_PARENT) + 2],
                         [*archive.LAN_PARENT, "/usr/bin/python3", "-I"])
        self.assertEqual(plist["ProgramArguments"][0], "/bin/sh")
        # The archive installs itself without glaeda-mini-setup, so it keeps its own copy of the wrapper.
        setup = load("glaeda_mini_setup_for_archive", ROOT / "scripts/glaeda-mini-setup")
        self.assertEqual(archive.LAN_PARENT, setup.LAN_PARENT)
        self.assertEqual(plist["StartInterval"], 15)
        self.assertEqual(plist["StandardOutPath"], "/dev/null")

    def test_only_a_run_that_did_something_is_logged(self):
        self.keep(KEYS[0], 100)
        first = self.run_archive()
        self.assertFalse(archive.quiet(first), first)
        again = self.run_archive()
        self.assertTrue(archive.quiet(again), again)
        self.assertFalse(archive.quiet({**again, "state": "error"}))
        self.assertFalse(archive.quiet({"state": "skipped", "reason": "hdiutil attach timed out"}))


if __name__ == "__main__":
    unittest.main()
