#!/usr/bin/env bash
set -euo pipefail

# Plugins enabled at project scope in .claude/settings.json that we keep fresh.
PLUGIN_IDS=(
    "imbue-code-guardian@imbue-code-guardian"
    "imbue-mngr-skills@imbue-mngr"
)

# Check if claude CLI is available
if ! command -v claude &>/dev/null; then
    exit 0
fi

# Clear stale plugin cache for our marketplaces to avoid using outdated agents/skills
CACHE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/cache"
rm -rf "$CACHE_DIR/imbue-mngr" "$CACHE_DIR/imbue-code-guardian" 2>/dev/null || true

# The plugins and marketplaces are configured at project scope in
# .claude/settings.json (extraKnownMarketplaces + enabledPlugins),
# so Claude Code handles installation automatically. Just update.
for plugin_id in "${PLUGIN_IDS[@]}"; do
    GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes' \
      claude plugin update "$plugin_id" 2>/dev/null || true
done
