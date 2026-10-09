#!/bin/bash
# Does the fleet store hold COMMIT for this host's Xcode? Checks the signed
# per-commit marker the trusted writer publishes after a complete fill.
#
# usage: fleet-cas-marker.sh REPO COMMIT
# exit 0: held (marker entries printed), 1: not held, 2: unverified or error.
# Pick catch-up mode only on 0 (docs/CMUX_BUILD_SLOT_MODES.md, "When to switch").
# A controller-allowlisted canary may set FLEET_CAS_STORE_OVERRIDE to a validated
# read endpoint. It never changes the trusted-key source or the local node socket.
set -u
# A recipe probes this before passing an override, so older helpers cannot
# silently use the production endpoint for a canary. No host state is read.
if [ "${1:-}" = --capabilities ] && [ "$#" -eq 1 ]; then
  echo fleet-cas-store-override/v1
  exit 0
fi
repo=${1:?usage: fleet-cas-marker.sh REPO COMMIT}
commit=${2:?usage: fleet-cas-marker.sh REPO COMMIT}
ROOT=${FLEET_CAS_ROOT:-/Users/Shared/cmux-build-fleet/xcode}
env_get() { sed -n "s/^$1=//p" "$ROOT/fleet-cas.env" 2>/dev/null | tail -1; }
store=${FLEET_CAS_STORE_OVERRIDE:-$(env_get FLEET_CAS_STORE)}
if [ -n "${FLEET_CAS_STORE_OVERRIDE:-}" ]; then
  endpoint_pattern='^[A-Za-z0-9][A-Za-z0-9._-]*:([0-9]{1,5})$'
  if ! [[ "$store" =~ $endpoint_pattern ]]; then
    echo "invalid FLEET_CAS_STORE_OVERRIDE endpoint (want HOST:PORT)" >&2
    exit 2
  fi
  port=${BASH_REMATCH[1]}
  if (( 10#$port < 1 || 10#$port > 65535 )); then
    echo "invalid FLEET_CAS_STORE_OVERRIDE port" >&2
    exit 2
  fi
fi
keys=$(env_get FLEET_CAS_TRUSTED_KEYS)
[ -n "$store" ] && [ -n "$keys" ] || { echo "$ROOT/fleet-cas.env has no store or trusted keys" >&2; exit 2; }
xcode=$(xcodebuild -version 2>/dev/null | awk '/Build version/ {print $3}')
[ -n "$xcode" ] || { echo "no Xcode build version" >&2; exit 2; }
[ -z "${FLEET_CAS_STORE_OVERRIDE:-}" ] || echo "fleet-cas: read-store=$store" >&2
exec "$ROOT/bin/fleet-cas" marker get "http://$store" "$repo/$commit/$xcode" --trusted-keys "$keys"
