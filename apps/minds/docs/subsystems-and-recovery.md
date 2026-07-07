# Minds subsystems: what they are, how they fail, how they recover

A subsystem-by-subsystem map of the minds app. For each subsystem: what it is
and how it works, the major things that can fail, what the user actually sees
when they fail, and the recovery mechanisms that exist today -- including what
happens when the recovery itself fails.

This re-synthesizes `error-recovery-audit.md`, `subsystem-resilience-report.md`,
and the repo-root `minds_background_process_audit.md`, re-grounded against the
code on `gabriel/recovery-audit`. It is deliberately descriptive: it records
what exists, not what should be built, and it calibrates severity by how often
a failure occurs and what a user actually loses -- not by how silent the code
path is in the abstract.

## The shape of the system

Four layers, each supervising the one below it:

1. **Electron shell** (`apps/minds/electron/`) -- spawns and supervises the one
   Python backend process (`minds run`), owns windows, startup routing, the
   full-window error takeovers, and quit.
2. **Desktop client** (`apps/minds/imbue/minds/`, a Flask/cheroot server) --
   the policy layer. Owns the health watchdogs, the recovery page, auth,
   backups, permissions UX, and the chrome SSE feed the shell consumes.
3. **Host-side mngr processes** -- one `mngr forward` subprocess (discovery
   consumer + HTTP/WS proxy) and one detached `mngr latchkey forward`
   supervisor (discovery producer + permission gateway + reverse tunnels),
   which survives minds restarts.
4. **Agent containers** (defined in the external forever-claude-template repo)
   -- supervisord-managed services whose outputs the host consumes: the
   system_interface web server, service-registration events, discovery records.

A recurring design pattern: detection is probe-confirmed rather than
timer-only, retry loops back off and retry forever rather than giving up, and
shutdown/sign-out paths resolve in favor of the user's intent.

---

## 1. Electron shell: backend supervision, windows, quit

**What it is.** `main.js`/`backend.js` spawn the single `minds run` backend via
uv, parse its stdout JSONL (`login_url`, `mngr_forward_started`,
notifications), and gate readiness on a ~10s port poll. A main-process loop
holds one SSE connection to the backend's `/_chrome/events` and fans events to
every renderer view over IPC (reconnecting forever on a 1.5s backoff). The
shell also owns window restore (`window-state.json`), startup routing, and the
quit sequence.

**What can fail, what the user sees, how it recovers:**

- **Backend fails to start** (uv sync offline, spawn failure, port never
  binds). User sees a full-window "Setup failed" / "Failed to start Minds"
  takeover with a Retry button that re-runs the full shutdown-start cycle.
  Retry is user-driven and idempotent, with no attempt cap; a persistently
  broken environment just keeps showing the takeover. The takeover has a
  report path that works with the backend down (one-shot main-process Sentry
  report with gzipped tails of the backend's own logs).
- **Backend crashes mid-session with a nonzero exit code.** "Minds stopped
  unexpectedly" takeover with the last log lines; user clicks Retry. No
  auto-restart.
- **Backend exits cleanly-but-unexpectedly (code 0) or is signal-killed
  (code null, e.g. OOM).** The exit handler deliberately ignores these
  (`main.js:2748`), so no takeover: the app keeps showing now-dead content
  (SSE reconnects spin, navigation fails). Recovery is the user restarting the
  app. There is also no post-startup liveness probe of the backend HTTP
  server, so a hung-but-alive backend is only detected by the per-workspace
  and discovery layers for their own slices.
- **A renderer process crashes.** There are no `render-process-gone` /
  `unresponsive` / `did-fail-load` handlers anywhere, so a crashed renderer is
  a blank or frozen view with no detection, auto-reload, or report. This is a
  rare event class in practice, but when it happens the only recovery is the
  user closing/reopening the window or app.
- **Startup routing lands wrong.** `fetchInitialChromeState` has a 25s
  timeout; on timeout the user is treated as unauthenticated and lands on
  `/welcome` -- wrong but safe, one navigation to fix. Window restore drops a
  window's workspace only on positive destroy evidence (`everSeenDestroying`),
  so a slow discovery snapshot never closes windows; the worst case is a
  window landing on Home. A 3s fallback timer surfaces windows even if their
  load events never fire.
- **Quit.** `runQuitSequence` always resolves: prompt about running minds (a
  failed liveness check is surfaced, not silently skipped), `#quitting`
  takeover, SIGTERM then SIGKILL after 5s, then give up and quit anyway. The
  user is never blocked from quitting; worst case is minds left running (their
  choice) or a force-killed backend. The `#quitting` and loading takeovers
  have no report button.

---

## 2. Discovery pipeline and forwarding proxy (the spine)

**What it is.** One detached `mngr latchkey forward` supervisor owns the
single `mngr observe --discovery-only` *producer*, which polls providers every
~10s and writes a shared discovery-events file. minds spawns one
`mngr forward --observe-via-file` subprocess -- the *consumer* -- which tails
that file, spawns one `mngr event <id> ... --follow` subprocess per agent for
service registrations, and is simultaneously the HTTP/WebSocket reverse proxy
for all `<agent>.localhost` traffic. It reports everything to minds as a JSONL
envelope stream on stdout, which feeds the backend resolver, which feeds the
chrome SSE, which feeds the shell.

**What can fail, what the user sees, how it recovers:**

- **A single proxied request fails.** The proxy maps connect errors to 503,
  mid-response read errors to 502, and 30s timeouts to 504, and emits a
  classification-free `system_interface_backend_failure` envelope for each.
  The 503 body is a styled "Loading workspace" page with a 1-second
  meta-refresh, so a browser hitting the proxy during a workspace restart
  auto-retries until the backend answers. Benign during restarts; if the
  backend stays down, the per-workspace health subsystem (section 3) takes
  over.
- **An agent has no route yet** (`UNRESOLVED`). Same 503 loader. Deliberately
  not enrolled for health probing -- a restart routes through the same proxy
  and cannot fix routing -- and it self-heals when the consumer catches up to
  discovery.
- **A per-agent `mngr event` subprocess dies** (e.g. host reboot). Detected on
  the next snapshot tick (~10s) and respawned with its known services
  preserved. If respawn keeps failing it is logged and skipped; the agent's
  services eventually go stale and the workspace-health path catches the
  fallout.
- **The producer stalls** (the observe child dies or stops emitting -- a dead
  supervisor manifests the same way). Detected by the minds-side
  `DiscoveryHealthWatchdog` (5s poll) when resolver freshness exceeds 35s:
  state goes `RECONNECTING`, one cheap SIGHUP bounce of the observe child,
  then full supervisor restarts on a 15s-to-300s capped exponential backoff,
  retrying forever. User-visible effect: for workspaces already loaded,
  nothing -- the consumer's in-memory routes are never expired, so traffic
  keeps flowing. What freezes is the workspace list, host-liveness dots, and
  anything needing fresh discovery (creating a workspace, switching to a
  not-yet-routed one). The only surfaced signal is the providers panel's
  "time since last discovery" counter; `RECONNECTING` never escalates to a
  takeover. If remediation never works, the app stays in `RECONNECTING`
  indefinitely -- and because the recovery redirect's freshness gate (section
  3) needs a fresh snapshot, a workspace that breaks *during* a persistent
  stall sits on the auto-refresh loader rather than reaching the recovery
  page.
  - Two structural notes, verified: the observe child has no in-process
    supervision (spawned `is_checked_by_group=False`, no exit callback in
    either the latchkey forward or the mngr forward process), so the minds
    watchdog is its sole resurrection path; and the watchdog's `restart()` is
    heavyweight (tears down and re-provisions the gateway, reverse tunnels,
    and every managed VPS host), which is why the backoff is deliberate.
- **The consumer dies** (the `mngr forward` subprocess exits). The minds-side
  lifecycle watcher fires `record_consumer_death` immediately: terminal
  `BLOCKED`, and the chrome redirects the whole app to an error takeover
  ("can't automatically reconnect", Restart button). This is the strongest
  signal in the app, and proportionate: the proxy is the traffic plane and its
  bound port is baked into app state, so nothing short of an app restart can
  fix it. There is no auto-restart; the user must click.
- **Duplicate supervisors** (historical incident: duplicate forwards caused
  stuck-new-mind 503s, PR #2285). `ensure_running` scans the process table
  and reaps duplicates scoped to the same latchkey directory before spawning.
  (PR #2328, which replaces reaping with leashing the producer to the
  embedder's PID, has *not* landed on this branch.)

---

## 3. Per-workspace health and recovery (STUCK -> recovery page -> restart)

**What it is.** The policy layer over section 2's failure envelopes. minds
enrolls an agent as a *suspect* only for connection-level failures or
infrastructure 5xx (502/503/504); app errors and `UNRESOLVED` are ignored. A
background probe loop (2s) is the sole authority: an unbroken 5s run of failed
probes flips the agent to STUCK, which navigates that workspace's content view
to a recovery page. The page runs a batched in-container `mngr exec`
diagnostic (30s outer / 5s inner caps, skipped when the provider is down or
the host isn't RUNNING) and classifies one of five tiers, in precedence order:
`BACKEND_UNREACHABLE` > `HOST_OFFLINE` > `HEALTHY` (a live 200 sends the user
straight back, preventing a needless restart) > `INTERFACE_UNRESPONSIVE`
(surgical restart) > `HOST_UNRESPONSIVE` (consent-gated host restart). The
restart worker runs `mngr stop`/`mngr start` and polls for the interface (15s
surgical / 30s host); `mark_restarting` is an atomic compare-and-set so
concurrent restart requests dedupe to one worker.

A freshness gate suppresses the STUCK redirect until a discovery snapshot
taken at/after the outage onset lands, so the tier classification never runs
against pre-outage host state (a just-stopped container still reading
RUNNING). The watchdog's 35s stall threshold sits above this 30s-class gate so
the two mechanisms never fight.

**What can fail, what the user sees, how it recovers:**

- **The system interface wedges or crashes.** User sees the auto-refresh
  loader for a few seconds, then the recovery page with live diagnostics (the
  literal probe commands and outputs) and a restart that auto-dispatches for
  unambiguous tiers and asks first for ambiguous ones.
- **The restart itself fails** -- stop/start errors, the interface doesn't
  come back in the window, or the worker thread crashes. Every path converges
  on a visible `RESTART_FAILED` with a reason string and a try-again
  affordance; a crashed worker reaches the same state via its `on_failure`
  handler. The probe loop keeps polling regardless, so a later spontaneous
  recovery flips the workspace back to HEALTHY on its own.
- **The provider/backend is unreachable** (connector down, Docker daemon
  stopped, expired login). `BACKEND_UNREACHABLE` short-circuits every other
  tier: the page shows the provider's verbatim error, offers only Retry (a
  restart would route through the same dead backend), and arms a background
  poll that returns the user the moment it recovers.
- **Edge:** if the forward port/preauth cookie is unset there is no way to
  probe, so a cleanly-dispatched restart is reported done without
  confirmation.

This is the best-behaved subsystem: probe-confirmed detection, deduplicated
restarts, and no failure path that ends silently.

---

## 4. Latchkey: permissions and gateway

**What it is.** The detached supervisor (section 2) also owns the shared
latchkey *gateway* -- a loopback web server on a dynamic port, reverse-tunneled
into every agent container at `:1989` -- through which agents make permission
requests and proxied service calls. On the minds side: a gateway client that
learns the port by polling the supervisor's on-disk record (0.2s cadence, 30s
cap, bails early if the supervisor is dead); a consumer thread holding a
long-lived `GET /permission-requests?follow=true` stream that pumps requests
into the in-app inbox; approve/deny handlers that write a durable response
event, DELETE the gateway record, and then send a fire-and-forget
`mngr message` nudge so the waiting agent wakes promptly; and an auto-register
callback that appends each newly discovered agent to its host's allowlist.

**What can fail, what the user sees, how it recovers:**

- **The stream drops** (network blip, gateway restart). Reconnect with 1s-30s
  exponential backoff; the gateway re-emits all still-pending requests from
  its on-disk files on each reconnect, and the inbox is keyed by `request_id`
  so redelivery is idempotent. A single malformed record is logged and
  skipped rather than killing the thread. Fully self-healing for transient
  failures.
- **The gateway never comes up / stays down.** The port-wait failure raises on
  a background thread (the UI comes up fine), and the consumer reconnect-loops
  forever with log-only output. User-visible effect: permission prompts simply
  never appear, and any agent waiting on one stays blocked. There is no
  "permission system offline" indicator; the failure is invisible until the
  user wonders why an agent is stuck. The same applies if the consumer thread
  itself dies (`is_checked=False` -- nothing watches it), though its loop is
  defended well enough that this requires an unexpected exception class.
- **The gateway rebinds to a new port mid-session.** Connect-level errors
  invalidate the client's cached URL; the next call re-reads the record and
  rebinds. One failed call, then self-healed.
- **The wake-up nudge is lost.** The handlers use the exit-code-judged
  `send()`, and `mngr message` exits 0 even when no agent matched -- so a
  nudge to nobody is neither detected nor retried. The grant itself is
  durable (gateway write + response event), so the consequence is latency,
  not loss: the user sees "granted" while the agent stays parked until its
  own next poll. (A delivery-verifying `deliver()` exists in the same module
  but has no production caller.)
- **Approve/deny fails.** The request stays pending -- never mis-recorded as a
  denial -- with a concrete error in the dialog and a retry. Approve
  deliberately does not tolerate a 404 (silently dropping a grant is worse
  than erroring); the cleanup DELETE does tolerate one.
- **Auto-register hits a store error.** Logged, and the pair is marked
  processed anyway -- a transient IO error permanently skips that agent, whose
  gateway calls are rejected until an operator runs
  `mngr latchkey register-agent`. Log-only.

---

## 5. SSH tunnels

**What it is.** `SSHTunnelManager` instances inside both the mngr forward and
latchkey forward processes (plus one in desktop-client state for
cross-workspace SSH). Forward (direct-tcpip) tunnels are created lazily per
connection; reverse tunnels (the gateway injection at `:1989`, cross-workspace
SSH) are supervised by a 30s health-check thread with 15s SSH keepalives.

**Failures and recovery.** A broken reverse tunnel is re-established on the
*same originally-requested remote port* (so in-container URLs keep working)
with per-tunnel exponential backoff capped at 300s, retrying forever -- a
laptop that comes back online overnight recovers unattended. During the gap
the agent-side endpoint is dead and there is no user-facing signal (agents'
permission requests queue on disk and are re-emitted on reconnect). A broken
forward tunnel surfaces as a connect error, i.e. the 503 loader, and flows
into the workspace-health path.

---

## 6. Auth and sessions

**What it is.** Four layered credentials, each with a distinct job:

- **Desktop session cookie** -- 30-day signed cookie minted by exchanging a
  single-use one-time code printed at startup. No refresh or sliding expiry.
  Signing-key generation is double-check-locked so a startup burst can't mint
  competing keys; corrupt code files fail closed.
- **`MINDS_API_KEY`** -- per-run in-memory bearer token for agent-to-minds
  calls (injected into the gateway env at supervisor spawn). Constant-time
  compare, fails closed, never persisted; rotates every `minds run`.
- **SuperTokens account session** -- owned entirely by the imbue_cloud plugin;
  minds mirrors identity and workspace associations only.
- **mngr_forward's subdomain cookie** -- pre-auth for agent-subdomain traffic,
  set by the shell at startup.

**What can fail, what the user sees, how it recovers:**

- **Session cookie expires.** Page routes 302 to `/login`; API routes return
  401/403 JSON. The chrome SSE stream emits a one-shot `auth_required` and
  closes -- but the Electron main process only clears titlebar accents on it,
  and `chrome.js` has no handler at all, so the sidebar sits empty with no
  re-sign-in prompt. The user recovers by navigating anywhere that 302s. A
  once-a-month mild confusion, not a data risk.
- **Account backend unreachable.** Visible 502 ("Authentication service is
  unavailable"); OAuth flows have a 10-minute TTL and run on a thread so a
  crash surfaces as `state=error` rather than an infinite "Waiting...". The
  identity cache is deliberately not poisoned by a transient `auth_list`
  failure (and orphan GC is skipped) to avoid losing workspace-account
  associations. Sign-out fails open toward the user's intent: the local
  mirror is dropped even if the connector revoke fails.
- **Sharing (Cloudflare tunnel).** Token absence or creation failure raises a
  user-facing `SharingError`. The token inject/clear over `mngr exec` are
  best-effort: a failed inject only logs (cloudflared never starts, so the
  share silently doesn't come up); a failed clear leaves a stale token until
  the agent stops. Log-only in both directions.

---

## 7. Workspace lifecycle: create, start/stop, destroy

**What it is.** Per-creation threads run the full clone -> key minting ->
`mngr create` -> readiness poll -> backup provisioning flow, reporting
per-phase status to the create UI. Start/Stop run `mngr` synchronously (300s
cap) with a short-lived optimistic UI override. Destroy runs as a detached
process with an on-disk record.

**What can fail, what the user sees, how it recovers:**

- **A create phase fails.** Visible FAILED status with the error string
  (credentials redacted from streamed output). Sub-failures degrade instead of
  aborting: latchkey wiring failure -> warning + empty permission setup (the
  agent still boots); backup setup -> detached retry (section 8); tunnel
  setup -> OS notification. The imbue_cloud fast path falls back to the slow
  rebuild only on a structured `FastPathUnavailableError`; anything else
  propagates visibly.
- **The new workspace is slow to become ready.** The readiness poll waits up
  to 300s (first boot legitimately takes 90-180s). On success it records a
  probe success so the chrome doesn't immediately bounce the fresh workspace
  to the recovery page. On timeout it publishes the redirect anyway, landing
  the user on the auto-refresh loader rather than a spinner that never
  resolves.
- **Start/Stop fails.** The optimistic override is cleared so the UI reverts
  to authoritative discovery rather than lying; `UNKNOWN` liveness is
  rendered distinctly from confirmed-stopped. The quit-time bulk stop
  recomputes liveness and offers Retry on partial failure.
- **Destroy fails or the app crashes mid-destroy.** Destroy survives an app
  crash (detached `start_new_session=True`; status is derived per-request
  from pid-liveness crossed with host state, so a partial destroy reads
  FAILED, never a false DONE). A FAILED record is kept with a log tail and a
  Retry that reuses a still-running record instead of double-destroying.
  Known cosmetic wart: a ~1s window where a successful destroy flashes
  FAILED before the next discovery tick corrects it.

---

## 8. Backups

**What it is.** Per-workspace restic repos. Provisioning runs detached after
create: idempotent, retried within a 300s/10s budget with an inner 60s/3s
retry for just-minted credential propagation, and the canonical env is written
to the minds-side store *before* injection into the container -- so minds can
always reach the repo even if injection fails. Credential files are atomic
0600 writes; the master password is write-once; envs are never auto-deleted,
even on destroy, so restore stays possible for dead workspaces. The landing
badge is derived from the snapshot list + an is-backing-up flag; export and
restore are synchronous.

**What can fail, what the user sees, how it recovers:**

- **Provisioning fails after the budget.** One transient OS notification
  ("Backup setup failed") and a log line -- on this branch there is no
  persistent failure state or badge, so if the toast is missed the workspace
  runs indefinitely without backups and its tile is indistinguishable from
  "no backups yet". This is the one place a data-protection feature can be
  silently off. (A persistent red-badge failure state exists on the separate
  `gabriel/backup-failure` branch.)
- **Status queries fail or hang.** Per-workspace errors degrade to `UNKNOWN`;
  the batch is wall-clock bounded (20s batch / 12s per call) with a
  non-blocking executor shutdown, so a wedged repo can never stall the page.
  Status works even when the workspace is offline (restic is queried
  directly).
- **Export/restore fails.** Synchronous and user-initiated, so the failure
  surfaces directly in the HTTP response; restore is capped at 600s and the
  temp restore tree is always removed in a `finally`.

---

## 9. Container-side services (external FCT repo)

**What it is.** Inside each agent container, supervisord keeps the
system_interface web server, the app watcher (service-registration JSONL),
cloudflared, and the agents themselves alive. The host consumes their outputs
through discovery and the per-agent event streams; minds has no direct control
plane into the container beyond `mngr exec`.

**Failures and recovery.** A dead or wedged in-container service manifests
upstream: system_interface death becomes proxy 503s -> the workspace-recovery
flow (whose surgical restart bounces the services agent, and whose diagnostics
run `supervisorctl status` inside the container); supervisord/container death
becomes host-tier recovery or a discovery signal. Supervisord's own restart
policy is the first line of defense and is invisible to minds except through
the diagnostics probe.

---

## 10. Error reporting (the backstop)

Two parallel Sentry pipelines (Electron JS and the Python backend), both gated
live per event by the same opt-out setting; the Python side rate-limits
per-exception and offloads oversized events/log attachments to S3. A manual
"Report a bug" path always bypasses the opt-out: the rich in-app collector
when the backend is up (reachable from the help button, the `BLOCKED`
takeover, and the recovery pages), and a one-shot main-process report with
gzipped backend-log tails when it is down. Gaps: the `#quitting`/loading
takeovers have no report button, and the renderer-crash state (section 1) is
neither auto-reported nor manually reportable because it has no surface at
all.

---

## Calibration notes

Where this report's severity judgment deliberately differs from the earlier
audits:

- **The genuinely-silent failures worth keeping in mind** are the permission
  gateway/stream being down (invisible until an agent's prompt never
  arrives), backup provisioning failure (transient toast only), a backend
  code-0/signal death (frozen app, no takeover), a renderer crash (blank
  view, nothing), and the expired-cookie empty sidebar. All five are
  *visibility* gaps: none loses data or corrupts state, and each has a
  mundane manual recovery (restart the app, sign in again, restart the
  workspace). They are papercuts that cost confused minutes, not incidents.
- **The producer stall was overweighted** in the earlier docs. A user with
  workspaces already loaded is unaffected (routes never expire); the
  freeze is scoped to the home screen, switching, and creation; and the
  watchdog already self-heals with unbounded retry. Its passivity is a design
  choice, not a hole.
- **The lost approval nudge is a latency bug, not a correctness bug.** The
  grant is durably recorded before the nudge is attempted; the agent's own
  polling is the actual delivery guarantee.
- **The renderer-crash gap is the largest by blast-radius-when-it-happens,
  not by expected frequency.** Electron renderer crashes are rare; "largest
  silent-failure surface in the product" (the earlier framing) overstates the
  expected cost.
- **The supervisor `restart()` "hammer" is half-justified.** Its launch-time
  use is required (the gateway's per-launch bearer key and backend port are
  injected only at spawn). Only its use as a stall remedy is debatable, and
  the capped backoff already bounds the damage.
- **Branch accuracy.** PR #2328 (`--exit-alongside-pid` producer leashing) has
  *not* landed here -- duplicate-forward reaping is still the live mechanism,
  contrary to the "resolved -- pending" framing in the resilience report. The
  persistent backup-failure badge likewise exists only on
  `gabriel/backup-failure`.
- The earlier resilience report's tier framework and its proposed mechanisms
  (status chips, in-process respawns, emit-site consolidation) are proposals,
  not current behavior; they are intentionally omitted from the subsystem
  descriptions above.
