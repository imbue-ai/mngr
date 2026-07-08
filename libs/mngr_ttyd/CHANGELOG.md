# Changelog - mngr_ttyd

A concise, human-friendly summary of changes to the `mngr_ttyd` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: ttyd web-terminal attach now targets the agent's primary tmux window by name (`tmux.primary_window_name`, default `agent`, read from `MNGR_PRIMARY_WINDOW_NAME`) instead of the literal `:0` index, so attaching to an agent's terminal in the browser works regardless of the user's tmux `base-index`.

### Fixed

- Fixed: Copy-paste in the browser terminal. The plugin now serves its own OSC 52-capable web client to the stock `ttyd` binary via `ttyd -I`, so copying text inside a tmux session running in the browser terminal reaches the system clipboard (the released `ttyd` 1.7.7 client has no OSC 52 handler). `mouse on` is kept so mouse-wheel scroll and in-app mouse continue to work. The client is vendored gzip-compressed and decompressed onto each agent host during provisioning; if missing, `ttyd` cleanly falls back to its built-in client.

## [v0.1.14] - 2026-06-18

## [v0.1.13] - 2026-06-16

## [v0.1.12] - 2026-06-16

## [v0.1.11] - 2026-06-15

## [v0.1.10] - 2026-06-13

## [v0.1.9] - 2026-06-08

## [v0.1.8] - 2026-06-05

## [v0.1.7] - 2026-06-01

## [v0.1.6] - 2026-05-28

### Fixed

- Fixed: `resources/ttyd_agent.sh` now uses `=$_SESSION:0` (the `=` exact-match prefix) when attaching to a named agent via URL arg, so the browser ttyd window no longer silently lands on a sibling-prefix session.
