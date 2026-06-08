# Changelog - mngr_usage

A concise, human-friendly summary of changes for the `mngr_usage` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

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
