#!/usr/bin/env bash
# Drive the packaged minds app via HTTP: authenticate, create two agents,
# delete one. Intended for overnight autonomous verification.
#
# Prereqs: minds.app is already launched and its backend is running.
# Returns non-zero on any failure; prints a clear summary.
set -u -o pipefail

EVENTS_LOG="${HOME}/.minds/logs/minds-events.jsonl"
COOKIES="/tmp/drive-minds-cookies.txt"
GIT_URL="${GIT_URL:-https://github.com/imbue-ai/forever-claude-template}"
GIT_BRANCH="${GIT_BRANCH:-pilot}"

AGENT1="oauth$(date +%H%M%S)"
AGENT2="oauth2$(date +%H%M%S)"
CREATE_TIMEOUT=600   # 10 min per agent — first agent also pays lima download + VM image download
DELETE_TIMEOUT=180

log() { echo "[$(date +%H:%M:%S)] $*" >&2; }
fail() { log "FAIL: $*"; exit 1; }

BASELINE_COUNT="${BASELINE_LOGIN_COUNT:-0}"
log "Waiting for fresh login URL in events log (baseline=$BASELINE_COUNT, up to 120s)"
for i in $(seq 1 120); do
  CURRENT_COUNT=$(grep -c "Login URL" "$EVENTS_LOG" 2>/dev/null || echo 0)
  if (( CURRENT_COUNT > BASELINE_COUNT )); then break; fi
  sleep 1
  [[ $i -eq 120 ]] && fail "no new login URL appeared within 120s (baseline=$BASELINE_COUNT)"
done
LATEST_LOGIN_URL=$(grep -oE 'http://127\.0\.0\.1:[0-9]+/login\?one_time_code=[A-Za-z0-9_-]+' "$EVENTS_LOG" | tail -1)
[[ -n "$LATEST_LOGIN_URL" ]] || fail "could not find login URL in $EVENTS_LOG"
BASE=$(echo "$LATEST_LOGIN_URL" | sed -E 's|^(http://[^/]+).*|\1|')
CODE=$(echo "$LATEST_LOGIN_URL" | sed -E 's|.*one_time_code=||')
log "Base=$BASE"
log "Login code=$CODE"

# Health-check backend is up
for i in $(seq 1 30); do
  if curl -s -o /dev/null -w '%{http_code}' "$BASE/" | grep -q '^[23]'; then break; fi
  sleep 1
  [[ $i -eq 30 ]] && fail "backend at $BASE didn't respond within 30s"
done

log "Authenticating with one-time code"
rm -f "$COOKIES"
STATUS=$(curl -s -c "$COOKIES" -o /tmp/drive-minds-auth.html -w '%{http_code}' "$BASE/authenticate?one_time_code=$CODE")
if [[ "$STATUS" != "307" && "$STATUS" != "200" ]]; then
  log "auth returned $STATUS — code likely consumed. Minting a new one via the event log after a restart..."
  fail "auth failed ($STATUS); see /tmp/drive-minds-auth.html"
fi
grep -q "minds_session" "$COOKIES" || fail "no session cookie set after auth"
log "Auth ok."

api() {
  local method="$1"; shift
  local path="$1"; shift
  curl -s -b "$COOKIES" -X "$method" -H 'Content-Type: application/json' "$BASE$path" "$@"
}

create_agent() {
  local name="$1"
  local body
  body=$(printf '{"agent_name":"%s","git_url":"%s","branch":"%s","launch_mode":"LIMA","include_env_file":false}' "$name" "$GIT_URL" "$GIT_BRANCH")
  log "POST /api/create-agent name=$name"
  local resp
  resp=$(api POST /api/create-agent -d "$body")
  log "  response: $resp"
  local agent_id
  agent_id=$(echo "$resp" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("agent_id",""))' 2>/dev/null)
  [[ -n "$agent_id" ]] || fail "no agent_id in response: $resp"
  echo "$agent_id"
}

poll_status() {
  local agent_id="$1"
  local timeout="$2"
  local end=$((SECONDS + timeout))
  local last_status=""
  while (( SECONDS < end )); do
    local resp
    resp=$(api GET "/api/create-agent/$agent_id/status")
    local status
    status=$(echo "$resp" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("status",""))' 2>/dev/null)
    if [[ "$status" != "$last_status" ]]; then
      log "  [$agent_id] status=$status  ($(( end - SECONDS ))s remaining)"
      last_status="$status"
    fi
    case "$status" in
      DONE) return 0 ;;
      FAILED)
        local err
        err=$(echo "$resp" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("error",""))' 2>/dev/null)
        log "  [$agent_id] FAILED: $err"
        return 1 ;;
    esac
    sleep 5
  done
  log "  [$agent_id] timed out after ${timeout}s"
  return 2
}

delete_agent() {
  local agent_id="$1"
  log "POST /workspace/$agent_id/delete"
  local code
  code=$(curl -s -b "$COOKIES" -o /tmp/drive-minds-delete.json -w '%{http_code}' -X POST --max-time $DELETE_TIMEOUT "$BASE/workspace/$agent_id/delete")
  log "  delete HTTP $code"
  [[ "$code" == "200" ]] || { cat /tmp/drive-minds-delete.json >&2; return 1; }
  grep -qE '"status"[[:space:]]*:[[:space:]]*"DELETED"' /tmp/drive-minds-delete.json || { cat /tmp/drive-minds-delete.json >&2; return 1; }
  return 0
}

# ---- actually do the thing ----

ID1=$(create_agent "$AGENT1") || fail "agent1 create returned no id"
poll_status "$ID1" $CREATE_TIMEOUT || fail "agent1 did not reach DONE"
log "agent1 $AGENT1 ($ID1) DONE"

ID2=$(create_agent "$AGENT2") || fail "agent2 create returned no id"
poll_status "$ID2" $CREATE_TIMEOUT || fail "agent2 did not reach DONE"
log "agent2 $AGENT2 ($ID2) DONE"

delete_agent "$ID1" || fail "delete agent1 failed"
log "agent1 deleted; verifying no longer visible..."
sleep 3
STATUS=$(api GET "/api/create-agent/$ID1/status")
if echo "$STATUS" | grep -q "Unknown agent creation\|404"; then
  log "deletion verified (status endpoint 404s as expected)"
else
  log "WARN: status after delete: $STATUS"
fi

log "SUCCESS: create $AGENT1, create $AGENT2, delete $AGENT1 all worked."
echo "ID1=$ID1"
echo "ID2=$ID2"
