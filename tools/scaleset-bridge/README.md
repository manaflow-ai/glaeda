# Glaeda Runner Scale Set bridge

This is a deliberately small process boundary around GitHub's official
[`actions/scaleset`](https://github.com/actions/scaleset) Go client. The module pins exact commit
`cb0405b2d874500e75ae34eff8d582ab75956b45` through the Go pseudo-version in `go.mod`.

The bridge exists so Glaeda does not reimplement GitHub's Runner Scale Set authentication,
message-session refresh, long polling, JIT configuration, or runner API semantics in Rust. It does
not use the client's convenience listener because that listener acknowledges a message before it
invokes lifecycle handlers. Glaeda needs this ordering instead:

```text
poll through official client
-> return one bounded normalized message to Rust
-> persist/reconcile it under the durable Glaeda lock
-> explicit ack from Rust
-> delete the message and acquire its available job request IDs
```

## Process contract

The binary rejects arguments, clears its inherited environment, and exchanges one bounded JSON
object per line on stdin/stdout. A bounded strict response gate rejects unknown service lifecycle
types before the pinned official client can normalize them. Protocol version 3 supports:

- `start`: accept exactly one credential through stdin, either the GitHub App private key
  (`client_id`, `installation_id`, `private_key`) or a GitHub App installation token
  (`installation_token`, `installation_token_expires_at` in Unix seconds), verify one
  already-enrolled exact scale set by ID/name/group/labels/disabled-update policy, and create a
  message session. A token with under 15 minutes of life is refused with `credential_expiring`;
- `poll` and `ack`: expose the latest validated statistics and bounded job lifecycle messages at an
  explicit capacity from zero through the enrolled maximum, without automatic acknowledgement,
  and accept only positive unique acquired IDs from the exact persisted available-job set;
- `resume`: restore one positive durable acknowledged-message cursor exactly once in a fresh
  process before polling, so zero-capacity lifecycle observation cannot admit another job;
- `generate_jit`: return one exact runner ID/name plus its one-time encoded JIT configuration;
- `observe_runner` and `remove_runner`: observe by exact name and remove only after re-observing the
  exact numeric ID, name, and configured scale-set identity.

Version 3 adds the installation-token start. A token session ends itself at its idle boundary:
once fewer than 10 minutes of token life remain, `poll` answers `credential_expiring` before any
service call (an outstanding `ack` still completes). The Rust supervisor drops that session and
starts a fresh bridge with a fresh read of the pushed token file. The pinned client sends the token
as the bearer of its registration-token exchange, exactly as it sends a personal access token
(`NewClientWithPersonalAccessToken`).

Why a token: a GitHub App private key never leaves its home host's login keychain
(leo-workspace `tools/app_key_guard.py`). That host mints one-hour installation tokens and pushes
them over ssh as a mode-600 JSON file every 30 minutes:

```json
{"app_id": 5067688, "token": "ghs_...", "expires_at": "2026-11-18T10:00:00Z",
 "permissions": {"organization_self_hosted_runners": "write", ...}, "minted_at": "..."}
```

Errors contain fixed codes only. The App private key and installation token never appear in argv,
the environment, stdout, or an error. The encoded JIT configuration appears only in the successful `jit` response
and remains secret-bearing data for the future Rust/guest handoff.

GitHub.com can directly assign organization Scale Set work with `runnerRequestId=0`, without a
preceding Available event. For that shape only, the bridge derives a stable positive private join
key from the exact job and assignment evidence. Distinct assignment times remain distinct, and the
Rust consumer reserves capacity directly from the Assigned event without calling `AcquireJobs` or
claiming a runner identity before JIT registration. A runnerless `Completed(canceled)` for that
exact assignment releases and retires the untouched reservation without acquiring VM or runner
deletion authority. The Quarry pilot observed GitHub.com repeat the exact nanosecond assignment
timestamp from message `100000001` (`Assigned`) in message `100000002`
(`Completed(canceled)`), for workflow run `31951901986`; this is the bounded live-service evidence
for the private join across the pre-runner reassignment lifecycle.

## Current nonclaims

The private Rust adapter and durable delivery controller now call this package, load the
pre-enrolled App key from the Mac Keychain, persist/reconcile messages before acknowledgement, and
recover ambiguous acquisition without replaying acknowledgement. This package still does not
create or adopt a scale set, launch a VM, transfer JIT data to a guest, supervise a runner, or by
itself settle an empty ambiguous acquisition. Cursor restore and zero-capacity polling are only the
process prerequisite for the next durable lifecycle-evidence transaction, not a usable autoscaler.

## Enrollment: `cmd/scaleset-enroll`

The bridge refuses to create a scale set. `cmd/scaleset-enroll` is the separate, explicit one-shot
that does, sharing this module's pinned client. It reads the pushed token file (`--token-file`,
no link followed, owned by the caller, mode 0600 or 0400, the expected `--app-id`, runner-admin
write, at least 5 minutes of life) and then:

1. ensures an org runner group (default `cmux-fleet-linux`, the name cmux's runner picker already
   accepts) with visibility `selected` = exactly one repository (default `manaflow-ai/cmux`), no
   public repositories, and `restricted_to_workflows` with the `--workflow` list. Every workflow
   must be pinned to `@refs/heads/main` or `@refs/heads/feat-cmux-next` of that repository: trusted
   base-branch code only, never pull request refs, because the guest egress gate (M4) is not built;
2. ensures a scale set (default `glaeda-linux-arm64`, label `glaeda-linux-arm64`, runner
   self-update disabled) in that group;
3. prints bounded JSON whose `enrollment` object holds the scale set ID, name, Actions runner group
   ID and labels for the disposable-worker enrollment.

It plans by default and mutates only with `--apply`. It never deletes. An existing group or scale
set is adopted only when its shape matches exactly; otherwise it refuses with a `next` hint
(`runner_group_conflict` suggests `--runner-group glaeda-linux-trusted`). Changing an existing
matching group's workflow list needs `--update-workflows`. Plan mode performs only reads, though
the pinned client mints a short-lived runner registration token to read the Actions service.
A failed `--apply` is converged by rerunning the same command: every run re-observes first.

```bash
go build -trimpath -o /tmp/scaleset-enroll ./cmd/scaleset-enroll
/tmp/scaleset-enroll --token-file "$HOME/.config/glaeda/scaleset-token.json" \
  --workflow manaflow-ai/cmux/.github/workflows/iroh-v2-production-drift.yml@refs/heads/feat-cmux-next
# review the plan, then rerun with --apply
```
