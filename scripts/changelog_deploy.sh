#!/usr/bin/env bash
set -euo pipefail

# (Re)deploy the nightly changelog-consolidation schedule from the current
# source. Removes any existing "changelog-consolidation" schedule first, so
# running this is the way to redeploy after editing the prompt or this
# script.
#
# The scheduled agent runs nightly at midnight Pacific time as a
# headless_claude agent. The schedule is deployed with an explicit
# --timezone (see SCHEDULE / TIMEZONE below) so the fire time does not
# depend on the deploying machine's local timezone. The
# orchestration steps live in scripts/changelog_consolidation_prompt.md and
# are executed by claude itself (running changelog_consolidate.py, summarizing
# each project's new dated sections into its per-project CHANGELOG.md -- the
# publishable libs/apps projects accumulate under [Unreleased] until release,
# while the dev project (never released) gets one summarized "## <date>"
# section per landed date, mirroring its UNABRIDGED_CHANGELOG.md -- committing,
# spawning one or more subagents to
# review the new bullets for factual accuracy against the code,
# pushing a branch, opening a PR). Claude's final assistant message is a single JSON object describing the
# outcome (status, with pr_url on success or notes on failure) -- visible in
# `mngr schedule run` stdout and Modal logs.
#
# Usage:
#   ./scripts/changelog_deploy.sh
#
# Secrets are read from Vault at deploy time and baked into the schedule via
# the --pass-env flags below. Run `vault login -method=oidc` first:
#   secrets/mngr/dev/github    key GH_TOKEN          - token for bot@imbue.com.
#   secrets/mngr/dev/anthropic key ANTHROPIC_API_KEY - claude key for the cron
#                                                       container.
#
# Optional environment:
#   CHANGELOG_VERIFY             - Verification mode (default: "none"). Set to
#                                  "quick" or "full" to run the agent once
#                                  during deploy.
#   VAULT_ADDR / VAULT_NAMESPACE - override the Vault endpoint (default: the
#                                  imbue HCP cluster).
#
# The provider is read from the shared `PROVIDER` constant in
# scripts/changelog_schedule_utils.py so the `changelog-trigger` justfile
# recipe's on-demand command targets the same deployment. To change
# providers, edit that constant and re-run this script.
#
# To trigger a fire on demand and read its JSON outcome (status, with pr_url on
# success or notes on failure), use the justfile recipe:
#   just changelog-trigger
# (claude's final assistant message is the structured outcome; see also Modal app logs)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

TRIGGER_NAME="changelog-consolidation"
# Midnight nightly, interpreted in TIMEZONE below (so it fires at midnight
# Pacific regardless of where this deploy script runs).
SCHEDULE="0 0 * * *"
TIMEZONE="America/Los_Angeles"
PROVIDER=$(uv run python "${REPO_ROOT}/scripts/changelog_schedule_utils.py" --print-provider)
VERIFY="${CHANGELOG_VERIFY:-none}"

# Use an isolated mngr config namespace so we don't load the repo's
# .mngr/settings.toml (which references plugins that won't exist in the
# container). Mirrors test_schedule_run.py's build_subprocess_env pattern.
export MNGR_ROOT_NAME="mngr-changelog-schedule"
unset MNGR_HOST_DIR
unset MNGR_PREFIX

# Pull the agent's credentials from Vault and export them so the --pass-env
# flags below bake them into the schedule. Requires a valid `vault login
# -method=oidc`. VAULT_ADDR / VAULT_NAMESPACE default to the imbue HCP cluster
# (matching apps/minds/imbue/minds/envs/vault_reader.py).
export VAULT_ADDR="${VAULT_ADDR:-https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200}"
export VAULT_NAMESPACE="${VAULT_NAMESPACE:-admin}"
if ! command -v vault >/dev/null 2>&1; then
    echo "Error: 'vault' CLI not found on PATH. Install it and run 'vault login -method=oidc'." >&2
    exit 1
fi

# Echo the value of the split-layout secret at secrets/<$1>/<$2>, or exit
# non-zero. Secrets use the split layout: each key is its own leaf at
# `<service>/<KEY>` holding a single `value` field. pipefail propagates a
# failed `vault kv get` (e.g. not logged in); `jq -e` with the `// "" | select`
# guard exits non-zero when the value is absent or empty. The value is never printed.
read_vault_secret() {
    vault kv get -format=json -mount=secrets "$1/$2" | jq -er '.data.data.value // "" | select(. != "")'
}

if ! GH_TOKEN=$(read_vault_secret mngr/dev/github GH_TOKEN); then
    echo "Error: could not read GH_TOKEN from secrets/mngr/dev/github/GH_TOKEN. Run 'vault login -method=oidc' and confirm the entry exists." >&2
    exit 1
fi
if ! ANTHROPIC_API_KEY=$(read_vault_secret mngr/dev/anthropic ANTHROPIC_API_KEY); then
    echo "Error: could not read ANTHROPIC_API_KEY from secrets/mngr/dev/anthropic/ANTHROPIC_API_KEY. Run 'vault login -method=oidc' and confirm the entry exists." >&2
    exit 1
fi
export GH_TOKEN ANTHROPIC_API_KEY

# IS_SANDBOX=1 lets claude accept --dangerously-skip-permissions as root
# inside the Modal container.
export IS_SANDBOX=1

# Compute --disable-plugin args via the shared helper so the deploy and
# the on-demand trigger (the `changelog-trigger` justfile recipe) stay in
# sync about which plugins must be disabled around `mngr schedule` invocations.
DISABLE_PLUGIN_ARGS=$(uv run python "${REPO_ROOT}/scripts/changelog_schedule_utils.py" --print-disable-plugin-args)

# Stop *every* Modal app in the changelog schedule's isolated environment(s)
# before redeploying. `mngr schedule remove` below only stops the app whose
# name matches the *current* naming scheme, so a past naming-scheme change once
# left an orphaned app firing a second nightly run. The schedule has its own
# dedicated environment, so sweeping the whole environment is safe and
# guarantees no orphaned cron app survives the redeploy.
echo "Stopping any existing Modal apps in the changelog environment(s)..."
uv run python "${REPO_ROOT}/scripts/changelog_schedule_utils.py" --stop-all-apps

# Always remove an existing trigger before recreating, so the deployed
# schedule reflects the current source no matter what was deployed before.
# (This also deletes the schedule's creation record from the state volume,
# which the app sweep above does not touch.)
EXISTING=$(uv run mngr schedule list --provider "$PROVIDER" --all --format json $DISABLE_PLUGIN_ARGS 2>/dev/null || echo '{"schedules":[]}')
if echo "$EXISTING" | python3 -c "
import json, sys
data = json.load(sys.stdin)
names = [s['trigger']['name'] for s in data.get('schedules', [])]
sys.exit(0 if '${TRIGGER_NAME}' in names else 1)
" 2>/dev/null; then
    echo "Removing existing schedule '${TRIGGER_NAME}' before redeploy..."
    uv run mngr schedule remove "$TRIGGER_NAME" --provider "$PROVIDER" --force $DISABLE_PLUGIN_ARGS
fi

echo "Creating schedule '${TRIGGER_NAME}' (provider=$PROVIDER, verify=$VERIFY)..."

# headless_claude with the orchestration spec staged from
# scripts/changelog_consolidation_prompt.md.
#
# cli_args explained (each is required for headless_claude on this path):
#   --dangerously-skip-permissions
#       so claude can run python3 / git / gh as tools; IS_SANDBOX=1
#       (passed in via the agent env) lets it accept that flag as root.
#   --output-format stream-json --verbose --include-partial-messages
#       headless_claude's stream_output() parses JSONL events from
#       stdout.jsonl (text deltas, assistant events, result events).
#       Without --output-format=stream-json, claude --print emits plain
#       text and the parser extracts zero events, so the framework
#       raises "claude exited without producing output" even when
#       claude succeeded. claude requires --verbose alongside
#       --output-format stream-json with --print; --include-partial-
#       messages gets us incremental deltas. Same pattern as
#       _HEADLESS_CLAUDE_ARGS in libs/mngr/imbue/mngr/cli/ask.py.
#
# Why cli_args via -S, not agent_args after `--`:
#   cron_runner appends `--host-env-file /staging/secrets/.env` to every
#   create invocation. When our --args end with a `--` passthrough
#   section, the appended --host-env-file lands inside the passthrough
#   and gets handed to the claude binary (which doesn't recognize it).
#   cli_args go through the same code path on the claude side but don't
#   require a `--` separator on the mngr CLI side, so cron_runner's
#   append stays in the mngr-flag section.
#
# Why single quotes around the -S value:
#   cron_runner runs `shlex.split` on the stored args string in POSIX
#   mode. Bare double quotes get stripped, reducing the JSON list to
#   bracketed bare tokens that fail json.loads inside
#   _parse_setting_value, which then falls through to treating the
#   value as a plain string. Single quotes survive shlex.split as part
#   of one token so json.loads sees the original quoted JSON list.
uv run mngr schedule add "$TRIGGER_NAME" \
    --command create \
    --schedule "$SCHEDULE" \
    --timezone "$TIMEZONE" \
    --provider "$PROVIDER" \
    --verify "$VERIFY" \
    --full-copy \
    --auto-merge \
    --auto-merge-branch main \
    --exclude-user-settings \
    --exclude-project-settings \
    --pass-env GH_TOKEN \
    --pass-env ANTHROPIC_API_KEY \
    --pass-env MNGR_ROOT_NAME \
    --pass-env IS_SANDBOX \
    --no-auto-fix-args \
    $DISABLE_PLUGIN_ARGS \
    --args "--type headless_claude --foreground --branch ':mngr/changelog-consolidation-{DATE}' --message-file /code/project/scripts/changelog_consolidation_prompt.md -S 'agent_types.headless_claude.cli_args=[\"--dangerously-skip-permissions\",\"--output-format\",\"stream-json\",\"--verbose\",\"--include-partial-messages\"]'"

echo "Schedule '${TRIGGER_NAME}' created successfully."
echo ""
echo "To trigger a run on demand and read its outcome JSON, run:"
echo "  just changelog-trigger"
echo "(claude's final assistant message is a single JSON object: status, with pr_url on success or notes on failure)"
