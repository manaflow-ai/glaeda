#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
helper="${script_dir}/quarry-trusted-runner.sh"
tmp="$(mktemp -d)"
trap 'rm -rf -- "${tmp}"' EXIT HUP INT TERM

bin="${tmp}/bin"
state="${tmp}/state"
home="${tmp}/home"
mkdir -p "${bin}" "${state}" "${home}/Library/LaunchAgents"
log="${state}/commands.log"
configured="${state}/configured"
routing="${state}/routing"
running="${state}/running"
service_active="${state}/service-active"
scheduling_label="${state}/scheduling-label"
pilot_scheduling_label="${state}/pilot-scheduling-label"
busy_latched="${state}/busy-latched"
: > "${log}"
: > "${running}"
printf 'ubuntu-24.04\n' > "${routing}"

cat > "${bin}/uname" <<'STUB'
#!/usr/bin/env bash
printf 'Darwin\n'
STUB

cat > "${bin}/gh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf 'gh %s\n' "$*" >> "${TRUSTED_RUNNER_TEST_LOG}"
case "$*" in
  'api --method POST repos/Quarry-Labs/quarry/actions/runners/registration-token --jq .token')
    printf 'registration-secret\n'
    ;;
  'api --method POST repos/Quarry-Labs/quarry/actions/runners/remove-token --jq .token')
    printf 'removal-secret\n'
    ;;
  'api --method POST repos/Quarry-Labs/quarry/actions/runners/22/labels --input -')
    IFS= read -r payload
    [ "${payload}" = \
      '{"labels":["quarry-trusted-local","smolrunner-quarry-pilot"]}' ]
    : > "${TRUSTED_RUNNER_TEST_SCHEDULING_LABEL}"
    : > "${TRUSTED_RUNNER_TEST_PILOT_SCHEDULING_LABEL}"
    ;;
  'api --method DELETE repos/Quarry-Labs/quarry/actions/runners/22/labels/quarry-trusted-local')
    rm -f -- "${TRUSTED_RUNNER_TEST_SCHEDULING_LABEL}"
    if [ "${TRUSTED_RUNNER_TEST_PILOT_ASSIGN_AFTER_COMMON_DRAIN:-0}" = 1 ] \
      && [ -f "${TRUSTED_RUNNER_TEST_PILOT_SCHEDULING_LABEL}" ]; then
      : > "${TRUSTED_RUNNER_TEST_BUSY_LATCHED}"
    fi
    ;;
  'api --method DELETE repos/Quarry-Labs/quarry/actions/runners/22/labels/smolrunner-quarry-pilot')
    rm -f -- "${TRUSTED_RUNNER_TEST_PILOT_SCHEDULING_LABEL}"
    ;;
  *'actions/runners --jq '*'.id == 22'*'| @tsv'*)
    if [ "${TRUSTED_RUNNER_TEST_GITHUB_BINDING:-exact}" = exact ]; then
      runner_state=offline
      [ ! -f "${TRUSTED_RUNNER_TEST_SERVICE_ACTIVE}" ] || runner_state=online
      busy="${TRUSTED_RUNNER_TEST_BUSY:-false}"
      [ ! -f "${TRUSTED_RUNNER_TEST_BUSY_LATCHED}" ] || busy=true
      common_label=false
      pilot_label=false
      [ ! -f "${TRUSTED_RUNNER_TEST_SCHEDULING_LABEL}" ] || common_label=true
      [ ! -f "${TRUSTED_RUNNER_TEST_PILOT_SCHEDULING_LABEL}" ] || pilot_label=true
      printf '22\t%s\t%s\t%s\t%s\n' \
        "${runner_state}" "${busy}" "${common_label}" "${pilot_label}"
    fi
    ;;
  'variable set CI_LINUX_RUNNER --repo Quarry-Labs/quarry --body '*)
    printf '%s\n' "${*: -1}" > "${TRUSTED_RUNNER_TEST_ROUTING}"
    ;;
  'variable get CI_LINUX_RUNNER --repo Quarry-Labs/quarry')
    cat "${TRUSTED_RUNNER_TEST_ROUTING}"
    ;;
  *)
    printf 'unexpected gh invocation: %s\n' "$*" >&2
    exit 91
    ;;
esac
STUB

cat > "${bin}/limactl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf 'limactl %s\n' "$*" >> "${TRUSTED_RUNNER_TEST_LOG}"
case "${1:-}" in
  list)
    if [ "${2:-}" = --quiet ]; then
      if [ "${TRUSTED_RUNNER_TEST_INSTANCE_EXISTS:-1}" = 1 ]; then
        if [[ "$*" != *'--filter'* ]] || [ -f "${TRUSTED_RUNNER_TEST_RUNNING}" ]; then
          printf 'smolrunner\n'
        fi
      fi
    elif [ "${2:-}" = --format ]; then
      if [ -f "${TRUSTED_RUNNER_TEST_RUNNING}" ]; then
        printf 'Running\n'
      else
        printf 'Stopped\n'
      fi
    else
      exit 92
    fi
    ;;
  start)
    : > "${TRUSTED_RUNNER_TEST_RUNNING}"
    ;;
  stop)
    rm -f -- "${TRUSTED_RUNNER_TEST_RUNNING}"
    ;;
  autostart)
    if [ "${2:-}" = enable ]; then
      : > "${HOME}/Library/LaunchAgents/io.lima-vm.autostart.smolrunner.plist"
    elif [ "${2:-}" = disable ]; then
      [ "${TRUSTED_RUNNER_TEST_AUTOSTART_DISABLE_STATUS:-0}" = 0 ] \
        || exit "${TRUSTED_RUNNER_TEST_AUTOSTART_DISABLE_STATUS}"
      rm -f -- "${HOME}/Library/LaunchAgents/io.lima-vm.autostart.smolrunner.plist"
    else
      exit 95
    fi
    ;;
  shell)
    shift
    [ "${1:-}" = smolrunner ]
    shift
    [ "${1:-}" = -- ]
    shift
    command_line="$*"
    case "${command_line}" in
      '/usr/bin/test -f /home/lima/actions-runner/.runner')
        [ -f "${TRUSTED_RUNNER_TEST_CONFIGURED}" ]
        ;;
      '/usr/bin/jq -r [.agentId, .agentName] | @tsv /home/lima/actions-runner/.runner')
        printf '22\tquarry-trusted-mac-arm64\n'
        ;;
      *'RUNNER_VERSION=2.336.0'*)
        ;;
      *'RUNNER_REPOSITORY=Quarry-Labs/quarry'*)
        IFS= read -r token
        [ "${token}" = registration-secret ]
        : > "${TRUSTED_RUNNER_TEST_CONFIGURED}"
        : > "${TRUSTED_RUNNER_TEST_SCHEDULING_LABEL}"
        : > "${TRUSTED_RUNNER_TEST_PILOT_SCHEDULING_LABEL}"
        ;;
      *'config.sh remove --token'*)
        IFS= read -r token
        [ "${token}" = removal-secret ]
        rm -f -- "${TRUSTED_RUNNER_TEST_CONFIGURED}"
        ;;
      *'RUNNER_DIR=/home/lima/actions-runner'*)
        if [[ "${command_line}" == *'svc.sh start'* ]]; then
          : > "${TRUSTED_RUNNER_TEST_SERVICE_ACTIVE}"
        fi
        if [[ "${command_line}" == *'svc.sh stop'* ]]; then
          rm -f -- "${TRUSTED_RUNNER_TEST_SERVICE_ACTIVE}"
        fi
        ;;
      '/usr/bin/systemctl is-active actions.runner.Quarry-Labs-quarry.quarry-trusted-mac-arm64.service')
        [ -f "${TRUSTED_RUNNER_TEST_SERVICE_ACTIVE}" ]
        printf 'active\n'
        ;;
      *)
        printf 'unexpected guest invocation: %s\n' "${command_line}" >&2
        exit 93
        ;;
    esac
    ;;
  *)
    printf 'unexpected limactl invocation: %s\n' "$*" >&2
    exit 94
    ;;
esac
STUB

cat > "${bin}/launchctl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf 'launchctl %s\n' "$*" >> "${TRUSTED_RUNNER_TEST_LOG}"
case "${1:-}" in
  print)
    target="${2:-}"
    if [ -f "${TRUSTED_RUNNER_TEST_LAUNCHD_LOADED:-}" ]; then
      printf 'state = running\n'
    else
      exit 113
    fi
    ;;
  bootstrap)
    domain="${2:-}"
    plist="${3:-}"
    [ -f "${plist}" ] || exit 1
    : > "${TRUSTED_RUNNER_TEST_LAUNCHD_LOADED}"
    ;;
  bootout)
    target="${2:-}"
    rm -f -- "${TRUSTED_RUNNER_TEST_LAUNCHD_LOADED:-}"
    ;;
  load)
    plist="${2:-}"
    [ -f "${plist}" ] || exit 1
    : > "${TRUSTED_RUNNER_TEST_LAUNCHD_LOADED}"
    ;;
  unload)
    plist="${2:-}"
    rm -f -- "${TRUSTED_RUNNER_TEST_LAUNCHD_LOADED:-}"
    ;;
  *)
    printf 'unexpected launchctl invocation: %s\n' "$*" >&2
    exit 96
    ;;
esac
STUB

chmod +x "${bin}/uname" "${bin}/gh" "${bin}/limactl" "${bin}/launchctl"

cat > "${bin}/sleep" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "${bin}/sleep"

launchd_loaded="${state}/launchd-loaded"
autoidle_state="${state}/autoidle.state"

run_helper() {
  PATH="${bin}:/usr/bin:/bin" \
    HOME="${home}" \
    LAUNCHCTL="${bin}/launchctl" \
    SMOLRUNNER_QUARRY_AUTOIDLE_STATE="${autoidle_state}" \
    TRUSTED_RUNNER_TEST_LOG="${log}" \
    TRUSTED_RUNNER_TEST_CONFIGURED="${configured}" \
    TRUSTED_RUNNER_TEST_ROUTING="${routing}" \
    TRUSTED_RUNNER_TEST_RUNNING="${running}" \
    TRUSTED_RUNNER_TEST_SERVICE_ACTIVE="${service_active}" \
    TRUSTED_RUNNER_TEST_SCHEDULING_LABEL="${scheduling_label}" \
    TRUSTED_RUNNER_TEST_PILOT_SCHEDULING_LABEL="${pilot_scheduling_label}" \
    TRUSTED_RUNNER_TEST_BUSY_LATCHED="${busy_latched}" \
    TRUSTED_RUNNER_TEST_LAUNCHD_LOADED="${launchd_loaded}" \
    bash "${helper}" "$@"
}

install_output="${tmp}/install.out"
run_helper install > "${install_output}"
grep -F 'runner=quarry-trusted-mac-arm64 state=online' "${install_output}" >/dev/null
[ "$(cat "${routing}")" = quarry-trusted-local ]
[ -f "${configured}" ]
grep -F 'autostart enable --condition=login --tty=false smolrunner' "${log}" >/dev/null
grep -F '.id == 22' "${log}" >/dev/null
binding_line="$(grep -n -m1 -F '.id == 22' "${log}" | cut -d: -f1)"
service_start_line="$(grep -n -m1 -F '/usr/bin/sudo ./svc.sh start' "${log}" | cut -d: -f1)"
[ "${binding_line}" -lt "${service_start_line}" ]

status_output="${tmp}/status.out"
run_helper status > "${status_output}"
grep -F 'runner=quarry-trusted-mac-arm64 state=online busy=false' "${status_output}" >/dev/null
grep -F 'runner_schedulable=true' "${status_output}" >/dev/null
grep -F 'routing=quarry-trusted-local' "${status_output}" >/dev/null
grep -Fx active "${status_output}" >/dev/null

run_helper unroute > /dev/null
[ "$(cat "${routing}")" = ubuntu-24.04 ]

binding_output="${tmp}/binding.out"
service_starts_before="$(grep -c -F '/usr/bin/sudo ./svc.sh start' "${log}")"
set +e
TRUSTED_RUNNER_TEST_GITHUB_BINDING=foreign \
  run_helper route > "${binding_output}" 2>&1
binding_status=$?
set -e
[ "${binding_status}" -ne 0 ]
[ "$(cat "${routing}")" = ubuntu-24.04 ]
grep -F 'not the exact Quarry runner' "${binding_output}" >/dev/null
[ "$(grep -c -F '/usr/bin/sudo ./svc.sh start' "${log}")" = "${service_starts_before}" ]

run_helper route > /dev/null
[ "$(cat "${routing}")" = quarry-trusted-local ]

busy_pause_output="${tmp}/pause-busy.out"
service_stops_before="$(grep -c -F '/usr/bin/sudo ./svc.sh stop' "${log}" || true)"
set +e
TRUSTED_RUNNER_TEST_PILOT_ASSIGN_AFTER_COMMON_DRAIN=1 \
  run_helper pause > "${busy_pause_output}" 2>&1
busy_pause_status=$?
set -e
[ "${busy_pause_status}" -ne 0 ]
[ "$(cat "${routing}")" = ubuntu-24.04 ]
grep -F 'runner is busy' "${busy_pause_output}" >/dev/null
[ "$(grep -c -F '/usr/bin/sudo ./svc.sh stop' "${log}" || true)" = "${service_stops_before}" ]
[ -f "${running}" ]
[ -f "${service_active}" ]
[ ! -f "${scheduling_label}" ]
[ ! -f "${pilot_scheduling_label}" ]
[ -f "${busy_latched}" ]
grep -F 'labels/quarry-trusted-local' "${log}" >/dev/null
grep -F 'labels/smolrunner-quarry-pilot' "${log}" >/dev/null

rm -f -- "${busy_latched}"
run_helper route > /dev/null
[ -f "${scheduling_label}" ]
[ -f "${pilot_scheduling_label}" ]

pause_output="${tmp}/pause.out"
run_helper pause > "${pause_output}"
[ "$(cat "${routing}")" = ubuntu-24.04 ]
[ ! -f "${running}" ]
[ ! -f "${service_active}" ]
[ -f "${configured}" ]
[ ! -f "${scheduling_label}" ]
[ ! -f "${pilot_scheduling_label}" ]
[ ! -e "${home}/Library/LaunchAgents/io.lima-vm.autostart.smolrunner.plist" ]
grep -F 'state=paused' "${pause_output}" >/dev/null
grep -F 'memory=released caches=preserved' "${pause_output}" >/dev/null

stops_before="$(grep -c -F 'limactl stop --tty=false smolrunner' "${log}")"
stopped_pause_output="${tmp}/pause-stopped.out"
set +e
run_helper pause > "${stopped_pause_output}" 2>&1
stopped_pause_status=$?
set -e
[ "${stopped_pause_status}" -ne 0 ]
grep -F 'exact runner offline state is unobserved' "${stopped_pause_output}" >/dev/null
if grep -F 'state=paused' "${stopped_pause_output}" >/dev/null; then
  printf 'already-stopped VM was reported as an exact paused runner\n' >&2
  exit 1
fi
[ "$(grep -c -F 'limactl stop --tty=false smolrunner' "${log}")" = "${stops_before}" ]

resume_output="${tmp}/resume.out"
run_helper resume > "${resume_output}"
[ -f "${running}" ]
[ -f "${service_active}" ]
[ -f "${home}/Library/LaunchAgents/io.lima-vm.autostart.smolrunner.plist" ]
[ "$(cat "${routing}")" = quarry-trusted-local ]
[ -f "${scheduling_label}" ]
[ -f "${pilot_scheduling_label}" ]
grep -F 'state=online' "${resume_output}" >/dev/null

absent_remove_output="${tmp}/remove-absent.out"
set +e
TRUSTED_RUNNER_TEST_INSTANCE_EXISTS=0 \
  run_helper remove > "${absent_remove_output}" 2>&1
absent_remove_status=$?
set -e
[ "${absent_remove_status}" -ne 0 ]
grep -F 'exact runner removal cannot be proven' "${absent_remove_output}" >/dev/null
if grep -F 'state=removed' "${absent_remove_output}" >/dev/null; then
  printf 'absent instance was reported as removed\n' >&2
  exit 1
fi
[ -f "${configured}" ]

run_helper route > /dev/null

foreign_remove_output="${tmp}/remove-foreign.out"
service_stops_before="$(grep -c -F '/usr/bin/sudo ./svc.sh stop' "${log}" || true)"
autostart_disables_before="$(grep -c -F 'autostart disable --tty=false smolrunner' "${log}" || true)"
set +e
TRUSTED_RUNNER_TEST_GITHUB_BINDING=foreign \
  run_helper remove > "${foreign_remove_output}" 2>&1
foreign_remove_status=$?
set -e
[ "${foreign_remove_status}" -ne 0 ]
grep -F 'not the exact Quarry runner' "${foreign_remove_output}" >/dev/null
[ "$(grep -c -F '/usr/bin/sudo ./svc.sh stop' "${log}" || true)" = "${service_stops_before}" ]
[ "$(grep -c -F 'autostart disable --tty=false smolrunner' "${log}" || true)" = "${autostart_disables_before}" ]
[ -f "${configured}" ]

run_helper route > /dev/null

failed_remove_output="${tmp}/remove-failed.out"
removal_tokens_before="$(grep -c -F 'actions/runners/remove-token' "${log}" || true)"
service_stops_before="$(grep -c -F '/usr/bin/sudo ./svc.sh stop' "${log}" || true)"
set +e
TRUSTED_RUNNER_TEST_AUTOSTART_DISABLE_STATUS=7 \
  run_helper remove > "${failed_remove_output}" 2>&1
failed_remove_status=$?
set -e
[ "${failed_remove_status}" = 7 ]
if grep -F 'state=removed' "${failed_remove_output}" >/dev/null; then
  printf 'failed autostart disable was reported as removed\n' >&2
  exit 1
fi
[ -f "${configured}" ]
[ "$(grep -c -F 'actions/runners/remove-token' "${log}" || true)" = "${removal_tokens_before}" ]
[ "$(grep -c -F '/usr/bin/sudo ./svc.sh stop' "${log}" || true)" = "${service_stops_before}" ]

remove_output="${tmp}/remove.out"
run_helper remove > "${remove_output}"
[ "$(cat "${routing}")" = ubuntu-24.04 ]
[ ! -e "${configured}" ]
grep -F 'autostart disable --tty=false smolrunner' "${log}" >/dev/null
grep -F 'vm_disk=preserved caches=preserved' "${remove_output}" >/dev/null

# Re-install runner to test autoidle lifecycle
autoidle_install_output="${tmp}/autoidle-install.out"
run_helper install > "${autoidle_install_output}"
[ -f "${running}" ]
[ -f "${configured}" ]
[ "$(cat "${routing}")" = quarry-trusted-local ]

autoidle_enable_output="${tmp}/autoidle-enable.out"
run_helper autoidle-enable > "${autoidle_enable_output}"
grep -F 'autoidle=enabled timeout=900s vm=smolrunner' "${autoidle_enable_output}" >/dev/null
plist_file="${home}/Library/LaunchAgents/io.smolrunner.quarry-autoidle.smolrunner.plist"
[ -f "${plist_file}" ]
grep -F '<string>io.smolrunner.quarry-autoidle.smolrunner</string>' "${plist_file}" >/dev/null
grep -F '<string>autoidle-tick</string>' "${plist_file}" >/dev/null
grep -F '<key>StartInterval</key>' "${plist_file}" >/dev/null
grep -A1 -F '<key>RunAtLoad</key>' "${plist_file}" | grep -F '<false/>' >/dev/null
! grep -F '<key>KeepAlive</key>' "${plist_file}" >/dev/null
[ -f "${launchd_loaded}" ]

# Idempotent enable test
run_helper autoidle-enable > /dev/null
[ -f "${plist_file}" ]
[ -f "${launchd_loaded}" ]

# Autoidle status test
autoidle_status_output="${tmp}/autoidle-status.out"
run_helper autoidle-status > "${autoidle_status_output}"
grep -F 'autoidle=enabled timeout=900s elapsed=0s' "${autoidle_status_output}" >/dev/null
grep -F 'vm=smolrunner state=Running' "${autoidle_status_output}" >/dev/null
grep -F 'runner=quarry-trusted-mac-arm64 state=online busy=false' "${autoidle_status_output}" >/dev/null
grep -F 'runner_schedulable=true' "${autoidle_status_output}" >/dev/null
grep -F 'routing=quarry-trusted-local' "${autoidle_status_output}" >/dev/null

# 1. Under threshold: ticks do not invoke pause
run_helper autoidle-tick 300
[ -f "${running}" ]
[ -f "${service_active}" ]
[ "$(cat "${routing}")" = quarry-trusted-local ]
status_300="${tmp}/status-300.out"
run_helper autoidle-status > "${status_300}"
grep -F 'autoidle=enabled timeout=900s elapsed=300s' "${status_300}" >/dev/null

run_helper autoidle-tick 300
status_600="${tmp}/status-600.out"
run_helper autoidle-status > "${status_600}"
grep -F 'autoidle=enabled timeout=900s elapsed=600s' "${status_600}" >/dev/null
[ -f "${running}" ]
[ "$(cat "${routing}")" = quarry-trusted-local ]

# 2. Busy resets the timer
status_busy="${tmp}/status-busy.out"
TRUSTED_RUNNER_TEST_BUSY=true run_helper autoidle-tick 15
run_helper autoidle-status > "${status_busy}"
grep -F 'autoidle=enabled timeout=900s elapsed=0s' "${status_busy}" >/dev/null
grep -F 'runner=quarry-trusted-mac-arm64 state=online busy=false' "${status_busy}" >/dev/null
[ -f "${running}" ]
[ "$(cat "${routing}")" = quarry-trusted-local ]

# Accumulate to 600s again after busy reset
run_helper autoidle-tick 600
status_resumed="${tmp}/status-resumed.out"
run_helper autoidle-status > "${status_resumed}"
grep -F 'autoidle=enabled timeout=900s elapsed=600s' "${status_resumed}" >/dev/null
[ -f "${running}" ]

# 3. Exact threshold invokes pause once
pauses_before="$(grep -c -F 'limactl stop --tty=false smolrunner' "${log}" || true)"
run_helper autoidle-tick 300 # 600 + 300 = 900s threshold reached
[ ! -f "${running}" ]
[ ! -f "${service_active}" ]
[ "$(cat "${routing}")" = ubuntu-24.04 ]
[ "$(grep -c -F 'limactl stop --tty=false smolrunner' "${log}")" = "$(( pauses_before + 1 ))" ]

status_paused="${tmp}/status-paused.out"
run_helper autoidle-status > "${status_paused}"
grep -F 'autoidle=enabled timeout=900s elapsed=0s' "${status_paused}" >/dev/null
grep -F 'vm=smolrunner state=Stopped' "${status_paused}" >/dev/null
grep -F 'routing=ubuntu-24.04' "${status_paused}" >/dev/null

# 4. Hosted / stopped quiescence (zero gh api calls while stopped)
gh_calls_before="$(grep -c -F 'gh api' "${log}" || true)"
run_helper autoidle-tick 15
run_helper autoidle-tick 15
gh_calls_after="$(grep -c -F 'gh api' "${log}" || true)"
[ "${gh_calls_before}" = "${gh_calls_after}" ]
[ "$(grep -c -F 'limactl stop --tty=false smolrunner' "${log}")" = "$(( pauses_before + 1 ))" ]

# 5. Busy race during pause retries gracefully
run_helper resume > /dev/null
[ -f "${running}" ]
[ "$(cat "${routing}")" = quarry-trusted-local ]
run_helper autoidle-tick 885
status_885="${tmp}/status-885.out"
run_helper autoidle-status > "${status_885}"
grep -F 'autoidle=enabled timeout=900s elapsed=885s' "${status_885}" >/dev/null

pauses_before="$(grep -c -F 'limactl stop --tty=false smolrunner' "${log}" || true)"
TRUSTED_RUNNER_TEST_PILOT_ASSIGN_AFTER_COMMON_DRAIN=1 \
  run_helper autoidle-tick 15
[ -f "${running}" ]
[ -f "${service_active}" ]
[ "$(grep -c -F 'limactl stop --tty=false smolrunner' "${log}")" = "${pauses_before}" ]

rm -f -- "${busy_latched}"
run_helper route > /dev/null

run_helper autoidle-tick 900
[ ! -f "${running}" ]
[ "$(cat "${routing}")" = ubuntu-24.04 ]
[ "$(grep -c -F 'limactl stop --tty=false smolrunner' "${log}")" = "$(( pauses_before + 1 ))" ]

# 6. Disable/uninstall is idempotent and preserves VM disk, caches, and registration
run_helper autoidle-disable > /dev/null
[ ! -f "${plist_file}" ]
[ ! -f "${launchd_loaded}" ]
[ -f "${configured}" ]

run_helper autoidle-disable > /dev/null
[ ! -f "${plist_file}" ]
[ ! -f "${launchd_loaded}" ]
[ -f "${configured}" ]

autoidle_disabled_status="${tmp}/autoidle-disabled-status.out"
run_helper autoidle-status > "${autoidle_disabled_status}"
grep -F 'autoidle=disabled timeout=900s elapsed=0s' "${autoidle_disabled_status}" >/dev/null

# Clean removal
final_remove_output="${tmp}/final-remove.out"
run_helper remove > "${final_remove_output}"
[ ! -e "${configured}" ]

for secret in registration-secret removal-secret; do
  if grep -F "${secret}" \
    "${log}" "${install_output}" "${status_output}" \
    "${binding_output}" "${busy_pause_output}" "${pause_output}" \
    "${stopped_pause_output}" \
    "${resume_output}" "${absent_remove_output}" \
    "${foreign_remove_output}" "${failed_remove_output}" \
    "${remove_output}" "${autoidle_install_output}" \
    "${autoidle_enable_output}" "${autoidle_status_output}" \
    "${status_300}" "${status_600}" "${status_busy}" \
    "${status_resumed}" "${status_paused}" "${status_885}" \
    "${autoidle_disabled_status}" "${final_remove_output}" >/dev/null; then
    printf 'one-time token escaped into observable output: %s\n' "${secret}" >&2
    exit 1
  fi
done

printf 'trusted Quarry runner tests passed\n'
