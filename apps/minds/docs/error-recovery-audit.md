# Minds error-recovery audit

A user-facing audit of the high-level error-recovery mechanisms in the minds app
(`apps/minds`) and its mngr-plugin dependencies (`mngr_forward`, `mngr_latchkey`,
`mngr_imbue_cloud`). It is organized by subsystem. For each recovery mechanism it
answers four questions:

1. **What can go wrong** (the user-facing failure, not individual exceptions).
2. **How it is detected.**
3. **What the recovery does.**
4. **What happens if the recovery itself fails** -- and crucially, whether the user
   gets a visible signal or is left in a silent/stuck state.

This is a read-only audit against branch `gabriel/recovery-audit`. No code was changed.

---

## The shape of the system

Minds is an Electron shell that supervises a local Python backend (`minds run`).
The backend is a desktop client that talks to `mngr` (the agent runtime) exclusively
through the `mngr` CLI and a set of plugins. A workspace is a persistent `mngr` agent
in a container, fronted by an in-container "system interface" web server. The desktop
client discovers, routes to, and recovers those workspaces.

Recovery responsibility is split across four layers, and the split is load-bearing:

- **Electron shell** (`electron/*.js`) -- supervises the backend process, owns the
  full-window "takeover" error screens, window restore, and the quit flow.
- **mngr-forward plugin** (`libs/mngr_forward`) -- the discovery *consumer* and the
  HTTP/WS *proxy*. It is deliberately origin-agnostic: it detects backend failures
  and emits envelopes, serves a styled auto-refresh 503 loader, but never decides
  what a failure *means* or where to navigate.
- **Desktop-client backend** (`imbue/minds/desktop_client/*`) -- the policy layer.
  It interprets the plugin's envelopes, runs health probes, drives the per-workspace
  recovery page, manages auth/sessions/backups/latchkey, and emits the chrome SSE
  stream that the shell listens to.
- **mngr plugins** (`mngr_latchkey` supervisor, `mngr_imbue_cloud` connector) and
  the **container-side bootstrap** -- the lifecycle and infra substrate.

A single recurring pattern threads through nearly every mechanism: **probe-confirmed,
forever-retry-with-backoff, fail-toward-the-user's-intent.** Recovery decisions are
almost never timer-driven alone; they are backed by a live probe. Most retry loops
back off and retry indefinitely rather than giving up. And shutdown/quit/sign-out
paths always resolve in favor of letting the user proceed.

---

## 1. Electron shell: backend supervision & app startup

### 1.1 Backend won't start (env sync / spawn / readiness)
- **Goes wrong:** `uv sync` fails (offline/disk/corrupt cache); the Python process
  fails to spawn or exits before emitting its `login_url`; or it emits the URL but
  never binds its port.
- **Detected:** non-zero `uv sync` exit (`env-setup.js:118`); `child.on('error')` /
  `child.on('exit')` before readiness (`backend.js:340-357`); `waitForPort` 50x200ms
  (~10s) readiness gate (`backend.js:296-300`). A stale process on the chosen port is
  handled pre-spawn by polling 6s then picking a new port (`backend.js:144-148`).
- **Recovery:** full-window takeover via `showErrorInAllWindows` ("Setup failed --
  you may not be connected to the internet" / "Failed to start Minds"), with a Retry
  button that re-runs the whole shutdown -> start cycle (`main.js:2550-2557, 2758-2760, 3118`).
- **If recovery fails:** the takeover stays, Retry is idempotent, and because the
  backend is down the report button uses the one-shot main-process Sentry path -- so
  the failure is surfaced and reportable. **Gap:** Retry has *no attempt cap and no
  backoff* -- a persistently failing backend loops forever.

### 1.2 Backend crashes after a good start
- **Goes wrong:** the backend process dies mid-session.
- **Detected:** a `proc.on('exit')` handler reattached on every start, firing only
  when `code !== 0 && code !== null && bundles.size > 0` (`main.js:2746-2756`).
- **Recovery:** "Minds stopped unexpectedly" takeover with the last 50 log lines.
  No auto-restart; user clicks Retry.
- **If recovery fails / gaps:** a **clean-but-unexpected exit (code 0)** or a
  **signal kill (`code === null`, e.g. OOM/SIGKILL)** is *deliberately ignored* -- no
  error screen, the app keeps showing now-dead content (SSE stops, navigation fails).
  There is also **no post-startup liveness probe**, so a hung-but-alive HTTP server
  is not detected here at all. These are real silent-failure paths.

### 1.3 Startup routing lands on the wrong screen
- **Goes wrong:** a signed-in user is bounced to `/welcome`; restored windows point
  at workspaces that no longer exist.
- **Detected/recovered:** pure decision in `decideStartupRoute` driven by
  `fetchInitialChromeState`. Restore filters saved windows against live workspaces
  plus a persisted last-good topology, so a slow provider doesn't drop valid windows.
- **If it fails:** the `fetchInitialChromeState` call has a 25s timeout; on timeout it
  returns `null` and is treated as unauthenticated, **bouncing an already-signed-in
  user to `/welcome`** (`main.js:2200-2205`). Worst case is a wrong-but-safe landing
  screen; the user re-navigates. No crash.

### 1.4 Window restore / lost windows
- **Goes wrong:** a window is lost on restart, left on a destroyed workspace, or the
  last window closing accidentally quits the app.
- **Recovery:** `window-state.json` (legacy shapes migrated), URLs canonicalized to
  port-independent `/goto/<agent>/`, off-screen bounds re-applied. A window is
  navigated away from a vanished workspace **only with positive destroy evidence**
  (`everSeenDestroying`), so a transient discovery blip never closes it; the last
  window is navigated to `/` rather than closed (`main.js:325-355, 1543-1560`).
- **If it fails:** a window whose workspace truly vanished lands on Home. Non-fatal.

### 1.5 Window never paints (stuck splash)
- **Recovery:** windows show on `did-finish-load` OR `ready-to-show`, with a 3000ms
  fallback `setTimeout(surface)` so a window appears even if neither event fires
  (`main.js:560-575`).
- **Gap (significant):** there are **no `render-process-gone` / `unresponsive` /
  `did-fail-load` / `child-process-gone` handlers anywhere in `main.js`**. If a
  renderer (chrome, content, or modal view) crashes, the app shows a blank/frozen
  view with no detection, no auto-reload, no error screen, and no Sentry report.
  This is the **largest silent-failure surface in the shell.**

### 1.6 Quit / shutdown
- Single chokepoint `runQuitSequence` (`main.js:3231-3285`): prompt to stop running
  minds (a failed liveness check surfaces "Could not check for running minds" rather
  than silently quitting), flip windows to a `#quitting` takeover, then `shutdown()`
  does SIGTERM -> SIGKILL after 5s -> give up after 500ms, always resolving. Signal
  handlers route through the same chain headless. The user is never blocked from
  quitting; worst case is minds left running (a user choice) or a force-killed backend.

---

## 2. Discovery, forwarding & reachability

This is the spine: if discovery or forwarding breaks, the workspace list, liveness
dots, and all agent traffic freeze.

### 2.1 Per-workspace backend unreachable -> STUCK -> recovery page
- **Goes wrong:** a loaded workspace stops answering (system interface wedged/crashed,
  502/503/504).
- **Detected:** the proxy emits a `system_interface_backend_failure` envelope on any
  backend trouble (`server.py:435`), but **classifies nothing**. The desktop client
  decides which failures matter: only connection-level failures or infra 5xx enroll a
  *suspect* (`system_interface_health.py:69`); app 4xx/5xx and `UNRESOLVED` are
  ignored. An enrolled suspect is then actively probed every 2.0s; HEALTHY->STUCK
  fires only after an unbroken 5.0s run of probe failures -- never an ephemeral blip.
- **Recovery:** STUCK navigates the content view to a recovery page that runs a
  diagnostics probe and offers a surgical (services) or host restart.
- **If it fails:** every restart failure path converges on a visible `RESTART_FAILED`
  state with a reason string and a try-again affordance; even a crashed restart worker
  reaches it via `RestartWorkerFailureHandler`. The probe loop keeps polling so a
  later spontaneous recovery flips it back to HEALTHY. **This flow is the model
  citizen of the codebase -- no silent failure.**

### 2.2 Recovery-redirect freshness gate
- **Goes wrong:** STUCK fires but the discovery snapshot predates the outage (a
  just-stopped container still reads RUNNING), which would misclassify the recovery.
- **Recovery:** the STUCK redirect is suppressed until a discovery snapshot taken
  *at/after* the outage onset lands (`app.py:158`). The 35s watchdog stall threshold
  sits deliberately above this 30s freshness gate so the two never fight.
- **If it fails:** during a *persistent* discovery stall the post-onset snapshot never
  arrives, so the user sits on the auto-refreshing **"Loading workspace" loader with
  no explicit message**. This is intentional -- the discovery watchdog (2.5) is the
  backstop -- but it is a silent-ish window from the user's seat.

### 2.3 Plugin-level connect/SSE/timeout (per request)
- The proxy maps connect errors -> 503, mid-response read errors -> 502, timeouts
  (30s) -> 504. The 503 is itself a styled "Loading workspace" page with a **1-second
  meta-refresh**, so a browser hitting the proxy mid-restart auto-retries until the
  backend answers -- no blank flash. Loops benignly during a restart; becomes the
  stuck-loader case if the backend never returns (then 2.1 takes over).

### 2.4 Unresolved agent / per-agent event-stream death
- A routeless agent emits `UNRESOLVED` -> 503 loader, and is *deliberately not*
  enrolled for probing (a restart can't fix routing; it self-heals when the proxy
  catches up to discovery). Separately, the long-lived per-agent `mngr event --follow`
  subprocess can die (host reboot); it is detected on the next snapshot tick (~10s)
  and respawned, preserving known services. If respawn keeps failing it is logged and
  skipped -> services stay empty -> eventually STUCK.

### 2.5 Discovery-pipeline watchdog (producer stall / consumer death)
- **Goes wrong:** the producer (`mngr observe`) emits nothing, or the consumer
  (the single `mngr forward` subprocess, which is *also* the HTTP proxy) dies.
- **Detected:** producer stall = resolver `last_event_at` older than 35s; consumer
  death is fed directly from the subprocess lifecycle watcher.
- **Recovery:** producer stall -> `RECONNECTING`: one cheap SIGHUP `bounce`, then full
  `restart`s on capped exponential backoff (15s -> ... -> 300s cap), **retrying
  forever.** Consumer death -> terminal **`BLOCKED`** (can't be fixed by producer
  remediation; the proxy's bound port is baked into app state).
- **If it fails:** `BLOCKED` redirects the **whole app** to a full-window error
  takeover ("disconnected ... can't automatically reconnect", Restart button) -- the
  strongest visible signal, and the backstop for 2.2's silent loader. But
  `RECONNECTING` surfaces **nothing** except a passive "time since last discovery"
  counter in the providers panel, and never escalates. **A producer that stalls
  forever while the consumer stays alive is the least-surfaced failure in the
  subsystem.**

### 2.6 SSH tunnel health
- Keepalives (15s) plus a 30s reverse-tunnel health thread detect half-dead tunnels.
  Broken reverse tunnels are re-established on the **same originally-requested remote
  port** (so in-container URLs keep working) with per-tunnel capped backoff (300s),
  **retrying forever** -- a laptop that comes back online overnight recovers. A broken
  forward tunnel surfaces to the browser as a connect error -> 503 -> STUCK; a broken
  reverse tunnel is invisible during the gap (best-effort, logged only).

### 2.7 Provider unreachable (recovery-page short-circuit)
- When the discovery snapshot carries a provider error (connector down, Docker daemon
  stopped, expired login), the recovery page classifies `BACKEND_UNREACHABLE`, which
  beats every host/interface tier, skips the doomed in-container probe, offers **only
  Retry** (a restart would route through the same dead backend), shows the provider's
  verbatim error, and polls to return the user when it recovers. Fully visible.

---

## 3. Workspace lifecycle & recovery (desktop client)

### 3.1 Recovery diagnostics + dispatch-tier classification
- The recovery page runs a batched in-container `mngr exec` probe (capped 30s, inner
  5s) **only** when the provider has no error and the host is RUNNING, and classifies
  one of five tiers in precedence order: `BACKEND_UNREACHABLE` -> `HOST_OFFLINE` ->
  `HEALTHY` (a live `curl /` 200 short-circuits and sends the user home, beating a
  needless restart) -> `INTERFACE_UNRESPONSIVE` (surgical restart) -> `HOST_UNRESPONSIVE`
  (consent-gated host restart). If the exec can't reach the container the sentinel is
  absent and it falls to the consent-gated tier rather than a wrong auto-dispatch.
- **If it fails:** with no concurrency group wired the health endpoint returns a 503
  "unavailable in this configuration" -- degraded but visible.

### 3.2 Restart worker (surgical / host)
- `run_restart_sequence` does `mngr stop` + `mngr start`, then polls the interface for
  15s (surgical) / 30s (host). `mark_restarting` is an atomic compare-and-set so
  concurrent requests dedupe to one worker; an auto-dispatched restart is skipped if
  the workspace already recovered. Every failure path -> `RESTART_FAILED` + a reason,
  surfaced on the recovery page. **No silent failures**, with one optimistic edge:
  if the forward port/cookie is unset a clean dispatch is reported DONE without
  confirming the interface actually came back.

### 3.3 Creation readiness + create-flow fallbacks
- A new workspace's interface can take 90-180s to bind; `_wait_for_workspace_ready`
  polls up to 300s. On success it records a probe success so the chrome doesn't bounce
  the fresh workspace to recovery. **On timeout it publishes the redirect anyway**, so
  the user lands on the proxy's auto-refresh retry page rather than spinning forever.
  Create-flow sub-failures degrade gracefully: latchkey wiring -> warning + empty
  setup (agent still boots); backup setup -> detached retry; tunnel -> OS notification;
  `mngr create` itself failing -> visible FAILED status with the error string.

### 3.4 Host start/stop + optimistic override
- Start/Stop on a local workspace runs `mngr` synchronously (300s cap) and sets a
  short-lived optimistic UI override that reconciles to authoritative discovery on the
  next poll. On failure the override is **cleared** so the UI reverts to truth and
  doesn't lie; the quit-time bulk stop recomputes liveness and offers Retry on partial
  failure. `UNKNOWN` liveness is deliberately distinguished from "confirmed stopped"
  so the UI shows "we can't tell" rather than a wrong dot.

### 3.5 Detached destroy with crash survival
- Destroy runs as a detached `Popen(start_new_session=True)` so it outlives an app
  crash; status is *derived* per request from pid-liveness crossed with whether the
  host is still active, so a partial destroy (agent gone, host alive) reads FAILED
  rather than a false DONE. A FAILED directory is kept for inspection with a log tail;
  Retry reuses a still-running record rather than spawning a duplicate. (Known ~1s
  jitter where a successful destroy briefly flashes FAILED; corrected next tick.)

---

## 4. Latchkey: permissions & gateway

### 4.1 Gateway bootstrap (port-wait)
- On startup the client polls the on-disk forward record every 0.2s for up to 30s for
  the gateway port, bailing early if the supervisor process is dead. On failure it
  raises pointing at `latchkey_forward.log`; this runs on a background thread so the UI
  still comes up, but the permission consumer then reconnect-loops (4.3) and **the user
  simply never sees permission prompts** -- no specific UI signal.

### 4.2 Stale cached gateway URL
- If the supervisor rebinds to a new port mid-session, connect-level errors trigger
  `invalidate_initialization()`, so the *next* call re-reads the record and rebinds.
  Self-healing across the next call; the failing call still raises into 4.3 / a 502.

### 4.3 Permission-requests stream drop -> reconnect (core resilience)
- The long-lived `GET /permission-requests?follow=true` stream is rebuilt on a 2s idle
  cycle (ReadTimeout treated as a clean close, not an error). Real errors reconnect on
  bounded exponential backoff (1s -> ... -> 30s cap), resetting on any success.
  On-disk request files survive and the gateway re-emits all pending requests on each
  reconnect; the inbox is keyed on `request_id` so redelivery is idempotent. A single
  un-translatable record is skipped (logged) rather than killing the whole thread.
- **If the gateway never returns:** the loop retries every 30s **forever with only log
  output -- no "permission system offline" UI indicator.** New prompts never appear and
  the agent stays blocked. **This is the subsystem's most significant silent failure.**

### 4.4 Approval-nudge delivery (`mngr message`) -- silent and unretried
- After a user approves/denies, the waiting agent is nudged via `mngr message`, which
  is **fire-and-forget, never retried.** Worse, success is judged by process exit code,
  and `mngr message` exits 0 even when *no agent matched the target* -- the handlers
  call `send()`, not the delivery-verifying `deliver()`, so a nudge that reached nobody
  isn't even detected. The design relies on the durably-written response event and the
  agent's own polling to eventually wake it. **Second notable silent surface:** the
  grant is applied and the dialog shows success, but the agent may sit idle.
  *(Note: the older macOS forkserver-SIGSEGV crash that silently dropped this nudge has
  been engineered out -- `mngr_caller.py` now uses an exec'd-socketpair model that
  avoids fork-without-exec. The remaining weakness is the no-retry / exit-0 ambiguity,
  not a fork crash.)*

### 4.5 Approve/Deny actions (user-initiated) -- the well-behaved path
- A failed sign-in/approval keeps the request **pending** (never mis-recorded as a
  denial), surfaces a concrete error/502 in the dialog, and is retryable. Approve does
  not tolerate a 404 (silently dropping a grant is worse than re-raising); DELETE on
  resolve tolerates 404 and is best-effort. The only reliably user-visible recovery
  path in the subsystem.

### 4.6 Agent auto-registration & supervisor lifecycle
- Auto-register is idempotent/atomic and fires on each discovery tick plus once at
  startup; on a store error it logs a warning and **marks the pair processed anyway**
  (no retry), so a *transient* IO error is permanently abandoned the same as a
  permanent one -- the agent's proxy calls keep getting rejected until an operator runs
  `mngr latchkey register-agent`. The supervisor's `ensure_running` is idempotent and
  reaps duplicate forwards scoped to the same latchkey directory (duplicates were a
  prior cause of stuck-new-mind 503s).

---

## 5. Auth & sessions

### 5.1 Desktop session cookie
- A 30-day signed cookie with **no refresh and no sliding expiry**; recovery is
  re-authentication only. Page routes 302 to `/login`; API routes return 403/401 JSON.
- **Gap:** an expired session emits a one-shot `auth_required` SSE event that
  **`chrome.js` has no handler for** -- so the sidebar silently renders empty with no
  "sign in again" prompt; the user only recovers if they happen to hit a route that
  302s to login. A genuine recovery hole.

### 5.2 One-time code -> cookie exchange
- Codes are single-use, fail-closed (corrupt code file degrades to "all codes
  rejected"). Signing-key generation is serialized with a double-checked lock to
  prevent a startup burst from minting different keys and silently invalidating the
  just-signed cookie. An invalid/used code shows an explicit HTML error (visible).

### 5.3 Central `MINDS_API_KEY` (agent -> client)
- Regenerated fresh in memory on every `minds run`, never persisted (shrinks a
  compromised key's window to one session). Constant-time compare, fails closed when
  unset. No in-session refresh -- a desynced supervisor would 401 every agent request
  silently until minds is restarted.

### 5.4 SuperTokens account session (owned by the plugin)
- minds holds no tokens; refresh is entirely the plugin's and opaque to minds. Backend
  unavailability -> 502 "Authentication service is unavailable" (user retries). OAuth
  flows have a 10-min TTL and run on a thread so an exception surfaces as `state=error`
  rather than stalling "Waiting..." forever. The in-memory identity cache is
  deliberately **not poisoned** on a transient `auth_list` failure (and skips orphan
  GC) to avoid catastrophic association loss; a user_id rotation is recovered by a
  one-shot cache refresh. Sign-out fails *open* toward the user's intent (local mirror
  dropped even if the connector revoke fails -- so a connector session can linger).

### 5.5 Cloudflare tunnel-token injection (sharing)
- Token absence/creation failure raises a user-facing `SharingError`. But the
  injection and clearing themselves are best-effort over `mngr exec`: **a failed
  inject only logs a warning** -- cloudflared never starts and the share silently
  doesn't come up; a failed clear leaves a stale token (cloudflared runs against a
  deleted tunnel until the agent stops). Neither has a user-visible signal beyond logs.

---

## 6. Backup, restore & snapshot-resume

### 6.1 Backup provisioning (detached, retry-with-budget)
- Runs on a detached thread after create (can't block or poison the workspace).
  Idempotent provisioning retried within a 300s wall-clock budget (10s between
  attempts), with an inner 60s retry specifically for just-minted R2/S3 credentials
  still propagating. The canonical env is written to the minds-side store *before*
  injection, so an injection-only failure still leaves minds able to reach the repo.
- **If it fails:** a **single transient OS notification "Backup setup failed"** and a
  log line. **There is no persistent visible UI signal on this branch** -- no red
  badge, no stored failure state. If the user misses the toast, the only later evidence
  is the backup badge sitting at `NEVER`, and the workspace runs with no backups.
  *(The persistent `backup_failure_store` + red badge work exists only on the separate
  `gabriel/backup-failure` branch and has not landed here.)*

### 6.2 Backup status badge
- Status is queried from restic directly (works even when the workspace is offline).
  Per-workspace errors degrade to `UNKNOWN` rather than propagating; the batch is
  wall-clock bounded (20s batch / 12s per call) with a non-blocking executor shutdown
  so a hung repo never stalls the page. **Note:** on this branch the rich
  `BackupStatusState` taxonomy (`compute_backup_status_for_workspace`) has no
  production caller -- the live badge is derived only from the snapshot list +
  `is_backing_up`, so a failed-provisioning tile is indistinguishable from a
  never-configured one (reinforcing 6.1).

### 6.3 Export / restore
- User-initiated and synchronous, so failures surface directly in the HTTP response
  (404 "not configured" / 500 with a warning). Restore is bounded at 600s; the temp
  restore tree is always `rmtree`'d in a `finally`. Visible at the point of action.

### 6.4 Credential persistence
- Canonical envs and the master password are written atomically at 0600; envs are
  **never auto-deleted even on destroy** (a deliberate restore affordance), and the
  master password is write-once (`O_EXCL`). Read failures feed 6.1/6.2/6.3.

### 6.5 Snapshot-resume (stopped -> running)
- This is workspace-lifecycle recovery (not restic): the in-container recovery probe
  detects the interface not coming back, and `run_restart_sequence` drives the
  surgical/host restart with the 15s/30s readiness poll. Unlike backups, this **does**
  surface a persistent `RESTART_FAILED` state with a reason on the recovery page.

---

## 7. Bootstrap & environments (container-side + cloud infra)

### 7.1 mngr settings self-heal (every startup)
- On every `minds run`, settings are reconciled idempotently from SuperTokens sessions
  (re-registering missing `imbue_cloud` provider blocks, cleaning stale legacy hosts),
  with atomic writes and per-account isolation. Silent by design -- a *persistently*
  failing reconcile produces only logs; the user just sees "can't create workspace".

### 7.2 Agent-creation pipeline
- Per-phase failures (clone, key minting, `mngr create`, ...) set a visible `FAILED`
  status with the error string; credentials are redacted from streamed git output. The
  imbue_cloud fast-path matches a structured `FastPathUnavailableError` *error class*
  and falls back once to the slow rebuild path; any other error propagates verbatim.

### 7.3 Deploy recover-target protocol (operator-facing)
- A failed `minds env deploy` leaves an atomic `.minds-deploy-recover-target-<env>.json`
  marker; a new deploy refuses while it exists, and `minds env recover` runs every
  reversal step in reverse order (Modal rollback, Neon instant-restore preserving the
  broken state, orphan-secret cleanup), each idempotent and re-runnable. A per-env
  `flock` serializes concurrent deploys. If recovery itself fails, the marker is left
  in place with a per-step error list so the operator re-runs after fixing the cause.
- Supporting idempotency: provider creates are lookup-first / treat "already exists"
  as success; Neon retries 423 (Locked) up to 120s; `minds env destroy` removes the
  local env root **last, only on full success**, so a partial teardown never silently
  leaks cloud resources (re-run picks up where it broke). Health checks tolerate
  cold-boot 4xx/5xx for the first 10s to avoid failing on Modal's stale-container swap.
- **If recovery fails:** all of these are operator-facing with explicit errors naming
  the file/branch/resource and a "re-run" instruction; the notable risk is an orphaned
  Neon snapshot branch if even the cleanup-on-write-failure delete fails (logged with
  manual-console instructions).

---

## 8. Error reporting (the backstop when recovery fails)

Two parallel Sentry pipelines (Electron-JS and Python-backend), both gated by the same
opt-out `report_unexpected_errors` setting read live per event, plus a web-UI surface.
The Python side adds per-exception rate-limiting, drops interrupts, and offloads
oversized events/log attachments to per-environment S3 buckets (dev never uploads).
Secret-bearing CLI flags are masked in *logged* command renderings (not a blanket log
scrub). A manual **"Report a bug"** path always bypasses the opt-out gate:

- **Backend up:** the rich `/help` collector (account emails, workspace ids, discovery
  flag, host resources, optional logs). Reachable from the help button, the
  discovery-`BLOCKED` takeover, and the recovery pages.
- **Backend down:** a one-shot main-process Sentry report of the on-screen error plus
  gzipped tails of the dead backend's own logs (5 MiB/file, 15 MiB total cap).

**Surfacing gaps:** the `#quitting`/loading takeovers have no report button, and the
renderer-crash case (1.5) has no takeover at all -- so those states are neither
auto-reported nor manually reportable.

---

## Cross-cutting findings

**Strengths**
- The per-workspace STUCK -> recovery -> restart flow (2.1 / 3.1-3.2) is exemplary:
  probe-confirmed detection, deduped restart, and every failure path converging on a
  visible `RESTART_FAILED` with a reason. Mirror this elsewhere.
- Defensive shell engineering: timeouts everywhere, shutdown always resolves, the
  last-window-never-closes and navigate-away-only-on-positive-destroy-evidence
  invariants, and the optimistic-override-cleared-on-failure pattern.
- "Definitive vs transient" classification is consistently narrow and correct (infra
  5xx vs app 5xx; cold-boot 4xx tolerance; 423-only Neon retry; connect-error-only
  gateway re-resolve). Over-broadening any of these would silently ship/mask failures.
- The deploy/destroy infra leans entirely on idempotency + a recover-target marker +
  deferred-root-removal, which is the right model for an operator CLI.

**The silent-failure surfaces, roughly in order of user impact**
1. **Renderer-process crash** (1.5) -- blank/frozen view, no detection, recovery, or
   report. The biggest hole. No `render-process-gone`/`unresponsive`/`did-fail-load`
   handlers exist.
2. **Permission-gateway permanently down** (4.1/4.3) -- prompts silently never appear,
   the agent stays blocked, only logs. No "permission system offline" indicator.
3. **Approval nudge silently lost** (4.4) -- fire-and-forget, never retried, and the
   exit-0/no-match gap means a nudge to nobody isn't even detected.
4. **Backend exits with code 0 or via signal/OOM** (1.2) -- ignored, app goes dead in
   place with no takeover.
5. **Expired session SSE `auth_required` unhandled** (5.1) -- empty sidebar, no
   re-login prompt.
6. **Backup provisioning failure** (6.1) -- only a transient toast; no persistent
   badge on this branch (the fix lives on `gabriel/backup-failure`).
7. **Persistent discovery producer stall while consumer is alive** (2.5
   `RECONNECTING`) -- retries forever with only a passive providers-panel counter;
   combined with 2.2's silent "Loading workspace" loader.
8. **Tunnel-token inject/clear failures** (5.5) and **transient auto-register IO
   errors** (4.6) -- logged only; share silently fails / agent stays rejected.

**Other notable design points**
- No token refresh exists anywhere in minds itself; every credential is fixed-lifetime
  or rotates only on process restart, with refresh (if any) delegated to the plugin.
- Backend Retry (1.1) has no attempt cap or backoff, and there is no generic
  post-startup backend health probe (a hung-but-alive server isn't detected outside
  the discovery and per-workspace subsystems).
- JS<->Python Sentry DSN/env config is hand-mirrored across `sentry.js` and
  `core.py`/`frontend.py` with only a "keep in sync" comment -- drift risk.
