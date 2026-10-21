#!/usr/bin/env python3
"""glaeda-cmux-tour-floor-check: the vendored cmux tour floor matches cmux, or CI says so.

glaeda-cmux-runner-hook's CMUX_TOUR_REQUIREMENTS is a copy of manaflow-ai/cmux
scripts/cmux-next/tour-requirements.json at CMUX_TOUR_REQUIREMENTS_PIN. The check reads that file through gh (the
workflow's GITHUB_TOKEN) at the pin and at feat-cmux-next: a different file fails, and a read that fails is a
visible warning, never a silent pass. These run it against a fake gh.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "glaeda-cmux-tour-floor-check"


def load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, os.fspath(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


hook = load("glaeda_cmux_runner_hook_under_test", ROOT / "scripts" / "glaeda-cmux-runner-hook")
FAKE_GH = """#!/bin/sh
# gh api -H ... repos/manaflow-ai/cmux/contents/scripts/cmux-next/tour-requirements.json?ref=REF
for arg; do last=$arg; done
echo "$@" >> "$FAKE_GH_DIR/calls"
ref=${last##*ref=}
[ -f "$FAKE_GH_DIR/$ref.json" ] || { echo "HTTP 404: Not Found" >&2; exit 1; }
cat "$FAKE_GH_DIR/$ref.json"
"""


class TourFloorCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "bin").mkdir()
        gh = self.dir / "bin" / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.pin = hook.CMUX_TOUR_REQUIREMENTS_PIN
        self.vendored = json.dumps(hook.CMUX_TOUR_REQUIREMENTS)

    def serve(self, ref: str, text: str) -> None:
        (self.dir / f"{ref}.json").write_text(text)

    def check(self, *, gh: bool = True) -> subprocess.CompletedProcess:
        # no gh: a PATH with nothing on it (a runner's /usr/bin has the real gh)
        path = os.fspath(self.dir / "bin") + os.pathsep + "/usr/bin:/bin" if gh else os.fspath(self.dir / "empty")
        return subprocess.run([sys.executable, os.fspath(CHECK)], capture_output=True, text=True, timeout=60,
                              env={"PATH": path, "FAKE_GH_DIR": os.fspath(self.dir), "HOME": os.fspath(self.dir)})

    def test_the_vendored_copy_holds_only_a_whole_number_and_a_commit(self) -> None:
        self.assertEqual(set(hook.CMUX_TOUR_REQUIREMENTS), {"min_free_gib"})
        floor = hook.CMUX_TOUR_REQUIREMENTS["min_free_gib"]
        self.assertTrue(isinstance(floor, int) and not isinstance(floor, bool) and floor > 0)
        self.assertRegex(self.pin, r"^[0-9a-f]{40}$")

    def test_a_match_at_the_pin_and_the_branch_passes(self) -> None:
        self.serve(self.pin, '{\n  "min_free_gib": %d\n}\n' % hook.CMUX_TOUR_REQUIREMENTS["min_free_gib"])
        self.serve("feat-cmux-next", self.vendored)
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("::warning::", result.stdout)
        calls = (self.dir / "calls").read_text()
        self.assertIn("repos/manaflow-ai/cmux/contents/scripts/cmux-next/tour-requirements.json?ref=feat-cmux-next",
                      calls)
        self.assertIn(f"?ref={self.pin}", calls)

    def test_a_different_floor_at_either_ref_fails(self) -> None:
        other = json.dumps({"min_free_gib": hook.CMUX_TOUR_REQUIREMENTS["min_free_gib"] + 5})
        for changed in (self.pin, "feat-cmux-next"):
            with self.subTest(changed=changed):
                for ref in (self.pin, "feat-cmux-next"):
                    self.serve(ref, other if ref == changed else self.vendored)
                result = self.check()
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("::error::", result.stdout)
                self.assertIn(changed, result.stdout)

    def test_an_unreadable_file_at_cmux_fails(self) -> None:
        self.serve(self.pin, self.vendored)
        self.serve("feat-cmux-next", "{")
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("::error::", result.stdout)

    def test_a_fetch_that_fails_warns_and_never_passes_silently(self) -> None:
        self.serve(self.pin, self.vendored)  # feat-cmux-next is a 404
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("::warning::", result.stdout)
        self.assertIn("feat-cmux-next", result.stdout)
        result = self.check(gh=False)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.count("::warning::"), 2, result.stdout)


if __name__ == "__main__":
    unittest.main()
