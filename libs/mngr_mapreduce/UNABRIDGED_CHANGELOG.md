# Unabridged Changelog - mngr_mapreduce

This file contains the full, verbatim per-PR entries for the `mngr_mapreduce` library. For the curated summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-28

Introduces `mngr_mapreduce`, a Python framework that generalizes the test-fanout pattern previously baked into `mngr_tmr` into a reusable map-reduce engine. Recipes subclass `MapReduceRecipe` to plug in discovery (`discover`), per-task prompts (`build_mapper_prompt`), the reducer prompt (`build_reducer_prompt`), and post-extraction hooks (`on_mapper_finalized`, `on_reducer_finalized`). The framework handles agent launching (with snapshot/host-pool support), polling, outputs-archive extraction, and report rendering/upload; the framework is content-agnostic, treating each agent's `outputs.tar.gz` as opaque and handing the extracted directory to the recipe for interpretation.

## [Unreleased]
