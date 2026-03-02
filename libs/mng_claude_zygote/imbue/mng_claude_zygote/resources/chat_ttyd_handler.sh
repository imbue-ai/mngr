#!/bin/bash
# Handler for the chat ttyd (invoked by ttyd --url-arg).
#
# When accessed with ?arg=NEW, starts a new conversation.
# When accessed with ?arg=<conversation_id>, resumes that conversation.
# When accessed with no args, shows usage.
#
# Environment:
#   MNG_HOST_DIR  - host data directory (contains commands/chat.sh)

set -euo pipefail

CHAT_SCRIPT="${MNG_HOST_DIR:?MNG_HOST_DIR must be set}/commands/chat.sh"

if [ -z "${1:-}" ]; then
    echo "Usage: pass ?arg=NEW or ?arg=<conversation_id> in the URL"
    echo "Press enter to close."
    read -r
    exit 1
fi

if [ "$1" = "NEW" ]; then
    exec "$CHAT_SCRIPT" --new
else
    exec "$CHAT_SCRIPT" --resume "$1"
fi
