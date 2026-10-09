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
RUNNER = ROOT / "tools" / "fleet-cas-prototype" / "scripts" / "fleet-cas-run"
MOUNT_HELPER = ROOT / "tools" / "fleet-cas-prototype" / "scripts" / "fleet-cas-mount"


def preflight_body() -> str:
    """Extract the side-effect-free check_upstream function from the installer."""
    text = INSTALLER.read_text(encoding="utf-8")
    start = text.index("check_upstream() {")
    end = text.index("\n}\n\n# The plist", start) + 2
    return text[start:end]


def volume_guard_body() -> str:
    """Extract the pure custom-volume validator from the launchd wrapper."""
    text = RUNNER.read_text(encoding="utf-8")
    start = text.index("validate_custom_store_volume() {")
    end = text.index("\n}\n\ncheck_custom_store_root", start) + 2
    return text[start:end]


def mount_validation_body() -> str:
    """Extract the pure APFS/UUID validation from the sparsebundle helper."""
    text = MOUNT_HELPER.read_text(encoding="utf-8")
    start = text.index("info_value() {")
    end = text.index("\nread_root_info()", start)
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


class CustomStoreVolumeTest(unittest.TestCase):
    def run_check(
        self,
        root: str,
        filesystem: str,
        mount_point: str,
        volume_uuid: str,
        expected_uuid: str,
    ) -> subprocess.CompletedProcess[str]:
        command = f'{volume_guard_body()}\nvalidate_custom_store_volume "$@"'
        return subprocess.run(
            ["bash", "-c", command, "fleet-cas-test", root, filesystem, mount_point, volume_uuid, expected_uuid],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_accepts_expected_apfs_volume_and_child_root(self) -> None:
        result = self.run_check(
            "/Volumes/compiler-cas/store",
            "apfs",
            "/Volumes/compiler-cas",
            "ABC-123",
            "ABC-123",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_non_apfs_volume(self) -> None:
        result = self.run_check("/Volumes/compiler-cas", "exfat", "/Volumes/compiler-cas", "ABC", "ABC")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be APFS", result.stderr)

    def test_rejects_boot_volume_mount(self) -> None:
        result = self.run_check("/Volumes/compiler-cas", "apfs", "/", "ABC", "ABC")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mounted /Volumes", result.stderr)

    def test_rejects_root_outside_reported_mount(self) -> None:
        result = self.run_check("/Volumes/other/store", "apfs", "/Volumes/compiler-cas", "ABC", "ABC")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside its mounted volume", result.stderr)

    def test_rejects_replacement_volume_uuid(self) -> None:
        result = self.run_check("/Volumes/compiler-cas", "apfs", "/Volumes/compiler-cas", "NEW", "OLD")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("volume UUID changed", result.stderr)

    def test_requires_recorded_volume_uuid(self) -> None:
        result = self.run_check("/Volumes/compiler-cas", "apfs", "/Volumes/compiler-cas", "ABC", "")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VOLUME_UUID is missing", result.stderr)


class SparsebundleMountContractTest(unittest.TestCase):
    def run_check(
        self,
        root: str,
        filesystem: str,
        mount_point: str,
        volume_uuid: str,
        expected_uuid: str,
    ) -> subprocess.CompletedProcess[str]:
        info = (
            f"Type (Bundle): {filesystem}\n"
            f"Mount Point: {mount_point}\n"
            f"Volume UUID: {volume_uuid}\n"
        )
        command = (
            'fail() { echo "mount: $*" >&2; return 1; }\n'
            f'root="$1"; expected_uuid="$3"; {mount_validation_body()}\n'
            'validate_root "$2"'
        )
        return subprocess.run(
            ["bash", "-c", command, "fleet-cas-test", root, info, expected_uuid],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_accepts_expected_attached_apfs_volume(self) -> None:
        result = self.run_check("/Volumes/compiler-cas", "apfs", "/Volumes/compiler-cas", "ABC", "ABC")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_wrong_uuid(self) -> None:
        result = self.run_check("/Volumes/compiler-cas", "apfs", "/Volumes/compiler-cas", "NEW", "OLD")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UUID changed", result.stderr)

    def test_rejects_non_apfs_or_wrong_mount(self) -> None:
        for filesystem, mount_point, expected in (
            ("exfat", "/Volumes/compiler-cas", "ABC"),
            ("apfs", "/Volumes/other", "ABC"),
        ):
            result = self.run_check("/Volumes/compiler-cas", filesystem, mount_point, "ABC", expected)
            self.assertNotEqual(result.returncode, 0)

    def test_helper_is_bounded_and_never_formats_or_partitions(self) -> None:
        text = MOUNT_HELPER.read_text(encoding="utf-8")
        self.assertEqual(text.count('"$hdiutil_bin" attach'), 1)
        self.assertIn("for attempt in 1 2 3", text)
        self.assertIn('"$hdiutil_bin" detach "$root"', text)
        self.assertIn("sparsebundle symlinks are refused", text)
        self.assertNotIn("hdiutil create", text)
        self.assertNotIn("hdiutil partition", text)


if __name__ == "__main__":
    unittest.main()
