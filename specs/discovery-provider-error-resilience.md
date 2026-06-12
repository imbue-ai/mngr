# Discovery provider-error resilience (loose threads from the workspace-flicker debug)

Status: **spec only — not yet implemented.** Captures decisions made while debugging the
"workspace flickers out of the minds desktop UI" issue so a future PR can pick it up.

## Background

A production workspace kept vanishing from the minds desktop list. Root cause was two
`mngr observe --discovery-only` processes (latchkey's + minds' system_interface forward's)
sharing one discovery event log; one observer lacked the `imbue_cloud` provider (it started
before the account was written to the profile) and emitted empty snapshots that the other
tailed and treated as authoritative "no hosts", dropping the live workspace.

Already shipped (PR #1885, branch `mngr/isolate-latchkey-discovery-observe`): latchkey's
discovery observer now writes to a **private per-env events dir** (`--events-dir`), so it can
no longer pollute minds' stream. That contained the symptom. Two related threads remain.

---

## Thread 1 — Retain known hosts/agents through a transient provider discovery error

### Problem
When a provider's discovery raises, the producer **omits that provider's agents/hosts from the
snapshot entirely** and records the provider in `FullDiscoverySnapshotEvent.error_by_provider_name`.
Consumers treat the snapshot as authoritative and fire `destroyed` for anything missing — so a
single errored poll drops live hosts/agents even though their state is merely *unknown*, not gone.
(In `forward_cli._handle_full_snapshot`, `error_by_provider_name` is currently used *only* for the
providers-panel OK/Error badges, not for the retention decision.)

### Decisions (this session)
- **Scope:** retain on `error_by_provider_name` **only** (explicit provider discovery error). A
  provider that *succeeds* and simply returns no hosts still drops them. (The separate
  "provider silently absent from `providers`" case is **out of scope** here.)
- **Drop rule:** only remove a host/agent on (a) an explicit destroy event, or (b) a *successful*
  (non-errored) discovery of its provider that shows it absent. **Never drop purely because a poll
  errored** — retain indefinitely while the provider keeps erroring.
- **Surfacing:** mark retained-but-unverified items as **unknown/stale**. Implement by **reusing the
  existing `error_by_provider_name`** signal already plumbed to the providers panel
  (`resolver.update_providers`) — render any retained host whose provider is currently errored with a
  stale/unknown indicator. **No new lifecycle-state enum and no discovery-event schema change.**
- **Reach:** apply the retention rule in **all** snapshot consumers (not just minds).

### Why consumer-side
The producer is stateless per poll and omits errored-provider agents, so "don't lose track" must be
done by each consumer **keeping its own prior state** for errored providers. The previously-known
`DiscoveredAgent`s carry `provider_name`, so attributing a removed agent to its (possibly-errored)
provider is feasible — snapshot the prior agent map before replacing it.

### Touch points
- `apps/minds/imbue/minds/desktop_client/forward_cli.py::_handle_full_snapshot`
  - Capture prior `_discovered_agents` (with `provider_name`) before replacing.
  - For each agent in `removed`, if its prior provider is in `event.error_by_provider_name`, **do not
    fire `destroyed`** and **keep it** in `_discovered_agents`.
  - UI renders those as stale via the `error_by_provider_name` the resolver already receives.
- `libs/mngr_forward/imbue/mngr_forward/resolver.py` + `stream_manager.py`
  - Retain the service/tunnel mapping for an errored provider's agents rather than dropping it.
- `libs/mngr_latchkey/imbue/mngr_latchkey/discovery.py` / `discovery_stream.py`
  - Retain the host's tunnel/permission for an errored provider rather than tearing it down.
- `libs/mngr/imbue/mngr/api/discovery_events.py`
  - **Flip the `FullDiscoverySnapshotEvent` docstring** (currently: "agents and hosts for providers in
    `error_by_provider_name` MUST NOT be retained") to the new rule: retain errored-provider
    hosts/agents from prior state and mark them unknown; only drop on explicit destroy or a
    successful poll showing absence.

### Tests / changelog
- Per-consumer unit test: a snapshot omitting an agent whose provider is in `error_by_provider_name`
  does NOT drop it; a subsequent *clean* snapshot omitting it DOES drop it.
- Changelog entry per touched project (`mngr`, `mngr_forward`, `mngr_latchkey`, `minds`).

---

## Thread 2 — latchkey does not refresh on *mid-session* config changes

### Current behavior (verified)
- minds **restarts** the detached `mngr latchkey forward` only on **minds startup**
  (`run.py::_ensure_mngr_latchkey_forward_supervisor` → `LatchkeyForwardSupervisor.restart()`).
- minds **bounces its own observe** on **config changes mid-session** — `consumer.bounce_observe()`
  (SIGHUP → `mngr forward` restarts its `mngr observe` child) on agent creation and on provider
  enable/disable (`app.py::_handle_provider_toggle`).
- minds does **not** bounce or restart the latchkey forward on those same mid-session changes.

### Gap
- **Provider-set changes mid-session** (account added to the profile, provider enabled/disabled) are
  picked up by minds' own observe (via bounce) but **not** by latchkey's — latchkey keeps the
  provider set it loaded at spawn until the next full minds restart.
- The latchkey forward's **startup spawn can race the config write** (the original bug: latchkey
  spawned at 17:42:15, the imbue_cloud account landed in `settings.toml` at 17:43:38 — same startup,
  wrong order), so even the startup restart can come up with stale providers.
- (New *agents* are fine — latchkey's observe is unfiltered, so it sees them on the next poll without
  a bounce. The gap is specifically the **provider set**.)
- With #1885 isolation this no longer flickers minds' UI, but latchkey's own gateway
  permission/tunnel setup can lag a stale provider view.

### Fix
- **Bounce/restart the latchkey forward on the same triggers minds already uses to bounce its own
  observe.** Concretely: wherever `consumer.bounce_observe()` fires for minds' system_interface
  observe (provider enable/disable in `app.py::_handle_provider_toggle`, agent creation), also
  bounce/restart the latchkey forward so its observer reloads the current provider set. This keeps
  latchkey's discovery in lockstep with minds' own.
- Honor the ordering subtlety: ensure latchkey (re)starts **after** the relevant config is finalized,
  not concurrently (otherwise the startup race that caused the original bug recurs).

---

## References
- PR #1885 — latchkey discovery-observer isolation (the shipped symptom fix).
- `MngrConfig.events_base_dir_override`, `get_discovery_events_dir` (config plumbing added in #1885).
