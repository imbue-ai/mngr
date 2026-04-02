#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ID="imbue-code-guardian@imbue-code-guardian"

# Check if claude CLI is available
if ! command -v claude &>/dev/null; then
    exit 0
fi

# Clear stale plugin cache for our marketplaces to avoid using outdated agents/skills
CACHE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/cache"
rm -rf "$CACHE_DIR/imbue-mngr" "$CACHE_DIR/imbue-code-guardian" 2>/dev/null || true

# The plugin and marketplace are configured at project scope in
# .claude/settings.json (extraKnownMarketplaces + enabledPlugins),
# so Claude Code handles installation automatically. Just update.
claude plugin update "$PLUGIN_ID" 2>/dev/null || true
