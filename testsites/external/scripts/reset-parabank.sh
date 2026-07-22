#!/usr/bin/env sh
set -eu
. "$(dirname -- "$0")/common.sh"
$COMPOSE rm -sf parabank >/dev/null
$COMPOSE up -d --no-deps parabank >/dev/null
wait_healthy parabank
curl --fail --silent --show-error --max-time 15 -X POST \
  http://127.0.0.1:18080/parabank/services/bank/initializeDB >/dev/null
echo "ParaBank recreated and initialized from its pinned image."
