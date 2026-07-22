#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
COMPOSE="docker compose --project-directory $ROOT -f $ROOT/compose.yml"
WAIT_TIMEOUT=${EXTERNAL_TARGET_TIMEOUT_SECONDS:-180}

wait_healthy() {
  service=$1
  deadline=$(( $(date +%s) + WAIT_TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    container_id=$($COMPOSE ps -q "$service" 2>/dev/null || true)
    if [ -n "$container_id" ]; then
      status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)
      [ "$status" = healthy ] && return 0
      [ "$status" = exited ] && break
    fi
    sleep 2
  done
  $COMPOSE ps "$service" >&2 || true
  $COMPOSE logs --tail 40 "$service" >&2 || true
  echo "$service did not become healthy within ${WAIT_TIMEOUT}s" >&2
  return 1
}
