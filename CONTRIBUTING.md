# Contributing to glaeda

glaeda deploys pinned to the fleet Macs and runs as root there, so it stays its own repository. Its landing goes
through the same tools as cmuxterm-hq: `cmux-gh land`, land-judge and the rescue path.

## How to land a glaeda PR

1. Open the PR in `manaflow-ai/glaeda` (a branch of that repository, not a fork of it) or in
   `teamleaderleo/glaeda`. Verify runs on it; `Verify / ci-status` is the one check that counts.
2. A change to the root runner hook (`scripts/glaeda-cmux-runner-hook`) or anything else that reaches the signing
   keys needs an approving review at its head. That is the only review a teammate's PR waits on.
3. Land it: `cmux-gh land manaflow-ai/glaeda#N` (or `teamleaderleo/glaeda#N`) from any fleet host. Your agent can do
   this itself; no person is needed.
   - A `manaflow-ai/glaeda` PR cannot merge in the fork: its `main` takes only the sync's pushes ("Cannot update
     this protected ref"). `land` carries it: it pushes the same head to `carry/manaflow-ai-glaeda-N` in
     `teamleaderleo/glaeda`, opens one PR there naming yours, and lands that, pinned. The fork's Verify at that commit
     counts there too, and `sync-main.yml` brings the merge back to the fork. Close yours once the twin merges.
   - A shard that failed on a test unrelated to your change is rerun once by the land queue; a red that is also red
     on `main` is logged and does not hold you.
4. Check on it with `cmux-gh land status manaflow-ai/glaeda#N`, or wait with
   `cmux-gh wait pr teamleaderleo/glaeda#TWIN --until merged`. Never poll GitHub in a loop.

If a land stops, its status line names the reason and the next step. A flaky test is fixed or quarantined in its
own PR; the cmuxterm-hq handbook page "Landing a glaeda PR" has the details.
