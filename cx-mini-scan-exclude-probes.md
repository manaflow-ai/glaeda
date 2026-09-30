# cx-mini-scan-exclude probes

These probes were run read-only on macOS 26.5/26.5.1 PR minis unless a command is explicitly
marked as a canary mutation. They must be run only with the mini drained and `lsof` clear for
any root being moved. Never move `/private/tmp/cmux-ci*`, runner `_work`, CAS, or shared cache
while a job is using it.

## Recompute the host-record evidence

```sh
python3 - <<'PY'
import datetime, glob, json, collections
lo = datetime.datetime(2026, 9, 25, tzinfo=datetime.timezone.utc).timestamp()
hi = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc).timestamp()
totals = collections.Counter()
by_job = collections.defaultdict(collections.Counter)
for path in glob.glob('/Users/leoli/Projects/.tmp-ci-regime/mini/*.jsonl'):
    with open(path) as stream:
        for line in stream:
            row = json.loads(line)
            if 'top_outside' not in row or not (lo <= row.get('at', 0) < hi):
                continue
            for item in row['top_outside']:
                totals[item['process']] += item.get('core_seconds', 0)
                by_job[row.get('job', '?')][item['process']] += item.get('core_seconds', 0)
for name in ('fseventsd (root)', 'XprotectService (root)',
             'mediaanalysisd (cmux)', 'com.apple.Ambien (root)'):
    print(name, totals[name], totals[name] / 3600)
for job in ('app-host-unit-tests', 'tests-build-and-lag'):
    print(job, {k: v / 3600 for k, v in by_job[job].items()
                if k in ('fseventsd (root)', 'XprotectService (root)',
                         'mediaanalysisd (cmux)', 'com.apple.Ambien (root)')})
PY
```

The exact retained-record totals are 309.733 h fseventsd, 128.565 h XProtect, 72.693 h
mediaanalysisd, and 57.774 h for the truncated `com.apple.Ambien` label. The app-host rows are
68.156 h fseventsd and 32.802 h XProtect; tests-build-and-lag has 22.405 h Ambient. A
run-id-only nonzero-first grouping gives the approximate 66.703 h XProtect headline but merges
different jobs; it is not the canonical accounting key. Keep `(run_id, job, host, instance,
run_attempt)` when comparing attempts.

## Path and executable attribution

On a drained or explicitly observed PR mini, Leo can run these bounded root probes. Redact all
unrelated paths before publishing:

```sh
sudo /usr/bin/fs_usage -ww -f filesystem -t 30 2>/dev/null \
  | egrep 'fseventsd\\.|XProtect|Xprotect|syspolicyd|amfid' \
  | egrep 'cmux-build|DerivedData|cmux-ci|runner|_work|CAS|private/tmp|Users/Shared|Library/Developer' \
  | head -500
sudo sample "$(pgrep -x fseventsd)" 5 1
sudo sample "$(pgrep -x XprotectService)" 5 1
```

Observed fseventsd paths include runner `_work/_temp` XCResult files, DerivedData
`TestResults/metadata.db*`, `/private/tmp/cmux-ah-*`, and
`/Users/Shared/cmux-build-fleet/capacity`. XProtectRemediatorColdSnap observed temp files
under `/private/var/folders/.../T/` (test SQLite WAL/SHM, hooks, startup scripts, bundles),
while unified logs attribute Developer Tools checks to `Runner.Listener` and
`SWBBuildService`. Sampled built products had no quarantine/provenance xattrs; unsigned,
not-yet-allowed executables still triggered an XProtect scan. Do not disable XProtect,
Gatekeeper, SIP, or security updates.

## APFS experiment (Leo-only, not run)

Current 26.5.1 minis have one Data APFS volume, indexing already disabled, and about 80--85 GiB
free. Do not create this volume on a busy host. After drain and `lsof` verification, run exactly
one canary and record the new volume identifier:

```sh
 diskutil apfs addVolume disk3 APFS CI-BUILD-CANARY -reserve 40g -quota 60g -mountpoint /Volumes/CI-BUILD-CANARY
sudo mkdir -p /Volumes/CI-BUILD-CANARY/.fseventsd /Volumes/CI-BUILD-CANARY/ci /Volumes/CI-BUILD-CANARY/cas
sudo touch /Volumes/CI-BUILD-CANARY/.fseventsd/no_log
sudo mdutil -i off -d /Volumes/CI-BUILD-CANARY
mount | grep CI-BUILD-CANARY
mdutil -s /Volumes/CI-BUILD-CANARY
ls -l /Volumes/CI-BUILD-CANARY/.fseventsd/no_log
```

The current OS contains the `fseventsd` `no_log` implementation, but activation is checked at
mount time. Unmount/remount before measuring. Point one drained canary runner's build/CAS roots
at the volume, then compare one hour of identical real jobs against the baseline. Roll back only
the exact new APFS volume after a second `lsof` check:

```sh
sudo diskutil unmount /Volumes/CI-BUILD-CANARY
sudo diskutil apfs deleteVolume <exact-new-disk-identifier>
```

Do not put `.fseventsd/no_log` on the Data volume: that would suppress host-wide FSEvents.

## Scoped media canary and rollback

The canary on `cmux14` disabled only the CI user's two LaunchAgents and stopped their current
processes; `launchctl bootout` is SIP-blocked but the disabled state persists:

```sh
uid=$(id -u)
launchctl disable gui/$uid/com.apple.mediaanalysisd
launchctl disable gui/$uid/com.apple.photoanalysisd
for p in $(pgrep -x mediaanalysisd 2>/dev/null) $(pgrep -x photoanalysisd 2>/dev/null); do kill -TERM "$p"; done
```

Re-enable only after comparing the one-hour sampler and confirming the account is a dedicated CI
user:

```sh
uid=$(id -u)
launchctl enable gui/$uid/com.apple.mediaanalysisd
launchctl enable gui/$uid/com.apple.photoanalysisd
launchctl kickstart -k gui/$uid/com.apple.mediaanalysisd
launchctl kickstart -k gui/$uid/com.apple.photoanalysisd
```

AmbientDisplayAgent is a root system XPC service, so there is no equivalent per-user disable.
