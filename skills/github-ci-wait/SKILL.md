---
name: github-ci-wait
description: "Wait for GitHub CI, watch a workflow run, poll PR checks, wait for a reply comment (a bot's receipt), or answer 'is CI green' without burning the shared GitHub quota. Use glaeda-gh (a local daemon that is the only poller) instead of gh run watch, gh run view loops, sleep-and-read loops, or per-PR REST scans. Use whenever you would wait for CI, watch the run, poll checks, wait for a comment, check whether CI is green or a PR merged, or sweep many PRs."
---

# Wait on GitHub with glaeda-gh

Every session on this machine shares one GitHub account, so one REST quota of 5000 requests an
hour. Sessions polling on their own used it all up. `glaeda-gh` is a local daemon that is the
only process polling: one batched GraphQL query for every watched PR, ETag requests for runs
(an unchanged run costs nothing), and one batched query every 15 s for comment waits. Its
commands read the daemon's cache and make no API calls.
On the tailnet the daemon also follows the cmux build controller, which receives GitHub's run,
check and comment webhooks (cmuxterm-hq#675). A finished run, a PR check change or a bot's
reply then reaches `wait` within a few seconds at no quota cost. Public repos need no token;
private ones (cmuxterm-hq) need `~/.config/cmux/build-fleet/controller.token`. Without the
controller everything still works by polling, only slower; `glaeda-gh budget` says which.

## Wait and check

```bash
glaeda-gh wait pr OWNER/REPO#N --sha "$(git rev-parse HEAD)"   # until green; 1 at the first failed check
glaeda-gh wait pr OWNER/REPO#N --until done      # until every check finishes (0 green, 1 red, 4 conflict)
glaeda-gh wait pr OWNER/REPO#N --until required  # only the checks GitHub requires decide (opt-in)
glaeda-gh wait pr OWNER/REPO#N --until merged    # ignores conflicts; 1 if closed without merging
# --until (and every other choice) takes a synonym or one typo of a choice (checks-done, passed, merge) and says on stderr which it ran
glaeda-gh wait run OWNER/REPO/RUN_ID [--jobs]    # 0 success, 1 failure
glaeda-gh wait comment OWNER/REPO#N --author 'github-actions[bot]' --match 'REGEX' --since COMMENT_URL
glaeda-gh status pr OWNER/REPO#N                 # one look: state, mergeable, checks, review
glaeda-gh status pr OWNER/REPO#12 13 14 o/x#7    # several PRs, one gather (bare N reuses the repo before it)
glaeda-gh status pr OWNER/REPO#12 13 --field head,checks.state,checks.failed   # just those, tab-separated
glaeda-gh status run OWNER/REPO/RUN_ID --jobs
glaeda-gh status comment OWNER/REPO#N            # the latest comments
glaeda-gh budget                                 # REST and GraphQL left, the daemon's own use, its heartbeat
glaeda-gh runners OWNER[/REPO] [--label L] [--json]  # online, busy and idle self-hosted runners per label
```

- `wait` exits 0 on success (or a matching comment), 1 on failure (including a PR closed without
  being merged), 2 on timeout (`--timeout S`, default 3600), 3 when the daemon is down, 4 when the
  PR conflicts with its base, 6 when no CI ever reported on the head, 7 (inconclusive) when `--until done`
  is left with only checks queued with no runner past 30 min or the deadline passes on a partial read,
  64 on bad arguments. Done and green never exit 0 while any check is queued, running or pending.
- Every non-zero exit of `status` and `wait pr|run` ends with a line saying what is wrong and a last line
  `next: <command>` (or `next: none (why)`), and with `--json` its one object carries `ok`, `code`, `reason`
  and `next` (codes: `not-found`, `timeout`, `checks-failed`, `run-failed`, `pr-closed`, `conflict`,
  `nothing-reported`, `inconclusive`, `no-data-yet`, `usage`, `refused`, `daemon-down`). A PR or run GitHub does not know is
  `not-found` (exit 1) on the daemon's first read of it, not a wait to the timeout. `status` exits 3 when
  the daemon has not read a target yet. Deadlines are UTC with a Z (`17:55:00Z`).
- While it blocks, a `wait pr|run` prints `glaeda-gh: still waiting on KEY: 240s of 1800s, deadline
  17:55:00Z; <state>` every 120 s on stderr, the stream of its waiting line; a timeout's last lines say
  `waited N s for ... (deadline ...Z); last seen: ...` and `next:` the same wait again.
- Run one background `wait` per PR, run or comment (Bash `run_in_background`), not one wait
  looping over several: each then notifies you on its own when it exits, and you carry on meanwhile.
- Every `wait` prints one line on **stderr** before it blocks, naming what it is waiting for, its
  deadline as a wall-clock time, and, with `--sha`, the commit it is pinned to. Read it one way
  only: the line in your capture says that wait reached the point of blocking. Its absence says
  nothing — the wait may have been reaped, or stderr may have been a stream the line was dropped
  on rather than becoming the wait's own failure (a closed fd, a pipe with no reader, a full pipe).
  To tell a live wait from a gone one, look for the process: `pgrep -af "glaeda-gh wait"`.
  A `wait` launched from a session's foreground shell is reaped with that shell, silently; a bare
  `&` from inside a tool call is what gets reaped — use the harness's background mode, or
  `setsid nohup … </dev/null &`.
  **Capture stderr or the line tells you nothing.** With `wait … > file` it goes to the terminal
  and the file is empty while blocked. Keep the streams apart if you parse the result:
  `… --json > wait.json 2> wait.err`, and read `wait.err` for the line. `2>&1` works too, but then
  the capture is no longer one JSON document. The daemon-down, failing-daemon and hung-daemon
  notices go to the same place, so `2>&-` loses them too — and loses nothing else: they never
  appear in `--json` stdout.
- Exit 1 is a red, not a merge verdict. Where the repo's merge bar excuses base reds (cmux
  `feat-cmux-next`), compare the failed check with the base before holding the PR.
- The summary names what failed and the failed step, e.g.
  `failed: CI / build (step: Run selected tests)`, so you rarely need a REST call to see why.
  Every check decides, required or not: a completed failure (`(not required)` ones too) exits 1,
  neutral and skipped pass, and a pending check is never a success. A merged PR went red when
  only required checks decided. `--until required` keeps that reading on request, for example
  when an optional check never finishes. The headline (`checks SUCCESS: ...`) is the verdict
  `wait` used; when GitHub's own rollup differs, the line says so. Attempts a newer attempt or
  run replaced are listed as `superseded` and not counted.
- A check reads as its last real attempt left it. An attempt that ended in `startup_failure` ran
  no jobs, so it is no result and never green: when a check suite's newest attempt ran nothing
  but an older one ran something, the older attempt's check runs decide (glaeda#1842). GitHub's
  own rollup shows only the newest attempt, so a red can vanish there after a rerun that failed
  to start; `wait` and `status` keep it.
- `wait` only rules on data fetched after it started. For a run wait on a configured controller, the
  client makes one bounded long-poll request to the existing run endpoint (up to 300 seconds), then
  uses the normal cache verdict path; unknown or unavailable controllers fall back to the daemon's
  ordinary wait cadence. This is a one-shot request, not a client-side polling loop.
- Add `--json` for machine-readable output. URLs work too: `glaeda-gh wait pr https://github.com/o/r/pull/12`.
- Looking at several PRs: one `status pr` with all of them, never a `for n in ...; do glaeda-gh status pr`
  loop. The daemon reads them together (one batched GraphQL query per 15 PRs when PRs read over GraphQL);
  a loop lands each in its own tick. `--json` then prints a list (one target still prints one object).
  `--field a,b.c` prints just those values of the `--json` object, tab-separated, one line per PR
  (prefixed `OWNER/REPO#N` when there are several): `head`, `state`, `mergeable`, `url`, and
  `checks.state|failed|pending|passed|held|total`; lists print comma-joined names, instead of piping
  `--json` into python. A quoted `'#N'` works like a bare `N` (unquoted, the shell reads `#` as a comment).
- A target takes whichever form you have, including **the one this tool prints**: the `pr:o/r#5` and
  `run:o/r/123` keys in its own waiting and still-waiting lines are targets you can hand straight back
  (`wait pr:o/r#5`, with or without the kind word before them), as are `o/r#5`, `o/r/5`, a GitHub URL
  and a bare `N` in the checkout you are standing in. The kind is never guessed across kinds: a prefix
  naming another kind (`wait pr run:o/r/123`) refuses and names the spellings that kind does take.

## Waiting on a file or a process: no sleep loops

```bash
glaeda-gh wait file PATH --timeout 600           # exists and not empty ([ -s PATH ]); PATH may be a glob
glaeda-gh wait file PATH --exists | --gone | --contains 'REGEX'
glaeda-gh wait until quota --timeout 4000       # until this host's GitHub quota reset; or 14:40Z, an ISO time, 45m
glaeda-gh wait pid 12345                         # until the process exits
```

Exit 0 when ready, 2 on timeout. Run it with `run_in_background`. The polling hook refuses
`until [ -s FILE ]; do sleep N; done`-style loops (on 2026-10-20 Huge Blue had nine of them keeping
sessions alive); these commands replace them.

## Reading many heads' checks: glaeda-gh checks

```bash
glaeda-gh checks OWNER/REPO SHA [SHA ...] --consumer NAME --json
```

The daemon reads each commit's checks once for every session and keeps them (a finished head 10
minutes, a running one 90 s), so ten sessions asking about one head cost one read. `--consumer` is
who you are (`autoland` for next-autoland, otherwise your session's name): each consumer has an
hourly budget of head reads. `autoland` is reserved, so its reads are served whatever the others
use; other consumers share a pool and, once it is spent or the quota is lean, get the cached answer
(`stale: true`) or `unread`. Exit 0 all fresh, 5 some stale or unread. `glaeda-gh budget` shows each
consumer's spend. Prefer it over `gh pr checks` or `gh api .../check-runs` for more than a look.

## After a push: --sha

Pass the commit you pushed: `--sha "$(git rev-parse HEAD)"` (`--head` is the same flag). Checks of an older head then count
as pending until GitHub shows yours. Give the full 40-hex id. A shorter prefix is resolved in the
current checkout; one that does not resolve must match the head GitHub shows, or `wait` exits 64
at once rather than waiting for a commit that will never appear. For a run, a `--sha` that is not
the run's commit is also an error: a run's commit never changes. A workflow_dispatch run's commit is
the dispatch ref's tip (main), not the commit it tested, so a dispatch that names the commit in its
run-name as `OWNER/REPO@<full sha>` (manaflow-ai/cmux fast-checks: `cmux-ci test` prints
`glaeda-gh wait run manaflow-ai/cmux/RUN --sha HEAD`) counts as that commit's. It must be the full
id and this run's repo. A prefix, another commit or another repo's commit is still an error.

## Merge conflicts: exit 4

A PR that conflicts with its base (mergeable `CONFLICTING`, merge state `DIRTY`) runs no
pull_request workflows until the next push. This happens to a stacked PR when the PR below it is
squash-merged and main moves under it. `wait pr --until green`, `done` and `required` stop with exit 4
as soon as the daemon sees it, printing `result: conflict` and `note: PR conflicts with its base`.
Rebase or merge the base, push, and wait again with the new `--sha`. The check uses the daemon's
cached PR data, so it costs no extra API calls. `UNKNOWN` mergeability (GitHub computes it
lazily) counts as pending, not as a conflict. With `--sha`, a conflict on an older head is pending
too. `--until merged` ignores conflicts and keeps waiting.

## Runner refusals and rescue attempts

A glaeda runner can refuse a job it was handed (its host is busy): the job fails within seconds at
the `Set up runner` step, and the repository's rescue (cmux: the owned-pool rescue sweeper)
re-runs it, usually on Blacksmith. `wait` treats such a failure as pending for `--rescue-grace`
seconds (default 360) and then decides on the next attempt; the summary says
`held: ... refused ... at setup`. With no new attempt within the grace, the failure stands and
the note says so. Any other failure in the same attempt decides at once. A PR check cancelled
before any step ran (the rescue cancels a job still queued for a runner, then re-runs it) is held
the same way: `held: ... cancelled before any step ran`.
`--rescue-grace 0` rules on each attempt as it finishes.

## Waiting for a comment

For a bot's reply, such as the callsign receipt on teamleaderleo/stensibly#454:

```bash
url=$(glaeda-gh post comment teamleaderleo/stensibly#454 --body "/callsign reserve ..." --wait 300)
glaeda-gh wait comment teamleaderleo/stensibly#454 --author 'github-actions[bot]' \
  --match "${url##*#}\b" --since "$url" --timeout 300
```

- `--since` takes a comment URL or id (only later comments count), an ISO time, or a duration
  such as `10m`. Without it only comments posted after the wait began count, and a reply that came
  before you started waiting is missed. Pass your own comment's URL.
- `--author` accepts `github-actions[bot]` or `github-actions`. `--match` is a regular expression
  searched in the body.
- It prints the first matching comment (URL and body) and exits 0. A reply on a repo the
  controller receives webhooks for (teamleaderleo/stensibly among them) arrives within seconds;
  otherwise the daemon reads the latest 50 comments every 15 s while someone waits.

## Posting comments, PRs and reviews: glaeda-gh post

GitHub limits content creation account-wide ("was submitted too quickly"). When every session
posts and retries on its own, they keep tripping the limit for each other. Queue the write instead:

```bash
glaeda-gh post comment OWNER/REPO#N --body-file - <<'EOF'      # prints "queued as ID; ..." and returns
...
EOF
glaeda-gh post pr OWNER/REPO --head BRANCH --base main --title "..." --body-file body.md [--reviewer LOGIN]
glaeda-gh post review OWNER/REPO#N --event approve|comment|request-changes --body-file -
glaeda-gh post status ID [--wait 300]        # the URL once posted (0), failed (1), still queued (2)
```

The daemon sends one write every few seconds, oldest first. When GitHub says to slow down, it
waits out Retry-After for every queued write. The write stays queued after your session ends.
Add `--wait S` when you need the URL right away (a PR number to claim, a comment URL for
`wait comment --since`). Do not retry a `gh` write that failed with "submitted too quickly":
queue it with `post`.

## Running a command when a run or job finishes: on-event

To act once per event (a deploy, a promote) instead of on a timer, configure the daemon, not a
poller. `~/.config/glaeda/on-event.json` lists hooks; each matches a finished run the build
controller's feed delivers (no GitHub read) by repo, branch and workflow (file or display name).
With `job`, that job must have finished with `conclusion` (default success):

```json
{"hooks": [{"name": "cloud-probe-promote", "repo": "manaflow-ai/cmux", "workflow": "backend.yml",
            "branch": "feat-cmux-next", "job": "promote-production", "conclusion": "success",
            "command": ["/usr/bin/python3", "/abs/path/probe.py", "run", "--sha", "{head_sha}"]}]}
```

- Runs once per (hook, run, attempt), across restarts and crashes: the claim is on disk before
  the command starts. A rerun attempt is a new event.
- `command[0]` is absolute. Placeholders: `{head_sha}` (always a full 40-hex), `{run_id}`,
  `{attempt}`, `{repo}`, `{run_url}`, `{job_url}`, `{hook}`; the same as `GLAEDA_EVENT_*`
  variables. The environment is only HOME, PATH, USER, LOGNAME, LANG, LC_ALL, TMPDIR and SHELL,
  plus the hook's own literal `env` map: no token. Output goes to `~/.cache/glaeda/gh/on-event/`.
- Only the repositories the controller's webhooks cover (not manaflow-ai/cmux-platform), and only
  while the daemon runs and reads the controller itself (not through an upstream, as lane VMs do).
- `glaeda-gh on-event` shows the hooks, refused config and the last outcomes (`ran` with its exit
  code, `no-match`, `unresolved`). `glaeda-gh on-event replay --run-file F --jobs-file F --wait 60`
  feeds one run record through the same path to prove a hook.

## Landing a PR: glaeda-gh land

Say it once; the daemon lands it. No update-branch / wait / read-the-head / merge-again loops by hand.

```bash
glaeda-gh land OWNER/REPO#N                         # queue it; returns at once
glaeda-gh land OWNER/REPO#N --after OWNER/REPO#M    # only after #M merged (a chain: one land per PR)
glaeda-gh land OWNER/REPO#N --notify HOST/SESSION   # who hears of the merge or the stop
glaeda-gh land OWNER/REPO#N --wait 3600             # also wait: 0 merged, 1 stopped, 2 still going
glaeda-gh land status [OWNER/REPO#N] [--json]       # state, last step, refusals, history; steps per day
glaeda-gh land cancel OWNER/REPO#N
```

- The daemon asks `land-judge explain R N --sha HEAD --json` at the head its cache shows, and takes
  the step the answer's `action` names: the pinned squash merge, a pinned update-branch, `gh run rerun
  ID --failed`, a base change, `router-review R N --post` (once per head), or a wait on the PR's checks,
  a base run or a review comment. It never asks while the head's checks still run, and after an
  update-branch it judges nothing until the cache shows the new head.
- Every `gh` goes through the gh-quota shim, so land_judge judges every merge; without the shim it
  merges nothing. The calls carry `CMUX_AGENT_SESSION=glaeda-land` and `MAY_MERGE_PATH=glaeda-land`.
- It stops and tells the requester (`acpmux send --no-wait`, default this host/`$ACPMUX_SESSION_NAME`)
  only on a real stop: a BLOCK, a hold, a conflict, a step only a person can take, the PR or an
  `--after` PR closed, or 3 identical refusals in a row. A refusal naming an update-branch or a wait
  never counts toward that: the daemon sends the update pinned, once per head, and a wait waits to its
  bound (checks: 4 h with none moving, `checks_stuck`; a review, an unreadable retry or a cancelled
  job's rerun: 2 h, `<kind>_deadline`). On a stop, do the named step, then `land` again. A land
  `--after` a stopped PR keeps waiting for it (24 h): landing or merging that PR resumes it.
- A PR that reads MERGED ends its entry merged; CLOSED ends it "closed unmerged", naming no step. A
  step GitHub refused because the PR is closed, or a base change on a lane VM's stale copy, reads the
  PR's state first; an unreadable state waits, never counting toward a stop.
- Landing again updates `--after` and `--notify`; landing a stopped or cancelled PR starts it over.

## When the daemon is down or hung

`glaeda-gh` exits 3 when the daemon is down or hung. Run `glaeda-gh restart` once, then wait
again. Do not fall back to polling GitHub yourself, and do not run `launchctl kickstart`,
`bootout`/`bootstrap` or `systemctl` yourself: on 10-20 about ten sessions kickstarted it at once,
each kickstart killed the daemon the previous one had just started, and launchd throttled the
respawns ("spawn scheduled", "spawn failed") for most of an hour.

`glaeda-gh restart` is safe from many sessions at the same time. One of them restarts the daemon;
the others are told that a restart is under way or happened in the last 3 minutes, and should just
re-run their wait. It does nothing when the daemon is healthy, and refuses when every cycle fails
(a restart cannot fix a bug). It stops a hung lock holder, kickstarts the LaunchAgent (or restarts
the systemd unit), and reloads the LaunchAgent once if kickstart fails with `5: Input/output
error`. A `wait` that finds the daemon down keeps waiting up to 90 s for it to come back before it
exits 3, and a restarted daemon writes its heartbeat as soon as it holds its lock.

If it was never installed: `glaeda-mini-setup --hygiene-only --apply` from the glaeda checkout.

On a lane VM (a host whose `~/.config/glaeda/upstream.json` names an upstream), the daemon reads through the
broker host's cache. The broker host reads a key a wait is blocked on within about a minute, even while the
shared GraphQL quota sits under the fleet's reserve (down to half of it). A wait whose data is older than 120 s reads its
one target itself through `lane-gh call get` (or the file's `"direct"` program) and says `direct read answered`, without a `glaeda-gh restart` hint.
A restart there never helps: the data is the upstream's (glaeda#2029).

Never run `glaeda-gh serve` yourself, not even under `timeout`: it refuses while the service is
loaded, and a hand-started serve that took the lock froze every session's waits on 10-20.

If it prints `every cycle since ... has failed with ERROR at SITE`, the daemon runs but a bug makes
each cycle raise, so a restart will not help. Tell the user (the message names the code site), and
meanwhile use single `gh` reads, never a polling loop.

## Reading GitHub state: the broker, NOW, pr-overlap

Leo, 2026-10-29: "dont we already have apps that tell us about stuff and we can just read from that
instead of directly callign either hte rest or graphql apis?" Read what our own systems already hold,
never a raw API loop:

- `gh` on an agent host is a chain that ends in glaeda's broker (`glaeda-gh gh`): the gh-quota shim and
  the PATH guard keep their rules, then the broker answers. A `pr view N --json` of fields the daemon
  holds costs nothing, and every other read it keeps (`pr view`, `pr list`, `pr checks N`, `pr diff N`,
  `issue view N`, `issue list`, `run view`, `run list`, `search ...`, `gh api` GET and GraphQL queries)
  is one request for every session on the host within 60 s; a finished job's log is kept 10 minutes.
  Prefer `--json` (or `--jq`): data output is shared across terminals, a rendered table is not.
- Status: `glaeda-gh status pr|run`, `glaeda-gh wait ...` and `glaeda-gh checks` (above) read the daemon's
  cache; `now` (or `now json`) is the NOW page, cmuxterm-hq `reconciler-state/now`, for what is in flight
  fleet-wide; `pr-overlap` (or `pr-overlap N`) for which open PRs touch the same files; the fleet
  dashboard (`data.json` on cmux-lawrence:8444) for runners, the queue and alerts.
- Source: `git show REF:PATH` in a checkout, never `gh api repos/.../contents/...` or `git/blobs`: the
  broker refuses those when the repository is checked out on the host.
- `which gh` that is not that chain (a bare `/usr/bin/gh` or `/opt/homebrew/bin/gh` first) is a host
  enrollment bug: `reach enroll HOST` names it as `gh-chain`. Do not route around it with an absolute
  path to the real gh.

## Do not

- `gh run watch`, `gh pr checks --watch`, or `gh run view` / `gh pr view` / `gh api .../comments`
  in a loop.
- Per-PR REST scans: listing PRs, then `gh api repos/.../pulls/<n>/files` (or `/reviews`, `/commits`) for each.
- `gh run view --json jobs` just to learn which step failed: the `wait` summary already names it.
- Calling the real gh by absolute path, or `CMUX_GH_BROKER_DISABLE=1`, to get past the broker.

## Fine to do directly

- Single reads, merges, labels, and dispatches with `gh`. Comments, PRs and reviews: `glaeda-gh post`.
- Logs: ask a question instead of reading a whole log. `glaeda-gh log errors OWNER/REPO JOB_ID` prints one
  job's error-level lines by step. `glaeda-gh log search OWNER/REPO 'PATTERN' --since 24h` names the recent
  jobs that printed it. `glaeda-gh log flaky OWNER/REPO [TEST]` lists failing tests by name and marks the ones
  whose job passed on re-run. Both scans read the build controller's job history and kept logs, never REST.
  `glaeda-gh log job` prints the whole log when you need it.
- One-off reads across many PRs: a single batched GraphQL query with aliases
  (`p0: repository(owner:, name:) { pullRequest(number:) { ... } } p1: ...`) through `gh api graphql`.
  GraphQL has its own 5000-point quota, and one aliased query costs about 1 point.
- Before a sweep that needs many REST calls, run `glaeda-gh budget` first and keep well above 500 left.
