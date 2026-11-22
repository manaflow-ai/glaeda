# ADR 0023: Decentralized land queue

## Status

Proposed. Phase 0 (the per-PR lease store and the merge fence, off by default) ships with this ADR. Phases 1-4 below are the plan.

## Context

Leo, 2026-11-22 08:00Z: "then you guys should've never centralized a merge queue on huge-blue in the first place, and should have fixed this far in advance in the first place, and actually measured w\*\* refusing above cores even means."

At 2026-11-22 07:59Z, huge-blue's own land entries showed this:

- 887 lands were open.
- It merged 29 PRs an hour.
- Queue to merge took 77 min at p50 and 3.4 h at p90.
- The land pool has 12 workers, but only 1-2 were busy, held back by the host load gate and the login's pace.

Its gh-quota call log for the hour before 08:14Z had 465 `session:glaeda-land` calls and 31 merge calls, about 15 calls per merge. Only 68 of the 465 were charged, because most were conditional reads. 363 of the 465 went to the `lander@manaflow-ai` App installation. The quota was not what bound the queue. The single host was.

### Today's land paths

| Step | Where it runs | State |
|---|---|---|
| Queue (`glaeda-gh land`) | Any host. A host whose `land.json` names an `executor` (cosmic-gray, lane VMs, today `["reach","huge-blue",".local/bin/glaeda-gh","land","serve"]`) forwards each request to it. Requests the executor does not take wait in the host's `land/requests/` outbox. | `~/.cache/glaeda/gh/land/{entries,requests}` on the executor host |
| Judge (`land-judge explain`) | The executor's `LandQueue` workers, on huge-blue only | Host files, plus the executor's cache and its gh-quota latch |
| Merge (`gh pr merge --match-head-commit`) | The executor through its gh-quota shim (lander App per owner, else the login). On a lane VM, `lane-gh call land` goes to the lane-gh broker (huge-blue, with cosmic-gray as cold standby), which merges with the host's gh (`owner_land: host-gh`). | Broker `~/.local/state/lane-gh-broker`, flocks in `state/lands/*.lock` |
| Hand-off | A lane VM that is refused for ownership calls `lane-gh call land_handoff`, which runs `glaeda-gh land serve` on the broker host (huge-blue) | The same host files |
| cmux train | `next_autoland` in GitHub Actions on cron `*/5`, with one concurrency group, minting a cmux-bot App token | State branch `reconciler-state/next-autoland` |

### Single points of failure

1. **huge-blue's glaeda land queue.** It is the only executor. Every open entry lives in its files, and no other host can read them.
2. **huge-blue's lane-gh broker.** Every lane VM merge and every land hand-off goes through it. The cosmic-gray standby is cold and keeps its own state, and it takes over only after two empty 60 s checks.
3. **The forward outbox.** While the executor is unreachable, a forwarding host holds its requests (with retries backing off to 600 s) and runs none of them itself.
4. **Host-local locks.** The per-key flocks and the broker's lock files do not span hosts, so no host can safely help another.
5. **Host-local App keys.** A host can merge as the lander App only if it holds the key.
6. **The cmux-ci controller.** It would be the obvious shared store, but it is one JSON file on cmux-lawrence, and peer failover is off (`CMUX_CI_PEERS` is unset).
7. **Nothing measured the wait.** `land-pool` logs busy and gated workers, but nothing split queue-to-merge into its parts: waiting for a worker, judging and steps, and CI.

## Decision

Any host that runs `glaeda-gh` with land enabled is a land worker. The queue lives in a shared store. A host works a PR only while it holds that PR's lease.

### Store: git refs on GitHub

The options were:

| Candidate | Reachable from fleet and lane VMs | New single point of failure | Exists today |
|---|---|---|---|
| cmux-ci controller | Fleet yes; lane VMs over the tailnet, unverified | Yes: one JSON file on cmux-lawrence, failover off | `/v1/fleet/leases` exists (hosts, not PRs) |
| lane-gh broker | Lane VMs yes | Yes: huge-blue, the host this ADR removes | It already queues lands |
| Git refs in a GitHub repository | Every host that can reach GitHub, which every land needs anyway | No. GitHub is already required: without it nothing merges. | hq `state_branch.py` (compare-and-swap with `--force-with-lease`), used by next-autoland, now and land nominations |

The store is git refs on GitHub. It adds no dependency that a merge does not already have, and it reuses a compare-and-swap that the fleet already runs.

Each PR gets its own refs, not one branch. With 887 entries and renewals every few minutes, a single branch would put every write behind one compare-and-swap. Per-PR refs only conflict when two hosts write the same PR, which is the conflict a lease exists to settle. Git protocol pushes and fetches spend no REST quota.

The refs live in a private repository, `teamleaderleo/glaeda-land-state`:

- `refs/glaeda-land/leases/OWNER/REPO/N`: the lease (this PR).
- `refs/glaeda-land/entries/OWNER/REPO/N`: the entry, the same document as today's `land/entries/*.json` (phase 2).
- `refs/glaeda-land/hosts/NAME`: each worker's heartbeat (phase 2). It records the owners the host can merge for, whether it can judge, its cores, its load, its glaeda version and when it last wrote.

Each value is one parentless commit whose message is the JSON document, so the old values are garbage-collected. Locally the swap is `git update-ref REF NEW OLD`. Against the remote it is `git push --force-with-lease=REF:OLD`, where an empty OLD means the ref must not exist yet.

### Leases and fencing

A lease is `{key, holder, gen, acquired, expires, state: held|released, outcome, head}`.

- **Claim.** A host may claim when the lease is absent, released or expired. The claim writes `gen + 1`, expiring in `LAND_LEASE_TTL` (600 s). When two hosts claim from the same old value, exactly one push wins.
- **Renew.** The holder extends `expires` and keeps `gen`. Renewal is refused once the ref holds any other value.
- **Release.** The holder writes `state: released` with an outcome (merged, stopped, cancelled), so the next claim can take it at once. A host never writes over another holder's lease.
- **Fence.** This runs before every merge and, from phase 1, before every update-branch. The fence re-reads the ref and passes only if it is the exact commit this host wrote, with the same holder and the same `gen`, and at least `LAND_LEASE_MERGE_ROOM` (120 s, at least the merge deadline) left. Otherwise no merge is sent. The merge stays pinned with `--match-head-commit`. A holder whose lease expires mid-merge can at worst send the same pinned merge twice, and GitHub refuses the second as already merged. The generation is the fencing token. A stale holder (a host that was asleep or partitioned) sees a newer `gen` and backs off.
- **Lease held elsewhere.** The entry waits (`retry`) until that lease could have expired, then claims. When the holder has merged the PR, the next judgment sees it merged and the entry ends.
- **Store unreachable.** The entry waits (`retry`, `LAND_FORWARD_RETRY`) and sends no merge. This fails closed for merges and open for everything else: judging continues, and the store's outage costs time, not a wrong merge.

### How a host claims work (phase 2)

Each tick, a host makes one `git ls-remote` of `refs/glaeda-land/*`. That is one git call for every entry and lease oid, and spends no REST quota. A PR is due when its entry is open, its wake time has passed and its lease is not live.

The host ranks the due keys by rendezvous hashing over the live hosts, using heartbeats younger than 2 × TTL. It claims its own top-ranked keys first. It takes another host's key only after that key has been due and unclaimed for 60 s (work stealing), so hosts do not all race for the same key and a dead host's share is picked up within a minute after its leases expire.

A host claims only up to its own free workers, using its existing pool ceiling and load gate (`land_worker_ceiling` and the load gate are unchanged by this ADR). It claims only PRs whose owner it can merge for, as its heartbeat says.

### Judgments run where the lease lands

`land-judge explain` already runs wherever `LandQueue` runs. It is resolved from `land.json` `landJudge`, then the ws-sync deploy, then the leo-workspace checkout (`LAND_JUDGE_PATHS`). A host without land-judge refuses to start its land worker (`land/refused.json`).

A holder judges with its own cache and its own gh-quota shim. A lane VM reads through its own read token (`upstream.json` `readToken`, `ReadTokenPool`) instead of the broker. The judgment's inputs are GitHub's state at the pinned head, so the answer does not depend on which host asks. `land_judge` judges the merge again inside the shim on the holder.

### Quota

Quota follows the identity, not the host. Every `lander@OWNER` installation pool (the lander App, per owner) is shared by every host that mints tokens for that App, and GitHub's `x-ratelimit-remaining` for an installation is the same on every host.

So:

- Each host gates on the remaining quota GitHub last reported to it. The existing latch marks and the shim already do this per host. In phase 2 each host also writes its last reading into its heartbeat, and every host takes the lowest fresh reading for a pool.
- Lands use the lander installation of the PR's owner. The login (Leo's account) stays a fallback only, never a lander's primary pool, so adding hosts never draws more on his login.
- One installation is 5,000 REST calls an hour at its base rate. At the measured ~15 calls per merge, and with most of those calls free conditional reads, one owner's pool covers more than 300 merges an hour before quota binds.

### Throughput with N hosts

The measured rate is 29 merges an hour with about 1.5 busy workers, roughly 19 merges an hour per busy worker. Rendezvous hashing spreads the work across hosts. A host contributes as many busy workers as its own gates allow, which is 2 on a loaded host and more on an idle one.

| Hosts (2 busy workers each) | Worker-bound merges/h | Bound |
|---|---|---|
| 1 (huge-blue today, 1.5 busy) | 29 (measured) | host load gate, login pace |
| 2 | ~75 | workers |
| 3 | ~115 | workers |
| 6 | ~230 | workers |
| 9+ | ~330 per owner | lander@OWNER at ~15 calls/merge |

These figures are worker throughput. Queue-to-merge also contains CI time, and adding hosts does not shorten CI. That is why phase 1 starts by measuring the wait in parts.

Losing a host costs at most one TTL (600 s) for the PRs it held. Its other due work moves to the next-ranked hosts at once.

## Migration

**Phase 0 (this ADR's PR).**

- `LandLeases` adds claim, renew, release and fence on git refs, with the swap described above.
- `do_merge` fences when `land.json` has `"leases": {"remote": URL, "cache": PATH, "holder": NAME}`.
- Leases are off by default, so today's executor and forwarding behave exactly as they do now.

**Phase 1.** Measure, then make two queues safe together.

- Log every land's queue-to-merge split into due-to-claimed (waiting for a worker), claimed-to-step (judging), and waiting on CI, review or other checks. These parts go in `land-pool` and in `land status`.
- Turn leases on, against the shared remote, on huge-blue and cosmic-gray.
- Fence update-branch as well as merge.
- Claim before judging too, so that two queues holding the same PR (one left over from a hand-off, for example) never judge it twice.

**Phase 2.** Entries go into the store, and the executor becomes optional.

- `glaeda-gh land` writes the entry ref directly. Lane VMs do this with a state-repo token: an App installation with contents:write on `glaeda-land-state` only, minted the same way as `readToken`.
- Workers claim as described in "How a host claims work".
- A host with leases that cannot reach its executor for more than 2 minutes runs its outbox itself instead of holding it. The leases make that safe.
- `land serve` on huge-blue keeps answering, as one worker among N.

**Phase 3.** Cut over.

- A one-shot import moves huge-blue's open entries into entry refs. It is idempotent by `requestId`.
- Remove `executor` from `land.json` on cosmic-gray and on the lane VMs.
- Done when: stopping huge-blue's glaeda daemon for 30 minutes under normal load stops no land (merges continue on the other hosts), and due-to-claimed stays under 2 minutes at p90.

**Phase 4.** Delete the executor, forwarding, outbox and hand-off code. Remove the lane-gh broker's `owner_land` merge path, because lane VMs now merge with their own owner's lander token.

## Consequences

- A merge needs GitHub plus the state repository. Both are GitHub, so there is no new failure domain.
- Each merge costs about 3 extra git round trips: claim, fence and release, about 1-2 s on a lane VM.
- A lease store outage delays merges and never merges wrongly.
- No host is special. huge-blue becomes one worker, and adding a host adds throughput until the per-owner App pool binds.
- Lease and entry documents hold PR keys, host names and requester names only, never tokens.
