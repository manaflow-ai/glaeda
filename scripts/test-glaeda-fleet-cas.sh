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

# A store launched before its tailnet address exists waits instead of crashing and being
# restarted repeatedly by launchd. The fake ifconfig exposes the address on its second call.
run_root=$temporary_root/run-wrapper
mkdir -p "$run_root/bin" "$run_root/run"
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
env FLEET_CAS_ROOT="$run_root" FLEET_CAS_IFCONFIG_STATE="$temporary_root/ifconfig.calls" PATH="$stub_bin:$PATH" \
  "$repo_root/tools/fleet-cas-prototype/scripts/fleet-cas-run" store >"$temporary_root/run-wrapper.out" 2>"$temporary_root/run-wrapper.err"
grep -q 'waiting for local bind address 100.89.140.13' "$temporary_root/run-wrapper.err"
grep -q 'started tcp:100.89.140.13:7450 store' "$temporary_root/run-wrapper.out"

echo "glaeda-fleet-cas role-scoped uninstall and bind-wait tests passed"
