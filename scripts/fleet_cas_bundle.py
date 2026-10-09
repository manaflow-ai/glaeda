#!/usr/bin/env python3
"""Build, verify, and stage an explicit fleet-CAS prebuilt candidate."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile

SCHEMA = "glaeda-fleet-cas-prebuilt/v1"
TARGETS = {"aarch64-apple-darwin", "x86_64-unknown-linux-gnu"}
FILES = (
    "fleet-cas", "fleet-cas-run", "fleet-cas-mount", "fleet-cas-settings.sh",
    "fleet-cas-marker.sh", "fleet-cas-warm.sh", "fleet-cas-prewarm.sh",
    "fleet-cas-writer-build.sh",
)
MAX_FILE = 100 * 1024 * 1024
MAX_TOTAL = 200 * 1024 * 1024
MAX_ARCHIVE = MAX_TOTAL


class BundleError(RuntimeError):
    pass


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_file(path: Path, limit: int = MAX_FILE) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise BundleError(f"bundle input cannot be opened safely: {path}") from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise BundleError(f"bundle input must be a bounded regular file: {path}")
        chunks = []
        total = 0
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise BundleError(f"bundle input changed while reading: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        if ((after.st_dev, after.st_ino, after.st_size) != (before.st_dev, before.st_ino, before.st_size)
                or total != before.st_size):
            raise BundleError(f"bundle input changed while reading: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def command(root: Path, argv: list[str], capture: bool = True) -> bytes:
    executable = shutil.which(argv[0])
    if executable is None:
        raise BundleError(f"required tool unavailable: {argv[0]}")
    env = {k: os.environ[k] for k in ("PATH", "HOME", "CARGO_HOME", "RUSTUP_HOME", "TMPDIR", "TMP", "TEMP") if k in os.environ}
    result = subprocess.run([executable, *argv[1:]], cwd=root, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if capture else None, check=False)
    if result.returncode:
        raise BundleError(f"{argv[0]} failed with exit code {result.returncode}")
    return result.stdout if capture else b""


def source_identity(root: Path, expected: str) -> dict[str, str]:
    if len(expected) != 40 or any(c not in "0123456789abcdef" for c in expected):
        raise BundleError("expected source must be a full commit SHA")
    if command(root, ["git", "status", "--porcelain", "--untracked-files=normal"]):
        raise BundleError("candidate source checkout must be clean")
    commit = command(root, ["git", "rev-parse", "HEAD^{commit}"]).decode().strip()
    if commit != expected:
        raise BundleError("candidate source commit does not match request")
    tree = command(root, ["git", "rev-parse", "HEAD^{tree}"]).decode().strip()
    return {"repository": "teamleaderleo/glaeda", "commit": commit, "tree": tree}


def private_parent(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts or path.resolve(strict=True) != path:
        raise BundleError("staging parent must be an existing canonical absolute directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise BundleError("staging parent must be owned by the current user with mode 0700")
        return fd
    except BaseException:
        os.close(fd)
        raise


def archive_bytes(files: dict[str, bytes], manifest: dict) -> bytes:
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        entries = {**files, "manifest.json": canonical(manifest)}
        checksums = "".join(f"{digest(entries[name])}  {name}\n" for name in sorted(entries))
        entries["SHA256SUMS"] = checksums.encode()
        for name, data in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), (0o755 if name in FILES else 0o644), 0
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def verified_contents(raw: bytes, expected_digest: str, source: str, target: str) -> tuple[dict, dict[str, bytes]]:
    if len(raw) > MAX_ARCHIVE or digest(raw) != expected_digest:
        raise BundleError("fleet-CAS archive digest mismatch or size limit exceeded")
    contents: dict[str, bytes] = {}
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as gz:
            tar_bytes = gz.read(MAX_TOTAL + 1)
        if len(tar_bytes) > MAX_TOTAL:
            raise BundleError("fleet-CAS archive expands beyond size limit")
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
            for entry in tar:
                if entry.name in contents or entry.name not in set(FILES) | {"manifest.json", "SHA256SUMS"} or not entry.isfile():
                    raise BundleError("fleet-CAS archive contains an unsafe or unexpected entry")
                expected_mode = 0o755 if entry.name in FILES else 0o644
                if entry.mode != expected_mode:
                    raise BundleError("fleet-CAS archive entry has an unexpected mode")
                if entry.size < 0 or entry.size > MAX_FILE:
                    raise BundleError("fleet-CAS archive file exceeds size limit")
                total += entry.size
                if total > MAX_TOTAL:
                    raise BundleError("fleet-CAS archive expands beyond size limit")
                stream = tar.extractfile(entry)
                if stream is None:
                    raise BundleError("fleet-CAS archive entry is unreadable")
                contents[entry.name] = stream.read(MAX_FILE + 1)
        if set(contents) != set(FILES) | {"manifest.json", "SHA256SUMS"}:
            raise BundleError("fleet-CAS archive is incomplete")
        manifest = json.loads(contents["manifest.json"])
        source_doc = manifest.get("source", {}) if isinstance(manifest, dict) else {}
        if (not isinstance(manifest, dict) or set(manifest) != {"schema", "source", "target", "toolchain", "files"}
                or manifest.get("schema") != SCHEMA
                or target not in TARGETS or manifest.get("target") != target or not isinstance(source_doc, dict)
                or source_doc.get("commit") != source
                or source_doc.get("repository") != "teamleaderleo/glaeda"
                or not isinstance(source_doc.get("tree"), str)
                or len(source_doc["tree"]) != 40
                or any(c not in "0123456789abcdef" for c in source_doc["tree"])
                or not isinstance(manifest.get("toolchain"), str)
                or set(manifest.get("files", {})) != set(FILES)):
            raise BundleError("fleet-CAS manifest identity is invalid")
        expected = {name: {"sha256": digest(contents[name]), "size": len(contents[name])} for name in FILES}
        if manifest["files"] != expected:
            raise BundleError("fleet-CAS manifest file inventory mismatch")
        lines = contents["SHA256SUMS"].decode().splitlines()
        if lines != [f"{digest(contents[name])}  {name}" for name in sorted(contents) if name != "SHA256SUMS"]:
            raise BundleError("fleet-CAS checksum inventory mismatch")
        return manifest, {name: contents[name] for name in FILES}
    except (OSError, UnicodeError, json.JSONDecodeError, tarfile.TarError, EOFError, KeyError, TypeError) as error:
        raise BundleError("fleet-CAS archive or manifest is malformed") from error


def build(root: Path, expected_source: str, target: str, output: Path) -> dict:
    source = source_identity(root, expected_source)
    if target not in TARGETS:
        raise BundleError("unsupported fleet-CAS target")
    toolchain = command(root, ["rustc", "-Vv"]).decode().strip()
    if f"host: {target}" not in toolchain.splitlines():
        raise BundleError("fleet-CAS build requires a native supported target")
    build_root = root / "target/fleet-cas-bundle-build"
    command(root, ["cargo", "build", "--locked", "--release", "--manifest-path", "tools/fleet-cas-prototype/Cargo.toml", "--target", target, "--target-dir", str(build_root)], capture=False)
    files = {"fleet-cas": read_file(build_root / target / "release/fleet-cas")}
    for name in FILES[1:]:
        files[name] = read_file(root / "tools/fleet-cas-prototype/scripts" / name)
    if source_identity(root, expected_source) != source:
        raise BundleError("candidate source changed during build")
    manifest = {"schema": SCHEMA, "source": source, "target": target, "toolchain": toolchain,
                "files": {name: {"sha256": digest(data), "size": len(data)} for name, data in files.items()}}
    raw = archive_bytes(files, manifest)
    verified_contents(raw, digest(raw), expected_source, target)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"glaeda-fleet-cas-{expected_source}-{target}.tar.gz"
    with tempfile.NamedTemporaryFile(dir=output, prefix=".fleet-cas.") as temporary:
        temporary.write(raw); temporary.flush(); os.fsync(temporary.fileno())
        os.link(temporary.name, destination)
    return {"archive": destination.name, "sha256": digest(raw), "source": source, "target": target}


def stage(archive: Path, destination: Path, source: str, target: str, expected_digest: str, apply: bool) -> dict:
    raw = read_file(archive, MAX_ARCHIVE)
    manifest, files = verified_contents(raw, expected_digest, source, target)
    if destination.name in ("", ".", ".."):
        raise BundleError("staging destination already exists")
    parent = private_parent(destination.parent)
    generation = None
    created_identity = None
    try:
        try:
            os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise BundleError("staging destination already exists")
        receipt = {"schema": "glaeda-fleet-cas-stage/v1", "state": "planned" if not apply else "staged",
                   "archiveSha256": digest(raw), "source": manifest["source"], "target": target,
                   "automaticUpdateAuthorized": False}
        if not apply:
            return receipt
        os.mkdir(destination.name, 0o700, dir_fd=parent)
        info = os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise BundleError("staging destination is not a directory")
        created_identity = (info.st_dev, info.st_ino)
        generation = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            all_files = {**files, "manifest.json": canonical(manifest)}
            all_files["SHA256SUMS"] = ("".join(f"{digest(data)}  {name}\n" for name, data in sorted(all_files.items()))).encode()
            for name, data in all_files.items():
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755 if name in FILES else 0o644, dir_fd=generation)
                try:
                    view = memoryview(data)
                    while view:
                        view = view[os.write(fd, view):]
                    os.fsync(fd)
                finally:
                    os.close(fd)
            os.fsync(generation)
        finally:
            os.close(generation)
            generation = None
        os.fsync(parent)
        return receipt
    except BaseException:
        try:
            current = os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
            if created_identity == (current.st_dev, current.st_ino) and stat.S_ISDIR(current.st_mode):
                if generation is None:
                    generation = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                held = os.fstat(generation)
                if (held.st_dev, held.st_ino) != created_identity:
                    raise BundleError("staging destination changed during cleanup")
                for entry in os.scandir(generation):
                    os.unlink(entry.name, dir_fd=generation)
                os.close(generation)
                generation = None
                os.rmdir(destination.name, dir_fd=parent)
                os.fsync(parent)
        finally:
            raise
    finally:
        if generation is not None:
            os.close(generation)
        os.close(parent)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build"); b.add_argument("--source", required=True); b.add_argument("--target", required=True); b.add_argument("--output", type=Path, required=True); b.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    v = sub.add_parser("verify"); v.add_argument("archive", type=Path); v.add_argument("--source", required=True); v.add_argument("--target", required=True); v.add_argument("--sha256", required=True, help="SHA-256 from the authenticated release.json asset entry")
    s = sub.add_parser("stage"); s.add_argument("archive", type=Path); s.add_argument("--source", required=True); s.add_argument("--target", required=True); s.add_argument("--sha256", required=True, help="SHA-256 from the authenticated release.json asset entry"); s.add_argument("--destination", type=Path, required=True); s.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "build": result = build(args.root, args.source, args.target, args.output)
        elif args.command == "verify":
            raw = read_file(args.archive, MAX_ARCHIVE)
            result = {"state": "verified", **verified_contents(raw, args.sha256, args.source, args.target)[0]}
        else: result = stage(args.archive, args.destination, args.source, args.target, args.sha256, args.apply)
        print(json.dumps(result, sort_keys=True)); return 0
    except BundleError as error:
        print(f"fleet-cas bundle: {error}", file=os.sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
