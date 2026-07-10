#!/usr/bin/env bash
# Create ONE workspace in a running box by slotting args straight into the create endpoint.
# fct-link / fct-branch are passed through VERBATIM (whatever the create endpoint does with them
# -- a local /work/clones/<x> path, a git URL, empty branch, etc.). Reusable for ad-hoc testing.
#
#   ./spin-up-workspace-in-docker-running-minds.sh container-name=NAME [name=WS] \
#       compute-provider=modal ai-provider=api_key anthropic-key=KEY \
#       fct-link=/whatever [fct-branch=""] [backup-provider=configure_later]
set -uo pipefail

CONTAINER=""; NAME=""; COMPUTE="modal"; AI="api_key"; KEY=""; BACKUP="configure_later"; LINK=""; BRANCH=""
for arg in "$@"; do
  case "$arg" in
    container-name=*)   CONTAINER="${arg#*=}" ;;
    name=*)             NAME="${arg#*=}" ;;
    compute-provider=*) COMPUTE="${arg#*=}" ;;
    ai-provider=*)      AI="${arg#*=}" ;;
    anthropic-key=*)    KEY="${arg#*=}" ;;
    backup-provider=*)  BACKUP="${arg#*=}" ;;
    fct-link=*)         LINK="${arg#*=}" ;;
    fct-branch=*)       BRANCH="${arg#*=}" ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done
[ -n "$CONTAINER" ] && [ -n "$LINK" ] \
  || { echo "usage: ... container-name=NAME [name=WS] fct-link=... [compute-provider=modal ai-provider=api_key anthropic-key=KEY fct-branch= backup-provider=configure_later]" >&2; exit 2; }
docker inspect "$CONTAINER" >/dev/null 2>&1 || { echo "no such container: $CONTAINER" >&2; exit 2; }
UI="$(docker exec "$CONTAINER" printenv MINDS_BARE_PORT 2>/dev/null || true)"
[ -n "$UI" ] || { echo "!! couldn't resolve the box's UI port" >&2; exit 1; }

# Enum fields go UPPERCASE on the wire (the create endpoint's LaunchMode/AIProvider/BackupProvider).
UP() { printf '%s' "$1" | tr 'a-z' 'A-Z'; }
BODY="$(python3 - "$LINK" "$BRANCH" "$NAME" "$(UP "$COMPUTE")" "$(UP "$AI")" "$KEY" "$(UP "$BACKUP")" <<'PY'
import json, sys
git_url, branch, name, compute, ai, key, backup = sys.argv[1:8]
body = {"git_url": git_url, "branch": branch, "launch_mode": compute, "ai_provider": ai,
        "anthropic_api_key": key, "backup_provider": backup}
if name:
    body["host_name"] = name
print(json.dumps(body))
PY
)"
TMP="$(mktemp)"; printf '%s' "$BODY" > "$TMP"; trap 'rm -f "$TMP"' EXIT

echo ">> creating workspace ${NAME:-<auto>} from ${LINK}@${BRANCH:-<default>}  (${COMPUTE}/${AI}) ..."
RESP="$(curl -s -X POST "http://localhost:${UI}/api/v1/workspaces" -H 'Content-Type: application/json' -d @"$TMP")"
OP="$(printf '%s' "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin).get('operation_id',''))" 2>/dev/null || true)"
[ -n "$OP" ] || { echo "!! create POST failed: $RESP" >&2; exit 1; }

echo ">> provisioning ..."
AG="-"; LAST=""
for _ in $(seq 1 240); do
  F="$(curl -s "http://localhost:${UI}/api/v1/workspaces/operations/create/${OP}" | python3 -c "
import sys, json
try: d = json.load(sys.stdin)
except Exception: d = {}
print('{}\t{}\t{}\t{}'.format(d.get('is_done'), d.get('error') or '', d.get('agent_id') or '-', d.get('status_text') or d.get('status') or ''))" 2>/dev/null || true)"
  DONE="$(printf '%s' "$F" | cut -f1)"; ERR="$(printf '%s' "$F" | cut -f2)"
  AG="$(printf '%s' "$F" | cut -f3)"; ST="$(printf '%s' "$F" | cut -f4)"
  [ -n "$ST" ] && [ "$ST" != "$LAST" ] && { echo "   … $ST"; LAST="$ST"; }
  [ "$DONE" = "True" ] && { echo "  ✅ ${NAME:-workspace} up (agent ${AG})  ·  dashboard: http://localhost:${UI}"; exit 0; }
  [ -n "$ERR" ] && { echo "!! create failed: $ERR" >&2; exit 1; }
  sleep 4
done
echo "!! timed out waiting for the workspace" >&2; exit 1
