# Over-the-air glaeda updates

Status: live for the glaeda host tools (glaeda-disk, glaeda-worktree-reclaim, glaeda-fleet-cas-prune,
glaeda-local-guard, glaeda-update itself, and the LaunchAgents and systemd units that run them), and for
the cmux runners' staged copy and hook files (step 3 below). Owner issues: #525
(releases) and #149 (updates on hosts). The fleet runtime bundle is published in the same release
but still installs through `glaeda-mini-fleet upgrade` (below).

## How a merge reaches every host

1. **Release on merge.** `.github/workflows/release.yml` builds once per push to `main`, for
   `aarch64-apple-darwin` and `x86_64-unknown-linux-gnu`, and publishes the GitHub release
   `r-<UTC date>-<commit[:12]>`:
   - `glaeda-hygiene-<target>.tar.gz`: the committed tree under `glaeda/` plus a prebuilt
     `bin/glaeda-worktree-reclaim`;
   - `glaeda-<commit>-<target>.tar.gz`: the fleet candidate bundle (`scripts/fleet_bundle.py`);
   - `release.json`: the source commit and the SHA-256 and size of every asset.

   Every asset gets a build-provenance attestation. Releases are prereleases, so none is marked
   "latest".
2. **Canary.** The same workflow points `canary.json` at the new release, but only if the new
   commit descends from the current canary, so a slow build never moves canary backwards.
   The `ota-channels` release holds the only mutable files, each with one writer:
   - `canary.json`, written by `release.yml`;
   - `stable.json`, written by `promote.yml`;
   - `control.json` (`paused`), written by `control.yml`.

   Only runs on `main` publish or write anything.
3. **Hosts pull.** `glaeda-update` runs hourly on every host: a LaunchAgent
   `com.teamleaderleo.glaeda.update` on macOS, the systemd user timer `glaeda-update.timer` on
   Linux, with up to 15 minutes of random delay. It reads its ring from
   `~/.config/glaeda/update.json`, then:
   - downloads by hash;
   - checks the build-provenance attestation, which must come from `release.yml` on `main`. A
     canary refuses to install without that check (it needs `gh`, signed in). A stable host
     without `gh` relies on the hash chain, since stable only names releases a canary verified;
   - runs the release's own `scripts/glaeda-mini-setup --apply` with the host's setup flags and
     the prebuilt reclaim binary;
   - checks that the installed tools run and that the new `glaeda-update` can plan against the
     live channel.

   If that fails, it re-runs the previous release's setup, or on a host's first update restores
   the tools it replaced (tools only; agents and config stay as the failed setup left them), and
   quarantines the failed release on that host. The health check plans from the channel files
   the run already fetched, so a network blip cannot fail a good release.

   A stale or unrelated channel target, or a target whose setup does not support this host's
   persisted flags, is refused before `--apply` and is re-evaluated after the channel or host
   configuration changes. It is not treated as a failed release artifact that needs operator
   quarantine clearing.

   On a runner mini, each run (not only one that installs a release) then brings
   `~/glaeda-runner/scripts` to the installed release when it is older, and the runners'
   `glaeda-hooks/glaeda-cmux-runner-hook` and `glaeda_reservation.py` too when theirs are versions the
   release descends from (`glaeda-cmux-runner --refresh-hooks`, docs/CMUX_MINI_RUNNER.md 2c). Runners
   with a job wait for the next run; the listener gates re-exec the new hook themselves. The hygiene
   archive carries `ancestry.txt` (the source's ancestors: a copy an operator staged from one is
   refreshed, a newer one kept) and `hook-history.json` (the SHA-256 of every version of those two
   files, so a newer hook is never downgraded). The canary hosts run no runners, so a hook change is
   gated by CI, the six-hour soak and these checks, not by a canary status.
4. **Canary health.** A canary host with an authenticated `gh` posts the commit status
   `glaeda-ota/<host>` (success or failure) on the release commit.
5. **Stable.** `.github/workflows/promote.yml` runs hourly, and each canary run also dispatches it,
   since GitHub's cron can lag for hours. It moves `stable` to the newest release for which all of
   these hold:
   - the release is at least 6 hours old;
   - at least one canary host reported success;
   - no canary host's newest status is a failure;
   - it descends from the current stable;
   - no newer release has a canary failure. A release's own updater installs its successor, so a
     failure there can be the older release's fault; promotion waits until a newer one is healthy.

   It looks past the current canary on purpose: with several merges a day the newest release is
   never 6 hours old, and stable would never move.

   Stable hosts pick it up within the hour.

End to end, a merged change reaches canary hosts in about an hour and the whole fleet in about
eight.

## Rings

| Host | Ring | Why |
| --- | --- | --- |
| Air Blue | canary | Leo's Mac; `gh` is authenticated, so it reports health |
| Big Red | canary | always on; reports health when the Mac is asleep |
| manaflow minis | stable | build hosts: take only what a canary has run for 6 hours; download from the fleet mirror |

Set a host's ring once with `glaeda-mini-setup --apply --ota-ring canary` (or `stable`). The
first setup writes the config with `stable`, and later runs leave it alone unless `--ota-ring`
names another ring.

Set where a host downloads from with `--ota-source manaflow-ai/glaeda` (or `teamleaderleo/glaeda`,
the default); it is kept in `update.json` as `source` the same way.

## Fleet mirror

The Manaflow minis install from [manaflow-ai/glaeda](https://github.com/manaflow-ai/glaeda), a fork
of this repository in the Manaflow organization, so the fleet does not depend on a personal
repository. Both repositories take pull requests, and their `main` branches are kept equal in both
directions. Releases, attestations and promotions still happen only in this repository.

**Anything merged to either `main` ships to the fleet.** A merge in the fork reaches this
repository's `main` within about 15 minutes, and every push to this `main` is released as above:
canary hosts install it within about an hour, and the minis (stable) after the six-hour canary soak,
about eight hours in all. There is no separate approval step after the merge. To hold a release
back, pause the rollout (`control.yml`, below).

- **Sync of `main`.** `.github/workflows/sync-main.yml` runs only in this repository, every 15
  minutes, on every push to `main`, and on dispatch. It calls `scripts/glaeda-sync-main`, which
  compares the two mains:
  - equal: nothing to do;
  - one is behind: it is fast-forwarded to the other;
  - diverged, merging cleanly: one merge commit (this repository's `main` first parent, the fork's
    second, author `glaeda-sync`, message naming both commits) is pushed to both;
  - diverged with conflicts: neither side changes. The job fails and opens (or updates) one issue
    here, titled "glaeda main sync: teamleaderleo/glaeda and manaflow-ai/glaeda conflict", listing
    both commits and the conflicting paths. Fix it with a pull request that merges one `main` into
    the other; the next run fast-forwards the other side and closes the issue. Repair steps:
    cmuxterm-hq `REPAIR.md`, row "glaeda main sync conflict".

  Nothing is ever force-pushed. Each push is `git push <remote> <commit>:refs/heads/main`, which
  GitHub refuses unless it is a fast-forward. If a side moves between the fetch and the push, the
  push is refused, the run reports `raced` and succeeds, and the next run starts over.
- **How it pushes.** Over SSH with one write deploy key per repository, stored in this repository
  as the secrets `SYNC_UPSTREAM_DEPLOY_KEY` (a deploy key of this repository) and
  `SYNC_MIRROR_DEPLOY_KEY` (a deploy key of the fork). Not the job token: a push made with it does
  not start `release.yml`, and it cannot push commits that change workflow files. The workflow runs
  the checked-out `scripts/glaeda-sync-main` with both write keys, so anyone who can merge to either
  `main` can change the sync logic itself.
- **Checks in the fork.** `ci.yml` (Verify) runs there for pull requests only; its jobs skip on a
  push outside this repository. `release.yml`, `promote.yml` and `control.yml` run only here. The
  fork's `main` takes changes only through pull requests and the sync's deploy key. A deploy-key
  push starts the fork's push workflows, so gate every new push-triggered workflow (or its jobs)
  with `github.repository == 'teamleaderleo/glaeda'`.
- **Releases and rings.** `.github/workflows/mirror.yml` runs only in the fork, every 15 minutes
  (and on dispatch). It copies the release tags, the newest 30 `r-*` releases plus the ones
  `canary.json` and `stable.json` name, and the `ota-channels` files, byte for byte; it no longer
  touches `main`. `control.json` is copied first and on its own, so a pause never waits on a
  release; a ring file is copied only once its release is published in the fork. It keeps every
  other fork workflow disabled except `ci.yml`. Tags go over SSH with the fork's deploy key (its
  secret `MIRROR_DEPLOY_KEY`), since the job token cannot create a tag on a commit whose workflow
  files differ from `main`'s; releases are then made on those tags with the job token. No personal
  token is stored anywhere.
- **What hosts check.** A host with `"source": "manaflow-ai/glaeda"` downloads channel files and
  assets from the fork, and checks them exactly as before: release.json must name
  `teamleaderleo/glaeda`, and the attestation (when `gh` is signed in) must come from this
  repository's `release.yml`. The fork's admins can write what minis download, as they can already
  change the minis themselves, and now merge what this repository releases.
- **Lag.** Each direction of `main` is up to 15 minutes behind (longer when GitHub's cron lags; a
  push to this `main` syncs at once). Releases and rings reach the fork after the next mirror run.
  A pause reaches the minis after the next mirror run; to hurry it, dispatch the mirror too:
  `gh workflow run mirror.yml --repo manaflow-ai/glaeda`.
- **Checkouts.** New minis clone the fork (`glaeda-mini-fleet`), and `~/glaeda` moves to the fork's
  `main`. Hosts set up earlier are moved by cmuxterm-hq's `build-fleet/mini-ops/glaeda-mirror.sh`,
  which sets their `origin` and `source`, one canary host first.
  A `glaeda-mini-fleet --glaeda-ref` pin to a commit merged in the last few minutes fails to fetch
  until the next sync run.
- **Takes effect** with the first `glaeda-update` that knows `source`: a host running an older
  release (or rolled back to one) ignores the key and keeps downloading from this repository, which
  still works.

## Operator controls

```bash
gh workflow run control.yml --repo teamleaderleo/glaeda -f paused=true    # every host stops updating
gh workflow run control.yml --repo teamleaderleo/glaeda -f paused=false   # resume
gh workflow run promote.yml --repo teamleaderleo/glaeda                   # check promotion now
gh workflow run mirror.yml --repo manaflow-ai/glaeda                      # copy releases and rings to the fork now
gh workflow run sync-main.yml --repo teamleaderleo/glaeda                 # sync main both ways now
gh release download ota-channels --repo teamleaderleo/glaeda -p '*.json' -D /tmp/ota   # what each ring runs
glaeda-update --status                     # on a host: ring, current, previous, quarantined
tail ~/Library/Logs/glaeda-update.jsonl    # macOS; ~/.local/state/glaeda-update.jsonl on Linux
```

A broken release on stable is fixed forward: revert on `main`, and the revert becomes the next
canary. Dispatching `promote` only runs the same check early; it never skips the soak.

## Trust

- Hosts trust GitHub over TLS and nothing by name. The ring file pins release.json by SHA-256,
  and release.json pins every asset. Tags and commits must have the release format, so a ring
  file cannot point a host at another repository's download.
- The attestation must name `release.yml` on `refs/heads/main` as its signer. A dispatch on
  another branch neither publishes nor attests.
- Stable hosts without a signed-in `gh` do not check the attestation. That does not widen trust:
  whoever can write this repository can already change what minis run, since
  `glaeda-mini-fleet` syncs their `~/glaeda` to `main`.
- The updater runs only what a release's `glaeda-mini-setup` installs. That command is
  idempotent, never uses sudo, and owns every file it writes (its receipt records them).
- A release comes only from a push to `main` of this repository by someone with write access.
  `main` has no branch protection rule today, so repository write access is the root of trust.

## Not automatic yet

The fleet runtime on the minis (`glaeda` binary, enrollment, class acceptance) still moves with
`glaeda-mini-fleet upgrade --candidate-run RUN --yes`. Its bundle says
`automaticUpdateAuthorized: false`, and renewing it needs a class acceptance (a 13-minute cold
cmux build on a seed per hardware class) before other hosts can adopt it. Automating that means a
canary mini that runs `accept-local` on each stable candidate and publishes the class receipt as
a release asset for the others to adopt. That work is tracked in #149.
