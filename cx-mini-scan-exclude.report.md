# cx-mini-scan-exclude report

Date: 2026-09-30. Merged source head: `fcfa79c71770520887be22d034cb3fa0fc759d9c`.
The setup change was developed in worktree branch `fleet/scan-exclude` and independently
accepted at exact head `6a9f1977f05f3968fee7b7682687ce6f6f25b245`.

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
security-policy files were moved or changed. After one hour of real jobs (19:07:03--20:07:03
UTC), the state was restored with `launchctl enable` for both labels.

The canary did not prove a useful CPU or latency reduction. The sampler's daemon columns were
discarded because macOS `ps comm` returned full paths on this host; job records provide the
validated bounded comparison:

| window | completed jobs | fseventsd top-outside CPU | XProtect top-outside CPU | all-job wall seconds | app-host p50 |
| --- | ---: | ---: | ---: | ---: | ---: |
| preceding hour | 25 | 1,629 core-s | 769 core-s | 11,004.3 s | 606.3 s (n=7) |
| canary hour | 31 | 377 core-s | 1,347 core-s | 9,673.0 s | 680.2 s (n=5) |

The windows contain different job mixes, and `mediaanalysisd` was running again at the end even
though both labels were disabled; `photoanalysisd` remained stopped. The result is therefore
inconclusive for mediaanalysisd and shows no job-time benefit. Both labels were re-enabled, and
this mitigation is not rolled out. The earlier sampler's zero columns are not used as evidence.

## Decision

The implementation adds idempotent `.metadata_never_index` markers only to user-owned
DerivedData, the Glaeda native cache, and existing runner `_work` roots. It refuses symlinked
runner roots and preserves pre-existing marker bytes/ownership. This is future-proof setup hygiene
and does not claim to fix fseventsd on hosts whose Data volume is already index-disabled.

Do not roll out the APFS volume or XProtect/Gatekeeper exemption from this evidence. The APFS
experiment needs a drained mini, exact volume identity, remount, and rollback; XProtect scans
unsigned executables and broad temp artifacts even without quarantine xattrs, so a path exemption
would weaken malware scanning without a measured job benefit. Leave AmbientDisplayAgent alone.

No runtime mitigation is supported by the current canary. Keep the media/photo rollback commands
in the probe document for a future dedicated CI account where the launchd behavior can be verified
before/after. Do not disable the services on the shared user fleet-wide.

## Rollout receipt

PR [#1377](https://github.com/teamleaderleo/glaeda/pull/1377) merged as
`fcfa79c71770520887be22d034cb3fa0fc759d9c`; its Verify and advisory checks were green. The release
workflow `36771939043` and fleet-candidate workflow `36772862535` both completed successfully for
that exact main head. `glaeda-mini-fleet upgrade --candidate-run 36772862535 --yes` then failed
closed without changing hosts: active host locks skipped busy minis, and the remaining minis
reported `needs a person: disk` during preflight. No cache directory was moved and no build was
interrupted. Re-run the same candidate upgrade after the disk/person blockers are cleared; the
candidate expires 2026-10-30T20:30:42Z.
