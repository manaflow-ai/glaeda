#!/usr/bin/env python3
"""Dev-box policy contracts: transcript retention, pressure-only caches and build outputs."""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("glaeda_disk_devbox", os.fspath(ROOT / "scripts/glaeda-disk"))
spec = importlib.util.spec_from_loader(loader.name, loader)
gd = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = gd
loader.exec_module(gd)


class DevboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.old_home, self.old_role, self.old_mode = gd.HOME, gd.ROLE, gd.ACTIVITY_MODE
        gd.HOME, gd.ROLE, gd.ACTIVITY_MODE = self.home, "devbox", None
        gd.process_evidence = lambda: ([], "")

    def tearDown(self) -> None:
        gd.HOME, gd.ROLE, gd.ACTIVITY_MODE = self.old_home, self.old_role, self.old_mode
        self.tmp.cleanup()

    def test_transcript_history_is_report_only(self) -> None:
        old = self.home / ".codex/sessions/old.jsonl"
        old.parent.mkdir(parents=True)
        old.write_text('{"cwd":"/old"}\n')
        os.utime(old, (time.time() - 31 * 86400,) * 2)
        new = old.parent / "new.jsonl"
        new.write_text("live\n")
        family = gd.Family("agent-transcripts", self.home / ".codex", False, "preserve history",
                           min_idle_hours=30 * 24, transcripts=True)
        items = gd.survey([family], 24, 0)
        self.assertEqual({Path(i.path).name: i.verdict for i in items},
                         {"old.jsonl": "report-only", "new.jsonl": "report-only"})
        receipt = self.home / "receipt.jsonl"
        with contextlib.redirect_stdout(io.StringIO()):
            gd.apply(items, {family.id: family}, receipt, None, 24)
        archive = self.home / ".codex/archive/sessions/old.jsonl.gz"
        self.assertFalse(archive.exists())
        self.assertTrue(old.exists())
        self.assertTrue(new.exists())

    def test_devbox_package_caches_wait_for_pressure(self) -> None:
        cache = self.home / ".cache/pkg"
        cache.mkdir(parents=True)
        (cache / "blob").write_bytes(b"x")
        family = gd.Family("user-cache", self.home / ".cache", True, "redownload", pressure_only=True)
        with mock.patch.object(gd, "ACTIVITY_MODE", "sweep"):
            item = gd.survey([family], 0, 0)[0]
        self.assertEqual(item.verdict, "kept")
        self.assertIn("pressure-only", item.reasons[0])

    def test_ghostty_and_package_build_caches_are_catalogued(self) -> None:
        checkout = self.home / "repo"
        (checkout / "ghostty/.zig-cache").mkdir(parents=True)
        (checkout / "Packages/macOS/Core/.build").mkdir(parents=True)
        self.assertEqual({p.relative_to(checkout.resolve()).as_posix() for p in gd._checkout_build_dirs(checkout)},
                         {"ghostty/.zig-cache", "Packages/macOS/Core/.build"})

    def test_devbox_checkout_family_only_scans_linked_worktrees(self) -> None:
        projects = self.home / "Projects"
        (projects / "canonical").mkdir(parents=True)
        worktree = projects / "worktrees/repo/feature"
        worktree.mkdir(parents=True)
        families = gd.default_families("devbox")
        family = next(f for f in families if f.id == "devbox-checkouts")
        self.assertFalse(family.reclaimable)
        self.assertEqual(family.root, projects / "worktrees")
        self.assertEqual(gd.candidates(family), [worktree])

    def test_stale_derived_data_is_reclaimable_on_devbox(self) -> None:
        derived = self.home / "Library/Developer/Xcode/DerivedData/old"
        derived.mkdir(parents=True)
        (derived / "build.bin").write_bytes(b"x" * (2 * 1024 * 1024))
        os.utime(derived, (time.time() - 48 * 3600,) * 2)
        family = gd.Family("xcode-derived-data", self.home / "Library/Developer/Xcode/DerivedData", True,
                           "rebuild", build_output=True, max_idle_hours=0.0)
        item = gd.survey([family], 24, 0)[0]
        self.assertEqual(item.verdict, "reclaimable")


if __name__ == "__main__":
    unittest.main()
