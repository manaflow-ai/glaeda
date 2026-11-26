# Dev-box disk and Projects hygiene

Glaeda uses one policy surface for fleet minis and developer machines. The role is explicit:

```text
glaeda-disk report --role devbox
glaeda-disk keepup --role devbox --apply
glaeda-projects tidy       # dry run
glaeda-projects tidy --apply
```

`scripts/glaeda-mini-setup --apply` installs these into `~/.local/bin` (`--hygiene-only` on a
developer machine, which defaults to the `devbox` role). A hygiene or `--gh-only` install with no
glaeda runner that was never a build host is `devbox` even where an earlier release recorded
`fleet-mini` for it (those releases migrated every role-less hygiene receipt to `fleet-mini` and kept
it); the next OTA setup run moves it. Only an explicit `--role fleet-mini`, kept in update.json's
setupArgs, keeps such a host on `fleet-mini`. A host moved this way gets the devbox disk policy but not
the daily Projects tidy agent, which moves checkouts; `--role devbox` opts it in.

`fleet-mini` retains the existing cmux hot-tier policy. `devbox` adds safe cleanup for stale
developer worktrees, `/tmp`, Xcode DerivedData, SwiftPM `.build`, Ghostty `.zig-cache`, and
other per-checkout build outputs. Developer worktrees and source checkouts remain report-only;
this policy does not remove user source or Git state. Package caches are pressure-only. Codex transcripts and their
archives remain report-only so Codex task history is preserved. Every candidate is rechecked for open files, a live cwd, command-line use,
socket markers, recent activity, Git state, and submodule state immediately before mutation.

Where cmux acts, `devbox` stands down. When the cmux setting `worktrees.cleanBuildCaches` is on, cmux's
worktree care removes the machine's idle build caches itself, so glaeda-disk keeps every item at or inside
`~/Library/Developer/Xcode/DerivedData`, `~/Library/Caches/org.swift.swiftpm`, `~/.npm/_cacache` and
`~/.bun/install/cache`. Each such item is `kept` with the reason "cmux cleans it", the report's first lines say
`cmux acts here`, and `--json` carries `"cmux_acts": true`. glaeda-disk reads the setting from the same file
cmux reads: `CMUX_NEXT_CONFIG_FILE`, else `~/.config/cmux/cmux-next.json`, then `~/.config/cmux/cmux.json`. A
missing or unreadable file reads as off, and glaeda-disk then acts. A `fleet-mini` host never stands down.

New checkouts go through `~/.local/bin/glaeda-worktree add`. `--no-build` creates the source
worktree without initializing submodules. `--build` initializes Ghostty with `--reference` to
the canonical checkout's module objects. Worktrees live under `~/Projects/worktrees/<repo>/<name>`.

`glaeda-projects tidy` reports older linked worktrees, redundant clean pushed clones, and scratch
folders that can move to the canonical layout. It skips live processes, recent paths, and recent
Codex sessions whose cwd names the checkout. Submodule worktrees are status-checked recursively;
if Git refuses to move one, the planner leaves it in place with the metadata and index intact.
The default is report-only;
the compatibility symlink step runs only after every child of a legacy `<repo>-worktrees` folder
has moved successfully.

macOS installs a pressure/idle LaunchAgent and a daily devbox Projects tidy agent. Linux installs
the equivalent user systemd units. Reports and apply receipts remain under the owning user's
Glaeda state directories.

## Undeclared growth (the checker)

On a fleet mini, `glaeda-disk report` reads `config/disk-registry.toml` (through `scripts/glaeda-disk-registry`,
the copy beside it or in the installed release) and prints, per volume, `used - [base] - registered caches - live
cmux-job-* volumes` as undeclared growth with its top paths, taken from rows the accounting already measured. Past
the registry's `[checker] undeclared_alarm_gib` (5 GiB; a `[hosts.<name>.checker]` table overrides it) it is the
`undeclared_growth` finding in `summary.json`, which `glaeda-mini-health` raises. It deletes nothing: the fix is a
registry entry or a scratch redirect, never a new deletion rule. `GLAEDA_DISK_CHECKER=0` turns it off, `=1` runs it
off a mini. The family rules stay as the fallback until undeclared growth stays near zero on every host for a week,
then they are deleted (cmuxterm-hq `docs/design/declared-disk-persistence.md`, section 3).

## Read-only checkout evidence

Two compiled tools (`src/bin/`) answer exact questions about a checkout without catalog,
adoption, residency, or mutation authority:

```bash
glaeda-project-observe --checkout /absolute/canonical/checkout --output json
glaeda-repo-query --checkout /absolute/canonical/checkout --project github.com/OWNER/REPO \
  --base COMPLETE_BASE_OID --head COMPLETE_HEAD_OID --tree COMPLETE_HEAD_TREE_OID
```

`glaeda-project-observe` runs the bounded offline checkout observer once, with Git credentials,
hooks, lazy fetch, replacement objects and ambient configuration disabled, and reports exact
commit and tree, the canonical GitHub source when provable, dirty, untracked and local-only work,
and worktree and submodule topology. `glaeda-repo-query` (`repo-query/v1`) returns object and
merge-base identity, ancestry, per-file numstat, a patch digest, and the patch itself only when it
fits the caller's ceiling; optional fields add exact-tree grep, bounded blob reads, path history
and object lookups. Neither accepts refs or arbitrary Git arguments, fetches, mutates, or grants
reuse authority. See the [resident repo-query
result](experiments/resident-repo-query-big-red-2026-08-31.md).

## Developer state never reaches hostile jobs

Developer checkouts and caches are trusted operator state. They are never mounted into a
hostile or disposable CI job, developer credentials are never inherited by one, and a surviving
checkout, cache, alias or directory name never proves project ownership or verification
authority. The older catalog and `glaeda project enter` design (issue #372) was never built and
is retired; recover it from Git history if that work resumes.

## Appcast extracts and legacy home trees

`Sparkle_generate_appcast` has its own `sparkle-appcast` family: one generated extraction per item,
six hours idle, checked for live processes again before removal. The generic Library cache family skips it.
The next appcast generation re-extracts the published app; deep bundle contents are generated build output.

On an enrolled fleet mini, `cmux-ci-perf`, `agent-work`, `toolchain-quarantine-*` and `fun` follow the
existing home-scratch policy, with no name-based deletion exception. No top-level `.git` does not mean no Git
work: nested repositories must be readable and archived before the idle tree can go. Unreadable refs keep the
tree and the receipt now names the nested git directory for owner recovery. Configuration references, live
processes, sockets, mounts and the post-archive rechecks still veto deletion.

## Tip-loop work on CI Macs

The `hq-tip-loop-work` family owns only `~/Library/Application Support/hq-tip-loop/state/work/*`
on `fleet-mini` Macs. Work idle for the existing one-hour fleet window may be reclaimed; deep writes refresh its age, live
processes/open files keep it, and unavailable evidence preserves it. Existing mirror archival protects
unpushed Git work. Sibling candidates, receipts and delivery outbox are outside the cleanup root.

Ultra2 held 316 GB in 1009 work directories on 2026-11-20. Its installed
`com.teamleaderleo.glaeda.disk-pressure` LaunchAgent invokes `disk-pressure.sh --once` every 900
seconds, which calls this script with pressure/idle cleanup. HQ's separate disk-pressure owner table
is not installed on that host. `glaeda-mini-setup` installs this source as `~/.local/bin/glaeda-disk`;
confirm its hash and a scheduled receipt before claiming rollout. The 24-host inventory found this
loop only on Ultra2. Incident and host receipts: manaflow-ai/cmuxterm-hq#15138.

Detector: `TipLoopWorkTest` in `scripts/test-glaeda-disk.py` exercises survey/apply, deep activity,
post-survey liveness, unavailable evidence, symlinked roots/ancestors and a post-survey redirect into outbox; mini-setup's existing byte-copy check covers installation.

Bound cleanup pins the parent directory with no-symlink `openat` traversal and deletes
relative to its descriptor, including interrupted deletion trees. A pathname redirected
after the scan cannot redirect deletion into delivery state. Inaccessible roots are skipped.
This family requires Python 3.11+ and fd-safe `shutil.rmtree`; older runtimes preserve the
work and report an incomplete cleanup. Read-only trees remain incomplete rather than
using a pathname-based chmod retry. The regression tests redirect the root immediately
before the recursive unlink for both normal and interrupted deletion.
