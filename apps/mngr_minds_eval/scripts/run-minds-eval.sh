#!/usr/bin/env bash
# Full eval run, composing the scripts above:
#   1) spin up the box for <mngr-branch>          (container-name = <name>)
#   2) prepare eval cases from <fct-branch> x personas   (vendors the box's mngr)
#   3) launch all workspaces (prime the shared Modal env, then parallel)  as EVAL-<name>-CASE-*
#
#   ANTHROPIC_API_KEY=sk-ant-... ./run-minds-eval.sh name=ABC mngr-branch=XYZ fct-branch=ABC \
#       persona-json-path=PATH [trials=1] [env=staging]
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NAME=""; MNGR_BRANCH=""; FCT_BRANCH=""; PERSONAS=""; TRIALS="1"; MINDS_ENV="staging"
for arg in "$@"; do
  case "$arg" in
    name=*)              NAME="${arg#*=}" ;;
    mngr-branch=*)       MNGR_BRANCH="${arg#*=}" ;;
    fct-branch=*)        FCT_BRANCH="${arg#*=}" ;;
    persona-json-path=*) PERSONAS="${arg#*=}" ;;
    trials=*)            TRIALS="${arg#*=}" ;;
    env=*)               MINDS_ENV="${arg#*=}" ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done
[ -n "$NAME" ] && [ -n "$MNGR_BRANCH" ] && [ -n "$FCT_BRANCH" ] && [ -n "$PERSONAS" ] \
  || { echo "usage: ANTHROPIC_API_KEY=... ./run-minds-eval.sh name=ABC mngr-branch=XYZ fct-branch=ABC persona-json-path=PATH [trials=1]" >&2; exit 2; }
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY}"
[ -f "$PERSONAS" ] || { echo "no such personas file: $PERSONAS" >&2; exit 2; }

echo "### 1/3  box: mngr ${MNGR_BRANCH}  ->  container ${NAME}"
"$HERE/spin-up-minds-in-docker.sh" "mngr-branch=$MNGR_BRANCH" "container-name=$NAME" "env=$MINDS_ENV" || exit 1

echo ""; echo "### 2/3  prepare cases: FCT ${FCT_BRANCH} x personas (vendoring the box's mngr)"
"$HERE/prepare-eval-cases-for-docker-running-minds.sh" \
  "container-name=$NAME" "fct-branch=$FCT_BRANCH" "persona-json-path=$PERSONAS" "trials=$TRIALS" || exit 1

echo ""; echo "### 3/3  launch workspaces (prime the shared Modal env, then parallel) as EVAL-${NAME}-CASE-*"
TTY=""; [ -t 1 ] && TTY="-t"
docker exec ${TTY} -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" -w /work/mngr "$NAME" \
  uv run --package mngr-minds-eval mngr-minds-eval launch-workspaces --eval-set "$NAME" || exit 1

echo ""
echo ">> eval '${NAME}' launched. Poll / collect results with:"
echo "     $HERE/retrieve-minds-eval-results.sh name=${NAME}"
