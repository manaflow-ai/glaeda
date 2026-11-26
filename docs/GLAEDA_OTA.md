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
   "latest". `release.json` also carries a hash-attested policy derived from the exact first-parent
   diff: docs/tests are low risk, ordinary runtime changes are normal risk, and updater, installer,
   runner, release, security, persistence, rollback, or ambiguous changes are high risk.

   The Rust binaries (glaeda, glaeda-worktree-reclaim, fleet-cas) are built once per Rust inputs, not once
   per push: an actions/cache entry keyed on each set's tracked input blobs, the target and `rustc -Vv`
   (`scripts/release_build_cache.py`) holds them, so a push that changes only scripts or docs, most of them,
   reuses the bytes an earlier release built and skips cargo. A hit is used only when the binaries' cargo
   dep-info names no checkout file the key missed; a changed set builds, with its dependencies restored from
   a cache keyed on the lock files. Only `main` writes these entries.
2. **Canary.** The same workflow points `canary.json` at the new release, but only if the new
   commit descends from the current canary, so a slow build never moves canary backwards.
   The `ota-channels` release holds the only mutable files, each with one writer:
   - `canary.json`, written by `release.yml`;
   - `stable.json`, written by `promote.yml`;
   - `control.json` (`paused`), written by `control.yml`.

   Only runs on `main` publish or write anything.
3. **Hosts pull.** `glaeda-update --apply` runs every minute on every host (its `PROBE_EVERY_S`, which
   setup's LaunchAgent reads and a test holds the Linux timer to): a LaunchAgent
   `com.teamleaderleo.glaeda.update` on macOS, the systemd user timer `glaeda-update.timer` on
   Linux, with a fixed per-host offset of up to 30 seconds. A run does the full update every 10
   minutes (`APPLY_EVERY_S`) and, between those, only when the channel changed: it sends one
   anonymous HEAD for `control.json` and one for its ring's file, and github.com's redirect names the
   uploaded asset, which every `gh release upload --clobber` replaces. An unchanged probe logs
   nothing; one that cannot tell (offline) starts nothing, so the backoff below still holds. A host
   sees a new release or a pause within about a minute. Channel files and assets come from
   `github.com/<repo>/releases/download`, anonymously, never through the GitHub API, so a spent
   API quota never stops a host from converging (a canary's attestation check is the one API call).
   A host whose `source` is the mirror reads the channel files here first (their one writer; the
   mirror when this repository cannot be read) and downloads a release from the mirror first (here
   when the mirror has not copied it yet), so a promotion does not wait for the mirror. It reads its ring from
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
   the run already fetched, so a network blip cannot fail a good release. A check that times out
   says the host is too busy, not that the release is bad: the run rolls back the same way but
   quarantines nothing and posts `pending`, and the next run (with backoff) tries the release
   again. The fifth timed-out check in a row for one release (over about two hours of backoff)
   counts as a failure. A rollback that itself fails is reported as blocking `rolled-back`.

   A stale or unrelated channel target, or a target whose setup does not support this host's
   persisted flags, is refused before `--apply` and is re-evaluated after the channel or host
   configuration changes. It is not treated as a failed release artifact that needs operator
   quarantine clearing.

   On a runner mini, each run (not only one that installs a release) then brings
   `~/glaeda-runner/scripts` to the installed release when it is older, and the runners'
   `glaeda-hooks/glaeda-cmux-runner-hook` and `glaeda_reservation.py` too when theirs are versions the
   release descends from (`glaeda-cmux-runner --refresh-hooks`, docs/CMUX_MINI_RUNNER.md 2c). A runner
   with a job is swapped in its next gap between jobs (watched up to 4 minutes; after that its
   job-completed installs the staged new hook); the listener gates re-exec the new hook themselves. The hygiene
   archive carries `ancestry.txt` (the source's ancestors: a copy an operator staged from one is
   refreshed, a newer one kept) and `hook-history.json` (the SHA-256 of every version of those two
   files, so a newer hook is never downgraded). The canary hosts run no runners, so a hook change is
   gated by CI, the signed policy soak and these checks, not by a canary status.
   Runners in another account get the same refresh (glaeda-update step 7b): each `runner.poolUser` (an AWS Mac's
   PR runners, in `cmux-pr-runner`) and `runner.dedicated` user the staged `~/glaeda-runner/mini-fleet.json`
   names is refreshed through `sudo -n -u ACCOUNT` from a copy of the release's runner files under /tmp. Before
   it, those hooks only changed on a fleet `--apply`, and converge.json never listed them; a refusal is now a
   blocked runner `~ACCOUNT` in converge.json.
   The same run brings `~/.local/bin/glaeda-promotion-admit` (HQ's nightly promotion lease runs it
   over ssh; `glaeda-cmux-runner-fleet --apply` installs it on every member it stages) to the
   release's bytes on those runner minis: installed when missing, replaced when `hook-history.json`
   lists its digest, kept when newer. It is swapped under the helper's `admit.lock` and never while
   a lease's supervisor runs; the copy and the installed file are both checked against the release's
   SHA-256, and converge.json's `running.promotionAdmit` shows the installed and release digests.
   **Convergence.** Each run then records `~/.local/state/glaeda/update/converge.json`: what the
   host actually runs, by content (the SHA-256 of each installed tool and of each runner's installed
   hook files, each against the installed release's bytes), the tag its ring wants, whether they
   agree, and why not. Tools whose bytes drifted are reinstalled by the installed release's own
   setup. While a host stays unconverged on the same wanted release it backs off from 10 minutes to
   an hour; a new release on the ring is tried at once, and an installed release's hooks are retried
   every run, so a runner busy with a job gets them between jobs. `glaeda-update --status` shows the
   record and a live reading. Its `live` is merge-to-live on this host: `merged` (the release
   commit's time on `main`, from release.json `committed`), `installedAt`, `hooksLiveAt` (when the
   last runner's hook files took the release's bytes, from their rename; null until every runner's
   has) and `mergeToInstallS` / `mergeToHooksLiveS`. Read it with
   `jq .live ~/.local/state/glaeda/update/converge.json` on the host (or through `reach HOST`).
   Releases published before `committed` existed show null spans. `glaeda-mini-health` reads it and reports `ota_not_converging`
   ("running X, wants Y, because Z") only past the `alertAfter` time glaeda-update wrote (two
   backed-off hours), as an error when the reason is one retrying cannot clear (a quarantined or
   refused release, a hook `--refresh-hooks` keeps), and `ota_stale` when no run has finished for
   three hours. ci-dash shows both in Health, from the report the probe forwards.
4. **Canary health.** A canary host with an authenticated `gh` posts the commit status
   `glaeda-ota/<host>` (success or failure) on the release commit. Its gh calls ride out a spent
   REST quota: a reset within five minutes is waited for, rate limits and transient errors are
   retried with backoff, a failure waits out the reset (up to an hour) so it lands before another
   canary's success can promote, and a success or failure that still is not delivered is kept in the host's
   state.json and posted again by the next run (commit statuses have no GraphQL mutation to fall
   back to). "Signed in" is the local `gh auth token`, not `gh auth status`, which spends quota.
5. **Stable.** `.github/workflows/promote.yml` runs on every canary's `glaeda-ota/<host>` commit
   status (the `status` event; a run for any other context is skipped and takes no concurrency
   slot), when a Release run finishes, hourly, and when a canary run dispatches it after its soak,
   since GitHub's cron can lag for hours. Nothing polls: each run evaluates once. It moves `stable`
   to the newest release for which all of these hold:
   - the release's signed `policy` is valid and its publication timestamp is older than that policy's
     soak floor: new releases use a 15-minute window. Historical signed 2/6/12-hour policies remain
     readable during the transition; manifests without a policy retain the original 6-hour fallback.
     The soak ends early once `CANARY_QUORUM` (2) canary hosts report success and none reports
     failure or pending: two canaries installed it and passed health, which is what the window
     waits for (Leo, 2026-11-17: "we should be able to propagate and have ci related changes
     reflect much faster");
   - at least one canary host's newest status is success, and no canary host's newest status is a
     failure. No host is required, whatever the risk: a canary that is silent or pending holds
     nothing once the soak is over. Policies above low risk signed before 2026-10-19 list Air Blue and
     Big Red in `requiredHosts`; they still parse, and the list is not enforced. (On 2026-10-19 a spent quota
     kept Air Blue from reporting, and requiring it held stable on one build for 15 hours.);
   - or, when no canary host reported anything (success or failure) for `CANARY_SILENT_HOURS`
     (2.5 h) past the soak, every target's `smoke-<target>.json` is an asset of the release: the
     build extracted that target's hygiene archive on a fresh runner and ran the checks
     `glaeda-update` runs after an install. That bounds what a spent shared quota can do to stable;
     a canary's failure, which is waited out for up to an hour, still blocks it;
   - it descends from the current stable;
   - no newer release has a canary failure. A release's own updater installs its successor, so a
     failure there can be the older release's fault; promotion waits until a newer one is healthy.

   Host authentication, network, stale-channel, and unsupported-host-profile observations are
   reported as `pending` or omitted, so they neither count toward promotion nor quarantine the
   release as a bad artifact. Only verification, setup, or health failures are reported as release failures.

   It looks past the current canary on purpose: with several merges a day the newest release is
   never old enough, and stable would never move. Candidate age uses GitHub's `publishedAt`, the
   same canonical publication time written into the channel receipt.

   Stable hosts pick it up within about a minute (the probe in step 3), the mirror's included.

End to end, by design: the release is published about 5 minutes after the merge (the build), canary
hosts see it within a minute and report after their install and health check, the second canary's
success starts a promote run that moves stable (about a minute for the run), and every host (an idle
runner's hook too) installs it within another minute plus its install: about 10 minutes from merge to
a runner's hook. A busy runner takes the hook at its next gap between jobs. With one canary awake (or
the second late or pending), stable waits out the 15-minute soak, and that canary's minute probe starts a
full run as the soak ends, which dispatches the promote run: about 16 minutes from publication. Until
2026-11-24 that dispatch waited for the canary's next scheduled full run, up to 10 minutes later.
Before 2026-11-17 the same path took 23 to 50 minutes (a 15-minute soak, then a 10-13 minute poll and
up to 15 more for the mirror).

## Fast path for glaeda-gh

The rings above stay the reconciler. Before they run, a merge to `main` also reaches each host's glaeda-gh
on a faster path:

- **Trigger.** The daemon's main watch (`MainWatch` in `scripts/glaeda-gh`) reads `main` with `git ls-remote`
  every few seconds. When `main` moves, it starts the operator's `fast-patch glaeda COMMIT` with
  `FAST_PATCH_TRIGGER=main-watch`.
- **Swap.** fast-patch fetches the commit, swaps the installed script, and waits for the daemon to report it.
  The daemon reports it through daemon.json's `scriptSha256`.
- **Wake.** `ScriptWake` stats the script every 0.5 s. On new content it ends the running tick at its next
  request boundary and wakes serve's sleep.
- **Re-exec.** serve then decides with `reexec_due`:
  - Land judgments in flight are handed to the next daemon.
  - Only a claimed land step holds or drains the re-exec (`land_busy`). A read is judged again after the
    restart.
- **Rollback.** fast-patch rolls back when the daemon is not on the new bytes within its bound.
- **Record.** Every run appends a line to `~/.local/state/fast-patch/runs.jsonl`. The line carries
  `seconds`, `fetchedS` and `swapS`, plus `deferred` when land work held the hand-over.

## Rings

| Host | Ring | Why |
| --- | --- | --- |
| Air Blue | canary | Leo's Mac; `gh` is authenticated, so it reports health, usually 3-4 minutes after publication |
| Cosmic Gray | canary | a second Mac, the quorum's second host; busy, so it often reports after the soak or posts `pending` (a health check or `gh auth token` timing out) |
| Big Red | stable | not a canary since 2026-10-31 (leo-workspace `machines/host-baseline.json` `glaeda_ota`); it posts no status |
| manaflow minis | stable | build hosts: take only what a canary has run healthy for 15 minutes and none failed; download from the fleet mirror |

Set a host's ring once with `glaeda-mini-setup --apply --ota-ring canary` (or `stable`). The
first setup writes the config with `stable`, and later runs leave it alone unless `--ota-ring`
names another ring.

Set where a host downloads from with `--ota-source manaflow-ai/glaeda` (or `teamleaderleo/glaeda`,
the default); it is kept in `update.json` as `source` the same way.

## Fleet mirror

The Manaflow minis install from [manaflow-ai/glaeda](https://github.com/manaflow-ai/glaeda), a fork
of this repository in the Manaflow organization, so the fleet does not depend on a personal
repository. Changes merge only here, and the sync keeps the fork's `main` equal to this one
(Sync of `main`, below: usually a fast-forward; a divergence becomes a merge commit or, on a
conflict, a tracking issue). Releases, attestations and promotions also happen only in this
repository.

**The fork's `main` takes no merges.** Its ruleset 26619652 ("main: deploy keys only") blocks
every update to `main` except a push made with a deploy key. A pull request opened in the fork
still runs Verify but cannot merge ("Cannot update this protected ref."): merge it here instead.
`glaeda-gh land manaflow-ai/glaeda#N` does that: it carries the head to `carry/manaflow-ai-glaeda-N` here, opens one
PR naming the original and lands it pinned (CONTRIBUTING.md, "How to land a glaeda PR").
On 11-04 two pull requests merged straight into the fork's `main` and stalled the sync for about
20 hours on a conflict. A ruleset cannot name one deploy key, so the bypass covers both of the
fork's write keys: the sync's (`SYNC_MIRROR_DEPLOY_KEY`, kept here) and `mirror.yml`'s tag key
(`MIRROR_DEPLOY_KEY`, kept in the fork). Only the sync uses one to push `main`; that is a
convention of the workflows, not something the ruleset checks. The ruleset stops the mistakes that
happen (a merge button, a direct push), not a fork admin or a workflow written to push `main`.

**Anything merged to `main` ships to the fleet.** Every push to this `main` is released as above:
canary hosts install it within minutes of its release, and the minis (stable) once two canaries
report it healthy (or after the 15-minute soak with one). There is no separate approval step after the merge. To hold a release
back, pause the rollout (`control.yml`, below).

- **Sync of `main`.** `.github/workflows/sync-main.yml` runs only in this repository, on every
  push to `main`, on dispatch, and on a 15-minute schedule (which GitHub runs only every few hours
  for this personal-account repository). In the fork, `.github/workflows/sync-dispatch.yml` starts
  it on every push to the fork's `main` and, on the fork's own 15-minute schedule, whenever the two
  mains differ; it needs the fork secret `SYNC_DISPATCH_TOKEN` (a fine-grained token with Actions
  read and write on this repository alone) and, without it, only warns. It calls
  `scripts/glaeda-sync-main`, which compares the two mains:
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
- **Checks in the fork.** `ci.yml` (Verify) runs there on Blacksmith for pull requests and for every
  push to `main` that changes more than documentation; here it runs for this repository's own pull requests only, on GitHub-hosted runners
  (Blacksmith installs on organizations only). Both mains hold the same commits, so the fork's Verify
  at a commit is its check of record in both repositories, and `glaeda-gh checks teamleaderleo/glaeda
  SHA` reads the fork's checks for that commit too. Opening a pull request in the
  fork keeps its Verify on Blacksmith as well. `release.yml`, `promote.yml` and `control.yml` run only here. The
  fork's `main` takes changes only through a deploy-key push (ruleset 26619652), which only the sync makes. A deploy-key
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
- **What hosts check.** A host with `"source": "manaflow-ai/glaeda"` downloads assets from the fork (here when the
  fork has not copied a release yet) and channel files from here first (step 3), and checks them exactly as before: release.json must name
  `teamleaderleo/glaeda`, and the attestation (when `gh` is signed in) must come from this
  repository's `release.yml`. The fork's admins can write what minis download, as they can already
  change the minis themselves, and now merge what this repository releases.
- **Lag.** Each direction of `main` is up to 15 minutes behind (longer when GitHub's cron lags; a
  push to this `main` syncs at once). Releases and rings reach the fork after the next mirror run,
  but the minis do not wait for it: they read the channel files here first and download a release
  the fork lacks from here (step 3), so a promotion or a pause reaches them within about a minute.
  The fork is their fallback when this repository cannot be read.
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
glaeda-update --apply --now                # on one host: the full update now, without waiting for the probe
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
