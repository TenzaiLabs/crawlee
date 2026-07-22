#!/usr/bin/env sh
set -eu
. "$(dirname -- "$0")/common.sh"
$COMPOSE rm -sf juice-shop >/dev/null
$COMPOSE up -d --no-deps juice-shop >/dev/null
wait_healthy juice-shop
echo "Juice Shop recreated from its pinned image."
