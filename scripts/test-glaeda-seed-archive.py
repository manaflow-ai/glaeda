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
            def __init__(self, entry): self.entry, self.name, self.path = entry, entry.name, entry.path
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
            def __getattr__(self, attr): return getattr(self.entry, attr)  # is_dir, inode, ... as the real one
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

        def scandir(path="."):
            # archive.os is the os module itself: every other caller (tearDown's rmtree, which passes an fd) gets
            # the real listing, and the cleanup that restores it runs only after tearDown
            if not isinstance(path, (str, os.PathLike)) or Path(path) != self.archive:
                return real_scandir(path)
            return Listing(Entry(e) for e in real_scandir(path))

        archive.os.scandir = scandir
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

    def test_an_entry_that_stays_unreadable_is_not_read_again(self):
        # 2026-11-27, cmux-lawrence: the X10 Pro under the archive image failed reads; several seeds read EIO and
        # their quarantine rename read EIO too. Every 15 s run read each bad entry three times (the fill's listing,
        # prune's and the count's), each read 20 kernel retries: about 5,000 kernel I/O error lines a minute.
        # An entry that stays unreadable is recorded and never read again.
        self.keep(KEYS[0], 100)
        self.keep(KEYS[1], 101)
        self.run_archive()
        bad = KEYS[0] + archive.SUFFIX
        real_scandir, real_rename = archive.os.scandir, archive.os.rename
        reads = []

        class Entry:
            def __init__(self, entry): self.entry, self.name, self.path = entry, entry.name, entry.path
            def is_file(self, follow_symlinks=True):
                if self.name == bad:
                    reads.append(self.name)
                    raise OSError(5, "Input/output error")
                return self.entry.is_file(follow_symlinks=follow_symlinks)
            def stat(self, follow_symlinks=True): return self.entry.stat(follow_symlinks=follow_symlinks)

        class Listing(list):
            def __enter__(self): return self
            def __exit__(self, *exc): return False

        def rename(src, dst):
            if Path(src).name == bad:
                reads.append("rename")
                raise OSError(5, "Input/output error")
            return real_rename(src, dst)

        archive.os.scandir = lambda path: Listing(Entry(e) for e in real_scandir(path))
        archive.os.rename = rename
        try:
            first = self.run_archive()
            after_first = len(reads)
            second = self.run_archive()
        finally:
            archive.os.scandir, archive.os.rename = real_scandir, real_rename
        self.assertEqual(first["state"], "applied", first)
        self.assertGreater(after_first, 0)
        self.assertEqual(reads[after_first:], [], "the second run read the known-bad entry again")
        self.assertEqual(second["state"], "applied", second)
        self.assertEqual(second.get("known_unreadable"), [KEYS[0]])
        self.assertEqual(second["fetched"], [])  # never fetched over it

    def test_a_known_unreadable_key_the_archive_no_longer_lists_is_dropped_and_fetched_again(self):
        # cmux-lawrence 2026-11-27: the archive moved from the failing X10 Pro to a copy on the T5 EVO without the
        # keys the X10 copy had read EIO, and unreadable.json kept all 20, so the fill never fetched them again.
        # A known key the listing does not name is gone from this disk: it is dropped and fetched like any other.
        self.keep(KEYS[0], 100)
        self.keep(KEYS[1], 101)
        self.run_archive()
        (self.archive / (KEYS[0] + archive.SUFFIX)).unlink()  # the new disk never had it
        archive.remember_unreadable({KEYS[0], KEYS[1]})  # KEYS[1] is still on this disk: stays known
        plan = self.run_archive(apply=False)
        self.assertEqual(plan.get("unreadable_dropped"), [KEYS[0]], plan)
        self.assertEqual(archive.known_unreadable(), {KEYS[0], KEYS[1]})  # a plan writes nothing
        record = self.run_archive()
        self.assertEqual(record["state"], "applied", record)
        self.assertEqual(record.get("unreadable_dropped"), [KEYS[0]])
        self.assertEqual(record.get("known_unreadable"), [KEYS[1]])
        self.assertEqual([f["key"] for f in record["fetched"]], [KEYS[0]])
        self.assertEqual(archive.known_unreadable(), {KEYS[1]})

    def test_a_failed_listing_drops_no_known_unreadable_key(self):
        self.keep(KEYS[0], 100)
        self.run_archive()
        archive.remember_unreadable({KEYS[0]})
        real_scandir = archive.os.scandir

        def scandir(path):
            raise OSError(5, "Input/output error")

        archive.os.scandir = scandir
        try:
            record = self.run_archive()
        finally:
            archive.os.scandir = real_scandir
        self.assertNotIn("unreadable_dropped", record)
        self.assertEqual(archive.known_unreadable(), {KEYS[0]})

    def test_the_server_skips_known_unreadable_seeds_and_lists_the_rest(self):
        # 2026-11-27: one seed that read EIO emptied the archive's whole seed-list-v1 answer (the listing's
        # comprehension raised), and every mini's ask re-read the bad entry: 20 kernel retries each. The server
        # leaves the archive's known-unreadable keys alone and lists every other seed.
        serve = load("glaeda_seed_serve_for_test", SERVE)
        self.keep(KEYS[0], 100)
        self.keep(KEYS[1], 101)
        self.keep(KEYS[2], 102)
        self.run_archive()
        known, odd = KEYS[0] + archive.SUFFIX, KEYS[2] + archive.SUFFIX
        archive.remember_unreadable({KEYS[0]})
        serve.KNOWN_UNREADABLE = archive.STATE / "unreadable.json"
        touched = []
        real_scandir, real_open = serve.os.scandir, serve.os.open

        class Entry:
            def __init__(self, entry): self.entry, self.name, self.path = entry, entry.name, entry.path
            def is_file(self, follow_symlinks=True):
                touched.append(self.name)
                if self.name in (known, odd):
                    raise OSError(5, "Input/output error")
                return self.entry.is_file(follow_symlinks=follow_symlinks)
            def stat(self, follow_symlinks=True): return self.entry.stat(follow_symlinks=follow_symlinks)

        def opener(path, flags, mode=0o777, *, dir_fd=None):
            touched.append(os.fspath(path))
            return real_open(path, flags, mode, dir_fd=dir_fd)

        serve.os.scandir = lambda path: [Entry(e) for e in real_scandir(path)]
        serve.os.open = opener
        try:
            listed = [key for key, _ in serve.listing("archive", Path("/nonexistent"), self.archive)]
            found = serve.archive_find(self.archive, [KEYS[0], KEYS[1]])
        finally:
            serve.os.scandir, serve.os.open = real_scandir, real_open
        self.assertEqual(listed, [KEYS[1]])  # the odd EIO one is left out too, not the whole answer
        self.assertNotIn(known, touched)
        self.assertIsNotNone(found)
        fd, key = found
        os.close(fd)
        self.assertEqual(key, KEYS[1])

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

    def test_the_archive_image_lives_on_the_t5_not_the_failing_x10(self):
        # 2026-11-27, cmux-lawrence: the X10 Pro failed reads at more LBAs each hour (1,877 final errors in
        # ten minutes at the worst), several under this image. The archive moved to the T5 EVO; the X10 is
        # only the first version's old folder, which install still empties.
        self.assertEqual(archive.HOST_VOLUME, Path("/Volumes/T5 EVO"))
        self.assertEqual(archive.IMAGE, Path("/Volumes/T5 EVO/glaeda-seed-archive.sparsebundle"))
        self.assertEqual(archive.MOUNT, Path("/Volumes/glaeda-seed-archive"))
        self.assertEqual(archive.OLD_ARCHIVE, Path("/Volumes/X10 Pro/glaeda-seed-archive"))

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


    # The archive host, 2026-11-27: a run walked the bare /Volumes/glaeda-seed-archive (0 seeds, the boot disk's free
    # space, "full"), the next logged "hdiutil attach: " with no exit code, and NOW read a stale count as a dying drive.
    def unmounted_main(self, attach: str = "", lose_at: str = "") -> tuple[dict, int]:
        """main(["--apply"]) with MOUNT faked: ATTACH is attach()'s answer; LOSE_AT, a step at which the mount goes."""
        real = (archive.run, archive.LOG, archive.attach, archive.mount_lost, archive.need_mount)
        gone = [False]

        def need(step: str) -> None:
            if lose_at and step.startswith(lose_at):
                gone[0] = True
            real[4](step)

        archive.attach = lambda: attach
        archive.mount_lost = lambda: "/Volumes/glaeda-seed-archive is not a mount point" if gone[0] else ""
        archive.need_mount = need
        archive.run = lambda apply: real[0](apply, self.archive, min_free=0)
        archive.LOG = Path(self.tmp.name) / "log.jsonl"
        out = io.StringIO()
        real_stdout, sys.stdout = sys.stdout, out
        try:
            code = archive.main(["--apply"])
        finally:
            sys.stdout = real_stdout
            archive.run, archive.LOG, archive.attach, archive.mount_lost, archive.need_mount = real
        return json.loads(out.getvalue()), code

    def test_a_failed_attach_is_unmounted_writes_nothing_and_reports_unknown(self):
        self.keep(KEYS[0], 100)
        self.main_once()
        path = archive.STATE.parent / "disk-errors" / archive.DISK_ERRORS_NAME
        path.write_text(json.dumps({"schema": "glaeda-disk-errors/v1", "unreadable": 20, "at": 1}))
        shutil.rmtree(self.archive)
        record, code = self.unmounted_main(attach="hdiutil attach exit 1, /Volumes/glaeda-seed-archive still not "
                                                  "a mount point: no output")
        self.assertEqual((record["state"], code), ("unmounted", 1), record)
        self.assertEqual(record["mount"], "/Volumes/glaeda-seed-archive")
        self.assertIn("exit 1", record["reason"])
        self.assertFalse(self.archive.exists())  # not even the archive directory under the bare mountpoint
        report = json.loads(path.read_text())
        self.assertEqual(report["state"], "unmounted")
        self.assertIsNone(report["unreadable"])  # unknown: not zero, not the stale 20
        self.assertIsNone(report["quarantined"])
        self.assertIn("exit 1", report["reason"])

    def test_a_mount_lost_mid_run_keeps_nothing_it_read_and_fetches_nothing(self):
        for i, key in enumerate(KEYS[:3]):
            self.keep(key, 100 + i)
        self.main_once()  # fills KEYS[:3]
        archive.remember_unreadable({KEYS[4]})  # a key a mounted listing would drop as gone (not listed)
        for key in KEYS[3:4]:
            self.keep(key, 200)
        record, code = self.unmounted_main(lose_at="using the archive listing")
        self.assertEqual((record["state"], code), ("unmounted", 1), record)
        self.assertIn("before using the archive listing", record["reason"])
        for k in ("fetched", "pruned", "archive_seeds", "disk_free_bytes", "full", "quarantined_aside"):
            self.assertNotIn(k, record)
        self.assertFalse((self.archive / (KEYS[3] + archive.SUFFIX)).exists())
        self.assertEqual(archive.known_unreadable(), {KEYS[4]})  # an empty bare listing drops no known key
        self.assertIsNone(self.disk_errors_file()["unreadable"])

    def test_a_mount_lost_before_a_fetch_stops_the_fill(self):
        self.keep(KEYS[0], 100)
        record, code = self.unmounted_main(lose_at="fetching")
        self.assertEqual(record["state"], "unmounted", record)
        self.assertEqual(list(self.archive.iterdir()), [])

    def test_attach_names_hdiutils_exit_code_when_stderr_is_empty(self):
        tmp = Path(self.tmp.name)
        fake = tmp / "hdiutil"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
        (tmp / "image").mkdir()
        saved = (archive.HDIUTIL, archive.IMAGE, archive.MOUNT)
        archive.HDIUTIL, archive.IMAGE, archive.MOUNT = os.fspath(fake), tmp / "image", tmp / "mnt"
        try:
            why = archive.attach()
        finally:
            archive.HDIUTIL, archive.IMAGE, archive.MOUNT = saved
        self.assertIn("hdiutil attach exit 1", why)
        self.assertIn("no output", why)
        self.assertEqual(archive.disk_errors({"state": "applied", "time": 1})["state"], "counted")

if __name__ == "__main__":
    unittest.main()
