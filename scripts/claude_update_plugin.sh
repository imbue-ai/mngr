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

# Non-interactive ssh so a marketplace git fetch can never hang the hook at a
# host-key or credential prompt, with a short connect timeout so that even with
# several plugins attempting update and install while offline, every attempt
# finishes (and the explanatory warning prints) within the hook time budget.
export GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=5'

# The plugins and marketplaces are configured at project scope in
# .claude/settings.json (extraKnownMarketplaces + enabledPlugins), so Claude
# Code handles installation automatically; this hook keeps them up to date.
#
# The plugin cache is deliberately left alone: `claude plugin update` refreshes
# it on success (verified on claude 2.1.205, where update repopulates even a
# deleted cache dir), while wiping it up front would strip every plugin skill
# from the session whenever the update fails (offline, git auth). Failures are
# surfaced (but not fatal) so a session missing /autofix and friends says why,
# instead of silently losing them; the final warning goes to stdout because
# SessionStart stdout is injected into the session context, where the agent
# can actually read it.
for plugin_id in "${PLUGIN_IDS[@]}"; do
    if output=$(claude plugin update "$plugin_id" 2>&1); then
        printf '%s\n' "$output"
        continue
    fi
    printf '%s\n' "$output" >&2
    # An update can fail because the plugin has never been installed for the
    # current scope -- e.g. a fresh machine, or a Sculptor workspace whose
    # project path has never seen an install. Converge by installing (which
    # lands at user scope, so every future workspace inherits it).
    if install_output=$(claude plugin install "$plugin_id" 2>&1); then
        printf '%s\n' "$install_output"
    else
        printf '%s\n' "$install_output" >&2
        echo "warning: failed to update or install plugin ${plugin_id}; this session will use the previously cached version, or lack its skills entirely if it was never installed"
    fi
done
