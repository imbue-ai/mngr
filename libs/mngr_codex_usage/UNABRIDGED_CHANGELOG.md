# Unabridged Changelog - mngr_codex_usage

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_codex_usage/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-19

Trimmed the README to user-relevant content and tightened it for concision.

## 2026-06-17

Bumped the trailing-comment ratchet snapshot to account for the converter's pre-existing `# noqa` lint suppression (an immovable trailing comment), surfaced by CI on the agent-mixins branch.

## 2026-06-16

New package `imbue-mngr-codex-usage`: cost/usage tracking for Codex agents in `mngr usage`. Codex reports cumulative token usage (no dollar cost), so cost is estimated from the pricing table and aggregated session-cumulatively. The writer (`codex_usage.sh`) reads codex's rollout stream and emits one `cost_snapshot` per `token_count` item; mngr_codex's background-tasks supervisor launches it when present. It also maps codex's 5h/7d rate-limit windows (subscription mode), so Codex subscription agents get Claude-style windows.

The writer tracks a byte-offset cursor (persisted under `plugin/codex/.usage_cursor`) so each poll processes only the new tail of the rollout (O(new bytes) per tick), important for long Codex sessions. Re-emission after a crash is harmless because the session-cumulative reader keeps the freshest reading per session.

The writer delegates event emission to a standalone `codex_usage_emit.py` (installed alongside `codex_usage.sh` and invoked by it), keeping that logic type-checked, linted, and unit-tested directly. Malformed rollout lines and unreadable cursor state are logged at warning level.
