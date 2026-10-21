#!/usr/bin/env python3
"""Pin the runner hook's sha256 in glaeda-disk's drain allowlist.

    scripts/glaeda_hook_pin.py           add the sha of scripts/glaeda-cmux-runner-hook to CMUX_NEXT_DRAIN_HOOK_SHA256
    scripts/glaeda_hook_pin.py --check   exit 1 (and name the sha) when it is missing; change nothing

glaeda-disk's cmux-next drain trusts only runner hooks whose bytes are a reviewed release
(CMUX_NEXT_DRAIN_HOOK_SHA256), so every change to the hook must append its new sha. A person had to
remember, and #1550 and #1561 did not: main's drain tests went red with "runner hook enrollment is not
proven". The tests now call this: test-glaeda-cmux-runner.py and test-glaeda-disk.py pin the hook when they
run on a checkout (so changing the hook and running its tests leaves the pin in the diff), and only check
under CI, where a missing pin fails with the command to run.

It appends the current hook's sha and keeps every released pin (runners that have not refreshed their hook
still drain). A sha it pinned for an earlier draft of the same change is dropped again: a pin stays only if
origin/main already has it or some version of the hook on origin/main hashes to it, so editing the hook twice
before merging leaves one new pin, not a trail of drafts nobody reviewed. Without origin/main it only appends.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "glaeda-cmux-runner-hook"
DISK = ROOT / "scripts" / "glaeda-disk"
BLOCK = re.compile(r"^CMUX_NEXT_DRAIN_HOOK_SHA256 = frozenset\(\{\n(?P<body>(?:    \"[0-9a-f]{64}\",\n)*)\}\)\n", re.M)
COMMAND = "python3 scripts/glaeda_hook_pin.py"


def hook_sha(hook: Path | None = None) -> str:
    return hashlib.sha256((hook or HOOK).read_bytes()).hexdigest()


def pinned(text: str) -> list[str]:
    match = BLOCK.search(text)
    if not match:
        raise ValueError("scripts/glaeda-disk has no CMUX_NEXT_DRAIN_HOOK_SHA256 block in the expected form")
    return re.findall(r"[0-9a-f]{64}", match["body"])


def pin(text: str, sha: str, released: set[str] | None = None) -> str:
    """text with sha appended to the allowlist and, when released is known, any pin outside it dropped (an earlier
    draft's sha); unchanged when sha is there and nothing is dropped."""
    pins = pinned(text)
    keep = [p for p in pins if released is None or p in released or p == sha]
    if sha not in keep:
        keep.append(sha)
    if keep == pins:
        return text
    match = BLOCK.search(text)
    return text[:match.start("body")] + "".join(f'    "{p}",\n' for p in keep) + text[match.end("body"):]


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", os.fspath(ROOT), *args], capture_output=True, text=True, check=True,
                          timeout=60).stdout


def released(base: str = "origin/main") -> set[str] | None:
    """The shas base pins plus the sha of every hook version on base; None when base is not here."""
    try:
        shas = set(pinned(git("show", f"{base}:scripts/glaeda-disk")))
        for commit in git("rev-list", base, "--", "scripts/glaeda-cmux-runner-hook").split():
            blob = subprocess.run(["git", "-C", os.fspath(ROOT), "show", f"{commit}:scripts/glaeda-cmux-runner-hook"],
                                  capture_output=True, timeout=60)
            if blob.returncode == 0:  # absent in the commit that deleted or renamed it
                shas.add(hashlib.sha256(blob.stdout).hexdigest())
        return shas
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def write(path: Path, text: str) -> None:
    """Replace path in one step (glaeda-disk is executable and imported by every test; never leave it half-written)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def ensure(check: bool, hook: Path | None = None, disk: Path | None = None, base: str = "origin/main") -> str | None:
    """None when the hook's sha is pinned (after pinning it, unless check); else what is wrong."""
    hook, disk = hook or HOOK, disk or DISK
    sha, text = hook_sha(hook), disk.read_text()
    if check:
        if sha in pinned(text):
            return None
        return f"scripts/glaeda-cmux-runner-hook ({sha}) is not in CMUX_NEXT_DRAIN_HOOK_SHA256; run {COMMAND}"
    if sha in pinned(text) and pinned(text)[-1] == sha:
        return None  # the usual case: nothing to pin, and no git calls on every test run
    new = pin(text, sha, released(base))
    if new != text:
        write(disk, new)
    return None


def under_ci() -> bool:
    return bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))


def main(argv: list[str]) -> int:
    if argv not in ([], ["--check"]):
        print(__doc__, file=sys.stderr)
        return 2
    problem = ensure(check=bool(argv))
    if problem:
        print(problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
