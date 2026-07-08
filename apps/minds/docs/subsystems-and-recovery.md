# Minds subsystems: recovery mechanisms and gaps

A map of the minds app's subsystems, focused on how each one recovers from
failure -- or doesn't.

The system is four layers, each supervising the one below: the **Electron
shell** supervises the one **Python desktop client** (`minds run`), which
supervises the host-side **mngr processes** (a `mngr forward`
discovery-consumer/proxy subprocess and a detached `mngr latchkey forward`
supervisor that survives minds restarts), which front the **agent containers**
(supervisord-managed services from the external forever-claude-template repo).

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
  `ensure_running` during startup by a process-table scan scoped to the latchkey directory.

**If recovery fails.** A producer stall that remediation never fixes stays
`RECONNECTING` forever with only the passive counter -- and because the
recovery redirect (section 3) requires a post-outage discovery snapshot, a
workspace that breaks *during* a persistent stall sits on the auto-refresh
loader rather than reaching the recovery page.

---

## 3. Per-workspace health and recovery

Proxy failure envelopes only *enroll suspects* (connection-level failures and
infra 5xx; app errors and routeless `UNRESOLVED` are ignored); a 2s background
probe loop is the sole authority, and an unbroken 5s run of failed probes
flips the agent to STUCK, navigating that workspace's view to a recovery page.
A freshness gate requires a discovery snapshot from after the outage onset
before the classification is trusted, so it never runs on pre-outage host
state.

**Recovery that exists.** The recovery page runs an in-container diagnostic
probe (the literal commands and outputs are shown) and classifies the failure:
provider unreachable (Retry only, provider's verbatim error, background poll
that returns the user on recovery), host offline (unattended host restart),
interface unresponsive (unattended in-place restart), ambiguous (consent-gated
host restart), or already-healthy (sends the user straight back, preventing a
needless restart). The restart worker (`mngr stop`/`start` + a 15s/30s
readiness poll) is deduped by an atomic compare-and-set.

**If recovery fails.** Every restart failure path -- command error, readiness
timeout, even a crash of the worker thread itself -- converges on a visible
`RESTART_FAILED` with a reason string and a try-again affordance. The probe
loop keeps polling regardless, so a spontaneous recovery flips the workspace
back to HEALTHY on its own. The one unconfirmed edge: with no plugin route to
probe through, a cleanly-dispatched restart is reported done without
verification. Container-internal supervision (supervisord inside the FCT
container) is the invisible first line of defense below all of this; minds
sees it only through this page's diagnostics.

**Known flaw, fixed by PR #2370 (open).** This subsystem was less well-behaved
than the framing above suggests: its *verdict* states did not keep checking.
Diagnosed from a real incident (Sentry `fc54dc12`): the in-container probe was
launched just before a laptop suspended, spanned the sleep, and was declared
timed-out at wake; combined with a pre-sleep `RUNNING` snapshot it
misclassified as `HOST_UNRESPONSIVE` -- and that consent-gated verdict page
never re-polled, stranding the user on a dead-end screen for a workspace that
answered ~3s later. Separately, the freshness gate held the STUCK *redirect*
(not just the verdict), so a stalled discovery pipeline stranded users on the
"Loading workspace" loader instead. PR #2370 makes three changes: the recovery
page arms a cheap idempotent liveness poll under *every* waiting and terminal
state (the moment the workspace answers, the user goes home); a timed-out
probe becomes non-evidence -- a new `INDETERMINATE` "keep checking" tier
instead of `HOST_UNRESPONSIVE` (only a clean-exit-with-no-sentinel, i.e. ssh
provably dead, keeps that verdict); and the freshness gate moves from the
redirect to the verdict path, so the page appears promptly and shows a live
"Reconnecting..." state rather than an indefinite loader when the snapshot
isn't trustworthy yet.

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

## Timeouts and sleep/wake

Nearly every recovery mechanism above is armed by a timeout, and a laptop
sleeping is the one event that fires many of them at once. On Apple Silicon
Macs the monotonic clock (`time.monotonic`, subprocess timeouts,
`Event.wait`) advances *during* sleep, so to every duration-based timer a
30-minute nap is indistinguishable from a 30-minute outage; wall-clock timers
obviously elapse too. (On Intel Macs some monotonic timers pause instead --
the incident behind PR #2370 confirms the elapsed behavior in practice.) The
timers fall into three classes with very different wake behavior:

**Data-age timers (wall clock) -- fire at every wake, by construction.** The
discovery watchdog's 35s stall threshold compares wall-clock `last_event_at`
to now, so any sleep longer than 35s makes the first post-wake tick read
"stalled" and SIGHUP-bounce the observe child -- every wake pays a
producer respawn for a producer that was never broken. The escalation math is
tighter than it looks: the first full `restart()` fires 15s after the bounce
if no event has landed, and a bounced observe needs a process spawn plus a
~10s provider poll (against possibly still-waking networking) to produce one
-- so a wake can plausibly escalate to the heavyweight supervisor restart
(gateway + tunnels + VPS re-provision) on a healthy system. The backoff cap
then bounds the damage, and a single fresh event resets everything.

**Duration timers (monotonic) -- treat the sleep as a real outage.** These
are the misclassification sources:

- The health tracker's 5s failure-run: one failed probe just before sleep
  plus one just after wake (while tunnels/proxy are still rebuilding)
  computes a run that spans the whole sleep -- instant STUCK for a healthy
  workspace. Common at wake; previously the entry point to the PR #2370
  incident.
- The recovery page's 30s in-container probe cap: a probe spanning sleep is
  declared timed-out at wake. Pre-#2370 that was treated as evidence
  (`HOST_UNRESPONSIVE` dead-end); post-#2370 it is non-evidence
  (`INDETERMINATE`, keep checking).
- Restart readiness windows (15s/30s) and mngr command caps (30-300s):
  sleeping mid-restart or mid-start/stop yields a spurious `RESTART_FAILED`
  or timeout error at wake. Visible and retryable, and the probe loop flips
  the workspace back on its next success, so these are transient noise.
- One-shot budgets are the sticky case: sleeping through the creation
  readiness window (300s) is benign (the redirect publishes anyway, landing
  on the auto-refresh loader), but sleeping through the backup provisioning
  budget (300s, tenacity `stop_after_delay`) permanently consumes it --
  the retry budget is spent on a sleeping machine, the one toast fires, and
  section 7's silent no-backups state follows. Sleep is the most plausible
  real-world way to hit that failure.

**Reconnect loops -- sleep-robust by design.** The chrome SSE loop (1.5s
retry), the permission follow-stream (backoff capped at 30s, disk-backed
re-emit of everything pending on reconnect), and the reverse-tunnel health
thread (30s checks; the backoff counter only grows on *awake* failed
attempts) carry no duration state across sleep. At wake they find a dead
socket, reconnect within seconds, and lose nothing. The proxy's 1s
auto-refresh loader is the same shape on the HTTP side.

The pattern that separates transient wake-noise from stranding: **misfires
are harmless wherever a poll keeps running and success always wins**
(`record_probe_success` overrides every bad state; the loader auto-refreshes;
tunnels rebuild). The two places sleep produced sticky failures are exactly
where that principle was violated -- the pre-#2370 verdict pages that never
re-checked, and the backup budget that never re-arms. Local containers
suspend and resume with the laptop; remote/VPS workspaces keep running
through it, so for them wake recovery is purely client-side re-establishment
(tunnels, then discovery freshness).

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
- **Conversely, "model citizen" flattered the workspace-recovery flow.** Its
  restart paths really do all converge on visible states, but its verdict
  pages didn't keep checking -- a real user was stranded 38 minutes on a
  dead-end `HOST_UNRESPONSIVE` page after a sleep/wake (the PR #2370
  incident). The earlier audits graded surfaces by whether failure paths
  were *visible*; this one failed by being visible but *stale*.
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
