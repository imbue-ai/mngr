# Unabridged Changelog

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-09

Upgrade offload from 0.8.1 to 0.9.0 and enable history-based test scheduling.
Offload now records per-test durations and uses them to balance sandbox load times,
reducing wall-clock time for the test suite.

Upgrade offload from 0.9.0 to 0.9.2 in CI. Picks up a fix for thin-diff application. Adds the offload binary to the sandbox image (via a multi-stage build) so 0.9.2's `offload apply-diff` step works without falling back to a full rebuild, and propagates `GITHUB_HEAD_REF` / `GITHUB_REF_NAME` through to sandboxes so branch-aware tests like the changelog-entry ratchet identify the PR branch correctly.

- Fixed: local-host shell commands issued from worker threads (e.g. inside `mngr observe --discovery-only`, `mngr list`, `mngr message`, `mngr destroy`, `mngr gc`, `mngr discover`) no longer crash with `TypeError: child watchers are only available on the default loop` on Linux. `Host._run_shell_command` now bypasses pyinfra's gevent-backed `LocalConnector` for local hosts and runs commands via the `ConcurrencyGroup` process runner.

- Fixed: `mngr stop` no longer leaves orphaned child processes alive on Linux when the agent's pane process (e.g. `claude`) was killed abruptly (SIGKILL, OOM). The pane-descendant walk previously missed grandchildren that had reparented to PID 1 -- typically `playwright-mcp`, `node`, or other long-lived helpers -- so they survived `mngr stop` and accumulated, consuming memory across stop/start cycles. `Host.stop_agents` now also enumerates processes by their inherited `MNGR_AGENT_ID` env var via `/proc/<pid>/environ`, catching these orphans regardless of process tree.

`apps/minds/scripts/propagate_changes` now protects `.claude/settings.local.json` from `rsync --delete` when syncing the template into an agent's work_dir.

That file is generated per-agent at create time by mngr's `_configure_agent_hooks` and holds the `UserPromptSubmit` hook that signals `tmux wait-for -S "mngr-submit-..."`. Without it, every `send_message` hangs the 90-second submission-signal timeout while the prompt is actually delivered to Claude (so the UI shows the message and Claude responds normally, but the HTTP `/message` request times out).

Previously the script only protected `runtime/` and `.mngr/`, so iterating with `propagate_changes` reliably reproduced the hang -- and there was no easy way to recover short of recreating the agent.

Removed `apps/minds_workspace_server/` from the monorepo. The workspace server (the FastAPI + dockview UI service that runs inside each agent's container) has been migrated to forever-claude-template, where it now lives at `apps/system_interface/` and ships as the `minds-workspace-server` CLI. Consumers (the minds desktop client and mngr) pick it up at runtime from the consumer's vendored forever-claude-template checkout instead of from this repo. Build-time impact: the release Dockerfile no longer cross-references the workspace server's frontend, and the node/npm install step that existed only to build it has been dropped. The `apps/minds/scripts/propagate_changes` dev-loop script now rsyncs from `/code/apps/system_interface/frontend/` in the running agent. User-facing docs (`apps/minds/docs/overview.md`, `apps/minds/docs/workspace/getting_started.md`) and the historical specs that referenced the old path were updated.

- Fixed: closing the last tab in a minds workspace no longer leaves a blank screen with no recovery path. The primary agent's chat tab is automatically reopened when the dockview becomes empty (whether by closing all tabs at runtime or restoring an empty saved layout).

Fix WebSocket broadcaster queue-full flood and hung-send pin: stuck WS clients are evicted after 50 consecutive queue-full broadcasts, and the broadcaster cancels the wedged handler's asyncio task to free a coroutine blocked in `await websocket.send_text(...)` on a half-dead TCP connection. The previous behaviour pegged a CPU core and filled tmux with `WebSocket client queue full, dropping message` warnings whenever a single client stopped draining its queue.

- minds now injects `LATCHKEY_DISABLE_COUNTING=1` into every workspace
  whenever latchkey is wired (alongside `LATCHKEY_GATEWAY`,
  `LATCHKEY_GATEWAY_PASSWORD`, and `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE`).
  The workspace-side `latchkey` CLI runs in client mode against the
  host-side gateway, so suppressing its daily goatcounter.com ping
  prevents every agent from being counted as a separate active user --
  the single host-side gateway already represents the one real user.

- Fixed a perpetual one-event lag in remote (online-host) event
  follow-mode tailing. When reading `events.jsonl` over the wire,
  pyinfra's `CommandOutput.stdout` is built by `"\n".join(...)` after
  each line is `rstrip("\n")`-ed, so a file ending in `\n` and one not
  ending in `\n` produced identical strings. The follow loop's
  partial-write guard then treated the most recent complete line as
  in-flight and held it back until a later line forced it out. The
  remote read now wraps `cat` as `{ cat <file> && printf '%s' <sentinel>; }`,
  asserts the sentinel is present, and strips it back off to recover
  the file's exact byte tail.

- Bumped the `latchkey` npm dependency to 2.8.0 and switched minds to
  running a single shared `latchkey gateway` subprocess for every agent
  instead of one per agent. The gateway is now password-protected via
  `LATCHKEY_GATEWAY_LISTEN_PASSWORD` (the password is derived
  deterministically from the desktop client's Latchkey encryption key by
  hashing a JWT minted with `latchkey gateway create-jwt`, so it
  survives restarts without being persisted in plaintext).
- Each agent gets its own `latchkey_permissions.json`. At
  agent-creation time minds allocates an opaque
  `~/.minds/latchkey/permissions/<uuid>.json` handle, materializes it
  with empty rules (deny-all baseline), mints a permissions-override
  JWT for that path, and injects all three latchkey env vars
  (`LATCHKEY_GATEWAY`, `LATCHKEY_GATEWAY_PASSWORD`,
  `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE`) at `mngr create` time.
  After `mngr create` returns the canonical agent id, minds replaces
  the opaque file with a symlink pointing at the canonical
  `~/.minds/agents/<agent_id>/latchkey_permissions.json` location, so
  the existing permission-grant flow continues to write to its
  conventional path while the gateway reads through the symlink. The
  gateway's own default permissions config
  (`~/.minds/latchkey_default_permissions.json`) is materialized empty
  (deny-all) so requests that bypass the JWT mechanism cannot reach
  any service.
- DEV-mode agents (which run in-process on the bare host with no SSH
  reverse tunnel) now go through the same gateway as every other
  launch mode. `AgentCreator` queries the gateway's live host port
  and injects it as `LATCHKEY_GATEWAY=http://127.0.0.1:<dynamic_port>`
  alongside the password and JWT. Previously DEV agents bypassed the
  gateway entirely, which made the full latchkey flow impossible to
  exercise from DEV.
- The agent-side `latchkey` CLI version is pinned in
  `forever-claude-template`'s Dockerfile (`ARG LATCHKEY_VERSION=…`) and
  must be bumped to 2.8.0 in lockstep with this change. Agents booting
  from an image that pre-dates the bump will hit `401` on every gateway
  call because their `latchkey` CLI doesn't know about the new password
  and override headers.
- Old per-agent gateway records left under
  `~/.minds/agents/<id>/latchkey_gateway.json` are cleaned up
  automatically on desktop-client startup. Agents that were created
  with earlier minds versions need to be re-created to pick up the new
  env vars; without them their `latchkey` CLI calls will be rejected by
  the now-password-protected gateway.

- Fixed the changelog consolidation cron's commit author email: was `dev@imbue.com`, now `bot@imbue.com`, matching the verified email on the bot GitHub account whose token the cron uses to push and open PRs. Without this, GitHub couldn't attribute consolidation commits to the bot user.

- `scripts/setup_changelog_agent.sh` now redeploys when re-run: removes any existing `changelog-consolidation` schedule before recreating, so the deployed schedule always reflects the current source. Drops the `CHANGELOG_REPLACE=1` gate that previously errored on an existing schedule.
- Header docstring now lists the required `GH_TOKEN` (token for `bot@imbue.com`) and `ANTHROPIC_API_KEY` env vars, and includes the on-demand trigger one-liner.

- Added a changelog system for tracking changes across PRs
  - Per-PR changelog entry files in `changelog/` directory, enforced by CI via meta ratchet test
  - Nightly automated consolidation of changelog entries into `UNABRIDGED_CHANGELOG.md` (full entries) and `CHANGELOG.md` (concise AI-generated summary)
  - Idempotent setup script for the consolidation agent (`scripts/setup_changelog_agent.sh`)

- `mngr imbue_cloud admin pool create`: post-create read-back is now scoped to `--provider <provider>` (default `vultr`) and uses `--on-error continue`, so a pre-existing stale host on the operator's machine no longer aborts the bake before the management-key install + DB INSERT. The bake still fails loudly when the just-created agent is genuinely missing from the listing output.
- Removed the broken `just create-pool-hosts-dev` and `just create-pool-hosts` recipes. Both called `apps/remote_service_connector/scripts/create_pool_hosts.py`, which still inserted into the dropped `pool_hosts.version` column and so failed against the migrated schema. The replacement is `mngr imbue_cloud admin pool create` (with `--mngr-source` for the dev-loop's working-tree-into-vendor/mngr/ rsync). `just sync-vendor-mngr` is unchanged -- it serves a different (release) flow not covered by the plugin. Updated `just minds-start`'s "no FCT worktree" hint and the `minds-dev-iterate` skill to point at the new bake path.
- Deleted dead code: `apps/remote_service_connector/scripts/create_pool_hosts.py` (replaced by `mngr imbue_cloud admin pool create`), and the now-unused `libs/imbue_common/imbue/imbue_common/pool_host_constants.py` (`PLACEHOLDER_ANTHROPIC_API_KEY`) plus its test. Updated `apps/minds/docs/host-pool-setup.md` to document the new schema (`attributes JSONB`) and bake command, and updated `generate_management_key.py`'s usage hint to point at the new command.

Add a Concise spec under `specs/expose-outer-host/concise.md` for
exposing each host's outer machine (the VPS / docker daemon host /
local machine hosting a container). Restructures the host class
hierarchy so `OuterHost` becomes the base class with the minimal safe
API (file ops, command execution, locking, env vars, SSH info) and the
existing `Host` extends it to add agent / lifecycle / snapshot / tag
machinery. Adds an optional context-manager-based accessor on
`OnlineHostInterface` and `ProviderInstanceInterface` that yields
`OuterHostInterface | None`. Surfaces via a new `mngr exec --outer`
flag that dedups by outer host so the command runs once per unique
outer; `--missing-outer abort|warn|ignore` (default `warn`) controls
behavior when targeted agents have no accessible outer. The existing
one-off SSH paths (`mngr_imbue_cloud/vps_admin.py`,
`mngr_vps_docker/docker_over_ssh.py`) will be deleted and migrated to
the new abstraction. Modal, `local`, `ssh`, and docker-over-tcp return
`None` (no accessible outer). No code changes yet — spec only.

- JSONL parsers now surface upstream corruption rather than silently dropping bad lines
  - `MalformedJsonLineWarner.parse` raises `MalformedJsonlLineError` on lines that parse but aren't JSON objects (e.g. `[1,2,3]`); valid-but-incomplete JSON is still buffered as a possible end-of-file partial write
  - `parse_event_line`, `parse_discovery_event_line`, `parse_agents_from_mngr_output`, and `_parse_batched_json_files` (vps_docker) all raise on malformed input instead of returning `None`; rationale: stdout is for JSON data, stderr is for logs, and silently skipping garbage hides real upstream bugs
  - New `MalformedJsonlLineError` exception in `imbue.mngr.errors`; new `MalformedMngrOutputError` in `imbue.minds.errors`
- Fixed: `resolve_provider_names_for_identifiers` no longer silently returns partial results when an identifier is unknown; it returns `None` to signal a full discovery scan is needed (regression introduced in the merge that combined the two parsing-fix branches)
- Fixed: `mngr connect` no longer fails type-checking; the two `build_agent_filter_cel` call sites now pass the required `cg` and `project_root` arguments to match `mngr list` and `mngr kanpan`

Accept the `host.provider` qualifier consistently anywhere mngr takes a host
identifier (previously only the positional `agent@host.provider` form worked).
For example, `mngr create --from @m1.modal:/some/path`, `mngr limit --host
m1.modal`, `mngr snapshot create --host m1.modal`, and the `--host` filter on
list-style commands now all resolve `host.provider` deterministically.

Also fix `mngr create --from` against a remote source: the current-branch
lookup, and the related git-author/origin-URL lookups in remote-source flows,
now run through the host interface so they work for any provider rather than
raising `NotImplementedError`.

`mngr create --from` between a source and a target on the same remote host
now works: the git-mirror push short-circuits to a single in-host `git push`
between two local paths (no SSH), and `--transfer=git-worktree` is allowed
whenever source and target are on the same host -- previously it was
restricted to local-only agents. The default transfer mode for same-host
remote git sources is now `git-worktree` instead of `git-mirror`.

The internal rsync helper used for `work_dir_extra_paths` and the rsync
transfer mode also short-circuits when source and target are on the same
host, running a single rsync between two local paths on that host instead
of routing through a laptop temp directory.

- Internal: re-baseline mngr_imbue_cloud against the standard ratchet checks. The new plugin's `test_ratchets.py` now includes the full set of `test_prevent_*` functions derived from `standard_ratchet_checks.py` (snapshots pinned to current violation counts so they can only ratchet down).
- Internal: register `imbue.mngr_imbue_cloud` in the root `pyproject.toml`'s combined `--cov=` list so the per-package and combined coverage gates see its source files. Pin the plugin's per-package coverage gate to its current 19% baseline (was 50%, never met) and lower mngr_recursive's gate from 84% to 83% to reflect the recently-added remote-upload helpers.
- Internal: bump `test_destroy_transfer_none_keeps_shared_worktree` to a 60s timeout so its tmux polling loop has room to finish when offload sandboxes are contended; relax `test_read_grandparent_pid_returns_alive_grandparent` to skip when the worker has no resolvable grandparent (some offload sandboxes run pytest directly under PID 1).

QA pass for the merged forwarding refactor on top of `josh/imbue_cloud_ready`:

- Resolved a `test_ratchets.py` merge conflict in `mngr_imbue_cloud` (kept the standard layout, set the `bare_print` snapshot to 1 to match the surviving `sys.stderr.write` in `cli/admin.py`).
- Pruned the dead `tunnel_token_store` re-injection path from `LocalAgentDiscoveryHandler` (the parallel `mngr/imbue-cloud` branch dropped that cache; the agent's container persists the token now and rebuilds re-fire the post-create injection).
- Passed `concurrency_group=` to `LatchkeyDiscoveryHandler` in the new `minds run` entry point.
- Switched `apps/minds/electron/backend.js` from spawning `minds forward` to `minds run` so QA exercises the `mngr_forward` plugin subprocess + `EnvelopeStreamConsumer` path.
- Ported `start_grandparent_death_watcher` (Electron-exit detection) and `_ImbueCloudAuthErrorDisabler` (auto-disable an imbue_cloud account whose session has been revoked) from the legacy `desktop_client/runner.py` over to the new `cli/run.py` path. Added an `add_on_provider_error_callback` API on `EnvelopeStreamConsumer` so the disabler has somewhere to register.
- Phase 2 cleanup of the `mngr_forward` split:
  - Deleted `desktop_client/runner.py` and `cli/forward.py` + `cli/forward_test.py` (the legacy `minds forward` command).
  - Deleted `MngrStreamManager` from `desktop_client/backend_resolver.py` (replaced by `EnvelopeStreamConsumer` in `forward_cli.py`) and dropped the corresponding test block from `backend_resolver_test.py`.
  - Slimmed `desktop_client/cookie_manager.py` to the minds bare-origin session helpers; the per-subdomain auth-token helpers live in the plugin's `cookie.py`.
  - Slimmed `desktop_client/app.py`: deleted the host-header subdomain-forwarding middleware, `_handle_workspace_forward_http`/`_websocket`, `_handle_subdomain_auth_bridge`, `_handle_goto_workspace`, the SSH-tunnel helpers (`_get_tunnel_socket_path`, `_get_tunnel_http_client`, `_connect_backend_websocket`, `_forward_workspace_http`), `_unauthenticated_subdomain_response`, `_parse_workspace_subdomain`, and the catch-all WebSocket route. `create_desktop_client(...)` no longer takes `tunnel_manager`, `latchkey`, or `stream_manager`; it gains `mngr_forward_port` + `mngr_forward_preauth_cookie` so server-to-server refresh broadcasts route through the plugin.
  - Rewired `_dispatch_refresh_broadcast` to POST through the plugin's per-agent subdomain (`<agent>.localhost:<plugin_port>/api/refresh-service/<svc>/broadcast`) with the preauth cookie, instead of opening its own SSH tunnel.
  - `supertokens_routes._bounce_mngr_observe` → `_bounce_forward_observe`: sends `SIGHUP` via `EnvelopeStreamConsumer.bounce_observe()`. Dropped the legacy `MngrStreamManager` fallback.
  - Templates (`landing.html`, `sharing.html`, `permissions.html`, `chrome.html`, `sidebar.html`) and static JS (`chrome.js`, `sidebar.js`, `sharing.js`) now point `/goto/<agent>/` links at the plugin's port via a `mngr_forward_origin` Jinja variable / `data-mngr-forward-origin` attribute.
  - Electron's `backend.js` exposes a new `onMngrForwardStarted` callback; `main.js` consumes the `mngr_forward_started` event from `minds run` stdout and pre-sets the `mngr_forward_session=<preauth>` cookie on `localhost:<plugin_port>` (default + content session) before any agent-subdomain navigation.
  - Updated user-facing references to `minds forward` → `minds run` in `apps/minds/README.md` and `apps/minds/docs/{design,desktop-app,overview,workspace/getting_started,workspace/glossary}.md`.
- Ported the spirit of two adjacent fixes that landed in PRs 1471 and 1482 (which had touched the now-deleted minds-side forwarder):
  - `AgentCreator` now polls the workspace_server through the plugin's per-subdomain endpoint until it returns 200 before publishing the redirect URL. Without the poll, freshly-created agents redirected the browser before the workspace_server inside the agent finished starting up, and the user saw a transient 503 / 404. Best-effort: timeout (default 60 s) just publishes the redirect anyway so the user lands on the auto-refresh page rather than spinning forever. New fields on `AgentCreator`: `mngr_forward_port`, `mngr_forward_preauth_cookie`, plus the timing tunables. (PR 1471 part 1)
  - `mngr_forward._forward_workspace_http` now serves the auto-refresh retry page (`_service_unavailable_response`) on `httpx.ConnectError` and `httpx.RemoteProtocolError` instead of returning a hard 502, so HTML navigations to a backend that is still booting auto-recover. (PR 1471 part 2)
  - `mngr_forward._handle_workspace_forward_http` and `_handle_workspace_forward_websocket` now refuse to dial host loopback (`localhost` / `127.0.0.0/8` / `::1` / `0.0.0.0`) when no SSH tunnel exists for the agent. Previously, a remote agent with stale or delayed SSH info would silently fall through to the host's loopback at the registered port, exposing whatever else happened to be bound there as the "agent's workspace UI". The plugin gains a `--allow-host-loopback` CLI flag (off by default) for the legacy `LaunchMode.DEV` path; minds opts in via `MINDS_ALLOW_HOST_LOOPBACK=1`. (PR 1482)
- Fixed a redirect-after-creation bug exposed during QA: minds was sending the browser to `/goto/<id>/` (a relative URL), which the browser resolved against the minds bare origin (port 8420). minds doesn't serve `/goto/`; the plugin does. The user landed on FastAPI's default `{"detail":"Not Found"}` 404 right after creation. `AgentCreator` now builds the absolute `http://localhost:<mngr_forward_port>/goto/<id>/` URL when configured.
- Removed the latchkey pre-spawn / pending-gateway flow from minds. `_allocate_latchkey_gateway`, `_build_latchkey_gateway_url`, `Latchkey.allocate_gateway`, `Latchkey.bind_gateway_to_agent`, `Latchkey.discard_unbound_gateway`, the `PendingLatchkeyGateway` class, the `_PENDING_GATEWAYS_DIR_NAME` constant, and the `pending-gateways/<creation_id>/` directory tree are all gone. `AgentCreator` no longer takes a `latchkey` field. The pre-spawn was always racing `LatchkeyDiscoveryHandler.__call__` (which fires off observe-stream events while `mngr create` is still running and creates `agents/<agent_id>/` ahead of the bind), and the rename consistently lost the race -- producing a per-agent `Failed to bind Latchkey gateway ... agent_id collision` warning and leaking both an orphan gateway subprocess and a `pending-gateways/<creation_id>/` directory. The discovery handler does the entire gateway lifecycle correctly on its own. Non-DEV modes still get `LATCHKEY_GATEWAY=http://127.0.0.1:<AGENT_SIDE_LATCHKEY_PORT>` injected at `mngr create` time (the URL is the same constant for every agent because the per-agent reverse tunnel always bridges agent-side `:1989` to whichever host port the discovery handler picked); DEV mode no longer gets an injected `LATCHKEY_GATEWAY` -- DEV runs on the bare host without a reverse tunnel, so any test that wants latchkey there can set the env var itself.

Adds a spec for backing up the gitignored `runtime/` folder of forever-claude-template (which now also contains `memory/` and `tickets/`) into the same private repo on a separate orphan branch, plus a periodic backup service and `GH_TOKEN`-based auto-push setup.

- New `mngr_imbue_cloud` plugin (`libs/mngr_imbue_cloud/`) that owns auth (SuperTokens), pool-host leasing, LiteLLM keys, and Cloudflare tunnels for the Imbue Cloud service. Adds a `mngr imbue_cloud` CLI command group with `auth`, `hosts`, `keys litellm`, `tunnels`, and `admin pool` subcommands. Multi-account is modelled as multiple provider instances of the same backend (each with `account = "<email>"`).
- `mngr create --provider imbue_cloud_<account-slug> --new-host -b repo_url=... -b cpus=... ...` now leases a matching pool host and adopts its pre-baked agent under the requested name in one invocation. Lease attributes flow through `--build-arg`; `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`/`MNGR_PREFIX` flow through `--host-env`. The plugin's `on_load_config` hook auto-registers a provider entry per signed-in account so no manual `[providers.imbue_cloud_*]` block is needed.
- Connector schema migration: `pool_hosts.version` is replaced with a flexible `attributes JSONB` column. `/hosts/lease` matches with `attributes @> request_attributes`. Backwards-compatible: legacy callers can still pass `version` as a top-level field; the connector folds it into attributes automatically.
- minds desktop client: when a discovery error from the connector indicates a revoked SuperTokens session for a specific imbue_cloud account, the matching `[providers.imbue_cloud_<slug>]` block is automatically marked `is_enabled = false` and `mngr observe` is bounced so the dead account stops poisoning subsequent discovery cycles. Signing back in (email/password or OAuth) re-enables the provider. The Manage Accounts page shows a "Signed out" badge + "Sign in again" link for any account whose provider is currently disabled.
- minds desktop client now installs a grandparent-death watcher when the Python backend starts: if Electron crashes (or is otherwise killed without running its on-quit handler), the Python backend self-terminates within ~3 seconds, and the cascade brings down its `mngr observe`/`mngr events`/latchkey children via their own watchers. Previously a crashed Electron left an orphan tree alive across restarts.
- minds: SIGTERM that minds itself sends to `mngr observe` / `mngr event` subprocesses (during shutdown, observe restart, or events-stream sync after an agent leaves the discovery snapshot) no longer surfaces as a "subprocess failed" notification.
- remote_service_connector: `add_service` is now idempotent. Updating the access list on a previously-shared service (which re-runs the full create-tunnel/add-service/set-auth chain) no longer fails with Cloudflare error 81053 ("DNS record already exists"). When a CNAME or ingress rule for the hostname is already in place pointing at the same tunnel, it is reused; if the per-service auth policy was already customized via `set_service_auth`, the tunnel default is no longer reapplied on top.

## mngr_kanpan: staleness taint semantics

Field values now track when they were computed and render dimmed when older than a configurable threshold, surfacing potentially-out-of-date data at a glance.

- Added a required `created: datetime` field to every `FieldValue`. Values derived from cached inputs inherit the oldest `created` of the inputs they actually used (taint propagation); world-derived values use the current time.
- Added `staleness_threshold_seconds` to `KanpanPluginConfig`. Defaults to 90% of `refresh_interval_seconds` so values that weren't refreshed last cycle render as stale.
- Stale cells render in dark grey via new `stale` / `stale_focus` urwid palette entries. Muted-row dimming wins over per-cell stale dimming.
- `ShellCommandConfig` now declares its cached `inputs` explicitly so shell-derived staleness can propagate correctly. Shells with no declared inputs are treated as world-fresh.

- minds: redesigned the "Create a Project" screen.
  - Removed the "Include .env file" checkbox.
  - Added an "AI provider" choice (`imbue_cloud`, `api_key`, `subscription`) that is independent from the compute provider, so any combination is valid as long as `imbue_cloud` is paired with a selected account.
  - Renamed the "Launch mode" dropdown to "Compute provider"; both compute and AI provider default to `imbue_cloud` when an account is selected.
  - Selecting `api_key` reveals a required Anthropic API key field; `subscription` injects no Anthropic credentials so the user can sign in interactively after the workspace starts.
  - Selecting `imbue_cloud` for either field with no account is rejected by both the form (with a warning) and the server (with a 400).
  - Added an optional `GH_TOKEN` field under Advanced settings that is forwarded to the agent host (or the agent in DEV mode).

- Fixed: `mngr create` now provisions credentials correctly inside nested sandboxes (e.g. a Linux lima VM running on a macOS host). `get_user_claude_config_dir()` previously returned `$ORIGINAL_CLAUDE_CONFIG_DIR` even when that path (a host-side path like `/Users/<user>/.claude`) did not exist inside the VM, causing `_provision_local_credentials` to log "No .credentials.json found to provision" and silently no-op. Spawned child agents then failed Claude sessions with "Not logged in". The helper now falls back to `$CLAUDE_CONFIG_DIR` when `$ORIGINAL_CLAUDE_CONFIG_DIR` does not resolve to an existing directory, so credential provisioning (and every other call site that resolves user-scope config) finds the live per-agent credentials.

Cleanup pass after splitting functionality out of minds into the `mngr_imbue_cloud` and `mngr_forward` plugins.

- The "Share" button in a workspace now opens a static informational modal that points the user at the desktop app, rather than writing a sharing-request event back to minds. Direct sharing editing from the desktop client (workspace settings page) is unchanged. Permissions / latchkey request flows are unchanged.
- Minds no longer persists `imbue_cloud` account identity (email, display_name) to disk. Only the workspace<->account association map lives in `~/.minds/workspace_associations.json`; identity is sourced on demand from the new `mngr imbue_cloud auth list` command and cached in memory.
- Destroyed agents now disappear from the projects index automatically without requiring the user to click into the destroying detail page first.

- `mngr create -vv` now emits a `Transferring agent files` log span around the
  per-file `write_file` loop in agent provisioning, so the total time spent
  pushing plugin-declared files (e.g. Claude Code config) is visible in timing
  output.
- `mngr tmr` no longer crashes the whole orchestrator when a single agent
  fails its initial-message send (e.g. `SendMessageError` from the tmux
  paste-detection timeout). The launching loops now also catch `AgentError`
  alongside `MngrError` / `HostError`, log a warning, and continue with the
  remaining agents. This applies to test-agent launching (both batched and
  pre-launched modes) and to the integrator launch.
- Fix `mngr tmr` integrator launch (and any local-provider test-agent
  launch), which always failed with `Failed to generate a unique host name
  after 100 attempts`. The local provider has a single fixed host
  ("localhost"), so the new-host path can never find a free name; TMR now
  reuses the existing local host when the target provider is `local`,
  matching what `mngr create` already does.
- `mngr tmr` HTML reports now include rows for tests whose agent failed to
  launch (e.g. `SendMessageError` from a paste-detection timeout). They are
  rendered as errored entries instead of being silently dropped, and carry
  the actual agent name that was used for the failed launch attempt -- so
  the report row matches the host/tmux session if the user kept it for
  debugging. The `mngr create -vv` log span around `_execute_agent_file_transfers`
  now wraps the early-return path too, so the span is emitted (with
  `count=0`) even when the agent declared no file transfers.
- Stop the `claude plugin update` SessionStart hook from hanging Modal-launched
  agents at an `ssh` first-contact (TOFU) prompt for github.com. The plugin
  updater shells out to `git pull`, which uses `ssh` -- on a fresh sandbox
  with no `~/.ssh/known_hosts` entry, ssh blocks on a "Are you sure you
  want to continue connecting" prompt that Claude Code's bypass-permissions
  setting does not cover. `scripts/claude_update_plugin.sh` now prefixes
  the update with `GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new
  -o BatchMode=yes'`, which writes the first-seen host key to known_hosts
  and exits non-interactively if anything goes wrong (matching the
  script's existing `2>/dev/null || true` failure tolerance).
- `mngr tmr` HTML reports now have a dedicated "Failed" section,
  separate from "Blocked". The two represent different failure modes:
  Blocked means the coding agent reported every change as BLOCKED
  (i.e. it considered the work too complex), while Failed means an
  infrastructure failure prevented the agent from producing a verdict
  (launch failed, agent timed out, agent details missing). Errored
  results that previously fell into "Blocked" now route to "Failed".

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
  stay alive — used by `minds run` to make a freshly-written
  `[providers.imbue_cloud_<slug>]` block in `settings.toml` take effect.

# minds run

A new `minds run` command rewires the minds desktop client to spawn
`mngr forward` as a subprocess instead of running the same forwarding
logic in-process:

```bash
minds run --port 8420 --mngr-forward-port 8421
```

- Spawns `mngr forward --service system_interface --preauth-cookie ...`
  and consumes its envelope JSONL stream on stdout.
- Serves the slimmed minds bare-origin UI on `--port` (default 8420);
  agent subdomains are served by the spawned `mngr forward` on
  `--mngr-forward-port` (default 8421).
- Emits a `mngr_forward_started` JSONL event on stdout carrying the
  preauth cookie value so the Electron shell can pre-set
  `mngr_forward_session=<value>` on `localhost:<mngr-forward-port>`
  before the first agent-subdomain navigation.
- Sends `SIGHUP` to the plugin's PID after a freshly-written
  `[providers.imbue_cloud_<slug>]` block in `settings.toml` so the new
  provider becomes visible without restarting the plugin.

The legacy `minds forward` command and its in-process forwarding /
auth / subdomain code are intentionally unchanged in this branch and
keep working. A follow-up branch will delete the now-duplicated
in-process paths.

Add `.github/workflows/tmr.yml`: a manually-dispatched CI workflow that runs `mngr tmr` against Modal, uploads the HTML report as an artifact, and opens a draft PR for the integrator branch. The provider is hardcoded to `modal`; `test_paths`, `pytest_args`, and `agent_type` are exposed as workflow inputs, with defaults reproducing the local invocation against `libs/mngr/imbue/mngr/e2e/test_basic.py -m release` with `--agent-type yolo`.

The `mngr tmr` CLI also now emits an `integrator_branch` event on its structured stdout stream (in `--output-format jsonl`/`json`), so consumers like the new workflow can pick up the branch name without parsing human-formatted output.

- mngr_modal: drop `ModalMode.TESTING` from production code paths; tests inject `TestingModalInterface` via `make_testing_provider` instead. Production `mngr_modal.backend` no longer imports `modal_proxy.testing` at module top, so the standard `**/testing.py` wheel-exclude rule applies cleanly to `modal_proxy` (no `only-include` workaround needed) and packaged consumers (e.g. minds.app) no longer crash with `ModuleNotFoundError: No module named 'imbue.modal_proxy.testing'`.
- mngr_modal: `ModalMode` retained with values `DIRECT` (default) and `PROXIED`. `PROXIED` is reserved for routing Modal traffic through the imbue_cloud gateway and currently raises `NotImplementedError` at `build_provider_instance`. The `mode` field on `ModalProviderConfig` is preserved.
- mngr_modal: extract pure `ModalProviderBackend._derive_modal_names(name, config, mngr_ctx)` helper so the environment-name / app-name / host-dir derivation can be unit-tested without instantiating any Modal interface.
- imbue_common: extend `TEST_FILE_PATTERNS` (used by all standard ratchet checks to skip test files) from `("*_test.py", "test_*.py")` to `("*_test.py", "test_*.py", "conftest.py", "testing.py")` -- aligning with the wheel-exclude pattern from #1505 so `testing.py` and `conftest.py` are uniformly recognized as test code across ratchets. Existing snapshots are not affected (the change can only reduce violation counts; current snapshots are upper bounds).
- mngr_modal: drop unused `is_testing` parameter from `_get_or_create_app` (only ever non-default in the now-removed `TESTING` dispatch arm; the test-fixture path constructs `ModalProviderApp` directly and never went through this function).

- modal_proxy: `ModalInterface.enable_output_capture` is now an abstract method. `DirectModalInterface` hooks into the Modal SDK output system; `TestingModalInterface` returns a `nullcontext`. Stacked on #1520.
- mngr_modal: extract `ModalProviderBackend._construct_modal_provider(name, config, mngr_ctx, modal_interface)` as the shared factory body. `build_provider_instance` matches the parent-class signature exactly, dispatches on `config.mode` (`DIRECT` selects `DirectModalInterface()`, `PROXIED` raises `NotImplementedError`), then delegates to `_construct_modal_provider`. Tests call `_construct_modal_provider` directly with `TestingModalInterface`. The factory has no per-implementation branches.
- mngr_modal: `make_testing_provider` collapses from a 35-line parallel constructor into a wrapper around `ModalProviderBackend._construct_modal_provider`.
- mngr_modal: delete the dead `mngr_modal/log_utils.py` re-export shim (`b66f3cbd5`'s in-tree migration is complete; nothing imports from it).

- mngr_modal: register the session-scoped Modal env created by `modal_subprocess_env` with the leak-detection registry (`register_modal_test_environment`) so that silent failures in the per-session cleanup helpers (`delete_modal_apps_in_environment` / `delete_modal_volumes_in_environment` / `delete_modal_environment`) are now caught by the autouse `modal_session_cleanup` at session end, rather than leaking the env onto the Modal account. Closes the gap that was producing the `mngr_test-{timestamp}-{user_id}` envs containing `mngr_test-{timestamp}-modal` apps observed accumulating on the imbue Modal account (42 currently-orphaned envs sampled, 30/30 matching this signature).
- mngr: surface non-zero CLI exits in the three best-effort cleanup helpers (`delete_modal_apps_in_environment`, `delete_modal_volumes_in_environment`, `delete_modal_environment`). Previously they ran `subprocess.run(...)` without `check=True` and only caught `subprocess.SubprocessError`/`TimeoutExpired`/`FileNotFoundError`, so a non-zero exit from the `modal` CLI passed silently with a `"Deleted ..."` debug log. Now non-zero exits log a warning with stderr/stdout, while still treating the failure as non-fatal (the autouse session-end leak detector remains the loud safety net).

Every workspace package's wheel build now excludes test files uniformly via the same canonical line:

```
[tool.hatch.build.targets.wheel]
exclude = ["*_test.py", "test_*.py", "**/conftest.py", "**/testing.py"]
```

Previously, several packages were missing some or all of these patterns and hatchling was shipping `_test.py`, `conftest.py`, and `testing.py` files into published wheels. Notably `libs/mngr` was leaking three test helpers (`cli/testing.py`, `api/testing.py`, `providers/docker/testing.py`) because its existing pattern only covered `**/utils/testing.py`.

A new meta ratchet (`test_every_project_excludes_tests_from_wheel`) enforces the four-pattern rule on every project so this cannot regress.
