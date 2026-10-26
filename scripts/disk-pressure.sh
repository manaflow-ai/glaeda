#!/bin/bash
# Run one bounded disk-pressure pass for a fleet mini.  The LaunchAgent owns the
# cadence; keeping this as a tiny wrapper gives runner installs the same explicit
# entry point as the fleet bootstrap and makes a one-shot operator pass testable.
set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
  echo "usage: disk-pressure.sh [--once]"
  exit 0
fi
if [[ "${1:-}" != "--once" ]]; then
  echo "usage: disk-pressure.sh --once" >&2
  exit 2
fi

home="${HOME:?HOME is required}"
python="${GLAEDA_PYTHON:-/opt/homebrew/bin/python3}"
disk="${GLAEDA_DISK:-$home/.local/bin/glaeda-disk}"
export GLAEDA_DISK_CALLER="${GLAEDA_DISK_CALLER:-disk-pressure LaunchAgent}"
if [[ -n "${GLAEDA_ROLE:-}" ]]; then
  exec "$python" "$disk" --role "$GLAEDA_ROLE" --pressure --idle --apply --top 0
fi
exec "$python" "$disk" --pressure --idle --apply --top 0
