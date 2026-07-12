# minds: persistent background / daemon process audit

Audit of every persistent background process, daemon, supervisor, and long-lived
thread/task that the **minds** desktop app spawns or relies upon while running --
i.e. everything that is *not* the main request-serving thread and that produces
output the main process later consumes.

The system is best understood as **three concentric rings**:

1. **Ring 0 -- Electron shell** (`apps/minds/electron/*.js`): the only OS process
   it directly owns is the Python backend; everything else is main-process async.
2. **Ring 1 -- host-side `minds run` desktop client** (`apps/minds/imbue/minds`, a
   cheroot/Flask WSGI server): owns the bulk of the daemons -- the `mngr forward`
   proxy subprocess, the detached `mngr latchkey forward` supervisor, and a dozen
   background threads (health, permissions, discovery, SSH tunnels, warm pool).
3. **Ring 2 -- agent-container services** (defined in the external
   default-workspace-template repo, supervised by `supervisord`): the producers whose
   outputs Ring 1 consumes (service events, discovery snapshots, the
   system_interface HTTP server).

A note on the data plane: nearly all cross-process IO is **newline-delimited JSON
over stdout pipes** (`mngr observe`, `mngr event`, the `mngr forward` envelope
stream, the backend's JSONL log) or **long-lived HTTP/SSE streams** (the gateway
permission-requests follow stream, the chrome `/_chrome/events` SSE feed). Almost
nothing uses a shared on-disk queue except the discovery-events file and the
supervisor PID record.

---

## Ring 0 -- Electron main process (`apps/minds/electron`)

| # | Process / worker | Kind | Spawn site | What the shell reads from it |
|---|---|---|---|---|
| 0.1 | **Python `minds run` backend** | OS subprocess (`child_process.spawn`) | `backend.js:268` | stdout JSONL parsed for `login_url`, `notification`, `auth_success`/`auth_required`, `mngr_forward_started`; TCP port-ready poll via `waitForPort` |
| 0.2 | **chrome SSE consumer loop** | main-process long-lived HTTP (`net.request`) | `main.js:1746` `runChromeSSELoop()` | `GET /_chrome/events` text/event-stream; broadcasts `workspaces`, `system_interface_status`, `auth_*`, `requests`, `discovery_health` to all renderer views over IPC |
| 0.3 | **`uv sync` env bootstrap** | one-shot OS subprocess (packaged builds only) | `env-setup.js:81` | stderr scanned for `Installing`/`Resolved`/`Downloading` progress; resolve/reject gates backend start |
| 0.4 | **ToDesktop auto-updater** | background updater (packaged only) | `main.js:23` `todesktop.init(...)` | update-ready prompt; no renderer wiring |
| 0.5 | inbox-refresh debounce + SSE backoff timers | `setTimeout` | `main.js:1057`, `main.js:1810` | internal coalescing only |

**Key fact:** the Python backend (0.1) is the *only* OS subprocess Electron owns.
It is crash-monitored via an `exit` handler (`main.js:2748`) that shows an error
takeover, and shut down with SIGTERM -> 5s -> SIGKILL (`backend.js:365-408`). The
SSE loop (0.2) reconnects forever with 1500ms backoff and exits only on app quit.

---

## Ring 1 -- host-side `minds run` desktop client

This is where the real daemon zoo lives. All threads are owned by a single root
`ConcurrencyGroup` ("minds-run") and stopped during `server.py` shutdown unless
noted. Spawn sites are in `apps/minds/imbue/minds/cli/run.py` unless stated.

### 1A. The `mngr forward` proxy subprocess (discovery consumer + HTTP/WS proxy)

This is the central dependency. One global `mngr forward` **subprocess** is spawned
at boot (`run.py:377` via `forward_cli.py:200`). It is simultaneously the
HTTP/WebSocket reverse proxy for `<agent-id>.localhost:8420/*` **and** the discovery
consumer. The desktop client talks to it over a **stdout JSONL envelope stream**.

Host-side threads that wrap this subprocess (`forward_cli.py`):

| Thread | Spawn | Job / output consumed |
|---|---|---|
| `mngr-forward-stdout-reader` | `forward_cli.py:219` | parses ForwardEnvelope JSONL -> updates `MngrCliBackendResolver`, fires the `listening` port handshake, feeds health tracker |
| `mngr-forward-stderr-reader` | `forward_cli.py:225` | logs plugin stderr via loguru |
| `mngr-forward-lifecycle-watcher` | `forward_cli.py:231` | `process.wait()`; on *unintended* exit fires `record_consumer_death()` -> discovery health goes terminal `BLOCKED` |

Inside the `mngr forward` process itself (`libs/mngr_forward`) the following run:

| Process / thread | Spawn | Role |
|---|---|---|
| **uvicorn HTTP/WS server** (main thread) | `cli.py:341` | proxies subdomain requests to backends resolved from discovery |
| **`mngr observe --discovery-only --quiet`** subprocess | `stream_manager.py:228` | (observe mode) discovery *producer*; JSONL of agent/host/SSH events |
| **discovery-events file tailer** thread | `stream_manager.py:237` | (observe-via-file mode -- the mode minds uses) tails the shared discovery file instead of spawning its own observe |
| **`mngr event <id> services requests --follow`** subprocess, one per agent | `stream_manager.py:509` | per-agent service-registration + request events; respawned if it dies |
| SIGHUP watcher thread | `cli.py:490` | bounces observe on config change |

> **Important nuance:** minds runs `mngr forward` with `--observe-via-file`, so the
> forward process does **not** spawn its own `mngr observe`. The single discovery
> *producer* is owned by the `mngr latchkey forward` supervisor (1B). minds'
> `mngr forward` only *tails the file* and spawns the per-agent `mngr event`
> followers. This is the consumer/producer split that the "duplicate forwards"
> failure mode is about.

### 1B. The detached `mngr latchkey forward` supervisor

A **detached** (`setsid`, survives minds restarts) supervisor process spawned via
`spawn_detached_mngr_latchkey_forward()` (`mngr_latchkey/_spawn.py:83`), managed by
`LatchkeyForwardSupervisor` (`forward_supervisor.py`). On every `minds run` boot a
background thread `mngr-latchkey-supervisor-and-gateway-init` (`run.py:305`) calls
`supervisor.restart()` then pre-warms the gateway client. Its PID lives in
`<latchkey_dir>/mngr_latchkey/latchkey_forward.json`; duplicates bound to the same
directory are reaped before spawn (`forward_supervisor.py:415`).

Children/threads it owns (`libs/mngr_latchkey`):

| Process / thread | Spawn | Role |
|---|---|---|
| **`mngr observe --discovery-only`** | `discovery_stream.py:143` | the **single shared discovery producer** for the whole host (writes the file 1A tails) |
| **latchkey gateway** subprocess | `core.py:1202` | shared gateway on a dynamic loopback port; reverse-tunneled into every agent at port 1989 |
| observe bounce watcher thread | `cli.py:573` | restarts observe on SIGHUP |
| remote-state-sync watchdog (3 threads: fs observer + stop + sentinel) | `discovery.py:419` | syncs gateway creds/permissions to VPS hosts on file change |
| per-agent VPS gateway provisioning threads (fire-and-forget, per-host coalesced) | `discovery.py:312` | stands up a gateway + reverse tunnel on remote VPS agents |
| per-agent desktop gateway tunnel-setup threads (fire-and-forget) | `discovery.py:182` | reverse-tunnels the host gateway into each discovered agent |

On **remote VPS** agents it additionally provisions two *detached* processes on the
VPS itself: a VPS-resident `latchkey gateway` (`remote_gateway.py:456`) and a
VPS->container reverse SSH tunnel (`remote_gateway.py:542`).

### 1C. Host-side latchkey permission consumer

| Thread / task | Spawn | Job / output |
|---|---|---|
| **permission-requests consumer** `latchkey-permission-requests-consumer` | `run.py:537` (`permission_requests_consumer.py:178`) | holds `GET /permission-requests?follow=true` open to the gateway; turns each into a `RequestEvent` appended to the in-memory `RequestInbox`; reconnects 1s->30s backoff |
| `LatchkeyGatewayClient` | `run.py:276` | HTTP client; `_wait_for_gateway_port()` polls the supervisor PID record (0.2s, 30s cap) to learn the gateway port; approve/delete/stream calls |
| `LatchkeyAutoRegister` | `run.py:453` | resolver change-callback (not a thread) that writes newly-discovered agents into each host's `latchkey_permissions.json` allowlist |
| `MngrMessageSender` `mngr-message-send` threads | `run.py:328` | fire-and-forget `mngr message` nudges to unblock an agent after its permission request is resolved |

### 1D. Health / liveness / recovery monitors

| Monitor | Spawn | Cadence | Reads -> Produces |
|---|---|---|---|
| **discovery-health watchdog** | `run.py:511` (`app.py:2355`) | **5.0s** (`_DISCOVERY_WATCHDOG_POLL_INTERVAL_SECONDS`) | resolver freshness (`_DISCOVERY_FRESHNESS_THRESHOLD_SECONDS = 3 x stream poll`) -> `HEALTHY`/`RECONNECTING`/`BLOCKED`; remediates via SIGHUP bounce once then `restart()` w/ exp backoff (15s base, 300s cap) |
| **system-interface health probe** | `run.py:521` (`app.py:2305`) | **2.0s** (`_HEALTH_PROBE_INTERVAL_SECONDS`; corrected -- not 30s) | HTTP-probes suspect/STUCK agents through the plugin; 200 -> HEALTHY, run of failures past `stuck_threshold_seconds` -> STUCK -> RESTARTING |
| provider/host failure enrollment | `run.py:405` | event-driven | consumes `system_interface_backend_failure` envelopes; enrolls connection-level + 5xx failures as probe suspects |
| workspace-readiness wait (recovery) | `workspace_recovery.py:138` | 1.0s, synchronous during a restart only | probes interface until 200 or deadline (15s surgical / 30s host-restart) |
| backup-status query (on demand, not a daemon) | `backup_status.py` | 12s/workspace, 20s batch | shells out to `restic snapshots`/`list locks` on the landing-page GET |

Status from 1D flows to the renderer via the chrome SSE feed (`/_chrome/events`,
`app.py:913+`) which Ring 0's loop (0.2) consumes.

### 1E. Other persistent host threads

| Thread | Spawn | Job |
|---|---|---|
| **mngr warm-process pre-warmer + pool** | `run.py:326` (`mngr_caller.py:219`) | keeps one warm forkserver mngr interpreter ready so `mngr message`/`mngr latchkey` calls skip the 1-3s import cost; respawns a replacement when one is claimed |
| **grandparent death watcher** | `run.py:318` | polls the grandparent PID (Electron); SIGTERMs minds if Electron dies without clean child exit |
| per-creation agent-creator threads (per workspace) | `agent_creator.py:1373` | run the full clone -> `mngr create` -> readiness -> backup-provision flow; write status to in-memory `_statuses` polled by the create UI |
| geo-detection (one-shot), browser-opener (one-shot), cheroot shutdown helper, notification threads | various | transient; not part of steady-state |

### 1F. SSH tunnels (host, via `mngr_forward/ssh_tunnel.py`)

Two `SSHTunnelManager` instances exist (one inside minds' `mngr forward`, one inside
`mngr latchkey forward`); the desktop client also holds one in
`DesktopClientState.ssh_tunnel_manager` for cross-workspace SSH.

| Tunnel kind | Spawn | Supervision |
|---|---|---|
| **forward (direct-tcpip)** accept-loop thread, per unique `(ssh_host, remote_endpoint)` | `ssh_tunnel.py:267` | lazy; one relay thread per accepted connection; recreated on transport death |
| **reverse port-forward** (latchkey gateway injection, cross-workspace SSH, `--reverse` specs) | `ssh_tunnel.py:288` | **health-check supervisor thread** `reverse-tunnel-health-check` polls every **30s** (`ssh_tunnel.py:365`), repairs broken tunnels with exp backoff (300s cap), 15s SSH keepalives |

---

## Ring 2 -- agent-container services (producers consumed by the host)

These run **inside each agent's Docker container**, declared as `[program:*]` in the
default-workspace-template `supervisord.conf` (**external repo -- not in this monorepo**). Listed here
because the host *relies on* their outputs.

| Container service | Output it produces | How the host consumes it (this repo) |
|---|---|---|
| **app watcher** | `events/services/events.jsonl` (service register/deregister JSONL) | per-agent `mngr event <id> services --follow` -> parsed at `backend_resolver.py:368`, stored in `resolver._services_by_agent` (`forward_cli.py:578`) |
| **system_interface HTTP server** | the dockview UI + `/service/<name>/...` mux + `/...discovery` endpoint on :8420 | byte-forwarded by `mngr forward` (`relay.py:53`); identified as service `"system_interface"` (`forward_cli.py:110`); topology surfaced via discovery |
| **bootstrap + supervisord** | keeps all services alive; logs to `/var/log/supervisor` | indirect -- supervisord death shows up as a producer stall in the discovery-health watchdog |
| **cloudflared** | Cloudflare tunnel (network side-effect, no file) | indirect via Cloudflare API + agent labels; Share modal is authoritative |
| **telegram bot** | inbound only (`mngr message` into the agent) | not read by the host |
| **deferred-install** (one-shot) | installs Chromium/apt post-boot | not read by the host |
| **primary/services mngr agent** (`is_primary=true`, `sleep infinity`) | its discovery record | `mngr observe`; `MngrCliBackendResolver.list_known_workspace_ids()` filters `workspace`+`is_primary` (`backend_resolver.py:766`); last-good topology persisted for SSH-dead fallback |
| **chat agent(s)** (Claude Code) | discovery record (no `is_primary`) | `mngr observe`; routed through system_interface |

### The discovery pipeline end-to-end

```
[Ring 2] each agent's system_interface :8420
        |  polled (~DISCOVERY_STREAM_POLL_INTERVAL_SECONDS)
        v
[Ring 1B] mngr observe --discovery-only   (single PRODUCER, owned by mngr latchkey forward)
        |  writes discovery-events JSONL file
        v
[Ring 1A] mngr forward --observe-via-file  (CONSUMER: tails file; HTTP/WS proxy)
        |  + spawns per-agent `mngr event <id> services requests --follow`
        |  emits ForwardEnvelope JSONL on stdout
        v
[Ring 1] EnvelopeStreamConsumer threads -> MngrCliBackendResolver + health trackers
        |  chrome /_chrome/events SSE
        v
[Ring 0] Electron runChromeSSELoop -> IPC -> renderer views
```

---

## Consolidated inventory (every distinct persistent process / thread)

**OS processes**
1. Python `minds run` backend (Electron-owned)
2. `mngr forward` proxy + discovery-consumer subprocess (host, global)
3. `mngr latchkey forward` supervisor (host, detached, survives restarts)
4. `mngr observe --discovery-only` discovery producer (child of #3, single)
5. latchkey gateway (child of #3)
6. per-agent `mngr event ... --follow` subprocesses (children of #2)
7. mngr warm-pool interpreter (host)
8. VPS-resident latchkey gateway + VPS->container reverse SSH tunnel (remote agents only)
9. `uv sync` env bootstrap (packaged Electron, one-shot)

**Long-lived host threads (inside `minds run`)**
10. forward stdout/stderr/lifecycle readers (x3)
11. discovery-health watchdog (5.0s)
12. system-interface health probe (2.0s)
13. permission-requests follow-stream consumer
14. latchkey supervisor restart + gateway pre-warm
15. grandparent death watcher
16. LatchkeyAutoRegister resolver callback; MngrMessageSender dispatch threads
17. per-creation agent-creator threads (per workspace)

**Long-lived threads inside the forward / latchkey subprocesses**
18. uvicorn server thread; discovery-file tailer; SIGHUP watcher
19. SSH forward accept-loop + relay threads (per tunnel/connection)
20. SSH reverse-tunnel health-check supervisor (30s) + relay threads
21. latchkey remote-state-sync watchdog (fs observer + stop + sentinel); per-agent VPS-provision + tunnel-setup threads

**Electron main-process workers**
22. chrome SSE consumer loop; ToDesktop auto-updater; debounce/backoff timers
