# Production runner-log recovery/apply admission

This is the review contract for turning the merged native runner-log observer,
descriptor-bound store, and report-only handoff into a production adapter. It is
design guidance only. The current primary review and synthetic-matrix status is
in [`NATIVE_RUNNER_LOG_PRODUCTION_STATUS.md`](NATIVE_RUNNER_LOG_PRODUCTION_STATUS.md).
This branch does not enable live deletion.

## Current boundary and blockers

The observer and the store have deliberately different authority:

* `runner_log_reclaim::observe` validates the enrolled root, observes the
  process scope, enumerates `_diag`, and returns a bounded catalog. It takes a
  root snapshot before and after enumeration and fails on root drift
  ([`src/runner_log_reclaim.rs:618`](../src/runner_log_reclaim.rs#L618)). Public
  output redacts the absolute root to `<enrolled-runner>`
  ([`src/runner_log_reclaim.rs:652`](../src/runner_log_reclaim.rs#L652)).
* `runner_log_reclaim::reclaim` is still a planner. Even when `apply=true`, it
  only sets `operation` to `apply`, returns `reclaimed_count: 0`, and emits
  `reclaim_not_ready`; it ignores the requested state root
  ([`src/runner_log_reclaim.rs:676`](../src/runner_log_reclaim.rs#L676)).
* `RunnerLogStore` is currently compiled as a test-only private module
  (`#[cfg(test)] mod runner_log_reclaim_store` in [`src/lib.rs:400`](../src/lib.rs#L400)).
  Its `open`, `recover`, and `apply_batch` methods therefore cannot be called
  by a production binary. Making the module public by itself would be an
  unsafe API expansion: `apply_batch` accepts caller-built `LogItem` values and
  a caller-supplied observation closure, with no proof that those values came
  from one exact plan.
* `apply_batch` currently discards the `RecoveryReceipt` returned by
  `recover` ([`src/runner_log_reclaim_store.rs:728-745`](../src/runner_log_reclaim_store.rs#L728)).
  A staged row whose file is still open is left pending by `recover`
  ([`src/runner_log_reclaim_store.rs:696-699`](../src/runner_log_reclaim_store.rs#L696)),
  but the same `apply_batch` call can then continue to stage new candidates.
  A production adapter must treat any non-zero `pending` result as a hard
  admission veto and return the recovery receipt before considering a new
  candidate.
* The handoff script is intentionally report-only. It invokes the observer in
  plan mode and reads, but never opens or repairs, `journal.jsonl`
  ([`docs/RUNNER_LOG_HANDOFF.md:14-21`](RUNNER_LOG_HANDOFF.md#L14)). A missing
  state root is reported as clean by this inspection path; production apply
  must distinguish “no journal exists” from “recovery was performed”.

These are admission blockers, not reasons to bypass the boundary. Until the
adapter below exists, the only safe production operation is plan plus handoff
validation.

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
debt; it may be deleted only by the same fresh identity/open-process checks used
by recovery. Cancellation must not be reported as completion until a post-action
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
  row, including an open staged row (the current test-only `apply_batch` does
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
