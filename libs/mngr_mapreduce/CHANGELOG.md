# Changelog - mngr_mapreduce

A concise, human-friendly summary of changes for the `mngr_mapreduce` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `mngr_mapreduce` framework, generalizing the test-fanout pattern previously baked into `mngr_tmr`. Recipes subclass `MapReduceRecipe` to plug in discovery, per-task prompts, the reducer prompt, and post-extraction hooks (`on_mapper_finalized`, `on_reducer_finalized`). The framework handles agent launching (with snapshot/host-pool support), polling, outputs-archive extraction, and report rendering/upload; it treats each agent's `outputs.tar.gz` as opaque.
