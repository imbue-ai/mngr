#!/usr/bin/env bash
# Capture this agent's current agy conversation ID from a hook payload.
#
# agy fires this as a PreInvocation hook handler (see
# build_antigravity_hooks_config). On every invocation agy passes a JSON
# object on stdin that includes `"conversationId":"<uuid>"` (verified live
# against agy 1.0.4, alongside artifactDirectoryPath/transcriptPath). We
# extract that id and append it to the per-agent conversation-ids file when
# it differs from the last recorded id, so:
#
#   * `tail -n 1` of the file is the most-recently-active conversation --
#     AntigravityAgent.assemble_command resumes it via `agy --conversation`.
#   * `sort -u` of the file is every conversation this agent has touched --
#     stream_transcript.sh tails each one's transcript.
#
# Appending only on change (rather than on every invocation) keeps the file
# small while still recording every distinct conversation and preserving
# which one is current across `/clear`, `/fork`, and `/switch`.
#
# The file name is kept in sync with CONVERSATION_IDS_FILENAME in
# antigravity_config.py. This script must never write to stdout: agy treats
# non-empty PreInvocation stdout as injected steps. It also deliberately
# avoids `set -e`/non-zero exits on the common paths so a malformed payload
# never disrupts agy's execution loop.

# mngr sets MNGR_AGENT_STATE_DIR for every agent process, and agy invokes this
# script through a path that embeds it (`$MNGR_AGENT_STATE_DIR/commands/...`),
# so it is always set in the real hook path -- an unset/empty value means a
# wiring bug, not a tolerable runtime case. Fail loudly (to stderr, never
# stdout -- agy treats PreInvocation stdout as injected steps) rather than
# silently writing the ids file to the filesystem root.
if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "capture_conversation_id.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

ids_file="$MNGR_AGENT_STATE_DIR/antigravity_conversation_ids"

payload=$(cat)

# Extract the first `"conversationId":"<uuid>"` value. POSIX grep/sed only --
# no jq dependency (jq may be absent on remote hosts).
conv_id=$(
    printf '%s' "$payload" \
        | grep -oE '"conversationId":"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"' \
        | head -n 1 \
        | sed -E 's/.*:"([0-9a-f-]+)".*/\1/'
)

# No id in the payload -> nothing to record (never clobber the file).
if [ -z "$conv_id" ]; then
    exit 0
fi

last_id=""
if [ -f "$ids_file" ]; then
    last_id=$(tail -n 1 "$ids_file" 2>/dev/null || true)
fi

if [ "$conv_id" != "$last_id" ]; then
    printf '%s\n' "$conv_id" >> "$ids_file"
fi
