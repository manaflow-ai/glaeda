# Native runner-log benchmark

`scripts/benchmark-native-runner-log.py` runs the existing Python `cruft_sweep.py`
baseline and the hosted native runner-log adapter against fresh private Darwin
fixtures. The matrix defaults to 1, 16, and 64 stale diagnostic logs and one
trial; each fixture also contains a recent log, an expired held-open log, a
busy `Runner.Worker` root, and aged protected warm state. The harness records
wall, user, system, and peak child RSS, action counts, bytes, and each side's
receipt identities.

The native side must be a verified hosted artifact. Pass its binary hash, build
receipt, the managed Mac adapter receipt, and exact source commit/tree. The
managed receipt must report a successful synthetic matrix and
`live_deletion_enabled=false`; the benchmark rejects a receipt that is not bound
to the same source/tree. `--checkout-commit` and
`--checkout-tree` bind that receipt to the checkout being benchmarked. The
ordinary observer intentionally returns
`reclaim_not_ready` for `--apply`; the harness records that as
`blocked_native_effect_comparison` and does not permit a speedup claim. The
isolated fixture lane passes the explicit `--synthetic-fixture` flag; that
enrollment-gated path can delete only the private temporary fixture and does
not enable live runner deletion. A missing binary likewise runs the Python
baseline and emits a blocked receipt.

Example using the existing optimized Python baseline:

```sh
python3 scripts/benchmark-native-runner-log.py \
  --python-source /Users/leoli/Projects/cmuxterm-hq/build-fleet/mini-ops/cruft_sweep.py \
  --python-source-sha256 0e82415eda097505d8ff8496b8876330a990937d084d8a437a2c49f75168197a \
  --native-binary /absolute/path/to/glaeda-runner-log-reclaim \
  --native-sha256 VERIFIED_BINARY_SHA256 \
  --native-receipt /absolute/path/to/glaeda-runner-log-reclaim.receipt.json \
  --managed-adapter-receipt /absolute/path/to/managed-admission-receipt.json \
  --source-commit VERIFIED_HOSTED_SOURCE_SHA \
  --source-tree VERIFIED_HOSTED_SOURCE_TREE \
  --checkout-commit VERIFIED_BENCHMARK_HEAD \
  --checkout-tree VERIFIED_BENCHMARK_TREE \
  --output /absolute/path/to/native-runner-log-benchmark.json
```

The JSON receipt is the authoritative result. `speedup_claim_allowed` is true
only for `matched_fixture_effects`, when the native build receipt binds the
exact source commit and tree to the benchmark checkout, and every matrix row
has successful recovery, per-item, and post-action receipts. Native apply must
have no errors, the expected stale files must be gone, and every protected
file—including the aged warm marker—must survive. A row with only an action
count, a binary hash, or a plan result remains blocked.
Python batch IDs and native device/inode receipt IDs are intentionally reported
with different `identity_kind` values; they are not identity parity. These
isolated fixtures do not measure fleet throughput or authorize production
deletion.
