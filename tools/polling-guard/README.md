# polling-guard

Fails CI on a polling loop checked into a script or a workflow (`until ...; do sleep`, `while true`, `gh pr checks --watch`, a loop that waits on a file). Waits go through one `glaeda-gh wait run|pr|comment|file|pid` instead.

`block_polling.py` and `polling_scan.py` are copies of manaflow-ai/cmuxterm-hq `tools/agent-guards/`, where the same judge also runs as the agents' `block-gh-polling` hook. Change them there and copy them here. A loop that has to stay needs an entry with a reason in `.github/polling-allowlist.json`.

    python3 tools/polling-guard/polling_scan.py --root .
