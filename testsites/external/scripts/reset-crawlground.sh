#!/usr/bin/env sh
set -eu
. "$(dirname -- "$0")/common.sh"
wait_healthy crawlground
curl --fail --silent --show-error --max-time 10 \
  --data 'confirm=RESET' http://127.0.0.1:13456/reset >/dev/null
result=$(curl --fail --silent --show-error --max-time 10 http://127.0.0.1:13456/results.json)
python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["summary"]["scored"] == 0, d["summary"]' <<EOF
$result
EOF
echo "CrawlGround scores reset."
