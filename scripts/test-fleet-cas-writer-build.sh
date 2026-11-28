#!/bin/bash
# fleet-cas-writer-build.sh's exit codes for a failed build: the build's own
# code when the node answered every call, 5 when the node failed or skipped
# fleet-store calls, restarted or went down during it (the store's red, not the
# commit's; the catch-up fill counts 5 as transient). A fake root: a settings
# helper, a stats.json the build command rewrites, a fake xcodebuild.
set -u
here=$(cd "$(dirname "$0")/.." && pwd)
writer=$here/tools/fleet-cas-prototype/scripts/fleet-cas-writer-build.sh
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail=0
mkdir -p "$tmp/root/bin" "$tmp/root/node-store" "$tmp/path"
printf '#!/bin/sh\necho "Build version 17A1"\n' >"$tmp/path/xcodebuild"
printf '#!/bin/sh\n[ -e "%s/down" ] && exit 1\necho COMPILATION_CACHE_ENABLE_PLUGIN=YES\n' "$tmp" >"$tmp/root/bin/fleet-cas-settings.sh"
chmod +x "$tmp/path/xcodebuild" "$tmp/root/bin/fleet-cas-settings.sh"
printf 'FLEET_CAS_STORE=127.0.0.1:1\nFLEET_CAS_SIGN_KEY=k\n' >"$tmp/root/fleet-cas.env"
stats() {  # instance write_failed up_skipped
  printf '{"instance":%s,"write_failed":%s,"write_refused":0,"up_errors":0,"up_skipped":%s,"kv_get_hit":1,"kv_get_miss":1,"kv_put":1}\n' \
    "$1" "$2" "$3" >"$tmp/root/node-store/stats.json"
}
sha=0123456789abcdef0123456789abcdef01234567
# case NAME WANT BUILD-COMMAND
check() {
  local name=$1 want=$2; shift 2
  stats 7 0 0
  rm -f "$tmp/down"
  PATH="$tmp/path:$PATH" FLEET_CAS_ROOT="$tmp/root" bash "$writer" cmux "$sha" -- bash -c "$*" >/dev/null 2>"$tmp/err"
  local got=$?
  if [ "$got" = "$want" ]; then echo "ok: $name ($got)"; else echo "FAIL: $name: exit $got, want $want"; cat "$tmp/err"; fail=1; fi
}
S=$tmp/root/node-store/stats.json
check "a red build with a healthy node keeps its code" 65 "exit 65"
check "a red build during skipped store writes is the store's" 5 "printf '{\"instance\":7,\"write_failed\":0,\"write_refused\":0,\"up_errors\":0,\"up_skipped\":1310,\"kv_get_hit\":1,\"kv_get_miss\":1,\"kv_put\":1}' >$S; exit 65"
check "a red build during failed writes is the store's" 5 "printf '{\"instance\":7,\"write_failed\":3,\"write_refused\":0,\"up_errors\":0,\"up_skipped\":0,\"kv_get_hit\":1,\"kv_get_miss\":1,\"kv_put\":1}' >$S; exit 65"
check "a red build across a node restart is the store's" 5 "printf '{\"instance\":8,\"write_failed\":0,\"write_refused\":0,\"up_errors\":0,\"up_skipped\":0,\"kv_get_hit\":1,\"kv_get_miss\":1,\"kv_put\":1}' >$S; exit 65"
check "a red build with the node down after it is the store's" 5 "touch $tmp/down; exit 65"
check "a red build that left the stats unreadable is the store's" 5 "rm -f $S; exit 65"
exit "$fail"
