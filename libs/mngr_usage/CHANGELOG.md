# Changelog - mngr_usage

A concise, human-friendly summary of changes for the `mngr_usage` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.4] - 2026-06-16

## [v0.1.3] - 2026-06-15

## [v0.1.2] - 2026-06-13

### Added

- Added: `mngr usage` and `mngr usage wait` now preserve and read back usage from destroyed agents. When an agent (or its whole host) is destroyed, its `events/<source>/usage` directories (plus its `data.json`, for filtering) are copied to `<local_host_dir>/preserved/<agent-name>--<agent-id>/` before the state directory is deleted — the same place `mngr_claude` preserves session files. For remote agents the files are pulled to the local machine so they survive host destruction. By default `mngr usage` (and `mngr usage wait`) fold preserved usage back into their output so destroyed agents' spend still counts toward cost totals and rate-limit windows; preserved agents honor the same `--provider` / `--project` / `--local` / label / CEL filters as live agents (evaluated against their preserved `data.json`). A new `preserve_on_destroy` usage-plugin config option (default `true`) controls preservation; pass `--no-preserved` to consider only live agents.

## [v0.1.1] - 2026-06-08

### Added

- Added: macOS LaunchAgent section in `docs/cron_recipes.md` as the recommended alternative to `cron` on macOS. cron jobs run outside the GUI (Aqua) login session and can't reach the login Keychain (where Claude Code stores credentials), so cron-launched agents come up "Not logged in". A user LaunchAgent loaded into the Aqua session has Keychain access and authenticates normally. Includes a plist skeleton (`StartInterval`, `EnvironmentVariables` PATH, log paths), `launchctl bootstrap`/`bootout` commands, and the runs-only-while-logged-in tradeoff.
- Added: Auto-discovered as a publishable package by the release tooling; will be offered for first publication to PyPI on the next release.

### Changed

- Changed: Cron automation recipes doc (`docs/cron_recipes.md`) refined — `warm-window.sh` and `dispatch-task.sh` now `cd "$PROJECT_DIR"` before creating an agent (cron starts in `$HOME`, usually not a git repo, and mngr resolves project-scoped config from the cwd's git worktree root); dropped now-redundant `--from ":$PROJECT_DIR"` from `dispatch-task.sh`; clarified that `warm-window.sh` does no real work so its `PROJECT_DIR` can be any git repo already trusted in Claude Code. Reworked the Scheduling section so the `PATH` note covers both Linux (`/usr/bin` via apt) and macOS (`/opt/homebrew/bin` via Homebrew) around a single cron example.

## [v0.1.0] - 2026-06-05

### Added

- Added: `mngr usage` per-session cost aggregation with separate `subscription_cost` / `api_cost` aggregates, `--since`, `--detail`, and CEL/format-template surfaces; cost tracking now works for direct `ANTHROPIC_API_KEY` users.
- Added: New `docs/cron_recipes.md` "cron automation recipes" doc (linked from the README) with worked examples of driving `mngr` from cron using check mode (`mngr usage --format json`) rather than the blocking `mngr usage wait`, plus a shared `spare-capacity.sh` helper (exit 0 when the 5h window still has budget and the week is under pace). Worked examples cover: using up an about-to-expire 5h window via a dedicated agent's full lifecycle, warming a fresh 5h window early so it resets partway through your work, and dispatching a queue of task files capped by a shared `queue=live` label.
- Added: The usage plugin contributes its cron-recipes documentation as a `mngr help usage_cron_recipes` topic via mngr's new `register_help_topics` hook. The topic body is the plugin's `cron_recipes.md`, shipped inside the wheel via `force-include`, and its `DocFile` carries a GitHub `source_url` so relative links (e.g. `[Waiting on a predicate](../README.md#waiting-on-a-predicate)`) are rewritten to clickable absolute GitHub URLs in an interactive terminal.

### Changed

- Changed: Added to the release tooling's publish graph (`scripts/utils.py`); will be offered for first publication to PyPI on the next release. Stale `imbue-mngr==0.2.6` pin in `pyproject.toml` is realigned to the current `0.2.10`. No runtime change.

## [v0.2.8] - 2026-05-13

### Added

- Added: New `mngr usage` command reporting Claude Code's rolling 5h / 7d / overage quota usage with `human`/`json`/`jsonl` formats and the standard agent-filter flags.
- Added: New `mngr usage wait --until <CEL>` command that blocks until a usage snapshot matches a predicate, with exit codes mirroring `mngr wait`.
