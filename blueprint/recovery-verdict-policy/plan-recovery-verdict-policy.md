# Plan: recovery-verdict-policy

Server-side recovery classifier and dispatch semantics for minds workspace
recovery. One unit of the recovery-resilience work; follows the shared
principles in `apps/minds/docs/recovery-work-principles.md` (Principle 1:
auto-action only on unambiguous evidence; Principle 3: quiet surfaces still
report).

## Overview

- Drop the `INTERFACE_UNRESPONSIVE` tier and the entire surgical (services-scope)
  restart machinery. The in-place interface restart has never been the fix for
  real user issues, and auto-dispatching it off a heuristic verdict violates
  Principle 1. Exec-reachable-but-not-answering now renders the existing
  consent-gated `HOST_UNRESPONSIVE` page instead.

- Make the classifier interpret evidence it already collects rather than adding
  delays. From the in-container probe's supervisord status: `STARTING`/`BACKOFF`
  means self-heal is in progress (keep checking). From the discovery snapshot's
  host state: `STOPPING` is transitional (keep checking); `FAILED` renders the
  consent page; `STOPPED`/`CRASHED` render as offline. The tiers are
  display-only -- the recovery restart itself is dispatched unconditionally on
  page entry as an idempotent start-only restart (see "Expected behavior"),
  which is the path that revives workspaces after a laptop reboot.

- Add a dedicated `HostState.UNREACHABLE` so imbue_cloud outer-SSH auth
  rejection stops overloading `UNAUTHENTICATED`. minds maps it to the terminal
  `BACKEND_UNREACHABLE` page (retry/report only) with a canned message --
  restarting routes through the same rejected credential, so it cannot help.

- Correct the provider layer's `CRASHED` overloading: the three
  degraded-observation mints (imbue_cloud empty container state, unrecognized
  docker status, lima "Unknown") report `UNKNOWN` instead, so "could not
  determine state" never auto-restarts a host.

- Close the Principle 3 gap: every `RESTART_FAILED` branch reports at error
  level (reaches Sentry). Also close the stop/auto-restart collision: a
  workspace stopped through the agent-facing API now closes its open windows,
  the same way the landing-page stop already does.

## Expected behavior

- The recovery page never restarts the system-services agent in place, and
  never auto-restarts anything the probe cannot positively call safe to bounce.

- Container exec-reachable but interface not answering `GET /` with 200:
  - supervisord reports `STARTING` or `BACKOFF`: live "Reconnecting..." state,
    keep checking (supervisord is already fixing it).
  - any other supervisord result: consent-gated "Workspace unresponsive" page
    with a host-restart button. The page's liveness poll still auto-returns the
    user home the moment the interface self-heals, so no restart fires without
    a click.

- The recovery flow's restart is dispatched unconditionally on recovery-page
  entry as a start-only restart (`start_only` skips the stop step), with no
  knowledge of the host's state. The dispatch is commit-time-checked rather
  than trust-dependent: `mngr start` probes ground truth at dispatch time,
  no-ops on a live host, targets only STOPPED agents, and cold-boots a
  stopped one -- so a stopped workspace revives on first entry with no
  discovery-freshness crawl, and a workspace that merely blipped makes the
  dispatch a no-op while the liveness poll returns the user home. In-app
  stops (landing page and agent API) close open windows first, so a window
  that reaches the recovery page over a stopped host implies an out-of-app
  stop, and reviving it is intended. The classifier's tiers are display-only:
  a trusted `STOPPED`/`CRASHED` reading renders as `HOST_OFFLINE` on the
  failure page's diagnostics, but no dispatch ever branches on a tier.
  (History: this dispatch was originally tier-gated -- fire only on a
  classified `HOST_OFFLINE` -- which required the classifier to *know* the
  host was stopped. That spawned a freshness-gate exemption for the offline
  pair, then a persisted offline-state map with launch-time override seeding
  so the knowledge survived a quit, and that map proved unreconcilable
  against stale in-flight and replayed discovery snapshots, which kept
  erasing it (live traces 2026-07-21/22). Dispatching unconditionally removes
  the need for the knowledge, and with it that entire subsystem.)

- Host observed (with a trusted, post-onset snapshot) as:
  - `STOPPING`: transitional -- keep checking; it settles to STOPPED a moment
    later.
  - `FAILED`: consent-gated "Workspace unresponsive" page (a failed host is
    unresponsive; a plain start on a failed-to-create host mostly re-fails).
  - `UNREACHABLE`: terminal "Can't connect to ..." page with Retry and report
    only, showing the canned reason: "This machine's access to the workspace
    host was rejected. Retrying or restarting won't fix this -- the workspace
    may need to be recreated, or contact support." Subject to the same
    freshness gate as other negative verdicts (untrusted snapshot -> keep
    checking first).
  - `UNAUTHENTICATED`: unchanged -- consent-gated host restart (container
    observed running, inner SSH dead; the restart is the engineered fix).

- Degraded provider observations no longer read as crashes: imbue_cloud
  outer-SSH-OK-but-empty-state, unrecognized docker statuses, and lima
  "Unknown" all surface `UNKNOWN`, which classifies INDETERMINATE
  (keep checking) rather than auto-restarting.

- A slow crash loop (container boots, serves, dies) still auto-restarts each
  cycle by design: getting back into the workspace resets the cycle. Fast
  crash loops terminate at RESTART_FAILED, which never auto-dispatches.

- Stopping a workspace through the agent-facing `/api/v1/workspaces/<id>/stop`
  closes any Electron windows open to it (skipped when it is mid-restart), and
  in browser mode navigates the content frame to the landing page -- so an open
  view can no longer silently undo an agent-requested stop by auto-restarting.

- The v1 restart API accepts only `scope: "host"`; `"services"` returns 400.

- Every restart failure (stop step, start step, readiness timeout,
  services-agent-not-found, worker spawn failure) produces one error-level log
  per attempt, reaching Sentry through the loguru handler.

- `mngr` CLI behavior for `UNREACHABLE` hosts matches `UNKNOWN` semantics:
  listed by default, never GC'd (gc only destroys CRASHED/FAILED/DESTROYED).

Refinements from live testing (2026-07-15, testing on the branch build found
both stuck cases were caused by a dead discovery producer -- the
`mngr latchkey forward` supervisor died mid-session, so no post-onset snapshot
could ever land and the freshness gate never opened):

- An in-container exec probe that *completes* without reaching the container
  (nonzero exit or clean exit with no sentinel -- dead inner sshd, container
  not actually running) is direct fresh evidence and classifies the
  consent-gated HOST_UNRESPONSIVE without waiting on the snapshot-freshness
  gate. A probe *timeout* still classifies INDETERMINATE (it observed nothing;
  the macOS-sleep protection). A trusted `STOPPED`/`CRASHED` observation still
  wins over a completed exec failure, naming the condition precisely
  (HOST_OFFLINE).

- The exec probe is attempted not only when the host reads RUNNING but also
  whenever the workspace's provider has produced no discovery snapshot for
  over 90s (`is_workspace_discovery_stalled`, three missed 30s polls): with
  discovery down, the exec's outcome is the only direct evidence available,
  and it resolves the page to HEALTHY or the consent-gated verdict instead of
  an indefinite "Reconnecting". With discovery flowing, a trusted not-running
  state still skips the doomed exec round-trip.

- The diagnostics list gains "Is the system-services agent running?": the
  in-container script scans `/proc/*/environ` for the agent's `MNGR_AGENT_ID`
  marker (the same marker mngr's stop path kills by), so a stopped
  system-services agent is named as the cause rather than leaving only
  downstream supervisorctl connection errors. Purely diagnostic; the dispatch
  tier never branches on it.

Refinements from live testing of the app-restart flow (2026-07-20/21, driving
the staging build through quit/relaunch cycles with a stopped workspace
container):

- Provider errors carried by discovery snapshots that finished before the
  minds backend started are dropped at ingest (the snapshot's topology still
  merges as last-good). The detached `mngr latchkey forward` producer keeps
  polling while the app is closed and the quit flow deliberately stops the
  docker state container, so the pre-start backlog's last docker snapshot
  reliably carries a manufactured "Docker state container is stopped" error --
  which the startup replay then surfaced as a current BACKEND_UNREACHABLE at
  every launch. A genuinely-broken provider re-asserts its error on the first
  fresh cycle. (An age threshold does not work here: the gap error can be
  seconds old at replay.)

- No verdict page is a dead-end: the BACKEND_UNREACHABLE and HOST_UNRESPONSIVE
  renders re-arm the same slow re-probe the INDETERMINATE state uses, so a
  cleared provider error or a newly-landed STOPPED observation re-classifies
  and the flow continues without user action. Dispatching a restart silences
  the re-probe chain, so a stale in-flight probe result cannot overwrite the
  restarting state. Verdict renders also reset each other's residual elements
  (Retry button, provider-error paragraph).

- Background/diagnostic ``mngr exec`` calls in minds pass ``--no-start``
  (exec auto-starts the host by default): the periodic backup verification
  check was observed cold-booting a stopped container -- its online gate read
  the stale replayed RUNNING state -- which invalidated the recovery page's
  evidence mid-probe (the container came up behind the classifier's back and
  the page never dispatched or showed the offline restart). Two execs
  deliberately keep the auto-start behavior pending a design call: the
  tunnel-token inject behind share-enable and the agent-facing SSH-grant
  read/write; every diagnostic and cleanup exec fast-fails against a stopped
  host instead.

Redesign after further live testing (2026-07-22, quit/relaunch traces showing
the persisted offline map erased by stale in-flight and replayed discovery
snapshots -- see "Expected behavior"):

- The recovery page's restart dispatch is decoupled from the classifier
  entirely: a fresh entry POSTs the start-only restart (`start_only`, the
  renamed `host_already_stopped`) immediately and unconditionally, before any
  probe. The probe/classifier remains for display -- the failure page's
  diagnostics, the consent-gated manual stop+start, and the
  backend-unreachable page -- and all tiers are uniformly freshness-gated
  again (the offline pair's exemption is gone along with its reason).

- Deleted with the knowledge requirement: the persisted offline-state map in
  the resolver's last-good topology, its launch-time seeding as host-state
  overrides, its discovery reconcile, the classifier's offline freshness
  exemption, and the probe's offline exec-skip carve-out. Host-state
  overrides remain in-memory-only UI optimism for lifecycle clicks.

## Changes

mngr layer (`libs/mngr`, `libs/mngr_imbue_cloud`, `libs/mngr_lima`):

- `HostState` gains `UNREACHABLE`: "the host answered but rejected our access;
  observation of the container is impossible and retrying through the same
  path cannot help." Distinct from `UNKNOWN` (transient / unobservable) and
  `UNAUTHENTICATED` (container observed running, inner SSH dead).

- imbue_cloud `discover_hosts_and_agents`: the outer-SSH auth-failure fallback
  mints `UNREACHABLE` instead of `UNAUTHENTICATED` (the non-auth fallback stays
  `UNKNOWN`). Update the adjacent comments and the
  `_build_offline_details_from_lease` docstring; the PR #2247 deferral is now
  resolved.

- imbue_cloud `derive_host_state_from_raw`: empty `container_state` ->
  `UNKNOWN` (was CRASHED); `map_docker_status_to_host_state` unrecognized
  status -> `UNKNOWN` (was CRASHED). Notes/failure_reason strings updated to
  say the state could not be determined.

- lima `_LIMA_STATUS_TO_HOST_STATE`: `"Unknown"` -> `UNKNOWN` (was CRASHED).
  `"Broken"` stays CRASHED (limactl positively reports breakage).

minds policy layer (`apps/minds`):

- `recovery_probe.py`:
  - remove `DispatchTier.INTERFACE_UNRESPONSIVE`.
  - classifier consults the raw host state (not just the collapsed probe
    answer): offline set shrinks to `{STOPPED, CRASHED}`; `FAILED` ->
    `HOST_UNRESPONSIVE`; `STOPPING` -> INDETERMINATE; `UNREACHABLE` ->
    `BACKEND_UNREACHABLE` carrying the canned reason and provider label.
  - exec-reachable + curl non-200: consult the probe's supervisord state --
    `STARTING`/`BACKOFF` -> INDETERMINATE, anything else ->
    `HOST_UNRESPONSIVE`. curl 200 -> HEALTHY, unchanged.
  - update the module docstring and `_OBSERVED_RUNNING_STATES` comment block
    (the deferred-UNREACHABLE note resolves).

- `workspace_recovery.py`:
  - `run_restart_sequence` becomes host-only: drop the `is_host_restart`
    parameter, the surgical startup-wait constant, and the services tier label.
  - error-level logs on all five RESTART_FAILED paths: stop step failed, start
    step failed, readiness timeout (currently unlogged), services-agent-not-
    found (currently unlogged), plus the worker-spawn failure in `api_v1.py`.

- `api_v1.py` + `api_models.py`: restart route accepts only `scope: "host"`
  (400 for `"services"`); the skip-stop request field is `start_only`
  (renamed from `host_already_stopped`, which implied state knowledge the
  unconditional dispatch does not have); the restart dedup is unchanged. (The
  `auto_dispatched`+HEALTHY skip guard this plan originally left untouched
  was removed on main while this branch was in flight -- an auto-dispatched
  cold-boot degrades to a no-op via `mngr start` targeting only STOPPED
  agents, so no endpoint-side veto is needed.)

- stop flow: after a successful STOP, the backend broadcasts a one-shot
  `workspace_stopped` chrome SSE event (emitted from the
  `perform_mind_host_action` stop path so the landing-page and agent-API stops
  share one mechanism; requires a small one-shot broadcast hook on the
  chrome-events stream).

- Electron `main.js`: new `handleChromeSSEEvent` branch for `workspace_stopped`
  -> `detachWindowsForWorkspace(agentId)`, skipped when the workspace is
  mid-restart (same guard as `confirm-stop-mind`, whose own detach becomes a
  harmless redundancy). Browser mode: chrome shell navigates the content frame
  to the landing page on the same event. (These files are owned by the
  error-surfacing unit -- additive changes in a different region than their
  work; coordinate before merge.)

- `templates.py`: the `interface_unresponsive` JS branch (and its
  `scope: 'services'` dispatch) is removed, and the recovery script's fresh
  entry dispatches the start-only restart unconditionally -- `applyHealth`
  is display-only (no tier-branched dispatch remains). Unknown tiers fall
  back to the consent page.

- Tests updated across `recovery_probe_test.py`, `workspace_recovery_test.py`,
  `api_v1_test.py`, provider state-minting tests, and any ratchet counts that
  shrink.

- Changelog entries: `libs/mngr`, `libs/mngr_imbue_cloud`, `libs/mngr_lima`,
  `apps/minds`, and `dev` (this blueprint doc).

- Ships as one branch/PR (`gabriel/recovery-verdict-policy`): the minds
  classifier consumes the new provider state, so the layers land atomically.

### Follow-ups (recorded, not in scope)

- Genuine-evidence CRASHED sites left alone: `derive_offline_host_state`'s
  no-stop-reason default (shared canonical logic, also feeds GC), lima
  "Broken" / VM-provably-absent, and docker's connection-error offline path
  when the daemon is also unreachable at fallback time.

- Loop-persistence reporting: one error-level Sentry event when a workspace
  crosses N auto-restarts in a window (slow crash loops currently report
  nothing; each cycle logs at info).

- Verbatim `failure_reason` plumbing for UNREACHABLE: `DiscoveredHost` carries
  only `host_state`, so the page shows a canned reason; an additive schema
  extension would restore provider-verbatim fidelity.

- Error-surfacing unit: optionally give FAILED-condition consent pages tailored
  copy (today they show the generic "Workspace unresponsive" text).

## Acceptance criteria

- Classifier unit tests (pure `build_host_health_response` inputs -> tier):
  - exec OK + curl 200 -> HEALTHY; exec OK + curl non-200 + supervisord
    RUNNING/FATAL/EXITED/STOPPED/unparseable -> HOST_UNRESPONSIVE; exec OK +
    curl non-200 + supervisord STARTING or BACKOFF -> INDETERMINATE.
  - no tier value `interface_unresponsive` exists; no input produces it.
  - trusted STOPPED / CRASHED -> HOST_OFFLINE; STOPPING -> INDETERMINATE;
    FAILED -> HOST_UNRESPONSIVE; UNREACHABLE -> BACKEND_UNREACHABLE with the
    canned reason; UNKNOWN/absent -> INDETERMINATE; stale-snapshot gating is
    uniform across all host-state verdicts (a stale STOPPED is INDETERMINATE,
    or HOST_UNRESPONSIVE via a completed exec failure), and probe-timeout
    gating is unchanged (INDETERMINATE, including a timeout with a stale
    STOPPED reading).
  - The recovery page's fresh entry dispatches the start-only restart
    unconditionally (templates test), and `applyHealth` contains no dispatch.
- Provider unit tests:
  - imbue_cloud: outer-SSH `HostAuthenticationError` -> host state UNREACHABLE
    (agents re-attached, failure_reason carried); non-auth outer failure still
    UNKNOWN; empty/unrecognized container state -> UNKNOWN, not CRASHED.
  - lima: status "Unknown" -> UNKNOWN; "Broken" -> CRASHED.
- API tests: restart with `scope: "services"` -> 400; `scope: "host"` -> 202
  and the worker runs the host sequence; a dispatch for a never-probed
  workspace still proceeds (no health-keyed veto).
- Restart-failure reporting: each of the five failure branches emits exactly
  one `logger.error` per attempt (assert via log capture).
- Stop/window-close: stopping via the v1 API emits `workspace_stopped` on the
  chrome SSE stream; manual verification in the dev Electron app that an open
  workspace window closes on an agent-API stop and does NOT close when that
  workspace is mid-restart (tmux/CDP verification, not a pytest test).
- Full suite green via `just test-offload`; CLI docs regenerated if any mngr
  option text changed; changelog entries present for all five projects.
