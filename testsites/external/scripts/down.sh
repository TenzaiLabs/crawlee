#!/usr/bin/env sh
set -eu
. "$(dirname -- "$0")/common.sh"
$COMPOSE down --remove-orphans "$@"
