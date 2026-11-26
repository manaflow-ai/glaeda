# Runner account hygiene

`glaeda-runner-hygiene` installs the automatic disposable-cache sweep for a
named secondary macOS account. It is an operator command and plans by default:

```sh
/path/to/reviewed-release/scripts/glaeda-runner-hygiene --user cmux
sudo /path/to/reviewed-release/scripts/glaeda-runner-hygiene --user cmux --apply
sudo /path/to/reviewed-release/scripts/glaeda-runner-hygiene --user cmux --uninstall
sudo /path/to/reviewed-release/scripts/glaeda-runner-hygiene --user cmux --uninstall --apply
```

Use the exact merged release checkout (or pass its `--source` path). The
user-writable convenience copy under `~/.local/bin` is not an authority for a
root install.

The account must be a named non-system account with a real, user-owned home
directory. `--apply` requires root. It copies the exact `glaeda-disk` source to
`/Library/Application Support/glaeda/runner-hygiene/<uid>/glaeda-disk`, records
its SHA-256 and the account home/filesystem device in a root-owned manifest, and
installs the matching root-owned plist at
`/Library/LaunchDaemons/com.teamleaderleo.glaeda.runner-hygiene.<uid>.plist`.
The copy is written and renamed atomically, mode `0555`, and marked immutable
when macOS supports `chflags`.

The LaunchDaemon has `UserName` set to the selected account and sets `HOME` to
that account's passwd home. It never runs the cleanup as root. Every 15 minutes
it executes the pinned copy with Python isolated mode and the bounded runner
cache profile:

```text
python3 -I <pinned-glaeda-disk> --runner-caches --pressure --idle --apply --top 0
```

The same `--apply` also installs the host's one root census job (kept current
by every enrolled account's `--check`/`--apply`, removed with the last
account): a pinned copy at
`/Library/Application Support/glaeda/runner-hygiene/root-census/glaeda-disk`
and `/Library/LaunchDaemons/com.teamleaderleo.glaeda.runner-hygiene.root-census.plist`
with no `UserName`, so launchd runs it as root every 30 minutes:

```text
python3 -I <pinned-glaeda-disk> --root-census
```

It is read-only (du, directory listings, lstat) and walks every folder of the
Data volume, other accounts' homes and privacy-guarded folders included: a
system-domain daemon has no login session, so no privacy prompt can reach a
screen. It writes one world-readable doc,
`/Library/Application Support/glaeda/root-census/census.json`, each row with the
account that owns it. The fleet user's `glaeda-disk` merges it in place of its
own census, so a CI mini's `accounting_gap` is only what no folder holds (APFS
snapshots, purgeable and open-deleted blocks) and never "unreadable". The fleet
user's own walks keep their privacy guards.

A CI mini needs no enrolled account to have the census. When no account is
enrolled, the root `glaeda-daemon-heal` pass keeps the census on its own with
its release's `glaeda-runner-hygiene --root-census --check`, and `--apply` when
stale. A host counts as a CI mini when its fleet user (the owner of
`/Users/Shared/cmux-build-fleet`) runs runners glaeda-cmux-runner enrolled:
`~/actions-runner*` with a `.runner` config and `glaeda-hooks`. An EC2 Mac or
cmux-lawrence never counts. The report carries the result as
`runnerHygiene.rootCensus`, and `glaeda-mini-health` raises
`disk_root_census_stale` on it. Where an account is enrolled, that account's
job keeps the census, so the plist has one writer. By hand, as root:

```sh
sudo glaeda-runner-hygiene --root-census --apply   # the census alone, no account
glaeda-runner-hygiene --root-census --check        # read-only: current or stale, and why
```

The allowlist is five families: Xcode `DerivedData`, cmux job caches,
GhosttyKit caches, CUA recordings, and CUA frame caches. Each item must be
idle for at least 168 hours. A pressure wake happens every 15 minutes; when
the disk is healthy, the idle sweep is throttled to once per hour. Git index
refreshes do not count as use, while process, open-file, command-line, socket,
and git-loss checks still veto a deletion.

The daemon's stdout and stderr go to `/dev/null`; the disk tool's own receipt
is the bounded deletion record. Existing plist/install paths are accepted only
when their management marker and account binding match. Unrelated files,
symlinks, and edited plists are refused. Planning performs no writes and does
not invoke `launchctl`; apply uses ten-second bounded `launchctl` calls.

This job only covers the selected account's rebuildable runner caches. It does
not remove wallpapers, credentials, SSH material, runner registration, shared
CI stores, or another account's data.

The normal mini setup and OTA release install the operator command beside the
other Glaeda tools, but they do not guess which old accounts are disposable.
Run the `--apply` install once for each account that is actually enrolled as a
runner. After that, no hand step keeps it current: the root
`glaeda-daemon-heal` LaunchDaemon, which already follows the stable release
through the verified channel, keeps that release's `glaeda-disk` and
`glaeda-runner-hygiene` in its own root-owned snapshot. On each pass, for every
account that already has a job here, it runs that release's
`glaeda-runner-hygiene --user NAME --check` and, when the snapshot or plist is
not what its `--apply` leaves, that `--apply`. An unchanged, current job is
checked again every 6 hours; a failed reinstall is retried after 15 minutes.
Its report (`runnerHygiene`) feeds `glaeda-mini-health`'s
`disk_root_copy_stale:<uid>`. OTA (`glaeda-update`, the runner user) still only
updates the user-writable helper, and nothing adopts an unenrolled account.

```sh
glaeda-runner-hygiene --user cmux --check    # read-only: current or stale, and why
```

A plist that an older release wrote (its bytes are the ones the manifest
recorded) is replaced; a plist edited by hand is still refused.

Before removing a runner account or running `glaeda-mini-setup --uninstall`,
uninstall its root job first with `--uninstall --apply`; the normal setup
uninstall does not own or remove this separate LaunchDaemon.

## Pool and dedicated runner accounts (no job of their own)

A PR runner pool account (`runner.poolUser`) or a dedicated runner account has
no `glaeda-disk` of its own and a 0700 home, so the fleet account's passes can
neither size nor reclaim it, and its bytes showed as "accounting gap". Every
`glaeda-disk --apply` pass that is not a runner hook's own pass (`--need`, or
`GLAEDA_DISK_HOOK_PASS=1`) now also runs one pass in each such account, as that
account (`sudo -n -u NAME -H`, never root): it copies its own bytes to
`~NAME/.cache/glaeda-disk/runner-account/glaeda-disk` and runs it there with the
same mode flags plus `--runner-account`. That pass acts only when the home has an
enrolled glaeda runner (`actions-runner*/.runner` and `glaeda-hooks`); it takes
that account's own evict lock and applies every normal veto, then reports the
home's size, which the fleet account's accounting counts as measured (scope
`runner-accounts`). A candidate is a non-admin account with uid >= 500 and a home
directly in `/Users`, other than the caller, with no runner-hygiene job here (that
account keeps the narrow profile above). Without passwordless sudo the account is
listed as `not reached`. A host turns this off with `"runner_accounts": false` in
its disk policy.

```sh
glaeda-disk --dead --apply    # then: runner account NAME: ran: reclaimed N, home M
```
