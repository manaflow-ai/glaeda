# cmux-next CI retention

`glaeda-disk` recognizes only these cmux-next state roots under
`~/Library/Caches/cmux-next-ci`:

- `cmux-scheme/<runner>`;
- `dev-daily-release/<hex key>/Release/<hex Xcode hash>`;
- `release-compile/<hex key>/Release/<hex Xcode hash>`.

The key is one per runner and workspace, not a source fingerprint: cmux's
`scripts/cmux-next/release-derived-data-path.sh` names it
`sha256("$RUNNER_NAME\n$GITHUB_WORKSPACE\n")[:16]`. glaeda-disk computes it for
each enrolled runner (`<workFolder>/cmux/cmux`, the work folder read from its
`.runner`, as given and resolved), so a release
state's owner is known like a `cmux-scheme/<runner>` state's, and the runner
floor hold covers it.

## A runner's running job

A runner whose `Runner.Worker` runs keeps the states its job may be using:
those written since the job began. Every build into a state rewrites the
state's top level (xcodebuild's `info.plist` as it starts, cmux's
`cmux-next-build.log` as it ends), and the steps that read a state's products
follow the build into it in the same job, so a state whose top level is older
than the `Runner.Worker` (its `ps` start time) holds the runner's last job's
output, as an idle runner's does. A job whose start `ps` cannot show keeps
everything its runner owns. Once any state's generated child is renamed
aside, evidence is taken again, against the state's times read before the
rename: if a process names the state or is open in it, its owner changed (a
re-registration), its runner's job no longer predates it (in a drain, any
running job), another top-level entry of the state was written since (a build
that began rewrites `info.plist`), a marker, socket or unknown child appeared,
or the state cannot be read, the child is put back. cmux14,
2026-10-27: ten runners had a job around the clock, every state was last built
in before its runner's job began, and the cache sat at 79 GiB against its
40 GiB cap with nothing reclaimable. An explicit drain (below) still refuses
any live `Runner.Worker`.

The Xcode state component may be the historical bare hash or the production
`xcode-<hash>` form. The family rejects symlinked layout parents and state
roots before traversing them.

The retention family is an allowlist. It can reclaim generated children named
`Build`, `Index.noindex`, `Logs`, `Products`, `Intermediates.noindex`,
`DerivedSources`, or `XCBuildData` after 72 hours of observed inactivity. It
keeps `SourcePackages`, `ModuleCache.noindex`, `SDKStatCaches.noindex`,
`SDKExplicitPrecompiledModules`, `info.plist`, metadata markers, unknown
children, and every directory outside the two validated layouts.

Both report and apply paths fail closed when a validated state root is named by
a process, holds a socket, has a `.cmux-active`, `.build.active`, `.lock`,
`lease.json`, or `reservation.json` marker, or contains an unknown direct
child. Apply re-collects process evidence immediately before each rename. The
family never deletes a source checkout, nor a whole state root except a stale one
under the size cap below, a runner floor slot, or the per-host build root below. Use the normal `glaeda-disk` report first; `--apply`
writes the standard JSONL receipt and rechecks every candidate.

## Size cap

On a host with enrolled glaeda runners, only a pressure run (`--pressure`, or
`keepup --apply` under pressure) measures the whole cache, inputs included, with
one `du` (a probe: its timeout records no `eviction_stuck` and no cooldown), and
keeps the result in `~/.cache/glaeda-disk/cmux-next-cap.json` beside the size
snapshot. A text report, or `keepup` without `--apply`, shows that last
measurement against `cmux_next_ci_cap_gib` (default 40 GiB; set it in
`~/.config/glaeda/disk-policy.json` or with `--cmux-next-cap-gib`, 0 turns it
off) and never walks the cache; `--json` (glaeda-fleet-status asks every mini)
neither measures nor shows it. A pressure run line records
`cmux_next_ci_over_gib`. Only a pressure run acts: while the
cache is over the cap, the 72-hour window gives way, and candidates go least
recently used first until the cache is back under the cap or the free target is
met. Over the cap a state root idle at least `cmux_next_ci_unowned_idle_hours`
(default 6, GitHub's default job timeout) is listed whole, inputs included,
whoever owns it, and a release state leaves no empty `<key>/Release` behind; a
younger one loses only generated output. Keeping every enrolled runner's inputs
held the cache over its cap for good: a runner keeps a state per kind, and on
cmux14 (2026-10-27) eight states of five runners held 4-5 GiB of
`SourcePackages` and module cache each. Every veto above still holds, a
runner's running job included (above). An unmeasurable
cache keeps the 72-hour window. cmux11s refused jobs at 21 GiB free under a
40 GiB floor with 78 GiB here and nothing reclaimable (2026-10-21).

## The per-host build root

cmux#25147 builds the DEV Release compile on each mini from one tree,
`dev-daily-release/host` (`src`, `Release/xcode-<toolchain>`, `lock`), so the
compilation cache's path-bearing keys match whichever runner instance builds
(cmux14, 2026-10-26: 424 of 502 compile jobs hit the cache warm, 548 s of
`xcodebuild` against 794 s cold). Builds hold `flock(2)` on its `lock`; a busy
lock builds in the old per-instance state, `dev-daily-release/<key>`, which is
now only that fallback. `disk-policy.json`'s `cmux_next_ci_host_roots` (default
`["dev-daily-release/host"]`, `[]` off) names such roots, relative to the cache.

A host root is one item, listed whole, and never a state root, so the size cap
never takes it. Apply takes it after everything else, the runner floor's slots
included, and only:

- once idle `cmux_next_ci_host_idle_hours` (default 48), or
- under the runner floor hold while free space is still below the floor itself.

It is never touched while its lock is held: the survey probes the lock, and the
removal takes it exclusive, without waiting, and holds it while it removes every
child except `lock` itself. The lock file keeps its inode, so a build that had
already opened it still excludes every other build. A build that starts
meanwhile falls back per instance, and one that starts after sets up a fresh
tree. A process open in it or naming it keeps it too. Among the other
cmux-next items, the per-instance states of a kind with a host root go first,
and over the size cap such a state idle `cmux_next_ci_unowned_idle_hours` goes
whole, inputs included, as any other does (its runner's running job and every
other veto still keep it).

The ordinary `--dedupe` pass visits only reusable package/module/SDK inputs. Build
output is write-heavy, so it is excluded from the periodic WatchPaths agent. An
operator may opt into the narrower `--dedupe --dedupe-builds` pass after the roots
are inactive. It scans each validated top-level `Products` directory, plus the sealed
`Build/Products` leaf used by current Xcode state roots; the rest of the write-heavy
`Build` tree remains excluded. It waits at least 15 minutes after the last file mtime
and re-collects process evidence before every APFS clone. A process, socket, lease/lock
marker, unknown state child, or unavailable `lsof`/`ps` evidence refuses that clone.
APFS copy-on-write keeps later compiler writes private to the path that performs them;
the command never hardlinks build outputs.

## Explicit drained maintenance

The 72-hour floor remains the default. An operator may make an explicit
pressure pass over younger generated output only with the enrolled-runner drain
gate:

```sh
glaeda-disk --runner-caches --pressure --need 100 --family cmux-next-ci-generated \
  --cmux-next-reservation-owner leo@fleet \
  --cmux-next-drain --apply --json --receipt ~/Projects/recovery/cmux-next-drain.jsonl
```

The command requires a bounded active `glaeda-reservation/v1` marker, an
installed `actions-runner*/glaeda-hooks/job-started.sh` wrapper that invokes
`glaeda-cmux-runner-hook job-started`, and an exclusive nonblocking
`/Users/Shared/cmux-build-fleet/host.lock`. The lock is held through the fresh
process/open-file/marker checks and each rename. Any live `Runner.Worker`,
`xcodebuild`, unreadable enrollment, lock contention, or unknown state keeps the
candidate and returns a deferred result. The flag is restricted to
`cmux-next-ci-generated` with `--pressure --need --apply`; it cannot widen ordinary cache,
paused-runner, or idle cleanup.

## The nightly-next per-runner trees

cmux's `scripts/ci/nightly_build_state.py` keeps NEXT's package checkouts,
compilation cache and notices crates under
`~/Library/Caches/cmux-nightly-next/<runner>`, one 7-9 GiB tree per runner that
ever built nightly-next. On 2026-10-27 that was 0-35 GiB per mini.

Once the build routes to one dedicated builder (cmux's
`vars.CI_NIGHTLY_NEXT_DEDICATED_LABEL`), set `disk-policy.json`'s
`nightly_next_dedicated_label` to that label, `glaeda-trusted-next-nightly`.
Then:

- the root leaves `library-caches`;
- each pool runner's tree (`<member>-glaeda[-K]`) becomes its own
  `cmux-nightly-next-fallback` item, which goes once idle 12 h, the normal age;
- the dedicated builder's tree (`*-nightly-next`) and any name the family does
  not recognize are never candidates.

While the builder is busy, the build still falls back to an idle pool mini and
then Blacksmith (cmux `scripts/ci/nightly_build_runner.py`). So a recently used
fallback tree stays warm for that overflow. Before this change, any pool build
kept the whole root fresh, so every tree on a mini stayed. Past 12 h idle, a cold
build is cheap: Blacksmith's cold Release build had a median of 620 s, against
830 s on a warm pool mini, over 22 builds.

The key defaults to `""`. With any value other than that label, the root stays
a single `library-caches` item, as before.
