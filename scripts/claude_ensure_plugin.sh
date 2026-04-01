#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ID="imbue-code-guardian@imbue-code-guardian"
MARKETPLACE_NAME="imbue-code-guardian"
MARKETPLACE_REPO="imbue-ai/code-guardian"

# Check if claude CLI is available
if ! command -v claude &>/dev/null; then
    exit 0
fi

# The plugin is enabled at project scope in .claude/settings.json, so it
# can't be uninstalled per-user. We just need to ensure the marketplace is
# added (so Claude Code can fetch the plugin) and keep it up to date.

if ! claude plugin marketplace list --json 2>/dev/null | jq -e ".[] | select(.name == \"$MARKETPLACE_NAME\")" &>/dev/null; then
    echo "ERROR: The '$MARKETPLACE_NAME' marketplace is not configured." >&2
    echo "" >&2
    echo "Run this command to add it:" >&2
    echo "" >&2
    echo "  claude plugin marketplace add $MARKETPLACE_REPO" >&2
    echo "" >&2
    exit 2
fi

# Marketplace is present -- update the plugin silently
claude plugin update "$PLUGIN_ID" 2>/dev/null || true
