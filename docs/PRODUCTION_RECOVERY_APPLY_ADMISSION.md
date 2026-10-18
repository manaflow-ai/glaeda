# Production runner-log recovery/apply admission

This is the review contract for turning the merged native runner-log observer,
descriptor-bound store, and report-only handoff into a production adapter. It is
design guidance only. The current primary review and synthetic-matrix status is
in [`NATIVE_RUNNER_LOG_PRODUCTION_STATUS.md`](NATIVE_RUNNER_LOG_PRODUCTION_STATUS.md).
The next typed cancellation/replay contract is in
[`LIVE_APPLY_ADMISSION_DESIGN.md`](LIVE_APPLY_ADMISSION_DESIGN.md).
This branch does not enable live deletion.

## Current boundary and blockers

The current code has distinct observation, recovery, synthetic mutation, and
production-apply boundaries:

* `runner_log_reclaim::observe` validates enrollment, observes the process
  scope and returns a bounded diagnostic catalog. Ordinary `reclaim(...,
  apply=true)` still emits `reclaim_not_ready` and ignores requested state.
* `RunnerLogStore` is production-compiled; raw `open`, `recover` and
  `apply_batch` operations remain crate-private. The public synthetic fixture
  helper is confined to the benchmark layout and synthetic enrollment.
* `admit_descriptor_store` performs recovery under the lock, vetoes pending
  recovery and checks report/action binding. The lock is dropped on return;
  this is not yet a consuming live-apply capability. `apply_admitted` rejects
  this backend and `apply_live` returns `live_apply_disabled`.
* The raw `apply_batch` calls recovery but discards its pending result. A
  future production entry point must not bypass the adapter's pending-debt
  veto or use recovery to resume deletion after cancellation.
* The handoff script is report-only. Its inspected clean journal is not
  evidence that descriptor-bound recovery ran. The synthetic lifecycle adapter
  serializes checkpoints but does not itself persist/fsync them.

The proposed durable live path and cancellation/replay rules are specified in
[`LIVE_APPLY_ADMISSION_DESIGN.md`](LIVE_APPLY_ADMISSION_DESIGN.md). They remain
unimplemented and take precedence over historical design sketches below where
cancellation or durability details differ. No green synthetic row admits live
filesystem mutation.

## Safest API boundary

The production surface should be one adapter-owned, immutable admission token
that is created only after a complete plan and consumed by a store operation.
The store should not accept an arbitrary `&[LogItem]` as its production entry
point. The token should bind:

1. **Enrollment and filesystem identity.** The exact `install_id`, enrolled
   runner path, root `(device, inode, uid)`, diagnostics directory identity, and
   marker identity/value. `RunnerLogStore::open` already checks enrollment,
   marker, owner, descriptor-relative paths, and a non-blocking state lock
   ([`src/runner_log_reclaim_store.rs:455-537`](../src/runner_log_reclaim_store.rs#L455)).
2. **Policy and plan.** Schema version, minimum idle seconds, batch limit,
   plan time, and an ordered candidate list. Each candidate carries the
   relative path, complete `FileIdentity`, decision, and the plan receipt ID.
   The adapter must reject a candidate whose decision is anything other than
   `ExpiredRunnerDiagnosticLog`, whose identity is absent, or whose candidate
   list exceeds the bound.
3. **Observation authority.** A complete process observation with the enrolled
   UID, no busy process, no unreadable in-scope process, and no open candidate
   identity. An incomplete scan is a veto, not an empty candidate set. If the
   adapter also gates on `ActionsRunnerReadinessObservation`, it must bind the
   exact Lima instance/source identity, configured root and installation
   digests, and a fresh readiness state; public readiness output intentionally
   keeps command evidence private ([`src/actions_runner_readiness/model.rs:322-350`](../src/actions_runner_readiness/model.rs#L322)).
4. **Recovery generation.** The state-root identity, journal digest/size, and a
   recovery disposition. `clean` is admissible only after a store recovery pass
   under the lock; a report-only handoff's `clean` means only “no pending rows
   were observed”. A prepared, staged, or delete-intent row must be represented
   in the token's recovery receipt.
5. **Cancellation and expiry.** An opaque admission ID, cancellation epoch,
   plan deadline, and batch sequence. The store must revalidate the epoch and
   deadline while holding the lock before staging each candidate. Cancellation
   is durable adapter state, not a best-effort process flag.

The token is the only value accepted by a future `apply` method. The method
should take a fresh observer callback internally (or a typed observer object),
not an untyped closure supplied by a caller. It returns a typed apply receipt
and a post-action observation; it never returns success without both.

## Required receipts

The adapter must persist and return bounded receipts at each authority edge:

| Receipt | Required evidence | Meaning |
| --- | --- | --- |
| Plan | observer schema/version, exact policy, complete flag, root/install binding, candidate `(relative_path, FileIdentity, receipt_id)` list, and plan digest | A read-only candidate set; it authorizes nothing by itself. |
| Admission | plan digest, state-root identity, journal digest/length, recovery result, cancellation epoch, expiry, and batch sequence | The store may begin a bounded transaction under the lock. |
| Recovery | pending at start, rows repaired/aborted/projected, rows still pending, and the exact error/refusal code | Recovery happened under the same root/install binding. |
| Item apply | admission ID, transaction ID, candidate identity, phase, and bounded outcome (`reclaimed`, `skipped`, or refusal) | One item’s durable state transition. |
| Post-action | fresh root/diagnostics identity, process completeness/busy/open facts, journal digest/length, and remaining pending count | The only evidence for reporting the resulting state. |

Public receipts must retain only bounded opaque IDs, relative paths, identity
numbers, enums, counts, and timestamps. Do not copy absolute runner paths,
command output, process arguments, credentials, or private readiness evidence.

## Lifecycle invariants

### Initial admission

1. Read and validate enrollment and policy.
2. Run one complete observer plan. Require `complete=true`, no busy process,
   and no open candidate identity.
3. Open the descriptor-bound store and acquire its non-blocking lock.
4. Recover existing journal rows before admitting new candidates. A recovery
   error or remaining pending row blocks apply.
5. Re-observe and compare the plan candidates by full `FileIdentity`, policy,
   root/install binding, and cancellation epoch. A mismatch skips the item or
   refuses the whole batch; it must never widen the candidate set.

### Cancellation

Cancellation must stop admission before the next `prepared` journal row. A
request observed after a row is `prepared` cannot delete the source or silently
drop the row: the adapter records cancellation, exits without further staging,
and leaves recovery to the journal. A staged or delete-intent row is recovery
debt; it remains preserved while cancellation is active. Fresh identity/open-process
checks alone do not authorize deleting a cancelled stage; use the separately
reviewed compensation/recovery policy in the live-apply design. Cancellation
must not be reported as completion until a post-action
receipt says there are no pending rows for that admission.

### Restart and crash recovery

The first operation after restart is `open` plus recovery under the lock. The
store's existing phase behavior is the required baseline:

* `prepared` with no stage file is projected to `aborted`; the original source
  remains. `prepared` with a stage file refuses recovery and blocks apply
  ([`src/runner_log_reclaim_store.rs:656-695`](../src/runner_log_reclaim_store.rs#L656)).
* `staged` is deleted only when the staged identity is unchanged, private, idle,
  and not open. An open staged file remains pending for a later recovery pass.
* `delete_intent` with an absent stage is projected idempotently; an existing
  stage follows the same final identity/open checks. Repeating recovery after
  projection is a no-op, as covered by the existing recovery tests
  ([`src/runner_log_reclaim_store.rs:979-1017`](../src/runner_log_reclaim_store.rs#L979)).
* A writable, hard-linked, replaced, or root-alias-changed stage is a refusal;
  it is never “fixed” by deleting an unknown object
  ([`src/runner_log_reclaim_store.rs:1066-1100`](../src/runner_log_reclaim_store.rs#L1066)).

The adapter must return the recovery receipt even when no new candidates are
applied. A process restart may repeat recovery; it must not mint a second
terminal result for a projected transaction.

### Idempotency and replay

Journal phase order is monotonic: `prepared` → `staged` → `delete_intent` →
`projected` or `aborted`. A phase regression or conflicting duplicate row is a
journal error, not a new attempt. The journal writer syncs each row and its
parent before the next filesystem effect ([`src/runner_log_reclaim_store.rs:352-382`](../src/runner_log_reclaim_store.rs#L352)).

The current transaction ID includes a random nonce
([`src/runner_log_reclaim_store.rs:384-399`](../src/runner_log_reclaim_store.rs#L384)),
so an adapter must use its admission/plan digest to prevent replaying the same
plan as a fresh batch after an ambiguous caller timeout. A successful terminal
receipt is returned from durable journal state; the caller must not infer
completion from a process exit or a lost response.

## Exact blockers to live deletion

Live deletion remains blocked until all of the following are true:

* a production-visible store API accepts only a typed admission token, not
  arbitrary candidates and an arbitrary observation closure;
* the adapter refuses to start a new batch when recovery returns any pending
  row, including an open staged row (the current crate-private `apply_batch` does
  not enforce this on its own);
* the adapter can prove that the plan receipt and store-open identities refer to
  the same install/root/diagnostics generation;
* recovery runs under the store lock and returns a durable receipt before any
  new stage; prepared, staged, or delete-intent debt cannot be hidden by a
  report-only “clean” handoff;
* cancellation has a durable epoch and defined behavior at every journal phase;
* restart/replay returns the existing terminal transaction result and cannot
  mint a duplicate apply after a lost response;
* every item is re-observed immediately before stage, after stage, before
  delete, and after delete, with fresh post-action evidence; and
* a production CLI emits bounded plan, recovery, item, and post-action receipts
  and has focused tests for replacement, open identity, busy/incomplete scan,
  root alias change, malformed/foreign journal rows, cancellation, restart, and
  repeated recovery.

Until these conditions are met, `glaeda-runner-log-handoff` is the correct
production adapter behavior: plan and report, then stop before opening the
store or deleting a live file.
