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
  *getifaddr*) printf '172.20.21.202\n' ;;
  *"inet 172.20.21.158 "*) printf 'inet 172.20.21.158 netmask 0xffffff00\n' ;;
  *"inet 172."*) exit 1 ;;
  *ifconfig*) printf '100.89.225.106\n' ;;
  *) printf '%s\n' "$*" >>"${ROLLOUT_CALLS:?}" ;;
esac
EOF
cat >"$temporary_root/rsync" <<'EOF'
#!/usr/bin/env bash
printf 'rsync %s\n' "$*" >>"${ROLLOUT_CALLS:?}"
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
  --store-root '/Volumes/Glaeda CAS' \
  --store-image '/Volumes/T5 EVO/glaeda-fleet-cas.sparsebundle' cmux8s >"$temporary_root/allow.out"
test ! -s "$temporary_root/calls"
grep -q 'FLEET_CAS_ROOT=/Volumes/Glaeda\\ CAS' "$temporary_root/allow.out"
grep -q 'FLEET_CAS_IMAGE=/Volumes/T5\\ EVO/glaeda-fleet-cas.sparsebundle' "$temporary_root/allow.out"

PATH="$temporary_root:$PATH" ROLLOUT_CALLS="$temporary_root/calls" \
  "$script" --store cmux-lawrence --allow-coordinator \
  --store-root '/Volumes/Glaeda CAS' \
  --store-image '/Volumes/T5 EVO/glaeda-fleet-cas.sparsebundle' cmux8s --apply >"$temporary_root/apply.out"
grep -q 'FLEET_CAS_ROOT=/Volumes/Glaeda\\ CAS' "$temporary_root/calls"
grep -q 'FLEET_CAS_IMAGE=/Volumes/T5\\ EVO/glaeda-fleet-cas.sparsebundle' "$temporary_root/calls"
grep -q -- '--apply' "$temporary_root/calls"

if PATH="$temporary_root:$PATH" ROLLOUT_CALLS="$temporary_root/calls" \
  "$script" --store cmux-lawrence --allow-coordinator \
  --store-root '/Volumes/Glaeda CAS' --store-image relative.sparsebundle cmux8s \
  >"$temporary_root/relative.out" 2>"$temporary_root/relative.err"; then
  exit 1
fi
grep -q -- '--store-image must be an absolute path' "$temporary_root/relative.err"

if PATH="$temporary_root:$PATH" ROLLOUT_CALLS="$temporary_root/calls" \
  "$script" --store cmux-lawrence --allow-coordinator \
  --store-image '/Volumes/T5 EVO/glaeda-fleet-cas.sparsebundle' cmux8s \
  >"$temporary_root/no-root.out" 2>"$temporary_root/no-root.err"; then
  exit 1
fi
grep -q -- '--store-image requires --store-root' "$temporary_root/no-root.err"

# --store-lan: the store listens on its LAN address; the writer and nodes go through the relay,
# and the store's only writer is the writer's LAN source address toward the store.
PATH="$temporary_root:$PATH" ROLLOUT_CALLS="$temporary_root/calls" \
  "$script" --store cmux-lawrence --allow-coordinator --store-root /Volumes/glaeda-fleet-cas \
  --store-lan 172.20.21.158 --writer cmux15 --sign-key /k --trusted-keys abc cmux15 cmux8s >"$temporary_root/lan.out"
grep -q -- 'store --listen 172.20.21.158:7450 --writers 172.20.21.202 ' "$temporary_root/lan.out"
grep -q -- 'writer --relay 172.20.21.158:7450 --sign-key /k ' "$temporary_root/lan.out"
grep -q -- 'node --relay 172.20.21.158:7450 --trusted-keys abc ' "$temporary_root/lan.out"
! grep -q -- '--store 100\.' "$temporary_root/lan.out"

if PATH="$temporary_root:$PATH" ROLLOUT_CALLS="$temporary_root/calls" \
  "$script" --store cmux-lawrence --allow-coordinator --store-root /Volumes/glaeda-fleet-cas \
  --store-lan 172.20.21.99 cmux8s >"$temporary_root/lan-miss.out" 2>"$temporary_root/lan-miss.err"; then
  exit 1
fi
grep -q 'has no address 172.20.21.99' "$temporary_root/lan-miss.err"

if PATH="$temporary_root:$PATH" ROLLOUT_CALLS="$temporary_root/calls" \
  "$script" --store cmux8s --store-lan 100.89.140.13 cmux9s >"$temporary_root/lan-tail.out" 2>"$temporary_root/lan-tail.err"; then
  exit 1
fi
grep -q 'private (RFC 1918)' "$temporary_root/lan-tail.err"


# --nodes-only: repoint readers at the LAN store through the relay without redeploying the store
# (cmux-lawrence, a coordinator: no --allow-coordinator or --store-root needed when it is only read).
: >"$temporary_root/calls"
PATH="$temporary_root:$PATH" ROLLOUT_CALLS="$temporary_root/calls" \
  "$script" --store cmux-lawrence --store-lan 172.20.21.158 --trusted-keys abc --prewarm cmux@catchup-v1 \
  --nodes-only cmux17 cmux20 >"$temporary_root/nodes.out"
if grep -q '== cmux-lawrence' "$temporary_root/nodes.out"; then exit 1; fi
if grep -q 'glaeda-fleet-cas store' "$temporary_root/nodes.out"; then exit 1; fi
grep -q '== cmux17' "$temporary_root/nodes.out"
grep -q 'glaeda-fleet-cas node --relay 172.20.21.158:7450 --trusted-keys abc --prewarm cmux@catchup-v1' "$temporary_root/nodes.out"
grep -q '== cmux20' "$temporary_root/nodes.out"
if PATH="$temporary_root:$PATH" ROLLOUT_CALLS="$temporary_root/calls" \
  "$script" --store cmux-lawrence --store-lan 172.20.21.158 --writer cmux15 --sign-key /k --trusted-keys abc \
  --nodes-only cmux17 >"$temporary_root/nodes-writer.out" 2>"$temporary_root/nodes-writer.err"; then
  exit 1
fi
grep -q -- '--nodes-only redeploys no store or writer' "$temporary_root/nodes-writer.err"
if PATH="$temporary_root:$PATH" ROLLOUT_CALLS="$temporary_root/calls" \
  "$script" --store cmux-lawrence --store-lan 172.20.21.158 --nodes-only \
  >"$temporary_root/nodes-none.out" 2>"$temporary_root/nodes-none.err"; then
  exit 1
fi
grep -q -- '--nodes-only needs at least one NODE_HOST' "$temporary_root/nodes-none.err"

echo "glaeda-fleet-cas rollout guard tests passed"
