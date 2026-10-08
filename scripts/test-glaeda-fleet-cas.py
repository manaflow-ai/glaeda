#!/usr/bin/env python3
"""Tests for the fleet-cas installer preflight."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "glaeda-fleet-cas"


def preflight_body() -> str:
    """Extract the side-effect-free check_upstream function from the installer."""
    text = INSTALLER.read_text(encoding="utf-8")
    start = text.index("check_upstream() {")
    end = text.index("\n}\n\n# The plist", start) + 2
    return text[start:end]


class UpstreamPreflightTest(unittest.TestCase):
    def run_check(self, endpoint: str, nc_exit: int = 0) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            (bin_dir / "nc").write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >\"$NC_ARGS\"\n"
                f"exit {nc_exit}\n",
                encoding="utf-8",
            )
            (bin_dir / "nc").chmod(0o755)
            args_file = Path(td) / "nc.args"
            command = f"{preflight_body()}\ncheck_upstream \"$1\""
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "NC_ARGS": str(args_file)}
            return subprocess.run(
                ["bash", "-c", command, "fleet-cas-test", endpoint],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_reachable_store_is_checked_with_bounded_probe(self) -> None:
        result = self.run_check("100.89.140.13:7450")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unreachable_store_refuses_apply(self) -> None:
        result = self.run_check("100.89.140.13:7450", nc_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not reachable; refusing to apply", result.stderr)

    def test_malformed_endpoint_is_rejected_before_probe(self) -> None:
        result = self.run_check("100.89.140.13:7450:extra")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("want HOST:PORT", result.stderr)


if __name__ == "__main__":
    unittest.main()
