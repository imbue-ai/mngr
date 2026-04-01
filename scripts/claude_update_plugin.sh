#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ID="imbue-code-guardian@imbue-code-guardian"

# Check if claude CLI is available
if ! command -v claude &>/dev/null; then
    exit 0
fi

# The plugin and marketplace are configured at project scope in
# .claude/settings.json (extraKnownMarketplaces + enabledPlugins),
# so Claude Code handles installation automatically. Just update.
claude plugin update "$PLUGIN_ID" 2>/dev/null || true
