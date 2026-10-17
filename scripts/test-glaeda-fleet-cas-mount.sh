#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
helper=$repo_root/tools/fleet-cas-prototype/scripts/fleet-cas-mount
temporary_root=$(mktemp -d)
trap 'rm -rf "$temporary_root"' EXIT

mount_base="$temporary_root/volumes"
backing="$mount_base/T5-EVO-test"
mount_root="$mount_base/glaeda-fleet-cas"
image="$backing/glaeda-fleet-cas.sparsebundle"
mount_state="$temporary_root/mount-state"
mount_created="$temporary_root/mount-created"
mount_calls="$temporary_root/mount-calls"
stub_bin="$temporary_root/bin"
mkdir -p "$backing" "$image" "$stub_bin"
: >"$mount_calls"

cat >"$stub_bin/diskutil" <<'EOF'
#!/usr/bin/env bash
path=${2:?diskutil info PATH}
if [ "$path" = "${FLEET_CAS_MOUNT_BACKING:?}" ]; then
  [ "${FLEET_CAS_MOUNT_MODE:-}" != backing-fail ] || exit 1
  printf '   Type (Bundle): APFS   \n   Mount Point: %s   \n' "$path"
  exit 0
fi
if [ "$path" = "${FLEET_CAS_MOUNT_ROOT:?}" ] && [ -f "${FLEET_CAS_MOUNT_STATE:?}" ] && [ -f "${FLEET_CAS_MOUNT_CREATED:?}" ]; then
  uuid=E36525CA-ED1B-4141-907E-59213CFC8FC6
  [ "${FLEET_CAS_MOUNT_MODE:-}" = wrong-uuid ] && uuid=wrong
  printf '   Type (Bundle): APFS   \n   Mount Point: %s   \n   Volume UUID: %s   \n' "$path" "$uuid"
  exit 0
fi
exit 1
EOF
cat >"$stub_bin/hdiutil" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FLEET_CAS_MOUNT_CALLS:?}"
[ "${FLEET_CAS_MOUNT_MODE:-}" != hang ] || { trap 'exit 124' TERM; while :; do :; done; }
case "${1:-}" in
  attach)
    mount_point=
    while [ "$#" -gt 0 ]; do
      [ "$1" = -mountpoint ] && mount_point=$2
      shift
    done
    : >"${FLEET_CAS_MOUNT_STATE:?}"
    printf '%s\n' "$mount_point" >"${FLEET_CAS_MOUNT_CREATED:?}"
    ;;
  detach)
    rm -f "${FLEET_CAS_MOUNT_STATE:?}"
    ;;
esac
EOF
cat >"$stub_bin/sleep" <<'EOF'
#!/usr/bin/env bash
:
EOF
chmod +x "$stub_bin/diskutil" "$stub_bin/hdiutil" "$stub_bin/sleep"

mount_env=(
  FLEET_CAS_VOLUMES_ROOT="$mount_base"
  FLEET_CAS_MOUNT_BACKING="$backing"
  FLEET_CAS_MOUNT_ROOT="$mount_root"
  FLEET_CAS_MOUNT_STATE="$mount_state"
  FLEET_CAS_MOUNT_CREATED="$mount_created"
  FLEET_CAS_MOUNT_CALLS="$mount_calls"
  DISKUTIL="$stub_bin/diskutil"
  HDIUTIL="$stub_bin/hdiutil"
  SLEEP="$stub_bin/sleep"
)

# A valid already-mounted root is accepted without invoking hdiutil.
mkdir -p "$mount_root"
: >"$mount_state"
: >"$mount_created"
env "${mount_env[@]}" "$helper" "$mount_root" "$image" E36525CA-ED1B-4141-907E-59213CFC8FC6
test ! -s "$mount_calls"

# Disk Arbitration creates a missing /Volumes mountpoint as part of attach. An
# unprivileged caller cannot mkdir below the real root-owned /Volumes parent,
# so the helper must leave creation to hdiutil. The stub models that attach
# contract without writing the mountpoint itself.
rm -rf "$mount_root" "$mount_state"
rm -f "$mount_created"
chmod 0555 "$mount_base"
: >"$mount_calls"
env "${mount_env[@]}" "$helper" "$mount_root" "$image" E36525CA-ED1B-4141-907E-59213CFC8FC6
chmod 0755 "$mount_base"
grep -q -- "attach -quiet -nobrowse -owners on -mountpoint $mount_root $image" "$mount_calls"
test "$(cat "$mount_created")" = "$mount_root"

# An absent root attaches exactly the configured image with ownership enforced,
# then validates it. DiskImages defaults must not silently produce a noowners mount.
rm -rf "$mount_root" "$mount_state"
: >"$mount_calls"
env "${mount_env[@]}" "$helper" "$mount_root" "$image" E36525CA-ED1B-4141-907E-59213CFC8FC6
grep -q -- "attach -quiet -nobrowse -owners on -mountpoint $mount_root $image" "$mount_calls"

# A wrong UUID is detached and rejected.
rm -rf "$mount_root" "$mount_state"
: >"$mount_calls"
if env "${mount_env[@]}" FLEET_CAS_MOUNT_MODE=wrong-uuid "$helper" \
  "$mount_root" "$image" E36525CA-ED1B-4141-907E-59213CFC8FC6; then
  exit 1
fi
grep -q -- "detach $mount_root" "$mount_calls"

# A missing image backing volume fails before hdiutil is called.
rm -rf "$mount_root" "$mount_state"
: >"$mount_calls"
if env "${mount_env[@]}" FLEET_CAS_MOUNT_MODE=backing-fail "$helper" \
  "$mount_root" "$image" E36525CA-ED1B-4141-907E-59213CFC8FC6; then
  exit 1
fi
test ! -s "$mount_calls"

# A hung hdiutil call is killed by the per-call timeout and cannot consume an
# unbounded launchd retry window.
: >"$mount_calls"
if env "${mount_env[@]}" FLEET_CAS_MOUNT_MODE=hang HDIUTIL_TIMEOUT_SECONDS=1 "$helper" \
  "$mount_root" "$image" E36525CA-ED1B-4141-907E-59213CFC8FC6; then
  exit 1
fi
grep -q -- "attach -quiet -nobrowse -owners on -mountpoint $mount_root $image" "$mount_calls"

echo "glaeda-fleet-cas mount helper end-to-end tests passed"
