# Changelog consolidation cron

Nightly Modal-deployed agent that consolidates per-PR `changelog/<branch>.md`
entries into `UNABRIDGED_CHANGELOG.md` (verbatim) and `CHANGELOG.md`
(AI-summarized), commits to a per-run branch, pushes, and opens a PR.

## Files

| File | Role |
|---|---|
| `scripts/setup_changelog_agent.sh` | Idempotent deploy of the Modal schedule via `mngr schedule add` |
| `scripts/changelog_consolidation_prompt.md` | The 10-step orchestration spec the cron's `headless_claude` reads via `--message-file` |
| `scripts/consolidate_changelog.py` | Deterministic consolidation step (collects `changelog/*.md`, appends to `UNABRIDGED_CHANGELOG.md`, deletes the source files) |
| `scripts/consolidate_changelog_test.py` | Unit tests for the consolidation script |
| `test_meta_ratchets.py` | Contains `test_pr_has_changelog_entry` ratchet enforcing per-PR entry files |

## Schedule shape

- Cron: `0 8 * * *` UTC = midnight PST
- Provider: Modal
- App: `mngr-schedule-changelog-consolidation`
- Modal env: `mngr-changelog-schedule-920da0b3db46415d8c4aec37ca23a637`
  - Set up via `MNGR_ROOT_NAME=mngr-changelog-schedule` for an isolated mngr config namespace
- Container forks a per-run branch via `mngr create --branch ':mngr/changelog-consolidation-{DATE}'` so the consolidation PR's diff against `main` is just the consolidation commit (assuming the cron is deployed from `main`)

## Required credentials

The deploy bakes two secrets into the schedule via `--pass-env`:

1. **`GH_TOKEN`** — must be the **bot account token** at `~/.credentials/bot-git-token`, **not** your personal `gh auth token`. The bot account has `bot@imbue.com` as a verified email; the prompt's step 6 sets `git config user.email "bot@imbue.com"` so commit attribution lines up. Using a personal token will push and open PRs as you, which we don't want for automation.
2. **`ANTHROPIC_API_KEY`** — at `~/.credentials/anthropic-api-key`. Used by claude inside the cron.

`IS_SANDBOX=1` is also passed so claude accepts `--dangerously-skip-permissions` as root inside the Modal container.

## (Re)deploy

The schedule is already created on Modal. To replace it (e.g. after editing the
prompt or the setup script):

```bash
export GH_TOKEN="$(cat ~/.credentials/bot-git-token)"
export ANTHROPIC_API_KEY="$(cat ~/.credentials/anthropic-api-key)"
export CHANGELOG_REPLACE=1   # required to clobber the existing schedule
bash scripts/setup_changelog_agent.sh
```

If a previous deploy left an image-cache checkpoint that conflicts, clear it
first:

```bash
rm -f ~/.mngr-changelog-schedule/build/*/mngr_build/*.checkpoint \
      ~/.mngr-changelog-schedule/build/*/mngr_build/current.tar.gz
```

Without `CHANGELOG_REPLACE=1`, the script errors out rather than silently
clobbering a live schedule.

## Trigger on demand

```bash
env -u MNGR_HOST_DIR -u MNGR_PREFIX MNGR_ROOT_NAME=mngr-changelog-schedule \
  bash -c '
    DISABLE_PLUGIN_ARGS=$(uv run python -c "
import importlib.metadata
enabled = {\"schedule\", \"modal\", \"headless_claude\", \"claude\", \"file\"}
names = sorted({ep.name for ep in importlib.metadata.entry_points(group=\"mngr\")} - enabled)
print(\" \".join(f\"--disable-plugin {n}\" for n in names))
")
    uv run mngr schedule run changelog-consolidation --provider modal $DISABLE_PLUGIN_ARGS
  '
```

## Reading a fire's outcome

Claude's **final assistant message** is a single JSON object with this schema:

```json
{
  "status": "done" | "skipped-no-entries" | "failed",
  "pr_url": "<url>" | null,
  "notes": "<freeform human-readable string>"
}
```

It appears in:

- `mngr schedule run` stdout (foreground triggers)
- The Modal app logs at the deployment URL printed during deploy

There is intentionally **no separate `status.json` artifact** — the previous
design wrote one to the state volume but the agent record was destroyed before
the file could be retrieved (no agent ID to address `mngr file get` against).
The JSON-in-final-message contract removes that hop.

## Common deploy pitfalls

- **`Error: Agent type 'headless_claude' is a headless agent type. Use --foreground...`** — `--foreground` flag missing from `--args`. Already in the live script; only an issue if someone edits it out.
- **`Error: Create command should either use --branch with a {DATE} placeholder or --reuse...`** — schedule needs a per-run branch pattern. Already in the live script.
- **Claude's command emits plain text, framework reports "claude exited without producing output"** — `--output-format stream-json --verbose --include-partial-messages` missing from the cli_args list. Already in the live script.
- **`shlex.split` strips the JSON quotes from the `-S` value** — the value must be **single-quoted** in the `--args` string so the embedded double quotes survive cron_runner's POSIX-mode shlex. Already in the live script.
- **`error: unknown option '--host-env-file'`** — cron_runner appends `--host-env-file` to every `mngr create`; if it lands in agent_args (after `--`) it gets handed to claude. Pass tool-level flags via `-S agent_types.headless_claude.cli_args=[...]` instead of `-- ...`. Already in the live script.

The current live script already accounts for all of the above; this list is
documentation for anyone tempted to "simplify" it.
