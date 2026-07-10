#!/usr/bin/env bash
# Reach into the box named <name>, walk each EVAL-<name>-CASE-* workspace, report its status
# (unreachable / no_state / ongoing+turns / finished), pull finished Claude transcripts, and copy
# everything back to a host dir. Safe to run repeatedly while an eval is in progress (idempotent).
#
#   ./retrieve-minds-eval-results.sh name=ABC [out-dir=./results-ABC]
set -uo pipefail

NAME=""; OUT=""
for arg in "$@"; do
  case "$arg" in
    name=*)    NAME="${arg#*=}" ;;
    out-dir=*) OUT="${arg#*=}" ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done
[ -n "$NAME" ] || { echo "usage: ./retrieve-minds-eval-results.sh name=ABC [out-dir=./results-ABC]" >&2; exit 2; }
OUT="${OUT:-./results-${NAME}}"
docker inspect "$NAME" >/dev/null 2>&1 || { echo "no such container: $NAME" >&2; exit 2; }

CONTAINER_OUT="/work/results/${NAME}"
docker exec -e EVAL_SET="$NAME" -e CONTAINER_OUT="$CONTAINER_OUT" -w /work/mngr "$NAME" bash -lc '
  eval "$(uv run minds env activate "${MINDS_ENV:-staging}" 2>/dev/null)"
  uv run --package mngr-minds-eval mngr-minds-eval retrieve-test-results --eval-set "$EVAL_SET" -o "$CONTAINER_OUT"
' || { echo "retrieve failed" >&2; exit 1; }

mkdir -p "$OUT"
docker cp "$NAME:${CONTAINER_OUT}/." "$OUT/" && echo ">> results -> $OUT"
