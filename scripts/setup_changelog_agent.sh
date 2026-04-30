#!/usr/bin/env bash
set -euo pipefail

# Idempotent setup of the nightly changelog consolidation agent.
#
# This script ensures exactly one "changelog-consolidation" schedule exists.
# Safe to run multiple times: skips creation if the schedule already exists.
#
# The scheduled agent runs at midnight PST as a headless_claude agent. The
# orchestration steps live in scripts/changelog_consolidation_prompt.md and
# are executed by claude itself (running consolidate_changelog.py, summarizing
# the new section, committing, pushing a branch, opening a PR, writing
# status.json).
#
# Usage:
#   ./scripts/setup_changelog_agent.sh
#
# Environment:
#   CHANGELOG_PROVIDER  - Provider to use (default: "modal").
#   CHANGELOG_VERIFY    - Verification mode (default: "none"). Set to "quick"
#                         or "full" to run the agent once during deploy.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

TRIGGER_NAME="changelog-consolidation"
# Midnight PST (UTC-8) = 08:00 UTC.
SCHEDULE="0 8 * * *"
PROVIDER="${CHANGELOG_PROVIDER:-modal}"
VERIFY="${CHANGELOG_VERIFY:-none}"

# Use an isolated mngr config namespace so we don't load the repo's
# .mngr/settings.toml (which references plugins that won't exist in the
# container). Mirrors test_schedule_run.py's build_subprocess_env pattern.
export MNGR_ROOT_NAME="mngr-changelog-schedule"
unset MNGR_HOST_DIR
unset MNGR_PREFIX

# Validate that required env vars are set so the agent can function.
for var in GH_TOKEN ANTHROPIC_API_KEY; do
    if [ -z "${!var:-}" ]; then
        echo "Error: $var is not set. The scheduled agent needs this to operate." >&2
        exit 1
    fi
done

# IS_SANDBOX=1 lets claude accept --dangerously-skip-permissions as root
# inside the Modal container.
export IS_SANDBOX=1

# Compute --disable-plugin args for every installed plugin EXCEPT the
# minimum set the scheduled run needs.
DISABLE_PLUGIN_ARGS=$(uv run python -c "
import importlib.metadata
enabled = {'schedule', 'modal', 'headless_claude', 'claude', 'file'}
names = sorted({ep.name for ep in importlib.metadata.entry_points(group='mngr')} - enabled)
print(' '.join(f'--disable-plugin {n}' for n in names))
")

# Check if the trigger already exists. Error unless CHANGELOG_REPLACE=1 was
# set, since the user probably wants to know they're about to clobber a live
# schedule.
EXISTING=$(uv run mngr schedule list --provider "$PROVIDER" --all --format json $DISABLE_PLUGIN_ARGS 2>/dev/null || echo '{"schedules":[]}')
if echo "$EXISTING" | python3 -c "
import json, sys
data = json.load(sys.stdin)
names = [s['trigger']['name'] for s in data.get('schedules', [])]
sys.exit(0 if '${TRIGGER_NAME}' in names else 1)
" 2>/dev/null; then
    if [ "${CHANGELOG_REPLACE:-}" != "1" ]; then
        echo "Error: Schedule '${TRIGGER_NAME}' already exists on provider '$PROVIDER'." >&2
        echo "       Set CHANGELOG_REPLACE=1 to remove the existing schedule and redeploy." >&2
        exit 1
    fi
    echo "CHANGELOG_REPLACE=1 set. Removing existing schedule before redeploy..."
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
    --provider "$PROVIDER" \
    --verify "$VERIFY" \
    --full-copy \
    --no-auto-merge \
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
echo "To check the result of a run (works even if sandbox has exited):"
echo "  uv run mngr list --format json $DISABLE_PLUGIN_ARGS"
echo "  uv run mngr file get <agent-id> status.json --relative-to state $DISABLE_PLUGIN_ARGS"
