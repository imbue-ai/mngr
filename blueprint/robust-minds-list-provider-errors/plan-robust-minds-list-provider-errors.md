# Robust handling of provider errors in minds discovery and `mngr list` callers

## Refined prompt

Make Minds/`mngr list` interactions principled about provider errors, now that unauthenticated providers raise `ProviderNotAuthorizedError` (a `ProviderUnavailableError` subclass) and `mngr list` exits **6** (`EXIT_CODE_PROVIDER_INACCESSIBLE`) instead of warning.

* **Guiding principle:** each caller decides whether a provider error is acceptable. A **blanket** all-providers `mngr list` may treat exit 6 as benign (some providers unauthenticated is OK); a call **scoped** to a single agent / host / provider treats exit 6 as a real failure.
* **Discovery is already robust — confirm only:** `mngr observe --discovery-only` (used by minds via latchkey-forward, and by FCT `agent_manager`) and FCT `agent_discovery.py` already call `list_agents(error_behavior=ErrorBehavior.CONTINUE)`; per-provider failures surface via `error_by_provider_name`, agents from healthy providers are retained, and the streams never crash.
* **Fix — FCT `apps/system_interface/.../claude_auth.py`** (separate repo; work in `.external_worktrees/forever-claude-template` on the same branch; do **not** touch or commit `vendor/mngr` — it is synced automatically): this is a blanket caller. Add `--on-error continue` to `_build_list_command`, and treat exit 6 as success (parse the JSON body, use the authenticated providers' agents), keep raising `ClaudeAuthError` on other nonzero exits. Read the `errors[]` body and debug-log each skipped provider. Import the named `EXIT_CODE_PROVIDER_INACCESSIBLE` constant rather than hardcoding `6`.
* **Fix — monorepo `justfile`** agent-name lookup recipe (`mngr list --format jsonl`): blanket caller — add `--on-error continue`.
* **Fix — `libs/mngr_forward`**: add a `mngr forward --on-error {abort,continue}` flag (CLI-only, default `abort`, mirroring `mngr list`). Under `continue`, the `--no-observe` startup snapshot passes `--on-error continue` to its `mngr list` call and treats exit 6 as a partial success instead of raising. The flag gates **only** `--no-observe`; observe / `--observe-via-file` modes stay always-tolerant (their continuous nature can't meaningfully "abort"), documented in `--help`. SIGHUP re-snapshot is already tolerant and is unchanged.
* **minds host-state probe (scoped):** keep exit 6 meaningful (it already feeds `WORKSPACE_UNREACHABLE`) and keep its current warning — no behavior change. minds is confirm-only: no `apps/minds` code change.
* **FCT `agent_manager.py`:** keep the cwd workaround but update its now-stale comment (observe tolerates provider errors via `CONTINUE`; the cwd is kept only to scope to project providers / reduce noise).
* **Tests:** unit-test the changed callers (FCT `claude_auth` argv includes `--on-error continue` and exit 6 yields agents instead of raising; `mngr_forward` snapshot argv includes `--on-error continue` under `continue` and treats exit 6 as partial success vs raises under `abort`). No new acceptance tests.
* **Changelog:** entries for `dev/` (justfile), `libs/mngr_forward` (new flag), and FCT `apps/system_interface` (in its own repo). No `apps/minds` entry (confirm-only).

## Overview

- Background work already landed on the base branch (`josh/consistent-provider-auth-failures`): a consistent `ProviderNotAuthorizedError`, `mngr list --on-error continue` aggregation, and the granular `EXIT_CODE_PROVIDER_INACCESSIBLE = 6`. This branch makes the *callers* robust to that new contract.
- The core principle is caller-local: an unauthenticated provider is acceptable for a blanket "show me everything" listing, but a real failure for a listing scoped to one agent/host/provider. We apply exit-6 tolerance only to blanket callers.
- The continuous discovery paths (mngr observe stream consumed by minds and by FCT) are already tolerant; this branch confirms that and changes nothing there.
- The remaining fragile callers are three blanket `mngr list` invocations that still inherit the default abort and/or raise on any nonzero exit: FCT `claude_auth.py`, the monorepo `justfile` lookup recipe, and `mngr forward --no-observe`'s startup snapshot.
- minds itself needs no code change: its single `mngr list` call (the provider-scoped host-state probe) already passes `--on-error continue` and already classifies exit 6 as `WORKSPACE_UNREACHABLE`.

## Expected behavior

- An enabled-but-unauthenticated provider (e.g. `modal` configured in `~/.mngr` but not logged in) no longer breaks any blanket `mngr list` consumer.
- **FCT claude auth recovery:** `list_claude_agent_names()` returns the `type: claude` agents from the authenticated providers even when another provider is unauthenticated; it raises `ClaudeAuthError` only on genuinely-bad exits (non-provider errors, exit 1) or unparseable output. Skipped providers are logged at debug.
- **`just` agent-name lookup:** resolves a local agent's id even when an unrelated provider is unauthenticated, instead of returning empty / "no agent found".
- **`mngr forward --no-observe`:** with `--on-error continue`, the forward server starts and serves the agents from healthy providers even if a provider is unauthenticated; with the default `--on-error abort`, behavior is unchanged (fails fast on the first provider error at startup).
- **`mngr forward` observe / `--observe-via-file`:** unchanged — already tolerate provider errors and keep forwarding healthy agents; `--on-error` has no effect on these modes (and `--help` says so).
- **minds workspace recovery:** unchanged — a workspace whose own (scoped) provider is unauthenticated still surfaces as `WORKSPACE_UNREACHABLE` with the existing warning, since exit 6 on a scoped probe is a real signal.
- **minds continuous discovery:** unchanged — still degrades gracefully, retaining agents from errored providers and surfacing per-provider errors in the providers panel and workspace staleness.

## Changes

- **FCT `apps/system_interface/imbue/system_interface/claude_auth.py`** (in `.external_worktrees/forever-claude-template`):
  - `_build_list_command` adds `--on-error continue`.
  - `list_claude_agent_names` treats exit `EXIT_CODE_PROVIDER_INACCESSIBLE` (6) as success: parse the JSON body, debug-log each provider in `errors[]`, return the authenticated agents; keep raising `ClaudeAuthError` on other nonzero exits and on non-JSON / malformed output.
  - Import `EXIT_CODE_PROVIDER_INACCESSIBLE` from mngr rather than hardcoding `6`.
- **Monorepo `justfile`** agent-name lookup recipe: add `--on-error continue` to the `mngr list --format jsonl` invocation.
- **`libs/mngr_forward`**:
  - Add `--on-error {abort,continue}` (default `abort`) to the `forward` CLI options and the `ForwardCliOptions` dataclass; CLI-only (no `ForwardPluginConfig` field). Document in `--help` that it affects only `--no-observe`.
  - Thread the choice from `_seed_resolver_from_snapshot` into `mngr_list_snapshot`, which under `continue` appends `--on-error continue` and treats exit 6 as a partial success (parse stdout) instead of raising `ForwardSubprocessError`; under `abort`, behavior is unchanged.
- **FCT `apps/system_interface/imbue/system_interface/agent_manager.py`**: update the stale comment on the observe cwd workaround (no code change).
- **minds**: confirm-only — no code change to the scoped host-state probe or discovery consumers.
- **Tests**: unit tests for the FCT `claude_auth` argv + exit-6-as-success path, and for the `mngr_forward` snapshot argv + exit-6 handling under both flag values.
- **Changelog**: per-PR entries for `dev/` (justfile), `libs/mngr_forward`, and FCT `apps/system_interface` (its own repo). No `apps/minds` entry.
