# Recovery-resilience work: shared principles and unit contracts

Ground rules for the set of parallel work units spun out of the recovery
audit (see `subsystems-and-recovery.md` for the subsystem map and
`subsystem-recovery-updates.md` for the originating notes). Every unit's spec
must follow these principles and cite this file. When a spec needs to violate
one, it says so explicitly and explains why.

## Principle 1: auto-action only on unambiguous evidence

The app performs a recovery action on its own (restarting a process,
restarting a host) only when the evidence is unambiguous: a provably dead
process it owns, or an explicit user intent (a clicked button, a
`?intent=restart` navigation). Heuristic or ambiguous verdicts -- timeouts,
staleness thresholds, "unresponsive" classifications -- get a *surface* (a
pill, a badge, a page state) with a user-clickable action, never an automatic
one.

Corollaries:

- A supervisor may respawn its own child that has provably exited. It may not
  restart a child because output looks stale.

- A shown restart button must restart unconditionally when clicked; the
  design burden is on *when to show it*, not on second-guessing the click.

- Timeouts are non-evidence: a probe that timed out proves nothing and must
  not mint a destructive verdict (this is the merged PR #2370 semantics --
  keep them).

## Principle 2: never tear down a rendered view

Error, recovery, loading, and reconnecting states are layered *on top of* the
already-rendered view: a small pill anchored at the bottom of the view with
the error message and an action button, a modal, or a page-local state. The
app never navigates away from or destroys rendered content to show an error.

- The only full-window replacements are: content-renderer death (the
  `crashed.html` page from PR #2428) and death of the Python backend itself
  (nothing is left to serve the UI).

- A first render of something that is not up yet may show a loader. An
  already-rendered view never regresses to a loader.

- When the content behind an overlay is dead (a workspace that stopped
  answering), the overlay must visually own that fact so the frozen view
  reads as expected, not broken.

## Principle 3: quiet surfaces still report

Replacing loud failure paths (full-app takeovers, auto-restarts, OS
notifications) with calm in-app surfaces must not cost fleet visibility.
Any failure that gets a pill/badge/page state -- and any watched process
found dead -- also emits an opt-out-gated Sentry event at the moment it is
detected. Mechanically: `logger.error(...)` and above reaches Sentry through
the loguru handler (threshold is level 37; `warning` is 30 and does not);
`ObservableThread` already reports uncaught thread exceptions at error level,
but *expected* failure branches that catch and `logger.warning` stay
invisible. Reporting is per-detection (deduplicated by state transition, not
re-fired every poll tick); forever-retry loops escalate to one error-level
report when failure persists past a threshold, not one per attempt.

### Known reporting gaps (audited 2026-07-13), by owning unit

Failure paths that today produce no Sentry event (warning-level or below, or
no log at all). The owning unit closes the ones in its scope as part of its
work; transient-then-recovered cases should report only on persistence.

- **discovery-supervision**: producer bounce failure
  (`discovery_health.py:152`) and restart failure (`:292`) are warnings;
  the observe child being *found dead* is never itself error-reported
  (`discovery_stream.py:203` respawn failure is a warning;
  `forward_supervisor.py:521` SIGHUP failure is a warning); per-agent
  `mngr event --follow` follower death/respawn is info/debug
  (`stream_manager.py:459,487`), so a persistently-dead follower is silent;
  reverse-tunnel re-establishment failure is a warning (`ssh_tunnel.py:495`)
  with no escalation on persistent failure. (Consumer death and the
  watchdog's BLOCKED transition are already error-reported.)

- **recovery-verdict-policy**: the expected RESTART_FAILED branches -- stop
  step failed (`workspace_recovery.py:332`) and start step failed (`:342`)
  -- are warnings; only a restart-worker *crash* reaches Sentry via
  `ObservableThread`.

- **notification-policy**: backup provisioning failure after the retry
  budget is a warning plus the OS toast (`agent_creator.py:2013`) -- no
  error log or capture anywhere; Cloudflare tunnel setup failures are
  warnings plus the OS toast (`workspace_create.py:144,152`); share
  tunnel-token inject/clear failures are warnings
  (`tunnel_token_injection.py:51,68`); `notifyOpenFailed` in Electron is
  reached from a swallowed `.catch` with no capture (`main.js:815`).

- **error-surfacing**: a backend that exits with code 0 or dies by signal is
  deliberately ignored in `main.js` (`proc.on('exit')`, ~`:2932`) -- no
  takeover today, no capture; the nonzero-exit takeover is also not
  auto-captured (report is manual-only).

- **latchkey-nudge-delivery**: the no-match nudge is debug-level
  (`messaging.py:132`; the unit's `deliver()` swap adds the error-level
  report); the gateway follow-stream reconnect loop retries forever at
  warning (`permission_requests_consumer.py:237`) with no escalation when
  the gateway is persistently down; a latchkey auto-register store error
  marks the agent/host pair processed anyway at warning
  (`latchkey_auto_register.py:112`), permanently skipping that agent with
  no report -- the last two are adjacent scope this unit should pick up or
  explicitly hand off.

## Interface contracts between units

Two contracts let the units proceed in parallel. They are deliberately
minimal; the owning unit's spec elaborates them, and consuming units code
against the shapes below.

### Surfacing contract (owned by the error-surfacing unit)

Two patterns, both produced by the Python backend and rendered without
tearing down content (Principle 2):

- **Pill**: an overlay anchored to the bottom of a view. Payload shape
  (transported over the existing chrome SSE stream and the Electron
  `broadcastChromeEvent` fan-out):

  ```json
  {
    "id": "stable-identifier-for-dedup",
    "severity": "info | warning | error",
    "message": "human-readable, one sentence",
    "action": {"label": "Restart", "kind": "ipc | http", "target": "..."},
    "scope": "app | workspace:<agent_id>"
  }
  ```

  A pill with the same `id` replaces its predecessor; clearing is an explicit
  empty-message event for that `id`.

- **Page state**: a server-rendered state local to one page (landing page,
  sharing/settings page), for conditions that only matter where the user
  would act on them. Not transported as a pill; the page's own data source
  carries it.

### Environment-signals contract (owned by the environment-signals unit)

A small query API in the Python backend; consumers adopt it incrementally:

- `was_asleep_between(t0, t1) -> bool` -- did the machine sleep during the
  interval (wall-clock datetimes)?
- `last_wake_at() -> datetime | None`
- `is_online() -> bool` -- cached known-endpoint reachability, refreshed on
  a short interval.

Consumption semantics (binding on consumers):

- Signals only *suppress negative verdicts* or *re-arm budgets*; they never
  assert health.
- A timeout whose window overlapped a sleep is non-evidence: rerun the
  operation once rather than arithmetically adjusting the deadline. The one
  arithmetic exception: staleness detectors re-baseline to
  `max(last_event_at, last_wake_at)`.
- Offline suppresses *remote-provider* judgments and discovery remediation
  only. Local providers (docker, lima) work offline; their probes and
  discovery keep running and their verdicts stay trusted.

## The work units

Each unit is specced and implemented in its own workspace, self-contained:
it changes only the files in its own scope, codes against the contracts
above rather than reaching into a sibling unit's implementation, and leaves
a sibling's dead code alone if removing it would touch the sibling's files.

- **recovery-verdict-policy**: server-side classifier and dispatch semantics
  in `recovery_probe.py` / `workspace_recovery.py`. Drop the
  `INTERFACE_UNRESPONSIVE` tier and its surgical-restart machinery; decide
  auto-vs-consent for observed-STOPPED hosts; audit what mints
  CRASHED/FAILED in the docker/lima providers; add the dedicated
  `UNREACHABLE` provider state so outer-SSH auth rejection stops minting
  `UNAUTHENTICATED`. Touches Python only; the recovery-page JS is owned by
  error-surfacing.

- **error-surfacing**: the pill component, the recovery-page-to-modal
  conversion (the keep-checking probe/verdict JS in `templates.py` re-homed
  over the still-rendered workspace view), the landing-page
  "workspace discovery failed -- click to retry" faded state, and removal of
  the `BLOCKED` full-app takeover in `main.js`. Depends on PR #2428 landing
  first (same `main.js`/`shell.html` regions).

- **notification-policy**: OS notifications are reserved for agent-initiated
  messages (`POST /agents/<agent_id>/notifications`) -- decide there whether
  to wire the dead click-through `url` field and drop the inert `urgency`
  field. Backup-provisioning failure moves to a landing-page state;
  Cloudflare tunnel failure moves to the sharing/settings page; failed
  external-link open becomes an immediate alert. Prune the unused
  osascript/tkinter dispatcher channels.

- **discovery-supervision**: reduce `discovery_health.py` to detection only
  (drop the bounce/backoff/restart escalation); the latchkey forward
  supervisor respawns its own provably-dead observe child (Principle 1);
  expose a user-triggered restart-discovery endpoint for the landing-page
  state and the recovery modal; consumer death drives a pill instead of the
  takeover.

- **environment-signals**: the sleep tracker (backend heartbeat thread;
  optionally corroborated by Electron `powerMonitor` later) and the online
  detector, implementing the contract above. First task: empirically settle
  whether `time.monotonic()` advances during sleep on Apple Silicon, which
  decides whether the monotonic timeout sites are consumers at all.

- **latchkey-nudge-delivery**: swap the four permission handlers from
  fire-and-forget `send()` to the delivery-verifying `deliver()` (parses the
  `message_sent` JSONL event -- the only signal distinguishing "delivered"
  from "no agent matched", since `mngr message` exits 0 on no-match), with
  an error-level log on no-delivery and a bounded retry. Do not change
  `mngr message` CLI exit semantics. First task: verify in the
  default-workspace-template repo whether a blocked agent self-polls the
  durable grant (decides whether a lost nudge is latency or deadlock).
