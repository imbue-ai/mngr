# Unabridged Changelog - mngr_claude_usage

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_claude_usage/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-14

The Claude statusline writer (`mngr_claude_usage`) captures `rate_limits` +
per-render `session_id` + `cost.*` from Claude Code's statusline JSON, into
`events/claude/usage/events.jsonl` (renamed from `events/claude/rate_limits/`
since the file is no longer rate-limit-only). The event `type` is
`cost_snapshot`. The writer no longer skips emission when only `cost` is
present (no `rate_limits`), so cost tracking now works for direct
`ANTHROPIC_API_KEY` users -- Claude Code doesn't emit `rate_limits` for them
(it's Pro/Max only), but `cost` is always present. The writer script is
named `claude_usage_writer.sh` and reads `$MNGR_USAGE_EVENTS_PATH` for
the test override.

## 2026-05-12

- Events are appended by a per-agent statusline shim (in the `mngr_claude_usage` plugin) that captures the JSON snapshot Claude Code feeds to its statusline command on every render. The shim composes with any pre-existing user `statusLine.command` (the user's command runs after ours via `MNGR_USER_STATUSLINE_CMD`). All provisioning file I/O goes through `host.read_text_file` / `host.write_file`, so the shim works for local and remote agents (Modal, vps_docker, lima, ...) uniformly.

The Claude writer now also emits `window_seconds` per fixed-duration
window (`five_hour=18000`, `seven_day=604800`), enabling the reader to
derive `elapsed_seconds` / `elapsed_percentage` per window. These new
fields are surfaced in `mngr usage --format json` output (alongside the
existing `seconds_until_reset`) and are available to `mngr usage wait`
CEL predicates. Variable-duration windows (Claude's overage) intentionally
omit `window_seconds`, so the derived fields are `null` there.
