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
# `${VAR:-default}` substitutes default for both unset and empty string,
# so a workflow input that defaulted to '' still falls through to these.
GIT_URL="${GIT_URL:-https://github.com/imbue-ai/forever-claude-template}"
GIT_BRANCH="${GIT_BRANCH:-pilot}"
HOST_NAME="${HOST_NAME:-firstmsg$(date +%H%M%S)}"
PROMPT="${PROMPT:-Reply with exactly the four characters: pong}"
EXPECT_SUBSTRING="${EXPECT_SUBSTRING:-pong}"
CREATE_TIMEOUT_SECONDS=900
# Cold-start claude (first launch on a fresh CI runner) can take several
# minutes -- the agent has to settle into the TUI, then claude has to
# start, auth, and process the prompt. 480s leaves headroom over the
# ~3-5 min observed locally on cold caches.
REPLY_TIMEOUT_SECONDS="${REPLY_TIMEOUT_SECONDS:-480}"
DESTROY_TIMEOUT_SECONDS=180
# When SKIP_DESTROY=1, leave the agent running so a follow-on step (slack
# flow, Playwright drive) can reuse it. The follow-on owns teardown.
SKIP_DESTROY="${SKIP_DESTROY:-0}"
AGENT_INFO_PATH="${AGENT_INFO_PATH:-/tmp/first-message-agent-info.json}"

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
  # The events log contains MULTIPLE login URLs: mngr forward emits its
  # own on port 8421 ("Login URL (one-time use)") and minds.app's
  # backend emits the real one on a random high port ("Minds login URL
  # (one-time use)"). Only the backend's accepts /authenticate; the
  # forward one returns 403. Match the backend message specifically.
  LOGIN_URL=$(grep -oE 'Minds login URL \(one-time use\): http://(127\.0\.0\.1|localhost):[0-9]+/login\?one_time_code=[A-Za-z0-9_-]+' "$EVENTS_LOG" 2>/dev/null \
    | grep -oE 'http://[^[:space:]]+' \
    | tail -1)
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

# After DONE, find the agent mngr actually placed on this host. The
# minds README claims "chat agent name == host_name", but on FCT pilot
# the only post-bootstrap agent is the workspace's `system-services`
# agent named per the template (not the host). Poll mngr list for up
# to 60s -- the bootstrap that spawns the chat agent runs inside the
# VM after host READY and can lag the create-flow's DONE event.
log "settling then polling mngr list for an agent on host=$HOST_NAME (up to 60s)"
agent_deadline=$((SECONDS + 60))
AGENT_NAME=""
while (( SECONDS < agent_deadline )); do
  "$MNGR_BIN" list --provider lima 2>&1 | tee /tmp/first-message-mngr-list.txt >&2 || true
  # Capture stdout AND stderr so warnings (e.g. RUNNING_UNKNOWN_AGENT_TYPE
  # noise that mngr can prepend) don't silently break json parsing. The parser
  # finds the first '{' or '[' in the raw output and parses from there; any
  # WARNING: prefix lines are stripped naturally.
  AGENT_NAME=$(HOST_NAME="$HOST_NAME" "$MNGR_BIN" list --provider lima --format json 2>&1 \
    | HOST_NAME="$HOST_NAME" python3 -c '
import json, os, sys
host = os.environ["HOST_NAME"]
raw = sys.stdin.read()
# Skip any non-JSON prefix (mngr warnings, log lines, ANSI codes).
start = next((i for i, ch in enumerate(raw) if ch in "{["), -1)
if start < 0:
    print(f"  (no JSON payload in mngr list output, first 200 chars: {raw[:200]!r})", file=sys.stderr)
    sys.exit(0)
try:
    data = json.loads(raw[start:])
except Exception as exc:
    print(f"  (json parse failed: {exc}; payload[:200]={raw[start:start+200]!r})", file=sys.stderr)
    sys.exit(0)
agents = data.get("agents", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
# Prefer the chat agent (name == host) over the workspace `system-services`
# agent (which is RUNNING_UNKNOWN_AGENT_TYPE under the pilot FCT and
# mngr message would route through BaseAgent rather than the TUI agent).
chat = next((a for a in agents if a.get("name") == host), None)
if chat is None:
    chat = next(
        (a for a in agents
         if (a.get("host") or {}).get("name") == host
         or a.get("host_name") == host),
        None,
    )
if chat is not None:
    print(chat.get("name", ""))
    sys.exit(0)
print(f"  (no agent on host {host!r}; {len(agents)} agents seen)", file=sys.stderr)
' 2>>/tmp/first-message-mngr-list.txt)
  [[ -n "$AGENT_NAME" ]] && break
  sleep 3
done
[[ -n "$AGENT_NAME" ]] || fail "no mngr agent on host $HOST_NAME after 60s"
log "resolved agent name: $AGENT_NAME"

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
# Capture the agent's tmux pane inside the lima VM and grep for the
# expected substring. The chat agent's tmux session name is
# `${MNGR_PREFIX}${AGENT_NAME}`. capture-pane -p prints the pane text
# to stdout; -S -500 grabs the last 500 lines of scrollback so we can
# see the claude reply even if the screen has scrolled.
VM_NAME="minds-${HOST_NAME}"
TMUX_SESSION="minds-${AGENT_NAME}"
reply_deadline=$((SECONDS + REPLY_TIMEOUT_SECONDS))
while (( SECONDS < reply_deadline )); do
  pane=$(limactl shell "$VM_NAME" -- tmux capture-pane -t "$TMUX_SESSION" -pS -500 2>/dev/null || echo "")
  if [[ -n "$pane" ]]; then
    # Look for the expected substring as a model reply. The user's
    # prompt is also echoed in the pane (after the `❯` prompt), so a
    # naive substring match would false-positive on the prompt itself.
    # The model's reply is on its own line after a `●` bullet marker.
    if echo "$pane" | grep -qE "^[[:space:]]*●[[:space:]]+.*${EXPECT_SUBSTRING}" \
       || echo "$pane" | grep -qE "${EXPECT_SUBSTRING}[[:space:]]*$"; then
      echo "$pane" | grep -B 1 -A 3 "$EXPECT_SUBSTRING" > "$REPLY_FILE" 2>/dev/null
      log "assistant replied (from tmux pane):"
      head -10 "$REPLY_FILE" | sed 's/^/  /' >&2
      break
    fi
  fi
  if (( (SECONDS - reply_deadline + REPLY_TIMEOUT_SECONDS) % 30 == 0 )); then
    log "  still waiting ($((reply_deadline - SECONDS))s remaining)"
  fi
  sleep 5
done
if [[ ! -s "$REPLY_FILE" ]]; then
  log "no assistant reply matching '$EXPECT_SUBSTRING' in ${REPLY_TIMEOUT_SECONDS}s -- dumping diagnostics"
  # Find the VM the agent runs in and snapshot its tmux session so we can
  # tell whether the message arrived, whether claude started, whether it
  # crashed, etc. The bundled limactl path matches first-message-verify's
  # PATH prefix.
  VM_NAME="minds-${HOST_NAME}"
  TMUX_OUT=/tmp/first-message-agent-tmux.txt
  AGENT_LOG=/tmp/first-message-agent-mngr-logs.txt
  log "VM=$VM_NAME; capturing tmux + agent state"
  limactl shell "$VM_NAME" -- bash -c '
    echo "=== tmux ls ==="; tmux ls 2>&1 || true
    for S in $(tmux ls -F "#{session_name}" 2>/dev/null); do
      echo "=== tmux capture-pane $S ==="
      tmux capture-pane -t "$S" -pS -500 2>&1 || true
    done
    echo "=== agent processes ==="
    ps auxe 2>&1 | grep -iE "claude|mngr" | grep -v grep | head -20
    echo "=== claude session dir ==="
    ls -la ~/.claude/ 2>&1 | head -10
  ' > "$TMUX_OUT" 2>&1 || true
  log "tmux/agent diagnostics at $TMUX_OUT (head):"
  head -50 "$TMUX_OUT" >&2 || true
  fail "no assistant reply matching '$EXPECT_SUBSTRING' in ${REPLY_TIMEOUT_SECONDS}s"
fi

log "writing agent info to $AGENT_INFO_PATH (host=$HOST_NAME agent=$AGENT_NAME creation_id=$AGENT_ID)"
HOST_NAME="$HOST_NAME" AGENT_NAME="$AGENT_NAME" AGENT_ID="$AGENT_ID" BASE="$BASE" python3 -c '
import json, os
print(json.dumps({
    "host_name":    os.environ["HOST_NAME"],
    "agent_name":   os.environ["AGENT_NAME"],
    "creation_id":  os.environ["AGENT_ID"],
    "base_url":     os.environ["BASE"],
}))
' > "$AGENT_INFO_PATH"

if [[ "$SKIP_DESTROY" == "1" ]]; then
  log "SKIP_DESTROY=1 -- leaving agent running; caller is responsible for cleanup"
  log "SUCCESS"
  exit 0
fi

log "destroying agent"
DEL=$(curl -s -b "$COOKIES" -o /tmp/first-message-delete.json -w '%{http_code}' \
  -X POST --max-time $DESTROY_TIMEOUT_SECONDS "$BASE/workspace/$AGENT_ID/delete")
log "delete HTTP $DEL"
case "$DEL" in
  2*|404) ;;
  *) fail "delete returned HTTP '$DEL' -- agent may be dangling; body at /tmp/first-message-delete.json";;
esac

log "SUCCESS"
