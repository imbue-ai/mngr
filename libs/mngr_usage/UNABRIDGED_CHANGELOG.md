# Unabridged Changelog - mngr_usage

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_usage/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
