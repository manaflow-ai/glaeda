# Rolling-tip ledger MVP

The `rolling_tip_ledger` module is the first Glaeda integration boundary for a
continuously building development branch. It is scoped to one stream, such as
`manaflow-ai/cmux@feat-cmux-next`, and is intentionally path-free and
transport-independent.

Each `BuildKey` includes the repository and branch, complete commit and tree
IDs, workflow revision, lockfile and source-bundle digests, toolchain, toolchain
image, capability and reset digests, target, platform, architecture, and
configuration. A published artifact carries that key plus content digest and
producer run provenance. The ledger therefore reuses only an exact build and
does not treat a mutable `latest` label as identity. Publication also records
the execution attempt that produced the bytes, and Glaeda accepts the artifact
only when that exact attempt is currently `succeeded`.

The event envelope is append-only and typed. It covers tip observation, build
execution state, host leases, artifact publication, reuse hit/miss/in-flight
deduplication, promotion requests and decisions, backpressure, and closed
receipts. Execution state is separate from promotion state: a newer tip can be
delayed or rejected while an older build continues running, and no operation
automatically cancels work.

Promotion uses a compare-and-swap revision. `accept`, `delay`, and `reject`
replace the candidate pointer only when the caller supplies its current
revision. `accept` additionally requires a published artifact bound to the
current successful execution attempt; `delay` and `reject` can record an
operator decision for a requested tip before a successful artifact exists. A
stale caller receives a conflict and cannot clobber a newer operator decision.
The in-memory ledger remains the transition kernel. `RollingTipLedgerStore`
provides the first durable adapter: it writes one envelope per JSONL line,
syncs the complete event before exposing the resulting state, and reconstructs
the derived indexes with strict schema, stream, sequence, event-id, and state
validation on reopen. An unterminated final line is treated as a crash tail
and truncated; malformed complete lines fail closed. The store exposes a
receipt query for the latest observed tip, latest artifact, event sequence, and
promotion pointer so Glaeda can forward one idempotent stream to fleet
intelligence without adding scheduler behavior to this module.

The store's transaction boundary clones the kernel, applies one operation,
syncs only its new envelopes, then swaps the clone into memory. A failed write
therefore cannot publish an in-memory state that is absent from the journal.
Lease ownership remains intentionally descriptive: this kernel does not infer
ownership from a lease label or accept an unverified replay.

The first consumer should ingest the existing CI receipts for the
`feat-cmux-next` stream. Scheduler changes, host selection, cancellation, and
workflow edits remain outside this tranche.
