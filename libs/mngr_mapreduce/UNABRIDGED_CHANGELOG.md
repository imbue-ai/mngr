# Unabridged Changelog - mngr_mapreduce

This file contains the full, verbatim per-PR entries for the `mngr_mapreduce` library. For the curated summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-28

Removed the per-project `test_no_type_errors` and `test_no_ruff_errors` ratchet tests from `mngr_mapreduce`. These checks now run repo-wide from the root `test_meta_ratchets.py`, and the per-project copy imported `check_no_ruff_errors` (which was never centralized into `ratchet_testing.ratchets`), producing a ty `unresolved-import` error.

Introduces `mngr_mapreduce`, a Python framework that generalizes the test-fanout pattern previously baked into `mngr_tmr` into a reusable map-reduce engine. Recipes subclass `MapReduceRecipe` to plug in discovery (`discover`), per-task prompts (`build_mapper_prompt`), the reducer prompt (`build_reducer_prompt`), and post-extraction hooks (`on_mapper_finalized`, `on_reducer_finalized`). The framework handles agent launching (with snapshot/host-pool support), polling, outputs-archive extraction, and report rendering/upload; the framework is content-agnostic, treating each agent's `outputs.tar.gz` as opaque and handing the extracted directory to the recipe for interpretation.

### Migrate to the new `imbue.mngr.api.rsync` interface

`mngr_mapreduce`'s reducer-launch path now calls `rsync_to_remote` (from
`imbue.mngr.api.rsync`) instead of the removed `push_files` wrapper.
``extra_args`` replaces the dropped ``is_dry_run``/``is_delete`` parameters,
and the source path is passed with an explicit trailing ``/`` since mngr no
longer mangles slashes on the caller's behalf. Behavior is unchanged.

## [Unreleased]
