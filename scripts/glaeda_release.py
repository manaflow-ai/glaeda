#!/usr/bin/env python3
"""Durable glaeda releases and their update channels (#525), consumed by scripts/glaeda-update (#149).

Every push to main builds one release in CI (.github/workflows/release.yml):

    glaeda-hygiene-<target>.tar.gz   the source tree at that commit under glaeda/, plus a prebuilt
                                     bin/glaeda-worktree-reclaim; glaeda-update installs it with the
                                     tree's own scripts/glaeda-mini-setup --reclaim-binary
    glaeda-<source>-<target>.tar.gz  the fleet candidate bundle (scripts/fleet_bundle.py), carried
                                     for the operator's glaeda-mini-fleet upgrade; not auto-applied
    release.json                     source commit, signed risk/soak policy, and the SHA-256 and size of every asset

The release is tagged r-<UTC date>-<source[:12]> and each asset carries a build-provenance
attestation from release.yml on main. One more release, tag `ota-channels`, holds the only mutable
files, each with exactly one writer so concurrent workflow runs never overwrite each other:

    canary.json   {"schema": "glaeda-ota-channel/v1", "ring": "canary", "tag", "source",
                   "releaseSha256", "published"}                      written by release.yml
    stable.json   the same, ring "stable", plus "promoted"            written by promote.yml
    control.json  {"schema": "glaeda-ota-control/v1", "paused": false} written by control.yml

Hosts read them anonymously (the repository is public) and trust nothing by name: release.json
must match `releaseSha256`, and every asset must match release.json. Canary only moves forward
(release.yml checks ancestry). Stable moves to the newest release that has soaked (`SOAK_HOURS`),
that canary hosts reported success on and none failure (commit statuses `glaeda-ota/<host>`), and
that descends from the current stable. One canary's success is enough and no host is required, so
a canary that never reports cannot hold stable, while any canary's failure blocks it (2026-10-19).
Not only the current canary: with several merges a day the newest release never finishes soaking,
and stable would starve.

Promotion never waits on the shared GitHub quota (2026-10-20). Canary hosts post their statuses with the
fleet's one REST quota; when it is spent for hours, no host can say "healthy". Every release also carries
smoke-<target>.json for each target: release.yml's build extracted that target's hygiene archive on a
fresh runner and ran the same health checks glaeda-update runs after an install (smoke). A release
whose every target smoked, that no canary reported failure on, and that no canary reported at all for
CANARY_SILENT_HOURS past its soak, is promoted on that evidence alone. A canary's failure still blocks
it: glaeda-update waits up to an hour to post one, well inside that window.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

REPOSITORY = "teamleaderleo/glaeda"
CHANNELS_TAG = "ota-channels"
CHANNEL_SCHEMA = "glaeda-ota-channel/v1"
CONTROL_SCHEMA = "glaeda-ota-control/v1"
RELEASE_SCHEMA = "glaeda-release/v1"
POLICY_SCHEMA = "glaeda-release-policy/v1"
TARGETS = ("aarch64-apple-darwin", "x86_64-unknown-linux-gnu")
STATUS_PREFIX = "glaeda-ota/"
SOAK_HOURS = 6
# A short window catches immediate install/health regressions. Risk still controls
# canary coverage, while legacy manifests retain the original six-hour behavior.
RISK_SOAK_HOURS = {"low": 0.25, "normal": 0.25, "high": 0.25}
HISTORICAL_RISK_SOAK_HOURS = {"low": 2, "normal": 6, "high": 12}
# No canary host is required: promotion takes one success and no failure. Policies signed before
# 2026-10-19 list these two as required; they still parse, and that list is no longer enforced
# (a spent REST quota kept air-blue from reporting and held stable on one build for 15 hours).
LEGACY_REQUIRED_HOSTS = ("air-blue", "big-red")
MAX_ASSET = 200 * 1024 * 1024
# How long past its soak a release waits for any canary host's report before its CI smoke alone promotes
# it. Longer than glaeda-update's FAILURE_WAIT_MAX plus BACKOFF_MAX_S (test-glaeda-update.py checks), so a
# failing canary has posted its failure by then even through a spent quota.
CANARY_SILENT_HOURS = 2.5
SMOKE_SCHEMA = "glaeda-release-smoke/v1"
TAG_RE = re.compile(r"r-\d{8}-[0-9a-f]{12}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")


class ReleaseError(RuntimeError):
    pass


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# The release's source commit and its ancestors, newest first, one per line. glaeda-update reads it to tell a
# runner copy an operator staged from an ancestor of this release (older: refresh it) from a newer one (keep it).
# The build checks out full history for it (release.yml); a shallow checkout lists only the source.
ANCESTRY = "ancestry.txt"
ANCESTRY_MAX = 5000
# {file: SHA-256 of every version of it in the source's history} for the files glaeda-cmux-runner --refresh-hooks
# installs. A runner hook with one of these digests is one this release descends from, so replacing it is never
# a downgrade; any other hook (installed from a newer main, or edited) is left to --apply.
HOOK_HISTORY = "hook-history.json"
HOOK_FILES = ("glaeda-cmux-runner-hook", "glaeda_reservation.py")
HOOK_HISTORY_MAX = 1000


def hook_history(root: Path, source: str) -> bytes:
    history = {}
    for name in HOOK_FILES:
        path = f"scripts/{name}"
        commits = subprocess.run(["git", "-C", str(root), "rev-list", f"--max-count={HOOK_HISTORY_MAX}", source,
                                  "--", path], check=True, capture_output=True, text=True).stdout.split()
        digests = set()
        for commit in [source, *commits]:
            blob = subprocess.run(["git", "-C", str(root), "show", f"{commit}:{path}"], capture_output=True)
            if blob.returncode == 0:  # a commit that deleted or renamed it has no version
                digests.add(sha256(blob.stdout))
        history[name] = sorted(digests)
    return canonical(history)


def hygiene_asset(target: str) -> str:
    return f"glaeda-hygiene-{target}.tar.gz"


def smoke_asset(target: str) -> str:
    return f"smoke-{target}.json"


def smoke(archive: Path, target: str, source: str, python: str = sys.executable) -> dict:
    """Extract a hygiene archive into a scratch home and run what glaeda-update's health() runs after an
    install: the tools answer --help and the release's glaeda-update plans from a channel offline."""
    import shutil
    import tempfile
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            if hasattr(tarfile, "data_filter"):  # 3.12 (and backports); older Pythons have no filter argument
                tar.extractall(root / "gen", filter="data")
            else:
                tar.extractall(root / "gen")  # our own just-built archive
        scripts, home, channel = root / "gen/glaeda/scripts", root / "home", root / "channel"
        home.mkdir()
        channel.mkdir()
        (channel / "control.json").write_bytes(canonical(control(False)))
        (channel / "stable.json").write_bytes(b"")
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
        runs = [[python, str(scripts / name), "--help"]
                for name in ("glaeda-disk", "glaeda-worktree", "glaeda-projects", "glaeda-gh", "glaeda-mini-health")]
        runs += [[str(root / "gen/bin/glaeda-worktree-reclaim"), "--help"],
                 [python, str(scripts / "glaeda-update"), "--channel-dir", str(channel)]]
        for argv in runs:
            name = Path(argv[1] if argv[0] == python else argv[0]).name
            result = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=env,
                                    stdin=subprocess.DEVNULL)
            if result.returncode != 0:
                raise ReleaseError(f"smoke: {name} exited {result.returncode}: {(result.stderr or result.stdout)[-300:]}")
            if name == "glaeda-update":
                planned = json.loads(result.stdout.strip().splitlines()[-1]).get("result")
                if planned not in ("plan", "current", "idle"):
                    raise ReleaseError(f"smoke: glaeda-update planned {planned}")
            checks.append(name)
        shutil.rmtree(root / "gen", ignore_errors=True)
    version = subprocess.run([python, "-c", "import platform; print(platform.python_version())"],
                             capture_output=True, text=True, timeout=60).stdout.strip()
    return {"schema": SMOKE_SCHEMA, "target": target, "source": source, "checks": checks, "python": version}


def smoked(doc: dict) -> bool:
    """Every target's smoke receipt is an asset of this release.json (and so hash-pinned and attested)."""
    assets = doc.get("assets") or {}
    return all(smoke_asset(target) in assets for target in TARGETS)


def release_tag(source: str, when: dt.datetime) -> str:
    if not SHA_RE.fullmatch(source):
        raise ReleaseError("source must be a full commit id")
    return f"r-{when.strftime('%Y%m%d')}-{source[:12]}"


def _add(tar: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), mode, 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def hygiene_archive(root: Path, source: str, reclaim: Path) -> bytes:
    """The committed tree at `source` under glaeda/, plus the reclaim binary and ANCESTRY, as a stable tar.gz."""
    tree = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", "--prefix=glaeda/", source],
                          check=True, capture_output=True).stdout
    ancestry = subprocess.run(["git", "-C", str(root), "rev-list", f"--max-count={ANCESTRY_MAX}", source],
                              check=True, capture_output=True).stdout
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(tree)) as src, \
            gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for member in src.getmembers():
            if member.isfile():
                _add(tar, member.name, src.extractfile(member).read(), 0o755 if member.mode & 0o111 else 0o644)
            elif member.isdir():
                info = tarfile.TarInfo(member.name)
                info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, 0
                tar.addfile(info)
            # symlinks and anything else are left out; extraction refuses them anyway
        _add(tar, "bin/glaeda-worktree-reclaim", reclaim.read_bytes(), 0o755)
        _add(tar, ANCESTRY, ancestry, 0o644)
        _add(tar, HOOK_HISTORY, hook_history(root, source), 0o644)
    return out.getvalue()


def policy_for_paths(paths: list[str]) -> dict:
    """Classify a release from its exact first-parent diff; unknown input is high risk."""
    if not paths:
        risk = "high"
    else:
        low = all(path.startswith(("docs/", "tests/", "scripts/test-")) or
                  path in {"README.md", "CHANGELOG.md"} for path in paths)
        high_markers = ("glaeda-update", "glaeda_release.py", "glaeda-mini-setup",
                        "glaeda-cmux-runner", "glaeda-fleet", "fleet_", "Cargo.",
                        "src/", ".github/workflows/", "auth", "attest", "rollback",
                        "persist", "lease", "cleanup", "ownership", "recovery", "security")
        risk = "low" if low else "high" if any(any(marker in path for marker in high_markers)
                                                 for path in paths) else "normal"
    return {"schema": POLICY_SCHEMA, "risk": risk, "soakHours": RISK_SOAK_HOURS[risk],
            "requiredHosts": [], "paths": sorted(paths)}


def release_policy(root: Path, source: str) -> dict:
    parent = f"{source}^1"
    result = subprocess.run(["git", "-C", str(root), "diff", "--name-only", parent, source],
                            check=True, capture_output=True, text=True)
    return policy_for_paths([path for path in result.stdout.splitlines() if path])


def parse_policy(value: object, *, legacy: bool = False) -> dict:
    if value is None and legacy:
        return {"schema": POLICY_SCHEMA, "risk": "normal", "soakHours": SOAK_HOURS,
                "requiredHosts": [], "paths": [], "legacy": True}
    if isinstance(value, dict) and value.get("legacy") is True:
        return parse_policy(None, legacy=True)
    if not isinstance(value, dict) or value.get("schema") != POLICY_SCHEMA:
        raise ReleaseError("release policy is missing or malformed")
    risk = value.get("risk")
    hours = value.get("soakHours")
    hosts = value.get("requiredHosts")
    paths = value.get("paths")
    valid_hours = {RISK_SOAK_HOURS[risk], HISTORICAL_RISK_SOAK_HOURS[risk]} if risk in RISK_SOAK_HOURS else set()
    if risk not in RISK_SOAK_HOURS or hours not in valid_hours or not isinstance(hosts, list) \
            or hosts and (risk == "low" or not all(isinstance(host, str) for host in hosts)
                          or sorted(hosts) != sorted(LEGACY_REQUIRED_HOSTS)) \
            or not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ReleaseError("release policy is malformed")
    return {"schema": POLICY_SCHEMA, "risk": risk, "soakHours": hours,
            "requiredHosts": sorted(hosts), "paths": sorted(paths)}


def release_manifest(source: str, tag: str, assets: dict[str, bytes], policy: dict | None = None) -> dict:
    out = {
        "schema": RELEASE_SCHEMA, "repository": REPOSITORY, "tag": tag, "source": source,
        "assets": {name: {"sha256": sha256(raw), "size": len(raw)} for name, raw in sorted(assets.items())},
    }
    if policy is not None:
        out["policy"] = policy
    return out


def parse_release(raw: bytes, expected_sha256: str) -> dict:
    """release.json, checked against the digest the channel names."""
    if sha256(raw) != expected_sha256:
        raise ReleaseError("release.json does not match the channel's digest")
    doc = json.loads(raw)
    if (doc.get("schema") != RELEASE_SCHEMA or doc.get("repository") != REPOSITORY
            or not TAG_RE.fullmatch(str(doc.get("tag"))) or not SHA_RE.fullmatch(str(doc.get("source")))):
        raise ReleaseError("release.json is malformed")
    doc["policy"] = parse_policy(doc.get("policy"), legacy=doc.get("policy") is None)
    for name, meta in doc.get("assets", {}).items():
        if ("/" in name or not HEX64.fullmatch(str(meta.get("sha256")))
                or not isinstance(meta.get("size"), int) or not 0 < meta["size"] <= MAX_ASSET):
            raise ReleaseError(f"release.json lists a malformed asset: {name!r}")
    return doc


def channel_entry(ring: str, doc: dict, release_raw: bytes, published: str) -> dict:
    return {"schema": CHANNEL_SCHEMA, "ring": ring, "tag": doc["tag"], "source": doc["source"],
            "releaseSha256": sha256(release_raw), "published": published,
            "policy": parse_policy(doc.get("policy"), legacy=doc.get("policy") is None)}


def parse_channel(raw: bytes, ring: str) -> dict:
    doc = json.loads(raw)
    if (doc.get("schema") != CHANNEL_SCHEMA or doc.get("ring") != ring
            or not TAG_RE.fullmatch(str(doc.get("tag"))) or not SHA_RE.fullmatch(str(doc.get("source")))
            or not HEX64.fullmatch(str(doc.get("releaseSha256")))):
        raise ReleaseError(f"{ring}.json is malformed")
    return doc


def control(paused: bool) -> dict:
    return {"schema": CONTROL_SCHEMA, "paused": paused}


def parse_control(raw: bytes) -> dict:
    doc = json.loads(raw)
    if doc.get("schema") != CONTROL_SCHEMA or not isinstance(doc.get("paused"), bool):
        raise ReleaseError("control.json is malformed")
    return doc


def canary_verdict(statuses: list[dict]) -> tuple[int, int, int]:
    """(successes, failures, other) among the newest status of each glaeda-ota/<host> context."""
    newest: dict[str, dict] = {}
    for status in statuses:
        context = str(status.get("context", ""))
        if not context.startswith(STATUS_PREFIX):
            continue
        seen = newest.get(context)
        if seen is None or str(status.get("updated_at", "")) > str(seen.get("updated_at", "")):
            newest[context] = status
    states = [status.get("state") for status in newest.values()]
    successes = sum(state == "success" for state in states)
    failures = sum(state in ("failure", "error") for state in states)
    return successes, failures, len(states) - successes - failures


def promotion(canary: dict | None, stable: dict | None, statuses: list[dict], now: dt.datetime,
              soak_hours: float | None = None, smoke_passed: bool = False) -> tuple[bool, str]:
    """Whether `canary` may become `stable` now, and why. Pausing stops hosts, not this."""
    if canary is None:
        return False, "no canary release"
    if stable is not None and stable["tag"] == canary["tag"]:
        return False, "stable is already the canary"
    policy = parse_policy(canary.get("policy"), legacy=canary.get("policy") is None)
    soak_hours = policy["soakHours"] if soak_hours is None else soak_hours
    published = dt.datetime.fromisoformat(canary["published"].replace("Z", "+00:00"))
    age = (now - published).total_seconds() / 3600
    if age < soak_hours:
        return False, f"canary soaking: {age:.1f} h of {soak_hours} h"
    # One success and no failure. A canary that is silent or pending holds nothing past the soak.
    ok, failed, _ = canary_verdict(statuses)
    if failed:
        return False, f"{failed} canary host(s) reported failure"
    if not ok:
        if smoke_passed and age >= soak_hours + CANARY_SILENT_HOURS:
            # No host could report (the shared quota): the release's own CI smoke is the evidence.
            return True, f"no canary host reported in {age:.1f} h; CI smoke passed on {', '.join(TARGETS)}"
        return False, "no canary host reported success" + (
            f"; CI smoke promotes it at {soak_hours + CANARY_SILENT_HOURS:g} h if none reports" if smoke_passed else "")
    return True, f"{ok} canary host(s) healthy after {age:.1f} h"


def choose(candidates: list[dict], stable: dict | None, now: dt.datetime,
           soak_hours: float | None = None) -> tuple[dict | None, str]:
    """The newest candidate that may become stable, and why (or why none).

    Each candidate is {"entry": <canary-shaped entry>, "statuses": [...], "descendsFromStable": bool}.
    Candidates include releases still soaking: their failures block older releases.
    """
    reasons = []
    for candidate in sorted(candidates, key=lambda c: c["entry"]["published"], reverse=True):
        entry = candidate["entry"]
        if stable is not None and entry["tag"] == stable["tag"]:
            reasons.append(f"{entry['tag']}: already stable")
            break  # everything older is older than stable too
        policy = parse_policy(candidate.get("policy"), legacy=candidate.get("policy") is None)
        if canary_verdict(candidate["statuses"])[1]:
            # A release's own updater installs its successor, so a canary failure on a newer release can
            # be an older release's fault: nothing older is promoted until a newer one is healthy.
            reasons.append(f"{entry['tag']}: a canary reported failure; nothing older is promoted")
            break
        if stable is not None and not candidate.get("descendsFromStable"):
            reasons.append(f"{entry['tag']}: does not descend from stable")
            continue
        ok, why = promotion(entry, stable, candidate["statuses"], now,
                            soak_hours if soak_hours is not None else policy["soakHours"],
                            smoke_passed=candidate.get("smoked") is True)
        if ok:
            return entry, f"{entry['tag']}: {why}"
        reasons.append(f"{entry['tag']}: {why}")
    return None, "; ".join(reasons) or "no releases"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(when: dt.datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    h = sub.add_parser("hygiene", help="write glaeda-hygiene-<target>.tar.gz")
    h.add_argument("--source", required=True)
    h.add_argument("--target", required=True, choices=TARGETS)
    h.add_argument("--reclaim", type=Path, required=True)
    h.add_argument("--output", type=Path, required=True)
    sm = sub.add_parser("smoke", help="smoke a hygiene archive; write smoke-<target>.json beside it")
    sm.add_argument("--source", required=True)
    sm.add_argument("--target", required=True, choices=TARGETS)
    sm.add_argument("--output", type=Path, required=True, help="the directory holding the archive")
    sm.add_argument("--python", default=sys.executable, help="the interpreter hosts run the tools with")
    t = sub.add_parser("tag", help="print the release tag for a source commit")
    t.add_argument("--source", required=True)
    m = sub.add_parser("manifest", help="write release.json over the assets in a directory")
    m.add_argument("--source", required=True)
    m.add_argument("--tag", required=True)
    m.add_argument("--assets", type=Path, required=True)
    c = sub.add_parser("canary", help="print canary.json for a release")
    c.add_argument("--release", type=Path, required=True)
    d = sub.add_parser("candidate", help="print one promotion candidate as a JSON line")
    d.add_argument("--release", type=Path, required=True, help="the release's release.json")
    d.add_argument("--published", required=True, help="the release's creation time (ISO 8601)")
    d.add_argument("--statuses", type=Path, required=True, help="its source commit's statuses (API JSON)")
    d.add_argument("--descends", choices=("true", "false"), required=True, help="descends from stable")
    p = sub.add_parser("promote", help="print stable.json for the best candidate (exit 3 if none)")
    p.add_argument("--candidates", type=Path, required=True, help="JSON lines from `candidate`")
    p.add_argument("--stable", type=Path, help="current stable.json; absent means none yet")
    p.add_argument("--soak-hours", type=float, help="explicit operator override; otherwise use release policy")
    k = sub.add_parser("control", help="print control.json")
    k.add_argument("--paused", choices=("true", "false"), required=True)
    a = ap.parse_args(argv)
    try:
        if a.command == "promote" and a.soak_hours is not None \
                and (not math.isfinite(a.soak_hours) or a.soak_hours < 0):
            raise ReleaseError("--soak-hours must be finite and non-negative")
        if a.command == "hygiene":
            raw = hygiene_archive(Path(__file__).resolve().parents[1], a.source, a.reclaim)
            a.output.mkdir(parents=True, exist_ok=True)
            (a.output / hygiene_asset(a.target)).write_bytes(raw)
            print(json.dumps({"asset": hygiene_asset(a.target), "sha256": sha256(raw), "size": len(raw)}))
        elif a.command == "smoke":
            receipt = smoke(a.output / hygiene_asset(a.target), a.target, a.source, a.python)
            (a.output / smoke_asset(a.target)).write_bytes(canonical(receipt))
            print(json.dumps(receipt))
        elif a.command == "tag":
            print(release_tag(a.source, now_utc()))
        elif a.command == "manifest":
            assets = {p.name: p.read_bytes() for p in sorted(a.assets.iterdir())
                      if p.is_file() and p.name != "release.json"}
            if not assets:
                raise ReleaseError("no assets")
            root = Path(__file__).resolve().parents[1]
            (a.assets / "release.json").write_bytes(canonical(
                release_manifest(a.source, a.tag, assets, release_policy(root, a.source))))
        elif a.command == "canary":
            release_raw = a.release.read_bytes()
            doc = parse_release(release_raw, sha256(release_raw))
            sys.stdout.buffer.write(canonical(channel_entry("canary", doc, release_raw, iso(now_utc()))))
        elif a.command == "candidate":
            release_raw = a.release.read_bytes()
            doc = parse_release(release_raw, sha256(release_raw))
            statuses = json.loads(a.statuses.read_bytes())
            if not isinstance(statuses, list):
                raise ReleaseError("statuses must be the API's JSON list")
            entry = channel_entry("canary", doc, release_raw, a.published)
            policy = parse_policy(doc.get("policy"), legacy=doc.get("policy") is None)
            print(json.dumps({"entry": entry, "policy": policy, "statuses": statuses,
                              "descendsFromStable": a.descends == "true", "smoked": smoked(doc)},
                             sort_keys=True))
        elif a.command == "promote":
            stable = parse_channel(a.stable.read_bytes(), "stable") if a.stable else None
            candidates = [json.loads(line) for line in a.candidates.read_text().splitlines() if line.strip()]
            for candidate in candidates:
                parse_channel(canonical(candidate["entry"]), "canary")
            chosen, why = choose(candidates, stable, now_utc(), a.soak_hours)
            print(why, file=sys.stderr)
            if chosen is None:
                return 3
            sys.stdout.buffer.write(canonical({**chosen, "ring": "stable", "promoted": iso(now_utc())}))
        elif a.command == "control":
            sys.stdout.buffer.write(canonical(control(a.paused == "true")))
        return 0
    except (ReleaseError, OSError, ValueError, subprocess.CalledProcessError) as e:
        print(f"glaeda_release: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
