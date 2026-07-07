# Minds subsystems: recovery mechanisms and gaps

A map of the minds app's subsystems, focused on how each one recovers from
failure -- or doesn't. Re-synthesized from `error-recovery-audit.md`,
`subsystem-resilience-report.md`, and the repo-root
`minds_background_process_audit.md`, re-grounded against the code on
`gabriel/recovery-audit`. Descriptive only: it records what exists, with
severity calibrated by how often a failure occurs and what a user actually
loses.

The system is four layers, each supervising the one below: the **Electron
shell** supervises the one **Python desktop client** (`minds run`), which
supervises the host-side **mngr processes** (a `mngr forward`
discovery-consumer/proxy subprocess and a detached `mngr latchkey forward`
supervisor that survives minds restarts), which front the **agent containers**
(supervisord-managed services from the external forever-claude-template repo).

Recurring pattern: detection is probe-confirmed rather than timer-only, retry
loops back off and retry forever rather than giving up, and shutdown/sign-out
paths resolve in favor of the user's intent.

---

## 1. Electron shell (backend supervision)

The shell owns exactly one OS process -- the `minds run` backend, spawned via
uv and gated on a ~10s port poll. Its other duties (window restore, startup
routing, quit) are defensively engineered and not interesting failure
surfaces.

**Recovery that exists.** A start failure or a nonzero-exit crash produces a
full-window error takeover with a Retry button that re-runs the whole
shutdown-start cycle, plus a report path that works with the backend down
(one-shot Sentry report with gzipped backend-log tails). Retry is user-driven
and uncapped; a persistently broken environment just keeps showing the
takeover. Quit always resolves (SIGTERM, then SIGKILL after 5s, then give up),
so the user can never be trapped.

**No recovery.**

- A backend that exits with code 0 or dies by signal (e.g. OOM) is
  deliberately ignored by the exit handler: no takeover, frozen UI, SSE
  reconnects spinning against a dead port. Only recovery is the user
  restarting the app. There is also no post-startup liveness probe, so a
  hung-but-alive backend is invisible at this layer.
- A crashed renderer process is completely undetected -- there are no
  `render-process-gone` / `unresponsive` / `did-fail-load` handlers -- leaving
  a blank or frozen view with no reload, takeover, or report. Rare event
  class; total silence when it happens.

---

## 2. Discovery and forwarding (the spine)

One detached `mngr latchkey forward` supervisor owns the single
`mngr observe` discovery *producer* (writes a shared events file); minds' one
`mngr forward` subprocess is the *consumer* -- it tails that file, spawns a
per-agent `mngr event --follow` subprocess for service registrations, and is
simultaneously the HTTP/WS proxy for all workspace traffic. When this pipeline
breaks, the workspace list and liveness dots freeze; if the consumer dies,
traffic stops entirely.

**Recovery that exists.**

- *Per-request proxy failures* map to 502/503/504; the 503 body is an
  auto-refreshing loader (1s meta-refresh), so a browser hitting the proxy
  during a workspace restart retries itself back to health. Persistent
  failure hands off to workspace health (section 3).
- *Per-agent `mngr event` follower death* is respawned on the next ~10s
  snapshot tick with known services preserved. Persistent respawn failure is
  logged and skipped; the workspace-health path eventually catches the
  fallout.
- *Producer stall* (>35s with no discovery event, which also covers a dead
  supervisor): the minds-side watchdog enters `RECONNECTING` and remediates
  forever -- one cheap SIGHUP bounce, then full supervisor restarts on a
  15s-to-300s capped backoff. Loaded workspaces keep working throughout
  (the proxy's routes are never expired); what freezes is home-screen data,
  switching to an unrouted workspace, and creation. The only surfaced signal
  is the providers panel's freshness counter; `RECONNECTING` never escalates
  to a takeover. Two structural notes: the observe child has no in-process
  supervision anywhere, so this watchdog is its sole resurrection path; and
  `restart()` is heavyweight (re-provisions the gateway, tunnels, and every
  managed VPS host), which is why the backoff is deliberate.
- *Consumer death* is detected immediately by the lifecycle watcher: terminal
  `BLOCKED`, whole-app error takeover with a Restart button. Proportionate --
  the proxy is the traffic plane and its bound port is baked into app state --
  but there is no auto-restart; the user must click.
- *Reverse SSH tunnels* (the gateway injection into each container) are
  supervised by a 30s health thread that re-establishes broken tunnels on the
  same remote port with capped backoff, forever -- a laptop that comes back
  online overnight recovers unattended. Forward tunnels fail per-connection
  into the 503 path.
- *Duplicate supervisors* (the PR #2285 stuck-new-mind incident) are reaped at
  `ensure_running` by a process-table scan scoped to the latchkey directory.
  (PR #2328's leash-to-embedder replacement has not landed on this branch.)

**If recovery fails.** A producer stall that remediation never fixes stays
`RECONNECTING` forever with only the passive counter -- and because the
recovery redirect (section 3) requires a post-outage discovery snapshot, a
workspace that breaks *during* a persistent stall sits on the auto-refresh
loader rather than reaching the recovery page.

---

## 3. Per-workspace health and recovery

The best-behaved subsystem. Proxy failure envelopes only *enroll suspects*
(connection-level failures and infra 5xx; app errors and routeless
`UNRESOLVED` are ignored); a 2s background probe loop is the sole authority,
and an unbroken 5s run of failed probes flips the agent to STUCK, navigating
that workspace's view to a recovery page. A freshness gate holds the redirect
until a discovery snapshot from after the outage onset lands, so the
classification never runs on pre-outage host state.

**Recovery that exists.** The recovery page runs an in-container diagnostic
probe (the literal commands and outputs are shown) and classifies the failure:
provider unreachable (Retry only, provider's verbatim error, background poll
that returns the user on recovery), host offline (unattended host restart),
interface unresponsive (unattended in-place restart), ambiguous (consent-gated
host restart), or already-healthy (sends the user straight back, preventing a
needless restart). The restart worker (`mngr stop`/`start` + a 15s/30s
readiness poll) is deduped by an atomic compare-and-set.

**If recovery fails.** Every failure path -- command error, readiness timeout,
even a crash of the worker thread itself -- converges on a visible
`RESTART_FAILED` with a reason string and a try-again affordance. The probe
loop keeps polling regardless, so a spontaneous recovery flips the workspace
back to HEALTHY on its own. The one unconfirmed edge: with no plugin route to
probe through, a cleanly-dispatched restart is reported done without
verification. Container-internal supervision (supervisord inside the FCT
container) is the invisible first line of defense below all of this; minds
sees it only through this page's diagnostics.

---

## 4. Latchkey permissions and gateway

Agent permission requests flow through a gateway owned by the detached
supervisor, reverse-tunneled into each container; minds holds a long-lived
follow-stream from the gateway into its request inbox. Approve/deny writes a
durable response event to disk, then nudges the waiting agent via
`mngr message`.

**Recovery that exists.**

- *Stream drop:* reconnect on 1s-30s backoff; the gateway re-emits everything
  still pending from its on-disk files on each reconnect, idempotent by
  request id. Fully self-healing for transient failures.
- *Gateway rebinds to a new port:* connect errors invalidate the client's
  cached URL; the next call re-reads the record and rebinds.
- *Failed approve/deny:* the request stays pending (never mis-recorded as a
  denial), the error surfaces in the dialog, and it is retryable. A grant is
  never silently dropped.

**No recovery / silent.**

- *Gateway permanently down* (or the consumer thread dead -- nothing watches
  it): the reconnect loop retries forever with log-only output. Permission
  prompts simply never appear and the requesting agent stays blocked; there is
  no "permission system offline" indicator anywhere.
- *Lost wake-up nudge:* fire-and-forget, judged by exit code -- and
  `mngr message` exits 0 even when no agent matched, so a nudge to nobody is
  neither detected nor retried. Latency, not loss: the grant is durable and
  the agent's own polling eventually picks it up. (A delivery-verifying
  `deliver()` exists in the same module with no production caller.)
- *Auto-register store error:* the agent/host pair is marked processed anyway,
  so one transient IO error permanently skips that agent -- its gateway calls
  are rejected until an operator runs `mngr latchkey register-agent`.

---

## 5. Auth and sessions

Layered credentials: a 30-day desktop session cookie (no refresh; re-auth is
the only recovery), a per-run in-memory `MINDS_API_KEY` for agent-to-minds
calls, and the plugin-owned SuperTokens account session (minds mirrors
identity only).

**Recovery that exists.** An expired cookie 302s page routes to `/login` and
401/403s API routes. Account-backend unavailability is a visible 502; OAuth
flows surface crashes as `state=error` rather than hanging; the identity cache
is deliberately not poisoned by transient failures (avoiding association
loss); sign-out fails open toward the user's intent (local mirror dropped even
if the connector revoke fails).

**No recovery / silent.**

- On cookie expiry the chrome SSE emits a one-shot `auth_required` that
  nothing meaningfully handles (Electron only clears titlebar accents;
  `chrome.js` has no handler) -- the sidebar sits empty with no re-sign-in
  prompt until the user happens to hit a page route that redirects.
- Sharing: the cloudflared tunnel-token inject/clear over `mngr exec` are
  best-effort and log-only -- a failed inject means the share silently never
  comes up; a failed clear leaves a stale token until the agent stops.

---

## 6. Workspace lifecycle (create / start-stop / destroy)

User-initiated paths, so failures are visible by construction -- the user is
watching a status surface when they happen.

**Recovery that exists.** Create reports per-phase FAILED with the error
string, and sub-failures degrade instead of aborting: latchkey wiring failure
becomes a warning (the agent still boots), backup setup detaches into its own
retry (section 7), tunnel failure becomes an OS notification. A readiness
timeout (300s; first boot legitimately takes 90-180s) publishes the redirect
anyway, landing the user on the auto-refresh loader instead of a dead spinner.
Start/stop failure clears the optimistic UI override so the UI reverts to
authoritative discovery rather than lying, and unknown liveness is rendered as
unknown rather than guessed. Destroy survives an app crash (detached process;
status is derived from pid-liveness crossed with host state, so a partial
destroy reads FAILED, never a false DONE), keeps failed records for
inspection, and Retry reuses a still-running record instead of
double-destroying.

**No recovery / silent.** Nothing notable.

---

## 7. Backups

Provisioning runs detached after create, retried within a 300s budget (with an
inner retry for just-minted credential propagation). The canonical env is
stored minds-side *before* injection and never auto-deleted -- even on destroy
-- so restore keeps working for dead workspaces.

**Recovery that exists.** Provisioning retries within its budget; status
queries degrade per-workspace errors to `UNKNOWN` and are wall-clock bounded,
so a wedged repo can never stall the UI; export/restore are synchronous, so
their failures land directly in the HTTP response.

**No recovery / silent.** A provisioning failure that exhausts the budget
produces one transient OS toast and a log line -- no persistent state. Miss
the toast and the workspace runs indefinitely with no backups, its tile
indistinguishable from "no backups yet". This is the one place a
data-protection feature can be silently off. (A persistent failure badge
exists only on the separate `gabriel/backup-failure` branch.)

---

## 8. Error reporting (the backstop)

Two Sentry pipelines (Electron and Python), opt-out gated per event. A manual
"Report a bug" path bypasses the opt-out and works even with the backend down
(one-shot main-process report plus gzipped backend-log tails); it is reachable
from the help button, the `BLOCKED` takeover, and the recovery pages. Gaps:
the quitting/loading takeovers have no report button, and the renderer-crash
state (section 1) has no surface at all -- neither auto-reported nor manually
reportable.

---

## Calibration notes

Where this report's severity judgment deliberately differs from the earlier
audits:

- **The real silent failures are visibility papercuts, not incidents.** The
  five that matter -- permission gateway/stream down, backup provisioning
  failure, backend code-0/signal death, renderer crash, expired-cookie empty
  sidebar -- lose no data and each has a mundane manual recovery (restart the
  app, sign in again, restart the workspace). Their cost is confused minutes.
- **The producer stall was overweighted.** Loaded workspaces are unaffected,
  the watchdog self-heals with unbounded retry, and its passivity is a design
  choice, not a hole.
- **The lost nudge is a latency bug, not a correctness bug** -- the grant is
  durably recorded before the nudge is attempted.
- **The renderer-crash gap is the largest by blast radius when it happens,
  not by expected frequency.** "Largest silent-failure surface in the
  product" overstated it.
- **The supervisor `restart()` "hammer" is half-justified**: its launch-time
  use is required (per-launch gateway key/port), and only its use as a
  stall remedy is debatable -- with the capped backoff already bounding the
  damage.
- **Branch accuracy:** PR #2328 (`--exit-alongside-pid` producer leashing) has
  not landed here -- duplicate-forward reaping is still the live mechanism,
  contrary to the resilience report's "resolved -- pending" framing. The
  earlier report's proposed mechanisms (tier framework, status chips,
  in-process respawns) are proposals, not current behavior, and are omitted
  above.
