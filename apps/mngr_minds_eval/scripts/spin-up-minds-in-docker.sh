#!/usr/bin/env bash
# Build + boot a headless Minds "box" (the CONTROLLER) for an mngr branch, in Docker.
# ALWAYS builds from the branch's remote TIP (SHA cache-bust -> no stale Docker layers). The box's
# mngr is both what runs Minds and what gets vendored into eval clones. Names the box's single
# Modal environment after the container, so an eval run's sandboxes are findable by that name.
#
#   ./spin-up-minds-in-docker.sh mngr-branch=XYZ container-name=NAME [env=staging]
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$HERE/.." && pwd)"          # apps/mngr_minds_eval (Docker build context)
MNGR_REPO="https://github.com/imbue-ai/mngr.git"
MNGR_BRANCH=""; CONTAINER=""; MINDS_ENV="staging"
for arg in "$@"; do
  case "$arg" in
    mngr-branch=*)    MNGR_BRANCH="${arg#*=}" ;;
    container-name=*) CONTAINER="${arg#*=}" ;;
    env=*)            MINDS_ENV="${arg#*=}" ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done
[ -n "$MNGR_BRANCH" ] && [ -n "$CONTAINER" ] \
  || { echo "usage: ./spin-up-minds-in-docker.sh mngr-branch=XYZ container-name=NAME [env=staging]" >&2; exit 2; }
[ -f "${HOME}/.modal.toml" ] || { echo "need ~/.modal.toml (Modal auth)" >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "Docker daemon not running -- start Docker Desktop" >&2; exit 2; }

# Freshness: resolve the branch's remote-tip SHA so the Docker clone layer busts when it moves.
REF="$(git ls-remote "$MNGR_REPO" "refs/heads/${MNGR_BRANCH}" 2>/dev/null | cut -f1)"
[ -n "$REF" ] || { echo "branch '${MNGR_BRANCH}' not found on ${MNGR_REPO}" >&2; exit 2; }

freeport() { python3 -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1]);s.close()"; }
UI="$(freeport)"; FWD="$(freeport)"
MODAL_ENV="$(printf '%s' "$CONTAINER" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9-' '-')"
TAG="minds-box:${CONTAINER}"

echo ">> building ${TAG} from mngr ${MNGR_BRANCH}@${REF:0:12} (fresh tip; eval app overlaid) ..."
docker build -f "${APP_DIR}/docker/Dockerfile" \
  --build-arg MNGR_BRANCH="${MNGR_BRANCH}" --build-arg MNGR_REF="${REF}" \
  -t "${TAG}" "${APP_DIR}" || { echo "!! build failed" >&2; exit 1; }

docker rm -f "${CONTAINER}" >/dev/null 2>&1
echo ">> starting '${CONTAINER}' (dashboard ${UI}, forward ${FWD}) ..."
docker run -d --name "${CONTAINER}" \
  -p "${UI}:${UI}" -p "${FWD}:${FWD}" \
  -v "${HOME}/.modal.toml:/root/.modal.toml:ro" \
  -e MINDS_BARE_PORT="${UI}" -e MINDS_FORWARD_HOST=0.0.0.0 -e MINDS_FORWARD_PORT="${FWD}" \
  -e MINDS_ENV="${MINDS_ENV}" \
  -e MNGR__PROVIDERS__MODAL__USER_ID="${MODAL_ENV}" \
  "${TAG}" >/dev/null || { echo "!! docker run failed" >&2; exit 1; }

echo ">> waiting for Minds on ${UI} ..."
for _ in $(seq 1 100); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${UI}/" 2>/dev/null)" != "000" ] && break
  docker ps -q -f "name=${CONTAINER}" | grep -q . || { echo "!! container exited early -- docker logs ${CONTAINER}" >&2; exit 1; }
  sleep 3
done
LOGIN_URL=""
for _ in $(seq 1 20); do
  LOGIN_URL="$(docker logs "${CONTAINER}" 2>&1 | grep -oE "http://localhost:${FWD}/login\?one_time_code=[A-Za-z0-9_-]+" | tail -1)"
  [ -n "${LOGIN_URL}" ] && break
  sleep 2
done
echo ""
echo "  dashboard:        http://localhost:${UI}"
echo "  workspace login:  ${LOGIN_URL:-"(not ready; docker logs ${CONTAINER})"}"
echo "  container:        ${CONTAINER}   (stop: docker rm -f ${CONTAINER})"
echo "  modal sandboxes:  search 'minds-staging-${MODAL_ENV}' in the Modal environment dropdown"
