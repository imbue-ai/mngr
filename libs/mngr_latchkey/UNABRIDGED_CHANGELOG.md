# Unabridged Changelog - mngr_latchkey

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_latchkey/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
