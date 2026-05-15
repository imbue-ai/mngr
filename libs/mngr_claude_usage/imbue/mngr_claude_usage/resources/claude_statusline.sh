#!/bin/bash
# Statusline shim provisioned by mngr_claude_usage's on_before_provisioning hookimpl.
#
# Claude Code calls statusLine.command (defined in <work_dir>/.claude/settings.local.json)
# on every render. After the first successful API response of the session, the
# JSON snapshot piped to stdin includes a `rate_limits` field with five_hour /
# seven_day / overage windows (Claude.ai subscriptions only). We:
#   1. Capture stdin once into a variable (any user statusline also reads stdin).
#   2. Forward the payload to the usage writer (sibling script in this
#      same commands/ dir), which appends one JSONL event per render to
#      $MNGR_AGENT_STATE_DIR/events/claude/usage/events.jsonl.
#   3. Replay the payload to the user's pre-existing statusLine.command (if
#      any) captured at provision time into the sibling user_statusline_cmd
#      sidecar file, so any pre-existing user statusline keeps working
#      unchanged.
set -euo pipefail

: "${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR must be set}"
commands_dir="$MNGR_AGENT_STATE_DIR/commands"
writer="$commands_dir/claude_usage_writer.sh"
user_cmd_file="$commands_dir/user_statusline_cmd"

payload=$(cat)

# Writer is best-effort: a failure here (jq missing, malformed payload, etc.)
# must not break the user's pre-existing statusline.
if [ -x "$writer" ]; then
  printf '%s' "$payload" | "$writer" || true
fi

# Pass the user's command's exit status through unchanged: if their statusline
# was already misbehaving (or their command path is broken), Claude Code should
# see the same non-zero exit it would have seen without us in the chain. Eating
# the failure here would hide problems they were previously aware of.
if [ -s "$user_cmd_file" ]; then
  user_cmd=$(cat "$user_cmd_file")
  printf '%s' "$payload" | sh -c "$user_cmd"
fi
