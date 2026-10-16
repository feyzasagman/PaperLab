#!/bin/sh
# The E2E stack: its own compose project, volumes and ports, so a test run never touches your library
# (project `paperlab` on 8000/5180/5433). Fake providers run only here.
#   scripts/e2e-stack.sh up [--no-search-model] [--live-providers]   start or recreate it, wait for the proxy
#   scripts/e2e-stack.sh down | reset                                 stop it | stop it and delete its volumes
#   scripts/e2e-stack.sh status | exec <service> <command…>
set -eu
cd "$(dirname "$0")/.."

PROJECT=paperlab-e2e
export API_PORT=8100 FRONTEND_PORT=5280 DB_PORT=5533
export LLM_PROVIDER=fake DISCOVERY_PROVIDER=fake MODELS_DIR=/models
export DEV_RELOAD=1 # the test stack runs the code being worked on; your own stack does not reload
export COMPOSE_FILE=docker-compose.yml:docker-compose.e2e.yml # memory caps, so a run can't starve your stack
LIVE_MODELS=paperlab_models # the downloaded search model, copied once (read-only) so specs needing it run

up() {
  for arg in "$@"; do
    case "$arg" in
      --no-search-model) MODELS_DIR=/models/none ;;
      --live-providers) LLM_PROVIDER=ollama DISCOVERY_PROVIDER=live ;; # acceptance runs with the real local model
      *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
  done
  docker compose -p "$PROJECT" create --quiet-pull api # creates the project's volumes before the copy
  if docker volume inspect "$LIVE_MODELS" >/dev/null 2>&1; then
    docker run --rm -v "$LIVE_MODELS":/from:ro -v "${PROJECT}_models":/to alpine:3.22 \
      sh -c '[ -n "$(ls -A /to)" ] || cp -a /from/. /to/'
  else
    echo "No $LIVE_MODELS volume: the test stack starts without the search model." >&2
  fi
  docker compose -p "$PROJECT" up -d
  i=0
  until curl -sf -o /dev/null "http://localhost:$FRONTEND_PORT/api/llm/models"; do
    i=$((i + 1)); [ "$i" -lt 60 ] || { echo "E2E stack did not answer on :$FRONTEND_PORT" >&2; exit 1; }
    sleep 2
  done
  echo "E2E stack ready on http://localhost:$FRONTEND_PORT"
  echo "Stop it with scripts/e2e-stack.sh down when the run ends."
}

case "${1:-}" in
  up) shift; up "$@" ;;
  down) docker compose -p "$PROJECT" down ;;
  reset) docker compose -p "$PROJECT" down -v ;;
  status) docker compose -p "$PROJECT" ps ;;
  exec) shift; docker compose -p "$PROJECT" exec -T "$@" ;;
  *) sed -n '2,6p' "$0"; exit 2 ;;
esac
