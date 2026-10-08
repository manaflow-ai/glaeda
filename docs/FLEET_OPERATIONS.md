# CMUX fleet operations

This is the operator front door for the CMUX Mac fleet. Use the `fleet` wrapper
from cmuxterm-hq for host operations, and use the Glaeda commands for typed
observation, reconciliation, disk receipts, and seed decisions. Plans are
read-only unless a command ends with `--apply` or `--yes`.

## Which source owns which fact

| Fact | Source of truth | Read surface |
| --- | --- | --- |
| Host roles, hardware, labels, availability, and routing | cmuxterm-hq `build-fleet/mini-fleet.json` | `glaeda-mini-fleet` manifest and `fleet runner status` |
| What a host actually reports | the host probe | `glaeda-mini-fleet observe` and `glaeda-fleet-status` |
| Current worker, queue, and controller state | the controller status endpoint | `build-fleet/status.py --json` or its saved JSON |
| Installed Glaeda generation | the host's OTA receipt | `glaeda-fleet-status` tooling source and `fleet up` |
| Disk pressure and reclaim receipts | the host's `glaeda-disk` state | `glaeda-disk --json` and `glaeda-fleet-status` disk source |
| Seed usefulness and transfer cost | job records and seed receipts | `glaeda-seed-prefetch`, seed logs, and build receipts |
| Dashboard presentation | ci-dash's deployed data document | the dashboard version and role metadata in its snapshot |

If two surfaces disagree, refresh the observation first. Do not repair a host
from a stale dashboard row or from a copied manifest that is not the current
cmuxterm-hq `main` manifest.

## One bounded status pass

For a human-readable report with host checks, disk, recent job contention, and
installed-tooling age:

```bash
python3 scripts/glaeda-fleet-status --output text
```

For a replayable document and page:

```bash
python3 scripts/glaeda-fleet-status collect --sources-out fleet-sources.json
python3 scripts/glaeda-fleet-status build fleet-sources.json --out fleet-status.json --html fleet-status.html
```

The collector is read-only apart from the normal `glaeda-disk` size snapshot.
It does not query GitHub for runner or queue state unless
`--github-runners` or `--queue-repo` is passed. The default manifest copy may
perform the one-hour cached comparison with cmuxterm-hq `main`; pass an explicit
`--manifest` or use the saved sources bundle when investigating a known
incident instead of repeatedly fetching provider state.

## Host and runner checks

Run the cmuxterm-hq operator wrapper for the live fleet view:

```bash
fleet --no-fetch up all
fleet --no-fetch runner status HOST...
fleet --no-fetch xcode-pin-audit HOST...
```

`runner status` compares registered labels with the labels Glaeda main would
register from the manifest. It is the read-only prerequisite to:

```bash
fleet runner relabel HOST --apply --wait
```

The relabel command waits for current jobs and re-registers the existing runner
in place. It does not change workflow routing variables.

AWS and other routed hosts must be reached through the manifest route. Do not
replace that route with direct SSH, a copied key, or a guessed login user.

## Cleanup and disk policy

Always inspect both layers. The cmuxterm-hq sweep covers older broad categories;
Glaeda owns the current per-family safety checks and receipts.

```bash
fleet --no-fetch cruft-sweep --all
fleet --no-fetch disk-policy --all
ssh HOST '~/.local/bin/glaeda-disk --pressure --idle --json'
```

The first two commands plan by default. Apply only the host-specific reclaim
that the fresh receipt identifies as rebuildable:

```bash
ssh HOST '~/.local/bin/glaeda-disk --pressure --idle --apply'
```

Never remove active workspaces, current seeds, checkouts, user projects, or
unclassified space by hand. A low free-space number is a reason to refresh the
receipt, not permission to delete a directory.

## Probe and dashboard rollout

The probe and dashboard have separate rollout steps:

```bash
fleet --no-fetch push-probe
fleet --no-fetch deploy ci-dash --dry-run
fleet --no-fetch autodeploy status DASHBOARD_HOST
```

After a dashboard or probe change lands, verify the deployed version in the
snapshot before interpreting new role, disk, or health rows. A role row carries
owner, purpose, group, capabilities, availability, and protected/excluded
state. It must not carry credentials, private paths, or SSH details.

## Seeds and shared state

Use seed telemetry before changing retention or cadence:

```bash
glaeda-seed-prefetch
fleet --no-fetch seed-archive --plan HOST...
```

Keep the trusted archive and per-root seed receipts separate from PR runner
state. A seed is useful when it reduces measured admission work for the same
toolchain and source lineage. Its presence alone is not evidence that it should
be retained.

## Runbook map

- [`FLEET_STATUS.md`](FLEET_STATUS.md) defines the typed status document and finding schema.
- [`HOST_RECONCILIATION.md`](HOST_RECONCILIATION.md) defines desired, observed, and planned host state.
- [`CMUX_FLEET_EXECUTION_ROLES.md`](CMUX_FLEET_EXECUTION_ROLES.md) defines workload roles and capabilities.
- [`CMUX_MINI_RUNNER.md`](CMUX_MINI_RUNNER.md) defines runner admission, hooks, labels, and seed behavior.
- cmuxterm-hq `skills/infra/fleet-ops/SKILL.md` maps incidents to one operator command.
- cmuxterm-hq `skills/infra/build-fleet/SKILL.md` defines build routing and fleet-only native validation.

The old `macfleet` skill and direct SSH build allocation path are retired. Keep
the compatibility files for callers, but do not use them for new operations.
