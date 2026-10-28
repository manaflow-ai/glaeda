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
