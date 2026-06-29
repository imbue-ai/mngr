# Unabridged Changelog - mngr_opencode_usage

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_opencode_usage/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-19

Trimmed the README to user-relevant content and tightened it for concision.

## 2026-06-16

Corrected the package version back to 0.1.0 (it had been bumped to 0.1.1, but that version was never actually released).

## 2026-06-16

New package `imbue-mngr-opencode-usage`: cost/usage tracking for OpenCode agents in `mngr usage`. It installs a second in-process OpenCode plugin (alongside the lifecycle one) that appends one `cost_snapshot` event per assistant message -- OpenCode's own per-message cost (reported, not estimated) plus tokens and the provider-qualified model -- to `events/opencode/usage/events.jsonl`. A reader hookimpl claims the `opencode` source and aggregates it with the session-incremental strategy (sum each session's messages). After provisioning an OpenCode agent, its spend now shows up in `mngr usage` like Claude's.
