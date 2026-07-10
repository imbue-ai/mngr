#!/usr/bin/env bash
# Prepare eval cases inside a running box: fresh-clone the FCT branch TIP from GitHub per persona,
# vendor the box's mngr into each clone (so the sandbox runs THAT mngr), and slot the persona.
# The FCT clone is a runtime `git clone --branch` (rm + re-clone every run) -> always the tip.
#
#   ./prepare-eval-cases-for-docker-running-minds.sh container-name=NAME fct-branch=ABC \
#       persona-json-path=PATH [trials=1] [--no-vendor]
set -uo pipefail

FCT_REPO="https://github.com/imbue-ai/forever-claude-template.git"
CONTAINER=""; FCT_BRANCH=""; PERSONAS=""; TRIALS="1"; VENDOR="/work/mngr"
for arg in "$@"; do
  case "$arg" in
    container-name=*)    CONTAINER="${arg#*=}" ;;
    fct-branch=*)        FCT_BRANCH="${arg#*=}" ;;
    persona-json-path=*) PERSONAS="${arg#*=}" ;;
    trials=*)            TRIALS="${arg#*=}" ;;
    --no-vendor)         VENDOR="" ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done
[ -n "$CONTAINER" ] && [ -n "$FCT_BRANCH" ] && [ -n "$PERSONAS" ] \
  || { echo "usage: ... container-name=NAME fct-branch=ABC persona-json-path=PATH [trials=1] [--no-vendor]" >&2; exit 2; }
[ -f "$PERSONAS" ] || { echo "no such personas file: $PERSONAS" >&2; exit 2; }
docker inspect "$CONTAINER" >/dev/null 2>&1 || { echo "no such container: $CONTAINER" >&2; exit 2; }

docker cp "$PERSONAS" "$CONTAINER:/work/personas.json"
VENDOR_ARG=""; [ -n "$VENDOR" ] && VENDOR_ARG="--vendor-mngr $VENDOR"
# shellcheck disable=SC2086
docker exec -w /work/mngr "$CONTAINER" \
  uv run --package mngr-minds-eval mngr-minds-eval prepare-test-clones /work/personas.json \
  --fct-repo "$FCT_REPO" --fct-branch "$FCT_BRANCH" -n "$TRIALS" ${VENDOR_ARG}
