#!/usr/bin/env sh
set -eu
. "$(dirname -- "$0")/common.sh"

targets=${*:-"crawlground juice-shop parabank"}
for service in $targets; do
  case "$service" in
    crawlground|juice-shop|parabank) wait_healthy "$service" ;;
    *) echo "unknown target: $service" >&2; exit 2 ;;
  esac
done
