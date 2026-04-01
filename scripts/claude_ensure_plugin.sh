#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ID="imbue-code-review@imbue-mngr"
MARKETPLACE_NAME="imbue-mngr"
MARKETPLACE_REPO="imbue-ai/mngr"

# Check if claude CLI is available
if ! command -v claude &>/dev/null; then
    exit 0
fi

# Check if marketplace is added (look in both user and project scopes)
if ! claude plugin marketplace list --json 2>/dev/null | jq -e ".[] | select(.name == \"$MARKETPLACE_NAME\")" &>/dev/null; then
    echo "ERROR: The '$MARKETPLACE_NAME' marketplace is not configured." >&2
    echo "" >&2
    echo "Run these commands to install the required plugin:" >&2
    echo "" >&2
    echo "  claude plugin marketplace add $MARKETPLACE_REPO" >&2
    echo "  claude plugin install $PLUGIN_ID" >&2
    echo "" >&2
    echo "To scope the plugin to only this project, add --scope project:" >&2
    echo "" >&2
    echo "  claude plugin marketplace add $MARKETPLACE_REPO --scope project" >&2
    echo "  claude plugin install $PLUGIN_ID --scope project" >&2
    echo "" >&2
    exit 2
fi

# Check if plugin is installed
if ! claude plugin list --json 2>/dev/null | jq -e ".[] | select(.id == \"$PLUGIN_ID\")" &>/dev/null; then
    echo "ERROR: The '$PLUGIN_ID' plugin is not installed." >&2
    echo "" >&2
    echo "Run this command to install it:" >&2
    echo "" >&2
    echo "  claude plugin install $PLUGIN_ID" >&2
    echo "" >&2
    echo "To scope the plugin to only this project, add --scope project:" >&2
    echo "" >&2
    echo "  claude plugin install $PLUGIN_ID --scope project" >&2
    echo "" >&2
    exit 2
fi

# Plugin is installed -- update it silently
claude plugin update "$PLUGIN_ID" 2>/dev/null || true
