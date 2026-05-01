#!/bin/bash
# Statusline shim provisioned by mngr_usage's claude_extra_per_agent_settings hookimpl.
#
# Claude Code calls statusLine.command (defined in per-agent settings.json) on every
# render. The harness pipes a JSON snapshot to stdin that includes a `rate_limits`
# field. We:
#   1. Capture stdin once into a variable (the user's downstream command also reads stdin).
#   2. Forward the payload to the rate-limits writer to update the shared cache.
#   3. Replay the payload to MNGR_USER_STATUSLINE_CMD if set, so any pre-existing
#      user statusline (caveman, starship, etc.) keeps working unchanged.
#
# Env vars (set by mngr_claude's settings.json env block):
#   MNGR_RATE_LIMITS_WRITER  Path to claude_rate_limits_writer.sh
#   MNGR_USER_STATUSLINE_CMD The user's pre-existing statusLine.command (optional)
set -euo pipefail

payload=$(cat)

if [ -n "${MNGR_RATE_LIMITS_WRITER:-}" ] && [ -x "$MNGR_RATE_LIMITS_WRITER" ]; then
  printf '%s' "$payload" | "$MNGR_RATE_LIMITS_WRITER" statusline
fi

if [ -n "${MNGR_USER_STATUSLINE_CMD:-}" ]; then
  printf '%s' "$payload" | sh -c "$MNGR_USER_STATUSLINE_CMD"
fi
