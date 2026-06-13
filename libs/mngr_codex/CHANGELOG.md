# Changelog - mngr_codex

A concise, human-friendly summary of changes for the `mngr_codex` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: real `codex` agent-type support as its own plugin (`imbue-mngr-codex`), wiring OpenAI's Codex CLI into mngr and replacing the previous in-core `BaseAgent` stub. Each agent runs under its own per-agent `CODEX_HOME` for isolated config, sessions, and transcripts, while sharing authentication through a write-through `auth.json` symlink so logging in once authenticates every agent. Codex agents report RUNNING while working and WAITING when idle via `UserPromptSubmit`/`Stop` lifecycle hooks, with subagent-aware gating so a root agent stays RUNNING while a subagent it launched is still working. Includes conversation resume across stop/start, common transcripts readable by `mngr transcript`, and seeded trust/onboarding for a silent first launch.
