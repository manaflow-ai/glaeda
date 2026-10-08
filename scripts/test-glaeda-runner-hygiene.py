#!/usr/bin/env python3
"""Focused contract tests for glaeda-runner-hygiene."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import errno
import json
import os
import plistlib
import pwd
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("glaeda_runner_hygiene", os.fspath(ROOT / "scripts" / "glaeda-runner-hygiene"))
spec = importlib.util.spec_from_loader(loader.name, loader)
rh = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rh
loader.exec_module(rh)


class RunnerHygieneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        # The real account is used only for its validated uid/name; its home is
        # patched to the isolated real directory so planning cannot touch it.
        self.entry = pwd.getpwuid(os.getuid())
        self.source = self.base / "glaeda-disk"
        self.source.write_text("#!/usr/bin/env python3\nprint('disk')\n")
        self.source.chmod(0o555)
        self.root = self.base / "runner-hygiene"
        self.daemons = self.base / "LaunchDaemons"
        self.daemons.mkdir()
        self.launchctl = Path("/usr/bin/true")
        self.account = rh.Account(self.entry.pw_name, max(self.entry.pw_uid, 500), self.home)
        self.install = rh.Install(self.account, Path("/usr/bin/python3"), self.source,
                                  self.root, self.root / str(self.account.uid),
                                  self.root / str(self.account.uid) / rh.DISK_NAME,
                                  self.root / str(self.account.uid) / rh.MANIFEST_NAME,
                                  self.daemons / f"{rh.LABEL_PREFIX}.{self.account.uid}.plist",
                                  f"{rh.LABEL_PREFIX}.{self.account.uid}", self.launchctl)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_profile_is_user_scoped_and_every_argument_is_pinned(self) -> None:
        plist = rh.plist_for(self.install)
        self.assertEqual(plist["UserName"], self.account.name)
        self.assertEqual(plist["EnvironmentVariables"], {"HOME": str(self.home)})
        self.assertEqual(plist["StartInterval"], 900)
        self.assertEqual(plist["ProgramArguments"], [
            "/usr/bin/python3", "-I", str(self.install.snapshot),
            "--runner-caches", "--pressure", "--idle", "--apply", "--top", "0",
        ])
        self.assertEqual(plist["StandardOutPath"], "/dev/null")
        self.assertEqual(plist["StandardErrorPath"], "/dev/null")

    def test_plan_has_no_side_effects(self) -> None:
        before = sorted(self.base.rglob("*"))
        with mock.patch.object(rh, "service_loaded", return_value=False):
            result = rh.plan(self.install)
        self.assertEqual(result["state"], "planned")
        self.assertEqual(before, sorted(self.base.rglob("*")))
        self.assertFalse(self.install.snapshot.exists())
        self.assertFalse(self.install.plist.exists())

    def test_uninstall_plan_does_not_require_the_source_checkout(self) -> None:
        self.source.unlink()
        install = rh.build_install(self.account.name, None, None, self.root, self.daemons,
                                   self.launchctl, uninstall=True)
        result = rh.plan(install, uninstall=True)
        self.assertEqual(result["operation"], "uninstall")
        self.assertIsNone(result["plist"])

    def test_unrelated_plist_is_refused_for_plan_and_uninstall(self) -> None:
        self.install.plist.write_bytes(plistlib.dumps({"Label": self.install.label, "ProgramArguments": ["other"]}))
        with self.assertRaisesRegex(rh.HygieneError, "not a glaeda-runner-hygiene"):
            rh.plan(self.install)
        with self.assertRaisesRegex(rh.HygieneError, "not a glaeda-runner-hygiene"):
            rh.plan(self.install, uninstall=True)

    def test_orphan_managed_plist_is_not_removed(self) -> None:
        with mock.patch.object(rh, "_managed_plist", return_value=(True, "managed")), \
                mock.patch.object(rh, "_managed_install", return_value=(False, "missing")):
            with self.assertRaisesRegex(rh.HygieneError, "matching install manifest"):
                rh.plan(self.install, uninstall=True)

    def test_unrelated_install_contents_are_refused(self) -> None:
        self.install.install_dir.mkdir(parents=True)
        (self.install.install_dir / "operator-file").write_text("keep")
        with self.assertRaisesRegex(rh.HygieneError, "root-owned and protected|unrelated files"):
            rh.plan(self.install)

    def test_home_symlink_is_refused(self) -> None:
        link = self.base / "home-link"
        link.symlink_to(self.home, target_is_directory=True)
        entry = SimpleNamespace(pw_name=self.entry.pw_name, pw_uid=self.entry.pw_uid,
                                pw_dir=os.fspath(link))
        with mock.patch.object(rh.pwd, "getpwnam", return_value=entry):
            with self.assertRaisesRegex(rh.HygieneError, "real directory"):
                rh.build_install(entry.pw_name, self.source, Path("/usr/bin/python3"), self.root,
                                 self.daemons, self.launchctl)

    def test_manifest_records_source_digest(self) -> None:
        # This exercises the receipt shape without invoking launchctl or root effects.
        digest = rh._sha256(self.source)
        self.assertEqual(len(digest), 64)
        self.install.install_dir.mkdir(parents=True)
        manifest = {"version": 1, "uid": self.account.uid, "user": self.account.name,
                    "home": str(self.home), "device": self.install.install_dir.stat().st_dev,
                    "sha256": digest, "snapshot": str(self.install.snapshot)}
        self.install.manifest.write_text(json.dumps(manifest))
        self.install.snapshot.write_bytes(self.source.read_bytes())
        self.assertEqual(json.loads(self.install.manifest.read_text())["sha256"], digest)

    def test_atomic_write_uses_os_directory_fsync_on_python39(self) -> None:
        target = self.base / "atomic" / "snapshot"
        with mock.patch.object(rh.Path, "open", side_effect=IsADirectoryError), \
                mock.patch.object(rh.os, "fsync", wraps=rh.os.fsync) as fsync:
            rh._atomic_write(target, b"snapshot", 0o644, os.getuid(), os.getgid())
        self.assertEqual(target.read_bytes(), b"snapshot")
        self.assertGreaterEqual(fsync.call_count, 2)  # file, then parent directory

    def test_directory_fsync_falls_back_when_directory_flag_is_rejected(self) -> None:
        with mock.patch.object(rh.os, "open", side_effect=[OSError(errno.EINVAL, "directory"), 41]), \
                mock.patch.object(rh.os, "fsync") as fsync, \
                mock.patch.object(rh.os, "close") as close:
            rh._fsync_directory(self.base)
        self.assertEqual(fsync.call_args.args, (41,))
        close.assert_called_once_with(41)


if __name__ == "__main__":
    unittest.main()
