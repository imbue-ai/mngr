#!/bin/bash
# Handler for the agent-tmux ttyd (invoked by ttyd --url-arg).
#
# When accessed with ?arg=<agent_name>, attaches to that agent's tmux session.
# When accessed with no args, shows an error message.

set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "Error: agent name required (pass via ?arg=<name>)"
    echo "Press enter to close."
    read -r
    exit 1
fi

AGENT_NAME="$1"
TMUX_SESSION="mng-${AGENT_NAME}"

unset TMUX
exec tmux attach -t "${TMUX_SESSION}:0"
