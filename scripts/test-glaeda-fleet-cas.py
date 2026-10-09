#!/usr/bin/env python3
"""Tests for the fleet-cas installer preflight."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "glaeda-fleet-cas"
RUNNER = ROOT / "tools" / "fleet-cas-prototype" / "scripts" / "fleet-cas-run"
MOUNT_HELPER = ROOT / "tools" / "fleet-cas-prototype" / "scripts" / "fleet-cas-mount"
ROLLOUT = ROOT / "scripts" / "glaeda-fleet-cas-rollout"


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
    info_end = text.index("\n}\n\nimage_volume", start) + 2
    validate = text.index("validate_root() {", start)
    end = text.index("\n}\n\nread_root_info", validate) + 2
    return text[start:info_end] + "\n" + text[validate:end]


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
        result = self.run_check("/Volumes/compiler-cas", "APFS", "/Volumes/compiler-cas", "ABC", "ABC")
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
        self.assertEqual(text.count('run_hdiutil attach'), 1)
        self.assertIn("HDIUTIL_TIMEOUT_SECONDS", text)
        self.assertIn("for attempt in 1 2 3", text)
        self.assertIn("run_hdiutil detach", text)
        self.assertIn("sparsebundle symlinks are refused", text)
        self.assertNotIn("hdiutil create", text)
        self.assertNotIn("hdiutil partition", text)


class RolloutPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.calls = self.directory / "calls.jsonl"
        self.effects = self.directory / "remote-effects"
        stub_bin = self.directory / "bin"
        stub_bin.mkdir()
        stub = (
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "tool, args = Path(sys.argv[0]).name, sys.argv[1:]\n"
            "with open(os.environ['ROLLOUT_TEST_CALLS'], 'a') as stream:\n"
            "    stream.write(json.dumps([tool, args]) + '\\n')\n"
            "if tool == 'ssh' and args[0] == '-G':\n"
            "    print('hostname ' + args[1])\n"
            "elif tool == 'ssh' and args[-1].startswith('ifconfig | awk '):\n"
            "    print('100.64.0.11' if args[-2] == 'writer' else '100.64.0.10')\n"
            "else:\n"
            "    Path(os.environ['ROLLOUT_TEST_EFFECTS']).touch()\n"
        )
        for name in ("ssh", "rsync"):
            path = stub_bin / name
            path.write_text(stub, encoding="utf-8")
            path.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{stub_bin}:{os.environ['PATH']}",
            "ROLLOUT_TEST_CALLS": str(self.calls),
            "ROLLOUT_TEST_EFFECTS": str(self.effects),
        }

    def run_rollout(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(ROLLOUT), *args], env=self.env,
            text=True, capture_output=True, timeout=20, check=False,
        )

    def recorded_calls(self) -> list:
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def assert_read_only(self) -> None:
        self.assertFalse(self.effects.exists(), self.recorded_calls())
        for tool, args in self.recorded_calls():
            self.assertEqual(tool, "ssh")
            self.assertTrue(args[0] == "-G" or args[-1].startswith("ifconfig | awk "), args)

    def make_prebuilt(self) -> Path:
        bundle = self.directory / "prebuilt bundle"
        bundle.mkdir()
        names = (
            "fleet-cas", "fleet-cas-run", "fleet-cas-mount", "fleet-cas-settings.sh",
            "fleet-cas-marker.sh", "fleet-cas-warm.sh", "fleet-cas-prewarm.sh",
            "fleet-cas-writer-build.sh",
        )
        checksums = []
        for name in names:
            data = b"#!/bin/sh\nexit 0\n"
            path = bundle / name
            path.write_bytes(data)
            path.chmod(0o755)
            checksums.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        (bundle / "SHA256SUMS").write_text("".join(checksums))
        return bundle

    def test_default_plan_never_stages_or_executes_remote_installer(self) -> None:
        result = self.run_rollout(
            "--store", "store", "--writer", "writer", "--sign-key", "/keys/writer",
            "--trusted-keys", "ab", "--prewarm", "cmux@catchup-v1", "node", "writer",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_read_only()
        self.assertIn("store --listen 100.64.0.10:7450 --writers 100.64.0.11", result.stdout)
        self.assertIn("writer --store 100.64.0.10:7450", result.stdout)
        self.assertIn("node --store 100.64.0.10:7450", result.stdout)
        self.assertIn("--prewarm cmux@catchup-v1", result.stdout)
        self.assertEqual(result.stdout.count("== writer"), 1)
        self.assertIn("remote installer checks are deferred", result.stdout)

    def test_plan_with_prebuilt_and_custom_volume_is_read_only(self) -> None:
        bundle = self.make_prebuilt()
        result = self.run_rollout(
            "--store", "store", "--store-root", "/Volumes/compiler cache",
            "--store-image", "/Volumes/External Disk/cache.sparsebundle",
            "--prebuilt-dir", str(bundle), "node",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_read_only()
        self.assertIn("FLEET_CAS_ROOT=/Volumes/compiler\\ cache", result.stdout)
        self.assertIn("FLEET_CAS_IMAGE=/Volumes/External\\ Disk/cache.sparsebundle", result.stdout)
        self.assertIn("GLAEDA_FLEET_CAS_PREBUILT=$HOME/.cache/glaeda-fleet-cas/prebuilt", result.stdout)

    def test_apply_preserves_staging_and_role_arguments(self) -> None:
        bundle = self.make_prebuilt()
        result = self.run_rollout(
            "--store", "store", "--writer", "writer", "--sign-key", "/keys/writer",
            "--trusted-keys", "ab", "--prewarm", "cmux@catchup-v1",
            "--prebuilt-dir", str(bundle), "node", "writer", "--apply",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.effects.exists())
        calls = self.recorded_calls()
        copies = [args for tool, args in calls if tool == "rsync"]
        self.assertEqual(len(copies), 9)
        self.assertEqual(sum("--delete" in args for args in copies), 6)
        installs = [args for tool, args in calls if tool == "ssh"
                    and "scripts/glaeda-fleet-cas " in args[-1]]
        self.assertEqual([args[-2] for args in installs], ["store", "writer", "node"])
        for args in installs:
            self.assertIn("--apply", args[-1])
            self.assertIn("GLAEDA_FLEET_CAS_PREBUILT=$HOME/.cache/glaeda-fleet-cas/prebuilt", args[-1])
        self.assertIn("--writers 100.64.0.11 --trusted-keys ab", installs[0][-1])
        self.assertIn("--sign-key /keys/writer --trusted-keys ab", installs[1][-1])
        self.assertIn("--trusted-keys ab --prewarm cmux@catchup-v1", installs[2][-1])

    def test_corrupt_prebuilt_refuses_plan_and_apply_before_ssh(self) -> None:
        bundle = self.make_prebuilt()
        (bundle / "fleet-cas").write_text("changed")
        for apply in ((), ("--apply",)):
            with self.subTest(apply=apply):
                result = self.run_rollout("--store", "store", "--prebuilt-dir", str(bundle), *apply)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("prebuilt bundle checksum failed", result.stderr)
                self.assertEqual(self.recorded_calls(), [])

    def test_nonexecutable_prebuilt_refuses_plan_and_apply_before_ssh(self) -> None:
        bundle = self.make_prebuilt()
        (bundle / "fleet-cas-mount").chmod(0o644)
        for apply in ((), ("--apply",)):
            with self.subTest(apply=apply):
                result = self.run_rollout("--store", "store", "--prebuilt-dir", str(bundle), *apply)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("prebuilt bundle missing regular executable", result.stderr)
                self.assertEqual(self.recorded_calls(), [])

    def test_coordinator_guard_still_refuses_plan_before_remote_effects(self) -> None:
        result = self.run_rollout("--store", "100.89.225.106")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--allow-coordinator", result.stderr)
        self.assert_read_only()


if __name__ == "__main__":
    unittest.main()
