# Changelog - mngr_claude_subagent_proxy

A concise, human-friendly summary of changes for the `mngr_claude_subagent_proxy` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: The plugin's own hooks (PROXY spawn/cleanup/reap, DENY deny/reap, proxy-child permission auto-allow) now go into the per-agent config-dir `settings.json` instead of the project's `.claude/settings.local.json`, matching where `mngr_claude` now bakes its own hooks. The plugin still operates on `settings.local.json` for the one thing that genuinely concerns the user's hooks — wrapping user-defined Stop/SubagentStop commands with the `MNGR_CLAUDE_SUBAGENT_PROXY_CHILD` guard so they no-op inside spawned proxy children. The gitignore-acceptance check that used to apply to every Claude agent now only fires here, immediately before that wrap is written.
- Changed: `mngr_claude_subagent_proxy` typed `subagent_type` (e.g. `imbue-code-guardian:verify-and-fix`) now preserves Claude Code's system-prompt contract in both PROXY and DENY modes by resolving on-disk agent definitions.
- Changed: Destroyed-agent transcript fallback now reads the preserved common transcript from its new location at `preserved/<name>--<id>/events/claude/common_transcript/events.jsonl` (via the shared `get_preserved_agent_dir` helper), reflecting `mngr_claude`'s switch to the unified `preserve_agent_data` layout; the former `plugin/mngr_claude/preserved_sessions/<name>--<id>/common_transcript/events.jsonl` path is no longer consulted.
- Changed: Plugin is now **disabled by default**; it only loads when a config layer sets `[plugins.claude_subagent_proxy] enabled = true`. Inverts the usual plugin default because this plugin is experimental and intercepts Claude Code's built-in `Task` tool.
- Changed: Provisioning artifacts moved under `mngr-proxy/` subdirs -- PROXY-mode agent at `.claude/agents/mngr-proxy/proxy.md` and DENY-mode skill at `.claude/skills/mngr-proxy/SKILL.md` (renamed from `mngr-subagents`); each path is covered by a single `.gitignore` line. Discovery is unaffected (Claude Code identifies the subagent by its frontmatter `name:` field).
- Changed: At provisioning, the plugin now refuses to write either artifact into a git-tracked worktree where the path is not gitignored, raising a clear error instead of leaving an untracked file. The error tells you to either gitignore the path or disable the plugin for the repository (`mngr config set --scope project plugins.claude_subagent_proxy.enabled false`).

## [v0.2.8] - 2026-05-13

### Added

- Added: New experimental `mngr_claude_subagent_proxy` plugin that reroutes Claude Code's `Task` tool through mngr-managed subagents, with `PROXY` (default) and `DENY` modes and a `mngr-subagents` Claude skill teaching the explicit two-command spawn-and-wait protocol.

### Changed

- Changed: `[plugins.claude_subagent_proxy]` is disabled in the project-level `.mngr/settings.toml`; the `mngr-subagents` skill no longer recommends `--reuse` on `mngr create` so slug collisions surface as a hard error.
