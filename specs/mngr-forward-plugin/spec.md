# mngr_forward plugin

## Overview

- Move the auth + subdomain-forwarding logic out of `apps/minds/`'s desktop client into a new `mngr_forward` plugin so the same forwarding works without running the Electron app.
- `mngr forward` exposes a long-running local proxy that serves `<agent-id>.localhost:<port>/*` and byte-forwards each request to the workspace's `system_interface` URL (the workspace_server entry point), via SSH tunnels for remote agents.
- Discovery is driven by spawning `mngr observe --discovery-only` and per-agent `mngr event --follow` subprocesses. Their lines pass through to the plugin's stdout as a merged JSONL stream wrapped in a `{stream, agent_id?, payload}` envelope, so consumers (notably minds) can drive their own state from one stream.
- Manual operation is supported via `--service` / `--port` (mutually exclusive, exactly one required), `--no-observe` (single `mngr list --format jsonl` snapshot, no event subprocesses; only valid with `--port REMOTE_PORT`, not with `--service`, because service URLs aren't in `mngr list` output and the events stream that resolves them is suppressed in this mode), and `--reverse <remote>:<local>` (auto-set up reverse tunnels per agent, repeatable; `<remote>` may be `0` to request a dynamically-assigned port from sshd). Agent filtering uses `--agent-include` / `--agent-exclude` for which agents the plugin forwards (default: empty — every discovered agent), and `--event-include` / `--event-exclude` for which `mngr event` sources are followed per agent. There is no `--ssh` flag — SSH info always comes from mngr.
- Authentication reuses the existing one-time-code + signed-cookie + subdomain-auth-bridge design, plus `--preauth-cookie <value>` so the Electron host can pre-set a session before the first navigation. The plugin's session cookie is renamed `mngr_forward_session`; minds keeps `minds_session` for its own bare-origin server. The signing key file is persisted across plugin restarts so cookies issued by previous runs continue to verify.
- The plugin handles `SIGHUP` by bouncing only its `mngr observe` child subprocess; SSH tunnels, per-agent `mngr event` subprocesses, browser sessions, and the FastAPI app stay alive. This replaces today's `MngrStreamManager.restart_observe()` path that minds calls after writing a new `[providers.imbue_cloud_<slug>]` block on sign-in. With the move, minds sends `SIGHUP` to the `mngr forward` child instead of bouncing observe directly.
- Implementation is two-phase: Phase 1 lands the plugin standalone (minds untouched, duplicated functionality during this phase). Phase 2 switches `minds forward` over to spawning `mngr forward` as a child, parses the envelope JSONL, renames the CLI command to `minds run`, and **fully removes** the now-duplicated forwarding/auth code from minds.
- Latchkey gateway tunneling is **out of scope** for this spec. Minds' `desktop_client/ssh_tunnel.py` and its `SSHTunnelManager` are NOT deleted in Phase 2 because the surviving `LatchkeyDiscoveryHandler` still needs per-agent reverse tunnels for the dynamic-port latchkey gateway. A separate, follow-up spec will migrate Latchkey to a single fixed-per-host gateway with in-band per-agent routing (likely via an agent API key); only after that lands can the plugin's static `--reverse 1989:<fixed>` subsume the latchkey tunnels and minds' `ssh_tunnel.py` finally be deleted.

## Expected Behavior

### `mngr forward` (standalone, browser user)

- `mngr forward --service system_interface` (no other flags):
  - Generates a fresh one-time login URL (no persisted `one_time_codes.json`); prints `Login URL (one-time use): http://localhost:8421/login?one_time_code=<code>` to stderr in human format, or emits a JSONL `login_url` event on stdout in `--format jsonl`.
  - Listens on `127.0.0.1:8421`.
  - Emits a `listening` event on stdout (jsonl).
  - Spawns `mngr observe --discovery-only --quiet` as a child and proxies its lines to stdout under `stream: "observe"`.
  - Spawns one `mngr event <agent-id> services requests refresh --follow --quiet` per agent that passes `--agent-include`/`--agent-exclude`, and proxies their lines under `stream: "event"`.
- The browser visits the login URL → the plugin sets a `mngr_forward_session` cookie on the bare origin and redirects to `/`.
- The bare-origin `/` debug index (only used outside Electron — minds doesn't touch it) lists every known agent ID as a flat list, including agents that the resolver can't yet route (no service URL discovered yet, or no SSH info on a remote agent). Unresolved entries are visually de-emphasized (greyed-out link) and annotated with the reason (e.g. `(no system_interface URL yet)`). Each resolved link goes through `/goto/<agent-id>/`, which mints a short-lived signed token, redirects to `http://<agent-id>.localhost:8421/_subdomain_auth?token=…&next=/`, the subdomain handler validates the token, sets a per-subdomain `mngr_forward_session` cookie, and lands the user on the proxied content.
- HTTP and WebSocket traffic to `<agent-id>.localhost:8421/*` is byte-forwarded to the agent's resolved backend URL (the URL of `--service` for that agent in observe-driven mode; `127.0.0.1:<remote-port>` on the agent's host when `--port` is in effect). For remote agents, the forward goes through a paramiko SSH tunnel (Unix-domain socket on the local side, direct-tcpip channel on the SSH side).
- When the configured `--service`'s URL isn't yet known for an agent, or the agent's host has no SSH info, the plugin returns 503 with a small auto-refreshing HTML page for `text/html` requests, plain `503` body otherwise — same shape as the current minds desktop client behavior.
- `--open-browser` additionally opens the printed login URL in the system browser; default is print-only.
- Strict subdomain matcher: only `agent-<hex>.localhost(:port)?` (and the `127.0.0.1` synonym) is accepted; everything else returns 404 (or, for HTML navigations, redirects to the bare-origin landing page).
- One OTP per process — restart `mngr forward` to issue another.

### `mngr forward --reverse 8420:8420 --reverse 9090:9090`

- Repeatable flag: each `<remote-port>:<local-port>` pair becomes a reverse tunnel.
- For every known agent on a remote host (and any new ones discovered by observe), the plugin opens each pair via `SSHTunnelManager.setup_reverse_tunnel(remote_port=<remote>, local_port=<local>, …)`.
- Each successful establishment emits a `forward`-stream JSONL line:
  ```
  {"stream":"forward","agent_id":"<id>","payload":{"type":"reverse_tunnel_established","agent_id":"<id>","remote_port":<int>,"local_port":<int>,"ssh_host":"<host>","ssh_port":<int>}}
  ```
- Tunnels are health-checked every ~30s; broken tunnels are re-established and re-emit the same payload (with a possibly new `remote_port` if sshd reassigned it). Idempotent for the consumer.
- The local port must be a positive integer. The remote port may be `0`, in which case the plugin asks sshd for a dynamically-assigned port; the actual bound port is reported in the `reverse_tunnel_established` event, so consumers (e.g. minds writing `minds_api_url`) wire up after-the-fact rather than relying on a fixed pre-known value. A non-zero `<remote>` is bound verbatim and is what enables minds to skip the URL-write step entirely once Latchkey-style fixed ports are adopted everywhere.

### `mngr forward --no-observe --port 8080 --reverse 7777:7777`

- Plugin runs `mngr list --format jsonl` once at startup, parses agents and SSH info from the output, applies `--agent-include` / `--agent-exclude` CEL filters client-side, and starts forwarding the resulting set.
- Auto-discovery / `mngr event` subprocesses don't run.
- `--no-observe` is only valid in combination with `--port REMOTE_PORT`. `--no-observe --service NAME` is rejected as a CLI usage error during option validation, because `mngr list --format jsonl` doesn't carry per-service URLs and the events stream that resolves `--service` to a backend URL is suppressed in this mode.
- If the post-filter snapshot is empty, `mngr forward` exits non-zero with a clear error (manual mode is supposed to be deterministic).
- `--reverse` still sets up tunnels for every snapshot agent at startup; no rediscovery.

### `mngr forward` on `SIGHUP` (observe-only restart)

- Used by minds to make a freshly written `[providers.imbue_cloud_<slug>]` block (or any other `settings.toml` change that affects discovery) take effect without losing tunnels or per-agent event streams.
- The plugin installs a `SIGHUP` handler that asks `ForwardStreamManager.bounce_observe()` to terminate the existing `mngr observe --discovery-only --quiet` child and spawn a new one with the same args. The new observe process re-emits a `FullDiscoverySnapshotEvent` early, which the plugin proxies to stdout and which any consumer (minds') resolver replaces in place.
- Per-agent `mngr event` subprocesses are not touched — they don't depend on provider registration. SSH tunnels (forward + reverse) stay up. The FastAPI app stays up. Browser sessions remain valid because the signing key on disk is preserved across the bounce (and the `--preauth-cookie` value, if one was passed, is also still trusted).
- In `--no-observe` mode, `SIGHUP` re-runs `mngr list --format jsonl` once, re-applies the agent filters, and updates the resolver in place. If the new snapshot is empty, the plugin logs a warning and keeps serving the previous set rather than exiting (mid-flight empty-snapshot is treated as transient, unlike the startup case where it's fatal).
- `SIGHUP` arriving before observe has been started, or during shutdown, is a no-op.

### `mngr forward` (subprocess from `minds run`)

- `minds run` spawns `mngr forward` via `ConcurrencyGroup.run_process_in_background`, passing:
  - `--port <minds-supplied>` (default 8421, overridable via `minds run --mngr-forward-port`).
  - `--service system_interface`.
  - `--agent-include 'has(agent.labels.workspace) && has(agent.labels.is_primary)'` (matches today's `MngrCliBackendResolver.list_known_workspace_ids` filter; non-primary workspaces and non-workspace agents are not forwarded by minds).
  - `--preauth-cookie <opaque-base64-token>`.
  - `--format jsonl`.
  - `--reverse <minds-api-port>:<minds-api-port>` so agents can reach minds' bare-origin API back through the SSH transport.
- `minds run` parses each stdout line as `{"stream": "observe"|"event"|"forward", ["agent_id": ...,] "payload": ...}`. Lines for `observe` / `event` feed the surviving in-process `MngrCliBackendResolver` via the existing `parse_discovery_event_line(...)` and `parse_service_log_record(...)` helpers. `observe` events for new agents additionally fan out to any `on_agent_discovered` callbacks registered on the `EnvelopeStreamConsumer` (this is where minds' light replacement for the deleted `AgentDiscoveryHandler` hooks in — see "Changes in `apps/minds/`" below). Lines for `forward.reverse_tunnel_established` trigger the `minds_api_url` write directly (the SSH info is in the payload, no plugin coordination needed); the writer overwrites unconditionally on every event so a port reassignment after a tunnel-repair is reflected without any diff bookkeeping.
- `minds run` separately runs the minds-side bare-origin FastAPI app on its own port (default `8420`), serving `/`, `/welcome`, `/create*`, `/api/create-agent*`, `/creating/*`, `/accounts*`, `/workspace/*`, `/sharing/*`, `/_chrome*`, `/requests*`, `/api/agents/*/telegram/*`, `/api/destroy-agent/*`. Cookies are `minds_session` (origin-scoped to `localhost:8420`).
- The browser visits minds' UI at `localhost:8420`; minds' templates link / iframe across to `<agent-id>.localhost:8421/...`. Electron pre-sets `mngr_forward_session=<preauth-cookie-value>` on `localhost:8421` (bare origin) before the first agent-subdomain navigation; the existing `/goto/<agent>/` → `/_subdomain_auth?token=…` bridge mints the per-subdomain cookie on first visit just like today.
- Plugin death is detected via the `RunningProcess` wrapper that `ConcurrencyGroup` returns (plus envelope JSONL EOF as a backup). On detected exit, minds captures plugin stderr + exit code, surfaces both via `NotificationDispatcher` + a logged error line, and exits non-zero. Graceful restart is deferred.
- `EnvelopeStreamConsumer` exposes `bounce_observe()`, which sends `SIGHUP` to the plugin's PID (looked up via `RunningProcess`) so that `settings.toml` changes (e.g. a new `[providers.imbue_cloud_<slug>]` block written when a user signs in) take effect without restarting the whole plugin. Today's `_bounce_mngr_observe` path in `apps/minds/imbue/minds/desktop_client/supertokens_routes.py` is rewritten to call this method instead of `MngrStreamManager.restart_observe()`. The consumer also exposes `add_on_agent_discovered_callback(cb: Callable[[AgentId, RemoteSSHInfo | None, str], None])` and `add_on_agent_destroyed_callback(cb: Callable[[AgentId], None])` — same shapes as today's `MngrStreamManager` callbacks — so minds can register the light replacement for the deleted `AgentDiscoveryHandler` (writes local `minds_api_url`, re-injects stored Cloudflare tunnel tokens) and the existing `LatchkeyDiscoveryHandler` / `LatchkeyDestructionHandler`.

### Naming, packaging, defaults

- Plugin lives at `libs/mngr_forward/`; entry-point identifier `forward`. Disabled by default (matches `mngr_vultr` / `mngr_imbue_cloud`); users run `mngr plugin enable forward` to activate.
- `mngr forward` is a single click command (no subcommands). Uses `add_common_options` + `setup_command_context` like every other mngr command, inheriting `--profile`, `--host-dir`, `--mngr-prefix`, `-v/-q`, `--format`, `--log-file`. Logging is routed through the standard mngr/loguru config those options set up.
- Default bind: `--host 127.0.0.1 --port 8421`.
- Plugin on-disk state lives at `$MNGR_HOST_DIR/plugin/forward/` (signing key only — OTPs are not persisted). The signing key is generated once and reused on every subsequent process start, so cookies issued by previous plugin invocations remain valid (this is what makes browser sessions survive `SIGHUP`-driven observe bounces and restarts in general). No `--data-dir` flag; minds controls placement via `MNGR_HOST_DIR` when it spawns the plugin.
- Output discipline follows mngr `--format human|json|jsonl`: subprocess use defaults to JSONL.
- `CommandHelpMetadata` registers the one-liner: `"Forward web traffic to agents via <agent>.localhost subdomains [experimental]"`.
- Documentation is a single `README.md` at the package root.

## Implementation Plan

### New plugin: `libs/mngr_forward/`

Package root files:
- `pyproject.toml` — `name = "imbue-mngr-forward"`, deps `imbue-mngr`, `imbue-common`, `imbue-concurrency-group`, `paramiko`, `fastapi`, `uvicorn`, `websockets`, `itsdangerous`, `jinja2`, `httpx`. `[project.entry-points.mngr] forward = "imbue.mngr_forward.plugin"`. Workspace deps via `[tool.uv.sources]`. Standard `[tool.pytest.ini_options]` + `[tool.coverage.run]` copied from `libs/mngr_imbue_cloud/pyproject.toml`.
- `README.md` — synopsis, examples, brief architecture summary linking to source files.
- `conftest.py`, `imbue/__init__.py`, `imbue/mngr_forward/__init__.py` — empty per CLAUDE.md.

Source files under `libs/mngr_forward/imbue/mngr_forward/`:

- **`primitives.py`**: `ForwardPort` (`PositiveInt`), `MngrForwardSessionCookieName` constant (`"mngr_forward_session"`), `ForwardSubdomainPattern` constant matching today's `_WORKSPACE_SUBDOMAIN_PATTERN`. `ReverseTunnelSpec(remote_port, local_port)` — frozen model with two `PositiveInt` fields.
- **`errors.py`**: `MngrForwardError(MngrError)` base + subclasses: `ForwardManualConfigError` (mutually-exclusive flags, empty snapshot in `--no-observe`), `ForwardAuthError` (signing key issues), `ForwardSubprocessError` (observe/event spawn failures).
- **`data_types.py`**: pydantic frozen models — `ProxyTarget(url: AnyUrl, ssh_info: RemoteSSHInfo | None)`, `ForwardEnvelope(stream: Literal["observe","event","forward"], agent_id: AgentId | None, payload: dict)`, `LoginUrlPayload(type: Literal["login_url"], url: AnyUrl)`, `ListeningPayload(type: Literal["listening"], host: str, port: ForwardPort)`, `ReverseTunnelEstablishedPayload(type: Literal["reverse_tunnel_established"], agent_id: AgentId, remote_port: PositiveInt, local_port: PositiveInt, ssh_host: str, ssh_port: PositiveInt)`. `agent_id` is omitted from the envelope when not applicable (consumers do `raw.get("agent_id")`).
- **`auth.py`** (moved from `apps/minds/imbue/minds/desktop_client/auth.py`): `OneTimeCodeStatus`, `StoredOneTimeCode`, `AuthStoreInterface`, `FileAuthStore`. Same API as today, minus the `one_time_codes.json` persistence — codes are kept in memory only and the file is never written. The signing key is read from `signing_key` at startup and generated once if absent (today's behavior); it is **not** regenerated on every spawn, so cookies from previous runs continue to verify.
- **`cookie.py`** (moved from minds' `cookie_manager.py`, subdomain-related parts only): `create_session_cookie`, `verify_session_cookie`, `create_subdomain_auth_token`, `verify_subdomain_auth_token`. Cookie name pinned to `mngr_forward_session`. `verify_session_cookie` first checks the (optional) preauth cookie value passed in at construction, then falls back to the signed-token check.
- **`ssh_tunnel.py`** (moved from `apps/minds/imbue/minds/desktop_client/ssh_tunnel.py`): `RemoteSSHInfo`, `SSHTunnelError`, `ReverseTunnelInfo`, `_ForwardedTunnelHandler`, `SSHTunnelManager` (forward Unix-socket tunnels via `direct-tcpip`, reverse port-forwards with the per-forward handler, the 30s health-check loop, and `parse_url_host_port`). No behavior change — file moves verbatim with imports adjusted.
- **`resolver.py`**: `ForwardResolver(MutableModel)`. Holds the configured forwarding strategy (a `ForwardServiceStrategy(name: ServiceName)` or `ForwardPortStrategy(remote_port: PositiveInt)`) plus a `services_by_agent: dict[str, dict[str, str]]` map and an `ssh_by_agent: dict[str, RemoteSSHInfo]` map. Single public method `resolve(agent_id) -> ProxyTarget | None`. Latest write wins when manual + observed agree on a label (just overwrite via `update_*`).
- **`stream_manager.py`**: `ForwardStreamManager(MutableModel)`. Slimmed-down sibling of today's `MngrStreamManager`. Spawns `mngr observe --discovery-only --quiet` and per-agent `mngr event …services requests refresh --follow --quiet` subprocesses through a `ConcurrencyGroup`. Parses each line, applies `--agent-include` / `--agent-exclude` CEL filters client-side using the existing `compile_cel_filters(...)` helper, fires `on_agent_discovered(agent_id, ssh_info, provider_name)` / `on_agent_destroyed(agent_id)` callbacks, updates `ForwardResolver`, and forwards every line to a configurable JSONL emitter (`EnvelopeWriter`). Exposes `bounce_observe()` that terminates and respawns the observe child with the same args, leaving per-agent event subprocesses, the resolver's existing state, and any callbacks wired up untouched. The new observe process emits a `FullDiscoverySnapshotEvent` shortly after start, which the existing line-handling code will use to atomically replace the agent list.
- **`snapshot.py`**: `mngr_list_snapshot(mngr_ctx) -> ParsedAgentsResult` — runs `mngr list --format jsonl` once via subprocess and parses agents + SSH info using `parse_agents_from_json`. Used both at startup when `--no-observe` is set, and when `SIGHUP` arrives in `--no-observe` mode (re-snapshotting to pick up config changes; mid-flight empty results are warned but not fatal).
- **`reverse_handler.py`**: `ReverseTunnelHandler(MutableModel)` — registered as an `on_agent_discovered` callback on `ForwardStreamManager`. For each new remote agent, opens each configured `ReverseTunnelSpec` pair via `SSHTunnelManager.setup_reverse_tunnel`, then emits a `ReverseTunnelEstablishedPayload` per success through the `EnvelopeWriter`. On health-check repair, the same payload is re-emitted (the handler subscribes to the tunnel manager's repair callback, added in this same package).
- **`envelope.py`**: `EnvelopeWriter(MutableModel)`. Wraps stdout writes with the envelope schema. Methods: `emit_observe(line: str)`, `emit_event(agent_id: AgentId, line: str)`, `emit_forward(payload: ForwardEnvelopePayload)`. Serializes through a single `threading.Lock` so concurrent emitters don't interleave bytes mid-line. Writes `\n`-terminated JSON.
- **`server.py`**: FastAPI app + the auth/forwarding routes (`_handle_login`, `_handle_authenticate`, `_handle_subdomain_auth_bridge`, `_handle_goto_workspace`, `_handle_workspace_forward_http`, `_handle_workspace_forward_websocket`, `_handle_debug_index`), the host-header middleware, the `_managed_lifespan`, and `create_forward_app(...)` factory. Imports reduced relative to today's `app.py` because services routing, agent creation, accounts, telegram, latchkey, request inbox, sharing, and `/api/v1` are minds-only and stay there. Templates loaded from the plugin's `templates/` via `jinja2.Environment(loader=PackageLoader("imbue.mngr_forward", "templates"))`.
- **`templates/`**: `login.html` (JS redirect to `/authenticate?one_time_code=...` to prevent prefetch consumption), `login_redirect.html`, `auth_error.html`, `index.html` (the debug agent list, `<ul>` of `agent_id` links to `/goto/<id>/`). Plain HTML, no Tailwind, no inheritance from minds' `base.html`, no static assets.
- **`config.py`**: `ForwardPluginConfig(PluginConfig)` with `port: ForwardPort`, `agent_include: str | None`, `agent_exclude: str | None`, `event_include: str | None`, `event_exclude: str | None`, `auto_open_browser: bool`. Implements `merge_with(...)` per the convention. `register_plugin_config("forward", ForwardPluginConfig)` is called at module import time in `plugin.py`.
- **`cli.py`**: the `forward` click command. Options:
  - `--host` (default `127.0.0.1`), `--port` (default `8421`).
  - Mutually-exclusive forwarding-target group (exactly one required): `--service <remote-service-name>` or `--port <remote-port>`. (Click's `cloup` or a manual check enforces mutual exclusion.)
  - `--reverse <remote-port>:<local-port>` (multiple). `<remote-port>` of `0` means "ask sshd to assign one"; `<local-port>` must be a positive integer.
  - `--no-observe` (flag). Manually validated to be incompatible with `--service` (only valid alongside `--port REMOTE_PORT`); using both together raises a `ForwardManualConfigError` before the FastAPI app starts.
  - `--agent-include` / `--agent-exclude` (multiple, CEL strings) filter which agents the plugin tracks/forwards. Default: empty — no filtering, every discovered agent is included.
  - `--event-include` / `--event-exclude` (multiple, CEL strings) filter which `mngr event` source streams are followed per-agent (e.g. `services`, `requests`, `refresh`). Default: empty — every source the plugin currently uses (`services requests refresh`) is followed.
  - `--preauth-cookie <value>` with env var `MNGR_FORWARD_PREAUTH_COOKIE`.
  - `--open-browser/--no-open-browser` (default no).
  - `add_common_options` decorator + `setup_command_context(...)` to inherit common mngr CLI options.
  - On invocation, the command (in this exact order):
    1. Calls `start_parent_death_watcher(mngr_ctx.concurrency_group)`.
    2. Builds `EnvelopeWriter(stdout)`.
    3. Builds `ForwardResolver` with the chosen `--service`/`--port` strategy.
    4. Builds `SSHTunnelManager`.
    5. If `--no-observe`: calls `mngr_list_snapshot(...)`, applies `--agent-include`/`--agent-exclude`, exits non-zero if empty, seeds `ForwardResolver`. Otherwise: builds `ForwardStreamManager` with the resolver + envelope writer + filter expressions and starts it.
    6. If `--reverse` was passed: builds `ReverseTunnelHandler` and registers it on the stream manager (or directly opens tunnels for the snapshot agents in `--no-observe` mode).
    7. Generates the OTP via `secrets.token_urlsafe(32)`, registers it in the in-memory `FileAuthStore`, builds the login URL, emits `login_url` (jsonl) and logs to stderr (human), optionally opens the browser.
    8. Registers a `SIGHUP` handler via `signal.signal(signal.SIGHUP, _on_sighup)`. The handler is short — it sets a `threading.Event` that a small background watcher thread (spawned in step 9 below) consumes and dispatches to either `ForwardStreamManager.bounce_observe()` (observe mode) or a `_resnapshot_now()` helper that re-runs `mngr_list_snapshot(...)` and updates the resolver in place (`--no-observe` mode). Doing the actual work off the signal-handling thread avoids touching paramiko / FastAPI state from inside the (re-entrant-unsafe) signal handler.
    9. Spawns the SIGHUP-watcher thread under `mngr_ctx.concurrency_group`.
    10. Builds `create_forward_app(...)` with the auth store, resolver, tunnel manager, preauth cookie value (if any), envelope writer.
    11. Emits `listening` event.
    12. Calls `uvicorn.run(app, host=opts.host, port=opts.port, timeout_graceful_shutdown=1)`.
  - Lifespan shutdown stops the stream manager, cleans up tunnels, and closes the envelope writer.
- **`plugin.py`**: top-level `@hookimpl def register_cli_commands()` returning `[forward]`; module-level `register_plugin_config("forward", ForwardPluginConfig)`; module-level `CommandHelpMetadata("forward", one_line_description="Forward web traffic to agents via <agent>.localhost subdomains [experimental]", synopsis="mngr forward [--service NAME | --port REMOTE_PORT] [OPTIONS]", examples=(...)).register()`. Includes `add_pager_help_option(forward)` like other commands.

Tests under `libs/mngr_forward/imbue/mngr_forward/`:
- Per-module unit tests: `auth_test.py`, `cookie_test.py`, `ssh_tunnel_test.py`, `resolver_test.py`, `stream_manager_test.py`, `envelope_test.py`, `snapshot_test.py`, `reverse_handler_test.py`, `server_test.py`, `config_test.py`, `data_types_test.py`, `primitives_test.py`, `cli_test.py`.
- `test_forward_e2e.py` (`@pytest.mark.acceptance`).
- `test_forward_modal_release.py` (`@pytest.mark.release`).
- `test_ratchets.py` mirroring the standard rule set used in other `libs/mngr_*/` plugins (prevent built-in raises, prevent `monkeypatch.setattr`, etc.), with initial counts taken from the moved code.
- `conftest.py` exposing fixtures specific to this package; reuses mngr's `temp_host_dir`, `temp_mngr_ctx`, `local_provider` fixtures from upstream.

### Changes in `apps/minds/`

**Phase 1**: none. Minds keeps its existing in-process implementation untouched. The plugin and minds coexist with duplicated functionality during this phase.

**Phase 2 — files deleted**:
- `apps/minds/imbue/minds/desktop_client/auth.py`, `auth_test.py`.
- `apps/minds/imbue/minds/desktop_client/runner.py`, `runner_test.py` (replaced by the new `cli/run.py`).

**Phase 2 — files explicitly NOT deleted (despite the in-process forwarder going away)**:
- `apps/minds/imbue/minds/desktop_client/ssh_tunnel.py` and `ssh_tunnel_test.py` survive. The surviving `LatchkeyDiscoveryHandler` still uses `SSHTunnelManager.setup_reverse_tunnel(local_port=info.port, remote_port=AGENT_SIDE_LATCHKEY_PORT)` for the per-agent dynamic-port latchkey gateway. Deleting `ssh_tunnel.py` is gated on a follow-up "Latchkey fixed-per-host gateway" spec.
- The `_OnCreatedCallbackFactory` / `_run_tunnel_setup` Cloudflare-tunnel injection path in `app.py` (and the `tunnel_token_store.py` it depends on) survive — they handle initial token issue + injection at agent-create time. Re-injection on subsequent discoveries moves into the new `LocalAgentDiscoveryHandler` (see "files added" below).

**Phase 2 — files reworked**:
- `apps/minds/imbue/minds/desktop_client/cookie_manager.py` — keep `SESSION_COOKIE_NAME = "minds_session"`, `create_session_cookie`, `verify_session_cookie` for the bare-origin minds session cookie. Delete `create_subdomain_auth_token`, `verify_subdomain_auth_token` (moved to plugin's `cookie.py`). Update `cookie_manager_test.py`.
- `apps/minds/imbue/minds/desktop_client/app.py` — delete the host-header `_subdomain_forwarding_middleware`, `_handle_workspace_forward_http`, `_handle_workspace_forward_websocket`, `_handle_subdomain_auth_bridge`, `_handle_goto_workspace`, `_handle_login`, `_handle_authenticate`, the SSH-tunnel helpers (`_get_tunnel_socket_path`, `_get_tunnel_http_client`, `_connect_backend_websocket`, `_forward_workspace_http`), the `subdomain_forwarding_websocket` catch-all WebSocket route, and `_unauthenticated_subdomain_response`. Keep all minds-specific routes (`/`, `/welcome`, `/create*`, `/api/create-agent*`, `/creating/*`, `/accounts*`, `/workspace/*`, `/sharing/*`, `/_chrome*`, `/requests*`, `/api/agents/*/telegram/*`, `/api/destroy-agent/*`). The `create_desktop_client(...)` factory loses its `tunnel_manager`, `latchkey` (moves into a separate concern), `auth_store`, `stream_manager`, `auth_backend_client`, `output_format` parameters.
- `apps/minds/imbue/minds/desktop_client/backend_resolver.py` — keep `MngrCliBackendResolver`, `BackendResolverInterface`, `StaticBackendResolver`, `parse_service_log_record`, `parse_service_log_records`, `ServiceLogRecord`, `ServiceDeregisteredRecord`, `parse_agents_from_json`, `parse_agent_ids_from_json`, `ParsedAgentsResult`, `AgentDisplayInfo`. Delete `MngrStreamManager` and its `restart_observe()` method (the bounce path is now driven by `EnvelopeStreamConsumer.bounce_observe()` sending `SIGHUP` to the plugin's PID). Update `backend_resolver_test.py`.
- `apps/minds/imbue/minds/desktop_client/supertokens_routes.py` — rewire `_bounce_mngr_observe(request)` to call `forward_subprocess.bounce_observe()` on the `EnvelopeStreamConsumer` stored in `request.app.state` instead of `stream_manager.restart_observe()`. The wrapper sends `SIGHUP` to the running `mngr forward` PID; per-agent event subprocesses, SSH tunnels, and the FastAPI app on the plugin side stay alive. Update `supertokens_routes_test.py` accordingly. Rename the helper to `_bounce_forward_observe` for clarity.

**Phase 2 — files added**:
- `apps/minds/imbue/minds/desktop_client/forward_cli.py` — minds-side wrapper around the `mngr forward` subprocess (named for symmetry with `imbue_cloud_cli.py`). Contains:
  - `EnvelopeStreamConsumer(MutableModel)` — owns the `RunningProcess` for `mngr forward`. Reads stdout line-by-line on a thread, parses each line into a `ForwardEnvelope` using the plugin's data types (re-imported from `imbue.mngr_forward.data_types`). Dispatches:
    - `stream == "observe"` → existing `parse_discovery_event_line(...)` flow → `MngrCliBackendResolver.update_agents(...)`, **and** fans the resulting per-agent discovery / destruction events to any `add_on_agent_discovered_callback` / `add_on_agent_destroyed_callback` consumers (signature `(agent_id, ssh_info, provider_name)` for discovery and `(agent_id,)` for destruction — same as today's `MngrStreamManager`). The plugin re-emits a `FullDiscoverySnapshotEvent` after `bounce_observe()`, which translates to a fresh round of discovery callbacks.
    - `stream == "event"` → existing `parse_service_log_record(...)` flow → `MngrCliBackendResolver.update_services(...)`, plus the request/refresh callback routing already in place.
    - `stream == "forward"` and `payload.type == "reverse_tunnel_established"` → calls a registered `MindsApiUrlWriter` callback.
    - `stream == "forward"` and `payload.type` in `{"login_url","listening"}` → logged at debug level.
  - Exposes `bounce_observe()` — sends `SIGHUP` to the plugin's PID (looked up via `RunningProcess.pid`). This is the replacement for today's `MngrStreamManager.restart_observe()` and is what `apps/minds/imbue/minds/desktop_client/supertokens_routes.py` calls after writing a new `[providers.imbue_cloud_<slug>]` block on sign-in. Logs and no-ops if the plugin is no longer running.
  - Exposes `add_on_agent_discovered_callback(callback)` / `add_on_agent_destroyed_callback(callback)` — public API mirroring today's `MngrStreamManager`. Used by minds startup to wire in the light `LocalAgentDiscoveryHandler` below, plus the surviving `LatchkeyDiscoveryHandler` and `LatchkeyDestructionHandler`.
  - `ForwardSubprocessConfig(FrozenModel)` — args passed to `mngr forward`: `port`, `preauth_cookie`, `service`, `agent_include`, `reverse_specs`.
  - `start_mngr_forward(concurrency_group, config) -> tuple[EnvelopeStreamConsumer, str]` — generates a 64-byte `secrets.token_urlsafe` preauth token, spawns `mngr forward` with the assembled args via `concurrency_group.run_process_in_background`, attaches the `EnvelopeStreamConsumer`, returns the consumer plus the preauth cookie value (so callers can hand it to Electron).
  - `MindsApiUrlWriter` — the callback that handles `reverse_tunnel_established` for **remote** agents. Given `(agent_id, remote_port, local_port, ssh_host, ssh_port)`, it opens a paramiko SSH connection to the agent's host (using SSH info already cached in the surviving resolver) and writes `http://127.0.0.1:<remote_port>` to the agent's `<state_dir>/minds_api_url`. The write is **unconditional** — the writer overwrites the file on every event with no diff check, so a tunnel-repair that drew a new sshd-assigned remote port is reflected immediately without bookkeeping. Same logic as today's `AgentDiscoveryHandler.write_api_url_to_remote`, just driven by the envelope payload instead of in-process discovery.
  - `LocalAgentDiscoveryHandler(MutableModel)` — registered via `EnvelopeStreamConsumer.add_on_agent_discovered_callback`. Replaces the parts of the deleted `AgentDiscoveryHandler` that didn't depend on the plugin's reverse-tunnel events:
    - For agents with `ssh_info is None` (local agents, which never get a `reverse_tunnel_established` event because there's no SSH transport to tunnel through), writes `http://127.0.0.1:<minds-api-port>` to `<MNGR_HOST_DIR>/agents/<agent-id>/minds_api_url` directly on the local filesystem.
    - For every newly-discovered agent (local or remote), looks up any persisted Cloudflare tunnel token via `load_tunnel_token(...)` and re-injects it via `inject_tunnel_token_into_agent(...)`. This restores today's behavior where a `minds run` restart picks the token back up so cloudflared inside the agent can reconnect.
  - `_ForwardSubprocessLifecycleWatcher` — a thread that calls `RunningProcess.wait()` and, on plugin exit, captures stderr + exit code, dispatches a `NotificationDispatcher` notification + a logged error line, and signals minds to exit non-zero via the surrounding `ConcurrencyGroup`.
- `apps/minds/imbue/minds/desktop_client/forward_cli_test.py`.
- `apps/minds/imbue/minds/cli/run.py` — the renamed entry point (full rewrite of the deleted `runner.py`). The new `run` command:
  - Click flags: today's `--host`, `--port`, `--no-browser`, plus a new `--mngr-forward-port` (default 8421).
  - Builds `WorkspacePaths`, `MindsConfig`, `Latchkey`, `MultiAccountSessionStore`, `AgentCreator`, `TelegramSetupOrchestrator`, `NotificationDispatcher`, `RequestInbox`, `LatchkeyPermissionGrantHandler`, `SharingRequestHandler`, `ImbueCloudCli` — the same minds-specific dependencies as today's `start_desktop_client`.
  - Calls `start_mngr_forward(...)` to spawn the plugin and obtain `(EnvelopeStreamConsumer, preauth_cookie)`.
  - Builds `MngrCliBackendResolver` and registers it on the consumer so the resolver gets fed from the plugin's stream.
  - Registers `MindsApiUrlWriter` as the consumer's reverse-tunnel callback.
  - Registers `LocalAgentDiscoveryHandler` via `consumer.add_on_agent_discovered_callback` for local-agent `minds_api_url` writes and Cloudflare tunnel-token re-injection.
  - Re-registers the surviving `LatchkeyDiscoveryHandler` / `LatchkeyDestructionHandler` against the consumer's discovery callbacks (these still use the in-process `SSHTunnelManager` for now; their migration to the plugin's `--reverse` is a follow-up spec).
  - Builds the minds-side bare-origin FastAPI app via the slimmed `create_desktop_client(...)` and runs `uvicorn` on `--port` (default 8420).
  - Emits a `mngr_forward_started` JSONL event on its own stdout (carrying the preauth cookie value) so the Electron shell can pre-set the cookie on `localhost:<mngr-forward-port>` before opening any agent subdomain.
  - On shutdown / parent death / plugin death, terminates the plugin subprocess and exits non-zero with the captured stderr + exit code.
- `apps/minds/imbue/minds/cli/run_test.py`.
- `apps/minds/imbue/minds/cli_entry.py` — drop `from imbue.minds.cli.forward import forward` / `cli.add_command(forward)`; add the `run` import + registration.

**Phase 2 — Electron and tooling**:
- `apps/minds/electron/main.js`, `backend.js` — change spawn args from `minds forward` to `minds run`. Read the `mngr_forward_started` event from `minds run`'s stdout, then call `BrowserWindow.webContents.session.cookies.set({ url: 'http://localhost:<mngr-forward-port>', name: 'mngr_forward_session', value: '<preauth>', httpOnly: true })` before any agent-subdomain navigation.
- `justfile` recipes that call `minds forward` (`minds-start`, `minds-stop`, `propagate-changes`, `create-pool-hosts-dev`, etc.) — audit and rewrite to call `minds run`. Recipe names themselves stay (no user-visible change to cadence).
- `apps/minds/scripts/install.sh` and any other shell scripts mentioning `minds forward` — updated.
- `apps/minds/README.md`, `apps/minds/docs/*.md` — references updated.
- E2E tests (`apps/minds/test_desktop_client_e2e.py`, `apps/minds/test_sse_redirect.py`, `apps/minds/test_supertokens_auth_e2e.py`) — invocation changed to `minds run`; assertions about subdomain forwarding adjusted to expect the plugin-served origin.
- `changelog/mngr-forwarder.md` — user-visible summary: `mngr forward` available standalone; `minds forward` renamed to `minds run`; the `mngr_forward` plugin must be enabled (`mngr plugin enable forward`) for full minds startup.

### Coordination notes

- Plugin imports are eager (no lazy-import discipline) — paramiko, FastAPI, uvicorn, websockets, itsdangerous, jinja2, httpx are all loaded at `mngr` plugin-discovery time when `forward` is enabled. This is acceptable because the plugin is opt-in.
- The plugin's `MngrError`-derived exception hierarchy keeps the CLAUDE.md "never raise built-in Exceptions" rule.
- `cookie_manager.py` is split, not relocated whole — minds and the plugin each keep a copy of `create_session_cookie`/`verify_session_cookie` for their own session cookies (different names, different signing keys, different ports). The two small implementations are nearly identical; this avoids a cross-package dependency for two trivial helpers.
- Minds keeps its own `MngrCliBackendResolver` (the rich service-aware resolver minds uses for its UI). The plugin uses the slimmer `ForwardResolver` for its own resolution. The two are not unified.
- Multiple `mngr forward` processes on the same machine pick different ports; cookies are origin-scoped so they don't interfere.
- Plugin lifecycle: `mngr forward` exits when its parent (the `minds run` process, in the Electron flow) dies, via `start_parent_death_watcher(...)`. On exit it terminates its own observe/event children, cleans up tunnels, and stops listening.
- **Phase 1 coexistence**: while the plugin is enabled and minds is also running its in-process forwarder, both bind ports must differ (plugin defaults to `8421`, minds to `8420`) and signing-key files live in different directories (plugin: `$MNGR_HOST_DIR/plugin/forward/signing_key`; minds: minds' own data dir), so cookies don't collide. This is documented as user-facing guidance ("don't enable the `forward` plugin while minds is running, or change the plugin's `--port`") rather than enforced in code.
- **Latchkey is intentionally out of scope.** The plugin doesn't know about Latchkey, doesn't bundle the `LatchkeyDiscoveryHandler`, and doesn't attempt to set up its per-agent reverse tunnel. After Phase 2, minds keeps `desktop_client/ssh_tunnel.py` and `latchkey/core.py` exactly as-is, registering the surviving `LatchkeyDiscoveryHandler` / `LatchkeyDestructionHandler` against `EnvelopeStreamConsumer`'s discovery callbacks. The end-state where the plugin's static `--reverse 1989:<fixed>` covers latchkey requires a follow-up spec to first migrate the latchkey gateway from one-process-per-agent to one-process-per-host with in-band per-agent routing (likely an agent API key the latchkey client passes in a request header). Only then can `ssh_tunnel.py` be deleted.

## Implementation Phases

### Phase 1 — land the plugin standalone

- Create `libs/mngr_forward/` package: `pyproject.toml`, `README.md`, `conftest.py`, source layout, click command, FastAPI app, all moved code, templates, ratchets, unit + acceptance + release tests.
- Add the new package to the workspace (root `pyproject.toml` workspace members, `uv.lock` regenerated via `uv sync --all-packages`).
- `mngr forward` is fully functional standalone — `mngr forward --service system_interface` against a local agent works end-to-end, including OTP login, subdomain forwarding, observe/event passthrough, and `--reverse` tunnels.
- `apps/minds/` is unchanged. Minds still runs its own in-process forwarding via the existing `desktop_client/runner.py` and the unchanged `app.py`. The plugin and minds coexist with duplicated functionality during this phase.
- `mngr plugin enable forward` is required at install time to activate (default-disabled like `mngr_vultr` / `mngr_imbue_cloud`).
- Add a `changelog/mngr-forwarder.md` placeholder for the user-visible summary; final wording lands in Phase 2.

End state: plugin tested end-to-end (in-process acceptance + Modal release), CI green, docs in `libs/mngr_forward/README.md`, `mngr forward` usable today by anyone willing to enable the plugin.

### Phase 2 — switch minds over and prune the duplicates

- Add `apps/minds/imbue/minds/desktop_client/forward_cli.py` (`EnvelopeStreamConsumer`, `MindsApiUrlWriter`, `start_mngr_forward`, `_ForwardSubprocessLifecycleWatcher`).
- Rename `apps/minds/imbue/minds/cli/forward.py` → `cli/run.py` and rewrite it as the subprocess-orchestrating entry point (no `runner.py` left behind).
- Delete `desktop_client/auth.py`, `runner.py`, the subdomain-forwarding portions of `app.py`, `MngrStreamManager` from `backend_resolver.py`, the subdomain-auth helpers in `cookie_manager.py`, and all of their tests. Do **not** delete `desktop_client/ssh_tunnel.py` / `ssh_tunnel_test.py` — Latchkey still uses `SSHTunnelManager` for its per-agent dynamic-port reverse tunnel.
- Slim `create_desktop_client(...)` to just the minds-specific routes and parameters.
- Update `apps/minds/imbue/minds/cli_entry.py` to register `run` instead of `forward`.
- Rewire Electron: `apps/minds/electron/main.js` / `backend.js` spawn args change to `minds run`; cookie pre-setting logic added.
- Audit and update the justfile / scripts / docs / e2e tests for the rename.
- Finalize `changelog/mngr-forwarder.md`.

End state: minds is smaller (no SSH-tunnel / auth / subdomain-forwarding code in-process), the plugin owns those concerns, all tests pass, no duplicated code paths. `minds run` is the entry point everywhere.

## Testing Strategy

### Unit tests (per module, in `libs/mngr_forward/`)

- `auth_test.py` — OTP issuance + validation + single-use semantics; in-memory persistence (no `one_time_codes.json` written); rejection of unknown / already-used codes; signing-key generation + reuse.
- `cookie_test.py` — `create_session_cookie` + `verify_session_cookie` round-trip; tampered-cookie rejection; preauth-cookie exact-match path; `create_subdomain_auth_token` audience binding to a specific `agent_id`; expired-token rejection.
- `ssh_tunnel_test.py` — forward-tunnel Unix-socket creation + relay (against a stub paramiko transport); reverse-tunnel `request_port_forward` with the per-forward handler; health-check repair on broken transport; AF_UNIX socket-path-length safety on macOS.
- `resolver_test.py` — `--service` resolution from the services-by-agent map; `--port` resolution against discovered SSH info; missing-URL → `None`; latest-write-wins when manual snapshot collides with observed update.
- `stream_manager_test.py` — observe/event subprocess fan-out (against stub processes producing canned JSONL); line parsing; `--agent-include` / `--agent-exclude` CEL filtering pre-spawn; `on_agent_discovered` / `on_agent_destroyed` callback firing; correct passthrough emission for every line read; `bounce_observe()` terminates only the observe child (per-agent event subprocesses, registered callbacks, and resolver state survive); the new observe child receives the same args and re-emits a `FullDiscoverySnapshotEvent` that lands on the resolver.
- `envelope_test.py` — JSONL emission per stream + payload type; `agent_id` presence / absence per envelope contract; thread-safety under concurrent emitters (no interleaved bytes mid-line).
- `snapshot_test.py` — `mngr list --format jsonl` parsing in `--no-observe` mode; empty-snapshot causes non-zero exit; SSH info plumbed through.
- `reverse_handler_test.py` — per-agent setup-on-discovery; multi-pair `--reverse` invocation; `reverse_tunnel_established` event payloads with full SSH info; re-emission on health-check repair (with new remote port if reassigned).
- `server_test.py` — auth + forwarding routes (unauthenticated → login form; valid OTP → cookie set; bare origin `/` → debug index for authenticated, OTP form for not; `<agent>.localhost` catch-all routes correctly; `/_subdomain_auth` token validation; 503 retry HTML when the URL isn't ready); strict `agent-<hex>.localhost` pattern enforcement.
- `config_test.py` — `ForwardPluginConfig` defaults, `merge_with`, parsing from TOML.
- `cli_test.py` — flag mutual exclusion (`--service` and `--port`), `--no-observe + --service` rejected as a usage error, required-flag enforcement, `--reverse` repeatability, `--reverse 0:<local>` accepted (dynamic remote port), `--no-observe` empty-snapshot exit, `--preauth-cookie` env var fallback, `--event-include` / `--event-exclude` plumbed into the events filter, `SIGHUP` handler dispatch (registers, sets the watcher event, dispatches to `bounce_observe()` in observe mode and `_resnapshot_now()` in `--no-observe` mode, no-ops on early/late signals).

### Acceptance test (in plugin, `@pytest.mark.acceptance`)

- `test_forward_e2e.py` — uses mngr's `temp_host_dir` + `local_provider` + `temp_mngr_ctx`. Creates a real local agent that exposes a tiny `aiohttp` stub serving 200 with a known body at `system_interface`. Starts `mngr forward --service system_interface` on a free port (via `subprocess.Popen` against the installed click command). Asserts:
  - Bare-origin GET → login form HTML.
  - GET to the printed login URL → `mngr_forward_session` cookie set, redirect to `/`.
  - GET `/` (with cookie) → debug index lists the agent.
  - GET `/goto/<id>/` → 302 to `<id>.localhost:<port>/_subdomain_auth?...`.
  - Following the redirect → per-subdomain cookie set, redirect to `/`.
  - GET `<id>.localhost:<port>/` (with subdomain cookie) → byte-forwarded stub body.
  - Plugin emits `listening`, `login_url`, and an envelope-formatted `observe` snapshot on stdout.
  - WebSocket connect to `<id>.localhost:<port>/ws` is forwarded to the stub.
  - Manual `--port 8080 --no-observe` mode also serves the agent (after `mngr list` snapshot).
  - Sending `SIGHUP` to the plugin's PID after a `settings.toml` rewrite causes a fresh observe child to start (visible by a new `FullDiscoverySnapshotEvent` on stdout) without dropping the per-agent event stream or pre-existing browser sessions (cookie issued before the bounce still authenticates after).

### Release test (in plugin, `@pytest.mark.release`)

- `test_forward_modal_release.py` — boots a Modal-sandbox agent (smallest workspace template). Runs `mngr forward --service system_interface --reverse 7777:7777` locally. Asserts:
  - `agent-<id>.localhost:<port>/` returns 200 from the real workspace_server through the SSH tunnel.
  - A locally-listening stub on `127.0.0.1:7777` is reachable from inside the Modal sandbox via the reverse tunnel.
  - `reverse_tunnel_established` is emitted with the correct `ssh_host` / `ssh_port`.
  - Killing the parent (the test runner's `ConcurrencyGroup`) terminates `mngr forward` cleanly within a couple of seconds.

### Cross-cutting tests in `apps/minds/`

- `apps/minds/imbue/minds/desktop_client/test_desktop_client.py` keeps tests of bare-origin minds-side behavior: login flow for the `minds_session` cookie, create page, accounts, sharing, requests, telegram. Tests of subdomain forwarding are deleted (those paths are tested in the plugin).
- New `apps/minds/imbue/minds/desktop_client/forward_cli_test.py` exercises the `EnvelopeStreamConsumer` + `MindsApiUrlWriter` + `LocalAgentDiscoveryHandler` integration against a stubbed subprocess emitting hand-crafted envelope lines. Asserts envelope dispatching feeds the resolver correctly, that `reverse_tunnel_established` triggers the SSH write **unconditionally on every event** (including a re-emit with a different remote port), that `add_on_agent_discovered_callback` registered handlers fire on observe-stream agent-discovered events with `(agent_id, ssh_info, provider_name)`, that `LocalAgentDiscoveryHandler` writes `minds_api_url` for `ssh_info is None` agents and re-injects stored Cloudflare tunnel tokens, that `bounce_observe()` sends `SIGHUP` to the right PID and is a no-op when the plugin is already gone, and that subprocess death raises a notification + exits non-zero.
- `apps/minds/imbue/minds/desktop_client/supertokens_routes_test.py` is updated so the post-sign-in path asserts a `bounce_observe()` call on a stubbed `EnvelopeStreamConsumer` instead of the old `MngrStreamManager.restart_observe()` call.

### Manual verification

- `just minds-start` (after Phase 2): Electron opens the minds UI on `localhost:8420`; the `mngr forward` subprocess is running on `localhost:8421`; the user is auto-authenticated against the plugin's port; navigating to a workspace works; opening a service inside the workspace works (workspace_server still does its own per-service routing).
- `mngr forward --service system_interface --open-browser` (no minds, no Electron): the system browser opens; the OTP flow works; `agent-<id>.localhost:8421/` works for a real local agent; `/` debug index lists discovered agents.
- `mngr forward --no-observe --port 8080 --reverse 7777:7777`: `mngr list` snapshot is taken; only listed agents are forwarded; reverse tunnels come up; plugin exits with a clear error if the snapshot is empty.
- Kill `mngr forward` mid-flight from `minds run`: minds detects the death, surfaces a notification with stderr + exit code, and exits non-zero.

### Edge cases

- Two `mngr forward` processes on the same machine — they pick different ports; cookies don't cross.
- Plugin process killed mid-flight — reverse tunnels collapse, minds detects via `RunningProcess` and EOF, surfaces the failure, and exits non-zero.
- `--reverse` with a port already in use on the remote host — paramiko raises, the plugin logs and continues without that tunnel; the next health-check tick retries.
- `mngr observe` produces a `DiscoveryErrorEvent` — passed through to stdout under `stream: "observe"` so minds can handle it the same way it handles every other observe error today.
- `<agent-id>.localhost` request before the agent's `services/events.jsonl` has emitted a `system_interface` URL — 503 + auto-refresh HTML page; once the URL is known, normal forwarding resumes on the next request.
- Manual `--port` against a local agent — bypasses SSH tunneling entirely; forwards directly to `127.0.0.1:<port>` since local agents have no `ssh_info`.
- Subdomain that doesn't match `agent-<hex>` — 404 (or, for HTML, redirect to the bare-origin landing page).
- `SIGHUP` while the observe child is mid-startup or mid-shutdown — the watcher thread serializes bounces, so concurrent SIGHUPs collapse into one effective bounce.
- `SIGHUP` in `--no-observe` mode where the new `mngr list` snapshot is empty — log a warning and keep serving the previous set (in contrast to startup, where empty is fatal).

## Open Questions

- **Latchkey gateway routing scheme** (deferred to a separate spec): once the latchkey gateway moves to a single fixed-per-host process on a single fixed local port, how does that gateway identify which agent each inbound request belongs to? The likely answer is an agent API key the latchkey client passes in a request header, but the exact mechanism — header name, key issuance, rotation, key->agent lookup — is left to a follow-up spec. Resolving this is what unblocks deleting minds' `desktop_client/ssh_tunnel.py` and folding latchkey reverse tunnels into the plugin's `--reverse 1989:<fixed>`.
