#!/usr/bin/env python3
import hashlib
import gzip
import io
import importlib.util
import json
import tarfile
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fleet_cas_bundle", ROOT / "scripts/fleet_cas_bundle.py")
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FleetCasBundleTest(unittest.TestCase):
    def make_archive(self) -> tuple[bytes, str]:
        files = {name: f"#!/bin/sh\necho {name}\n".encode() for name in module.FILES}
        source = "a" * 40
        manifest = {
            "schema": module.SCHEMA,
            "source": {"repository": "teamleaderleo/glaeda", "commit": source, "tree": "b" * 40},
            "target": "x86_64-unknown-linux-gnu",
            "toolchain": "rustc 1.97.1",
            "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for name, data in files.items()},
        }
        return module.archive_bytes(files, manifest), source

    def test_verify_and_stage_are_bounded_and_no_overwrite(self) -> None:
        raw, source = self.make_archive()
        archive_digest = hashlib.sha256(raw).hexdigest()
        manifest, files = module.verified_contents(raw, hashlib.sha256(raw).hexdigest(), source, "x86_64-unknown-linux-gnu")
        self.assertEqual(set(files), set(module.FILES))
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary).resolve() / "bundle"
            archive = Path(temporary).resolve() / "candidate.tar.gz"
            archive.write_bytes(raw)
            self.assertEqual(module.stage(archive, destination, source, "x86_64-unknown-linux-gnu", archive_digest, False)["state"], "planned")
            self.assertFalse(destination.exists())
            self.assertEqual(module.stage(archive, destination, source, "x86_64-unknown-linux-gnu", archive_digest, True)["state"], "staged")
            self.assertEqual((destination / "fleet-cas").read_bytes(), files["fleet-cas"])
            self.assertEqual((destination / "fleet-cas").stat().st_mode & 0o777, 0o755)
            with self.assertRaises(module.BundleError):
                module.stage(archive, destination, source, "x86_64-unknown-linux-gnu", archive_digest, True)
            dangling = Path(temporary).resolve() / "dangling"
            dangling.symlink_to("missing")
            with self.assertRaises(module.BundleError):
                module.stage(archive, dangling, source, "x86_64-unknown-linux-gnu", archive_digest, True)

    def test_tampered_checksum_and_identity_are_rejected(self) -> None:
        raw, source = self.make_archive()
        with self.assertRaises(module.BundleError):
            module.verified_contents(raw[:-1], hashlib.sha256(raw).hexdigest(), source, "x86_64-unknown-linux-gnu")
        with self.assertRaises(module.BundleError):
            module.verified_contents(raw, hashlib.sha256(raw).hexdigest(), "c" * 40, "x86_64-unknown-linux-gnu")

    def test_external_digest_is_required_to_accept_archive(self) -> None:
        raw, source = self.make_archive()
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary).resolve() / "candidate.tar.gz"
            archive.write_bytes(raw)
            with self.assertRaises(module.BundleError):
                module.stage(archive, Path(temporary).resolve() / "out", source, "x86_64-unknown-linux-gnu", "0" * 64, False)

    def test_archive_symlink_is_refused_before_read(self) -> None:
        raw, source = self.make_archive()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            real = root / "real.tar.gz"
            real.write_bytes(raw)
            alias = root / "alias.tar.gz"
            alias.symlink_to(real)
            with self.assertRaises(module.BundleError):
                module.stage(alias, root / "out", source, "x86_64-unknown-linux-gnu", hashlib.sha256(raw).hexdigest(), False)

    def test_failed_write_removes_partial_generation(self) -> None:
        raw, source = self.make_archive()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            archive = root / "candidate.tar.gz"
            archive.write_bytes(raw)
            destination = root / "out"
            original_write = module.os.write
            calls = 0

            def fail_after_first(fd, data):
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise OSError("injected write failure")
                return original_write(fd, data)

            with mock.patch.object(module.os, "write", side_effect=fail_after_first):
                with self.assertRaises(OSError):
                    module.stage(archive, destination, source, "x86_64-unknown-linux-gnu", hashlib.sha256(raw).hexdigest(), True)
            self.assertFalse(destination.exists())

    def test_failed_generation_open_removes_empty_destination(self) -> None:
        raw, source = self.make_archive()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            archive = root / "candidate.tar.gz"
            archive.write_bytes(raw)
            destination = root / "out"
            original_open = module.os.open
            failed = False

            def fail_generation_open(path, flags, *args, **kwargs):
                nonlocal failed
                if not failed and path == "out" and kwargs.get("dir_fd") is not None and flags & module.os.O_DIRECTORY:
                    failed = True
                    raise OSError("injected generation open failure")
                return original_open(path, flags, *args, **kwargs)

            with mock.patch.object(module.os, "open", side_effect=fail_generation_open):
                with self.assertRaises(OSError):
                    module.stage(archive, destination, source, "x86_64-unknown-linux-gnu", hashlib.sha256(raw).hexdigest(), True)
            self.assertFalse(destination.exists())

    def test_malformed_entries_and_expansion_limits_are_rejected(self) -> None:
        raw, source = self.make_archive()

        def mutate(entry: tarfile.TarInfo, payload: bytes = b"") -> bytes:
            out = io.BytesIO()
            with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
                tar.addfile(entry, io.BytesIO(payload))
            return out.getvalue()

        for entry in (
            tarfile.TarInfo("../escape"),
            tarfile.TarInfo("fleet-cas"),
        ):
            if entry.name == "fleet-cas":
                entry.type = tarfile.SYMTYPE
                entry.linkname = "outside"
            with self.subTest(entry=entry.name):
                with self.assertRaises(module.BundleError):
                    module.verified_contents(mutate(entry), hashlib.sha256(mutate(entry)).hexdigest(), source, "x86_64-unknown-linux-gnu")

        duplicate = tarfile.TarInfo("fleet-cas")
        duplicate.mode, duplicate.size = 0o755, 1
        out = io.BytesIO()
        with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
            tar.addfile(duplicate, io.BytesIO(b"a"))
            tar.addfile(duplicate, io.BytesIO(b"a"))
        duplicate_raw = out.getvalue()
        with self.assertRaises(module.BundleError):
            module.verified_contents(duplicate_raw, hashlib.sha256(duplicate_raw).hexdigest(), source, "x86_64-unknown-linux-gnu")

        bomb = tarfile.TarInfo("fleet-cas")
        bomb.mode, bomb.size = 0o755, module.MAX_FILE + 1
        out = io.BytesIO()
        class Zeroes:
            def __init__(self, remaining: int): self.remaining = remaining
            def read(self, size: int = -1) -> bytes:
                size = min(size, self.remaining)
                self.remaining -= size
                return b"\0" * size
        with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
            tar.addfile(bomb, Zeroes(bomb.size))
        bomb_raw = out.getvalue()
        with self.assertRaises(module.BundleError):
            module.verified_contents(bomb_raw, hashlib.sha256(bomb_raw).hexdigest(), source, "x86_64-unknown-linux-gnu")


if __name__ == "__main__":
    unittest.main()
