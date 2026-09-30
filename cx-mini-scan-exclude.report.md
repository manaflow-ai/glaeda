# cx-mini-scan-exclude report

Date: 2026-09-30. Source head before this report: `62dbba34b99d98951e98fc88fff634738123c766`.
The setup change is isolated in worktree branch `fleet/scan-exclude`.

## Evidence recomputation

The direct retained-record sum for 2026-09-25 through 2026-09-30 (UTC), over all JSONL records
with `top_outside`, is:

| process | CPU seconds | CPU hours |
| --- | ---: | ---: |
| fseventsd (root) | 1,115,038 | 309.733 |
| XprotectService (root) | 462,833 | 128.565 |
| mediaanalysisd (cmux) | 261,696 | 72.693 |
| com.apple.Ambien (root) | 207,985 | 57.774 |

The supplied app-host and Ambient values recompute exactly within their stated scope:
`app-host-unit-tests` has 245,363 s (68.156 h) fseventsd and 118,088 s (32.802 h)
XProtect; `tests-build-and-lag` has 80,659 s (22.405 h) of the truncated Ambient label.
The approximate 66 h XProtect headline is reproduced only by a non-canonical run-id-only
nonzero-first grouping (66.703 h); distinct jobs share run IDs. The report therefore retains
`(run_id, job, host, instance, run_attempt)` and publishes the direct 128.565 h total.

## Live diagnosis

PR-mini observations were taken on macOS 26.5/26.5.1 while real Xcode jobs were active. Root
fs_usage showed fseventsd activity under runner `_work/_temp` XCResults, DerivedData
`TestResults/metadata.db*`, `/private/tmp/cmux-ah-*`, and shared fleet capacity. XProtect's
scanner read temporary test artifacts under `/private/var/folders/.../T/`; unified logs showed
Gatekeeper/Developer Tools checks attributed to `Runner.Listener` and `SWBBuildService`, followed
by XProtect scans for unsigned/not-yet-allowed built apps and Rust build helpers. Sampled
products had no quarantine/provenance xattrs. `mediaanalysisd` held only Photos/Syndication and
MediaAnalysis databases for the CI user. `AmbientDisplayAgent` is a root system XPC service and
was unrelated to CI paths.

Spotlight indexing is already disabled on `/` and `/System/Volumes/Data` on sampled minis;
path-level `mdutil` reports `unknown indexing state` because the parent volume is disabled.
Therefore `.noindex`, `.metadata_never_index`, and Spotlight GUI exclusions cannot account for
or materially reduce current fseventsd CPU. fseventsd is independent of Spotlight.

## Canary

The scoped canary on `cmux14` (macOS 26.5.1, CI uid 501) disabled only
`gui/501/com.apple.mediaanalysisd` and `gui/501/com.apple.photoanalysisd`, then terminated their
current per-user processes. SIP blocked `launchctl bootout` (operation 150), but
`launchctl print-disabled gui/501` recorded both services disabled. No cache, runner, job, or
security-policy files were moved or changed. An hour-long sampler is running from
2026-09-30T19:07Z; it records daemon CPU, total CPU, and active xcodebuild count every 10 s.
This report will be amended with the completed post-canary aggregate before publication.

## Decision

The implementation adds idempotent `.metadata_never_index` markers only to user-owned
DerivedData, the Glaeda native cache, and existing runner `_work` roots. It refuses symlinked
runner roots and preserves pre-existing marker bytes/ownership. This is future-proof setup hygiene
and does not claim to fix fseventsd on hosts whose Data volume is already index-disabled.

Do not roll out the APFS volume or XProtect/Gatekeeper exemption from this evidence. The APFS
experiment needs a drained mini, exact volume identity, remount, and rollback; XProtect scans
unsigned executables and broad temp artifacts even without quarantine xattrs, so a path exemption
would weaken malware scanning without a measured job benefit. Leave AmbientDisplayAgent alone.

The only scoped runtime mitigation supported by the current evidence is disabling media/photo
analysis for a dedicated CI user, subject to the completed one-hour before/after sampler and the
rollback commands in `cx-mini-scan-exclude-probes.md`.
