#!/usr/bin/env sh
set -eu
. "$(dirname -- "$0")/common.sh"

$COMPOSE up -d --build
for service in crawlground juice-shop parabank; do
  wait_healthy "$service"
done
echo "All external targets are healthy."
