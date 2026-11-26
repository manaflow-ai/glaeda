# Glaeda agent entry

Use **Glaeda** for the project and **`glaeda`** for the binary/crate. Use
**SmolRunner** only in history, v1, or migration references.

## Start here

1. Run `./scripts/bootstrap`.
2. Read the assigned issue and all current comments, then inspect the current PR,
   Git state, and overlapping work before editing.
3. Read only the contract for the surface you will change:

When creating a developer checkout, use `~/.local/bin/glaeda-worktree add --no-build <repo> <name>`;
use `--build` only on a build host. This keeps every worktree under `~/Projects/worktrees/<repo>/`
and avoids initializing Ghostty on no-build machines. Run `~/.local/bin/glaeda-projects tidy` for a
report before reorganizing older checkouts. In zsh, `"$var:refs/..."` applies the `:r` modifier;
write `"${var}:refs/..."` in scripts and one-liners.

Most current work is the fleet track in `scripts/`:

| Work surface | Read next |
| --- | --- |
| Waiting on CI, PR checks, runs or comments | `skills/github-ci-wait/SKILL.md` (`glaeda-gh`) |
| cmux runner, job hook, capacity ledger, runner rollout | `docs/CMUX_MINI_RUNNER.md` and `.claude/skills/glaeda-runner-change` |
| A slow, stuck or refused fleet job; fleet health | `docs/FLEET_STATUS.md` and `.claude/skills/glaeda-fleet-diagnose` |
| Fleet operator commands (status, disk, runners, seeds) | `docs/FLEET_OPERATIONS.md` |
| Where a cmux job runs (`glaeda-route`) | `docs/GLAEDA_ROUTE.md` |
| Releases and host updates (`glaeda-update`, deploys) | `docs/GLAEDA_OTA.md` |
| Disk and checkout hygiene (`glaeda-disk`, `glaeda-projects`, `glaeda-worktree`) | `docs/DEVBOX_DISK_AND_PROJECTS.md` |

The paused Rust/Lima runtime track (`src/`, `docs/ROADMAP.md`):

| Work surface | Read next |
| --- | --- |
| Hostile/disposable execution | `docs/THREAT_MODEL.md` plus applicable `docs/DISPOSABLE_*.md` |
| Residency, reuse, cache, performance | `docs/BLAZINGLY_HOT.md` plus the owning issue |
| Workspace bootstrap | `docs/WORKSPACE_BOOTSTRAP.md` |
| Ownership, persistence, mutation, recovery, subprocesses, physical experiments | `docs/AGENT_EXECUTION_SAFETY.md` |
| Lease transitions | `docs/adr/0004-lease-lifecycle-core.md` |
| Adding, exporting, or retiring a public module | `docs/MODULE_INVENTORY.md` (generated; do not hand-edit) |

`docs/AGENT_COORDINATION.md` is an older delegation protocol; read it only when a task
hands work to another agent.

Changing coordination belongs in current issues and PRs. Keep broad document
preloading out of the normal startup path.

## Product boundary

Glaeda runs trust-tiered work on operator-controlled compute: today the cmux Mac
mini runner fleet, its routing, OTA updates and host hygiene; the paused Rust runtime
defines three execution classes:

- Hostile/unknown work: one fresh isolated Linux worker, one bounded job, exact teardown.
- Trusted CI: prepared workers and reviewed reusable generations with a clean job boundary.
- Ultra-trusted project work: resident project state under explicit lease, validity, reset,
  and revalidation rules.

GitHub Actions remains the scheduler, workflow language and check surface; Glaeda
owns compute-side admission, execution policy, identity, lifecycle, reusable-state
policy, and recovery. Do not build a new pipeline language, runner protocol,
container/hypervisor runtime, Kubernetes controller, public multi-tenant platform,
or generic cloud scheduler.

Product details live in `README.md` and `docs/COMPUTE_RUNTIME.md`.

## Universal invariants

- Exact identity, bounded authority, fresh observation before action/release,
  durable crash recovery, and rebuildability form the correctness kernel.
- Trust decides residency. Surviving VMs, processes, worktrees, caches, indexes,
  artifacts, or services gain no source, ownership, result, merge, cleanup, or
  mutation authority by surviving. Reuse binds exact trust/project/lease/source/
  toolchain/capability/reset identity.
- Names, paths, PIDs, tags, slots, cache hits, survivors, and lease IDs are
  observations rather than ownership proof. Ambiguous state goes through fresh
  revalidation, reset, quarantine, rebuild, or explicit recovery; protect
  foreign, conflicting, and unknown state.
- Planning and read paths perform no side effects. Mutation requires validated
  preconditions, a complete plan, defined rollback/compensation semantics, and
  fresh post-effect observation. `docs/AGENT_EXECUTION_SAFETY.md` owns the cold
  mechanics.
- Performance observations, diagnoses, optimizer outputs, and desired-state
  recommendations carry advisory authority only. Preserve complete cold/reset
  fallback and prove fixes by reproducing the failure then observing the repair.
- `unsafe` Rust is forbidden. Use locked Cargo resolution and commit `Cargo.lock`.
- Human and JSON output must stay bounded and typed. Keep secrets, credentials,
  raw logs, repository contents, environment dumps, and private paths out of
  public evidence.

## Native Apple agent builds

For initialized native projects, prefer `glaeda-apple submit --operation check`.
After coordinated pulls or branch changes, use `refresh` to ensure dependency
readiness and then check incrementally; `plan-refresh` previews both stages.
Save the returned request id; use `wait-request --request-id <id> --wait-seconds 300`
in an active runner’s asynchronous process facility for event-based completion,
or collect it with `request-status --request-id <id>`
and explicitly `forget-request --request-id <id>` after consuming a terminal result.
Use operation `dependencies` for preparation and `build` for a complete app build.
The worker starts on demand and exits when idle; `wake` resumes pending work after
interruption. Submission targets latest source at execution, not a pinned revision.
Preserve each project's launch rules and coordinate source/branch edits separately.
Never run direct and managed builds concurrently. Foreground callers can still use
`check --wait-seconds 300`; timeout never authorizes killing another builder or
removing its state. See `docs/APPLE_NATIVE_BUILDS.md` for recovery, receipt scope,
source-observation limits and the bounded request history.

## Verify

The full suites run in GitHub Actions (`.github/workflows/ci.yml`, the Verify
workflow), which shards the heavy ones: `scripts/test-glaeda-cmux-runner.py` runs
as four shards there. Do not run full suites on a workstation. Several agent
sessions share one laptop, and a full runner-hook suite spawns dozens of fake
runner processes for minutes; parallel copies of it (and orphans left by a
crashed session) have driven the owner's Mac past load 150.

Locally, run only what the change touches:

```bash
./scripts/verify focused                  # script lints, compile, format, repo-query test
python3 scripts/test-<file>.py -k <name>  # only the cases you changed or added
```

`focused` runs the two script lints always and its three cargo phases only when the diff against
`origin/main` can affect them; a skipped phase prints the paths it read. When the diff cannot be
read at all, every phase runs.

Run one test file or `-k` selection at a time, in the foreground, with a
timeout, and never leave a test process behind when the session ends. Then push
the branch and let the Verify workflow run the full suites; its result is the
final verification. Use `./scripts/verify fast`, `full-tests` or `required`
locally only when the owner asks, or on a fleet mini rather than a workstation.

Documentation-only changes follow the repository docs-only policy. Record when
GitHub's normal Verify workflow is intentionally absent for Markdown/`docs/**`
changes.

## Work rules

- Hostile jobs receive no host integrations or unrelated secrets; constrain
  network and resources, and destroy useful hostile writable persistence after
  the job.
- Every hot-state family needs canonical rebuild inputs and bounded reset,
  revalidation and eviction. Losing hot state may cost time, never truth.
- Invalid or irreversible mutation plans fail before the first executor call.
  Mutations need exact ownership, plan/dry-run behavior, rollback or accurately
  named compensation, and fresh post-action evidence.
- Use absolute executable paths and argument vectors; child environments start
  empty and receive only allowlisted values.
- Tests must not require privileged or physical infrastructure unless explicitly
  isolated and opted into. Planning must not mutate host, GitHub, fleet,
  provider, release, or durable state.
- Measure complete comparable loops: baseline and candidate identity, fixed work,
  semantic validation, latency, resource and storage effects, and reset/fallback.
  Physical experiments need explicit boundaries and cleanup/rebuild evidence.
- Prefer mature OS, filesystem, service-manager and runtime primitives. Add
  dependencies or competing control machinery only for a measured gap.
- Never upgrade a failed or blocked result into a pass.

## Finish

- Complete work in the active session or leave one exact durable blocked
  continuation. Never poll: run one background `glaeda-gh wait pr OWNER/REPO#N
  --sha <full 40-hex>` per PR or run (`skills/github-ci-wait`).
- Review the full diff and exact head. Record relevant checks, authority, and
  provider evidence for the surface changed.
- Claim only evidence observed for the exact tested head and physical effect.
- Low-risk repository edits may use self-review. Privilege, ownership/adoption,
  persistence/recovery, rollback, secrets, concurrency, and destructive changes
  use the independent review required by their owning contract.
- Merge only the reviewed expected head when GitHub reports it mergeable and
  required checks pass. Ordinary repository writes and merges follow repository
  authority and that head. An explicitly authorized task may perform a named,
  host-scoped service, network, or cleanup mutation when it records the exact
  scope, bounded command, receipt, and fresh post-state verification. That
  authorization does not widen credentials or access, approve material spend,
  broad fleet changes, releases/signing, external contact, destructive non-test
  data changes, or irreversible migrations without a proven recovery path.

## Agent-facing writing

Prefer short commands, concrete nouns, explicit outcomes, and one rule per owner.
Repository scripts and tests are the executable truth.
