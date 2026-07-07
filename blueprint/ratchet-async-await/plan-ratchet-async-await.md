# Plan: Ratchet against `async def` and `await`

> Ratchet against `async def` and `await` across the monorepo, to freeze and gradually reduce async usage. We strongly prefer sync code — it's far easier to debug, and our software is low-scale so async buys us nothing.
> * **One combined regex ratchet** (`check_async_await`) matching either `async def` or `await`, with a single shared `rule_description` explaining that async is strongly discouraged (hard to debug, no scale benefit, prefer sync in almost all cases).
> * Built on the existing common-ratchet infrastructure: `RegexRatchetRule` in `common_ratchets.py` + wrapper in `standard_ratchet_checks.py` + `sync_common_ratchets.py` fan-out to all `libs/*` and `apps/*` `test_ratchets.py`.
> * **Includes all files** (test + production), like the existing `PREVENT_ASYNCIO_IMPORT` ratchet; baselines pinned per-project via inline-snapshot.
> * **Dead-simple regex**, no comment/string handling — any false positives are absorbed into the per-project baselines.
> * **Per-project only** (`libs/*` + `apps/*`); no repo-wide `scripts/` coverage added.
> * **`mngr_robinhood` is exempted entirely** — its `test_prevent_async_await` is a no-op `pass` (it wraps a fundamentally-async SDK). Every other project gets a real ratcheting cap.
> * **Changelog**: one per-PR entry in every touched project (strict adherence to the one-entry-per-project rule).

## Overview

- We are codifying an existing, strong style preference into an automated check: **sync code is preferred over async almost everywhere**. Async is hard to debug and brings no benefit at our scale.
- This complements the existing `PREVENT_ASYNCIO_IMPORT` ratchet (which bans `import asyncio`) by also freezing the language-level constructs `async def` and `await`, regardless of which async runtime backs them (anyio, the Claude Agent SDK, etc.).
- We reuse the established three-layer ratchet infrastructure (rule definition → wrapper function → per-project `test_ratchets.py`) and the `sync_common_ratchets.py` fan-out, so the new check appears uniformly across all projects and is enforced by `test_meta_ratchets.py`.
- A ratchet freezes the *current* count and only allows it to drop. Existing async code keeps passing; any newly added `async def`/`await` fails the project's ratchet test until removed or until the baseline is deliberately raised.
- `mngr_robinhood` is the one project that fundamentally wraps an async SDK, so it is exempted from the cap entirely while still defining the test (required by the meta-test that all projects share the same ratchet test names).

## Expected behavior

- Adding a new `async def` or `await` anywhere in `libs/*` or `apps/*` (except `mngr_robinhood`) makes that project's `test_prevent_async_await` fail, with a message explaining that sync code is strongly preferred and why.
- Removing async usage is always allowed; after removal the baseline can be tightened via inline-snapshot trim.
- The check scans **all** Python files in a project — production *and* test files — mirroring `PREVENT_ASYNCIO_IMPORT`.
- Each project's failure threshold equals its current `async def` + `await` count, captured as an inline-snapshot baseline (counts in the low hundreds repo-wide; most concentrated in `mngr_robinhood`, which is exempt).
- `mngr_robinhood`'s `test_prevent_async_await` always passes (no-op `pass` body) — its heavy, intrinsic async usage neither fails CI nor needs a baseline number.
- `test_meta_ratchets.py::test_all_test_ratchets_files_have_same_tests` continues to pass because every project (including `mngr_robinhood`) defines a `test_prevent_async_await` function.
- The dead-simple regex may match the words `async def` / `await` inside comments or strings; any such matches are folded into the per-project baseline and do not need special handling.
- `scripts/` and other repo-root files are not covered (no async-related growth is expected there, and the per-project mechanism does not reach them).

## Changes

- **Define the rule** (`libs/imbue_common/.../ratchet_testing/common_ratchets.py`): add a single `RegexRatchetRule` (e.g. `PREVENT_ASYNC_AWAIT`) under the "Banned libraries and patterns" section. Its pattern matches either `async def` or a bare `await`; its `rule_description` states that async is strongly discouraged — sync code is far easier to debug, our software is low-scale so async provides no benefit, and there are almost no valid exceptions.
- **Add the wrapper** (`libs/imbue_common/.../ratchet_testing/standard_ratchet_checks.py`): add `check_async_await(source_dir, max_count)` delegating to the standard `assert_ratchet` helper, and import the new rule. The function name (`check_async_await`) drives the generated test name (`test_prevent_async_await`) and its position under the matching section header.
- **Fan out to all projects**: run `scripts/sync_common_ratchets.py` to insert `test_prevent_async_await` into every `libs/*` and `apps/*` `test_ratchets.py` (default `snapshot(0)` body), then set real baselines with the inline-snapshot update across the ratchet tests.
- **Exempt `mngr_robinhood`**: manually replace the generated body of its `test_prevent_async_await` with a no-op `pass` plus a short comment explaining the exemption (the project wraps a fundamentally-async SDK). The sync script only *adds* missing tests and never overwrites an existing body, so this edit is stable across future syncs.
- **Changelog**: add one per-PR changelog entry (`<project>/changelog/<branch>.md`) in every project the PR touches — `imbue_common` (rule + wrapper), the `dev` bucket (the `scripts/sync_common_ratchets.py` run is mechanical, but any root-level edit belongs to `dev`), and each `libs/*` / `apps/*` project whose `test_ratchets.py` gains the new test.
- **Verification**: run the full ratchet and meta-ratchet suites to confirm baselines are correct, the meta-test still sees a uniform set of ratchet tests, and `mngr_robinhood`'s exemption holds.

## Notes / non-decisions

- `async with` and `async for` are not separately matched: both can only appear inside an `async def`, which the ratchet already catches, so covering `async def` transitively covers them.
- The regex approach matches the existing simple banned-import ratchets and is reliable for `async def`; `\bawait\b` does not match `awaitable` (word boundary), so the main residual false-positive source is the literal words in comments/strings, intentionally absorbed into baselines.
