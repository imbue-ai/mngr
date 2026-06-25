# Unabridged Changelog - mngr_forward

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_forward/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-23

The websocket forward path now emits a `system_interface_backend_failure` envelope when the backend connection fails (unresolved target, SSH-tunnel setup failure, refused host-loopback dial, or a connect-time backend-websocket failure), matching what the HTTP/SSE paths already did. Previously only HTTP failures emitted this envelope, so a consumer like minds could go blind to a dead system interface whose only live channel was a websocket: an already-loaded SPA whose backend died would silently retry its websocket forever, never enrolling the agent as a recovery probe suspect, so the recovery redirect never fired and the user was stranded on a frozen workspace. The websocket path now feeds the same recovery signal as HTTP.

## 2026-06-22

Added a `mngr forward --on-error {abort,continue}` flag (default `abort`). Under
`continue`, the `--no-observe` startup snapshot tolerates an
unauthenticated/unreachable provider: it runs `mngr list --on-error continue` and
forwards the agents the healthy providers reported instead of failing to start.
The flag affects only `--no-observe`; the observe and `--observe-via-file` modes
already tolerate provider errors and are unchanged.

## 2026-06-19

Removed the now-vestigial `ForwardPluginConfig.merge_with` override; the config merge is routed through the overlay pipeline, which reproduces the same assign-by-default semantics. No user-visible behavior change.

Trimmed the README to user-relevant content and tightened it for concision.

## 2026-06-11

Hardened reverse SSH tunnel teardown so a half-dead connection no longer orphans the forwarded port on the remote sshd (which made the next run's port forward request get denied):

- `SSHTunnelManager.cleanup()` now attempts to cancel each reverse port forward unconditionally, instead of skipping the cancel when the connection looks inactive.

- Every tunnel SSH connection now sends periodic keepalives, so an idle reverse tunnel does not silently die unnoticed and the health check can repair a dead connection promptly.

## 2026-06-10

Raised the stale coverage floor from 50% to 70% to match the coverage CI already measures (~75%).

## 2026-06-08

Tests now isolate $HOME the same way as every other mngr plugin: the project
conftest calls `register_plugin_test_fixtures(globals())`, which brings in the
autouse `setup_test_mngr_env` fixture. Previously this plugin's tests did not
redirect $HOME, so running them on their own could read or write the real
`~/.mngr` / `~/.claude.json`. Internal test-infrastructure change only; no
user-facing behavior change.

- Now auto-discovered as a publishable package by the release tooling (it is a standalone `mngr forward` plugin, usable outside the minds bundle). It will be offered for first publication to PyPI on the next release. Its previously-unpinned internal deps (`imbue-mngr`, `imbue-common`, `concurrency-group`) are now pinned with `==` to their current workspace versions, as a published wheel requires. No runtime change.

## 2026-06-05

- Added to the release tooling's publish graph (`scripts/utils.py`). It will be offered for first publication to PyPI on the next release. Its previously-unpinned internal deps (`imbue-mngr`, `imbue-common`, `concurrency-group`) are now pinned with `==` to their current workspace versions, as a published wheel requires. No runtime change.

## 2026-06-04

`mngr forward --observe-via-file` makes the forward server consume discovery by tailing the shared discovery events file in-process rather than spawning its own `mngr observe --discovery-only` subprocess. Per-agent `mngr event` streams are still spawned for discovered agents. The flag is mutually exclusive with `--no-observe` and works with either `--service` or `--forward-port`. In this mode SIGHUP is a no-op (there is no observe child to bounce; the file's own writer re-emits snapshots that the tailer picks up automatically).

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-04

`mngr forward` no longer drops a live agent when its provider's discovery merely errored on a poll. The stream manager now retains agents whose provider is in the snapshot's `error_by_provider_name`, keeping their service mapping and per-agent events stream alive (logged at debug). A retained agent is only torn down on an explicit destroy or a later successful snapshot that omits it. A provider that succeeds and simply returns fewer agents still drops the missing ones as before.

## 2026-06-03

Stop following the per-agent `refresh` event source in `ForwardStreamManager`: the default event sources are now just `services` and `requests`.

This is part of tearing out the unused refresh-event plumbing across `mngr_forward` and `minds.desktop_client`. The refresh-via-desktop-client mechanism has been superseded by an `open_tab` WebSocket broadcast from the workspace server.

## 2026-06-02

The forwarding plugin now reports an unreachable backend as a backend
failure instead of flashing a raw error, and reports HTTP error responses
with a single generic reason that consumers interpret themselves.

- An SSH-tunnel setup failure and a refused host-loopback dial (no SSH
  tunnel available) are both treated as backend failures: the plugin
  emits a `CONNECT_ERROR` `system_interface_backend_failure` envelope and
  serves the styled "Loading workspace" loader to HTML callers, the same
  as other unreachable-backend cases. A consumer of the envelope stream
  can use this to drive its own recovery UI.
- The "Loading workspace" loader no longer shows the explanatory "This
  page will reload automatically..." line -- it just shows the heading,
  vertically centered against the spinner.
- HTTP error handling is simplified. The plugin no longer special-cases
  which status codes matter (it previously tagged only 502/503/504 as
  `FIVEXX_RESPONSE` and 404-on-`GET` as `NOT_FOUND_RESPONSE`). It now
  forwards every response unchanged and emits a single `ERROR_RESPONSE`
  reason -- carrying the `status_code` -- for any non-2xx response,
  leaving the policy decision (which statuses warrant action, and what
  action) entirely to the consumer.
- New `resolver_snapshot` envelope: the plugin emits the full per-agent
  service map on every mutation of that map -- both `update_services`
  (set/replace for one agent) and the destruction paths
  (`remove_known_agent` and `update_known_agents` when they drop an agent
  that had services) -- so a consumer's mirror does not retain stale
  entries for destroyed agents. The full map is sent on every change (no
  per-agent diff) so a late-attaching consumer only needs the most recent
  envelope to be in sync. No periodic flushes, no debouncing, no initial
  empty emission; the first envelope is sent on the first real services
  event. A consumer older than this change transparently drops the new
  payload; a consumer running against an older plugin simply sees no
  `resolver_snapshot` -- the same transient as a fresh plugin startup.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# ty 0.0.39 / paramiko 4.0 type fixes

- Converted bracketed `# type: ignore[...]` suppressions to `# ty: ignore[...]` (test files), since `ty` 0.0.39 no longer honors the mypy-style bracketed form.
- The new `types-paramiko` stubs (pulled in by the paramiko 4.0 bump) surfaced an intentional Liskov-violating `get_transport` override in the SSH-tunnel test fake (`FakeSSHClient`); this is now annotated with `# ty: ignore[invalid-method-override]`.

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

Test-only changes; no user-facing behavior change.

## 2026-05-26

# `SSHTunnelManager` is now the single SSH tunneling implementation

`mngr_forward/ssh_tunnel.py`'s `SSHTunnelManager` (and `RemoteSSHInfo`,
`ReverseTunnelInfo`, `SSHTunnelError`) absorbed the latchkey package's
parallel copy and is now the only SSH tunneling implementation in the
monorepo. Both the plugin's own forward (direct-tcpip) and reverse
(`--reverse REMOTE:LOCAL`) paths as well as the `mngr latchkey forward`
supervisor use it.

Behavior changes the existing forward-plugin callers will see:

- `ReverseTunnelInfo` gained an optional `agent_id: str | None = None`
  field; `setup_reverse_tunnel` gained an optional `agent_id`
  parameter. Existing callers can ignore both -- the default `None`
  matches the pre-change behavior.
- The reverse-tunnel repair loop now uses per-tunnel exponential
  backoff (1s, 2s, 4s, ..., capped at 5min) instead of the previous
  flat 30s cadence. A healthy target sees the same recovery latency;
  a permanently-gone target costs one paramiko handshake every five
  minutes instead of every 30s. Failures clear on a successful repair
  (or when a sibling tunnel on the same SSH host gets repaired and
  the connection comes back).
- New `remove_reverse_tunnels_for_agent(agent_id)` method tears down
  every reverse tunnel tagged with a given `agent_id`. It is careful
  not to close an SSH client out from under any live *forward* tunnel
  that shares the same host -- the two flavors of tunnel can coexist
  on one connection.

Public API additions are backward-compatible. The deleted
`mngr_latchkey/ssh_tunnel.py` is now re-exported transparently from
this module's existing public surface.

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

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
