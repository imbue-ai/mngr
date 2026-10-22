# Unabridged Changelog - mngr_usage

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_usage/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-07

Price Claude Sonnet 5.5 (`anthropic/claude-sonnet-5-5`), Claude Haiku 5.5 (`anthropic/claude-haiku-5-5`) and GPT-6.1 Sol (`openai/gpt-6.1-sol`). default-workspace-template's new pins make them what a chat's Sonnet and Haiku entries run and the model every new codex agent starts on, so token-only usage on any of them was unpriced. The rates are litellm's published ones, and `sync_litellm_prices --check` corroborates all three: Sonnet 5.5 and GPT-6.1 Sol at $2/$10 per MTok with cache reads at $0.10 and 5-minute cache writes at $2.50, and Haiku 5.5 at $0.10/$0.50 with cache reads at $0.01 and cache writes at $0.125.

Two limits of the flat table. Haiku 5.5 bills prompts over 100K tokens at 5x those rates, which the table cannot express, so a long-context Haiku 5.5 request is priced at the lower rate. And litellm prices the Sonnet 5.5 cache read at $0.10 per MTok, while Claude Code's own model catalog puts Sonnet 5.5 in the same $2/$10 tier as Sonnet 5, whose cache read is $0.20; the row keeps litellm's figure, because the daily price sync would otherwise rewrite it. Claude Code reports its own cost, so for claude sessions this table is only a fallback.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-09-30

Removed the 5 comment lines that had accumulated inside `libs/mngr_usage/imbue/mngr_usage/test_ratchets.py` explaining why individual ratchet counts had been raised.

A ratchet file records counts, not history. The reason a count moved now goes in the commit message and the changelog entry, the reason a particular code site is allowed goes in that module's docstring, and the set of exempt files goes in the `excluded_patterns=(...)` argument -- all places that stay correct as the code changes, which the comments did not.

## 2026-09-25

Per-token prices now live in a committed table, `model_prices.toml`, that ships beside the pricing code instead of being written inline in `pricing.py`. What each model costs is unchanged; where the number comes from is.

The table used to be pinned by a test that compared it to litellm's `model_prices_and_context_window` map. litellm resolves that map by fetching its own main branch whenever it is imported, and that file is edited hundreds of times a month, so the test failed whenever an unrelated upstream edit landed -- and failed differently again if the fetch itself failed, because litellm then silently answers from the older copy bundled in its wheel. That test is gone. `scripts/sync_litellm_prices.py` reconciles the table against litellm's published map on a daily schedule and opens a pull request when a rate has moved, so a price change arrives as a reviewed diff.

The nine models dropped from the table last week are priced again: `anthropic/claude-opus-4-1`, `anthropic/claude-opus-4-20250514`, `anthropic/claude-sonnet-4-20250514`, `openai/gpt-5-codex`, `openai/gpt-5.1-codex`, `openai/gpt-5.1-codex-max`, `openai/gpt-5.1-codex-mini`, `openai/gpt-5.2-codex` and `openai/codex-mini-latest`. litellm had retired them from its catalog, which is not the same as the provider ceasing to bill for them -- Codex still reports several of these ids, and because cost is derived from stored token counts at read time, removing a rate also un-priced every session already recorded under it. Usage on these models reports an estimated cost again rather than no cost at all.

A rate of zero is now rejected outright when the table is read. litellm answers 0 for a model whose entry it has pruned, rather than raising, so accepting one would have recorded the model as free instead of as unpriced.

Supersedes the narrower fix that landed on main in the meantime (#1241), which kept the live-map test but exempted models litellm had retired from its price check. That stopped a *removal* upstream from breaking the build; a *restatement* of a price litellm still lists would still have broken it, on a commit belonging to nobody here. `anthropic/claude-opus-5-5` and its fast-mode membership carry over from that work unchanged.

## 2026-09-24

The usage pricing table may now keep entries for models litellm has retired from its `model_prices_and_context_window` map, so `mngr usage` can still price older sessions that ran on them. `litellm_pricing_test` checks prices only for models litellm still lists; an entry litellm no longer lists must still be in the map bundled with the pinned litellm, so an entry for a model litellm never listed still fails. A model litellm drops from its map (e.g. `openai/o4-mini`, which shuts down on 2026-10-23) no longer turns CI red, as long as the pinned litellm's bundled map has it.

Price Claude Opus 5.5. The Claude Code pin moving to 2.1.280 repoints the `opus` alias at `claude-opus-5-5`, which had no entry in `MODEL_PRICING` and no place in `FAST_MODE_MODELS`, so a token-only usage record on it would have logged "Missing pricing" and shown unpriced. Claude Code reports its own cost, so this table is its fallback rather than its main path; both now carry the model.

Opus 5.5 gets its own `PerTokenPrices` rather than sharing the Opus 5 generation's: it is cheaper ($4/$20 per MTok against $5/$25) and cuts the cache read to 0.05x the input rate where the rest of the family charges 0.1x. The numbers are litellm's, as `litellm_pricing_test` requires.

## 2026-09-22

Drop the nine usage-pricing entries litellm retired from its `model_prices_and_context_window` map: `anthropic/claude-opus-4-1`, `anthropic/claude-opus-4-20250514`, `anthropic/claude-sonnet-4-20250514`, `openai/gpt-5-codex`, `openai/gpt-5.1-codex`, `openai/gpt-5.1-codex-max`, `openai/gpt-5.1-codex-mini`, `openai/gpt-5.2-codex` and `openai/codex-mini-latest`. The table is pinned to that map, so these entries failed its pricing test once litellm removed them. Usage recorded under one of these models now reports as unpriced instead of carrying an estimated cost.

## 2026-09-21

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-09-14

Usage preservation now shares mngr's host-aware archive destination selection, preventing known cross-host agent identity collisions from overwriting an existing preserved archive.

New usage archives record identity and copy outcomes in the shared core manifest instead of a separate usage metadata sidecar. Usage and usage-wait discovery read the manifest, retain legacy archive support, and continue including preserved spend by default.

Destroying an agent no longer walks the host's agent listing to resolve its provider, so usage preservation costs one fewer remote round trip per destroy. A preserved `data.json` that names no usable agent is now reported rather than silently dropped, which would otherwise leave that agent's spend out of `mngr usage` with no explanation.

## 2026-09-11

Add `anthropic/claude-fable-5-1` (Claude Fable 5.1) and `openai/gpt-6-astra` (GPT-6 Astra) to the usage pricing table, so a usage source that reports either model's tokens without a dollar figure is priced rather than reported unpriced. Both entries are pinned against litellm's `model_prices_and_context_window` map like the rest of the table.

Fable 5.1 keeps Fable 5's rates except for cache reads, which are a quarter of the price. GPT-6 Astra is the first OpenAI entry the map bills a cache write for, so the litellm pin's OpenAI half now compares that bucket against the map instead of asserting it is zero.

## 2026-08-19

Cost can now be computed at fast-mode rates.

Fast mode returns the same tokens faster for twice the price ($10/$50 per MTok against $5/$25 on Claude Opus 5 and Claude Opus 4.8, across the full context window), and it is chosen per request rather than per model -- the same model id bills at either rate depending on how the request asked. The pricing table held only the standard rate, so any fast-mode traffic priced through it came out at half of what it actually cost, with nothing to indicate that.

`compute_cost` takes an `is_fast_mode` flag that applies `FAST_MODE_PRICE_MULTIPLIER` (2x) to the model's standard rates. Every bucket doubles, not just input and output: the cache rates are multiples of the input rate (a cache write costs 1.25x an input token, a read 0.1x), so doubling the input rate carries them along. Only Opus 5 and Opus 4.8 can serve fast mode, listed in `FAST_MODE_MODELS`; a model outside that set reports null rather than falling back to the standard rate, since silently halving a real bill is worse than reporting the number as unavailable. The default is unchanged, so existing callers keep their current behaviour -- and a caller that cannot observe which tier served a request necessarily prices it standard, which is a floor rather than a figure.

Added Claude Opus 5 to the standard pricing table, which was missing it entirely -- usage reported against that model priced as unknown.

Estimated costs price every prompt-cache write at the 5-minute-TTL rate (1.25x an input token). Anthropic bills a 1-hour write at 2x, and the token buckets carry no TTL, so a cost estimate over 1-hour-cached traffic understates that bucket by 37.5%. This is now stated where the rate is applied and where the bucket is defined; correcting it needs the TTL carried through from every writer.

Add `anthropic/claude-opus-5` to the usage pricing table, at the current Opus tier ($5 / $25 per 1M input / output tokens), so a usage source that reports Claude Opus 5 tokens without a dollar figure is priced rather than reported unpriced.

Pin the Anthropic entries directly against litellm's `model_prices_and_context_window` map, the same map the LiteLLM proxy bills from. They were previously pinned against the proxy's inline price table, which no longer exists -- the proxy now takes pricing from litellm's map rather than carrying its own copy. OpenAI entries were already pinned this way; both providers now answer to the same source, which also means the new Opus 5 entry is checked against it rather than hand-verified.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-14

Add `anthropic/claude-fable-5` to the usage pricing table ($10 / $50 per 1M input / output tokens), so token-only usage sources report cost for Claude Fable 5 instead of showing it as unpriced.

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-19

Removed the now-vestigial `UsagePluginConfig.merge_with` override; the config merge is routed through the overlay pipeline, which reproduces the same assign-by-default semantics. No user-visible behavior change.

Trimmed the README to user-relevant content and tightened it for concision.

## 2026-06-16

Began generalizing `mngr usage` beyond Claude (groundwork; no user-visible change yet).

Added a `TokenSnapshot` data type and a `pricing` module (`compute_cost`) so usage sources that report token counts rather than a pre-computed dollar cost can be priced centrally by the reader. The Anthropic prices mirror `apps/modal_litellm` (and were independently confirmed against a live pi session); the OpenAI / Codex prices (`gpt-5.x`, `*-codex`, `o3`/`o4-mini`, ...) are mirrored from litellm directly and pinned by a `litellm_pricing_test` (OpenAI has no cache-write surcharge, so cache-creation cost is 0). An unknown model resolves to no estimate rather than a misleading `$0`.

Moved per-source aggregation behind a reader hook (`aggregate_usage_source`, contributed via `register_hookspecs`) so a usage plugin can ship its own reader. `mngr_usage` now provides three reusable aggregation utilities: `aggregate_process_cumulative` (the existing Claude strategy, where one cost counter spans `session_id`s), `aggregate_session_cumulative` (each `session_id` is its own cumulative counter -- e.g. Codex), and `aggregate_session_incremental` (each event reports one message's own cost/tokens, summed per session -- e.g. OpenCode and pi). The session strategies derive cost from tokens when the harness reports none, flagging each session `REPORTED` vs `ESTIMATED`. The reader dispatches to whichever plugin claims a source, falling back to process-cumulative. `SessionCostRecord` gained `tokens`, `model`, and `cost_provenance` fields.

The `mngr usage` JSON / CEL surface now exposes the token side too, mirroring the cost split: `subscription_tokens` / `api_tokens` aggregates, an `is_estimated` flag inside each `*_cost` block (true when any contributing session's dollars were token-derived rather than harness-reported), and per-session `tokens` / `model` / `cost_provenance`. The human `api cost:` line is marked `(estimated)` when its dollars were derived from tokens, and the `subscription cost:` line is marked `(imputed, estimated)` in the same case.

Removed the now-unused `aggregate_events_to_snapshots` from the production reader (the dispatch path supersedes it); it survives only as a test helper.

Refactored the reader internals (no user-visible behavior change, except one new error): the three per-source walkers now operate on a typed `UsageEvent` instead of repeating `event.get(...)` + `isinstance` guards, with the "drop events without a usable timestamp or session_id" rule centralized in one parser -- which keeps the window/session filtering consistent across strategies. A window key that collides with a reserved source-level CEL field (e.g. a writer emitting a window named `api_cost`) now raises instead of silently clobbering that field. Optional-arithmetic helpers were consolidated into `add_optional` / `sub_optional` in `data_types`. The `gather_usage_snapshots` / `wait_for_usage` library functions no longer carry default arguments (clock, filters, recency window, ...); callers pass every parameter explicitly.

The reader hook (`aggregate_usage_source`) and the shared `aggregate_*` functions now take typed `UsageEvent`s (parsed once at the read boundary via the new public `parse_usage_events`) rather than raw event dicts; `UsageEvent` moved to `data_types`, and reading an events file (`parse_events_from_content`) now yields typed events directly.

Cron recipes: the "dispatch tasks from a queue directory" recipe now creates each agent on its own fresh branch off `main` (`mngr create --from ":$PROJECT_DIR" --branch main:`), so concurrent tasks never share a working branch. Also documented the random-selection variant (swap `sort` for `sort -R`).

## 2026-06-15

## wait_for_usage: own daemon poll loop instead of injected sleep

- `wait_for_usage` no longer takes injected `monotonic_fn` / `sleep_fn` callables (which had aliased `time.sleep` to dodge the `time_sleep` ratchet while still sleeping for real). It now owns a real `time.sleep` directly -- the single sanctioned sleep in this package -- because it is a background/daemon wait that deliberately supports `timeout_seconds=None` (run indefinitely until the usage predicate flips). That no-timeout case is why it does not use the shared `poll_until`, which requires an explicit timeout on purpose. `now_fn` (the wall-clock value fed into the CEL context) is unchanged. No behavior change for callers; purely internal.

Rename the `--max-age` flag (and the `max_age_seconds` plugin-config option) to `--stale-after` (`stale_after_seconds`), so the name reflects that it only controls the snapshot stale-warning threshold and is not an event-age filter. No alias is kept for the old name.

README: add a "Filtering by event age" section documenting `--since` as the way to bound the per-session cost aggregation by event age, and clarifying that `--stale-after` is a stale-warning threshold, not a filter.

`mngr usage --help`: move `--since`, `--stale-after`, `--detail`, and `--preserved/--no-preserved` out of "Ungrouped". `--since` and `--preserved/--no-preserved` now render under the existing "Filtering" group (matching `mngr usage wait`, where `--preserved/--no-preserved` already lives); `--stale-after` and `--detail` render under a new "Display" group (matching the convention in `mngr transcript` / `mngr events`).

`mngr usage --help` synopsis: enumerate the options unique to `mngr usage` (`--stale-after`, `--detail`, `--since`, `--no-preserved`) instead of the placeholder `[OPTIONS] [COMMAND]`, matching the style used by `mngr usage wait` and other `mngr` commands (which omit shared filter options like `--include` / `--provider`).

## 2026-06-12

Internal: import `get_agent_state_dir_path` from its new canonical location `imbue.mngr.hosts.common` (relocated there from `imbue.mngr.hosts.host` to avoid circular-import issues). No behavior change.

## 2026-06-10

Raised the stale coverage floor from 80% to 90% to match the coverage CI already measures (~93%).

## 2026-06-09

Fixed a type error introduced when an older branch merged into main: `mngr_usage`'s usage-preservation code referenced `VolumeFileType`, which had been renamed to `FileType` in `imbue.mngr.interfaces.data_types`. Updated the import and references to use `FileType`.

`mngr usage` now preserves and reads back usage from destroyed agents.

When an agent (or its whole host) is destroyed, its `events/<source>/usage` directories (plus its `data.json`, for filtering) are now copied to `<local_host_dir>/preserved/<agent-name>--<agent-id>/` before the state directory is deleted -- the same place `mngr claude` preserves session files. For remote agents the files are pulled to the local machine so they survive host destruction. This is controlled by the new `preserve_on_destroy` usage-plugin config option (default `true`; set to `false` to discard usage data on destroy).

`mngr usage` (and `mngr usage wait`) now fold this preserved usage back into their output by default, so destroyed agents' spend still counts toward cost totals and rate-limit windows. Preserved agents honor the same `--provider` / `--project` / `--local` / label / CEL filters as live agents (evaluated against their preserved `data.json`). Pass `--no-preserved` to consider only live agents.

## 2026-06-08

Refined the cron automation recipes doc (`docs/cron_recipes.md`):

- The agent-spawning recipes (`warm-window.sh`, `dispatch-task.sh`) now `cd
  "$PROJECT_DIR"` before creating an agent, using a placeholder project path.
  cron starts in `$HOME` (usually not a git repo), and mngr resolves
  project-scoped config from the cwd's git worktree root -- so running inside
  the project is what gives the new agent a git root to branch from and applies
  the project's settings (`create_templates`, labels, etc.). Dropped the now
  redundant `--from ":$PROJECT_DIR"` from `dispatch-task.sh` (cd makes the
  create source default to the project's git root). Clarified that the
  `warm-window.sh` warmer does no real work, so its `PROJECT_DIR` can be any git
  repo already trusted in Claude Code -- the project context is irrelevant, and
  `--no-connect` can't answer the trust prompt on first use.
- Reworked the Scheduling section: the `PATH` note now covers both the Linux
  (`/usr/bin` via apt) and macOS (`/opt/homebrew/bin` via Homebrew) dependency
  locations around a single cron example.
- Added a macOS LaunchAgent section as the recommended alternative to `cron` on
  macOS. cron jobs run outside the GUI (Aqua) login session and so can't reach
  the login Keychain, where Claude Code stores its credentials -- cron-launched
  agents come up "Not logged in". A user LaunchAgent loaded into the Aqua
  session has Keychain access and authenticates normally. Includes a plist
  skeleton (`StartInterval`, `EnvironmentVariables` PATH, log paths),
  `launchctl bootstrap`/`bootout` load/unload commands, and the
  runs-only-while-logged-in tradeoff.

- Now auto-discovered as a publishable package by the release tooling (it is a standalone `mngr usage` plugin with its own help-topic docs). It will be offered for first publication to PyPI on the next release. Its stale `imbue-mngr==0.2.6` pin is realigned to the current `0.2.10`. No runtime change.

## 2026-06-05

- Added to the release tooling's publish graph (`scripts/utils.py`). It will be offered for first publication to PyPI on the next release. Its stale `imbue-mngr==0.2.6` pin is realigned to the current `0.2.10`. No runtime change.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-02

Internal refactor with no user-visible behavior change. Updated the JSON output call sites to use the renamed `write_json_line` helper from `imbue.mngr.cli.output_helpers` (formerly `emit_final_json`, now removed).

## 2026-06-01

Added a "cron automation recipes" doc (`docs/cron_recipes.md`), linked from the
README, with worked examples of driving `mngr` from `cron` using check mode
(`mngr usage --format json`) rather than the blocking `mngr usage wait`, plus a
shared `spare-capacity.sh` helper (exit 0 when the 5h window still has budget and
the week is under pace):

- Use up an about-to-expire 5h window: one cron job owns a dedicated agent's whole
  lifecycle, starting it in the tail of an open 5h window when there's spare
  capacity and stopping it once the window rolls over or the week falls off pace.
- Warm a fresh 5h window early: when the last recorded window has elapsed, nudge a
  dedicated warming agent to fire one prompt and open the next window so it resets
  partway through your work rather than a full 5h later.
- Dispatch a queue of task files: launch an agent per task file from the project
  repo, only while there's spare capacity, capped by a shared `queue=live` label;
  finished agents are stopped and relabeled `queue=in-review` for later review.

The usage plugin contributes its cron-recipes documentation as a `mngr help` topic via mngr's `register_help_topics` hook. With the plugin installed, `mngr help` lists `usage_cron_recipes` ("mngr usage: Cron automation recipes") and `mngr help usage_cron_recipes` renders the cron automation recipes. The topic's body is the plugin's `cron_recipes.md`, now shipped inside the wheel (`force-include`) so it works in a PyPI install; the key and description are namespaced so they are unambiguous in the global topic list.

The `usage_cron_recipes` help topic's `DocFile` now carries a GitHub `source_url`, so when `mngr help usage_cron_recipes` is shown in an interactive terminal, its relative links (e.g. `[Waiting on a predicate](../README.md#waiting-on-a-predicate)`) are rewritten to clickable absolute GitHub URLs instead of dead relative targets.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

## 2026-05-14

## mngr usage: per-session cost aggregation across recent sessions

`mngr usage` now aggregates cost **per session** within a recency window
instead of just rendering the freshest event's reading, and keeps
**subscription** and **API-key** spend in separate aggregates so imputed
estimates never get lumped with real billable spend:

- Reader scans every line of each agent's events file (not just the last),
  partitions each agent's events into Claude Code processes via cost-drop
  detection (cost is process-cumulative; `/clear` doesn't reset it), and
  within each process builds a `SessionCostRecord` per session whose `cost`
  is its delta from the prior session's cumulative reading.
- Each session is tagged with a `cost_mode`: `SUBSCRIPTION` if any event in
  its Claude Code process carried `rate_limits` (Claude.ai Pro/Max --
  cost is imputed by Claude Code, the user actually pays a flat subscription)
  or `API_KEY` otherwise (direct `ANTHROPIC_API_KEY` -- cost is real
  billable spend).
- Sessions are filtered to those whose last event is within `--since`
  (default 24h, configurable per-invocation or via plugin config).
- Human output (default): one cost line per mode that contributed --
  `subscription cost (imputed): $X.YY ...` and/or `api cost: $X.YY ...`
  -- followed by the populated rate-limit window lines. Subscription is
  rendered first; either or both can be present.
- Human output with `--detail`: adds indented per-session lines (newest-first)
  between the cost lines and the window lines, each tagged `[sub]` or `[api]`.
- JSON output (default): `source.subscription_cost.*` and `source.api_cost.*`
  are the per-mode aggregates; `source.subscription_session_count`,
  `source.api_session_count`, and `source.session_count` (total) are also
  exposed. There is intentionally **no** combined `source.cost` field.
  `sessions[]` is omitted unless `--detail` is set.
- JSON output with `--detail`: adds `source.sessions[]` (newest-first
  records, each carrying `cost_mode`).
- `mngr usage wait --until` CEL surface: `subscription_cost.total_cost_usd`
  and `api_cost.total_cost_usd` are the per-mode aggregates; no combined
  `cost` field exists. To predicate on a specific session, index
  `sessions[]` directly. New `--since` flag affects the aggregates.
- Format template: top-level `{subscription_cost.*}` / `{api_cost.*}` keys;
  the format-template surface intentionally doesn't expose per-session
  paths (use `--format json` if you need them).

Examples:

```
mngr usage --since 7d                                # aggregate over 7 days
mngr usage wait --until 'api_cost.total_cost_usd > 20'  # real billable spend crossed $20
mngr usage wait --until 'subscription_cost.total_cost_usd > 50'  # imputed >$50 of value
mngr usage wait --until 'sessions[0].cost.total_cost_usd > 5'  # most recent session only
```

## 2026-05-12

- New `mngr usage` command (in a new `mngr_usage` plugin) reports Claude Code's rolling 5h / 7d / overage quota usage. Supports the same output ergonomics as `mngr list`: `--format human`/`json`/`jsonl`, `--format` template strings like `'5h:{five_hour.used_percentage}/7d:{seven_day.used_percentage}'`, and the same agent-filter flags (`--include`, `--exclude`, `--local`, `--provider`, `--project`, ...). The command is a pure reader -- it incurs no Anthropic API charges.
- `mngr usage` discovers events by enumerating agents via `list_agents` and reading each agent's `events/<source>/rate_limits/events.jsonl` via the events API. The writer side is wired up via a single `on_before_provisioning` hookimpl on mngr core, with no Claude-specific hookspec.
- `mngr usage` prints an actionable hint when no rate-limit events are present, explaining that the most likely cause is agents provisioned before the plugin was active and pointing users at provisioning a fresh agent or re-provisioning an existing one.

Add `mngr usage wait`: block until a usage snapshot matches a CEL
predicate, then exit 0. Useful for composing with `mngr message` / `mngr
create` to launch new work once budget conditions are met (e.g. "75% of
the 5h window has elapsed and at most 50% of the limit has been used"):

```
mngr usage wait --until 'five_hour.elapsed_percentage > 75 && five_hour.used_percentage < 50' \
  && mngr message my-agent "ok, kick off the next batch"
```

The CEL context per source matches `mngr usage --format json`'s
`sources[i]`. Exit codes mirror `mngr wait` (0 matched, 1 error, 2
timeout); JSONL output uses the same `state_change` envelope as
`mngr wait` so downstream consumers see one consistent shape across
both wait commands. Restrict matching to a specific writer with the
top-level `source` field in CEL (e.g. `source == "claude"`). Default
poll interval is 30s.

Internal: shared exit-code constants moved from `mngr_wait.primitives`
to `mngr.cli.exit_codes`, callable from both `mngr_wait` and
`mngr_usage`.
