#!/bin/bash
# Statusline shim provisioned by mngr_claude_usage's on_before_provisioning hookimpl.
#
# Claude Code calls statusLine.command (defined in <work_dir>/.claude/settings.local.json)
# on every render. This shim lives at a host-stable path -- <host_dir>/commands/
# claude_statusline.sh -- so the statusLine.command entry written into the work_dir's
# settings.local.json stays valid across agent lifecycles, even after every agent that
# wrote it has been destroyed.
#
# Behavior:
#   1. If MNGR_AGENT_STATE_DIR is unset (e.g. claude is invoked standalone, outside of
#      an mngr agent), exit 0 silently. Erroring would flood the user with errors on
#      every render; emitting an event would have nowhere to write to (the per-agent
#      events file is the whole point of the writer).
#   2. Otherwise, capture stdin once, forward the payload to the sibling writer script
#      (which appends one cost_snapshot event per render to
#      $MNGR_AGENT_STATE_DIR/events/claude/usage/events.jsonl), then replay the payload
#      to the user's pre-existing statusLine.command (if any), captured at provision
#      time into $MNGR_AGENT_STATE_DIR/commands/user_statusline_cmd.
set -euo pipefail

if [ -z "${MNGR_AGENT_STATE_DIR:-}" ]; then
  exit 0
fi

shim_dir=$(cd "$(dirname "$0")" && pwd)
writer="$shim_dir/claude_usage_writer.sh"
user_cmd_file="$MNGR_AGENT_STATE_DIR/commands/user_statusline_cmd"

payload=$(cat)

# Writer is best-effort: a failure here (jq missing, malformed payload, etc.) must not
# break the user's pre-existing statusline.
if [ -x "$writer" ]; then
  printf '%s' "$payload" | "$writer" || true
fi

# Pass the user's command's exit status through unchanged: if their statusline was
# already misbehaving (or their command path is broken), Claude Code should see the
# same non-zero exit it would have seen without us in the chain. Eating the failure
# here would hide problems they were previously aware of.
if [ -s "$user_cmd_file" ]; then
  user_cmd=$(cat "$user_cmd_file")
  printf '%s' "$payload" | sh -c "$user_cmd"
fi
