#!/usr/bin/env bash
# UserPromptSubmit hook: record the codex session id + rollout transcript path.
#
# codex runs this before each user turn (see build_codex_hooks_config), passing a
# JSON payload on stdin that carries the session id and the rollout transcript
# path. This hook does NO lifecycle-state work -- RUNNING vs WAITING comes from the
# app-server thread/status, not from any marker. It only records two pointers other
# machinery needs:
#
#   - session_id      -> $MNGR_AGENT_STATE_DIR/codex_root_session
#     The rollout session id the adopt/preserve machinery resumes by (kept in sync
#     with ROOT_SESSION_FILENAME in codex_config.py).
#   - transcript_path -> $MNGR_AGENT_STATE_DIR/codex_transcript_path
#     The absolute path of the active rollout JSONL, which stream_transcript.sh
#     tails (kept in sync with TRANSCRIPT_PATH_FILENAME in codex_config.py).
#
# Both are re-recorded on every turn because a resume can open a fresh rollout with
# a new session id and path. Never writes stdout (codex treats UserPromptSubmit
# stdout as additional model context); avoids `set -e` so a malformed payload
# cannot disrupt codex's loop.

if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "record_session_pointers.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

payload=$(cat)

# Extract the first value of a `"<key>":"<value>"` JSON string field from the
# payload. transcript_path may contain spaces and slashes, so the value is matched
# up to the first closing quote rather than constrained to a UUID shape. POSIX
# grep/sed only -- no jq (it may be absent on remote hosts).
_extract_field() {
    printf '%s' "$2" \
        | grep -oE "\"$1\":\"[^\"]*\"" \
        | head -n 1 \
        | sed -E "s/^\"$1\":\"(.*)\"\$/\1/"
}

session_id=$(_extract_field "session_id" "$payload")
if [ -n "$session_id" ]; then
    printf '%s' "$session_id" > "$MNGR_AGENT_STATE_DIR/codex_root_session"
fi

transcript_path=$(_extract_field "transcript_path" "$payload")
if [ -n "$transcript_path" ]; then
    printf '%s' "$transcript_path" > "$MNGR_AGENT_STATE_DIR/codex_transcript_path"
fi
