#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
script=$repo_root/scripts/glaeda-fleet-cas
temporary_root=$(mktemp -d)
trap 'rm -rf "$temporary_root"' EXIT

root=$temporary_root/xcode
home=$temporary_root/home
daemons=$temporary_root/LaunchDaemons
stub_bin=$temporary_root/bin
mkdir -p "$root/run" "$root/node-store" "$root/fleet-store" \
  "$home/Library/LaunchAgents" "$daemons" "$stub_bin"
printf 'node data\n' >"$root/node-store/keep"
printf 'store data\n' >"$root/fleet-store/keep"
printf 'store=100.89.140.13:7450\n' >"$root/fleet-cas.env"
for role in node store; do
  label="com.teamleaderleo.glaeda.fleet-cas-$role"
  printf 'args\n' >"$root/run/$role.args"
  : >"$home/Library/LaunchAgents/$label.plist"
done
prewarm_label=com.teamleaderleo.glaeda.fleet-cas-prewarm
: >"$home/Library/LaunchAgents/$prewarm_label.plist"

cat >"$stub_bin/launchctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FLEET_CAS_TEST_LAUNCHCTL_LOG:?}"
EOF
chmod +x "$stub_bin/launchctl"

cat >"$stub_bin/diskutil" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = info ]; then
  printf 'Type (Bundle): apfs\nMount Point: /\n'
fi
EOF
chmod +x "$stub_bin/diskutil"

common_env=(
  FLEET_CAS_ROOT="$root"
  FLEET_CAS_LAUNCH_DAEMONS_DIR="$daemons"
  FLEET_CAS_TEST_LAUNCHCTL_LOG="$temporary_root/launchctl.log"
  HOME="$home"
  PATH="$stub_bin:$PATH"
)

# Planning is read-only, including the role selector.
env "${common_env[@]}" "$script" uninstall store >"$temporary_root/store-plan"
test -e "$root/run/store.args"
test -e "$root/run/node.args"
test -e "$root/fleet-cas.env"
grep -q 'fleet-cas-store' "$temporary_root/store-plan"
! grep -q 'fleet-cas-node' "$temporary_root/store-plan"
env "${common_env[@]}" "$script" uninstall >"$temporary_root/all-plan"
grep -q 'fleet-cas-node' "$temporary_root/all-plan"
grep -q 'fleet-cas-store' "$temporary_root/all-plan"

# Store removal leaves the reader node, its configuration and both data stores.
env "${common_env[@]}" "$script" uninstall store --apply >"$temporary_root/store-apply"
test ! -e "$root/run/store.args"
test ! -e "$home/Library/LaunchAgents/com.teamleaderleo.glaeda.fleet-cas-store.plist"
test -e "$root/run/node.args"
test -e "$home/Library/LaunchAgents/com.teamleaderleo.glaeda.fleet-cas-node.plist"
test -e "$root/fleet-cas.env"
test -e "$root/node-store/keep"
test -e "$root/fleet-store/keep"
grep -q 'bootout gui/.*/com.teamleaderleo.glaeda.fleet-cas-store' "$temporary_root/launchctl.log"
! grep -q 'fleet-cas-node' "$temporary_root/launchctl.log"

# Node removal also removes its prewarm agent and node environment, but keeps stores.
env "${common_env[@]}" "$script" uninstall node --apply >"$temporary_root/node-apply"
test ! -e "$root/run/node.args"
test ! -e "$home/Library/LaunchAgents/com.teamleaderleo.glaeda.fleet-cas-node.plist"
test ! -e "$home/Library/LaunchAgents/$prewarm_label.plist"
test ! -e "$root/fleet-cas.env"
test -e "$root/node-store/keep"
test -e "$root/fleet-store/keep"
grep -q 'bootout gui/.*/com.teamleaderleo.glaeda.fleet-cas-prewarm' "$temporary_root/launchctl.log"
grep -q 'bootout gui/.*/com.teamleaderleo.glaeda.fleet-cas-node' "$temporary_root/launchctl.log"

# A root-installed service is never silently removed by an unprivileged caller.
: >"$daemons/com.teamleaderleo.glaeda.fleet-cas-store.plist"
env "${common_env[@]}" "$script" uninstall store --apply >"$temporary_root/root-plan"
grep -q "ROOT STEP NEEDED: sudo launchctl bootout system/com.teamleaderleo.glaeda.fleet-cas-store" "$temporary_root/root-plan"
test -e "$daemons/com.teamleaderleo.glaeda.fleet-cas-store.plist"

# A custom store root must already be mounted. This prevents a controller rollout from
# silently creating the CAS on its boot volume when an APFS sparsebundle was not mounted.
if env "${common_env[@]}" "$script" store --listen 100.89.225.106:7450 --apply \
  >"$temporary_root/boot-root.out" 2>"$temporary_root/boot-root.err"; then
  exit 1
fi
grep -q 'custom fleet-CAS store root must be a separate mounted /Volumes APFS volume' "$temporary_root/boot-root.err"

if env "${common_env[@]}" FLEET_CAS_ROOT="$temporary_root/not-mounted" \
  "$script" store --listen 100.89.225.106:7450 --apply >"$temporary_root/missing-root.out" 2>"$temporary_root/missing-root.err"; then
  exit 1
fi
grep -q 'custom fleet-CAS store root is not mounted' "$temporary_root/missing-root.err"

# A store launched before its tailnet address exists waits instead of crashing and being
# restarted repeatedly by launchd. The fake ifconfig exposes the address on its second call.
run_root=$temporary_root/run-wrapper
mkdir -p "$run_root/bin" "$run_root/run"
wrapper=$run_root/fleet-cas-run
# Keep this bind-wait test on an isolated temporary root without presenting it as a deployed
# custom store. The custom-store mount guard is exercised separately through its pure validator.
sed "s|^ROOT=.*|ROOT=$run_root|" \
  "$repo_root/tools/fleet-cas-prototype/scripts/fleet-cas-run" >"$wrapper"
chmod +x "$wrapper"
cat >"$run_root/bin/fleet-cas" <<'EOF'
#!/usr/bin/env bash
printf 'started %s\n' "$*"
EOF
chmod +x "$run_root/bin/fleet-cas"
cat >"$stub_bin/ifconfig" <<'EOF'
#!/usr/bin/env bash
state=${FLEET_CAS_IFCONFIG_STATE:?}
n=$(cat "$state" 2>/dev/null || echo 0)
n=$((n + 1)); printf '%s\n' "$n" >"$state"
[ "$n" -ge 2 ] && echo '    inet 100.89.140.13'
EOF
cat >"$stub_bin/sleep" <<'EOF'
#!/usr/bin/env bash
:
EOF
chmod +x "$stub_bin/ifconfig" "$stub_bin/sleep"
printf 'tcp:100.89.140.13:7450\nstore\n' >"$run_root/run/store.args"
printf 'old-pid\n' >"$run_root/run/store.pid"
printf 'old-bin\n' >"$run_root/run/store.bin"
env -u FLEET_CAS_ROOT FLEET_CAS_IFCONFIG_STATE="$temporary_root/ifconfig.calls" PATH="$stub_bin:$PATH" \
  "$wrapper" store >"$temporary_root/run-wrapper.out" 2>"$temporary_root/run-wrapper.err"
grep -q 'waiting for local bind address 100.89.140.13' "$temporary_root/run-wrapper.err"
grep -q 'started tcp:100.89.140.13:7450 store' "$temporary_root/run-wrapper.out"
test "$(cat "$run_root/run/store.pid")" != old-pid
test "$(cat "$run_root/run/store.bin")" = "$(shasum -a 256 "$run_root/bin/fleet-cas" | cut -c1-64)"

# A missing address is bounded and clears stale receipts instead of leaving a false daemon
# identity behind. The no-op sleep keeps this test fast while the wrapper still counts seconds.
cat >"$stub_bin/ifconfig" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
printf 'stale-pid\n' >"$run_root/run/store.pid"
printf 'stale-bin\n' >"$run_root/run/store.bin"
if env -u FLEET_CAS_ROOT PATH="$stub_bin:$PATH" \
  "$wrapper" store >"$temporary_root/timeout.out" 2>"$temporary_root/timeout.err"; then
  exit 1
fi
grep -q 'timed out after 60s waiting for local bind address 100.89.140.13' "$temporary_root/timeout.err"
test ! -e "$run_root/run/store.pid"
test ! -e "$run_root/run/store.bin"
! grep -q '^started ' "$temporary_root/timeout.out"

# Node, wildcard and IPv6 endpoints go straight to the binary's own validation.
cat >"$stub_bin/ifconfig" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FLEET_CAS_IFCONFIG_CALLS:?}"
exit 1
EOF
for role_endpoint in \
  'node|unix:/tmp/fleet-cas.sock' \
  'store|tcp:0.0.0.0:7450' \
  'store|tcp:[fd7a:115c:a1e0::1]:7450' \
  'store|tcp:999.1.1.1:7450' \
  'store|tcp:100.89.140.13:not-a-port'; do
  role=${role_endpoint%%|*}; endpoint=${role_endpoint#*|}
  printf '%s\n' "$endpoint" >"$run_root/run/$role.args"
  env -u FLEET_CAS_ROOT FLEET_CAS_IFCONFIG_CALLS="$temporary_root/ifconfig.unexpected" PATH="$stub_bin:$PATH" \
    "$wrapper" "$role" >"$temporary_root/$role-${endpoint//[^A-Za-z0-9]/_}.out"
done
test ! -e "$temporary_root/ifconfig.unexpected"

# The installer must recognize a wrapper that is still waiting for its address. Otherwise a
# changed args file would leave the old endpoint loaded until launchd eventually restarts it.
cat >"$run_root/bin/fleet-cas-run" <<'EOF'
#!/usr/bin/env python3
import signal
import time
def stop(*_):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while True:
    time.sleep(1)
EOF
chmod +x "$run_root/bin/fleet-cas-run"
ROOT="$run_root" BIN="$run_root/bin"
eval "$(sed -n '/^restart_role()/,/^}/p' "$script")"

# A same-role wrapper from another checkout must not be killed by this checkout's stale PID.
foreign_root=$temporary_root/foreign-wrapper
mkdir -p "$foreign_root/bin"
cp "$run_root/bin/fleet-cas-run" "$foreign_root/bin/fleet-cas-run"
"$foreign_root/bin/fleet-cas-run" store &
foreign_pid=$!
sleep 0.2
printf '%s\n' "$foreign_pid" >"$run_root/run/store.pid"
ROOT="$run_root" BIN="$run_root/bin"
restart_role store tcp:100.89.140.13:7450
kill -0 "$foreign_pid" 2>/dev/null
kill "$foreign_pid" 2>/dev/null || true
wait "$foreign_pid" 2>/dev/null || true

"$run_root/bin/fleet-cas-run" store &
waiting_pid=$!
sleep 0.2
printf '%s\n' "$waiting_pid" >"$run_root/run/store.pid"
ROOT="$run_root" BIN="$run_root/bin"
restart_role store tcp:100.89.140.13:7450
wait "$waiting_pid" 2>/dev/null || true
! kill -0 "$waiting_pid" 2>/dev/null

# A missing args file fails before launching the binary and clears the wrapper identity.
rm -f "$run_root/run/store.args"
printf 'stale-pid\n' >"$run_root/run/store.pid"
printf 'stale-bin\n' >"$run_root/run/store.bin"
if env -u FLEET_CAS_ROOT PATH="$stub_bin:$PATH" \
  "$wrapper" store >"$temporary_root/missing.out" 2>"$temporary_root/missing.err"; then
  exit 1
fi
test ! -e "$run_root/run/store.pid"
test ! -e "$run_root/run/store.bin"
! grep -q '^started ' "$temporary_root/missing.out"

echo "glaeda-fleet-cas role-scoped uninstall and bind-wait tests passed"
