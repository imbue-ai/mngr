#!/usr/bin/env bash
# Mini rig for ad-hoc testing: command 1 (spin up a box for an mngr branch) + command 3 (create
# ONE workspace in it). No personas, no eval harness -- just a live workspace to poke at while
# iterating on mngr / FCT changes.
#
#   ANTHROPIC_API_KEY=sk-ant-... ./quick-test.sh mngr-branch=XYZ container-name=NAME \
#       fct-link=/whatever [fct-branch=""] [name=t1] [compute-provider=modal] [ai-provider=api_key]
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MNGR_BRANCH=""; CONTAINER=""; FCT_LINK=""; FCT_BRANCH=""; WSNAME="t1"
COMPUTE="modal"; AI="api_key"; BACKUP="configure_later"; MINDS_ENV="staging"
for arg in "$@"; do
  case "$arg" in
    mngr-branch=*)      MNGR_BRANCH="${arg#*=}" ;;
    container-name=*)   CONTAINER="${arg#*=}" ;;
    fct-link=*)         FCT_LINK="${arg#*=}" ;;
    fct-branch=*)       FCT_BRANCH="${arg#*=}" ;;
    name=*)             WSNAME="${arg#*=}" ;;
    compute-provider=*) COMPUTE="${arg#*=}" ;;
    ai-provider=*)      AI="${arg#*=}" ;;
    backup-provider=*)  BACKUP="${arg#*=}" ;;
    env=*)              MINDS_ENV="${arg#*=}" ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done
[ -n "$MNGR_BRANCH" ] && [ -n "$CONTAINER" ] && [ -n "$FCT_LINK" ] \
  || { echo "usage: ANTHROPIC_API_KEY=... ./quick-test.sh mngr-branch=XYZ container-name=NAME fct-link=... [fct-branch=] [name=t1]" >&2; exit 2; }
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY}"

# command 1 -- spin up the box
"$HERE/spin-up-minds-in-docker.sh" "mngr-branch=$MNGR_BRANCH" "container-name=$CONTAINER" "env=$MINDS_ENV" || exit 1

# command 3 -- create one workspace in it
echo ""
"$HERE/spin-up-workspace-in-docker-running-minds.sh" \
  "container-name=$CONTAINER" "name=$WSNAME" \
  "compute-provider=$COMPUTE" "ai-provider=$AI" "anthropic-key=$ANTHROPIC_API_KEY" \
  "backup-provider=$BACKUP" "fct-link=$FCT_LINK" "fct-branch=$FCT_BRANCH"
