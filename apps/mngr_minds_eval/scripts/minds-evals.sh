#!/usr/bin/env bash
# Host entrypoint for the minds-evals CLI: runs it inside a minds-box (the create API, clone paths
# and mngr all live there). Any subcommand/args are passed straight through.
#
#   ./minds-evals.sh <container> launch --name web1 --personas ../sample-personas.json --turns 4
#   ./minds-evals.sh <container> list-batches
#   ./minds-evals.sh <container> inspect web1_20260713-101500
#   ./minds-evals.sh <container> restore web1_20260713-101500 --case todo-app --message 2
#
# launch needs ANTHROPIC_API_KEY in the environment; all subcommands need ~/.minds-eval/aws.env
# (mounted into the box by spin-up-minds-in-docker.sh).
set -uo pipefail

CONTAINER="${1:?usage: ./minds-evals.sh <container> <subcommand> [args...]}"
shift
[ "$#" -gt 0 ] || { echo "usage: ./minds-evals.sh <container> <subcommand> [args...]" >&2; exit 2; }

docker inspect "$CONTAINER" >/dev/null 2>&1 || { echo "no such container: $CONTAINER" >&2; exit 2; }
docker exec "$CONTAINER" test -f /root/.minds-eval/aws.env \
  || { echo "box has no /root/.minds-eval/aws.env -- create ~/.minds-eval/aws.env and re-spin the box" >&2; exit 3; }

# A personas file on the host needs to be visible inside the box: copy it in and rewrite the arg.
ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --personas)
      SRC="${2:?--personas needs a path}"
      [ -f "$SRC" ] || { echo "no such personas file: $SRC" >&2; exit 2; }
      docker cp "$SRC" "$CONTAINER:/work/personas.json" >/dev/null
      ARGS+=(--personas /work/personas.json); shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

TTY_FLAG=""; [ -t 1 ] && TTY_FLAG="-t"
docker exec ${TTY_FLAG} -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" -w /work/mngr "$CONTAINER" \
  uv run --package mngr-minds-eval minds-evals "${ARGS[@]}"
