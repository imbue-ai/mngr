# Unabridged Changelog - mngr_forward

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_forward/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-22

`mngr forward` no longer crashes when its bind port is already in use. `--port` is now optional: when omitted, the server tries its default (8421) and falls back to an OS-assigned port if that is taken; when supplied explicitly, it still binds exactly that port and fails fast (with a clean error) if it is unavailable. The server binds its listen socket up front and hands it to uvicorn, so the `listening` envelope always reports the port actually bound.

## Discovery schema bump

- `mngr_forward` parses `FullDiscoverySnapshotEvent` lines from its inner `mngr observe --discovery-only` subprocess. The event grew two additional fields (`providers` and `error_by_provider_name`) in `libs/mngr`. This build picks them up transparently -- older `mngr_forward` builds running against new snapshots will raise `DiscoverySchemaChangedError` and must be rebuilt.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Renamed the workspace-server envelope contract to system-interface in lockstep with the mngr-side rename: `WorkspaceBackendFailure*` → `SystemInterfaceBackendFailure*`, the envelope type literal `workspace_backend_failure` → `system_interface_backend_failure`, and the plugin's 503 loader page now reads "System interface starting".

Workspace-server restart and health-recovery support on the `mngr_forward` plugin (consumed by minds).

- The plugin emits `workspace_backend_failure` envelopes when it sees connection errors, mid-SSE EOF, or 5xx responses from the workspace backend. Consumers (minds) can track these as a per-agent health state machine to trigger a recovery UI.
- The plugin's 503 fallback page (shown while the workspace server is
  unreachable) is now a styled card with a loading spinner instead of the
  blank "Backend not yet available. Retrying..." page. It still auto-refreshes
  every second.
- The "Workspace server starting" loader spinner's animation duration now
  matches the page's 1-second auto-refresh interval, so the spinner is at
  the cycle boundary (rather than 90 degrees past it) when the reload fires.

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

## 2026-05-09

- Fixed: the `mngr forward` subprocess no longer pegs ~130% CPU after agents disconnect. The bidirectional relay loop (now lifted to a single shared module at `libs/mngr_forward/imbue/mngr_forward/relay.py`, used by both the desktop client's reverse tunnels and `mngr forward`'s direct-tcpip forwards) terminates when the paramiko channel has received EOF; previously `select.select` would mark the channel readable but `recv_ready()` returned False, falling through and spinning the loop at ~1M iters/sec on each half-closed channel.

## 2026-05-06

# mngr_forward plugin

A new `mngr_forward` plugin (in `libs/mngr_forward/`) lands the auth +
subdomain-forwarding logic that used to live inside the minds desktop
client. The plugin runs as a standalone tool:

```bash
mngr plugin enable forward
mngr forward --service system_interface
```

What you get:

- Local proxy on `127.0.0.1:8421` that serves
  `<agent-id>.localhost:8421/*` and byte-forwards each HTTP and WebSocket
  request to the agent's `system_interface` URL via SSH tunnels for
  remote agents.
- One-time login URL printed to stderr (or emitted as a JSONL `login_url`
  event in `--format jsonl`); the resulting cookie is signed with a key
  persisted under `$MNGR_HOST_DIR/plugin/forward/` so browser sessions
  survive plugin restarts.
- `--reverse <remote-port>:<local-port>` (repeatable) sets up reverse SSH
  tunnels for every discovered remote agent. `<remote-port>` may be `0`
  for sshd-assigned ports; the actual bound port is reported via a
  `forward.reverse_tunnel_established` envelope event.
- `--no-observe --forward-port REMOTE_PORT` mode runs `mngr list` once
  and forwards a fixed snapshot. `--no-observe --service NAME` is rejected
  as a CLI usage error.
- `--agent-include` / `--agent-exclude` / `--event-include` /
  `--event-exclude` CEL filters control which agents and event sources
  the plugin tracks.
- `SIGHUP` bounces only the `mngr observe` child subprocess; SSH tunnels,
  per-agent event subprocesses, browser sessions, and the FastAPI app
  stay alive.
