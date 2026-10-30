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
# Runners per member and the weighted capacity units they share (glaeda-cmux-runner-hook): units are cores, so
# capacityUnits is the member's core count and a job costs the cores it is estimated to use (a compile 5, an
# isolated job 4, a GUI job 1: the hook's CLASS_COST). The manifest's defaults.runner.classes.<class>
# {runners, capacityUnits} overrides these, and a host's overrides.runner.classes likewise. compileSlots
# (default 1) is how many compiles run at once: more than one needs cmux's compile admission to keep its state
# per runner.
# canonicalRoots (default 1) is how many canonical roots (/private/tmp/cmux-ci, -2, ...) the mini has: that
# many runners (instances 0 to canonicalRoots - 1) also carry the root pool label (root_label), and the rest
# carry the side pool label (side_label) instead. guiRunners (0 to runners - canonicalRoots, default 0) makes the
# mini's last runners its gui-role runners. The last of them is the console gui runner: it carries the gui pool
# label (gui_label), so GitHub hands each mini at most one GUI job, the one its single gui token allows, and a
# second GUI job waits in GitHub's queue for another mini's gui runner instead of on a root runner here
# (cmuxterm-hq#661). The other gui-role runners (session runners) carry only the session pool label (session_label),
# for jobs that need a logged-in session but not the console token; the console runner carries it too only with
# guiRunners 1, when it has no session runner beside it. None is a root or side runner.
# fastRunners (0 or 1, default 0) makes the mini's last runner its fast runner: it carries only the fast pool
# label (fast_label), and cmux-next's fast-tier job runs on it with the mini's two fast tokens instead of the
# shared capacity units, so it never queues behind the side lane. With guiRunners as well, the gui-role runners
# are the instances before it. Neither a root, side nor gui runner.
# Defaults for a class the manifest does not size: std is an M4 Pro mini (14 cores), light an M4 mini (10); xl kept
# its old 8 units at the old 2.8 cores a unit.
CLASS_CAPACITY = {"xl": (8, 22), "std": (4, 14), "light": (2, 10)}
# gatewayRunners (0 to MAX_GATEWAY_RUNNERS, default 0) adds that many gateway runners after the member's `runners`
# instances (instances runners to runners + gatewayRunners - 1; never counted in runners, so no role moves). A gateway
# runner carries only the gateway label (gateway_label: glaeda-gateway-xcode-<version>, no class, no pool label) and
# takes only cmux's thin cmux-ci client jobs (glaeda-cmux-runner-hook GATEWAY_JOBS): they submit to the cmux-ci
# controller and wait, with about no local CPU. Its gate listens through CPU saturation and a full mini (units) and
# holds for memory pressure, the disk floor and the fleet's host claim; its job takes one of the GATEWAY_TOKENS and no
# shared unit (the hook's gateway class). On 2026-10-30 at 12:43Z every std mini sat at 76 to 100% CPU and the side
# label cmux used for these clients had 1 idle of 51 listeners, so no client could start and the build-fleet compiles
# they submit did not run. At most MAX_GATEWAY_RUNNERS per mini (the hook's two tokens); never on a trusted member,
# which takes no pull request job, and only on a dedicated one (an opportunistic member carries no pool label).
MAX_GATEWAY_RUNNERS = 2
GATEWAY_PREFIX = "gateway"
COMPILE_CORES = 5  # the hook's CLASS_COST["compile"] units (a test keeps them equal)
MAX_RUNNERS = 16
# A host's overrides.runner {trustedRef: refs/heads/main, trustedRepo: owner/name} makes its runners trusted-only:
# the hook admits only push and schedule jobs of that one repository on that ref, and the pool label is
# glaeda-trusted-<class>-xcode-<version>, so a PR run's picker never counts or routes to them. For a
# mini that holds a secret, such as the fleet-cas signing key.
TRUSTED_REF_RE = r"refs/heads/[A-Za-z0-9._/-]+"
REPO_RE = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
POOL_PREFIX_RE = r"[a-z0-9]+-"
RESERVED_POOL_PREFIXES = {"root-", "side-", "gui-", "fast-", "trusted-", "std-", "light-", "xl-", "dev-", "nightly-",
                          "gateway-"}
DEV_COMPILE_POOL_PREFIX = "dev-"
# A host's runner.nightlySlot: true gives its next side instance after canonicalRoots (after the freshness slot when
# devCompile is set too) the nightly pool label glaeda-nightly-<class>-xcode-<version> instead of its side label. Only
# cmux's nightly jobs (glaeda-cmux-runner-hook --nightly-runner: nightly.yml on nightly-next or main, and the Release
# compile that gates nightly-next's promotion) run there. GitHub hands a queued job to the next idle runner in arrival
# order, so a nightly job queued on a side label waited behind every PR job queued there before it (2026-10-27
# 14:08Z: a nightly-next build-nightly-app on the light side label was never acquired by a runner while 37 side
# runners ran PR jobs). No PR job carries this label, so the nightly job is the only one this runner can take; it
# listens on a full mini, and its job waits there first in line (FEED_FIRST) while no other job takes a unit.
NIGHTLY_POOL_PREFIX = "nightly-"


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


def side_classes(klass: str) -> tuple[str, ...]:
    """The classes whose side label a `klass` runner carries: its own, then every narrower class.

    RUNNER_CLASSES is widest first, and a wider mini runs whatever a narrower one runs (a std M4 Pro, 48 GB, runs a
    light M4's side-lane job; never the reverse). So a std side runner also carries glaeda-side-light-xcode-<v>, and
    GitHub's own queue hands a job asking for the light side label to whichever of those runners is idle: a starved
    light pool spills onto idle std side runners for every workflow, picker or not. On 2026-10-20 at 22:22Z the light
    side label had 133 jobs queued on 3 online runners while 51 std side runners sat idle. Only side labels widen:
    root, gui, session and fast labels keep their own class (canonical roots, the gui token and the fast tokens are
    per class), and the pool namespace and trust stay the member's own (pool_label's arguments).
    """
    return RUNNER_CLASSES[RUNNER_CLASSES.index(klass):] if klass in RUNNER_CLASSES else ()


def widened_labels(labels: list[str]) -> list[str]:
    """`labels` plus every side label side_classes() adds to the side labels among them, in order, each once.

    The registered labels of a runner installed before side_classes() lack the narrower classes' side labels.
    A reconciler with the organization's runner-label permission (GitHub fixes labels at registration, and a
    mini holds no admin token) sets a runner's custom labels to this, so a manifest change reaches every
    registered runner without a relabel by hand. Pure and idempotent: widened_labels(widened_labels(x)) ==
    widened_labels(x), and it only adds a label member_labels() would itself issue for that runner.
    """
    out = list(dict.fromkeys(labels))
    for label in list(out):
        parts = label.split("-")
        index = next((i for i, part in enumerate(parts) if part in RUNNER_CLASSES), None)
        if (index is None or parts[0] != "glaeda" or "side" not in parts[1:index]
                or parts[index + 1:index + 2] != ["xcode"] or not re.fullmatch(VERSION_RE, "-".join(parts[index + 2:]))):
            continue
        pool = "-".join(part for i, part in enumerate(parts) if not (part == "side" and 0 < i < index))
        if side_label(pool) != label:
            continue
        for klass in side_classes(parts[index])[1:]:
            narrower = pool.split("-")
            narrower[narrower.index(parts[index])] = klass
            wanted = side_label("-".join(narrower))
            if wanted not in out:
                out.append(wanted)
    return out


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


def gateway_label(version: str) -> str:
    """The gateway runners' label for an Xcode version: glaeda-gateway-xcode-<version>. No class: a thin client runs
    the same on any mini, so one label spans every class's gateway runners."""
    return f"glaeda-{GATEWAY_PREFIX}-xcode-{version}"


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
    dev_compile = runner.get("devCompile", False) if isinstance(runner, dict) else False
    if not isinstance(dev_compile, bool):
        return None, f"{member} runner.devCompile must be boolean"
    if dev_compile and (klass != "std" or availability != "dedicated" or trusted_ref is not None):
        return None, f"{member} runner.devCompile needs a dedicated std runner without trustedRef"
    nightly_slot = runner.get("nightlySlot", False) if isinstance(runner, dict) else False
    if not isinstance(nightly_slot, bool):
        return None, f"{member} runner.nightlySlot must be boolean"
    if nightly_slot and (availability != "dedicated" or trusted_ref is not None):
        return None, f"{member} runner.nightlySlot needs a dedicated runner without trustedRef"
    pool_prefix = runner.get("poolPrefix", "") if isinstance(runner, dict) else ""
    prefix_ok = isinstance(pool_prefix, str) and (pool_prefix == "" or (
        re.fullmatch(POOL_PREFIX_RE, pool_prefix) and
        (pool_prefix not in RESERVED_POOL_PREFIXES or (dev_compile and pool_prefix == DEV_COMPILE_POOL_PREFIX))))
    if not prefix_ok:
        return None, f"{member} runner.poolPrefix must be empty or a lowercase prefix such as aws-"
    if dev_compile and pool_prefix not in ("", DEV_COMPILE_POOL_PREFIX):
        return None, f"{member} runner.devCompile uses poolPrefix {DEV_COMPILE_POOL_PREFIX}"
    if nightly_slot and pool_prefix not in ("", DEV_COMPILE_POOL_PREFIX):
        return None, f"{member} runner.nightlySlot needs the default pool namespace (no poolPrefix)"
    normal_pool_prefix = "" if dev_compile and pool_prefix == DEV_COMPILE_POOL_PREFIX else pool_prefix
    ready = [a for a in apps if re.fullmatch(VERSION_RE, str(a.get("version") or "")) and xcode_ok(a)]
    versions = [str(a["version"]) for a in ready]
    # Opportunistic members never carry a pool label, so they never receive a required job.
    pools = [pool_label(klass, v, bool(trusted_ref), normal_pool_prefix) for v in versions] if availability == "dedicated" else []
    dev_pools = ([pool_label(klass, v, False, DEV_COMPILE_POOL_PREFIX) for v in versions]
                 if dev_compile and availability == "dedicated" else [])
    nightly_pools = ([pool_label(klass, v, False, NIGHTLY_POOL_PREFIX) for v in versions]
                     if nightly_slot and availability == "dedicated" else [])
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
        gateway = declared.get("gatewayRunners", 0)
    else:
        compile_slots, roots, gui, fast, gateway = 1, 1, 0, 0, 0
    if not (isinstance(runners, int) and not isinstance(runners, bool) and 1 <= runners <= MAX_RUNNERS):
        return None, f"runner.classes.{klass}.runners must be 1 to {MAX_RUNNERS}"
    # every mini holds at least one compile slot, so it needs a compile's cores; glaeda-route checks the same
    if not (isinstance(units, int) and not isinstance(units, bool) and COMPILE_CORES <= units <= 4 * MAX_RUNNERS):
        return None, (f"runner.classes.{klass}.capacityUnits must be {COMPILE_CORES} to {4 * MAX_RUNNERS} (cores; "
                      f"a compile is {COMPILE_CORES})")
    if not (isinstance(compile_slots, int) and not isinstance(compile_slots, bool)
            and 1 <= compile_slots <= max(1, units // COMPILE_CORES)):
        return None, (f"runner.classes.{klass}.compileSlots must be 1 to {max(1, units // COMPILE_CORES)} "
                      f"(a compile is {COMPILE_CORES} units)")
    if not (isinstance(roots, int) and not isinstance(roots, bool) and 1 <= roots <= runners):
        return None, f"runner.classes.{klass}.canonicalRoots must be 1 to {runners} (one root runner per root)"
    if not (isinstance(gui, int) and not isinstance(gui, bool) and 0 <= gui <= runners - roots):
        return None, (f"runner.classes.{klass}.guiRunners must be 0 to {max(0, runners - roots)}, runners ({runners}) "
                      f"less canonicalRoots ({roots}): the gui runners are the last instances (before the fast "
                      "runner), never a root runner")
    if not (isinstance(fast, int) and not isinstance(fast, bool) and 0 <= fast <= 1 and roots + gui + fast <= runners):
        return None, (f"runner.classes.{klass}.fastRunners must be 0 or 1, and canonicalRoots plus guiRunners plus "
                      f"fastRunners at most runners ({runners}): the fast runner is the last instance")
    if not (isinstance(gateway, int) and not isinstance(gateway, bool) and 0 <= gateway <= MAX_GATEWAY_RUNNERS):
        return None, f"runner.classes.{klass}.gatewayRunners must be 0 to {MAX_GATEWAY_RUNNERS}"
    if gateway and (trusted_ref is not None or availability != "dedicated" or normal_pool_prefix):
        return None, (f"{member} runner.classes.{klass}.gatewayRunners needs a dedicated runner without trustedRef "
                      "(a gateway runner takes pull request jobs) in the default pool namespace (no poolPrefix)")
    if compile_slots > roots:
        return None, (f"runner.classes.{klass}.compileSlots {compile_slots} needs canonicalRoots {compile_slots} "
                      "(every compile holds a root)")
    if dev_compile and roots + gui + fast >= runners:
        return None, f"{member} runner.devCompile needs an existing side instance after canonicalRoots"
    if nightly_slot and roots + gui + fast + int(dev_compile) >= runners:
        return None, (f"{member} runner.nightlySlot needs an existing side instance after canonicalRoots"
                      + (" and the freshness slot" if dev_compile else ""))
    return {"member": member, "class": klass, "availability": availability, "roles": roles,
            "labels": list(dict.fromkeys(labels)), "pools": list(dict.fromkeys(pools)),
            "devPools": list(dict.fromkeys(dev_pools)), "nightlyPools": list(dict.fromkeys(nightly_pools)),
            "nightlySlot": nightly_slot, "nightlyInstance": roots + int(dev_compile) if nightly_slot else None,
            "minFreeGib": floor if isinstance(floor, (int, float)) and floor > 0 else None,
            "hardware": hardware, "xcodeApps": [str(a.get("path")) for a in ready], "iosSim": ios_sim,
            "runners": runners, "capacityUnits": units, "compileSlots": compile_slots,
            "trustedRef": trusted_ref, "trustedRepo": trusted_repo, "trustedNextNightly": next_nightly,
            "devCompile": dev_compile, "devCompileInstance": roots if dev_compile else None,
            "canonicalRoots": roots, "rootPools": [root_label(label) for label in dict.fromkeys(pools)],
            "sidePools": list(dict.fromkeys(side_label(pool_label(k, v, bool(trusted_ref), normal_pool_prefix))
                                            for v in (versions if pools else []) for k in side_classes(klass))),
            "guiRunners": gui, "guiPools": [gui_label(label) for label in dict.fromkeys(pools)] if gui else [],
            "sessionPools": [session_label(label) for label in dict.fromkeys(pools)] if gui else [],
            "fastRunners": fast, "fastPools": [fast_label(label) for label in dict.fromkeys(pools)] if fast else [],
            "gatewayRunners": gateway,
            "gatewayPools": list(dict.fromkeys(gateway_label(v) for v in (versions if pools else []))) if gateway
            else []}, None


def declared_pools(manifest: Any, xcode_ok: Callable[[str, dict[str, Any]], bool] | None = None) -> dict[str, list[str]]:
    """{pool label: [members]}. With no xcode_ok, every manifest Xcode counts (the declared view);
    pass xcode_ok(member, app) with observed results for the conforming view."""
    out: dict[str, list[str]] = {}
    hosts = manifest.get("hosts") if isinstance(manifest, dict) else None
    for member in hosts if isinstance(hosts, dict) else {}:
        check = (lambda app, m=member: xcode_ok(m, app)) if xcode_ok else (lambda app: True)
        summary, _ = member_labels(manifest, member, check)
        for label in [*(summary or {}).get("pools", []), *(summary or {}).get("devPools", []),
                      *(summary or {}).get("nightlyPools", [])]:
            out.setdefault(label, []).append(member)
    return {label: sorted(members) for label, members in sorted(out.items())}


# A host's runner.dedicated list (in its overrides, or the defaults) declares runners that are not pool instances: a
# builder that takes one job shape, such as cmux's nightly-next app build or the iOS internal archive. glaeda-cmux-runner
# --dedicated NAME installs each one like an instance (glaeda's release, .path, hooks, gate, disk floor, OTA hook
# refresh), but with exactly the labels listed (no self-hosted, macOS or pool label, so no other job can land on it),
# its own repo or org scope, and its own admission: the job-started hook runs glaeda's checks, then the entry's admit
# command, and refuses the job when that fails. watch, when given, is the builder's own loop, run as a glaeda
# LaunchAgent beside the runner. env goes into the runner's .env and the watch loop's environment; "{runnerDir}" in a
# value is the runner's directory. A leading "~/" in a command word or env value is the runner user's home.
DEDICATED_NAME_RE = r"[a-z][a-z0-9-]{0,39}"  # starts with a letter: never read as a pool instance number
DEDICATED_POOL_SUFFIX_RE = r"(?:^|-)glaeda(?:-\d+)?$"  # <member>-glaeda-<name> would read as another member's instance
RUNNER_LABEL_RE = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
ENV_NAME_RE = r"[A-Z_][A-Z0-9_]{0,63}"
DEFAULT_LABELS = ("self-hosted", "macos", "arm64", MINI_LABEL)


# Where a member's pool instances register (glaeda-cmux-runner-fleet, when no --org/--repo is passed): the manifest's
# runner.org and runner.group (defaults, or a host's overrides), so a re-apply keeps the scope the pools already have
# and nobody passes flags by hand. A trusted-only member (runner.trustedRepo) stays on that repository unless its own
# overrides name runner.org (a trusted runner registered in the org's group). Neither set:
# None, and the fleet tool falls back to its default repository as before.
GROUP_RE = r"[A-Za-z0-9][A-Za-z0-9 ._-]*"
ORG_RE = r"[A-Za-z0-9][A-Za-z0-9-]*"


def pool_scope(manifest: Any, member: str) -> tuple[dict[str, str | None] | None, str | None]:
    """({"org", "group"} or {"repo"}, None), (None, None) when the manifest names no scope, or (None, why) when the
    member's runner.org/runner.group are unusable. Pure."""
    hosts = manifest.get("hosts") if isinstance(manifest, dict) else None
    host = hosts.get(member) if isinstance(hosts, dict) else None
    if not isinstance(host, dict):
        return None, f"{member} is not a member in the manifest"
    defaults, overrides = manifest.get("defaults") or {}, host.get("overrides") or {}
    if not isinstance(defaults, dict) or not isinstance(overrides, dict):
        return None, f"manifest defaults or {member} overrides is not an object"
    runner = merge(defaults, overrides).get("runner")
    runner = runner if isinstance(runner, dict) else {}
    org, group, trusted = runner.get("org"), runner.get("group"), runner.get("trustedRepo")
    if org is not None and not (isinstance(org, str) and re.fullmatch(ORG_RE, org)):
        return None, f"{member} runner.org must be an organization login"
    if group is not None and (org is None or not (isinstance(group, str) and re.fullmatch(GROUP_RE, group))):
        return None, f"{member} runner.group must be a runner group name and goes with runner.org"
    if trusted is not None and not (isinstance(trusted, str) and re.fullmatch(REPO_RE, trusted)):
        return None, f"{member} runner.trustedRepo must be owner/name"  # never fall back to the pools' scope
    own = (overrides.get("runner") or {}) if isinstance(overrides.get("runner"), dict) else {}
    if isinstance(trusted, str) and re.fullmatch(REPO_RE, trusted) and own.get("org") is None:
        return {"repo": trusted}, None  # the defaults' org is for the pools; the host's own org says otherwise
    if org is None:
        return None, None
    return {"org": org, "group": group}, None


def dedicated_runners(manifest: Any, member: str) -> tuple[list[dict[str, Any]], str | None]:
    """([{name, repo, org, group, labels, admit, watch, env, keep, minFreeGib}], None), or ([], why the member's list
    is unusable). Pure. A member with no runner.dedicated has none. minFreeGib is the entry's own disk floor, or None
    for the member's disk.min_free_gib."""
    hosts = manifest.get("hosts") if isinstance(manifest, dict) else None
    host = hosts.get(member) if isinstance(hosts, dict) else None
    if not isinstance(host, dict):
        return [], f"{member} is not a member in the manifest"
    defaults, overrides = manifest.get("defaults") or {}, host.get("overrides") or {}
    if not isinstance(defaults, dict) or not isinstance(overrides, dict):
        return [], f"manifest defaults or {member} overrides is not an object"
    runner = merge(defaults, overrides).get("runner")
    entries = runner.get("dedicated", []) if isinstance(runner, dict) else []
    if not isinstance(entries, list):
        return [], f"{member} runner.dedicated must be a list"
    if entries and host.get("gui") is False:
        # a dedicated runner is a LaunchAgent in the user's GUI session; the headless supervisor is not wired for it
        return [], f"{member} has gui: false; runner.dedicated runs only on a member with a GUI session"
    out: list[dict[str, Any]] = []
    pools: set[str] | None = None  # every pool label the manifest declares, read once when there is an entry
    for index, entry in enumerate(entries):
        where = f"{member} runner.dedicated[{index}]"
        if not isinstance(entry, dict):
            return [], f"{where} is not an object"
        unknown = sorted(set(entry) - {"name", "repo", "org", "group", "labels", "admit", "watch", "env", "keep",
                                       "minFreeGib", "note"})
        if unknown:
            return [], f"{where} has unknown keys: {', '.join(unknown)}"
        name, repo, org, group = entry.get("name"), entry.get("repo"), entry.get("org"), entry.get("group")
        if not isinstance(name, str) or not re.fullmatch(DEDICATED_NAME_RE, name):
            return [], f"{where}.name must match {DEDICATED_NAME_RE}"
        if re.search(DEDICATED_POOL_SUFFIX_RE, name):
            return [], f"{where}.name must not end in glaeda or glaeda-<n> (it would read as a pool instance)"
        if name in {d["name"] for d in out}:
            return [], f"{where}.name {name} is declared twice"
        if (repo is None) == (org is None):
            return [], f"{where} needs exactly one of repo (owner/name) and org"
        if repo is not None and not (isinstance(repo, str) and re.fullmatch(REPO_RE, repo)):
            return [], f"{where}.repo must be owner/name"
        if org is not None and not (isinstance(org, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", org)):
            return [], f"{where}.org must be an organization login"
        if group is not None and (org is None or not isinstance(group, str) or not group.strip()):
            return [], f"{where}.group goes with org"
        labels = entry.get("labels")
        if not (isinstance(labels, list) and labels and all(isinstance(x, str) and re.fullmatch(RUNNER_LABEL_RE, x)
                                                            for x in labels)):
            return [], f"{where}.labels must be a non-empty list of runner labels"
        if len(set(labels)) != len(labels) or any(x.lower() in DEFAULT_LABELS for x in labels):
            return [], (f"{where}.labels must be distinct and leave out {', '.join(DEFAULT_LABELS)}: a dedicated "
                        "runner takes only the jobs that name its own labels")
        if pools is None:
            pools = {label.lower() for label in declared_pools(manifest)}
        shared = sorted(x for x in labels if x.lower() in pools)
        if shared:
            return [], (f"{where}.labels must leave out the manifest's pool labels ({', '.join(shared)}): a pool job "
                        "would land on the builder, outside pool capacity and reservations")
        commands = {}
        for key in ("admit", "watch"):
            argv = entry.get(key)
            if argv is None and key == "watch":
                continue
            if not (isinstance(argv, list) and argv and all(isinstance(w, str) and w and "\n" not in w and "\0" not in w
                                                           for w in argv)
                    and (argv[0].startswith("/") or argv[0].startswith("~/"))):
                return [], f"{where}.{key} must be a command (a list of words, the first an absolute or ~/ path)"
            commands[key] = list(argv)
        env = entry.get("env", {})
        if not (isinstance(env, dict) and all(isinstance(k, str) and re.fullmatch(ENV_NAME_RE, k) and isinstance(v, str)
                                              and "\n" not in v and "\0" not in v for k, v in env.items())):
            return [], f"{where}.env must map NAME to a one-line string"
        keep = entry.get("keep", [])
        if not (isinstance(keep, list) and all(isinstance(k, str) and k and "\n" not in k and "\0" not in k
                                               and (k.startswith("~/") or k.startswith("{runnerDir}/"))
                                               and ".." not in k.split("/") for k in keep)):
            return [], (f"{where}.keep must list paths under ~/ or {{runnerDir}}/ (no ..): the warm state glaeda-disk "
                        "keeps by role")
        # The builder's own disk floor: the runner's --min-free-gib, so the listener gate holds the runner off under
        # it (the job goes elsewhere) instead of the builder's admission refusing a job the runner already took.
        floor = entry.get("minFreeGib")
        # the range check alone (no float conversion): it refuses nan and inf, and a huge int never overflows
        if floor is not None and (isinstance(floor, bool) or not isinstance(floor, (int, float))
                                  or not 0 < floor <= 10000):
            return [], f"{where}.minFreeGib must be a number of GiB above 0 and at most 10000"
        out.append({"name": name, "repo": repo, "org": org, "group": group, "labels": list(labels),
                    "admit": commands["admit"], "watch": commands.get("watch"), "env": dict(env),
                    "keep": list(keep), "minFreeGib": None if floor is None else float(floor)})
    return out, None
