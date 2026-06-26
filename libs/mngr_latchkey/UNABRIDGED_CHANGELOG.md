# Unabridged Changelog - mngr_latchkey

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_latchkey/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-25

Fixed repeated macOS system keychain access dialogs (mentioning Latchkey) that appeared during normal Minds use. The detached `latchkey ensure-browser` subprocess was spawned without `LATCHKEY_ENCRYPTION_KEY`, so Latchkey's startup key-resolution fell through to the system keychain on every spawn. The per-directory encryption key is now injected into that subprocess's environment, matching the other Latchkey invocations.

Bump the pinned latchkey CLI version installed on remote VPS environments (the secondary gateway) to 2.19.1.

Added: the permission-request gateway (`POST /permission-requests`) now validates `agent_id` against the canonical `AgentId` format (`agent-` followed by 32 hex characters) and rejects a malformed value with HTTP 400 before persisting it. An agent that supplies a bad `agent_id` (e.g. a hand-crafted body with a placeholder like `ENV_AGENT`) is now notified of the failure at its request, rather than silently filing an unusable request that later crashed the desktop client's permission-requests consumer. The JS check mirrors Python's `imbue.imbue_common.ids.RandomId._validate`; a cross-language test posts a freshly generated `AgentId` to guard against the two definitions drifting apart.

## 2026-06-23

`LatchkeyForwardSupervisor` (and the underlying `spawn_detached_mngr_latchkey_forward`) now accept a `cwd` argument, so embedders can launch the detached `mngr latchkey forward` supervisor from a chosen working directory (e.g. `$HOME`) instead of inheriting the caller's cwd. This keeps the supervisor's `mngr` children from resolving project config out of a transient working directory.

## 2026-06-22

Bump the pinned latchkey CLI version installed on remote VPS environments (the secondary gateway) to 2.17.2.

## 2026-06-19

Removed the now-vestigial `LatchkeyPluginConfig.merge_with` override; the config merge is routed through the overlay pipeline, which reproduces the same assign-by-default semantics. No user-visible behavior change.

Reorganized the README: high-level user info (CLI, wiring an agent, settings, logs, permissions overview) is up top, and the deeper detail (the full gateway HTTP-extension reference and the Python embedding API) now lives in a clearly-marked Reference section below.

Bump the latchkey CLI to 2.17.1:

- The pinned version installed on remote VPS environments (the secondary gateway used by agents when the user's computer is down) is now 2.17.1.

- The minimum latchkey CLI version the wrapper will operate against is now 2.17.1, so initializing the gateway rejects any installed CLI older than 2.17.1.

## 2026-06-17

- Avoid `logger.warning()`.
- Replace `logger.debug()` with `logger.info()` to prevent logs from growing needlessly.

Fixed a bug where applying a latchkey permission grant could fail with a 500 (`ENOENT ... latchkey_permissions.json.tmp.<hex>`) when the per-host directory did not exist yet. The gateway's `permissions` extension (`POST /permissions/rules`) now creates the target file's parent directories (e.g. `hosts/<host_id>/`) before writing, matching its documented "creates the target file if it does not yet exist" behavior.

Added `maybe_recover_host_permissions_for_agent` to `agent_setup`: a best-effort repair that, given an agent's opaque permissions handle, host id, and agent id, materializes the canonical per-host permissions file (and points the opaque handle's symlink at it, recreating the handle if it had gone missing) when that file is missing, and idempotently re-registers the agent in the host's `minds-api-proxy` allowlist (closing the gap where discovery-time auto-register skipped the agent because the host file did not exist yet). Cheap in the common case (the canonical file already exists). Used by minds to self-heal hosts whose agent-creation finalize/link step was skipped or failed.

Added `point_opaque_handle_at_host` to `store`: (re)creates an opaque permissions handle as a symlink to the canonical host file without moving anything (the symlink-only tail of `link_opaque_permissions_to_host`, now shared between the two).

## 2026-06-16

Exposed the catch-all permission name as a public `WILDCARD_PERMISSION_NAME` constant (still `any`) so the minds permission dialog can present it to users as `all` while keeping the stored/granted value unchanged.

## 2026-06-15

`mngr latchkey forward` now has a structured, rotated, timestamped log, reusing the standard mngr/minds JSONL logging rather than the previous unrotated, untimestamped files.

- The supervisor now writes its structured log to `<latchkey_directory>/mngr_latchkey/events.jsonl` (one flat JSON object per line with a nanosecond timestamp, size-rotated with rotated copies pruned). Read this when you need to observe timing.

- The shared `latchkey gateway` subprocess's output is now routed through loguru (each line at DEBUG, prefixed with `[latchkey gateway]`) into that same structured log, so it is timestamped and rotated like the rest of the logs instead of accumulating in the separate, unrotated `latchkey_gateway.log`. That separate file is no longer written.

- The detached supervisor is now spawned with `--quiet`, so its raw `latchkey_forward.log` capture file no longer accumulates console output in steady state (everything goes to the structured `events.jsonl`). The raw file stays effectively empty and only ever captures rare startup-failure output (Click errors or a pre-logging traceback), which is exactly when you want it. Its fd is handed straight to the detached process, so it cannot be rotated mid-write -- keeping it near-empty is what bounds it.

## 2026-06-12

Fixed file-sharing permission grants for paths containing spaces or non-ASCII characters (e.g. an agent-requested or user-selected directory like `My Documents`). The per-file permission pattern is now built from the same WHATWG-URL-normalized (percent-encoded) form that the gateway matches incoming requests against, so a path with a space (`%20`) or accented letter now matches instead of silently never granting access.

File-sharing permission requests now accept paths that use `~` / `~/...` notation for the current user's home directory; the gateway expands them to an absolute path (the same root the WebDAV home mount is served from) before storing the grant. `~user` notation for another user's home is rejected with a clear error.

Fixed the gateway permissions extension so that services whose catalog lists no specific permissions (e.g. Linear) now surface the catch-all `any` permission. `GET /permissions/available/<service>` injects `any` as the first available permission for every scope, and `POST /permission-requests` accepts a `predefined` request naming `any` under any known scope. Previously such services appeared to have no permissions an agent could request.

## 2026-06-11

Hardened how the VPS-resident latchkey gateway receives its secrets. The encryption key and gateway listen password are no longer interpolated into the gateway start command (where they could surface in process listings or command logs). Instead they are written to short-lived 0600 files on the VPS that the start script reads into the gateway's environment and deletes immediately. This keeps the secrets out of process argv and logs, and -- importantly -- avoids leaving the encryption key on the VPS disk next to the encrypted credential store it decrypts. These temp files now also use random, non-descriptive names so they do not advertise which secret each one holds to anyone able to list the directory.

Decoupled per-agent latchkey gateway setup so a failure to reverse-tunnel the desktop-side gateway into an agent's container no longer prevents that agent's VPS-resident gateway from being provisioned (and vice versa). The two reachability paths are independent, so each is now attempted with its own error handling.

Coalesced VPS-resident gateway provisioning per outer host: when several agents share one outer host (VPS/container), only one provisioning pass runs at a time instead of multiple agents racing concurrent, redundant passes against the same host's gateway, tunnel, and credential/permission files.

Stopped re-provisioning an already-provisioned outer host on every discovery cycle. The discovery stream re-emits the full agent set continuously; previously each emission re-ran the full (expensive, idempotent) VPS gateway provisioning, flooding the log and the network with redundant SSH work. Each host is now provisioned at most once per supervisor lifetime (a failed pass still retries, and a supervisor restart re-provisions); ongoing credential/permission sync continues to be handled by the remote-state watcher.

The VPS-resident latchkey gateway now starts with the same shared password the
local desktop gateway uses. The desktop-derived gateway password (a pure
function of the shared Latchkey encryption key, as produced by
`Latchkey.derive_gateway_password`) is injected into the remote gateway as
`LATCHKEY_GATEWAY_LISTEN_PASSWORD`, matching the `LATCHKEY_GATEWAY_PASSWORD`
agents already present. Previously the remote gateway started without any
listen password, so it did not enforce the same authentication as the local
gateway.

Remote VPS gateways now receive only the latchkey credentials a host's permissions actually grant, instead of the full desktop credential store.

When syncing credentials to a remote VPS, mngr resolves the canonical services a host has been granted (mapping its permissions-rule scopes back to service names via the bundled `services.json` catalog, accessed through the new `services_catalog` module), drops any whose credentials are not actually stored (checked with `latchkey services info --offline`), and re-encrypts a host-scoped subset with the same encryption key via `latchkey auth re-encrypt --services`. The encryption key is unchanged, so the gateway's derived password and the agents' permissions-override JWTs keep validating. When nothing is left to ship (a deny-all host, or every granted service lacks stored credentials) the remote store is cleared instead. This limits the blast radius of a VPS compromise to the credentials the agent was actually permitted to use.

The `services_catalog` module also now owns the dialog-facing catalog (`ServicesCatalog` / `ServicePermissionInfo`), previously in the desktop client. It reads the bundled `services.json` directly rather than over HTTP, so the gateway's `permissions` extension no longer serves the bare `GET /permissions/available` collection endpoint (the per-service `GET /permissions/available/<service>` endpoint that agents use is unchanged).

Replaced a direct RuntimeError raise in the discovery stream consumer with a dedicated DiscoveryStreamError.

## 2026-06-09

Regenerated the latchkey `services.json` permission catalog from detent 1.5.0.
This adds the new `notion-mcp` service (Notion's hosted MCP endpoint at
`mcp.notion.com`, scope `notion-mcp-api`, displayed as "Notion (MCP)") with its
20 grantable permissions, and refreshes the Slack `slack-read-all` /
`slack-write-all` descriptions to match detent's updated wording. The catalog
generator (`scripts/generate_services_json.py`) gained curated display-name and
service-order entries for `notion-mcp`.

The VPS-resident latchkey gateway is now launched with
`LATCHKEY_DISABLE_CREDENTIALS_REFRESH=1`. The remote gateway runs on a synced
copy of the user's credentials, so disabling refresh there prevents it from
racing the desktop-side latchkey to rotate the same OAuth refresh token (which
would exhaust the user's token and invalidate the desktop's credentials). The
desktop-side latchkey remains the single owner of credential refresh.

The `permission-requests` gateway extension's approve endpoint
(`POST /permission-requests/approve/<id>`) now accepts an optional JSON body
carrying a single `path` field. When present, and only for `file-sharing`
requests, the file-sharing effect is recomputed for that path instead of using
the one precomputed at request-creation time -- this lets the Minds desktop
client honor a user who edited the shared path in the approval dialog. The
overridden path is re-validated with the same traversal-rejection rules used at
request creation, the access mode fixed at creation time is preserved (a path
override cannot escalate read-only to read-write), and only the `path` field is
accepted in the body. An empty or `null` body preserves the previous behavior
(apply the precomputed effect verbatim).

File-sharing requests are now validated to be within one of the Minds WebDAV
mount roots -- the user's home directory or the system temp directory -- at
request-creation time (and at approve time for a user-edited path override).
A grant for any path outside those roots is inert (the WebDAV server has no
provider for it and answers 404), so rejecting it up front gives the agent a
clear "must be within a shared root" error instead of an approve-then-404 dead
end. The roots are derived from the gateway process's `homedir()` / `tmpdir()`,
which match the `Path.home()` / `tempfile.gettempdir()` roots the Minds WebDAV
server serves on the desktop host. The comparison is case-insensitive (mirroring
the WebDAV share-prefix matching) and purely lexical (no symlink resolution or
existence check).

## 2026-06-08

- Now auto-discovered as a publishable package by the release tooling (it is a standalone `mngr latchkey` plugin). It will be offered for first publication to PyPI on the next release. Its stale `imbue-common==0.1.17` and `concurrency-group==0.1.17` pins are realigned to the current `0.1.18`. No runtime change.

## 2026-06-06

Added `libs/mngr_latchkey/imbue/mngr_latchkey/remote_gateway.py`, the first piece of "run the latchkey gateway on the VPS" support. It declares a pinned `LATCHKEY_VERSION` (2.15.1) and a small public surface for standing up a latchkey gateway on a remote VPS (the agent's outer host):

- `sync_credentials(host, latchkey_directory)` copies the local encrypted credential store (`<latchkey_directory>/credentials.json.enc`) to `~/.latchkey/` on the VPS so the remote latchkey CLI can decrypt the same credentials.
- `sync_permissions(host, latchkey_directory, host_id)` copies the per-host permissions file (`<latchkey_directory>/mngr_latchkey/hosts/<host_id>/latchkey_permissions.json`) to `~/.latchkey/permissions.json` on the VPS, falling back to the restrictive deny-all default when the host has no local permissions file.

Both syncs write atomically (to a sibling `.tmp` file, then `mv` into place), so the remote gateway never reads a half-written credentials or permissions file mid-sync.
- `provision_remote_gateway(host, host_id, container_ssh_user, container_ssh_port)` is the orchestrator (its internal steps are private helpers). It: installs the upstream `latchkey` CLI and its prerequisites (curl, Node.js via NodeSource, `latchkey@<version>` via `npm install -g`); starts `latchkey gateway` detached, bound to the VPS loopback on `OUTER_PORT` (`LATCHKEY_DISABLE_COUNTING=1`, and `LATCHKEY_ENCRYPTION_KEY` interpolated from the local `<latchkey_directory>/encryption_key` so the VPS gateway can decrypt the synced `credentials.json.enc`), unless one is already running; locates the agent's container on the VPS by its `com.imbue.mngr.host-id` label; mints an ad-hoc ed25519 keypair and authorizes it in the container via `docker exec` (the VPS owns the docker daemon, so no pre-existing SSH access is needed); and opens a reverse SSH tunnel from the VPS into the container (`-R 127.0.0.1:INNER_PORT:127.0.0.1:OUTER_PORT`) so the agent reaches the VPS gateway via its unchanged `LATCHKEY_GATEWAY=http://127.0.0.1:INNER_PORT`. It is a no-op when the outer host is the local machine (e.g. the outer of a local docker daemon), so latchkey is never installed or run on the user's own computer -- only genuinely-remote outers are provisioned. The install runs as a single POSIX-sh command (no bash-only `pipefail`). The detached gateway and tunnel are each launched under `nohup` and made idempotent via a PID file (`$HOME/.latchkey/{gateway,tunnel}.pid`) checked with `kill -0` plus a `/proc/<pid>/cmdline` marker -- not `pgrep -f`, which would self-match the shell running the launch script (its argv contains the launch command) and so could either never start the gateway or spuriously restart the tunnel.
- `LatchkeyDiscoveryHandler` now takes an `MngrContext`. On agent discovery, every SSH-reachable agent gets the desktop-side gateway reverse-tunneled onto its `127.0.0.1:AGENT_SIDE_LATCHKEY_PORT` (run inline; this is the only path for local agents). Agents whose host *also* has an accessible outer host (the VPS -- decided by a cheap, connection-free `outer_host_id_for` check) additionally get the heavy VPS-resident gateway provisioning thrown onto its own fire-and-forget concurrency-group thread, reverse-tunneled onto a distinct `127.0.0.1:INNER_PORT`, so a VPS agent can reach both the desktop gateway and the VPS gateway at once. The provisioning thread is unchecked so one agent's failure can't tear down the shared supervisor, but the group's `ObservableThread` logs any uncaught failure at error level so it is never silently missed, and a later discovery fire retries idempotently. The discovery callback now carries the host id.
- `INNER_PORT` is now `AGENT_SIDE_LATCHKEY_PORT + 1` (not 1989), so the VPS gateway's in-container reverse-tunnel port does not collide with the desktop gateway's in-container port.
- `LatchkeyDiscoveryHandler.start_remote_state_sync(concurrency_group)` keeps every known remote (VPS) host in sync with the desktop's latchkey state. It first syncs each currently-known remote host's permissions and then credentials (permissions first), and a newly-provisioned host gets the same initial sync inline (reusing the provisioning SSH connection). It then uses a `watchdog` observer to react to changes: a change to the local credentials file pushes credentials to every known remote host, and a change to a host's permissions file pushes that host's permissions. The observer is stopped when the supervisor shuts down; a host that no longer exists is dropped from the set. The observer's health is supervised on a *checked* concurrency-group strand: if it dies for any reason other than shutdown, that surfaces as a loud failure (the strand raises, the group surfaces it, and the supervisor is signalled to tear down) rather than silently leaving remote agents with stale credentials/permissions. Wired into `mngr latchkey forward`. Adds a `watchdog>=4.0` dependency.
- `prepare_agent_latchkey(..., is_tunneled=True)` now also injects `LATCHKEY_GATEWAY_SECONDARY` into the agent's host env: the agent's URL for the per-VPS gateway as seen from inside the workspace container (`http://127.0.0.1:<INNER_PORT>`, where the discovery handler reverse-tunnels the VPS-resident gateway). It is set for all tunneled agents (the endpoint is only live on genuinely-remote VPS-backed hosts; the URL is the agent's view either way) and omitted for on-host/DEV agents. This flows automatically to both `mngr latchkey create-agent-env` (CLI) and the minds desktop client (which lifts every `latchkey_env` entry into a `--host-env` flag).
- Adds `INNER_PORT`/`OUTER_PORT` constants: `INNER_PORT` is the in-container port on which the VPS gateway is reached (distinct from the desktop gateway's `AGENT_SIDE_LATCHKEY_PORT`) and `OUTER_PORT` is the gateway's VPS-loopback bind port (the tunnel's forward target).

## 2026-06-05

- Added to the release tooling's publish graph (`scripts/utils.py`). It will be offered for first publication to PyPI on the next release. Its stale `imbue-common==0.1.17` / `concurrency-group==0.1.17` pins are realigned to the current `0.1.18`. No runtime change.

## 2026-06-04

`mngr latchkey forward`'s discovery observer now writes to the standard discovery event log instead of a private per-env `discovery-observe` directory. It is the single discovery observer for the host dir (minds' `mngr forward --observe-via-file` tails the same log), so the previous isolation onto a separate event log -- needed only when two observers ran at once -- is no longer required. Old `discovery-observe/` directories left by prior versions are inert and can be deleted manually.

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-04

`mngr latchkey forward` now refreshes its provider set on SIGHUP instead of shutting down. SIGHUP bounces only the `mngr observe` child (the shared gateway and all reverse tunnels stay up); SIGINT/SIGTERM remain the shutdown signals. Its discovery consumer also retains agents whose provider errored on a poll rather than tearing down their reverse tunnels, dropping them only on an explicit destroy or a later successful poll. `LatchkeyForwardSupervisor` gained a `bounce()` method that SIGHUPs a live supervisor (or starts one if none is running) so embedders can refresh latchkey's provider set mid-session.

## 2026-06-03

The latchkey forward's discovery observer (`mngr observe --discovery-only`) now writes its event log to a private, per-environment directory under the latchkey plugin data dir instead of the shared mngr discovery log. This fixes workspaces flickering out of (and never reliably appearing in) the desktop UI: when latchkey's observer and another forward's observer shared one discovery log, a snapshot from one observer that didn't have the imbue_cloud provider registered (e.g. it started before the account was written to the profile) was tailed by the other observer and treated as authoritative "no hosts," repeatedly dropping live workspaces. The private log is cleared on forward (re)start so a prior run's stale snapshots aren't replayed.

## 2026-06-02

Internal refactor with no user-visible behavior change. Updated the JSON output call sites to use the renamed `write_json_line` helper from `imbue.mngr.cli.output_helpers` (formerly `emit_final_json`, now removed).

Added `libs/mngr_latchkey/scripts/generate_services_json.py`, a developer tool that regenerates the bundled `services.json` permission catalog from a detent checkout's built-in request schemas. It classifies each schema as a scope or a permission (mirroring detent's own doc generator, including the AWS special case), groups permissions under their owning scope, and carries over detent's per-schema `$comment` summaries.

Regenerated `services.json` against the current detent. Each scope entry now carries a `description` (detent's `$comment` for the scope), and `permissions` changed from a list of strings to a list of `{"name", "description"}` objects so each permission's summary is colocated with its name. The refresh also picks up detent's newer definitions: Slack gains `slack-auth-read`/`slack-auth-write`, and GitLab now exposes a separate `gitlab-git` scope (alongside `gitlab-api`), matching how GitHub is split.

The `permissions` gateway extension now documents and validates the new scope-level `description` and the `{name, description}` permission objects. The `GET /permissions/available` and `GET /permissions/available/<service_name>` endpoints surface the scope and per-permission descriptions (their documented contract was updated to match), covered by new end-to-end tests that drive the extension over HTTP.

The `permission_requests` gateway extension now validates the `scope` and `permissions` of incoming `predefined` POST `/permission-requests` bodies against the bundled `services.json` catalog. A request whose `scope` is not a known Detent scope, or whose `permissions` list contains entries that the catalog does not list under that scope, is rejected with HTTP 400 at creation time rather than persisted as a pending request that approval would happily splice into `permissions.json`. File-sharing requests are unaffected.

The two Node-driven extension tests (`minds_api_proxy_test.py`, `permission_requests_test.py`) no longer silently skip in CI. They spawn a Node child process to drive the `.mjs` gateway extensions and carried `skipif(shutil.which("node") is None)`, which skipped silently on the Node-less offload image -- so they exercised nothing. With Node now installed in the shared mngr image, the `skipif` is removed: they run on offload and assert Node is present (a missing Node fails loudly rather than skipping).

- pyproject.toml: align `imbue-mngr*==` pin stragglers with the satellites bumped in main's `e22e7010e` release commit. Several `imbue-mngr-*` libs still pinned to older versions even though `libs/mngr` had moved to 0.2.10; building the apps/minds ToDesktop bundle from main today would fail at `uv lock` in `apps/minds/scripts/build.js` because the workspace constraint graph is unsatisfiable. Day-to-day dev hides this because `[tool.uv.sources]` redirects every `imbue-mngr-*` to its workspace path, bypassing the `==` pin.

## 2026-06-01

Bump Latchkey to version 2.14.0 to support GitHub git operations via Latchkey gateway.

Changed the `services.json` catalog (and the `permissions` gateway extension that reads it) so each raw service name now maps to a *list* of scope entries instead of a single entry. This lets one service expose more than one detent scope. The `GET /permissions/available` and `GET /permissions/available/<service_name>` endpoints now return arrays of `{scope, display_name, permissions}` objects per service.

## 2026-05-28

- `Latchkey.auth_browser` now transparently recovers from latchkey's "Service `<name>` requires preparation first" error: when it sees that message it runs `latchkey auth browser-prepare <service>` and then retries `latchkey auth browser <service>` once, so callers (e.g. minds' predefined-permission grant flow) succeed on the first user-visible attempt instead of failing with a confusing error. Failures of either the prepare step or the retry are surfaced as the usual `(False, message)` result.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-26

# Minds-api-proxy: authorization injection + schema editing + baseline notification grant

The `minds-api-proxy` gateway extension now authenticates the
forwarded request *to* the upstream Minds API on the agent's behalf:

- It reads `LATCHKEY_EXTENSION_MINDS_API_KEY` on every request and,
  when set, overwrites the inbound `Authorization` header with
  `Bearer <LATCHKEY_EXTENSION_MINDS_API_KEY>` before forwarding.
  Agents therefore never see the key and cannot spoof one. With the
  env var unset, the inbound `Authorization` header is forwarded
  unchanged (used by tests).

The `permissions` extension grew matching CRUD for inline detent
schemas alongside its existing rule editor:

- `POST /permissions/schemas?path=<file>&schema_name=<name>` adds or
  replaces an inline schema. The body is a JSON object (the schema
  definition). Schema names must match the conservative pattern
  `^[A-Za-z0-9][A-Za-z0-9._-]*$` so they round-trip safely through
  URL path segments and detent's name lookup.
- `DELETE /permissions/schemas?path=<file>&schema_name=<name>` removes
  the named schema.

These let minds install per-agent path-pattern schemas (`"only
`/minds-api-proxy/api/v1/agents/<agent_id>/...`"`) at agent-creation
time without having to direct-write the per-host permissions file
itself.

The agent baseline (`_AGENT_BASELINE_PERMISSIONS` in
`mngr_latchkey/agent_setup.py`) now ships an extra permission schema
out of the box: every minds-created agent can
`POST /minds-api-proxy/api/v1/agents/<...>/notifications`. New
helpers expose the per-agent scope / permission names + inline
schemas that the desktop client adds for each agent on top of the
baseline:

- `agent_minds_api_proxy_scope_name(agent_id)`
- `agent_minds_api_proxy_permission_name(agent_id)`
- `build_agent_minds_api_proxy_schemas(agent_id)`

`mngr_imbue_cloud/host.py`'s `build_combined_inject_command` /
`normalize_inject_args` (and the helpers only they called) are
gone entirely: there is exactly one `MINDS_API_KEY` per minds
installation now, the latchkey gateway injects it transparently, and
agents never see the value -- so there was nothing left to push
down onto a leased pool host, and the functions had no caller in
the monorepo outside their own tests.

## Narrower interface for per-agent Minds API proxy permissions

The per-agent `minds-api-proxy` permissioning model has been simplified.
Instead of installing a per-agent scope schema + per-agent permission
schema + per-agent rule at agent creation time (via the low-level
`POST /permissions/schemas` extension endpoint), the baseline
permissions file carries two cooperating rules + a plain JSON list of
allowed agent ids. To allow a new agent, the desktop client (or an
operator running the CLI) just appends an entry to that list.

Concretely:

- The baseline now has **two** rules, in this exact order:
  1. `{minds-api-proxy-unauthorized: []}` -- scope matches any
     `/minds-api-proxy/api/v1/agents/<id>/...` request whose `<id>`
     is NOT in the allowed list (encoded as `not + anyOf` on the
     path schema). The empty permission list rejects the request
     immediately; detent stops at the first matching scope, so the
     rule below never gets a chance to allow it.
  2. `{latchkey-self: [...gateway-self baseline..., minds-api-proxy]}`
     -- the existing gateway-self rule, extended with a *generic*
     `minds-api-proxy` permission that matches any path under the
     proxy's `/agents/<id>/` subtree without enumerating ids.
     Authorized agents (those past Rule 1's `not + anyOf`) hit this
     rule and are let through by the generic permission.
- The source-of-truth list of allowed agent ids is a plain JSON
  `anyOf` array inside Rule 1's scope schema -- one entry per allowed
  agent of the form `{"pattern": "^/minds-api-proxy/api/v1/agents/<id>(/.*)?$"}`.
  No regex alternation parsing/building; reading the list is iteration,
  appending is `list.append`.
- New library helper: `imbue.mngr_latchkey.agent_setup.register_agent_for_host(plugin_data_dir, host_id, agent_id)`.
  Reads the host's permissions file (or starts from the baseline if it
  doesn't yet exist), extracts the existing allowed-agent list out of
  the `anyOf` block, appends a new entry if not already there, and
  writes back atomically. Idempotent.
- New CLI: `mngr latchkey register-agent --host-id ID --agent-id ID`
  wraps the helper for operators. Documented in the README's "Wiring a
  new agent using the CLI interface" section.
- `imbue.mngr_latchkey.store.load_permissions` is the new public
  reader that `register_agent_for_host` uses; symmetric with `save_permissions`.
- The shared `minds-api-proxy-notifications` baseline grant from the
  earliest design in this branch is gone entirely; notifications are
  reached via the same allowed-agent list as every other
  `/api/v1/agents/<id>/` endpoint.
- The per-agent helpers `agent_minds_api_proxy_scope_name`,
  `agent_minds_api_proxy_permission_name`, and
  `build_agent_minds_api_proxy_schemas` are gone -- nobody needs to
  mint a per-agent schema name anymore.
- The `POST /permissions/schemas` and `DELETE /permissions/schemas`
  endpoints I added to `permissions.mjs` in an earlier round of this
  branch are gone. The user-facing interface for granting Minds API
  access is now "add the agent id to the host's allowed-agent list"
  (via the helper or the CLI), not "install arbitrary inline schemas".

A permissions file whose `anyOf` block has been hand-edited into a
shape the parser doesn't recognize is left alone (the helper raises
`LatchkeyStoreError` rather than rebuild from scratch), so operators
who customize the file by hand won't lose their edits silently.

## Consolidation: shared `SSHTunnelManager`

The `SSHTunnelManager` (and `RemoteSSHInfo`, `ReverseTunnelInfo`,
`SSHTunnelError`) used to exist in two places: this package's own
`mngr_latchkey/ssh_tunnel.py` (driving the latchkey gateway's
reverse-into-each-agent tunnels) and the `mngr_forward` plugin's
`mngr_forward/ssh_tunnel.py` (driving forward + `--reverse` tunnels).
The two implementations were ~70% verbatim duplicates that diverged on
three things: latchkey added a per-tunnel exponential backoff for the
repair loop (capped at 5 minutes), an `agent_id` tag on each
`ReverseTunnelInfo`, and a `remove_reverse_tunnels_for_agent` cleanup
hook used by the destruction path.

All three latchkey improvements moved into the `mngr_forward` manager
(they're strictly better behavior for both callers), and
`mngr_latchkey/ssh_tunnel.py` is gone:

- `mngr_latchkey/discovery.py`, `cli.py`, `discovery_stream.py`,
  `discovery_stream_test.py`, and `core_test.py` now import
  `RemoteSSHInfo`, `SSHTunnelError`, `SSHTunnelManager` from
  `imbue.mngr_forward.ssh_tunnel` instead.
- The 635-line `mngr_latchkey/ssh_tunnel_test.py` has been
  consolidated into `mngr_forward/ssh_tunnel_test.py` (which now
  carries the previously-thin manager unit tests plus the new
  exponential-backoff + `remove_reverse_tunnels_for_agent` coverage).
- The reverse-tunnel repair loop in `mngr_forward` no longer uses a
  flat 30s retry; it uses per-tunnel exponential backoff with a 5min
  cap. Same recovery latency for healthy targets; much less wasted
  paramiko handshake against permanently-gone ones.
- `remove_reverse_tunnels_for_agent` is careful not to close an SSH
  client out from under any live *forward* tunnel using the same
  host, so the two flavors of tunnel can coexist on one connection.

- Internal: simplified `mngr_latchkey.store.LatchkeyPermissionsConfig` save/load to rely on Pydantic's built-in JSON serialization and validation instead of hand-rolled JSON parsing. The on-disk file format is unchanged; the model now uses `extra="ignore"` so unknown top-level keys (e.g. detent's `include`) continue to be silently dropped on load and not re-emitted on the next save.

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-25

### permission-requests extension: preserve symlinks at the approval target

`POST /permission-requests/approve/<id>` previously replaced the
target `permissions.json` with a regular file when the target path
was a symlink. This broke the per-agent opaque symlinks that
`mngr latchkey link-permissions` swings into the canonical host
permissions file: subsequent agents sharing the canonical file
silently desynced from the granted permissions.

The atomic-write helper now `lstat`s the target and, if it is a
symlink, resolves it via `realpath` before computing the temp path
and renaming. The atomic swap lands on the underlying file and the
symlink stays in place. Approving against a non-symlink target,
or a target that does not yet exist, is unchanged.

`permissions.mjs` was already safe: its write paths resolve symlinks
up front via `resolvePathParamUnderRoot`.

## 2026-05-22

- Latchkey gateway ships a new bundled `minds-api-proxy` extension that
  transparently reverse-proxies requests under
  `/minds-api-proxy` to the minds desktop client's bare-origin
  "Minds API". The upstream URL is read at request time from the
  `LATCHKEY_EXTENSION_MINDS_API_URL` environment variable, and is
  published to the detached `mngr latchkey forward` supervisor (via the
  new `LatchkeyForwardSupervisor.extra_env`) on every `minds run`
  startup, so the proxy always points at the live Minds API port even
  when minds re-binds on restart. The extension responds 503 when the
  env var is not configured; requests still go through the gateway's
  normal permission check.
- The Latchkey gateway's `permission-requests` extension grows a typed
  request schema and a new approve endpoint:
  - `POST /permission-requests` now takes
    `{agent_id, rationale, type, payload}` instead of the legacy flat
    `{scope, permissions, ...}` shape. The `type` field is
    `"predefined"` (payload `{scope, permissions}`) or `"file-sharing"`
    (payload `{path}`, absolute-only, no `..` segments).
  - Each pending request is persisted with the additional `target`
    (the extension's per-request `permissionsConfigPath`) and `effect`
    (a precomputed `{rules?, schemas?}` patch) fields. Pending requests
    live under `<latchkey-directory>/permission_requests/v2/` -- the
    `v2` segment is the on-disk schema version so future shape changes
    can land in a fresh directory rather than trying to migrate files
    in place.
  - `POST /permission-requests/approve/<request_id>` is new. It reads
    the pending request, merges its `effect.rules` (union by scope key)
    and `effect.schemas` (overwrite by name) into the stored `target`
    permissions.json (creating it if missing), and deletes the pending
    request file. Returns the fresh permissions file in the response
    body.
  - The legacy `DELETE /permission-requests/<id>` continues to remove
    a pending request without applying its effect; the minds desktop
    client uses it for the deny path.
- The `file-sharing` permission effect was rewritten to target the new
  WebDAV mount that replaced `/api/v1/file-server` on the minds side:
  - The effect no longer mints its own scope schema. The rule now
    attaches the per-file permission to the pre-existing `latchkey-self`
    scope from the agent baseline (defined in `agent_setup.py`), which
    already matches any request whose `domain` is
    `latchkey-self.invalid`. The per-file permission schema is what
    restricts the grant to a single WebDAV URL + verb set; the scope
    just identifies which rule list the permission belongs to.
  - The per-file permission schema now pins `path` (the URL path) to
    the WebDAV URL for the requested file -- the WebDAV mount serves
    each absolute path at the URL
    `/minds-api-proxy/api/v1/files<absolute_path>`, so a
    grant for `/home/user/foo.txt` matches exactly
    `/minds-api-proxy/api/v1/files/home/user/foo.txt`. The match is a
    regex `pattern` (`^<base>(/.*)?$`), not a `const`, so the grant
    also admits the same URL with a trailing slash (WebDAV clients
    commonly emit one when treating the target as a collection) and
    any sub-path nested below it: a grant on `/home/user/share`
    therefore transitively covers every file and sub-directory
    inside the share. We do not need to reject `..` segments in the
    pattern itself because the gateway feeds the permission check a
    WHATWG `Request`, and the WHATWG URL parser already collapses
    both literal `..` and percent-encoded `%2e%2e` segments out of
    `pathname` before the pattern is ever applied. The legacy
    `queryParams.path` constraint is gone (WebDAV identifies the
    file in the URL path, not via a query parameter).
  - The allowed `method` enum grew from `GET` / `POST` to the full set
    of WebDAV verbs needed to read, write, query, lock, copy, and
    delete the file: `GET`, `HEAD`, `OPTIONS`, `PUT`, `DELETE`,
    `PROPFIND`, `PROPPATCH`, `MKCOL`, `COPY`, `MOVE`, `LOCK`,
    `UNLOCK`.
  - The wire shape grew an `access` field; the agent now POSTs
    `{type: "file-sharing", payload: {path: "<absolute>", access: "READ" | "WRITE"}}`.
    `access` is required and must be one of the two literal strings
    above (case-sensitive). `READ` unlocks only the non-mutating WebDAV
    verbs (`GET`, `HEAD`, `OPTIONS`, `PROPFIND`); `WRITE` is a strict
    superset that also unlocks the single-path mutating ones (`PUT`,
    `DELETE`, `PROPPATCH`, `MKCOL`, `LOCK`, `UNLOCK`). Per-file
    permission schemas now embed the access mode and the full file
    path in their name (`minds-file-server-read-<absolute-path>` /
    `minds-file-server-write-<absolute-path>`, e.g.
    `minds-file-server-read-/home/user/notes.txt`) so a user can hold
    both grants for the same path independently and a later WRITE
    grant does not silently override an earlier READ grant (or vice
    versa). Re-approving the same `(path, access)` pair remains
    idempotent (same schema name, schemas merge by name on approve).
  - `COPY` and `MOVE` are intentionally **not** in the WRITE verb set.
    Both carry a second path in the WebDAV `Destination` HTTP header,
    and the per-file permission schema only constrains the request
    URL; granting either would let an agent write to any file under
    the WebDAV mount's share roots (`~/` or `/tmp/`) via the
    `Destination` header, regardless of what was actually shared. A
    single-file WRITE grant means "change this one file"; cross-path
    copy/move requires an explicit grant on the destination too.
    Agents that need copy semantics can `GET` the source and `PUT` to
    a destination they have a separate grant for; likewise for move
    (`GET` + `PUT` + `DELETE`).

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

- Bumped pinned `imbue-mngr` / `imbue-common` / `concurrency-group` versions to match the current monorepo.

## 2026-05-20

- The `permission-requests` latchkey gateway extension now expects POST
  bodies with the fields `agent_id`, `scope` (string), `permissions`
  (list of strings), and `rationale` in place of the previous
  `service_name` field. Pending requests are stored under
  `<latchkey-directory>/permission_requests/v1/` so any existing files
  left over from the old shape are silently ignored.
- The `permissions` latchkey gateway extension now exposes two new
  catalog endpoints: `GET /permissions/available` returns the full
  catalog as a JSON object keyed by raw service name, and
  `GET /permissions/available/<service_name>` returns a single entry
  (or 404 if the service is unknown). Each catalog value has the
  shape `{"scope": "<schema_name>", "display_name": "...",
  "permissions": [...]}`. The catalog is backed by a `services.json`
  data file that ships alongside the extensions and is materialized
  into `LATCHKEY_DIRECTORY/extensions/` together with the `.mjs` files
  at gateway-spawn time.
- The default permissions seeded for every new agent are broadened to
  let the agent read its own current permissions
  (`GET /permissions/self`) and read the per-service catalog entry
  (`GET /permissions/available/<service_name>`) in addition to the
  existing ability to file a new permission request
  (`POST /permission-requests`). The catalog read is granted under a
  path-pattern Detent permission schema (matching
  `/permissions/available/<service_name>` only) so the agent baseline
  does not also expose the unbounded collection endpoint.
- ``LatchkeyGatewayClient.get_available_services`` now returns a typed
  ``dict[str, AvailableServiceEntry]`` (pydantic-validated) instead of
  the previous untyped ``dict[str, object]``. Wire-shape validation
  (missing fields, wrong types, empty strings) now happens inside the
  client and surfaces as ``LatchkeyGatewayClientError``.

Fixed a race condition in `mngr_latchkey`'s per-directory encryption-key
resolution where a concurrent caller could read the on-disk key file
while another process was mid-write, observing an empty string. The key
file is now published atomically by writing to a sibling temp file,
`fsync`ing it, and `os.link`-ing it into the final path -- so the final
path only ever exists with complete contents.

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

Stop caching the latchkey per-directory encryption key on the long-lived `Latchkey` pydantic model. The optional `encryption_key: SecretStr | None` field is gone; instead, `Latchkey._load_encryption_key()` reads (and on first call mints) the key on every subprocess-spawn call, so the secret only lives in parent-process memory for the duration of a single env-builder + process-spawn call frame. `apps/minds/imbue/minds/cli/run.py:_build_latchkey` and `libs/mngr_latchkey/imbue/mngr_latchkey/cli.py:_build_initialized_latchkey` no longer pre-load the key at construction time.

`load_or_create_encryption_key` now validates the on-disk key file's permission bits every load. Any group or other access bit set (i.e. anything that isn't owner-only -- `0o400`, `0o600`, `0o700` are accepted) raises a new `LatchkeyEncryptionKeyPermissionError` with a copy-pasteable `chmod 600 <path>` hint, so an operator who relaxed the mode finds out loudly instead of silently leaking the key to other local users. The operator override branch (`LATCHKEY_ENCRYPTION_KEY` in the env) still wins and is unaffected. Adds `encryption_key_test.py` covering precedence, idempotence, owner-only mode acceptance, group/other rejection, and the umask-permissive minting path.

## 2026-05-14

## mngr-latchkey: switch permission management to the latchkey 2.9.0 gateway extensions

### Summary

Latchkey 2.9.0 ships two new gateway extensions that this branch wires
into `mngr_latchkey` and the minds desktop client:

- `permission_requests.mjs` -- per-process pending-permission queue.
  Agents `POST /permission-requests` when they hit a blocked service;
  the desktop client consumes `GET /permission-requests?follow=true`
  to learn about new requests and `DELETE /permission-requests/<id>`
  to clear them once granted or denied.
- `permissions.mjs` -- a `permissions.json` editor that operates on any
  file path inside `LATCHKEY_EXTENSION_PERMISSIONS_ROOT`. Used by the
  desktop client to apply per-host permission grants via
  `POST /permissions/rules?path=<host_file>&rule_key=<scope>`.

Both extensions are bundled in `imbue-mngr-latchkey` and dropped into
`<LATCHKEY_DIRECTORY>/extensions/` automatically every time `mngr
latchkey forward` spawns the shared gateway.

### `imbue-mngr-latchkey`

- `LATCHKEY_MIN_VERSION` bumped from 2.8.0 to 2.9.0.
- New extension files at
  `imbue/mngr_latchkey/extensions/{permission_requests,permissions}.mjs`,
  rewritten from the originally-supplied drafts:
    * `permissions.mjs` now takes the target file path and rule key
      via the `?path=` and `?rule_key=` query params. It requires the
      `LATCHKEY_EXTENSION_PERMISSIONS_ROOT` env var (set by
      `Latchkey._spawn_gateway` to the plugin data dir) and refuses
      any path that resolves outside it.
    * `permission_requests.mjs` no longer accepts a caller-supplied
      `request_id`; the extension generates one server-side (a
      UUID-shaped hex string) and returns it in the POST response.
- `Latchkey.create_admin_permissions_jwt()` -- materializes
  `<plugin_data_dir>/latchkey_admin_permissions.json` (idempotent,
  with the wildcard rule `{"any": ["any"]}`) and returns a cached
  JWT pointing at it. Calling code uses this JWT in the
  `X-Latchkey-Gateway-Permissions-Override` header when it needs
  full access to the gateway's extension endpoints.
- New `mngr latchkey admin-jwt` CLI subcommand wraps the above and
  prints the JWT on stdout for shell-driven workflows.
- New `mngr latchkey gateway-info` CLI subcommand that prints the
  shared gateway's URL + password as a single JSON object on stdout.
  The bound gateway port is stamped onto the existing
  `LatchkeyForwardInfo` record (`gateway_port` field) so non-spawning
  processes can discover where the gateway is listening; the
  password is intentionally **never** persisted on disk and is
  derived locally by every consumer via
  :meth:`Latchkey.derive_gateway_password` (a pure function of the
  user's latchkey encryption key).

- Bump bundled Latchkey version to 2.11.1.

Regenerated CLI docs for `mngr latchkey` to reflect current options.

## 2026-05-13

# Latchkey state is now keyed per-host instead of per-agent

Public API changes in `imbue-mngr-latchkey`:

- `imbue.mngr_latchkey.agent_setup.finalize_agent_permissions` is
  renamed to `finalize_host_permissions` and takes a `HostId` instead
  of an `AgentId`.
- `imbue.mngr_latchkey.store.permissions_path_for_agent` /
  `link_opaque_permissions_to_agent` are renamed to
  `permissions_path_for_host` / `link_opaque_permissions_to_host` and
  take a `HostId`.
- The `mngr latchkey link-permissions` subcommand takes `--host-id`
  instead of `--agent-id`.

## 2026-05-12

## mngr-latchkey: new package

Added a new `imbue-mngr-latchkey` workspace package that owns the
shared `latchkey gateway` lifecycle, per-agent latchkey wiring, and
the reverse SSH tunnel that bridges the host-side gateway into remote
agents. The minds desktop client used to host this logic in
`apps/minds/imbue/minds/desktop_client/`; it now imports the package
and keeps only its own UI-layer code (permission dialog, service
catalog, HTML templates).

The package is currently a plain Python library -- no `mngr` CLI
subcommands are registered yet.

### Python API

- `imbue.mngr_latchkey.core.Latchkey` -- single wrapper around the
  upstream `latchkey` CLI. Owns gateway spawn / adopt / stop, password
  derivation, JWT minting, services-info and auth-browser probes.
  `Latchkey.initialize()` now runs `latchkey --version` and refuses to
  continue if the installed binary is older than the new
  `LATCHKEY_MIN_VERSION = "2.9.0"` constant; misconfiguration surfaces
  immediately rather than at the first gateway spawn. Failures raise
  the new `LatchkeyVersionError` (subclass of `LatchkeyError`).
- `imbue.mngr_latchkey.agent_setup.prepare_agent_latchkey` -- assembles
  the env vars an agent needs (`LATCHKEY_GATEWAY[_PASSWORD,_PERMISSIONS_OVERRIDE,_DISABLE_COUNTING]`)
  and an opaque permissions handle. **Raises** on infrastructure
  failures (latchkey CLI broken, on-disk write failed); callers decide
  whether to abort agent creation or fall back to an empty setup.
- `imbue.mngr_latchkey.agent_setup.finalize_agent_permissions` --
  replaces the opaque handle with a symlink to the canonical
  agent-keyed `latchkey_permissions.json` once `mngr create` has
  returned the canonical agent id. **Raises** `LatchkeyStoreError` on
  failure; same policy stance as above.
- `imbue.mngr_latchkey.discovery.LatchkeyDiscoveryHandler` -- agent
  discovery callback that ensures the shared gateway is up and opens
  a reverse SSH tunnel from `127.0.0.1:AGENT_SIDE_LATCHKEY_PORT` into
  the agent. Each tunnel is tagged with its agent id.
- `imbue.mngr_latchkey.discovery.LatchkeyDestructionHandler` -- agent
  destruction callback that drops the destroyed agent's reverse tunnel
  so the SSH-tunnel health-check loop doesn't keep spinning paramiko
  transports against a host that no longer exists.
- `imbue.mngr_latchkey.ssh_tunnel.SSHTunnelManager` -- reverse-tunnel
  manager with per-tunnel exponential backoff, agent-id tagging, and
  `remove_reverse_tunnels_for_agent`.
- `imbue.mngr_latchkey.store` -- on-disk persistence: gateway record,
  permissions config read/write, opaque-handle allocation, per-agent
  symlink linking.

### Layout

Plugin metadata lives under `<latchkey_directory>/mngr_latchkey/`, keeping
it cleanly segregated from anything the upstream `latchkey` CLI writes
under the shared `LATCHKEY_DIRECTORY`. Minds uses `~/.minds/latchkey`
as that root directory.

### Dependencies

- `imbue-mngr-forward` for the bidirectional socket/channel relay
  helper (`imbue.mngr_forward.relay`), keeping the half-closed-channel
  fix in a single place rather than duplicating it.

## mngr-latchkey: register a `mngr latchkey` CLI surface

The `imbue-mngr-latchkey` package now ships as a proper `mngr` plugin:
it declares a `[project.entry-points.mngr]` entry point and registers
a `mngr latchkey` command group with three subcommands, plus a
`[plugins.latchkey]` settings.toml block.

### New CLI

- `mngr latchkey forward` -- long-running foreground supervisor.
  Spawns the shared `latchkey gateway` subprocess, consumes
  `mngr observe`'s discovery stream, and sets up / tears down a
  reverse SSH tunnel for every agent on a remote host. Stops the
  shared gateway on `SIGINT`/`SIGTERM` (coupled lifetime).
- `mngr latchkey create-agent-env` -- one-shot. Wraps
  `prepare_agent_latchkey(is_tunneled=True)` and emits
  `{"env": {...}, "opaque_permissions_path": "..."}` on stdout as a
  single JSON object.
- `mngr latchkey link-permissions --agent-id ID --opaque-path PATH` --
  one-shot. Wraps `finalize_agent_permissions` to swing the opaque
  handle's symlink to the canonical agent-keyed permissions path.

### New settings

```toml
[plugins.latchkey]
directory = "~/.mngr/latchkey"   # default
latchkey_binary = "latchkey"     # default; resolved via PATH
```

Both fields are overridable via `MNGR_LATCHKEY_DIRECTORY` and
`MNGR_LATCHKEY_BINARY` env vars and matching `--latchkey-directory` /
`--latchkey-binary` CLI flags. Precedence is CLI > env > settings.toml
> built-in default.

## mngr-latchkey: `LatchkeyForwardSupervisor`

New class in `imbue.mngr_latchkey.forward_supervisor`. Owns the
lifecycle of a single detached `mngr latchkey forward` subprocess for
a given `latchkey_directory`:

- `ensure_running()` -- idempotent. Spawns a fresh detached supervisor
  if no record exists; adopts the existing one if its PID is still
  alive and its cmdline matches `mngr latchkey forward`; otherwise
  discards the stale record and spawns a fresh one.
- `stop()` -- SIGTERMs the supervisor (which cascades into
  coupled-lifetime shutdown of the shared gateway + reverse tunnels)
  and deletes the record.
- `get_forward_info()` -- read-only inspection of the on-disk
  `LatchkeyForwardInfo` record.

## mngr-latchkey: drop the on-disk gateway record

With `LatchkeyForwardSupervisor` guaranteeing at most one `mngr
latchkey forward` process per latchkey directory, the only thing that
ever spawns a `latchkey gateway` is that single supervised forward
subprocess. Cross-process gateway adoption -- the original reason for
persisting a `LatchkeyGatewayInfo` record at `<plugin_data_dir>/latchkey_gateway.json`
-- is no longer needed.

Changes:

- `Latchkey.initialize()` no longer reads or reconciles a persisted
  gateway record. It still runs `latchkey --version` so misconfiguration
  surfaces eagerly.
- `Latchkey.ensure_gateway_started()` no longer persists / restores
  state across processes; it stays in-process-idempotent (subsequent
  calls return the cached `self._info`).
- `Latchkey.stop_gateway()` no longer deletes a record; just terminates
  the in-memory tracked subprocess.
- `imbue.mngr_latchkey.store`: removed `save_gateway_info`,
  `load_gateway_info`, `delete_gateway_info`, `gateway_info_path`, and
  the `_GATEWAY_RECORD_FILENAME` constant. `LatchkeyGatewayInfo`
  itself stays as the in-memory return-type for the spawn path.

## mngr-latchkey: spawn the gateway via ConcurrencyGroup, simplify Latchkey API

API changes:

- `Latchkey.ensure_gateway_started()` -> `Latchkey.start_gateway(cg)`.
  Takes the owning `ConcurrencyGroup` as an explicit argument (the CG's
  `__exit__` is what terminates the gateway). In-process idempotent.
- Replaced the `LatchkeyGatewayInfo` return value with simpler
  in-instance state and accessors:
  - `Latchkey.is_gateway_running` -- boolean.
  - `Latchkey.gateway_port` -- int (raises `LatchkeyNotInitializedError`
    when no gateway is running).
  - `Latchkey.gateway_url` -- `http://<listen_host>:<gateway_port>`.
- `LatchkeyGatewayInfo` itself is gone (was the in-memory return shape
  with `host`/`port`/`started_at`; replaced by the properties above).
- `Latchkey.get_gateway_info()` removed; callers use `is_gateway_running`
  / `gateway_port` directly.

## mngr-latchkey: do not let `mngr latchkey forward` die with its parent

`_forward_command` was calling `start_parent_death_watcher`, which polls
`os.getppid()` every ~3 seconds and SIGTERMs the process when the
original parent dies and the process gets reparented to PID 1. That
actively defeats the detached-supervisor pattern: when minds spawns
`mngr latchkey forward` with `start_new_session=True` and then exits,
the watcher saw the reparent and shut the gateway down within ~3
seconds.

Removed the watcher call. To still handle the *interactive* case (user
runs `mngr latchkey forward` in a terminal and closes the terminal),
SIGHUP is now wired into the same signal handler as SIGINT/SIGTERM,
so a terminal close triggers the clean coupled-lifetime shutdown path
rather than killing the python interpreter under the default handler
(which would leave the gateway orphaned).
