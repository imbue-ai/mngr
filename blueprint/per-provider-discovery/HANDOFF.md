# Per-provider discovery — handoff for remaining work

This branch (`mngr/per-provider-discovery`, PR #2335) makes mngr's provider discovery
**per-provider** instead of using a single global "full discovery snapshot", so that a
hung/slow provider can no longer block discovery of *all* providers.

> **STATUS (update): all three remaining items below are now DONE.**
> - **Item 1** (per-host/agent sub-timeouts + UNKNOWN): implemented via
>   `BoundedProviderDiscoveryResult` + `ProviderInstanceInterface.discover_hosts_and_agents_within_timeouts`
>   (per-host reads run on abandonable daemon threads; a host past its timeout is UNKNOWN). Batch
>   providers (modal/vps/imbue_cloud) override it to delegate to their batch path. The aggregator now
>   retains agents on UNKNOWN hosts. The intervening-event immediate-re-poll optimization was left
>   deferred (the correctness bug it would speed up is already fixed + tested by the span-aware aggregator).
> - **Item 2** (deprecate legacy): `FullDiscoverySnapshotEvent` / `DISCOVERY_FULL` are kept + deprecated
>   (old logs still parse); all live writers/producers removed; `mngr list` and `complete_names` migrated
>   to per-provider; `find_discovery_snapshot_replay_offset` added for attach/replay.
> - **Item 3** (forever-claude-template `system_interface`): rewritten onto `DiscoveryStateAggregator` on a
>   matching branch in `.external_worktrees/forever-claude-template` (verification deferred to re-vendor).
>
> The original instructions for each item are retained below for reference.

The historical context below describes the state when these items were still open.

Read first (re-gather context): `CLAUDE.md`, `style_guide.md`,
`blueprint/per-provider-discovery/plan-per-provider-discovery.md` (the spec + Q&A
decisions), and `libs/mngr/docs/architecture.md`.

---

## Spec / key design decisions (from the plan's Q&A)

- Replace the global `FullDiscoverySnapshotEvent` with a **per-provider snapshot event**, emitted independently as each provider finishes. **Remove the global snapshot entirely** (clean schema break — the user was emphatic: "STOP having the full_snapshot thing").
- Bound a hung provider *without killing threads*: warn after a first threshold, then emit a per-provider `DiscoveryError` after a longer error timeout; the abandoned thread keeps running and its late result is accepted on a later poll.
- Each provider polls on its **own decoupled loop** with per-provider configurable cadence.
- Apply the dual-timeout + per-provider treatment to both the streaming/observe pipeline and (eventually) the one-shot `mngr list` path.
- **Sub-provider timeouts**: per-host and per-agent discovery timeouts (default 30s), validated *below* the provider error timeout; a host/agent that exceeds its sub-timeout is marked **explicitly UNKNOWN** in the snapshot (distinct from destroyed/errored).
- **Span-aware reconciliation** (the latent-bug fix): each snapshot carries `discovery_started_at`/`discovery_finished_at`; a shared aggregator refuses to let an in-flight snapshot clobber a host/agent whose own state-change/destroy event landed during that span, and the producer should immediately re-poll that provider to converge.
- Defaults: poll **30s**, provider warn **20s**, provider error **120s**, host/agent **30s**.
- Shared reconciler is a stateful `DiscoveryStateAggregator` class delegating span/partition decisions to `@pure` helpers.
- Producer model: one `mngr observe --discovery-only` process, one decoupled poll loop (thread) per provider; a provider-set change still bounces the single process.

---

## What's DONE (Phases 1–5) — green + pushed + autofix-clean

Local test results: mngr discovery core 275 + 136, minds desktop_client 1179, mngr_forward+mngr_latchkey 639, config 118. Autofix verify-and-fix found no CRITICAL/MAJOR/MINOR bugs.

### New data model + config (`libs/mngr/imbue/mngr/api/discovery_events.py`, `config/data_types.py`)
- `DiscoveryEventType.DISCOVERY_PROVIDER` + `ProviderDiscoverySnapshotEvent` (added in the discriminated `DiscoveryEvent` union). Fields:
  - `provider_name: ProviderInstanceName`, `agents: tuple[DiscoveredAgent,...]`, `hosts: tuple[DiscoveredHost,...]`, `provider: DiscoveredProvider | None`, `error: DiscoveryError | None`, `unknown_host_ids: tuple[HostId,...]`, `unknown_agent_ids: tuple[AgentId,...]`, `discovery_started_at: datetime`, `discovery_finished_at: datetime`.
  - Builders: `make_provider_discovery_snapshot_event(...)`, `write_provider_discovery_snapshot(config, provider_name, agents, hosts, discovery_started_at, discovery_finished_at, provider=None, error=None, unknown_host_ids=(), unknown_agent_ids=())`.
- `ProviderInstanceConfig` (in `config/data_types.py`) gained: `discovery_poll_interval_seconds` (30), `discovery_warn_seconds` (20), `discovery_error_timeout_seconds` (120), `host_discovery_timeout_seconds` (30), `agent_discovery_timeout_seconds` (30), all `PositiveFloat`. A `@model_validator(mode="after")` `_validate_discovery_timeouts_are_ordered` raises `ProviderTimeoutConfigError` (new, in `errors.py`) if host/agent/warn timeout >= the error timeout. `make_discovered_provider` carries these onto `DiscoveredProvider.config`.

### Shared reconciler (`libs/mngr/imbue/mngr/api/discovery_aggregator.py`) — NEW
`DiscoveryStateAggregator(MutableModel)`, thread-safe (internal `_lock`). API:
- `apply_event(event: DiscoveryEvent) -> AggregatorDelta` — folds one event in. Handles `ProviderDiscoverySnapshotEvent` + incrementals (`AgentDiscoveryEvent`/`HostDiscoveryEvent`/`AgentDestroyedEvent`/`HostDestroyedEvent`/`HostSSHInfoEvent`/`DiscoveryErrorEvent`). **Ignores legacy `FullDiscoverySnapshotEvent`** (the `case _:` arm).
- `AggregatorDelta` (frozen): `.added_agent_ids`, `.removed_agent_ids`, `.added_host_ids`, `.removed_host_ids` (`frozenset[str]`).
- Queries (fresh copies): `get_agents()`, `get_agent_by_id()`, `get_hosts()`, `get_host_by_id()`, `get_providers()`, `get_error_by_provider_name()`, `get_unknown_agent_ids()`, `get_unknown_host_ids()`, `get_last_event_at()`, `get_last_snapshot_at_for_provider(name)`, `get_last_snapshot_at()`.
- `@pure` helpers: `parse_event_timestamp` (handles 9-digit nanosecond ISO via `datetime.fromisoformat`), `is_intervening_event(last_event_at, discovery_started_at)`, `classify_removed_item(is_provider_errored, has_intervening_event) -> RemovedItemDecision`, `should_apply_snapshot_item(has_intervening_event)`.
- Tests: `discovery_aggregator_test.py` (incl. the intervening-event-during-span race, per-provider scoping, errored-provider retain, unknown-id).

### Per-provider producer (`libs/mngr/imbue/mngr/api/provider_discovery_stream.py`) — NEW
- `run_per_provider_discovery_stream(mngr_ctx, on_line=None)` — one `_ProviderDiscoveryPoller` thread per provider; writes `ProviderDiscoverySnapshotEvent` lines; a tail thread (`tail_discovery_events_from_offset`, public — renamed from the old private `_discovery_stream_tail_events_file`) echoes appended events.
- `_ProviderDiscoveryPoller.poll_and_emit(submit_discovery)` — dual-threshold wait (warn → error), orphan/late-result handling (no second discovery while one is in flight), uses a long-lived `mngr_executor` (whose `submit` already captures exceptions — read via `future.exception()`, NOT a broad `except`).
- `cli/observe.py --discovery-only` now calls `run_per_provider_discovery_stream` (was `run_discovery_stream`).
- Tests: `provider_discovery_stream_test.py` (success/error/timeout-then-late-result). **Flakiness gotcha**: success/error tests use generous timeouts; only the gated timeout test uses tiny ones (a tight error-timeout spuriously fires under offload load).

### Consumers migrated to the aggregator
- `libs/mngr/imbue/mngr/api/observe.py` `AgentObserver`: `_on_discovery_stream_output` feeds events into `self._aggregator`, reconciles activity streams from the `AggregatorDelta` (`_sync_known_state_from_aggregator` + `_reconcile_activity_streams`). Removed `_handle_full_snapshot`/`_handle_host_destroyed`/`_handle_discovery_error_event` and `_polling_loop_crashed`. UNKNOWN synthesis in `_process_snapshot_agents` now keys only on `_currently_errored_providers`.
- `libs/mngr_forward/imbue/mngr_forward/stream_manager.py`, `libs/mngr_latchkey/imbue/mngr_latchkey/discovery_stream.py`: both fold events into a `DiscoveryStateAggregator` and drive resources off the delta. (They keep SSH info locally; aggregator doesn't model it.)
- `apps/minds/imbue/minds/desktop_client/`: `forward_cli.py` (aggregator + `resolver.update_providers(provider_name=, provider=, error=, last_snapshot_at=)` per-provider merge), `backend_resolver.py` (`_last_snapshot_at_by_provider` replacing global `_last_full_snapshot_at`; `get_last_snapshot_at_for_provider`; `get_freshness_timestamps()` returns `(last_event_at, max-across-providers)`), `app.py` (`_workspace_provider_snapshot_at` scopes the recovery-redirect gate to the workspace's provider). `discovery_health.py` still keys on `last_event_at` (unchanged, by design).
- Resolution replay (`_replay_discovery_events_into_maps` in `discovery_events.py`): added a `ProviderDiscoverySnapshotEvent` branch that calls `maps.reset_provider(...)` (per-provider reset) alongside the legacy full-snapshot reset. Used by `resolve_provider_names_for_identifiers`, `resolve_hosts_for_identifiers` (mngr stop), and the `discover_hosts_and_agents` optimization.

### Changelogs present (one per touched project)
`libs/mngr/changelog/`, `apps/minds/changelog/`, `libs/mngr_forward/changelog/`, `libs/mngr_latchkey/changelog/`, `dev/changelog/` — all `mngr-per-provider-discovery.md`.

### Branch hygiene note
The branch history has many `WIP:` commits (the cutover was assembled incrementally with parallel subagents in a shared worktree). Expect a squash-merge. The `.reviewer/outputs/autofix/` files (verified markers, issues, unfixed log) are gitignored/local.

---

## REMAINING ITEM 1 — per-host/agent sub-timeouts + UNKNOWN + intervening re-poll

**Status:** config fields + event fields (`unknown_*_ids`) + validator + aggregator handling all exist, but the **producer never uses them**. `_discover_one_provider` (in `provider_discovery_stream.py`) calls `provider.discover_hosts_and_agents()` as a black box; nothing bounds per-host/agent reads or populates `unknown_*_ids`. (Recorded as a MINOR in `.reviewer/outputs/autofix/issues/`.)

**What to do:**
- Add a producer-controlled discovery path that bounds each host's agent read by `host_discovery_timeout_seconds` (and agent reads by `agent_discovery_timeout_seconds`), marks timed-out hosts/agents UNKNOWN (omit from `agents`/`hosts`, add to `unknown_host_ids`/`unknown_agent_ids`), and passes those into `write_provider_discovery_snapshot`.
- The base discovery is `ProviderInstanceInterface.discover_hosts_and_agents` (in `libs/mngr/imbue/mngr/interfaces/provider_instance.py`, ~line 499): it does `discover_hosts()` then per-host `_discover_agents_on_host` (~line 275) in an `mngr_executor`. **Gotcha:** several providers OVERRIDE `discover_hosts_and_agents` for batch efficiency (`mngr_modal/.../instance.py`, `mngr_vps/.../instance.py` + `instance_offline.py`, `mngr_imbue_cloud/.../providers/instance.py`). A producer-side per-host wrapper only bounds the *base* path; for overriding providers you'd either (a) add an optional per-host-timeout discovery method to the interface, or (b) accept provider-level timeout only for them. Decide and document.
- **Intervening-event re-poll (also deferred):** the plan wanted the per-provider poll loop to watch the discovery log for state-change/destroy events touching its hosts *during its span* and immediately re-poll on completion. NOT implemented. The aggregator already guarantees span-*correctness* (the actual bug fix is done + tested), so this is only a faster-convergence optimization. Implement in `_ProviderDiscoveryPoller` if desired.

**Tests:** extend `provider_discovery_stream_test.py` (a slow host → UNKNOWN in the snapshot; aggregator already has unknown-id tests). Add a config test that host/agent timeout >= error timeout raises (already covered in `config/data_types_test.py`).

---

## REMAINING ITEM 2 — deprecate the legacy `FullDiscoverySnapshotEvent` (remove all *usages*, keep it parseable)

**IMPORTANT — backwards compatibility (do NOT delete the constants/classes):** existing on-disk discovery event logs (`$MNGR_HOST_DIR/events/mngr/discovery/events.jsonl` and rotated `.gz`) contain historical `DISCOVERY_FULL` lines. If we *delete* `DiscoveryEventType.DISCOVERY_FULL` or the `FullDiscoverySnapshotEvent` class, `parse_discovery_event_line` will raise `DiscoverySchemaChangedError` on those old lines (the discovery models use `extra="forbid"` and a discriminated union). So:
- **Keep** `DiscoveryEventType.DISCOVERY_FULL`, the `FullDiscoverySnapshotEvent` class, its membership in the `DiscoveryEvent` union, and the parse path — **mark them clearly deprecated** with a docstring/comment explaining their *historical* purpose (the pre-per-provider global "snapshot of all agents/hosts from one all-providers discovery scan", emitted by the old `run_discovery_stream` / `mngr list` side-effect, superseded by `ProviderDiscoverySnapshotEvent`) and that they remain only so historical on-disk logs still parse.
- **Keep** the `FullDiscoverySnapshotEvent` branch in `_replay_discovery_events_into_maps` and the aggregator's "ignore legacy full snapshot" arm — these are how old logs are tolerated on cold-start replay; just annotate them as deprecated/back-compat shims.
- **Remove all production *usages* / writers** (below) and any *emission* of `DISCOVERY_FULL`. Apply the same "deprecate, don't delete; remove usages" rule to any other legacy constant/class that may appear in persisted data.

**Status:** the streaming pipeline is fully per-provider, but `mngr list` still *writes* a global snapshot as a side-effect (read by completion/resolution; ignored by the migrated stream consumers). It is **not** a hang vector (`list_agents` already uses per-provider `ErrorBehavior.CONTINUE`). The goal is to stop *producing* it and remove all live usages, while leaving it parseable.

**Still present (all in `libs/mngr/imbue/mngr/api/discovery_events.py` unless noted):**
- `FullDiscoverySnapshotEvent`, `DiscoveryEventType.DISCOVERY_FULL`, `make_full_discovery_snapshot_event`, `write_full_discovery_snapshot`.
- `run_discovery_stream` (legacy producer — now unused in production; only docstring refs) + helpers `_write_unfiltered_full_snapshot`, `_write_unfiltered_full_snapshot_logged`, `_emit_latest_cached_snapshot`, `find_latest_full_snapshot_offset`.
- `extract_agents_and_hosts_from_full_listing` (used by list.py's global write).
- `_replay_discovery_events_into_maps` has both a `FullDiscoverySnapshotEvent` (reset-all) and `ProviderDiscoverySnapshotEvent` (reset-provider) branch.
- `libs/mngr/imbue/mngr/api/list.py`: `_maybe_write_full_discovery_snapshot`, `_build_provider_snapshot_state`, `_get_provider_config_for_snapshot` — `mngr list`'s global-snapshot side-effect.
- `libs/mngr/imbue/mngr/cli/complete_names.py`: `resolve_names_from_discovery_stream` / `_find_last_full_snapshot_line_idx` — its own bespoke "find last DISCOVERY_FULL + replay" (lines ~38–150). Relies on list.py writing DISCOVERY_FULL.
- Defensive `isinstance(event, FullDiscoverySnapshotEvent)` guards: `apps/minds/.../forward_cli.py` (~line 341), the aggregator's ignore arm, a comment in `mngr_latchkey/discovery_stream.py`.

**Plan:**
1. `list.py`: replace `_maybe_write_full_discovery_snapshot` with **per-provider** writes — group `result.agents` by `host.provider_name`; for each provider that was loaded, `write_provider_discovery_snapshot(...)` with that provider's agents/hosts + its `DiscoveredProvider` + any `ProviderErrorInfo` → `DiscoveryError`; use the listing's start/end as the span. Reuse/repoint `_build_provider_snapshot_state`.
2. `complete_names.py`: rewrite `resolve_names_from_discovery_stream` to consume per-provider snapshots — simplest is to reuse `_replay_discovery_events_into_maps` from `discovery_events.py` (already per-provider-aware) and derive names/ids from it, deleting the bespoke `_find_last_full_snapshot_line_idx` replay.
3. **Delete the writers/producers (these never appear in persisted data):** `make_full_discovery_snapshot_event`, `write_full_discovery_snapshot`, `run_discovery_stream` + its now-orphaned helpers (`_write_unfiltered_full_snapshot*`, `_emit_latest_cached_snapshot`), and `extract_agents_and_hosts_from_full_listing` (if unused after step 1). These are code, not data, so deleting them is safe. **Do NOT delete** `FullDiscoverySnapshotEvent`, `DiscoveryEventType.DISCOVERY_FULL`, their union membership, the parse path, the `_replay_discovery_events_into_maps` legacy branch, or `find_latest_full_snapshot_offset` if it's still needed to read old logs / by `tail_discovery_events_file` (see below) — **deprecate** those instead (see the back-compat note above).
4. Keep the defensive `FullDiscoverySnapshotEvent` "ignore legacy" guards in `forward_cli.py` + the aggregator + the latchkey comment — they protect against historical `DISCOVERY_FULL` lines still on disk. Annotate them as deprecated/back-compat.
5. **`tail_discovery_events_file` gotcha:** the public consumer-tail (used by `mngr forward --observe-via-file`, see `mngr_forward/cli.py` + `snapshot.py`) emits the latest *cached* snapshot on attach via `find_latest_full_snapshot_offset`. For per-provider there is no single global snapshot — redefine "cached snapshot on attach" as the latest per-provider snapshot of each provider (e.g. replay from the min byte-offset among each provider's latest `DISCOVERY_PROVIDER` line), or accept emitting from offset 0 (whole file; deduped) on attach.
6. **Tests** (the bulk of the churn): `discovery_events_test.py` (~30 tests use `write_full_discovery_snapshot`/`make_full_discovery_snapshot_event` — convert the *behavioral* ones to per-provider; but **keep** the back-compat *parse* tests like `test_full_discovery_snapshot_event_parses_legacy_lines_*` so we prove old `DISCOVERY_FULL` lines still parse — they may need to construct the event directly rather than via the deleted `write_full_discovery_snapshot`); `list_test.py` (assert per-provider side-effect); `complete_names_test.py` + `complete_test.py`; any `mngr_forward` tests asserting cached-snapshot-on-attach.

---

## REMAINING ITEM 3 — forever-claude-template `system_interface`

**Repo:** `~/project/forever-claude-template` (separate git repo; its CI does **not** gate the mngr PR). Per CLAUDE.md, work in a worktree at `.external_worktrees/forever-claude-template/` within this checkout, on a matching branch (`mngr/per-provider-discovery`), and commit there. **Do not re-vendor mngr manually** — that's automated by the minds release flow.

**Target:** `apps/system_interface/imbue/system_interface/agent_manager.py`:
- `_build_observe_command_argv` (~line 128) spawns `["mngr","observe","--discovery-only", ...]` — argv is UNCHANGED, so `libs/mngr_cli_contract/.../contract_test.py`'s argv pin still holds.
- `_handle_discovery_event` (~line 872) dispatches by type; `_handle_full_snapshot` (~line 886) wholesale-replaces `self._agents` from `event.agents` and computes removals as `old_ids - new_ids`. It does NOT read `error_by_provider_name`/`providers` and does NOT honor per-provider authority. There's a non-exhaustive match with a silent `else: pass` (FIXME).

**What to do:** rewrite to consume `ProviderDiscoverySnapshotEvent` via the vendored `DiscoveryStateAggregator` (`from imbue.mngr.api.discovery_aggregator import DiscoveryStateAggregator`) — same pattern as the other consumers: feed every parsed event into the aggregator, maintain `self._agents` from `get_agents()`/the delta, honor per-provider error retention (the aggregator does it). Make the `_handle_discovery_event` match exhaustive. The vendored mngr lives at `forever-claude-template/vendor/mngr/libs/mngr/` (editable install); after this branch's mngr is released + re-vendored, `discovery_aggregator` + `ProviderDiscoverySnapshotEvent` will be present there. Update `agent_manager_test.py` (uses `make_full_discovery_snapshot_event` → per-provider) and add a `forever-claude-template`/`dev` changelog entry per that repo's conventions.

---

## Gotchas / conventions (apply to all items)

- **Back-compat for persisted-data types — deprecate, don't delete.** Any enum constant or model class that can appear in *persisted data* (discovery event JSONL logs, host `data.json`, snapshots, etc.) must NOT be deleted, because old files still contain it and `parse_discovery_event_line` (and the other strict, `extra="forbid"` parsers) will raise on an unknown type. Instead: keep the constant/class + its parse path, mark it clearly **deprecated** with a docstring noting its historical purpose and what superseded it, and remove all live *usages/writers*. This applies to `DISCOVERY_FULL` / `FullDiscoverySnapshotEvent` (Item 2) and any similar legacy type you encounter.
- Run tests via `just test-quick "<path>"` from the repo root; set `PYTEST_MAX_DURATION_SECONDS` to your Bash timeout in seconds. The full CI gate is `just test-offload` (offload catches flakiness/ratchets local runs miss).
- **Ratchets** that bit us repeatedly (in `*/test_ratchets.py`): no broad `except Exception` (use narrow excepts or capture via a Future's `.exception()`); no importing `_`-prefixed names across modules (make a public name); no nested `def` (lambdas are OK); no trailing comments (comment-on-same-line-as-code); if/elif chains need an `else`.
- `mngr_executor` (`utils/thread_cleanup.py`) waits for submitted tasks on context-manager exit — do NOT use it per-poll for timeout work; hold one for the worker's lifetime (the poller already does this).
- Type checker is `ty` (`uv run ty check <files>`), NOT pyright. The pre-push hook runs it; CI `test_no_type_errors` runs `uv run ty check` repo-wide. `PositiveFloat` fields must be constructed as `PositiveFloat(0.05)` in tests (raw floats fail `ty`).
- Every PR needs one changelog entry per touched project: `<project_dir>/changelog/<branch-with-slashes-as-dashes>.md` (i.e. `mngr-per-provider-discovery.md`). Verify with `PYTHONPATH=. uv run python -m scripts.check_changelog_entries`.
- Pre-commit may reformat (ruff) and exit non-zero on the first try; re-add + re-commit.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
