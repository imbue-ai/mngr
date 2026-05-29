#!/usr/bin/env bash
# End-to-end verify: authenticate to a running minds.app backend, create a
# LIMA agent using ANTHROPIC_API_KEY, wait for it to reach DONE, send one
# message, wait for the agent's reply, then destroy the agent.
#
# Requires:
#   - /Applications/minds.app already running, with ~/.minds/logs/minds-events.jsonl
#     containing a fresh "Login URL" event (use launch-and-verify.sh first).
#   - ANTHROPIC_API_KEY in env.
set -uo pipefail

EVENTS_LOG="$HOME/.minds/logs/minds-events.jsonl"
COOKIES="/tmp/first-message-cookies.txt"
GIT_URL="${GIT_URL:-https://github.com/imbue-ai/forever-claude-template}"
GIT_BRANCH="${GIT_BRANCH:-pilot}"
HOST_NAME="${HOST_NAME:-firstmsg$(date +%H%M%S)}"
PROMPT="${PROMPT:-Reply with exactly the four characters: pong}"
EXPECT_SUBSTRING="${EXPECT_SUBSTRING:-pong}"
CREATE_TIMEOUT_SECONDS=900
REPLY_TIMEOUT_SECONDS=180
DESTROY_TIMEOUT_SECONDS=180

MNGR_BIN="$HOME/.minds/.venv/bin/mngr"
export MNGR_HOST_DIR="$HOME/.minds/mngr"
export MNGR_PREFIX="minds-"

log() { printf '[first-msg] %s\n' "$*" >&2; }
fail() { log "FAIL: $*"; exit 1; }

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  fail "ANTHROPIC_API_KEY is empty"
fi
if [[ ! -s "$EVENTS_LOG" ]]; then
  fail "$EVENTS_LOG not found or empty -- launch the app first"
fi
if [[ ! -x "$MNGR_BIN" ]]; then
  fail "bundled mngr binary missing at $MNGR_BIN"
fi

log "waiting for login URL in events log (up to 120s)"
LOGIN_URL=""
url_deadline=$((SECONDS + 120))
while (( SECONDS < url_deadline )); do
  LOGIN_URL=$(grep -oE 'http://127\.0\.0\.1:[0-9]+/login\?one_time_code=[A-Za-z0-9_-]+' "$EVENTS_LOG" 2>/dev/null | tail -1)
  [[ -n "$LOGIN_URL" ]] && break
  sleep 2
done
[[ -n "$LOGIN_URL" ]] || fail "no login URL in $EVENTS_LOG after 120s"
BASE=$(echo "$LOGIN_URL" | sed -E 's|^(http://[^/]+).*|\1|')
CODE=$(echo "$LOGIN_URL" | sed -E 's|.*one_time_code=||')
log "base=$BASE"

log "authenticating"
rm -f "$COOKIES"
STATUS=$(curl -s -c "$COOKIES" -o /tmp/first-message-auth.html -w '%{http_code}' "$BASE/authenticate?one_time_code=$CODE")
case "$STATUS" in
  200|307) ;;
  *) fail "auth returned HTTP $STATUS";;
esac
grep -q "minds_session" "$COOKIES" || fail "no session cookie after auth"
log "auth ok"

api() { curl -s -b "$COOKIES" -X "$1" -H 'Content-Type: application/json' "$BASE$2" "${@:3}"; }

log "POST /api/create-agent host_name=$HOST_NAME launch_mode=LIMA ai_provider=API_KEY"
BODY=$(python3 -c '
import json, os, sys
print(json.dumps({
    "agent_name": os.environ["HOST_NAME"],
    "host_name": os.environ["HOST_NAME"],
    "git_url": os.environ["GIT_URL"],
    "branch": os.environ["GIT_BRANCH"],
    "launch_mode": "LIMA",
    "ai_provider": "API_KEY",
    "anthropic_api_key": os.environ["ANTHROPIC_API_KEY"],
    "include_env_file": False,
}))
' HOST_NAME="$HOST_NAME" GIT_URL="$GIT_URL" GIT_BRANCH="$GIT_BRANCH" ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY")

CREATE_RESP=$(api POST /api/create-agent -d "$BODY")
AGENT_ID=$(echo "$CREATE_RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("agent_id",""))')
[[ -n "$AGENT_ID" ]] || fail "no agent_id in create response: $CREATE_RESP"
log "creation_id=$AGENT_ID"

log "polling status (timeout ${CREATE_TIMEOUT_SECONDS}s)"
deadline=$((SECONDS + CREATE_TIMEOUT_SECONDS))
last_status=""
while (( SECONDS < deadline )); do
  resp=$(api GET "/api/create-agent/$AGENT_ID/status")
  status=$(echo "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || echo "")
  if [[ "$status" != "$last_status" ]]; then
    log "  status=$status  ($((deadline - SECONDS))s remaining)"
    last_status="$status"
  fi
  case "$status" in
    DONE) break;;
    FAILED)
      err=$(echo "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error",""))' 2>/dev/null || echo "")
      fail "creation FAILED: $err  (full response: $resp)";;
  esac
  sleep 5
done
[[ "$last_status" == "DONE" ]] || fail "agent did not reach DONE in ${CREATE_TIMEOUT_SECONDS}s (last=$last_status)"
log "agent DONE"

# After DONE, the chat agent name == host_name (per minds README:
# "user's actual chat agent is a separate mngr agent ... named after the host").
AGENT_NAME="$HOST_NAME"

log "discovering agent via bundled mngr (5s settle)"
sleep 5
"$MNGR_BIN" list --format json 2>/dev/null | head -20 | tee /tmp/first-message-mngr-list.json >&2 || true

EVENTS_DIR_BEFORE=$(mktemp)
"$MNGR_BIN" event "$AGENT_NAME" --tail 1 --format json > "$EVENTS_DIR_BEFORE" 2>/dev/null || true

log "sending message: $PROMPT"
"$MNGR_BIN" message "$AGENT_NAME" -m "$PROMPT" || fail "mngr message failed"
SEND_AT=$(date +%s)

log "waiting for assistant reply (timeout ${REPLY_TIMEOUT_SECONDS}s)"
REPLY_FILE="/tmp/first-message-reply.txt"
rm -f "$REPLY_FILE"
reply_deadline=$((SECONDS + REPLY_TIMEOUT_SECONDS))
while (( SECONDS < reply_deadline )); do
  "$MNGR_BIN" event "$AGENT_NAME" --include 'event.type == "assistant_message"' --format json 2>/dev/null \
    | python3 -c "
import json, sys, os
expect = os.environ['EXPECT_SUBSTRING']
send_at = int(os.environ['SEND_AT'])
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        evt = json.loads(line)
    except Exception:
        continue
    ts = evt.get('timestamp') or evt.get('time') or ''
    text = json.dumps(evt)
    # any assistant_message that landed after our send and contains the expected text
    if expect.lower() in text.lower():
        print(text)
        sys.exit(0)
sys.exit(2)
" EXPECT_SUBSTRING="$EXPECT_SUBSTRING" SEND_AT="$SEND_AT" > "$REPLY_FILE" 2>/dev/null
  if [[ -s "$REPLY_FILE" ]]; then
    log "assistant replied:"
    head -1 "$REPLY_FILE" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print("  ", str(d)[:500])' >&2 || true
    break
  fi
  sleep 3
done
[[ -s "$REPLY_FILE" ]] || fail "no assistant reply matching '$EXPECT_SUBSTRING' in ${REPLY_TIMEOUT_SECONDS}s"

log "destroying agent"
DEL=$(curl -s -b "$COOKIES" -o /tmp/first-message-delete.json -w '%{http_code}' \
  -X POST --max-time $DESTROY_TIMEOUT_SECONDS "$BASE/workspace/$AGENT_ID/delete")
log "delete HTTP $DEL"

log "SUCCESS"
