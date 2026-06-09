#!/usr/bin/env bash
# End-to-end verify: authenticate to a running minds.app backend, create a
# LIMA agent using ANTHROPIC_API_KEY, wait for it to reach DONE, send one
# message, wait for the agent's reply, then destroy the agent.
#
# Requires:
#   - /Applications/minds.app already running, with ~/.minds/logs/minds-events.jsonl
#     containing a fresh "Login URL" event (use launch-and-verify.sh first).
#   - ANTHROPIC_API_KEY in env.
#
# FIXME: convert the script so that `-e` can be used.
set -uo pipefail

EVENTS_LOG="$HOME/.minds/logs/minds-events.jsonl"
COOKIES="/tmp/first-message-cookies.txt"
# `${VAR:-default}` substitutes default for both unset and empty string,
# so a workflow input that defaulted to '' still falls through to these.
GIT_URL="${GIT_URL:-https://github.com/imbue-ai/forever-claude-template}"
GIT_BRANCH="${GIT_BRANCH:-pilot_2}"
HOST_NAME="${HOST_NAME:-firstmsg$(date +%H%M%S)}"
PROMPT="${PROMPT:-Reply with exactly the four characters: pong}"
EXPECT_SUBSTRING="${EXPECT_SUBSTRING:-pong}"
CREATE_TIMEOUT_SECONDS=900
REPLY_TIMEOUT_SECONDS=180
DESTROY_TIMEOUT_SECONDS=180

MNGR_BIN="$HOME/.minds/.venv/bin/mngr"
LIMA_BIN_DIR="/Applications/minds.app/Contents/Resources/lima/bin"
export MNGR_HOST_DIR="$HOME/.minds/mngr"
export MNGR_PREFIX="minds-"
export PATH="$LIMA_BIN_DIR:$PATH"

log() { printf '[first-msg] %s\n' "$*" >&2; }
fail() { log "FAIL: $*"; exit 1; }

log "template repo: $GIT_URL @ $GIT_BRANCH"
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  fail "ANTHROPIC_API_KEY is empty"
fi
if [[ ! -s "$EVENTS_LOG" ]]; then
  fail "$EVENTS_LOG not found or empty -- launch the app first"
fi
if [[ ! -x "$MNGR_BIN" ]]; then
  fail "bundled mngr binary missing at $MNGR_BIN"
fi

# The login URL emitted on stdout is auto-consumed by the Electron frontend on
# startup; by the time we curl /authenticate the code is USED -> 403. Mint our
# own VALID code by appending to ~/.minds/auth/one_time_codes.json (read by the
# auth store on every /authenticate call), then drive auth with that.
log "waiting for login URL in events log (up to 120s)"
LOGIN_URL=""
url_deadline=$((SECONDS + 120))
while (( SECONDS < url_deadline )); do
  LOGIN_URL=$(grep -oE 'http://(127\.0\.0\.1|localhost):[0-9]+/login\?one_time_code=[A-Za-z0-9_-]+' "$EVENTS_LOG" 2>/dev/null | tail -1)
  [[ -n "$LOGIN_URL" ]] && break
  sleep 2
done
[[ -n "$LOGIN_URL" ]] || fail "no login URL in $EVENTS_LOG after 120s"
# Normalize host: backend prints localhost in the URL but we need 127.0.0.1
# for curl on macOS where localhost may resolve to ::1 and the server only
# binds 127.0.0.1.
BASE=$(echo "$LOGIN_URL" | sed -E 's|^http://localhost|http://127.0.0.1|; s|^(http://[^/]+).*|\1|')
log "base=$BASE"

log "waiting for HTTP server to actually bind (up to 60s)"
http_deadline=$((SECONDS + 60))
while (( SECONDS < http_deadline )); do
  if curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$BASE/" | grep -qE '^[123]'; then
    break
  fi
  sleep 2
done

log "minting fresh one-time code"
CODES_PATH="$HOME/.minds/auth/one_time_codes.json"
CODE=$(CODES_PATH="$CODES_PATH" python3 -c '
import json, os, secrets, pathlib
p = pathlib.Path(os.environ["CODES_PATH"])
existing = json.loads(p.read_text()) if p.exists() else []
new_code = secrets.token_urlsafe(32)
existing.append({"code": new_code, "status": "VALID"})
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(existing, indent=2))
print(new_code)
') || fail "mint code python3 failed"
[[ -n "$CODE" ]] || fail "mint code produced empty output"

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
BODY=$(HOST_NAME="$HOST_NAME" GIT_URL="$GIT_URL" GIT_BRANCH="$GIT_BRANCH" ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" python3 -c '
import json, os
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
') || fail "build create body failed"

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

log "settling 5s then listing what mngr sees (lima provider only)"
sleep 5
"$MNGR_BIN" list --provider lima 2>&1 | tee /tmp/first-message-mngr-list.txt >&2 || true

log "sending message to '$AGENT_NAME': $PROMPT"
"$MNGR_BIN" message --provider lima "$AGENT_NAME" -m "$PROMPT" 2>&1 | tee /tmp/first-message-mngr-message.txt >&2
mngr_message_rc=${PIPESTATUS[0]}
if [[ $mngr_message_rc -ne 0 ]]; then
  fail "mngr message to '$AGENT_NAME' failed (exit=$mngr_message_rc) -- see /tmp/first-message-mngr-message.txt"
fi
SEND_AT=$(date +%s)

log "waiting for assistant reply (timeout ${REPLY_TIMEOUT_SECONDS}s)"
REPLY_FILE="/tmp/first-message-reply.txt"
rm -f "$REPLY_FILE"
reply_deadline=$((SECONDS + REPLY_TIMEOUT_SECONDS))
while (( SECONDS < reply_deadline )); do
  EXPECT_SUBSTRING="$EXPECT_SUBSTRING" SEND_AT="$SEND_AT" \
    "$MNGR_BIN" event --provider lima "$AGENT_NAME" --include 'event.type == "assistant_message"' --format json 2>/dev/null \
    | EXPECT_SUBSTRING="$EXPECT_SUBSTRING" SEND_AT="$SEND_AT" python3 -c "
import json, sys, os, datetime
expect = os.environ['EXPECT_SUBSTRING']
send_at = int(os.environ['SEND_AT'])
clock_skew_slack_seconds = 5
def event_epoch(evt):
    for field in ('at', 'timestamp', 'created_at'):
        v = evt.get(field)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return datetime.datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp()
            except ValueError:
                continue
    return None
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        evt = json.loads(line)
    except Exception:
        continue
    ts = event_epoch(evt)
    if ts is not None and ts < send_at - clock_skew_slack_seconds:
        continue
    text = json.dumps(evt)
    if expect.lower() in text.lower():
        print(text)
        sys.exit(0)
sys.exit(2)
" > "$REPLY_FILE" 2>/dev/null
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
case "$DEL" in
  2*|404) ;;
  *) fail "delete returned HTTP '$DEL' -- agent may be dangling; body at /tmp/first-message-delete.json";;
esac

log "SUCCESS"
