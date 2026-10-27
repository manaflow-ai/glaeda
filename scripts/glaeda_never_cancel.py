"""The one place glaeda cancels a GitHub Actions run, and the rule it checks first.

Rulebook: "Never cancel a nightly, dev or promote build or its Release compile once it has started, for classic
and NEXT alike, unless it is stuck past its timeout or certain to fail." Leo, 2026-10-24: "it is unacceptable to
have nightlies >30min out of date. shouldn't be cancelling unless existing run would've literally been busted".

may_cancel() is the predicate: a delivery run (nightly, nightly-next, DEV or promote, on any branch, see
delivery_reason()) that has started is never cancelled, unless one of its jobs has worked longer than the longest
job-level timeout-minutes in its workflow at the run's own head_sha (GitHub ends a job at its own timeout, so such
a run is stuck). A job's wait for a runner, or for a runner hook's admission inside "Set up runner", never counts:
the clock starts at the job's first step after its setup, and a job still in setup is never past its timeout. When the workflow or the jobs cannot be read, the cancel is refused (fail closed).

cancel_run() is the only code in glaeda that POSTs a run's /cancel or /force-cancel. test-glaeda-never-cancel.py
scans the repository for any other cancel call, so a new path that skips the predicate fails CI.

The delivery list follows manaflow-ai/cmux scripts/ci/delivery_workflows.py (its globs and named DEV runs), plus
release.yml and cmux-next.yml push runs, which compile and promote the DEV and nightly-next feeds.
"""
from __future__ import annotations

import base64
import re
import time
import urllib.parse
from datetime import datetime
from fnmatch import fnmatch
from typing import Any, Callable, Mapping, Sequence

DELIVERY_GLOBS = (
    "nightly*.yml",          # nightly.yml, nightly-sign.yml, nightly-next-*.yml
    "*-nightly*.yml",        # cmux-tui-nightly.yml, ios-internal-nightly*.yml, resume-nightly-notarization.yml
    "cmux-next-dev-*.yml",   # cmux-next-dev-daily.yml (DEV build, sign and publish), its dispatcher and kick
    "*promote*.yml",         # cloud-vm-image-promote.yml and any other promote workflow
    "release.yml",           # classic release builds
)
# Delivery runs of a shared workflow, told apart by run name or event.
DELIVERY_RUN_NAME_PREFIXES: dict[str, tuple[str, ...]] = {"cmux-next.yml": ("cmux NEXT DEV daily · ",)}
DELIVERY_PUSH_WORKFLOWS = frozenset({"cmux-next.yml"})
DEFAULT_TIMEOUT_MIN = 360  # GitHub's job timeout when a job names none
NOT_STARTED = frozenset({"queued", "waiting", "requested", "pending"})


def workflow_file(path: Any) -> str:
    """The workflow's file name from a run's `path` (`.github/workflows/x.yml`, maybe with `@ref`)."""
    return str(path or "").split("@", 1)[0].rsplit("/", 1)[-1].lower()


def delivery_reason(run: Mapping[str, Any]) -> str:
    """Why this run is a nightly, DEV or promote build, or '' when it is not one."""
    name = workflow_file(run.get("path"))
    if name and any(fnmatch(name, pattern) for pattern in DELIVERY_GLOBS):
        return f"a {name} run"
    if name in DELIVERY_PUSH_WORKFLOWS and run.get("event") == "push":
        return f"a {name} push run"
    titles = (run.get("name"), run.get("display_title"))
    if any(str(t or "").startswith(p) for t in titles for p in DELIVERY_RUN_NAME_PREFIXES.get(name, ())):
        return f"a named DEV run of {name}"
    return ""


def started(run: Mapping[str, Any], jobs: Sequence[Mapping[str, Any]] | None) -> bool:
    """Whether the run has started: GitHub says in_progress or completed, or any job of it has a runner, a start
    time or a status past queued. Any one sign counts, so a run is called unstarted only when none shows."""
    if run.get("status") not in NOT_STARTED:
        return True
    return any(j.get("runner_name") or j.get("started_at") or j.get("status") in ("in_progress", "completed")
               for j in jobs or ())


# A runner's own setup, where a runner hook's admission waits for capacity: never part of the job's run time.
SETUP_STEPS = frozenset({"Set up job", "Set up runner"})


def work_started_at(job: Mapping[str, Any]) -> float | None:
    """When the job's own work began: the start of its first step after the runner's setup. None while it is still
    in setup (an admission wait), or when its steps are not listed: then no time of it counts toward a timeout."""
    steps = job.get("steps")
    for step in steps if isinstance(steps, list) else ():
        if not isinstance(step, Mapping) or step.get("name") in SETUP_STEPS or step.get("conclusion") == "skipped":
            continue
        began = parse_utc(step.get("started_at"))
        if began is not None:
            return began
    return None


def parse_utc(text: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp() if text else None
    except ValueError:
        return None


def job_timeout_minutes(text: str | None) -> int | None:
    """The longest job-level timeout-minutes in a workflow file (360 for a job with none); None when the file is
    missing or its `jobs:` map cannot be read, so no deadline is trusted."""
    if not text:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    start = next((i for i, ln in enumerate(lines) if re.fullmatch(r"jobs:\s*(#.*)?", ln)), None)
    if start is None:
        return None
    body: list[str] = []
    for ln in lines[start + 1:]:
        if not ln.startswith(" "):
            break
        body.append(ln)
    if not body:
        return None

    def indent(ln: str) -> int:
        return len(ln) - len(ln.lstrip(" "))

    job_indent = indent(body[0])
    jobs: list[list[str]] = []
    for ln in body:
        if indent(ln) == job_indent:
            if not re.match(r"\s*['\"]?[A-Za-z_][\w-]*['\"]?\s*:\s*(#.*)?$", ln):
                return None
            jobs.append([])
        elif indent(ln) < job_indent:
            return None
        else:
            jobs[-1].append(ln)
    longest = 0
    for job in jobs:
        child = min((indent(ln) for ln in job), default=0)
        value = next((m.group(1) for ln in job if indent(ln) == child
                      and (m := re.match(r"\s*timeout-minutes:\s*['\"]?(\d+)['\"]?\s*(#.*)?$", ln))), None)
        longest = max(longest, int(value) if value else DEFAULT_TIMEOUT_MIN)
    return longest or None


def may_cancel(run: Mapping[str, Any], jobs: Sequence[Mapping[str, Any]] | None, timeout_minutes: int | None,
               now: float) -> tuple[bool, str]:
    """(allowed, why) for cancelling this run now.

    jobs: the run's jobs as GitHub lists them, None when they could not be read. timeout_minutes: the longest
    job timeout of the run's workflow at its head_sha (job_timeout_minutes), None when unreadable.
    """
    what = delivery_reason(run)
    if not what:
        return True, "not a nightly, DEV or promote run"
    if run.get("status") == "completed":
        return False, f"{what} that has finished: nothing to cancel"
    if jobs is None:
        return False, f"{what} whose jobs could not be read: a started one is never cancelled"
    if not started(run, jobs):
        return True, f"{what} that has not started"
    if not timeout_minutes:
        return False, f"{what} that has started, and its workflow's job timeouts could not be read"
    for job in jobs:
        if job.get("status") != "in_progress":
            continue
        began = work_started_at(job)  # never job.started_at: that counts the runner setup's admission wait
        if began is not None and now - began > timeout_minutes * 60:
            return True, (f"{what} stuck past its timeout: job {job.get('name')!r} has worked "
                          f"{(now - began) / 60:.0f} min after its setup, past the longest job timeout of "
                          f"{timeout_minutes} min")
    return False, f"{what} that has started and no job of it has run past its timeout ({timeout_minutes} min)"


def read_jobs(get: Callable[[str], Any], repo: str, run: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """The jobs of the run's current attempt (up to 100), or None when they cannot be read."""
    try:
        body = get(f"/repos/{repo}/actions/runs/{run.get('id')}/attempts/{int(run.get('run_attempt') or 1)}"
                   "/jobs?per_page=100")
        jobs = body.get("jobs") if isinstance(body, dict) else None
        if not isinstance(jobs, list) or not all(isinstance(j, Mapping) for j in jobs):
            return None  # a malformed answer is an unreadable one
        return list(jobs)
    except Exception:  # noqa: BLE001 - any failure to read refuses the cancel
        return None


def read_workflow_timeout(get: Callable[[str], Any], repo: str, run: Mapping[str, Any]) -> int | None:
    """job_timeout_minutes of the run's workflow at the run's own head_sha, or None."""
    path, sha = str(run.get("path") or "").split("@", 1)[0], str(run.get("head_sha") or "")
    if not path or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return None
    try:
        body = get(f"/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={sha}")
        text = base64.b64decode(body["content"]).decode() if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001
        return None
    return job_timeout_minutes(text)


def cancel_run(get: Callable[[str], Any], post: Callable[[str], Any], repo: str, run: Mapping[str, Any], *,
               force: bool = False, now: Callable[[], float] = time.time) -> tuple[bool, str]:
    """Cancel (or force-cancel) a run only when may_cancel allows it. Returns (cancelled, why).

    get and post take an API path and raise on failure. The run is the API's run object, read just before.
    A run that is no delivery run costs no extra call; a started delivery run costs a jobs read and a
    workflow-file read.
    """
    run_id = run.get("id")
    jobs: list[dict[str, Any]] | None = []
    timeout: int | None = None
    if delivery_reason(run) and run.get("status") != "completed":
        jobs = read_jobs(get, repo, run)
        if jobs is not None and started(run, jobs):
            timeout = read_workflow_timeout(get, repo, run)
    ok, why = may_cancel(run, jobs, timeout, now())
    if ok:
        post(f"/repos/{repo}/actions/runs/{run_id}/{'force-cancel' if force else 'cancel'}")
    return ok, why
