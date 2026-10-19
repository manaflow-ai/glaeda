"""glaeda_fleet_labels: the one rule that turns a fleet manifest member into GitHub runner labels.

cmuxterm-hq build-fleet/FLEET-MEMBERSHIP.md makes the manifest the only source of a member's
behaviour. glaeda-cmux-runner issues these labels at install time, and glaeda-mini-fleet counts
members per pool label, so both import this module instead of keeping their own copy.

Pure: no I/O. Whether an Xcode is really installed is the caller's question (xcode_ok), because
the runner checks the machine it runs on and glaeda-mini-fleet checks what it observed over SSH.
"""

from __future__ import annotations

import re
from typing import Any, Callable

MINI_LABEL = "glaeda-mini"
# A host with the ios-simulators role, verified on the mini: every runtime build the manifest's ios_simulator.runtimes
# declares (23F77) is available, because the pinned Xcode refuses any other iOS SDK's simulators. With nothing
# declared, the member carries no label.
IOS_SIM_LABEL = "glaeda-ios-sim"
IOS_SIM_ROLE = "ios-simulators"
# The macOS major version the runner's host really runs (sw_vers on the mini), so a job can ask for one OS when a
# fleet straddles an upgrade: glaeda-macos-27. Never the bare macos-<N>, which is GitHub-hosted runner vocabulary
# that cmux's workflows treat as "must be GitHub-hosted". The caller observes the version; without one, no label.
MACOS_LABEL_PREFIX = "glaeda-macos-"
MACOS_MAJOR_RE = r"[1-9][0-9]*"
# dev machines take no jobs; borrowed machines run jobs only inside a VM, never as a host runner.
RUNNER_CLASSES = ("xl", "std", "light")
MEMBER_CLASSES = RUNNER_CLASSES + ("dev", "borrowed")
AVAILABILITY = ("dedicated", "opportunistic")
RUNNER_ROLE = "ci-runner"
VERSION_RE = r"[0-9][0-9.]*"
# Runners per member and the weighted capacity units they share (glaeda-cmux-runner-hook): a compile is
# 2 units, a light or GUI job 1. The manifest's defaults.runner.classes.<class> {runners, capacityUnits}
# overrides these, and a host's overrides.runner.classes likewise. compileSlots (default 1) is how many
# compiles run at once: more than one needs cmux's compile admission to keep its state per runner.
# canonicalRoots (default 1) is how many canonical roots (/private/tmp/cmux-ci, -2, ...) the mini has: that
# many runners (instances 0 to canonicalRoots - 1) also carry the root pool label (root_label), and the rest
# carry the side pool label (side_label) instead. guiRunners (0 to runners - canonicalRoots, default 0) makes the
# mini's last runners its gui-role runners. The last of them is the console gui runner: it carries the gui pool
# label (gui_label), so GitHub hands each mini at most one GUI job, the one its single gui token allows, and a
# second GUI job waits in GitHub's queue for another mini's gui runner instead of on a root runner here
# (cmuxterm-hq#661). Every gui-role runner carries the session pool label (session_label), for jobs that need a
# logged-in session but not the console token, and the others carry only that one. None is a root or side runner.
# fastRunners (0 or 1, default 0) makes the mini's last runner its fast runner: it carries only the fast pool
# label (fast_label), and cmux-next's fast-tier job runs on it with the mini's two fast tokens instead of the
# shared capacity units, so it never queues behind the side lane. With guiRunners as well, the gui-role runners
# are the instances before it. Neither a root, side nor gui runner.
CLASS_CAPACITY = {"xl": (8, 8), "std": (4, 4), "light": (2, 2)}
MAX_RUNNERS = 16
# A host's overrides.runner {trustedRef: refs/heads/main, trustedRepo: owner/name} makes its runners trusted-only:
# the hook admits only push and schedule jobs of that one repository on that ref, and the pool label is
# glaeda-trusted-<class>-xcode-<version>, so a PR run's picker never counts or routes to them. For a
# mini that holds a secret, such as the fleet-cas signing key.
TRUSTED_REF_RE = r"refs/heads/[A-Za-z0-9._/-]+"
REPO_RE = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
POOL_PREFIX_RE = r"[a-z0-9]+-"
RESERVED_POOL_PREFIXES = {"root-", "side-", "gui-", "fast-", "trusted-", "std-", "light-", "xl-"}


def merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """The deep merge glaeda-mini-fleet uses for a host's overrides over the manifest defaults."""
    out = dict(base)
    for key, value in over.items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def pool_label(klass: str, version: str, trusted: bool = False, pool_prefix: str = "") -> str:
    """The single label a pool picker routes a whole run to: class plus verified Xcode, dedicated only."""
    return f"glaeda-{pool_prefix}{'trusted-' if trusted else ''}{klass}-xcode-{version}"


def macos_label(major: str) -> str:
    """The OS label for a macOS major version: glaeda-macos-27."""
    return f"{MACOS_LABEL_PREFIX}{major}"


def root_label(pool: str) -> str:
    """The label of a pool's root runners (instances below canonicalRoots): the jobs that build in or restore
    into the canonical root (compile, app-host shards, cli-product, lag) run on it, so GitHub queues them
    until a root runner is free instead of handing one to a runner whose mini has no free root."""
    return _role_label(pool, "root")


def side_label(pool: str) -> str:
    """The label of a pool's non-root runners (instances from canonicalRoots on). A light side lane (no
    canonical root, no persistent state, no GUI) runs on it, so it never takes a root runner that a compile
    or app-host job is waiting for. A mini whose every runner is a root runner has none."""
    return _role_label(pool, "side")


def gui_label(pool: str) -> str:
    """The label of a pool's console gui runner (the last gui-role instance). The GUI jobs (app-host shards,
    tests-build-and-lag, app-host-test-rerun) run on it: each needs the mini's one gui token (one console session),
    so one runner per mini carries the label, and its listener gate holds while the token or every root is taken."""
    return _role_label(pool, "gui")


def session_label(pool: str) -> str:
    """The label of a pool's gui-role runners (guiRunners of them, the console gui runner included). Jobs that need
    the mini's logged-in session but not its console token (cmux-next daemon tests, generated files, action
    exports, the pr-media tour) run on it, so on a mini with two gui runners they run beside a console job. The
    gate of a session-only runner holds only for units (glaeda-cmux-runner-hook --session-runner)."""
    return _role_label(pool, "session")


def fast_label(pool: str) -> str:
    """The label of a pool's fast runner (the last instance when fastRunners is 1): cmux-next's fast-tier job
    runs on it, admitted against the mini's fast tokens rather than its shared units (glaeda-cmux-runner-hook's
    fast class), so it never waits behind side-lane jobs for a unit."""
    return _role_label(pool, "fast")


def _role_label(pool: str, role: str) -> str:
    """Insert a runner role after an optional pool namespace and before class metadata."""
    parts = pool.split("-")
    try:
        class_index = next(index for index, part in enumerate(parts) if part in RUNNER_CLASSES)
    except StopIteration:
        return pool.replace("glaeda-", f"glaeda-{role}-", 1)
    # `trusted` is part of the role-free label; a namespace such as `aws` is
    # part of the pool family and stays before the role.
    insert_at = 1 if class_index > 1 and parts[1] == "trusted" else min(2, class_index)
    parts.insert(insert_at, role)
    return "-".join(parts)


def xcode_apps(manifest: dict[str, Any], member: str) -> list[dict[str, Any]] | None:
    """The member's Xcode apps (defaults merged with its overrides), or None if the shape is wrong."""
    host = manifest["hosts"][member]
    defaults, overrides = manifest.get("defaults") or {}, host.get("overrides") or {}
    if not isinstance(defaults, dict) or not isinstance(overrides, dict):
        return None
    xcode = merge(defaults, overrides).get("xcode") or {}
    apps = xcode.get("apps") if isinstance(xcode, dict) else None
    return [a for a in apps if isinstance(a, dict)] if isinstance(apps, list) else []


def member_labels(manifest: Any, member: str, xcode_ok: Callable[[dict[str, Any]], bool],
                  ios_sim_ok: Callable[[list[dict[str, Any]], list[str]], bool] | None = None,
                  macos_major: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """(member summary with labels, None) or (None, why this member cannot be a runner).
    macos_major: the observed macOS major version of the host ("27"); it adds glaeda-macos-<major>. None (the
    declared view, or a host that could not tell) adds no OS label."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("hosts"), dict):
        return None, "manifest has no hosts table"
    host = manifest["hosts"].get(member)
    if not isinstance(host, dict):
        return None, f"{member} is not a member in the manifest"
    klass, availability = host.get("class"), host.get("availability")
    roles = host.get("roles") if isinstance(host.get("roles"), list) else []
    if klass not in MEMBER_CLASSES:
        return None, f"{member} class {klass!r} is not one of {', '.join(MEMBER_CLASSES)}"
    if klass not in RUNNER_CLASSES:
        return None, f"{member} is class {klass}, which never runs a GitHub runner on the host"
    if availability not in AVAILABILITY:
        return None, f"{member} availability {availability!r} is not one of {', '.join(AVAILABILITY)}"
    if RUNNER_ROLE not in roles:
        return None, f"{member} roles do not include {RUNNER_ROLE}"
    hardware = host.get("hardware")
    if not isinstance(hardware, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", hardware):
        return None, f"{member} has no hardware class, which names its fleet class receipt"
    apps = xcode_apps(manifest, member)
    if apps is None:
        return None, f"manifest defaults or {member} overrides is not an object"
    merged = merge(manifest.get("defaults") or {}, host.get("overrides") or {})
    runner = merged.get("runner")
    trusted_ref = runner.get("trustedRef") if isinstance(runner, dict) else None
    if trusted_ref is not None and not (isinstance(trusted_ref, str) and re.fullmatch(TRUSTED_REF_RE, trusted_ref)):
        return None, f"{member} runner.trustedRef must be a branch ref such as refs/heads/main"
    trusted_repo = runner.get("trustedRepo") if isinstance(runner, dict) else None
    if (trusted_ref is None) != (trusted_repo is None) or (
        trusted_repo is not None and not (isinstance(trusted_repo, str) and re.fullmatch(REPO_RE, trusted_repo))):
        return None, f"{member} runner.trustedRef and runner.trustedRepo (owner/name) go together"
    next_nightly = runner.get("trustedNextNightly", False) if isinstance(runner, dict) else False
    if not isinstance(next_nightly, bool) or (next_nightly and (
            trusted_ref != "refs/heads/main" or trusted_repo != "manaflow-ai/cmux"
            or availability != "dedicated" or "dev-builds" in roles)):
        return None, (f"{member} runner.trustedNextNightly must be boolean and true needs a dedicated "
                      "manaflow-ai/cmux trusted main runner with no dev-builds")
    pool_prefix = runner.get("poolPrefix", "") if isinstance(runner, dict) else ""
    if not (isinstance(pool_prefix, str) and (pool_prefix == "" or (
            re.fullmatch(POOL_PREFIX_RE, pool_prefix) and pool_prefix not in RESERVED_POOL_PREFIXES))):
        return None, f"{member} runner.poolPrefix must be empty or a lowercase prefix such as aws-"
    ready = [a for a in apps if re.fullmatch(VERSION_RE, str(a.get("version") or "")) and xcode_ok(a)]
    versions = [str(a["version"]) for a in ready]
    # Opportunistic members never carry a pool label, so they never receive a required job.
    pools = [pool_label(klass, v, bool(trusted_ref), pool_prefix) for v in versions] if availability == "dedicated" else []
    # Declared (the ios-simulators role) and, when the caller can look (the installer on the mini), a simulator
    # runtime actually present: an iOS job routed here must be able to boot one. Without a check (the fleet
    # plan) this is the declared view.
    ios = merged.get("ios_simulator")
    builds = ios.get("runtimes") if isinstance(ios, dict) else None
    builds = [b for b in builds if isinstance(b, str) and b] if isinstance(builds, list) else []
    ios_sim = (IOS_SIM_ROLE in roles and bool(ready) and bool(builds)
               and (ios_sim_ok is None or ios_sim_ok(ready, builds)))
    labels = [MINI_LABEL, f"glaeda-class-{klass}", f"glaeda-{availability}",
              *(["glaeda-trusted"] if trusted_ref else []),
              *(["glaeda-trusted-next-nightly"] if next_nightly else []),
              *[f"xcode-{v}" for v in versions], *pools,
              *([IOS_SIM_LABEL] if ios_sim else []),
              *([macos_label(macos_major)] if isinstance(macos_major, str)
                and re.fullmatch(MACOS_MAJOR_RE, macos_major) else [])]
    disk = merged.get("disk")
    floor = disk.get("min_free_gib") if isinstance(disk, dict) else None
    runners, units = CLASS_CAPACITY[klass]
    declared = ((runner.get("classes") or {}).get(klass) if isinstance(runner, dict)
                and isinstance(runner.get("classes"), dict) else None)
    if declared is not None:
        if not isinstance(declared, dict):
            return None, f"runner.classes.{klass} is not an object"
        runners, units = declared.get("runners", runners), declared.get("capacityUnits", units)
        compile_slots = declared.get("compileSlots", 1)
        roots = declared.get("canonicalRoots", 1)
        gui = declared.get("guiRunners", 0)
        fast = declared.get("fastRunners", 0)
    else:
        compile_slots, roots, gui, fast = 1, 1, 0, 0
    if not (isinstance(runners, int) and not isinstance(runners, bool) and 1 <= runners <= MAX_RUNNERS):
        return None, f"runner.classes.{klass}.runners must be 1 to {MAX_RUNNERS}"
    if not (isinstance(units, int) and not isinstance(units, bool) and 2 <= units <= 4 * MAX_RUNNERS):
        return None, f"runner.classes.{klass}.capacityUnits must be 2 to {4 * MAX_RUNNERS} (a compile is 2)"
    if not (isinstance(compile_slots, int) and not isinstance(compile_slots, bool)
            and 1 <= compile_slots <= max(1, units // 2)):
        return None, f"runner.classes.{klass}.compileSlots must be 1 to {max(1, units // 2)} (a compile is 2 units)"
    if not (isinstance(roots, int) and not isinstance(roots, bool) and 1 <= roots <= runners):
        return None, f"runner.classes.{klass}.canonicalRoots must be 1 to {runners} (one root runner per root)"
    if not (isinstance(gui, int) and not isinstance(gui, bool) and 0 <= gui <= runners - roots):
        return None, (f"runner.classes.{klass}.guiRunners must be 0 to {max(0, runners - roots)}, runners ({runners}) "
                      f"less canonicalRoots ({roots}): the gui runners are the last instances (before the fast "
                      "runner), never a root runner")
    if not (isinstance(fast, int) and not isinstance(fast, bool) and 0 <= fast <= 1 and roots + gui + fast <= runners):
        return None, (f"runner.classes.{klass}.fastRunners must be 0 or 1, and canonicalRoots plus guiRunners plus "
                      f"fastRunners at most runners ({runners}): the fast runner is the last instance")
    if compile_slots > roots:
        return None, (f"runner.classes.{klass}.compileSlots {compile_slots} needs canonicalRoots {compile_slots} "
                      "(every compile holds a root)")
    return {"member": member, "class": klass, "availability": availability, "roles": roles,
            "labels": list(dict.fromkeys(labels)), "pools": list(dict.fromkeys(pools)),
            "minFreeGib": floor if isinstance(floor, (int, float)) and floor > 0 else None,
            "hardware": hardware, "xcodeApps": [str(a.get("path")) for a in ready], "iosSim": ios_sim,
            "runners": runners, "capacityUnits": units, "compileSlots": compile_slots,
            "trustedRef": trusted_ref, "trustedRepo": trusted_repo, "trustedNextNightly": next_nightly,
            "canonicalRoots": roots, "rootPools": [root_label(label) for label in dict.fromkeys(pools)],
            "sidePools": [side_label(label) for label in dict.fromkeys(pools)],
            "guiRunners": gui, "guiPools": [gui_label(label) for label in dict.fromkeys(pools)] if gui else [],
            "sessionPools": [session_label(label) for label in dict.fromkeys(pools)] if gui else [],
            "fastRunners": fast, "fastPools": [fast_label(label) for label in dict.fromkeys(pools)] if fast else []}, None


def declared_pools(manifest: Any, xcode_ok: Callable[[str, dict[str, Any]], bool] | None = None) -> dict[str, list[str]]:
    """{pool label: [members]}. With no xcode_ok, every manifest Xcode counts (the declared view);
    pass xcode_ok(member, app) with observed results for the conforming view."""
    out: dict[str, list[str]] = {}
    hosts = manifest.get("hosts") if isinstance(manifest, dict) else None
    for member in hosts if isinstance(hosts, dict) else {}:
        check = (lambda app, m=member: xcode_ok(m, app)) if xcode_ok else (lambda app: True)
        summary, _ = member_labels(manifest, member, check)
        for label in (summary or {}).get("pools", []):
            out.setdefault(label, []).append(member)
    return {label: sorted(members) for label, members in sorted(out.items())}
