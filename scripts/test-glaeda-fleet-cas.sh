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
printf 'FLEET_CAS_STORE=100.89.140.13:7450\nFLEET_CAS_TRUSTED_KEYS=ab\n' >"$root/fleet-cas.env"
cat >"$root/bin-fleet-cas" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FLEET_CAS_TEST_CALLS:?}"
EOF
chmod +x "$root/bin-fleet-cas"
mkdir -p "$root/bin"
mv "$root/bin-fleet-cas" "$root/bin/fleet-cas"
cat >"$stub_bin/xcodebuild" <<'EOF'
#!/usr/bin/env bash
printf 'Build version 17F113\n'
EOF
chmod +x "$stub_bin/xcodebuild"
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
[ "$1" = print ] && [ -n "${FLEET_CAS_TEST_PRINT_FAIL:-}" ] && exit 113
exit 0
EOF
chmod +x "$stub_bin/launchctl"
# No passwordless sudo unless a test says so; a working one runs the command it is given, logged.
printf '#!/usr/bin/env bash\nexit 1\n' >"$stub_bin/nosudo"
cat >"$stub_bin/okSudo" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FLEET_CAS_TEST_SUDO_LOG:?}"
[ "$1" = chown ] && exit 0
exec "$@"
EOF
chmod +x "$stub_bin/nosudo" "$stub_bin/okSudo"

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
  FLEET_CAS_SUDO="$stub_bin/nosudo"
  HOME="$home"
  PATH="$stub_bin:$PATH"
)

# A canary overrides only the read endpoint. Trusted keys stay on disk, local
# materialization stays in node-store, and rejected values cannot reach the CAS.
calls="$temporary_root/fleet-cas.calls"
for name in fleet-cas-marker.sh fleet-cas-warm.sh; do
  helper="$repo_root/tools/fleet-cas-prototype/scripts/$name"
  test "$(env FLEET_CAS_ROOT=/nonexistent "$helper" --capabilities)" = fleet-cas-store-override/v1
  : >"$calls"
  env FLEET_CAS_ROOT="$root" FLEET_CAS_TEST_CALLS="$calls" \
    FLEET_CAS_TRUSTED_KEYS=untrusted-ambient-key PATH="$stub_bin:$PATH" \
    "$helper" cmux@catchup-v1 "$(printf 'a%.0s' {1..40})"
  grep -q 'http://100.89.140.13:7450 ' "$calls"
  grep -q -- '--trusted-keys ab' "$calls"
  : >"$calls"
  env FLEET_CAS_ROOT="$root" FLEET_CAS_TEST_CALLS="$calls" \
    FLEET_CAS_STORE_OVERRIDE=192.0.2.10:17450 PATH="$stub_bin:$PATH" \
    "$helper" cmux@catchup-v1 "$(printf 'a%.0s' {1..40})" 2>"$temporary_root/read-route"
  grep -q 'http://192.0.2.10:17450 ' "$calls"
  grep -q -- '--trusted-keys ab' "$calls"
  grep -qx 'fleet-cas: read-store=192.0.2.10:17450' "$temporary_root/read-route"
  if [ "$name" = fleet-cas-warm.sh ]; then grep -q -- "--store $root/node-store" "$calls"; fi
  for endpoint in 'http://evil' ':17450' '-host:17450' 'host:0' 'host:65536' 'host:1234567890123456789' 'host:abc' 'host:80/path' 'user@host:80' 'host:80:90'; do
    : >"$calls"
    rc=0
    env FLEET_CAS_ROOT="$root" FLEET_CAS_TEST_CALLS="$calls" \
      FLEET_CAS_STORE_OVERRIDE="$endpoint" PATH="$stub_bin:$PATH" \
      "$helper" cmux@catchup-v1 "$(printf 'a%.0s' {1..40})" \
      >"$temporary_root/override.out" 2>"$temporary_root/override.err" || rc=$?
    test "$rc" = 2
    test ! -s "$calls"
    grep -q 'invalid FLEET_CAS_STORE_OVERRIDE' "$temporary_root/override.err"
  done
 done

# Planning is read-only, including the role selector.
env "${common_env[@]}" "$script" uninstall store >"$temporary_root/store-plan"
test -e "$root/run/store.args"
test -e "$root/run/node.args"
test -e "$root/fleet-cas.env"
grep -q 'fleet-cas-store' "$temporary_root/store-plan"
! grep -q 'fleet-cas-node' "$temporary_root/store-plan"

# A validated prebuilt bundle owns the boot-volume helpers too. Poisoning the
# source checkout must not change what an image-backed launch installs.
prebuilt="$temporary_root/prebuilt"
poison_proto="$temporary_root/proto/scripts"
mkdir -p "$prebuilt" "$poison_proto"
for f in fleet-cas fleet-cas-run fleet-cas-mount fleet-cas-settings.sh fleet-cas-marker.sh fleet-cas-warm.sh fleet-cas-prewarm.sh fleet-cas-writer-build.sh; do
  printf 'prebuilt %s\n' "$f" >"$prebuilt/$f"
  chmod +x "$prebuilt/$f"
done
(cd "$prebuilt" && for f in fleet-cas fleet-cas-run fleet-cas-mount fleet-cas-settings.sh fleet-cas-marker.sh fleet-cas-warm.sh fleet-cas-prewarm.sh fleet-cas-writer-build.sh; do shasum -a 256 "$f"; done) >"$prebuilt/SHA256SUMS"
printf 'poison source runner\n' >"$poison_proto/fleet-cas-run"
printf 'poison source mount\n' >"$poison_proto/fleet-cas-mount"
install_root="$temporary_root/prebuilt-install"
mkdir -p "$install_root"
install_binaries_body=$(sed -n '/^install_binaries()/,/^write_env()/p' "$script" | sed '$d')
eval "$install_binaries_body"
say() { :; }
check_owner() { :; }
ROOT="$install_root" BIN="$install_root/bin" proto="$temporary_root/proto" apply=1 \
  custom_store_image='/Volumes/T5 EVO/glaeda-fleet-cas.sparsebundle' launch_root="$install_root/launch" \
  HOME="$temporary_root/home" GLAEDA_FLEET_CAS_PREBUILT="$prebuilt" install_binaries
cmp "$prebuilt/fleet-cas-run" "$install_root/launch/fleet-cas-run"
cmp "$prebuilt/fleet-cas-mount" "$install_root/launch/fleet-cas-mount"
! grep -q poison "$install_root/launch/fleet-cas-run"
! grep -q poison "$install_root/launch/fleet-cas-mount"
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
if env "${common_env[@]}" "$script" uninstall store --apply >"$temporary_root/root-plan"; then exit 1; fi
grep -q "ROOT STEP NEEDED: sudo launchctl bootout system/com.teamleaderleo.glaeda.fleet-cas-store" "$temporary_root/root-plan"
test -e "$daemons/com.teamleaderleo.glaeda.fleet-cas-store.plist"

# With passwordless sudo, uninstall removes the daemon, its gc daemon and both old agents.
: >"$daemons/com.teamleaderleo.glaeda.fleet-cas-store-gc.plist"
: >"$home/Library/LaunchAgents/com.teamleaderleo.glaeda.fleet-cas-store.plist"
: >"$home/Library/LaunchAgents/com.teamleaderleo.glaeda.fleet-cas-store-gc.plist"
: >"$temporary_root/sudo.log"
env "${common_env[@]}" FLEET_CAS_SUDO="$stub_bin/okSudo" FLEET_CAS_TEST_SUDO_LOG="$temporary_root/sudo.log" \
  "$script" uninstall store --apply >"$temporary_root/root-apply"
test ! -e "$daemons/com.teamleaderleo.glaeda.fleet-cas-store.plist"
test ! -e "$daemons/com.teamleaderleo.glaeda.fleet-cas-store-gc.plist"
test ! -e "$home/Library/LaunchAgents/com.teamleaderleo.glaeda.fleet-cas-store.plist"
test ! -e "$home/Library/LaunchAgents/com.teamleaderleo.glaeda.fleet-cas-store-gc.plist"
grep -q 'launchctl bootout system/com.teamleaderleo.glaeda.fleet-cas-store$' "$temporary_root/sudo.log"
grep -q 'launchctl bootout system/com.teamleaderleo.glaeda.fleet-cas-store-gc$' "$temporary_root/sudo.log"

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

# The LAN relay: a socket-activated LaunchAgent whose only LAN client is Apple's nc.
relay_root=$temporary_root/relay
mkdir -p "$relay_root/home/Library/LaunchAgents" "$relay_root/bin"
cat >"$stub_bin/plutil" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$stub_bin/plutil"
eval "$(sed -n '/^RELAY_LABEL=/,/^# Stop the role/p' "$script" | sed '$d')"
eval "$(sed -n '/^xml()/p' "$script")"
say() { :; }
bootstrap() { printf 'bootstrap %s %s\n' "$2" "$3" >>"$temporary_root/launchctl.log"; }
AGENTS=$relay_root/home/Library/LaunchAgents LOGS=$relay_root/logs RELAY_LISTEN=127.0.0.1:7451
BIN=$relay_root/bin apply=1 trusted=ab
PATH="$stub_bin:$PATH" write_relay 172.20.21.158:7450
plist=$AGENTS/com.teamleaderleo.glaeda.fleet-cas-relay.plist
tr -d ' \n' <"$plist" >"$relay_root/plist.flat"
grep -q '<string>/usr/bin/nc</string><string>-G</string><string>2</string><string>172.20.21.158</string><string>7450</string></array>' "$relay_root/plist.flat"
grep -q '<key>inetdCompatibility</key><dict><key>Wait</key><false/></dict>' "$relay_root/plist.flat"
grep -q '<key>SockNodeName</key><string>127.0.0.1</string><key>SockServiceName</key><string>7451</string>' "$relay_root/plist.flat"
grep -q 'bootstrap com.teamleaderleo.glaeda.fleet-cas-relay' "$temporary_root/launchctl.log"
# The relay check asks the store a real question: a marker lookup answers 0 or 1 only from a
# fleet-cas store; a transport failure (2) through the relay fails the install.
for code in 0 1; do
  printf '#!/usr/bin/env bash\necho "$*" >>%q\nexit %s\n' "$relay_root/calls" "$code" >"$BIN/fleet-cas"
  chmod +x "$BIN/fleet-cas"
  check_relay 172.20.21.158:7450
done
grep -q '^marker get http://127.0.0.1:7451 glaeda-relay-check/' "$relay_root/calls"
printf '#!/usr/bin/env bash\nexit 2\n' >"$BIN/fleet-cas"
if PATH="$stub_bin:$PATH" check_relay 172.20.21.158:7450 2>"$relay_root/check.err"; then exit 1; fi
grep -q 'does not reach the store at 172.20.21.158:7450' "$relay_root/check.err"
# No --relay removes it.
FLEET_CAS_TEST_LAUNCHCTL_LOG="$temporary_root/launchctl.log" PATH="$stub_bin:$PATH" write_relay ""
test ! -e "$plist"
grep -q 'bootout gui/.*/com.teamleaderleo.glaeda.fleet-cas-relay' "$temporary_root/launchctl.log"
# A node planned with --relay points its upstream and env at the local relay.
env "${common_env[@]}" "$script" node --relay 172.20.21.158:7450 --trusted-keys ab >"$relay_root/node-plan"
grep -q 'relay com.teamleaderleo.glaeda.fleet-cas-relay: 127.0.0.1:7451 to /usr/bin/nc 172.20.21.158:7450' "$relay_root/node-plan"
grep -q 'store 127.0.0.1:7451 (relay to 172.20.21.158:7450)' "$relay_root/node-plan"
grep -q -- '--upstream http://127.0.0.1:7451' "$relay_root/node-plan"
if env "${common_env[@]}" "$script" node --relay 172.20.21.158:7450 --store 100.89.140.13:7450 \
  >/dev/null 2>"$relay_root/both.err"; then exit 1; fi
grep -q 'drop --store' "$relay_root/both.err"

# Headless roles are system LaunchDaemons (UserName this user, HOME set), migrated from agents.
# The store host's own gc: a custom store root only, its own label, the gc-only prune.
ROOT=/Volumes/glaeda-fleet-cas launch_root=$relay_root/launch here=$repo_root FLEET_CAS_ROOT=/Volumes/glaeda-fleet-cas
LAUNCH_DAEMONS=$relay_root/daemons LOGS=$relay_root/logs SUDO="$stub_bin/okSudo"
export FLEET_CAS_TEST_SUDO_LOG=$relay_root/sudo.log FLEET_CAS_TEST_LAUNCHCTL_LOG=$temporary_root/launchctl.log
eval "$(sed -n '/^state_for()/,/^}/p;/^runner_for()/,/^}/p;/^plist_for()/,/^}/p' "$script")"
mkdir -p "$LAUNCH_DAEMONS"
eval "$(sed -n '/^root_run()/,/^health_failed()/p' "$script" | sed '$d')"
unset -f bootstrap
unset -f say; say() { :; }
gc=com.teamleaderleo.glaeda.fleet-cas-store-gc
: >"$AGENTS/$gc.plist"   # the old agent
: >"$relay_root/sudo.log"
PATH="$stub_bin:$PATH" apply=1 write_store_prune
gc_plist=$LAUNCH_DAEMONS/$gc.plist
test ! -e "$AGENTS/$gc.plist"
grep -q "bootout gui/.*/$gc" "$temporary_root/launchctl.log"
grep -q "launchctl bootstrap system $gc_plist" "$relay_root/sudo.log"
tr -d ' \n' <"$gc_plist" >"$relay_root/gc.flat"
grep -q "<key>UserName</key><string>$(id -un)</string>" "$relay_root/gc.flat"
grep -q "<key>HOME</key><string>$HOME</string>" "$relay_root/gc.flat"
grep -q "<string>$relay_root/launch/glaeda-fleet-cas-prune</string><string>--apply</string><string>--store-gc-only</string>" "$relay_root/gc.flat"
grep -q '<key>GLAEDA_FLEET_CAS_ROOT</key><string>/Volumes/glaeda-fleet-cas</string>' "$relay_root/gc.flat"
grep -q '<key>StartInterval</key><integer>3600</integer>' "$relay_root/gc.flat"
cmp "$repo_root/scripts/glaeda-fleet-cas-prune" "$relay_root/launch/glaeda-fleet-cas-prune"
# A re-run with the same daemon loaded changes nothing (no second bootstrap).
: >"$relay_root/sudo.log"
PATH="$stub_bin:$PATH" apply=1 write_store_prune
! grep -q bootstrap "$relay_root/sudo.log"
# Without sudo and without a daemon it stops with the root commands, and writes no agent.
rm -f "$gc_plist"
if (FLEET_CAS_TEST_PRINT_FAIL=1 SUDO="$stub_bin/nosudo" PATH="$stub_bin:$PATH" apply=1 write_store_prune) 2>"$relay_root/nosudo.err"; then exit 1; fi
test ! -e "$AGENTS/$gc.plist"
grep -q 'needs a system LaunchDaemon and there is no passwordless sudo' "$relay_root/nosudo.err"
rm -f "$gc_plist"
FLEET_CAS_ROOT= PATH="$stub_bin:$PATH" write_store_prune
test ! -e "$gc_plist"

# The store itself: plist as a daemon, never an agent, even when only an agent exists.
store=com.teamleaderleo.glaeda.fleet-cas-store
: >"$AGENTS/$store.plist"
FLEET_CAS_ROOT=$relay_root/xcode ROOT=$relay_root/xcode; mkdir -p "$ROOT/launchd"
custom_store_volume_uuid=u custom_store_image=
FLEET_CAS_ROOT= plist_for store "$store" sys >"$ROOT/launchd/$store.plist"
tr -d ' \n' <"$ROOT/launchd/$store.plist" >"$relay_root/store.flat"
grep -q "<key>UserName</key><string>$(id -un)</string>" "$relay_root/store.flat"
grep -q "<key>EnvironmentVariables</key><dict><key>HOME</key><string>$HOME</string></dict>" "$relay_root/store.flat"
: >"$relay_root/sudo.log"
PATH="$stub_bin:$PATH" install_daemon "$store" "$ROOT/launchd/$store.plist"
test "$DAEMON_STATE" = installed
test ! -e "$AGENTS/$store.plist"
cmp "$ROOT/launchd/$store.plist" "$LAUNCH_DAEMONS/$store.plist"
# An hand-installed daemon that differs is adopted, not duplicated, when sudo is missing.
printf 'hand\n' >"$LAUNCH_DAEMONS/$store.plist"
SUDO="$stub_bin/nosudo" PATH="$stub_bin:$PATH" install_daemon "$store" "$ROOT/launchd/$store.plist" 2>"$relay_root/adopt.err"
test "$DAEMON_STATE" = adopted
grep -q 'adopting it as is' "$relay_root/adopt.err"
test "$(cat "$LAUNCH_DAEMONS/$store.plist")" = hand

# refresh, glaeda-update's node rollout: an installed node whose bin/fleet-cas.source is not this
# checkout's source is rebuilt and restarted on its recorded arguments, only while no build uses it.
rf=$temporary_root/refresh
checkout=$rf/checkout
mkdir -p "$checkout/scripts" "$checkout/tools" "$rf/stubs" "$rf/home/Library/LaunchAgents"
cp "$script" "$checkout/scripts/glaeda-fleet-cas"
cp -R "$repo_root/tools/fleet-cas-prototype" "$checkout/tools/"
rm -rf "$checkout/tools/fleet-cas-prototype/target"
proto=$checkout/tools/fleet-cas-prototype
printf '#!/usr/bin/env bash\nexit 0\n' >"$proto/scripts/fleet-cas-settings.sh"
# The bash and Python digests (glaeda-update cas_source_digest) agree, and ignore target/.
eval "$(sed -n '/^source_digest()/,/^}/p' "$script")"
bash_digest=$(source_digest)
py_digest=$(python3 -c '
import importlib.machinery, importlib.util, sys
from pathlib import Path
loader = importlib.machinery.SourceFileLoader("gu", sys.argv[1])
gu = importlib.util.module_from_spec(importlib.util.spec_from_loader("gu", loader)); loader.exec_module(gu)
print(gu.cas_source_digest(Path(sys.argv[2])))' "$repo_root/scripts/glaeda-update" "$proto")
test "$bash_digest" = "$py_digest"
mkdir -p "$proto/target/release"; printf 'build output\n' >"$proto/target/release/x"
test "$(source_digest)" = "$bash_digest"
rm -rf "$proto/target"
printf '%s\n' '#!/usr/bin/env bash' \
  'printf "cargo %s\n" "$*" >>"${FLEET_CAS_TEST_CARGO_LOG:?}"' \
  'mkdir -p "$CARGO_TARGET_DIR/release"' \
  'printf "#!/usr/bin/env bash\necho fleet-cas %s\n" "$(cat "${FLEET_CAS_TEST_BUILD_ID:?}")" >"$CARGO_TARGET_DIR/release/fleet-cas"' \
  'chmod +x "$CARGO_TARGET_DIR/release/fleet-cas"' >"$rf/stubs/cargo"
printf '%s\n' '#!/usr/bin/env bash' \
  '[ -e "${FLEET_CAS_TEST_BUSY:?}" ] && [ "${*: -1}" = "$(cat "$FLEET_CAS_TEST_BUSY")" ]' >"$rf/stubs/pgrep"
printf '%s\n' '#!/usr/bin/env bash' \
  'if [ "$1" = -f ] && [ "$2" = %u ]; then id -u; exit 0; fi' \
  'exec /usr/bin/stat "$@"' >"$rf/stubs/stat"
# launchd: no system daemon here; the user agent is loaded once bootstrapped.
printf '%s\n' '#!/usr/bin/env bash' 'printf "%s\n" "$*" >>"${FLEET_CAS_TEST_LAUNCHCTL_LOG:?}"' \
  '[ "$1" = print ] && case $2 in system/*) exit 113 ;; esac' 'exit 0' >"$rf/stubs/launchctl"
cp "$stub_bin/plutil" "$rf/stubs/"
chmod +x "$rf/stubs/"*
rroot=$rf/xcode
mkdir -p "$rroot/run" "$rroot/bin"
recorded() { printf '%s\n' "$rroot/fleet-cas.sock" "$rroot/node-store" --read-only-kv --upstream http://127.0.0.1:7451; }
recorded >"$rroot/run/node.args"
printf 'old\n' >"$rf/build-id"
refresh_env=(
  FLEET_CAS_ROOT="$rroot" HOME="$rf/home" PATH="$rf/stubs:$PATH" FLEET_CAS_LAUNCH_DAEMONS_DIR="$rf/daemons"
  FLEET_HOST_LOCK="$rf/host.lock" FLEET_CAS_BUILD_PROCS="xcodebuild swift-build"
  FLEET_CAS_TEST_BUSY="$rf/busy" FLEET_CAS_TEST_CARGO_LOG="$rf/cargo.log"
  FLEET_CAS_TEST_BUILD_ID="$rf/build-id" FLEET_CAS_TEST_LAUNCHCTL_LOG="$rf/launchctl.log"
)
refresh() { env "${refresh_env[@]}" "$checkout/scripts/glaeda-fleet-cas" "$@"; }
: >"$rf/host.lock"; : >"$rf/cargo.log"; : >"$rf/launchctl.log"
# No receipt (a node built before it existed): stale. The plan builds nothing.
refresh refresh >"$rf/out"
test "$(tail -1 "$rf/out")" = "fleet-cas refresh: would rebuild -> ${bash_digest:0:12}"
test ! -s "$rf/cargo.log"
# A build is running: deferred, nothing built or restarted.
printf 'xcodebuild\n' >"$rf/busy"
refresh refresh --apply >"$rf/out"
test "$(tail -1 "$rf/out")" = "fleet-cas refresh: deferred (xcodebuild is running)"
test ! -s "$rf/cargo.log"
rm -f "$rf/busy"
# A fleet job holds the host lock: deferred too.
perl -e 'use Fcntl qw(:flock); open(my $l, "+<", $ARGV[0]) or die; flock($l, LOCK_EX) or die; $| = 1; print "held\n"; sleep 30' \
  "$rf/host.lock" >"$rf/held" &
holder=$!
for _ in $(seq 1 50); do grep -q held "$rf/held" 2>/dev/null && break; sleep 0.1; done
refresh refresh --apply >"$rf/out"
test "$(tail -1 "$rf/out")" = "fleet-cas refresh: deferred (a fleet job holds $rf/host.lock)"
kill "$holder"; wait "$holder" 2>/dev/null || true
test ! -s "$rf/cargo.log"
# Idle: builds outside the release tree, installs, records the receipt and writes the service on
# the recorded arguments.
refresh refresh --apply >"$rf/out"
test "$(tail -1 "$rf/out")" = "fleet-cas refresh: refreshed -> ${bash_digest:0:12}"
grep -q '^cargo build --release --locked -q$' "$rf/cargo.log"
test ! -e "$proto/target"
test -x "$rroot/build-target/release/fleet-cas"
test "$("$rroot/bin/fleet-cas")" = "fleet-cas old"
test "$(cat "$rroot/bin/fleet-cas.source")" = "$bash_digest"
cmp "$proto/scripts/fleet-cas-run" "$rroot/bin/fleet-cas-run"
recorded | cmp - "$rroot/run/node.args"
grep -q 'bootstrap gui/.*com.teamleaderleo.glaeda.fleet-cas-node.plist' "$rf/launchctl.log"
# The supervisor's wrapper records the binary it started; with that, the node is current.
shasum -a 256 "$rroot/bin/fleet-cas" | cut -c1-64 >"$rroot/run/node.bin"
: >"$rf/cargo.log"
refresh refresh --apply >"$rf/out"
test "$(tail -1 "$rf/out")" = "fleet-cas refresh: current ${bash_digest:0:12}"
test ! -s "$rf/cargo.log"
# A new node change: rebuilt, and the running node (started by bin/fleet-cas-run) is stopped for
# launchd's KeepAlive to restart on the new binary.
printf '// a merged fix\n' >>"$proto/src/main.rs"
new_digest=$(source_digest)
printf 'new\n' >"$rf/build-id"
bash -c 'exec -a "$0 node" sleep 30' "$rroot/bin/fleet-cas-run" &
node_pid=$!
echo "$node_pid" >"$rroot/run/node.pid"
for _ in $(seq 1 50); do ps -o command= -p "$node_pid" | grep -q "fleet-cas-run node" && break; sleep 0.1; done
refresh refresh --apply >"$rf/out"
test "$(tail -1 "$rf/out")" = "fleet-cas refresh: refreshed ${bash_digest:0:12} -> ${new_digest:0:12}"
case $(ps -o stat= -p "$node_pid" || true) in Z* | "") ;; *) echo "the running node was not stopped" >&2; exit 1 ;; esac
wait "$node_pid" 2>/dev/null || true
test "$("$rroot/bin/fleet-cas")" = "fleet-cas new"
test "$(cat "$rroot/bin/fleet-cas.source")" = "$new_digest"
# A build that starts during the rebuild: the swap under the lock defers it.
printf 'swift-build\n' >"$rf/busy"
rc=0; refresh refresh-swap --apply || rc=$?
test "$rc" = 75
rm -f "$rf/busy"
# An older layout (a service, no recorded arguments) is named, never rebuilt here.
mv "$rroot/run/node.args" "$rf/node.args.kept"
rc=0; refresh refresh --apply >"$rf/out" || rc=$?
test "$rc" = 3
grep -q '^fleet-cas refresh: legacy ' "$rf/out"
rm -f "$rf/home/Library/LaunchAgents/com.teamleaderleo.glaeda.fleet-cas-node.plist"
test "$(refresh refresh --apply)" = "fleet-cas refresh: none (no node on this host)"

echo "glaeda-fleet-cas role-scoped uninstall and bind-wait tests passed"
