# Changelog - mngr_autocompact

A concise, human-friendly summary of changes for the `mngr_autocompact` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.0] - 2026-10-22

### Added

- Added: New `mngr_autocompact` plugin: in `on_next_prompt` mode it compacts a stale agent right before a message is sent to it, and the `mngr autocompact` command group (`check` lists agents needing compaction, `run` compacts them) serves agents in `proactive_timer` mode that have sat idle until shortly before their prompt cache expires and whose context reaches `min_context_tokens` (default 200,000). Both commands take multiple agent targets (resolved like `mngr stop`) or `--all`, and skip targets without compaction support with a warning.
- Added: Python API `compact_stale_agents`, `compact_agent_if_stale`, and `compact_stale_agents_by_name(mngr_ctx, names, now=None)` (in `imbue.mngr_autocompact.manager`) for code that embeds mngr; the by-name entry point skips names it cannot use and hosts that fail to load instead of raising.
- Added: A compaction waits at most `COMPACTION_MESSAGE_LOCK_TIMEOUT_SECONDS` for the agent's message lock and re-checks that the agent is still idle before sending `/compact`; a busy or just-messaged agent is skipped until a later pass.
