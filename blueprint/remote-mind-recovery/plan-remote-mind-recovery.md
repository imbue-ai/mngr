# Extend minds recovery logic to remote (imbue_cloud) minds

## Overview

- Recovery was designed and tested only against local providers (docker/lima); the imbue_cloud provider wasn't finalized at the time, so remote behavior was never properly handled. Remote minds can be funneled to the recovery page on a transient blip and offered a destructive host restart that can break an otherwise-healthy mind.
- Core change: gate recovery on **provider reachability**, and never take a destructive action that can't help. If the provider (imbue_cloud connector / docker daemon) is unreachable, show a dedicated page instead of offering a restart; otherwise fall through to the existing host-state classification.
- Provider reachability is read from the `mngr list` the probe already runs (it round-trips the connector for imbue_cloud and the daemon for docker); imbue_cloud starts raising the specific `ProviderUnavailableError` so the signal is typed. No new probe and no new top-level CLI command.
- The destructive container stop is backstopped by giving the provider-unavailable tier precedence over the manual-restart tier, so you're never offered a destructive restart while the provider is unreachable. Note an unreachable host can't be destructively stopped anyway — the stop physically can't run over its SSH — so the only residual danger is "host reachable + mind actually fine." (A live pre-stop interface re-probe to catch that residual case was prototyped but descoped; see "Descoped during implementation".)
- Behavior applies uniformly across providers (a down local docker daemon is treated the same: don't offer a restart that can't help).
- **Follow-up (this branch, `gabriel/recovery-redundancy`):** the synchronous `mngr list` reachability probe described above is itself redundant with the passive discovery state and is being removed — the recovery page reads provider/host state from the resolver instead. The shipped behavior below stands as context; see **"Follow-up: collapse redundant provider/host state"** at the end of this document for the current plan.

## Descoped during implementation

These items appear in the plan below but were prototyped and then dropped from the shipped implementation. The rationale is kept below for context, but the current behavior does **not** include them:

- **Pre-stop interface re-confirmation** on the destructive `--stop-host` path — redundant with the provider-reachability gating and the recovery page's own auto-return, so the destructive path is unchanged from today.
- **Longer remote-only STUCK-entry window** (and the `SystemInterfaceHealthTracker` remoteness plumbing it required) — gating at the recovery page itself made it unnecessary, so remote and local minds keep the same recovery entry timing. (An attempt was reverted in `dd262625b`.)
- **Gating the `interface_unresponsive` auto-restart on a sustained/confirmed failure** rather than a single host-health probe.

## Expected behavior

- Brief connectivity blips no longer throw remote minds onto the recovery page: remote minds tolerate a longer sustained-failure window before going STUCK, and first show the existing "Loading workspace" state, escalating to the recovery page only if the failure persists. Local minds keep their current entry timing.
- Provider unreachable (your connection is down, or Imbue Cloud is down) → a "Can't connect to Imbue Cloud" page with Retry and **no** restart option.
- Provider reachable → recovery behaves much as today but prefers non-destructive actions: a stopped container is started in place; a wedged interface is restarted in place. The auto interface restart only fires after a sustained/confirmed failure, not a single probe.
- The destructive "Restart workspace" (container stop + start) remains available for remote minds, unchanged from today. (A pre-stop interface re-check that would silently abort the restart if the mind had recovered on its own was prototyped but descoped; see "Descoped during implementation".)
- Provider-surfaced auth/account problems (expired login, no account configured) show a plain "can't reach your workspace — <reason>" message with no restart (a restart can't fix them) and are not treated as a connectivity outage.
- The "Can't connect to Imbue Cloud" page keeps polling in the background (backing off over time, not spamming) and automatically returns the user to the workspace once it's reachable again; a manual Retry is also available.
- The same behavior holds for local providers: a down docker daemon yields the provider-unavailable page and suppresses the restart affordance.

## Changes

- Feed provider-reachability into recovery classification, derived from the `mngr list` probe (now carrying a typed `ProviderUnavailableError` in its `errors[]`); the existing host-state / in-container exec probes continue to drive the existing tiers. No separate active reachability probe is added. *(Superseded by the follow-up section — the synchronous `mngr list` is removed and reachability is read from the passive discovery store.)*
- imbue_cloud raises the specific `ProviderUnavailableError` when the connector is unreachable (today it raises nothing provider-specific from the discovery path).
- minds parses the `mngr list` JSON even when the command exits non-zero (today the stdout is discarded on a non-zero exit), so the typed `errors[]` is available to classification; scope this to the recovery probe rather than changing the shared mngr-runner globally. *(Superseded by the follow-up section — with the synchronous `mngr list` removed, the typed error is consumed from `get_provider_errors()`, populated by discovery's `error_by_provider_name`.)*
- Add one classification tier, provider-unavailable, with precedence **above** the existing host/interface tiers (notably the manual-restart `HOST_UNRESPONSIVE` tier). Because it takes precedence, the recovery page shows the provider-unavailable page — and nothing auto-dispatches — when the provider is unreachable, without wrapping each dispatch path individually.
- The provider-unavailable tier keys on `errors[].exception_type == "ProviderUnavailableError"`, which works **only if imbue_cloud raises that error narrowly** — for genuine connector-unreachability, never for auth/account-config failures (those must keep raising their own distinct types so they fall into the generic bucket below).
- Distinguish connector-unreachable (→ provider-unavailable: retry, no restart) from auth/account-config failures (→ generic "can't reach — <reason>": no restart, no dedicated recovery flow). Do not lump all of `errors[]` into provider-unavailable.
- A pre-stop re-confirmation on the destructive `--stop-host` path (re-probe the interface immediately before the stop and abort — silently returning the user to the workspace — if it now responds) was prototyped but **descoped** as redundant with the provider gating and the recovery page's own auto-return; the destructive path is unchanged from today. See "Descoped during implementation".
- Make remote minds more reluctant to enter recovery: a longer sustained-failure window before STUCK, plus a lighter first state that reuses the existing "Loading workspace" loading page, escalating to the recovery page only on persistence. This requires teaching `SystemInterfaceHealthTracker` about remoteness — it is keyed by agent id with a single global threshold today, while provider/remote info is only available at the host-health probe layer — so a remote-aware window means plumbing remoteness into the tracker.
- Gate the auto interface (`interface_unresponsive`) restart on a sustained/confirmed failure rather than a single host-health probe.
- Add the provider-unavailable page, with a manual Retry and background polling that backs off over time and auto-returns to the workspace on recovery.
- Apply the new classification and page uniformly to local providers (docker daemon down → provider-unavailable page, restart suppressed).
- Explicitly out of scope: a dedicated "host is down" page for the rare provider-reachable-but-host-unreachable case (a server-side VPS fault mngr can't fix anyway) — such a host keeps classifying as offline and falls through to existing behavior; the destructive stop can't run against it, so nothing is lost on safety. Also out of scope: the `CRASHED`-reconfirmation cleanup (no longer needed without the host-down page).
- Add a changelog entry under `apps/minds` (and `libs/mngr_imbue_cloud` / `libs/mngr` for the `ProviderUnavailableError` and list-error changes) per the repo's per-project changelog rule.

## Follow-up: collapse redundant provider/host state

> Branch `gabriel/recovery-redundancy`. This section revises the approach above. The shipped work landed the provider-unavailable tier and imbue_cloud's typed `ProviderUnavailableError`; this follow-up removes the redundant *second* sampler of provider/host state that the recovery probe introduced.

### Motivation

The shipped implementation reads provider reachability from a synchronous `mngr list` that the recovery host-health probe runs on demand. That is a **second independent sampler** of provider + host state, polled on its own clock, separate from the passive discovery polling (`mngr observe --discovery-only`) that already feeds the `MngrCliBackendResolver` and drives the rest of the UI (workspace list, providers panel). Two samplers of the same state layer can disagree — the recovery page can show host/provider state that conflicts with the sidebar — which is a latent source of races and inconsistent UI. The goal of this follow-up is **one source of truth per state layer**: read outer state from the passive store instead of re-sampling it.

### State layers

- **Layer A — outer state** ("is the provider reachable, and what is the host's lifecycle?"). Owned passively by the resolver, fed by discovery: `get_host_state()`, `get_provider_errors()`, `get_system_services_agent_id()`. The workspace list and providers panel already read Layer A from here.
- **Layer B — inner state** ("*why* isn't the interface answering?"): can-exec, `services.toml` declares the interface, inner port listening, inner `curl /`. Lives only in the on-demand `mngr exec` probe; discovery never collects it.
- **Trigger — `SystemInterfaceHealthTracker` STUCK**: sustained HTTP-probe failure (~5s) is the redirect trigger. A temporal reachability concern, not a state store; leave as-is. It does not conflict with discovery — "outer up, inner down" is two layers, not two truths.

### Per-signal disposition

| Recovery signal | Layer | Source today | In the passive store? | Disposition |
|---|---|---|---|---|
| host.state (probe 1) | A | synchronous `mngr list` | Yes — `get_host_state()` | Redundant → read from resolver |
| services-agent exists (probe 2) | A | synchronous `mngr list` | Yes — `get_system_services_agent_id()` | Redundant → read from resolver |
| provider error (`errors[]`) | A | synchronous `mngr list` | Yes — `get_provider_errors()` (typed, per-provider) | Redundant → read from resolver |
| can-exec (probe 3) | B | exec probe | No | Keep (only inner-reachability source) |
| services.toml declares interface (probe 4) | B | exec probe | No | Keep |
| inner port listening (probe 5) | B | exec probe | No | Keep |
| inner curl `/` (probe 6) | B | exec probe | No (tracker probes end-to-end, not inner) | Keep (complementary) |
| plugin resolver snapshot (probe 7) | B | passive (forward stream) | Yes — already single-source | Keep as-is |

The redundancy is entirely in Layer A. Layer B is not duplicated (probe 7 already reads the passive store; the inner `curl` and the tracker's end-to-end HTTP probe are different facts — inner-up-but-plugin-route-broken is a real, distinct diagnosis).

### Changes (this follow-up)

- `_run_host_health_probe` no longer builds or runs `_build_mngr_host_state_argv` / `mngr list`. Layer-A inputs (host state, services-agent id, provider error) are read from `backend_resolver` plus discovery freshness.
- The provider-unavailable signal comes from `get_provider_errors()[provider_name]` — the same typed `ProviderUnavailableError` imbue_cloud already raises through the discovery path (`error_by_provider_name`), now read from the passive store instead of re-sampled. (imbue_cloud's typed-error change from the shipped work stays; only the consumption point moves.)
- The `mngr exec` probe stays, but **purely** for Layer-B decomposition, and fires **only when Layer A is "provider reachable + host RUNNING"** — so an outage never pays a provider round-trip on this path.
- `build_host_health_response` keeps producing the same `dispatch_tier` and probe rows; its Layer-A inputs are now resolver-sourced rather than parsed from `list_json`. Add the transient **reachability-unconfirmed** outcome for the stale-discovery window (renders Retry, never the destructive restart).
- Recovery page client (`_RECOVERY_SCRIPT`): re-run the host-health classification on a backed-off interval while in a non-terminal awaiting-fresh-discovery state, so the affordance updates as the single sampler catches up (see "Decision: single sampler, with a reactive recovery page"). This is the "make sure it updates properly" requirement.

### Discipline: one sampler per layer

The exec probe **incidentally touches Layer A** — it calls `provider.get_host()` to reach the container. Its outcome is **Layer-B evidence only** ("could/couldn't reach the inner interface, plus the decomposition"). Layer-A classification (`PROVIDER_UNAVAILABLE` / `HOST_OFFLINE`) comes **solely** from the resolver. Do **not** parse the exec failure as a provider-state signal: `mngr exec` per-agent failures are emitted as untyped strings (`exec_error` events carry `{"agent","error":<str>}`, not a typed `exception_type`), so keying on them would recreate a *worse* second Layer-A sampler through the back door.

### Safety: gate the destructive tier on freshness

Reading Layer A from the resolver means the recovery page inherits discovery freshness (≤ one ~10s poll cycle). In the **post-outage window** — the first ~30-40s after connectivity drops, before the next discovery poll surfaces the typed error — the resolver may still show a stale `host=RUNNING` with no provider error yet, which would otherwise classify as the destructive `HOST_UNRESPONSIVE` tier. The destructive tier must be **gated on discovery freshness**: if discovery is stale/unconfirmed and reachability is not positively established, do not offer a destructive host restart — fall to a transient **reachability-unconfirmed** outcome that renders the non-destructive **Retry** affordance (reusing the provider-unavailable page's Retry + background-poll treatment, without asserting the provider is down). This preserves the safety property the shipped synchronous-timeout-as-signal provided, without a second sampler. (Composes with the provider-unavailable precedence from the shipped work.)

### Latency cross-check (do not reintroduce the 2-minute wait)

- The original 2-minute "Loading workspace…" wait was the host-health endpoint blocking on the synchronous `mngr list` at the default `_RESTART_COMMAND_TIMEOUT_SECONDS = 120s`; commit `470f0bbee` capped that path at `_HOST_HEALTH_PROBE_TIMEOUT_SECONDS = 30s`. The STUCK → recovery redirect itself is fast (~5s).
- Removing the synchronous list makes the Layer-A read effectively **instant** (a resolver lookup, no network) — strictly better than 30s and never 120s.
- **Landmine:** the exec probe also touches the provider (`get_host` → the connector's ~30s httpx) and currently runs via `_run_mngr` at the 120s default, saved today only by being skipped when the list times out. With the list gone, the retained exec probe must (a) carry an explicit 30s-class cap (never inherit the 120s `_RESTART_COMMAND_TIMEOUT_SECONDS`), and (b) fire only when Layer A is healthy (provider reachable + host RUNNING), so an outage never stacks or pays a provider round-trip here.

### Freshness trade (explicit)

The synchronous list's only genuine advantage was *fresher* Layer-A state — but "fresher **and** independently sampled" is exactly the conflicting second truth we're removing. We accept the resolver's ≤one-cycle freshness in exchange for a single consistent view shared with the rest of minds.

### Acceptance criteria

- The recovery host-health path issues **no** synchronous `mngr list`; Layer-A state is read from `backend_resolver`.
- With imbue_cloud unreachable (e.g. wifi off) and discovery having surfaced the typed error, the recovery page classifies `PROVIDER_UNAVAILABLE` and the host-health endpoint returns well under 30s (target: ~instant).
- In the post-outage window (provider error not yet surfaced, stale `RUNNING` host), the page does **not** offer a destructive restart — it shows Retry.
- From a cold post-outage load that renders Retry, the affordance **updates on its own** (no manual reload) within ~one discovery poll cycle to the real tier: "Restart workspace" once discovery confirms provider-reachable + host needs a restart, or the provider-unavailable page if the typed error surfaces.
- A docker mind's recovery during a simultaneous imbue_cloud outage is **not** misclassified as provider-unavailable (per-provider keying via `get_provider_errors()[provider_name]`).
- The recovery page's host/provider state matches the workspace list / providers panel for the same agent (single-source consistency).
- The existing tiers (`HOST_OFFLINE`, `INTERFACE_UNRESPONSIVE`, `WORKSPACE_MISCONFIGURED`, `HOST_UNRESPONSIVE`) still classify correctly when the provider is reachable.

### Decision: single sampler, with a reactive recovery page

Resolved: **keep a single sampler** (no synchronous provider touch). The recovery page reads Layer A from the resolver and returns *instantly*; in the post-outage window it shows the **Retry** affordance briefly and then updates to the real tier (e.g. **Restart workspace**) once discovery catches up. The brief "Retry" is acceptable **provided the page actually updates** without a manual reload — which the current client does **not** do, so this follow-up must add the reactive update below.

#### The gap in the current client

The recovery page JS (`_RECOVERY_SCRIPT` in `templates.py`) fetches `/api/agents/<id>/host-health` **once** per page entry via `runProbe()`, classifies `dispatch_tier`, and renders the matching affordance. Its background polls do **not** re-classify:

- `scheduleProviderPoll` (provider-unavailable) and `scheduleHealthyPoll` (restart-failed) only watch `pollUrl()` (the recovery page) for a **302 → return_to**, which the server emits when the *health tracker* flips HEALTHY — i.e. they detect **full recovery**, not a tier change.
- `scheduleRefresh` (restarting) is a full-page reload.

So a page that first renders **Retry** (reachability-unconfirmed) would sit on Retry indefinitely: the workspace isn't going to flip HEALTHY on its own (it needs a restart), so no 302 ever fires, and `runProbe()` is never re-invoked to discover that discovery has since confirmed `host_unresponsive` (→ "Restart workspace") or surfaced a provider error (→ provider-unavailable). This is the "make sure it updates properly" requirement.

#### Required reactive update

While the page is in a **non-terminal, awaiting-fresh-discovery** state — the new reachability-unconfirmed outcome, and `provider_unavailable` (so it can also de-escalate as discovery changes) — the client must **re-run the host-health classification on a (backed-off) interval**, re-rendering the tier each tick, in addition to the existing 302 recovery-watch. Concretely:

- Re-invoke `runProbe(true)` on a timer for these states (not just watch for a 302), so `dispatch_tier` is recomputed from the latest resolver state and the affordance transitions: **Retry → Restart workspace** (discovery confirmed provider-reachable + host needs restart), **Retry → provider-unavailable** (typed error surfaced), or **Retry → auto-dispatch** (`host_offline`/`interface_unresponsive`).
- Keep the existing 302 → `return_to` watch for the full-recovery transition (workspace came back on its own → return the user to it).
- Guard against stacking timers (single in-flight poll, like the existing `providerPollTimer`).

This is cheap *because of the collapse*: with host-health now a resolver read (no provider round-trip), re-polling every ~1-2s costs nothing — unlike today, where re-running `runProbe` meant a fresh ≤30s synchronous `mngr list`. Making host-health cheap is precisely what makes smooth reactive updates viable. (A push-based variant — subscribe the recovery page to a resolver on-change SSE and re-fetch on change — is a possible refinement, but the interval re-poll of the now-cheap endpoint is simpler and self-contained, and is the baseline requirement.)

### Changelog

- `apps/minds` changelog entry for the recovery host-health refactor (remove the synchronous `mngr list`; read Layer-A state from the resolver). No `libs/mngr*` change is required by this follow-up — the typed `ProviderUnavailableError` and the discovery `error_by_provider_name` plumbing already landed.

## Revision: gate the recovery redirect on fresh discovery (option A)

> Branch `gabriel/recovery-redundancy`. This section revises the **"reachability-unconfirmed + reactive recovery page"** approach above. That approach shipped a transient `REACHABILITY_UNCONFIRMED` dispatch tier plus a client-side convergence loop so a stale-discovery load would render Retry and then update itself once discovery caught up. This revision removes all of that machinery in favor of a single upstream gate.

### Motivation

`REACHABILITY_UNCONFIRMED` is, functionally, a *loading* state: it exists only for the brief window where discovery has not yet confirmed reachability, and it collapses into a real tier within one poll cycle. Expressing it as a peer of the actionable recovery tiers (`HOST_UNRESPONSIVE`, `PROVIDER_UNAVAILABLE`, ...) — with its own page, its own `reachability_confirmed` boolean threaded through the classifier, and its own client convergence loop — is more structure than the state deserves. Worse, it conflates two different concerns:

- a **transient** "discovery hasn't caught up yet" (cold start, or the ≤one-cycle post-outage window), and
- a **persistent** "the discovery pipeline itself is broken" — which is app-global infrastructure health, not a per-workspace recovery condition.

A cleaner cut: don't send the user to the recovery page at all until discovery is fresh. Then the recovery page can *assume* fresh discovery, and the whole `reachability_confirmed` / `REACHABILITY_UNCONFIRMED` / convergence apparatus disappears.

### Key facts that make this clean

- **The redirect is fully app-controlled.** The `mngr_forward` plugin only ever serves a dumb, auto-refreshing "Loading workspace" 503 loader (1s meta-refresh) and emits a `system_interface_backend_failure` envelope; it never navigates to recovery. The redirect is driven entirely by minds: the background probe loop marks the agent `STUCK` on the `SystemInterfaceHealthTracker`, the chrome-events SSE pushes a `system_interface_status` event, and `chrome.js` (`maybeRedirectToRecovery`) navigates the content view to `/agents/<id>/recovery` (guarded by a client-side redirect lock). Nothing about discovery freshness is consulted today.
- **A provider outage keeps discovery fresh.** The discovery poll emits a full snapshot every ~10s even when a provider is down — per-provider failures fold into `error_by_provider_name` (`ErrorBehavior.CONTINUE`). So a provider outage shows up as *fresh snapshot + typed error* → `PROVIDER_UNAVAILABLE`, never as stale discovery. Gating the redirect on freshness therefore does **not** stall the common outage case: discovery is fresh-with-error and the redirect fires promptly into a confident `PROVIDER_UNAVAILABLE`.
- **Stale discovery means the pipeline is broken (or cold-starting), not that a provider is down.** A snapshot is withheld only when the producer/consumer pipeline stalls (`mngr observe` or the `mngr forward` consumer wedged/dead) or `list_agents` itself raises structurally. So the only times the redirect gate actually defers are **cold start** (no first snapshot yet, ~10s) and **persistently broken discovery**.

### Design

- **Gate the `STUCK` redirect signal on discovery freshness, server-side.** In the chrome-events SSE (`_handle_chrome_events`), suppress emitting `system_interface_status` with status `stuck` for an agent while discovery is stale (`_is_discovery_fresh` is false) — at all three emission points (the connect-time snapshot, the per-transition drain, and the 15s re-assert backstop). While stale, the chrome receives no `stuck` event and stays on the plugin's auto-refreshing loader. Once a fresh snapshot lands (or the next re-assert tick after one does), the `stuck` event is emitted and the chrome redirects. No `chrome.js` change is required — the existing "redirect on `stuck`" logic is unchanged; it simply isn't told `stuck` until discovery is fresh.
- **Remove `reachability_confirmed` and `REACHABILITY_UNCONFIRMED`.** `_classify_dispatch_tier` returns `HOST_UNRESPONSIVE` unconditionally for the "host claims RUNNING but unreachable" case; `build_host_health_response` / the classifier no longer take a `reachability_confirmed` argument. The in-container exec gate keeps only its meaningful checks — fire the exec when `provider_error is None and host_state == RUNNING` — dropping the freshness conjunct (the page is only reached fresh).
- **Remove the client convergence loop.** Delete `scheduleConvergence` / `isAwaitingFreshDiscovery` / the `CONVERGE_*` constants / `renderReachabilityUnconfirmed`. `provider_unavailable` keeps auto-returning the user via the existing `scheduleHealthyPoll` 302-watch (the health tracker flips HEALTHY → the page 302s to `return_to`); it no longer needs to re-classify, because it was reached with fresh discovery.

### Safety note (residual, accepted)

The destructive-restart safety gate that `REACHABILITY_UNCONFIRMED` provided now lives upstream as the redirect gate: in the normal flow you only reach the recovery page with fresh discovery, so a destructive `HOST_UNRESPONSIVE` is only offered on confirmed-fresh state. The residual case is reaching the recovery page and *then* having discovery go stale (the pipeline breaks while you sit on the page), or a direct nav — where the probe would classify `HOST_UNRESPONSIVE` on possibly-stale state and offer the restart. This reverts to the pre-this-follow-up behavior, which is low-risk: a destructive `--stop-host` physically cannot run against an unreachable host (its SSH fails), so the only residual danger is "host reachable + mind actually fine," the same descoped residual noted at the top of this document. Acceptable; flagged.

### Constant / naming cleanup (folded in)

- Rename `_RESTART_COMMAND_TIMEOUT_SECONDS` → `_MNGR_COMMAND_TIMEOUT_SECONDS`: it is the default for `_run_mngr` generally (5 of 6 call sites — including `mngr label`, a non-restart command), not a restart-specific value. The comment keeps the "sized for the slowest legitimate case, a host stop/start" rationale.
- Derive the freshness threshold from the discovery poll cadence instead of a bare `30.0`: promote `_DISCOVERY_STREAM_POLL_INTERVAL_SECONDS` in `libs/mngr/imbue/mngr/api/discovery_events.py` to a public `DISCOVERY_STREAM_POLL_INTERVAL_SECONDS` and set `_DISCOVERY_FRESHNESS_THRESHOLD_SECONDS = 3 * DISCOVERY_STREAM_POLL_INTERVAL_SECONDS` (3 missed snapshots), so the threshold can't silently drift from the cadence it depends on.
- Drop the internal "Layer A / Layer B" jargon from comments/docstrings in `app.py` and `recovery_probe.py` in favor of the plain meaning ("provider reachability and host lifecycle" / "the in-container exec probe").

### Acceptance criteria (revision)

- A workspace whose system interface is down does **not** redirect to the recovery page while discovery is stale; it stays on the auto-refreshing "Loading workspace" loader. Once a fresh discovery snapshot lands (and the agent is still stuck), the chrome redirects to the recovery page.
- With imbue_cloud unreachable (discovery fresh-with-error), the redirect fires promptly and the recovery page classifies `PROVIDER_UNAVAILABLE` (no stale-window Retry state in the normal flow).
- The host-health endpoint no longer takes or computes `reachability_confirmed`, and there is no `REACHABILITY_UNCONFIRMED` tier; `HOST_UNRESPONSIVE` is returned for the host-claims-RUNNING-but-unreachable case.
- The recovery-page client has no convergence loop; `provider_unavailable` still auto-returns the user once the workspace recovers, via the 302 healthy-watch.
- Existing tiers (`HOST_OFFLINE`, `INTERFACE_UNRESPONSIVE`, `WORKSPACE_MISCONFIGURED`, `HOST_UNRESPONSIVE`, `PROVIDER_UNAVAILABLE`, `WORKSPACE_UNREACHABLE`) still classify correctly.

### Changelog (revision)

- `apps/minds` changelog entry: gate the recovery redirect on fresh discovery; remove the `REACHABILITY_UNCONFIRMED` tier, the `reachability_confirmed` plumbing, and the recovery-page convergence loop; rename the mngr-command timeout constant; derive the discovery-freshness threshold from the poll cadence.
- `dev` changelog entry for the promoted `DISCOVERY_STREAM_POLL_INTERVAL_SECONDS` constant if the `libs/mngr` change warrants it (it is a rename/visibility change in `libs/mngr`, so a `libs/mngr` entry, not `dev`).

## Follow-up: collapse the two backend-unreachable tiers into one

> Branch `gabriel/recovery-redundancy`. This section revises the two-page provider/workspace split described above. It supersedes the `PROVIDER_UNAVAILABLE` / `WORKSPACE_UNREACHABLE` distinction throughout this document.

### Motivation

The shipped work split a backend-reachability failure into two tiers keyed on the **exception class name** the provider raised: `ProviderUnavailableError` → `PROVIDER_UNAVAILABLE` ("Can't connect to ..."; retry + auto-reconnect poll), anything else → `WORKSPACE_UNREACHABLE` ("Can't reach your workspace"; retry, no auto-reconnect). Two problems surfaced in testing:

- The two pages are, to the user, the same page: an error message plus a Retry button. The only real behavioral difference was that `PROVIDER_UNAVAILABLE` armed the background healthy-poll (auto-return on recovery) and `WORKSPACE_UNREACHABLE` did not — and that difference isn't justified, since a restored login recovers via the same 302 healthy-watch as a resumed daemon. The auth/config framing ("your account or login needs attention") added nothing the provider's own message didn't already say.

- The exception-name split **misroutes** the most common Docker case. A *stopped* daemon raises mngr's `ProviderUnavailableError` (→ `PROVIDER_UNAVAILABLE`), but a *paused* daemon answers with a `docker.errors.APIError` (503), which is deliberately **not** wrapped (an `APIError` means the daemon is reachable, which GC relies on). So "Docker Desktop is manually paused" fell through to `WORKSPACE_UNREACHABLE` — the wrong page — and silently lost the auto-reconnect. The same user intent ("Docker just isn't available right now") landed on different pages depending on the failure mode.

### Decision

Collapse `PROVIDER_UNAVAILABLE` and `WORKSPACE_UNREACHABLE` into a single `BACKEND_UNREACHABLE` tier: "a restart can't help, show the backend's own error and wait for it to recover." No sub-classification by error kind.

- `_classify_dispatch_tier` returns `BACKEND_UNREACHABLE` whenever this workspace has any provider error; the exception-name check (`_PROVIDER_UNAVAILABLE_EXCEPTION`) is deleted. Only the error **message** is carried (as `unreachable_reason`); `ProviderProbeError` and its `exception_type` field are removed, and `_provider_error_for_workspace` becomes `_provider_error_message_for_workspace` returning `str | None`.
- One render function, `renderBackendUnreachable`: a provider-agnostic message, the provider's own error surfaced verbatim, a Retry, and the background healthy-poll **always** armed. Diagnostics are suppressed on this tier — the cause is the external backend (shown verbatim), not anything the in-container probes inspect.
- The `ProviderUnavailableError` mngr error class is untouched; it still serves the list/GC paths. Only minds' recovery classification stops branching on it.

### Acceptance criteria (revision)

- A paused Docker daemon and a stopped Docker daemon both classify as `BACKEND_UNREACHABLE` and render the same page (same title, the provider's own error, Retry, auto-reconnect) — neither loses the auto-return.
- The page shows no "check your internet connection" copy and no Diagnostics disclosure.
- The recovery page JS has a single `backend_unreachable` branch; there is no `provider_unavailable` / `workspace_unreachable` branch or render function.

### Changelog (revision)

- `apps/minds` changelog entry: collapse the two backend-unreachable recovery pages into one that always surfaces the provider's own error, offers Retry, and always auto-reconnects on recovery; drop the misleading "check your internet connection" copy and the diagnostics disclosure on that page.
