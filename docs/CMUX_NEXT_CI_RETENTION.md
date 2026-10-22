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
state's owner is known like a `cmux-scheme/<runner>` state's: a runner with a
`Runner.Worker` keeps its output, and the runner floor hold covers it.

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
under the size cap below. Use the normal `glaeda-disk` report first; `--apply`
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
met. Over the cap a state root no enrolled runner owns (a `cmux-scheme` state by
name, a release state by key) and idle at least `cmux_next_ci_unowned_idle_hours`
(default 6, GitHub's default job timeout) is listed whole, inputs included, and
leaves no empty `<key>/Release` behind; a younger one loses only generated output. Every enrolled runner's states lose only generated
output and keep `SourcePackages` and the module caches. Every veto above still
holds, and a runner with a `Runner.Worker` keeps its states. An unmeasurable
cache keeps the 72-hour window. cmux11s refused jobs at 21 GiB free under a
40 GiB floor with 78 GiB here and nothing reclaimable (2026-10-21).

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
