# Runner-log handoff validation

`glaeda-runner-log-handoff` gives an operator one bounded receipt for the native
runner-log plan and its durable store recovery state:

```text
scripts/glaeda-runner-log-handoff \
  --runner-root /path/to/actions-runner \
  --enrollment-receipt /path/to/enrollment.json \
  --state-root /path/to/state \
  --output human
```

The adapter invokes `glaeda-runner-log-reclaim` in plan mode and refuses a
non-plan observer result. It then reads `journal.jsonl` under the explicit state
root, checks the bounded row shape and the enrolled runner identity, and reports
whether recovery is clean, pending, or blocked by a prepared transaction. It
does not pass `--apply`, open the store for recovery, write the journal, stage a
file, delete a file, or change the runner's warm state. A missing state root is
reported as a clean handoff; an unsafe or malformed existing state root fails
closed.

The JSON receipt is intentionally compact. It contains the observer completion,
bounded planned action identifiers, and candidate count plus journal row,
foreign-row, pending-count, and recovery disposition fields. Planned paths are
relative to the enrolled runner; private absolute paths are not copied into the
handoff summary.

Run the focused contract tests with:

```text
python3 scripts/test-glaeda-runner-log-handoff.py
```
