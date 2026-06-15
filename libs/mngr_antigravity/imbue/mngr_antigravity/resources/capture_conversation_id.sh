#!/usr/bin/env bash
# Record every distinct agy conversation ID this agent touches, for transcripts.
#
# agy fires this as a PreInvocation hook handler (see
# build_antigravity_hooks_config). On every invocation -- for the root agent
# AND each subagent it spawns -- agy passes a JSON object on stdin that includes
# `"conversationId":"<uuid>"` (verified live against agy 1.0.4, alongside
# artifactDirectoryPath/transcriptPath). We extract that id and append it once
# to the per-agent conversation-ids file, whose `sort -u` is every conversation
# this agent has touched -- stream_transcript.sh tails each one's transcript.
#
# This file is the transcript-scoping *set* only; it does NOT pick the agent's
# main conversation for resume (its lines include subagents). Resume reads
# root_conversation, written by statusline.sh. See
# AntigravityAgent.assemble_command and CONVERSATION_IDS_FILENAME.
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
set -euo pipefail

if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
    echo "capture_conversation_id.sh: MNGR_AGENT_STATE_DIR is not set" >&2
    exit 1
fi

ids_file="$MNGR_AGENT_STATE_DIR/antigravity_conversation_ids"

payload=$(cat)

# Extract the first `"conversationId":"<uuid>"` value. POSIX grep/sed only --
# no jq dependency (jq may be absent on remote hosts).
# `|| true` keeps a payload with no conversationId match (grep exits 1)
# from tripping `set -e` via `pipefail` -- a missing id is a normal case
# handled by the empty-id branch below, not an error.
conv_id=$(
    printf '%s' "$payload" \
        | grep -oE '"conversationId":"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"' \
        | head -n 1 \
        | sed -E 's/.*:"([0-9a-f-]+)".*/\1/' \
        || true
)

# No id in the payload -> nothing to record (never clobber the file).
if [ -z "$conv_id" ]; then
    exit 0
fi

# Append each distinct id once. Order/recency does not matter: the only
# consumer is stream_transcript.sh, which reads the unique set (`sort -u`). The
# agent's main conversation for resume is tracked separately in
# root_conversation by statusline.sh. `grep -qxF` is a whole-line fixed
# match; on a missing file it returns non-zero, so the first id is appended.
if ! grep -qxF "$conv_id" "$ids_file" 2>/dev/null; then
    printf '%s\n' "$conv_id" >> "$ids_file"
fi
