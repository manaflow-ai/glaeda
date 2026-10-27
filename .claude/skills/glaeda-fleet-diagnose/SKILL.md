---
name: glaeda-fleet-diagnose
description: "Diagnose a slow, stuck or refused CI job on a glaeda runner mini, or check fleet health: find the host a job ran on, read its per-job host record (glaeda-cmux-jobs.jsonl), the runner slots' logs, the capacity ledger and glaeda-fleet-status. Use when someone asks why a cmux CI job took so long, whether a mini was busy or contended, which runner ran a job, or what state the fleet is in."
---

# Diagnose a glaeda fleet job or host

Start from evidence on the host, not from guesses about the change. Every step below is read-only.
The fleet manifest (members, SSH user, classes) is private: `~/.config/glaeda/mini-fleet.json` on the
operator machine, never in this repository.

## 1. Find where the job ran and which step was slow

Use the shared `glaeda-gh` watcher for the run state and exact head first; it
uses the Lawrence controller feed when available and refuses to become a
second polling loop:

```bash
glaeda-gh status run OWNER/REPO/RUN_ID --jobs --json
glaeda-gh wait run OWNER/REPO/RUN_ID --jobs --timeout 300 --json
```

Use one bounded GitHub job-log read only when the cached receipt names a
specific job whose step timing is needed. Do not turn `gh api`, browser reads,
or repeated job-log fetches into a status loop.

- `runner_name` is `<member>-glaeda[-K]`: member host plus runner slot K (directory `~/actions-runner-glaeda[-K]`).
- Get the job log with `gh api repos/OWNER/REPO/actions/jobs/JOB_ID/logs`
  (works before the whole run finishes; `gh run view --log` waits for the run).

## 2. Is it the host or the work? Read the job's host record

The runner hook writes a `started` line at admission, a `refused` line when job-started refuses, and a
`completed` line (the host record) when the job ends; `event` says which, and a line without it is `completed`.
`decision`, `wait_s`, `units`, `roots` and `gui` say what admission decided and held:

For a fleet mini, obtain the host's manifest-defined route first. AWS Macs are
reached through `cmux-lawrence` with the identity kept on the jump host; do not
paste a direct tailnet SSH command or send a Tailscale reauthentication link.
Use `glaeda-mini-fleet`/`glaeda-fleet-status` for the normal observation path.
If a raw host record is required, use the exact `ProxyJump` route emitted by
the manifest-aware helper and retain the host, route, and observation receipt:

```bash
glaeda-fleet-status --host MEMBER --output json
```

The host record is authoritative only when it is bound to the same runner name
and job/run identifier returned by the watcher.

Fields (`glaeda-cmux-job/v1`): `cores.job` / `cores.other_runner_jobs` / `cores.outside` (mean cores),
`load.mean` / `load.max`, `cpu_busy_pct` (iostat, host-wide), `disk_mb_s.mean` / `.max`,
`queue.running` / `queue.blocked` (processes in uninterruptible wait), `top_outside` (kernel executable name and user, estimated core-seconds),
`other_runner_jobs` (co-tenant runner slots), `thermal_limited`, `rss_footprint_peak_mib` (the job tree's summed
physical footprint at its largest 10 s sample: its memory; null when unknown), `rss_peak_mib` (summed RSS: shared pages
count per process and compressed pages not at all, so it reads high or low), `verdict` (`contended` or `clear`) and `reasons`.

- `contended` with high `cores.outside`: something outside every runner job used the CPU. `top_outside` names it.
- `cores.*` come from `ps` %cpu, a decaying average that undercounts short-lived compiler processes. When
  `load.mean` is far above the cores listed, trust `cpu_busy_pct`: near 100 means the host was saturated.
- High `queue.blocked` with high `disk_mb_s`: the job waited on disk, not CPU. High `queue.blocked` with low
  `disk_mb_s`: memory pressure (VM faults, compressor), check `memory_pressure` / swap on the host.
- High `other_runner_jobs`: co-tenant jobs on the same mini (capacity ledger admitted them; see step 4).
- `clear` but still slow: the work itself grew. Go to step 3.
- No record: the job predates the sampler, the hook ran with `--no-telemetry` / `GLAEDA_RUNNER_TELEMETRY=0`,
  or the job was refused before admission (look for `refused:` in the job's "Set up job" log).

Fleet-wide view of the same records, plus disk and runner state:

```bash
scripts/glaeda-fleet-status --no-lima          # text; add --output json for agents, --html FILE for a page
scripts/glaeda-fleet-status --host MEMBER      # one member
```

`jobs.contended@MEMBER` findings carry the latest contended job and its reason.

## 3. For cmux compile jobs: per-file cost, cache hits and scripts

From the job log's `Build Timing Summary` per xcodebuild pass:

- `SwiftCompile (N tasks) | S seconds`: S/N is the mean task time per file. Compare with the same job on another
  run or member. On an M4 Pro mini a clean app compile is about 1 s per file of task time; a 10x higher value on
  the same hardware means host contention, not code.
- `note: H hits / M cacheable tasks`: compilation cache hits. 0% with a full recompile is expected when the
  app module's top-level declarations changed (a package move); no seed or cache can skip that.
- `{"changed_inputs": ..., "hit": ...}` after "Adopt this owned Mac's DerivedData" says how much the reused build
  state covered.
- PR CI builds the PR merged into its **base branch**. A stacked PR on a stale base runs stale CI scripts; check
  the base before comparing timings with main.

## 4. What else was on the mini

Use the manifest-aware fleet observer for runner, capacity, reservation, and
host-lock state. Only after it identifies a native `Runner.Worker` job should a
person use the Lawrence route for bounded host-process evidence; preserve the
exact command and timestamp in the receipt.

- Worker logs give each slot's job windows, so overlaps are visible after the fact.
- `capacity/` holds the unit and token files of running jobs (`glaeda-cmux-runner-hook` CLASS_COST: a compile
  costs 2 of 4 units plus the persistent-dd and root tokens).
- `uptime` and `ps` show only now; the job record (step 2) is the history.

## Rules

- Read-only unless the user asks for a change. Never kill processes, unload LaunchAgents or delete files on a
  member while a `Runner.Worker` runs there; ask first, and hand anything that needs sudo to a person.
- Report the host evidence you found (record, overlaps, per-file cost) with the numbers, then the cause, then
  the fix. If the evidence is gone (the sampler did not run), say so rather than guess.
- Changing the hook, runner or status tooling: use the `glaeda-runner-change` skill.
- The controller mini is part of the recovery contract: `claim_paused:
  host_lock` plus missing/refused `with-host-lock` is a Lawrence-mediated repair;
  `claim_paused: runner_job` requires native process evidence before restart.
