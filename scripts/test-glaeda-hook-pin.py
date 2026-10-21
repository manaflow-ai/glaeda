#!/usr/bin/env python3
"""Tests for glaeda_hook_pin.py: the runner hook's sha is appended to glaeda-disk's drain allowlist, and nothing
else changes."""
from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT / "scripts"))
import glaeda_hook_pin as hp  # noqa: E402

OLD = "a" * 64
DISK = f'''X = 1
CMUX_NEXT_DRAIN_HOOK_SHA256 = frozenset({{
    "{OLD}",
}})
Y = frozenset({{
    "{"c" * 64}",
}})
'''


class PinTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hook, self.disk = Path(tmp.name) / "hook", Path(tmp.name) / "glaeda-disk"
        self.hook.write_bytes(b"#!/usr/bin/env python3\nprint('hook')\n")
        self.sha = hashlib.sha256(self.hook.read_bytes()).hexdigest()
        self.disk.write_text(DISK)
        self.disk.chmod(0o755)
        patch = mock.patch.object(hp, "released", lambda base="origin/main": {OLD})
        patch.start()
        self.addCleanup(patch.stop)

    def test_appends_only_the_hook_sha_and_keeps_earlier_pins(self) -> None:
        self.assertIsNone(hp.ensure(check=False, hook=self.hook, disk=self.disk))
        text = self.disk.read_text()
        self.assertEqual(hp.pinned(text), [OLD, self.sha])
        self.assertEqual(text, DISK.replace(f'    "{OLD}",\n', f'    "{OLD}",\n    "{self.sha}",\n', 1))
        self.assertIsNone(hp.ensure(check=False, hook=self.hook, disk=self.disk))
        self.assertEqual(self.disk.read_text(), text)  # idempotent

    def test_a_draft_pin_is_replaced_not_kept(self) -> None:
        """Editing the hook twice before merging must leave one new pin: the first draft's sha was never released."""
        self.assertIsNone(hp.ensure(check=False, hook=self.hook, disk=self.disk))
        draft = self.sha
        self.hook.write_bytes(b"#!/usr/bin/env python3\nprint('hook v2')\n")
        sha = hashlib.sha256(self.hook.read_bytes()).hexdigest()
        self.assertIsNone(hp.ensure(check=False, hook=self.hook, disk=self.disk))
        self.assertEqual(hp.pinned(self.disk.read_text()), [OLD, sha])
        self.assertNotIn(draft, self.disk.read_text())
        self.assertEqual(self.disk.stat().st_mode & 0o777, 0o755)  # replaced whole, still executable
        self.assertEqual(sorted(p.name for p in self.disk.parent.iterdir()), ["glaeda-disk", "hook"])  # no temp left

    def test_without_origin_main_it_only_appends(self) -> None:
        self.disk.write_text(DISK.replace(f'    "{OLD}",\n', f'    "{OLD}",\n    "{"b" * 64}",\n', 1))
        with mock.patch.object(hp, "released", lambda base="origin/main": None):
            self.assertIsNone(hp.ensure(check=False, hook=self.hook, disk=self.disk))
        self.assertEqual(hp.pinned(self.disk.read_text()), [OLD, "b" * 64, self.sha])

    def test_released_reads_main_pins_and_every_hook_version(self) -> None:
        mock.patch.stopall()
        shas = hp.released()
        if shas is None:
            self.skipTest("no origin/main in this checkout")
        self.assertIn(hp.hook_sha(), shas | set(hp.pinned(hp.DISK.read_text())))
        self.assertGreater(len(shas), 10)

    def test_check_names_the_sha_and_writes_nothing(self) -> None:
        problem = hp.ensure(check=True, hook=self.hook, disk=self.disk)
        self.assertIn(self.sha, problem)
        self.assertIn(hp.COMMAND, problem)
        self.assertEqual(self.disk.read_text(), DISK)

    def test_a_reshaped_block_is_an_error_not_a_guess(self) -> None:
        self.disk.write_text(DISK.replace("frozenset({\n", "frozenset({  # pins\n", 1))
        with self.assertRaises(ValueError):
            hp.ensure(check=False, hook=self.hook, disk=self.disk)

    def test_cli(self) -> None:
        with mock.patch.object(hp, "HOOK", self.hook), mock.patch.object(hp, "DISK", self.disk), \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(hp.main(["--check"]), 1)
            self.assertIn(self.sha, err.getvalue())
            self.assertEqual(hp.main([]), 0)
            self.assertEqual(hp.main(["--check"]), 0)
            self.assertEqual(hp.main(["--bogus"]), 2)

    def test_the_repo_block_parses(self) -> None:
        pins = hp.pinned(hp.DISK.read_text())
        self.assertGreater(len(pins), 10)
        self.assertEqual(len(pins), len(set(pins)))


if __name__ == "__main__":
    unittest.main()
