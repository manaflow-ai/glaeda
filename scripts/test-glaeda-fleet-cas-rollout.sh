#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
script=$repo_root/scripts/glaeda-fleet-cas-rollout
temporary_root=$(mktemp -d)
trap 'rm -rf "$temporary_root"' EXIT

cat >"$temporary_root/ssh" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "-G" ]; then
  printf 'hostname %s\n' "${2:?host}"
  exit 0
fi
case "$*" in
  *ifconfig*) printf '    inet 100.89.225.106\n' ;;
  *) printf '%s\n' "$*" >>"${ROLLOUT_CALLS:?}" ;;
esac
EOF
cat >"$temporary_root/rsync" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$temporary_root/ssh" "$temporary_root/rsync"

if PATH="$temporary_root:$PATH" ROLLOUT_CALLS="$temporary_root/calls" \
  "$script" --store cmux-lawrence cmux8s >"$temporary_root/refuse.out" 2>"$temporary_root/refuse.err"; then
  exit 1
fi
grep -q -- '--allow-coordinator' "$temporary_root/refuse.err"

if PATH="$temporary_root:$PATH" ROLLOUT_CALLS="$temporary_root/calls" \
  "$script" --store cmux8s --writer cmux-lawrence --sign-key /tmp/key \
  --trusted-keys abc --allow-coordinator >"$temporary_root/writer.out" 2>"$temporary_root/writer.err"; then
  exit 1
fi
grep -q 'never its writer or a reader node' "$temporary_root/writer.err"

if PATH="$temporary_root:$PATH" ROLLOUT_CALLS="$temporary_root/calls" \
  "$script" --store cmux8s --allow-coordinator 100.89.225.106 >"$temporary_root/node.out" 2>"$temporary_root/node.err"; then
  exit 1
fi
grep -q 'never its writer or a reader node' "$temporary_root/node.err"

: >"$temporary_root/calls"
PATH="$temporary_root:$PATH" ROLLOUT_CALLS="$temporary_root/calls" \
  "$script" --store cmux-lawrence --allow-coordinator \
  --store-root '/Volumes/Glaeda CAS/store' cmux8s >"$temporary_root/allow.out"
grep -q 'FLEET_CAS_ROOT=/Volumes/Glaeda\\ CAS/store' "$temporary_root/calls"

echo "glaeda-fleet-cas rollout guard tests passed"
