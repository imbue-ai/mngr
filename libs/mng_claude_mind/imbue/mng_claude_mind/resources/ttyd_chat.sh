#!/bin/bash
# Ttyd dispatch script for chat conversations.
#
# Invoked by the consolidated ttyd server when the URL contains ?arg=chat.
# Additional URL args are passed as positional parameters:
#   ?arg=chat&arg=NEW          -> $1=NEW   (start new conversation)
#   ?arg=chat&arg=<conv_id>    -> $1=<id>  (resume conversation)
#   ?arg=chat                  -> no args  (show usage)
#
# Environment:
#   MNG_AGENT_STATE_DIR  - agent state directory (contains commands/chat.sh)

set -euo pipefail

CHAT_SCRIPT="${MNG_AGENT_STATE_DIR:?}/commands/chat.sh"

if [ -z "${1:-}" ]; then
    echo "Usage: pass ?arg=chat&arg=NEW or ?arg=chat&arg=<conversation_id>"
    echo "Press enter to close."
    read -r
    exit 1
fi

if [ "$1" = "NEW" ]; then
    exec "$CHAT_SCRIPT" --new
else
    exec "$CHAT_SCRIPT" --resume "$1"
fi
