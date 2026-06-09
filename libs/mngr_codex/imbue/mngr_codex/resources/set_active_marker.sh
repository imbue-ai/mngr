#!/usr/bin/env bash
# UserPromptSubmit hook: mark the agent RUNNING and record the turn's root.
#
# codex runs this before each user turn (see build_codex_hooks_config), passing
# a JSON payload on stdin that carries the session id and the rollout transcript
# path. It touches the `active` marker (BaseAgent reads its presence as RUNNING)
# and, when the marker was absent -- i.e. this prompt opens a new turn -- records
# the payload's session id as the turn's root in `codex_root_session` and the
# rollout `transcript_path` in `codex_transcript_path`.
#
# Why record the root: a nested or recursive `codex` process sharing this same
# CODEX_HOME (and thus these hooks) could otherwise overwrite the root session id
# mid-turn, and its later Stop could flip the still-working root agent to
# WAITING. clear_active_marker.sh clears the marker only for the recorded root
# session, so it needs to know which one that is. The root agent's
# UserPromptSubmit always fires while `active` is absent (it opens the turn);
# guarding the capture behind "marker absent" means a nested codex (whose prompt
# fires while the parent marker is present) cannot steal the root. Re-recording
# at each turn boundary keeps the root + transcript path correct across resume.
#
# Why also record the transcript path here (unlike antigravity, which scopes the
# stream via a conversation-ids set): codex writes a single rollout JSONL per
# session and hands its absolute path to every hook as `transcript_path`.
# stream_transcript.sh tails exactly that file, so this hook is the single source
# of truth for which rollout to follow. The path can change across resume (codex
# may open a fresh rollout), so it is re-captured at each turn boundary too.
#
# Note: codex's Stop fires only at root scope -- Task-style subagents fire a
# distinct SubagentStop that mngr deliberately does not hook -- so there is no
# subagent Stop to defend against here (the root-session guard defends only
# against a *separate* nested codex process sharing this CODEX_HOME).
#
# Marker / root / transcript-path file names are kept in sync with
# codex_config.py. Never writes stdout (codex treats UserPromptSubmit stdout as
# additional model context); avoids `set -e` so a malformed payload can't disrupt
# codex's loop.

if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "set_active_marker.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

marker_file="$MNGR_AGENT_STATE_DIR/active"
root_file="$MNGR_AGENT_STATE_DIR/codex_root_session"
transcript_path_file="$MNGR_AGENT_STATE_DIR/codex_transcript_path"

payload=$(cat)

# Capture the root session id + rollout transcript path only at a turn boundary
# (marker absent), checked before the touch below so it reflects the
# pre-invocation state. POSIX grep/sed only -- no jq dependency (jq may be absent
# on remote hosts).
if [ ! -e "$marker_file" ]; then
    # session_id is a lowercase 8-4-4-4-12 hex UUID; the strict shape keeps a
    # stray field from injecting a bogus id.
    session_id=$(
        printf '%s' "$payload" \
            | grep -oE '"session_id":"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"' \
            | head -n 1 \
            | sed -E 's/.*:"([0-9a-f-]+)".*/\1/'
    )
    if [ -n "$session_id" ]; then
        printf '%s' "$session_id" > "$root_file"
    fi

    # transcript_path is an arbitrary absolute path (spaces/slashes allowed), so
    # match the JSON string value up to the first unescaped closing quote and
    # strip the surrounding double-quotes.
    transcript_path=$(
        printf '%s' "$payload" \
            | grep -oE '"transcript_path":"[^"]*"' \
            | head -n 1 \
            | sed -E 's/^"transcript_path":"(.*)"$/\1/'
    )
    if [ -n "$transcript_path" ]; then
        printf '%s' "$transcript_path" > "$transcript_path_file"
    fi
fi

touch "$marker_file"
