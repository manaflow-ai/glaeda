# Typed live-apply admission design

Status: proposed contract for review, not an implemented API or approval to
mutate live data. Ordinary CLI apply remains `reclaim_not_ready` and the
production adapter's `apply_live` remains `live_apply_disabled`. The separate
synthetic fixture helper is not a live admission capability.

## Observed implementation and missing guarantees

`ProductionRecoveryAdapter::admit_descriptor_store` opens the descriptor-bound
store, compares a store-generation value, runs recovery, vetoes pending rows,
and binds report actions to the observation. It does not retain the store lock
after returning. Policy is used during recovery but is not included in the
current plan digest. Root/enrollment validation at open is not a complete
immutable binding between the report and the eventual apply.

The adapter's events are in memory. `checkpoint` returns serialized bytes and
`restart` reconstructs a synthetic projection; neither persists those bytes or
proves fsync. The descriptor store separately persists file-transaction phases
and generates random transaction IDs. It has no durable admission identity,
cancellation epoch, or stored terminal apply result.

The proposed path must close those gaps without letting a retry, cancellation,
expired token, or synthetic observer acquire new deletion authority.

## Capability and operation identity

Use a distinct, non-deserializable `LiveApplyAdmission` with private fields.
Only the validated store/admission boundary may mint it, while holding the
transaction lock after recovery. A public receipt is evidence, not a token.
The apply call consumes the capability, retains the lock through the batch,
and checks fresh authority again before each effect. A capability cannot be
cloned, restored from JSON, or retained across process restart.

Bind the admission receipt and capability to:

| Binding | Required evidence |
| --- | --- |
| Operation | installation namespace, persisted caller request key, plan digest, batch sequence |
| Filesystem | held root/diagnostics/marker/state identities, UID, enrollment value/digest |
| Store | exact generation tuple and journal format version; the current folded `u64` alone is insufficient |
| Plan | ordered bounded candidate paths/identities, observer provenance and completeness |
| Policy | schema, idle window, batch limit, expiry policy, canonical policy digest |
| Journal | validated prefix digest, byte length and sequence observed under lock |
| Cancellation | current durable epoch and operation status |
| Lifetime | bounded monotonic deadline and boot identity; restart invalidates the capability |

The caller must persist its request key before submission. The store durably
maps `(installation, request_key, batch_sequence)` to one admission ID before
any filesystem effect. Matching retries find that operation; the same key with
a changed plan, root, or policy is a conflict, never a new admission. A new
batch uses a new sequence only after the preceding batch is terminal.

Journal digest/length bind an observed prefix, not the final length: the
operation's own validated append sequence advances that prefix. An unrelated
append, truncation, generation mismatch, or conflicting duplicate fails closed.

## Durable records and receipt publication

Use a versioned tagged journal format, bounded record/candidate sizes, strict
phase validation and checked sequence/epoch arithmetic. The existing unversioned
item journal cannot silently become this format. Legacy pending rows block new
admission until reviewed recovery; no automatic rewrite grants ownership.

Proposed records are:

```text
admission_opened(operation_key, admission_id, exact_bindings, candidates)
item_prepared(admission_id, item_index, transaction_id, source_identity, stage_name)
item_staged(admission_id, item_index, staged_identity)
item_delete_intent(admission_id, item_index)
item_projected(admission_id, item_index, outcome)
admission_terminal(admission_id, canonical_receipt, receipt_digest)
```

Persist transaction ID, candidate index and stage name before rename. Recovery
reuses those IDs; it never mints another transaction for an unresolved item.
Existing prepared rows with an ambiguous stage remain refusals until separately
proven recoverable. New journal vocabulary does not resolve that ambiguity.

Each authority-bearing append is synced before the next effect. The terminal
record stores the bounded canonical receipt bytes, not just a digest pointing
to unavailable data. Sync the journal and required parent directory before
returning completion. If publication or sync is ambiguous, return an uncertain
progress result; recovery must validate and complete publication before replay.
Torn/invalid records block mutation rather than being silently discarded.

The immutable terminal receipt contains the operation binding, recovery result,
per-item transaction/candidate identities and outcomes, fresh post-action facts,
journal prefix and zero pending recovery. Terminal outcomes are `applied`,
`cancelled`, or `refused`. Already completed effects remain in a cancelled
receipt; cancellation is not rollback.

Replay returns `{ receipt: original_bytes, replayed: true }`. The receipt and
its outcome remain byte-for-byte identical; `replayed` belongs to the response
envelope. Progress responses such as `recovery_pending` are not terminal and
cannot satisfy completion. Conflicting duplicate terminal records are corruption.

Keep bounded tombstones for request-key deduplication. On capacity exhaustion,
refuse new admissions. Compaction requires atomic publication, crash recovery
and an explicit key-retention contract; eviction must not turn an old request
into a fresh deletion. Until that contract exists, do not forget accepted keys.

## Cancellation ordering and locking

A separate process cannot cancel promptly by waiting for the applier's
batch-wide transaction lock. Use a descriptor-bound cancellation ledger and a
short control lock, separate from that transaction lock:

- The applier holds the transaction lock; lock ordering is transaction then
  control. A cancellation writer takes only the control lock and never waits
  for the transaction lock while holding it.
- Cancellation appends a checksummed, operation-bound epoch successor to the
  private control ledger and syncs it before returning `cancel_recorded`.
  Require expected-epoch compare-and-swap; a repeated request ID returns its
  existing receipt, and overflow/conflicting requests are refusals.
- Under the control lock, the applier revalidates aliases, cancellation, expiry,
  process scope and candidate identity before each prepare/stage/unlink effect.
  Hold that lock through the effect and its durable phase publication. It may
  not be released between the final cancellation check and unlink.
- A cancellation receipt therefore fences subsequent effects. An effect already
  ordered before cancellation may finish first; its receipt must remain visible.
  `cancel_recorded` is not `cancelled` completion and does not promise restoration.
- The writer returns the operation ID, request ID, committed epoch and observed
  phase/sequence. The applier records that epoch in progress/terminal receipts.
  Missing, corrupt, foreign or rebound control state vetoes further effects.

The control lock cannot cover the full batch. Each guarded effect has bounded
observation/I/O budgets; lock contention returns a typed retryable result.
An I/O stall may delay cancellation acknowledgement, so the API must not promise
instant cancellation. No success receipt is emitted before durable ordering.

## Cancellation and recovery by phase

| Phase when cancellation is observed | Required behavior |
| --- | --- |
| Admitted, no item prepared | stop; terminal cancelled only after post-action verification proves no debt |
| Prepared, source not staged | stop; durably abort the exact unused prepare after validating source/stage state |
| Staged or delete-intent, stage present | stop unlinking; preserve exact stage and return recovery pending |
| Delete-intent, stage absent | project an already completed deletion only with matching durable intent; never infer from source absence alone |
| Projected | preserve completed item result; cancellation prevents the next item effect |
| Terminal | return the immutable original result; a late cancel cannot rewrite success |

Recovery reads cancellation before touching pending files and uses the same
control lock at effect boundaries. It must not call the current deletion-capable
`recover` unchanged on a cancelled admission. Cancellation permits accounting
for prior effects, not deleting a retained stage. Restoring a stage, if offered,
requires a separately reviewed no-overwrite compensation policy; otherwise
preserve debt for explicit recovery and never report terminal cancellation.

## Apply/replay sequence

1. Open and validate the enrolled root/state/control bindings; acquire the
   transaction lock. Public plan/inspection remains read-only; recovery is a
   separately classified mutation with explicit authority.
2. Resolve the persisted operation key. If terminal, verify the stored receipt
   and return the original bytes without fresh filesystem effects. Expired
   admission deadlines do not prevent retrieving a historical result.
3. For a nonterminal operation, inspect pending phases and the cancellation
   ledger. Perform only recovery authorized by that phase and cancellation
   state; unresolved debt blocks new work.
4. Re-observe report/root/policy/process bindings, cancellation and deadline;
   sync admission before minting the consuming capability. Restart/boot changes
   require fresh admission to the same operation, not a new request key.
5. Run bounded item effects with durable phases and control-lock fencing.
   Incomplete process scope, open identity, busy runner, expiry or drift stops
   new effects. Validate the staged identity separately from rename-changed ctime.
6. Observe actual item/root/process/journal state and recovery debt under the
   locks. Publish terminal only with complete evidence and zero pending rows;
   otherwise return typed nonterminal progress with exact recovery requirements.
7. Sync the complete terminal result before returning. A lost-response retry
   resolves the same operation and returns it without new effects.

## Falsifying acceptance matrix

The implementation must test these failures, not merely the happy path:

| Injection | Required observation |
| --- | --- |
| Lost admission response or retry after prepared | same operation/item IDs; no second rename/unlink |
| Lost terminal response, expired token, or process restart | identical terminal receipt bytes; replay envelope only |
| Same request key with changed plan/policy/root | conflict before any effect |
| Cancel racing prepare, rename, or final unlink | effects ordered before cancel retained; no effect after committed cancel |
| Cancel writer runs while batch lock held | control path can commit between bounded effects without deadlock |
| Cancel plus open/rebound stage, crash or recovery | retained stage/debt; no generic recovery delete or false completion |
| Cancel after terminal | unchanged original terminal outcome |
| Journal/control torn write, fsync failure, foreign generation | uncertain/refused result, preserved objects, no new authority |
| Clock rollback, boot change, epoch overflow, budget exhaustion | no new effect; terminal lookup remains available when valid |
| Malformed or legacy journal and key-retention exhaustion | fail closed; no silent migration or key reuse |

Run phase fault injection and real fresh libproc authority checks on a managed
Mac at the exact source/tree/artifact, then implementation-independent review.
The synthetic matrix validates store effects with supplied fixture facts; it
cannot prove real PID/vnode completeness, cancellation durability or recovery.
A separately authorized, host-scoped canary may follow the complete isolated
acceptance matrix. Promotion requires that canary's soak/health receipts; canary
and promotion are not prerequisites that authorize themselves.

## Next implementation slice

Implement only the strict operation-key/epoch/terminal-receipt reducer and codec
first, without adding a CLI apply capability. Prove conflict refusal, replay
identity, bounds and monotonic transitions. Follow with descriptor-bound journal
and control-ledger persistence plus crash injection; then integrate the consuming
admission under the locks. Keep live apply disabled throughout those slices.
