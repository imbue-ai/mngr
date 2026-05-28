# Unabridged Changelog - minds

Full, unedited changelog entries consolidated nightly from individual files in `apps/minds/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-28

Pin minds desktop client JS toolchain to exact versions: pnpm 10.33.4 and Node.js 24.15.0. With `engine-strict=true` plus exact `engines.node`/`engines.pnpm`, installs fail fast on mismatch instead of breaking in confusing ways. Added `.nvmrc` so nvm/fnm users pick up the pinned Node automatically. Documented how existing developers can install the pinned versions (nvm/fnm for Node, `npm install --global pnpm@10.33.4` for pnpm, with a note that Homebrew's `@<major>` kegs drift) in `apps/minds/docs/desktop-app.md`. Also pinned end-user Python to `==3.12.13` in `apps/minds/electron/pyproject/pyproject.toml` (the packaged-app pyproject that uv reads at first launch) so every install downloads the same interpreter instead of the latest 3.12 patch available at the time.

Added a 7-day dependency cooldown (minimum release age) for supply-chain hardening: `minimumReleaseAge: 10080` in `apps/minds/pnpm-workspace.yaml` (pnpm) and `exclude-newer = "7 days"` under `[tool.uv]` in the packaged `apps/minds/electron/pyproject/pyproject.toml` (uv). Both refuse to resolve any distribution -- including transitive ones -- published within the last week, so a freshly-compromised release cannot be pulled in before it is noticed. The cooldown only affects resolution; frozen-lockfile installs are unaffected.

Upgraded Electron from `35.7.5` to `40.10.1` so the runtime shipped to end users bundles Node.js `24.15.0` -- matching the exact Node version pinned for development (`engines.node` / `.nvmrc`). Previously the bundled Electron shipped a different (Node 22.x) runtime than the one developers built against. Electron 40 is the lowest major on the Node 24 line, so 40.10.1 is the smallest jump that reaches the pinned `24.15.0`; staying on 40 avoids the behavior changes introduced in 41 (cookie `changed`-event cause values) and 42 (macOS notifications require code-signing) that a jump to the newest line would pull in, none of which our Electron code depends on today but which would otherwise widen the review/test surface. 40.10.1 also clears the new 7-day pnpm cooldown (published more than a week ago), so it needs no `minimumReleaseAgeExclude`.

Bumped the bundled `UV_VERSION` in `apps/minds/scripts/build.js` from `0.7.12` to `0.11.15`. uv only supports the relative-duration form of `exclude-newer` (e.g. `"7 days"`) as of 0.10.0; the older 0.7.12 fails to parse it, silently ignores the cooldown, and -- because the committed lockfile was generated with a timestamp cutoff that 0.7.12 then sees as "removed" -- discards the lockfile and re-resolves unpinned at end-user first launch. (The lockfile's `revision = 3` format is not the issue: 0.7.12 reads revision-3 lockfiles fine; the trigger is purely the unparseable relative `exclude-newer`.) Bumping to 0.11.15 makes the shipped uv able to parse the cooldown, so it takes effect and the committed lockfile is honored.

Bump Latchkey dependency to 2.12.2. The newest Latchkey version properly shows the ToS dialog to first-time Google Cloud users.

# Desktop e2e opts FCT's config into the pytest guard (test-only)

mngr's `is_allowed_in_pytest` config field now defaults to `False`, and every
config loaded during a pytest run must opt in. The desktop-client Docker e2e
(`test_desktop_client_e2e.py`) deliberately loads forever-claude-template's real
`.mngr/settings.toml` (it pins `MNGR_ROOT_NAME=mngr` to get the create
templates), so it now adds `is_allowed_in_pytest = true` to that checkout for the
duration of the test and restores it afterward. The opt-in is intentionally
added in-test (not shipped in FCT's config, which would disable the guard for
every FCT-based project). Test-only change.

# Extract Electron e2e workspace creation flow into a reusable runner

Split the Playwright-over-CDP driver out of
`apps/minds/test_desktop_client_e2e.py` into a new module at
`apps/minds/imbue/minds/desktop_client/e2e_workspace_runner.py` so the
same flow can be invoked outside pytest. The new module exposes the
public entry points `create_workspace_via_electron`, `resolve_fct_path`,
`ensure_minds_env_defaults`, `configure_logging`, `find_free_port`, and
`destroy_agent_best_effort`; everything else stays underscore-prefixed.

The existing pytest test was reduced to a thin wrapper that:

- calls `ensure_minds_env_defaults(setenv=monkeypatch.setenv)` so any
  injected env vars get reverted between tests,
- delegates the actual Electron / Playwright flow to
  `create_workspace_via_electron`, and
- always calls `destroy_agent_best_effort` in `finally` so a successful
  test never leaks an agent into the host.

`scripts/snapshot_minds_e2e_state.py` is the second caller: it invokes
`create_workspace_via_electron` directly and deliberately omits the
`mngr destroy` cleanup, because the whole point of the snapshot is to
capture a sandbox in which the workspace's Docker container is alive.

Also added a `*/desktop_client/e2e_workspace_runner.py` exclusion to the
`test_prevent_direct_subprocess` ratchet, since the new module
necessarily shells out to `electron`, `git`, and `uv run mngr destroy`
(operator-tool subprocesses with no `ConcurrencyGroup`-managed
equivalent). No user-visible behavior change.

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

Bump Latchkey dependency to 2.12.1. The newest Latchkey version is capable of reusing Google Projects which is important because the default limit on Google Project count is low.

# Fix a signing-key generation race that intermittently logged users out

`FileAuthStore.get_signing_key` generated the cookie signing key lazily
on first access without any synchronization. FastAPI dispatches sync
route handlers on a threadpool, so on a fresh data directory the desktop
client's startup burst -- `/authenticate` plus the `/` redirect target,
`/_chrome`, and `/welcome`, each of which checks authentication -- could
all reach key generation concurrently. Two interleavings both broke auth:

- A reader saw the just-created key file as momentarily empty (the old
  code did a non-atomic `write_text`) and raised `SigningKeyError`, so
  `/authenticate` returned 500 and no session cookie was set.
- Two threads each generated a *different* key and raced to write it;
  the last writer won and silently invalidated the cookie that had just
  been signed with the earlier key, so the next request's
  `verify_session_cookie` failed and the user appeared logged out.

Either way the subsequent page load came back unauthenticated. This was
the dominant cause of flaky failures in the `test-docker-electron` CI job
(`test_create_local_docker_workspace_via_electron` timing out on the
`#create-form` selector because `GET /create` returned 403).

`get_signing_key` now reads the existing key on the fast path and, when
it must generate one, serializes generation behind a per-store lock with
a double-checked re-read and writes the key via `atomic_write` so a
concurrent reader never observes a partial file. Concurrent first-time
callers now always converge on a single persisted key.

Fixed the Deny button on the latchkey permission-request dialog so it works even when the requested scope is not in the gateway's services catalog (e.g. a typo from the agent or a stale catalog). Previously clicking Deny returned `{"error": "Scope 'XYZ' is not in the gateway catalog"}`; the deny flow now falls back to the raw scope string for both the persisted response event and the agent-facing message, so the pending request is always torn down and the agent is always notified.

# ty 0.0.39 type fix

- `_resolve_ws_name_and_account` now returns `list[AccountSession]` instead of `list[object]`. `ty` 0.0.39 rejected the previous annotation because `list` is invariant (`list[AccountSession]` is not assignable to `list[object]`); the precise element type is also more accurate.

No user-facing behavior change.

## 2026-05-26

# Minds API access: gateway-only, single key, per-agent URL prefix

The minds desktop client used to expose its `/api/v1/...` REST API to
workspaces over a per-agent reverse SSH tunnel, writing the resulting
URL to `$MNGR_AGENT_STATE_DIR/minds_api_url` and injecting a per-agent
UUID4 `MINDS_API_KEY` into each new host's env file. None of that is
how agents actually reach the Minds API anymore -- the latchkey
gateway's `minds-api-proxy` extension already handled it -- so the
machinery is gone:

- `minds run` no longer asks the `mngr forward` plugin for a
  `--reverse 0:<port>` tunnel and no longer registers any
  `on_reverse_tunnel_established` callback. The `MindsApiUrlWriter`
  and `LocalAgentDiscoveryHandler` classes (and their tests) have
  been removed from `forward_cli.py`.
- `agent_creator.py` no longer generates a per-agent `MINDS_API_KEY`,
  no longer adds `--host-env MINDS_API_KEY=...` to `mngr create`, and
  no longer stores any per-agent `api_key_hash` file. Workspaces no
  longer carry the env var at all.
- The `apps/minds/imbue/minds/desktop_client/api_key_store.py` module
  has been rewritten around a single central key, freshly generated
  in memory on every `minds run` via `generate_api_key()`. The key is
  not persisted to disk -- the supervisor is always restarted on
  minds startup and gets the current value in its env, the bare-
  origin auth gate sees the same in-memory value, and no other
  process reads the key. Rotating per-startup removes a long-lived
  secret from the filesystem.
- The `/api/v1/...` bearer-auth gate (used by both `api_v1.py` and the
  WebDAV mount under `/api/v1/files`) now compares the inbound
  `Authorization: Bearer <key>` against that single value with a
  constant-time check. Routes that need an agent id take it from the
  URL path -- the auth dependency itself returns `None`.
- The notifications endpoint moved from `POST /api/v1/notifications`
  to `POST /api/v1/agents/<agent_id>/notifications`, matching the
  Telegram routes. Every `/api/v1` route is now per-agent.
- Every agent created by minds gets added to the host's
  `minds-api-proxy-allowed-agent` enum at finalize-host-permissions
  time. The baseline permissions file's first rule rejects any
  `/minds-api-proxy/api/v1/agents/<id>/...` whose `<id>` is not in
  that enum, so an agent on host A cannot reach the Minds API on
  behalf of an agent on host B (B's id only appears in B's host's
  permissions file).
- The desktop client now calls
  `imbue.mngr_latchkey.agent_setup.register_agent_for_host(...)` directly
  -- a single library call that does an atomic file edit -- instead of
  the previous gateway-extension dance that POSTed two schemas + one
  rule per agent. The `gateway_client` field on `AgentCreator` is
  gone; `LatchkeyGatewayClient` keeps its existing user-grant API
  (`set_permission_rule`, etc.) but no longer ships the low-level
  schema-altering methods (`set_permission_schema`,
  `delete_permission_schema`, `delete_permission_rule`).
- The operator-facing equivalent is the new
  `mngr latchkey register-agent --host-id ID --agent-id ID` CLI command.
- The `inject_tunnel_token_into_agent` helper moved out of
  `api_v1.py` into its own module so it can be imported without
  pulling the FastAPI router in.

Documentation:
[`apps/minds/docs/latchkey-permissions.md`](docs/latchkey-permissions.md)
now has a "Minds API access through the gateway" section describing
the new model; [`specs/minds-rest-api/spec.md`](../../specs/minds-rest-api/spec.md)
has a banner pointing out which parts are superseded.

- Renamed the minds ``LaunchMode.LOCAL`` compute provider to ``LaunchMode.DOCKER`` everywhere (Python code, ``/create`` form HTML, ``/api/create-agent`` JSON payloads, docs). The mode has always meant "Docker container on the user's machine"; the old name collided with mngr's own ``local`` provider (which runs agents as host processes), so the rename eliminates that ambiguity. The other modes (``LIMA``, ``CLOUD``, ``IMBUE_CLOUD``) are unchanged. ``/api/create-agent`` and the create form now expect ``launch_mode=DOCKER`` instead of ``LOCAL``; submitting ``LOCAL`` is no longer recognized.

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

- `apps/minds`: bundle Lima into the desktop app. `scripts/build.js` now downloads the official Lima 2.1.1 release tarball for the build host's platform/arch and extracts it into `resources/lima/`; the packaged backend prepends `resources/lima/bin` to `PATH` so `limactl` is found without a separate `brew install lima` step. The unsigned `lima-guestagent.Darwin-*.gz` Mach-O payloads are stripped after extraction -- they break macOS notarization and are unreachable (we run Linux VMs only). A new `entitlements.mac.plist` carries `com.apple.security.virtualization` (required by `limactl`'s VZ driver), and `todesktop.json` wires it in via a `mac` block that also deep-signs the bundled `limactl`. On macOS Apple Silicon this is fully self-contained via Lima's `vz` backend; macOS Intel and Linux still require QEMU on the host machine.
- `apps/minds`: bump `workspace_ready_timeout_seconds` from 60s to 300s (`agent_creator.py`). First-boot provisioning (uv sync, npm ci + run build for the system_interface frontend) regularly takes 90-180s on a fresh VM or Docker host, so the 60s default was bouncing users to the recovery page while the agent was still finishing provisioning. The probe is cheap so a generous cap is harmless.

- Fixed a stale `LaunchMode.LOCAL` reference in `agent_creator_test.py` that was missed during the `LaunchMode.LOCAL` -> `LaunchMode.DOCKER` rename, which was causing `test_no_type_errors` to fail. No user-visible behavior change.

Hardened the workspace-restart shell command in `desktop_client/app.py` to use
exact-session matching. The previous `tmux kill-window -t "${MNGR_PREFIX}system-services:svc-system_interface"`
form had no leading `=`, so if the `${MNGR_PREFIX}system-services` session was gone
but a sibling-prefix session was alive, the kill-window could silently land on the
wrong agent's session and kill a window there. The command now uses
`-t "=${MNGR_PREFIX}system-services:svc-system_interface"` so tmux refuses to misroute.

To prevent recurrences, adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule
(added in `imbue_common`) via `rc.check_bare_tmux_targets(_DIR, snapshot(0))` in
this project's `test_ratchets.py`. The ratchet flags new occurrences of
`tmux <subcmd> -t '<bare-name>'` -- targets without a leading `=` exact-match
prefix, which can silently route commands to a sibling session whose name shares
a prefix with the intended one. The adopting test starts at a baseline of zero
violations.

## 2026-05-22

`minds run` no longer dictates the `mngr forward` plugin's port. The `--mngr-forward-port` flag and the `MINDS_MNGR_FORWARD_PORT` environment variable are removed: the plugin now picks its own port (its default, or an OS-assigned fallback when the default is taken) and reports it back via its `listening` envelope. `minds run` blocks briefly at startup until that envelope arrives, then uses the reported port for everything downstream; if the plugin fails to report a port within 5s, startup aborts with a clear error.

- Bump bundled Latchkey version to 2.11.3.

Latchkey gateway ships a new bundled `minds-api-proxy` extension that
transparently reverse-proxies requests under `/minds-api-proxy`
to the minds desktop client's bare-origin "Minds API". The upstream URL
is read at request time from the `LATCHKEY_EXTENSION_MINDS_API_URL`
environment variable, and is published to the detached
`mngr latchkey forward` supervisor (via the new
`LatchkeyForwardSupervisor.extra_env`) on every `minds run` startup, so
the proxy always points at the live Minds API port even when minds
re-binds on restart. The extension responds 503 when the env var is not
configured; requests still go through the gateway's normal permission
check.

The Latchkey gateway's `permission-requests` extension grows a typed
request schema and a new approve endpoint:

* `POST /permission-requests` now takes `{agent_id, rationale, type,
  payload}` instead of the legacy flat `{scope, permissions, ...}`
  shape. The `type` field is `"predefined"` (payload
  `{scope, permissions}`) or `"file-sharing"` (payload `{path}`,
  absolute-only, no `..` segments).
* Each pending request is persisted with the additional `target`
  (the extension's per-request `permissionsConfigPath`) and `effect`
  (a precomputed `{rules?, schemas?}` patch) fields. Pending requests
  live under `<latchkey-directory>/permission_requests/v2/` -- the
  `v2` segment is the on-disk schema version so future shape changes
  can land in a fresh directory rather than trying to migrate files
  in place.
* `POST /permission-requests/approve/<request_id>` is new. It reads
  the pending request, merges its `effect.rules` (union by scope
  key) and `effect.schemas` (overwrite by name) into the stored
  `target` permissions.json (creating it if missing), and deletes
  the pending request file. Returns the fresh permissions file in
  the response body.
* The legacy `DELETE /permission-requests/<id>` continues to remove
  a pending request without applying its effect; the minds desktop
  client uses it for the deny path.
* The `file-sharing` effect now targets the WebDAV mount described
  below. The effect attaches a per-file permission to the
  pre-existing `latchkey-self` scope from the agent baseline rather
  than minting its own scope schema. The per-file permission schema
  matches the URL path via a regex `pattern` rooted at the WebDAV
  URL for the requested resource
  (`/minds-api-proxy/api/v1/files<absolute_path>`): the exact path,
  the same path with a trailing slash, and any sub-path nested
  below it. A grant on a directory therefore transitively covers
  every file and sub-directory inside it. `..` segments do not need
  to be rejected by the pattern because the gateway's permission
  check sees the WHATWG-normalised `pathname`, which has already
  collapsed both literal `..` and percent-encoded `%2e%2e` away.
  The legacy `queryParams.path` constraint is gone.
* File-sharing requests now carry a required `access` field on the
  payload (`READ` / `WRITE`). `READ` unlocks the non-mutating WebDAV
  verbs only (`GET`, `HEAD`, `OPTIONS`, `PROPFIND`); `WRITE` is a
  strict superset that also unlocks the single-path mutating verbs
  `PUT`, `DELETE`, `PROPPATCH`, `MKCOL`, `LOCK`, `UNLOCK`. `COPY` and
  `MOVE` are intentionally excluded -- both carry a second path in
  the WebDAV `Destination` header that the per-file permission schema
  cannot constrain, so granting either would let an agent write to a
  different file in the share than the one actually shared. Per-file
  permission schemas embed the access mode in their name
  (`minds-file-server-read-<hash>` / `minds-file-server-write-<hash>`)
  so the two grants are independent. The minds approval dialog shows
  a green "read-only" or amber "read & write" badge inline next to
  the requested file path and explains what the agent will be allowed
  to do; the granted / denied
  notification text reflects the mode as well.

The minds desktop client's latchkey-permission handler code was
reorganised so the two permission request types now live as siblings
under a single `imbue.minds.desktop_client.latchkey.handlers`
package: `.predefined` (the existing catalog-backed flow, moved from
`latchkey/permissions.py`) and `.file_sharing` (moved from
`latchkey/file_sharing.py`). Their shared helpers (`MngrMessageSender`
and the Jinja-template renderers) live alongside them in the same
package. The file-sharing approval dialog now uses the same Jinja
template + Tailwind base (`templates/permissions.html`) and visual
style as the predefined dialog instead of a hand-written HTML page.

The minds desktop client side learns to render and resolve both
request types:

* `LatchkeyPermissionRequestEvent` was renamed to
  `LatchkeyPredefinedPermissionRequestEvent` to mirror the wire
  `type=predefined` and to distinguish it from the new file-sharing
  event (both flow through Latchkey).
* A new `LatchkeyFileSharingPermissionRequestEvent` (and
  accompanying `FileSharingGrantHandler`) renders a single yes/no
  dialog per absolute file path. Approval calls
  `POST /permission-requests/approve/<id>` on the gateway; denial
  uses the existing DELETE path. There is no UI to revoke or edit
  an existing file-sharing grant -- the user has to edit
  `latchkey_permissions.json` by hand for that, for now.
* `LatchkeyGatewayClient` gains an `approve_permission_request`
  method. The `StreamedPermissionRequest` model carries the new
  wire shape (`request_type` + `payload` + `target` + `effect`).
  `payload` is typed directly as the `PredefinedRequestPayload |
  FileSharingRequestPayload` union (pydantic's smart-union mode
  resolves the two disjoint shapes at decode time), and `effect` is
  typed as a `PermissionEffect` model with `rules` and `schemas`
  fields. Consumers dispatch via `isinstance` on `payload` rather
  than re-validating the dict at the call site.

The Minds REST API ships a new WebDAV file-server mount at
`/api/v1/files`, backed by [`wsgidav`](https://wsgidav.readthedocs.io/)
wrapped in [`a2wsgi`](https://github.com/abersheeran/a2wsgi). Two
share roots are exposed:

* the current user's home directory (`Path.home()`); and
* `/tmp`.

Each share is mounted at its on-disk path so the outward URL mirrors
the absolute path one-to-one: `/home/<user>/foo.txt` is reached at
`/api/v1/files/home/<user>/foo.txt`, `/tmp/blob.bin` at
`/api/v1/files/tmp/blob.bin`. Any standard WebDAV verb works (`GET`,
`PUT`, `PROPFIND`, `DELETE`, ...); paths outside the two shares are
not served. The HTML directory browser is disabled.

The mount uses the same per-agent Bearer-token authentication as the
rest of `/api/v1/`: a thin ASGI wrapper verifies
`Authorization: Bearer <api_key>` against `find_agent_by_api_key` and
401s before any request reaches the filesystem; WsgiDAV itself runs
anonymous. The mount is reachable from agents through the
`minds-api-proxy` Latchkey extension.

`MINDS_API_KEY` is now written to the workspace host's env file via
`--host-env` (instead of the system-services agent's per-agent env via
`--env`) when running `mngr create` for a new workspace. Each workspace
now spawns multiple agents on the same host (the initial
`system-services` agent plus the chat agents the FCT bootstrap and the
system_interface's "New Chat" button create), and only the
system-services agent's `mngr create` is invoked by minds itself. Moving
the variable to the host env file lets every agent on the host inherit
the same key, so chat agents can authenticate against the desktop
client's `/api/v1/...` routes (including the new file-sharing endpoints)
just like the system-services agent. The API-key hash is still stored
once under the system-services agent's id, so all workspace-side
requests resolve to that id for caller identification.

## New providers panel on the landing page

- The landing page now includes a Providers section listing every configured provider (except `local`, which is always present and always healthy). Each entry shows the provider name, backend type, a status badge (OK / Error / Disabled), the last error message verbatim when applicable, and an Enable or Disable button.
- Two small freshness counters at the top of the panel show "time since last discovery event" and "time since last full discovery event" so a stalled discovery loop is immediately visible.
- Clicking Disable on a working or errored provider, or Enable on a disabled one, writes `is_enabled` to minds' active settings file and bounces `mngr observe` so the change takes effect on the next poll. The button shows "Waiting…" until the next full snapshot lands.

## No more silent auto-disable on auth errors

- Previously, when discovery surfaced `ImbueCloudAuthError`, minds would silently rewrite the user's settings to set `is_enabled = false` on the offending `imbue_cloud_<slug>` provider. That entire path is gone: `_ImbueCloudAuthErrorDisabler` and the provider-error callback plumbing on `EnvelopeStreamConsumer` are removed.
- The same outcome is now user-driven: an errored `imbue_cloud_<slug>` provider shows up in the providers panel with the verbatim error message; the user clicks Disable to silence it, or fixes the upstream auth and the provider recovers on the next snapshot.

## Agents no longer silently disappear when a provider fails

- When a provider (e.g. Modal or imbue_cloud) fails discovery, its agents previously vanished from the landing page agent list with no explanation. Now `AgentObserver` emits an `UNKNOWN` agent state for previously-observed agents on errored providers (sticky until they reappear or are explicitly destroyed). The landing page's agent list itself still shows only currently-discovered agents, but the providers panel surfaces the underlying provider error so the user can see *why* an agent might be missing.
- `mngr_notifications` users: see that project's changelog for the new `RUNNING -> UNKNOWN -> WAITING` transition handling.

## Internal: `set_provider_is_enabled`

- `disable_imbue_cloud_provider_for_account` was renamed to `set_provider_is_enabled(provider_name, is_enabled)` and generalized to work on any provider name. All callers in `apps/minds/` are migrated; no compatibility shim. The function writes to minds' active settings file and creates the `[providers.<name>]` block if it doesn't yet exist.

## 2026-05-21

Adds `test_create_local_docker_workspace_via_electron`: an acceptance test that drives the real Electron minds app via Playwright over CDP, clicks through the create form, waits for the workspace's `system_interface` dockview UI to render through the desktop client proxy, and cleans up the resulting `mngr` agent. Resolves the forever-claude-template source in three steps -- a local `.external_worktrees/` worktree first, then a shallow clone of the matching mngr branch on the FCT public remote, then `main` -- so the test runs unchanged in CI and against an operator's local FCT working tree.

Adds the `MINDS_MNGR_FORWARD_PORT` env var to `minds run` so test harnesses (and concurrent `just minds-start` invocations) can dodge the hardcoded default port 8421 collision.

Replaces the stale skipped `test_create_agent_e2e` (which never drove Electron and carried an out-of-date "TUI send-enter timeout" skip reason that no longer applies after FCT split its services agent from its chat agent).

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

- Add a new `ci` tier to the minds env system (alongside `dev`/`staging`/`production`). `ci-<...>` env names are now accepted everywhere `dev-<...>` names are; the new tier mirrors the dev tier's lifecycle (per-env Modal env, per-env Neon project + SuperTokens app, per-env local state) and reads its Vault secrets from `secrets/minds/ci/*` (mirrored from `secrets/minds/dev/*` for now).
- The deployment-tests orchestrator now mints ephemeral envs named `ci-<timestamp>-<uuid>` (was `dev-ci-<...>`); shared envs are now `ci-<run-id>` (was `dev-ci-<run-id>`). The shorter names stay within Modal's DNS-label budget with more headroom.

`minds env activate` no longer exports `MODAL_PROFILE` by default.
Activation now has two modes:

- **Use-only (default)**: `minds env activate <name>` exports the four
  use-side env vars (`MINDS_ROOT_NAME`, `MNGR_HOST_DIR`, `MNGR_PREFIX`,
  `MINDS_CLIENT_CONFIG_PATH`) and emits `unset MODAL_PROFILE`. This is
  what every non-deploying user wants -- the desktop client, mngr, and
  Latchkey no longer try to authenticate against a Modal workspace the
  operator may not have tokens for. Fixes the spurious "Modal is not
  authorized" warnings + Latchkey breakage that hit anyone running
  `minds run` after `eval "$(uv run minds env activate staging)"`
  without a `minds-staging` profile in `~/.modal.toml`.
- **Deploy-mode (`--deploy`)**: `minds env activate --deploy <name>`
  additionally exports `MODAL_PROFILE=<tier's modal_workspace>` and
  pre-validates that `~/.modal.toml` has a matching profile (fails up
  front with a `modal token set --profile <workspace>` hint when it
  doesn't, instead of letting downstream `modal …` shellouts surface
  the auth error).

`minds env deploy`, `minds env destroy`, and `minds env recover` now
refuse to run unless the shell is deploy-activated (`MODAL_PROFILE`
must equal the tier's `modal_workspace`). The refusal message tells
the operator the exact `eval "$(uv run minds env activate --deploy
<name>)"` to run.

The packaged Electron app and `deployment_tests/helpers.py` are
unchanged -- both set their Modal credentials independently of shell
activation.

## 2026-05-20

- The "Creating your project" page now updates its spinner caption as the setup progresses ("Starting...", "Cloning repository...", "Checking out branch...", "Provisioning AI access...", "Creating workspace...", "Waiting for workspace to be ready..."), instead of staying on "Cloning repository..." through the whole flow. Phase state is now carried on the existing ``AgentCreationStatus`` enum as the single source of truth -- the spinner caption is resolved from that enum value by the SSE stream, which polls the creation status on each loop iteration. The ``/api/create-agent/{id}/status`` JSON API now returns the new enum values (``INITIALIZING``, ``CLONING_REPO``, ``CHECKING_OUT_BRANCH``, ``PROVISIONING_AI``, ``CREATING_WORKSPACE``, ``WAITING_FOR_READY``, ``DONE``, ``FAILED``) instead of the previous ``CLONING`` / ``CREATING``.

- `just minds-start` now unsets `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` before launching the desktop client, so credentials exported in the developer's shell no longer leak into agents created by the dev app.

Renamed the "workspace server" feature to "system interface" in the desktop client: the menu item / recovery page label "Restart workspace server" became "Restart system interface". Frontend Electron clients automatically pick up the new wire format and labels.

Workspace-server restart and health-recovery UI on the `mngr_forward` plugin architecture.

User-visible changes:

- When an agent's workspace server stops responding, the chrome auto-navigates the workspace view to a recovery page where the user can restart the server. The recovery page streams server-status updates over SSE and reloads back to the workspace once the server is healthy again.
- The landing page now annotates each project row with a status badge when its workspace server is unresponsive or restarting; clicking such a row goes to the recovery page instead of the workspace.
- The sidebar context menu gained a "Restart workspace server" entry that opens the recovery page for the selected workspace.
- A dedicated recovery page (`/agents/<id>/recovery`) renders the restart button, streams server-status updates via SSE, and auto-reloads back to the workspace once the server is healthy again.
- Minds tracks `workspace_backend_failure` envelopes from the `mngr_forward` plugin as a per-agent state machine (HEALTHY -> STUCK after 5 seconds of continuous failures -> RESTARTING during a user-triggered restart -> back to HEALTHY on the first successful probe).

Restart UX improvements on top of the above:

- The `/api/agents/<id>/restart-workspace-server` endpoint now returns 200
  as soon as the `mngr exec` kill dispatch completes (it no longer blocks
  for up to 15 seconds polling the workspace through the plugin). The
  background workspace-health probe loop continues to flip the tracker back
  to HEALTHY once the workspace is responsive. This makes the endpoint a
  reliable "the workspace has been killed" signal for callers that want to
  navigate to the plugin's loader page.
- The recovery page's "Restart workspace server" button and the sidebar
  right-click "Restart workspace server" menu item now both await the
  restart API response before navigating to the workspace URL. Previously
  they fired the POST and navigated immediately, which on a still-healthy
  workspace raced against the in-flight kill and silently reloaded onto
  the unchanged iframe. Awaiting guarantees the user lands on the plugin's
  "Workspace server starting..." loader.
- The recovery page now notes that running agents are not interrupted by a
  workspace-server restart.
- Stale failure envelopes arriving immediately after a successful restart
  no longer cause a brief recovery-page flash; the health tracker now
  ignores failures within a short grace window after recovery.
- The "Workspace server starting" loader spinner no longer visibly jumps
  on each refresh. The spinner's animation duration now matches the page's
  1-second auto-refresh interval, so the spinner is at the cycle boundary
  (rather than 90 degrees past it) when the reload fires.

Minds: start the latchkey gateway client lazily on a background thread so `minds run` no longer blocks on the `mngr latchkey forward` supervisor binding its gateway port. Callers that need the gateway (the permission-request stream consumer and the FastAPI request handlers) wait on `ensure_initialized()` themselves the first time they use the client.

- The minds desktop client has been adapted to the new latchkey
  permission-request shape: `LatchkeyPermissionRequestEvent` now carries
  `scope` (Detent schema) and `permissions` (the agent's requested list)
  instead of `service_name`. The previously-bundled
  `apps/minds/imbue/minds/desktop_client/latchkey/services.toml` has
  been deleted; the desktop client now lazily fetches the catalog from
  the gateway's `/permissions/available` endpoint (cached in process)
  to look up display names and the legal permission set. The grant
  dialog continues to render the display name ("Slack" etc.) and lets
  the user broaden or narrow the requested permission set.
- The minds desktop client now tolerates legacy response events on
  disk. Older versions wrote a ``service_name`` field on each
  ``RequestResponseEvent``; the current schema replaced it with
  ``scope``. Without a migration the historical events.jsonl emitted
  a pydantic-extras warning per legacy line at every minds startup
  and the corresponding request would not be marked resolved. The
  loader now drops ``service_name`` before validating, so historical
  responses load cleanly and their requests are correctly filtered
  out of the pending list. The dropped ``service_name`` is
  informational only -- pending-request filtering uses
  ``request_event_id`` -- so no functional information is lost.
- The streamed-permission-request handler now dedupes redeliveries by
  ``event_id``. The gateway re-emits every still-pending request on
  each stream reconnect (every couple of seconds when idle), but the
  handler used to append a fresh entry to the in-memory request inbox
  and emit an INFO log line + an SSE wake-up for every redelivery. The
  ``requests`` list therefore grew unbounded for as long as a request
  stayed pending, and the desktop log filled with duplicate ``Streamed
  latchkey permission request ...`` lines. The handler now checks the
  inbox for the incoming ``event_id`` first and no-ops on a match.
- Fixed a startup race where the minds desktop client could cache a
  stale latchkey gateway port and then fail every subsequent call
  with ``[Errno 111] Connection refused``. The race occurred because
  the supervisor restart and the gateway-client pre-warm previously
  ran on independent background threads at minds startup: the
  gateway client could observe the previous supervisor's record
  (still on disk, still alive) before the restart deleted that
  record and stamped the fresh port. Two fixes:
  - ``LatchkeyGatewayClient`` now self-heals from a stale cached
    gateway URL on connect-level transport failures
    (``httpx.ConnectError`` / ``httpx.ConnectTimeout``): the cached
    URL is invalidated and the next call re-resolves the port from
    the supervisor's on-disk record. Non-connect errors (read
    failures mid-stream, 5xx responses, etc.) continue to propagate
    without invalidation, since those usually indicate a problem at
    the gateway end rather than a stale local cache.
  - The supervisor restart and the gateway-client pre-warm now run
    sequentially on a single background thread, eliminating the
    race in the first place. App startup is unaffected: this still
    runs in a background thread, so the supervisor restart's 10s
    SIGTERM grace never blocks the foreground startup path.
- The latchkey permission dialog no longer pre-checks the catch-all
  ``any`` permission as an implicit default. ``any`` is still offered
  as the first checkbox so the user can opt into unrestricted access
  explicitly, but the initial check state is now the union of (a)
  permissions already granted for the scope on the agent's host and
  (b) the permissions the agent declared in the request event.
  Approving without modification therefore grants exactly that union
  (matching the user's mental model of "give the agent what it's
  asking for, on top of what it already has"). Previously, existing
  grants alone seeded the pre-check and the agent's new ask was
  ignored unless the user actively ticked it; under the new behavior
  an unmodified Approve actually delivers the requested permissions.

Update `apps/minds/docs/staging-bringup.md`'s changelog-entry checklist item to reflect the new per-project layout (`changelog/minds/<branch-name>.md` instead of `changelog/<branch-name>.md`).

Batch of `minds env deploy` / connector follow-ups from the F-numbered
findings in `MANUAL_DEPLOY_FINDINGS.md`:

- ``minds env deploy``'s post-deploy health check now polls the connector's
  new ``GET /health/liveness`` route instead of ``/docs`` (smaller, faster,
  symmetric with the LiteLLM proxy's existing liveness probe). The
  per-attempt HTTP timeout bumped from 3s to 10s and the total budget
  from 30s to 60s so cold-booting Modal containers have a realistic
  chance to respond before being declared unhealthy. (F2, F3)
- ``DeployLifecycleConfig`` has a new pydantic model validator that
  rejects ``writes_local_state=true`` + ``creates_resources=false``
  at deploy.toml parse time. The combination would previously have
  AssertionError'd partway through deploy AFTER both Modal apps had
  been deployed; failing at config load is far less surprising. The
  matching asserts in ``deploy_env`` stay as defense-in-depth for
  non-CLI callers. (F18)
- ``minds env deploy`` runs ``apply_pool_hosts_migrations`` for every
  tier instead of only the dev tier. Shared tiers (staging /
  production) source the host_pool DSN from ``DATABASE_URL`` in their
  operator-managed ``secrets/minds/<tier>/neon`` Vault entry. Without
  this, a new ``.sql`` migration shipped via PR would apply to dev
  envs immediately but never to staging / production until the
  operator ran psql manually -- and the two schemas would diverge.
  (F17)
- ``minds env destroy`` proceeds with cloud-side cleanup even when the
  local env root has already been removed by hand. The cloud-side
  resources are keyed off the env *name*, not the local directory, so
  an operator who ``rm -rf``'d ``~/.minds-<env>/`` can still re-run
  destroy by name to clean up Modal apps / Neon / SuperTokens /
  Cloudflare tunnels / OVH instances. ``destroy_env`` no longer
  raises ``DevEnvNotFoundError`` for missing-root; it logs a warning
  and proceeds. Step 1 (``mngr destroy`` per agent) becomes a no-op
  since the agents directory is gone too. (F22)
- ``per_env_connector_url`` / ``per_env_litellm_proxy_url`` now take
  the ``tier`` as a keyword arg. The dev URLs stay shaped as
  ``rsc-dev`` / ``llm-dev`` so existing per-env deployments keep
  working without a redeploy, but any future ``PER_ENV`` tier other
  than dev gets the right ``rsc-<tier>`` segment automatically
  instead of silently colliding on the hardcoded ``dev`` segment.
  (F24)
- ``minds env deploy``'s ``find_monorepo_root`` check happens BEFORE
  the Vault credential read in the CLI and BEFORE
  ``make_deploy_id`` inside ``deploy_env``. Running from outside
  the monorepo now fails immediately with a clean error rather than
  reading Vault first and logging a misleading "Deploy id: ..."
  line. (F15)
- ``minds env list`` resolves the reserved tiers' (``production`` /
  ``staging``) client.toml to the committed in-repo
  ``apps/minds/imbue/minds/config/envs/<tier>/client.toml`` instead
  of showing ``(no client.toml)``. The ``DevEnvSummary`` gains a
  ``client_config_source: "env_root" | "in_repo" | None`` field so
  machine consumers can distinguish "per-env file" from "in-repo
  file" from "unprovisioned." Human-format output now reads
  ``<path>  (in-repo, committed)`` for reserved tiers and
  ``(no client.toml -- run `minds env deploy`)`` for unprovisioned
  dev envs. (F11)
- The ``litellm-connector`` Modal Secret no longer appears in
  ``[secrets].services`` -- it was never vault-backed (no
  ``secrets/minds/<tier>/litellm-connector`` Vault entry exists),
  and the carve-out (``_DERIVED_ONLY_SECRET_SERVICES``) that
  suppressed a misleading per-deploy warning was a code smell. The
  deploy now pushes the secret as a separate code-driven step at
  the end of the secret-push loop; ``_DERIVED_ONLY_SECRET_SERVICES``
  is deleted. ``[secrets].services`` in every tier's deploy.toml
  becomes a truthful "vault-backed only" list. The post-deploy GC
  picks up ``litellm-connector-<tier>-<deploy_id>`` secrets via the
  same suffix-match pattern, so no GC bookkeeping changes. (F25)
- Recover changes: when the captured pre-deploy app version is
  ``None`` (a first-ever deploy of this env / tier), ``minds env
  recover`` now ``modal app stop``s the deployed app instead of
  leaving it running -- otherwise the just-deleted Modal Secrets
  would leave the app 500'ing on every request. (F19)
- The recover-target file is per-env: each in-flight deploy gets
  its own ``.minds-deploy-recover-target-<env>.json`` at the
  monorepo root, so concurrent deploys against different envs don't
  refuse each other (useful for parallel test runs). The
  environment-scoped commands (``deploy`` / ``destroy``) refuse only
  if THEIR env's file exists; the env-agnostic commands (``activate``
  / ``deactivate`` / ``list``) still refuse loud if ANY recover-
  target file exists (listing all in the error). ``deploy_env`` and
  ``recover_env`` additionally hold a per-env ``flock`` on
  ``.minds-deploy-lock-<env>.lock`` for their entire process
  lifecycle, so two concurrent invocations against the same env
  serialize at the kernel level. (F26)
- Doc / spec updates: comment on the connector's ``/generation`` env
  var now explains empty-string is the steady state for
  ``tracks_generation=false`` tiers (not a legacy artifact). Spec
  ``specs/minds-deploy-safety-overhaul/spec.md`` updated to use
  branch-based Neon-snapshot terminology (the implementation pivoted
  from the spec's original named-restore-point design because Neon's
  named-restore-point API is org-tier-gated) and to refer to
  ``/health/liveness`` on both apps. (F1, F4, F9, F20)

Minds dev-environment fixes:

- Hard-enforces the `dev-<your-user>` naming convention for dev envs:
  `DevEnvName` rejects anything that does not start with `dev-`, and
  `MINDS_ROOT_NAME_PATTERN` only accepts `minds`, `minds-staging`, or
  `minds-dev-<rest>`. Dev env roots come out tier-first as
  `~/.minds-dev-<your-user>/` and `MINDS_ROOT_NAME=minds-dev-<your-user>`.
- `minds env activate` now exports `MODAL_PROFILE` derived from the
  activated tier's committed `modal_workspace`. Every subsequent
  `modal` CLI shellout (deploy, secret create, environment create) is
  pinned to the right workspace regardless of which profile is marked
  `active = true` in `~/.modal.toml`. Prerequisite: the operator must
  have a matching profile in `~/.modal.toml` for each tier
  (`modal token set --profile <workspace>` once per tier). Skipped
  when the tier's `modal_workspace` is still the literal `CHANGE_ME`
  placeholder.
- `min_containers` for the deployed `remote-service-connector-<tier>`
  and `litellm-proxy-<tier>` Modal apps is now driven by a tier's
  committed `deploy.toml` via a new `[min_containers]` block (fields:
  `connector`, `litellm_proxy`). Defaults to 0 in the Pydantic model;
  staging / production deploy.toml ship with `1` for both. The values
  thread into `modal deploy` as `MINDS_CONNECTOR_MIN_CONTAINERS` /
  `MINDS_LITELLM_PROXY_MIN_CONTAINERS`, which the modal app modules
  read at import time.
- Per-dev-env Neon **project** (not just a database): each dev env
  now owns a brand-new Neon project named `minds-<env>` under the
  dev-tier Neon org, containing two databases (`host_pool` and
  `litellm_cost`). `minds env deploy` provisions the project and
  applies the `pool_hosts` schema (via `apps/remote_service_connector/
  migrations/*.sql`) to `host_pool` automatically. `minds env destroy`
  deletes the project outright -- atomic teardown of both DBs, roles,
  and the project's pooler endpoint.

  The deploy now overrides BOTH `neon.DATABASE_URL` and
  `litellm.DATABASE_URL` in the per-env Modal Secrets with the per-env
  project's two DSNs, so the connector and the LiteLLM proxy talk to
  the same env-isolated Neon project. The per-env `secrets.toml` on
  disk grows two fields (`NEON_HOST_POOL_DSN`, `NEON_LITELLM_DSN`,
  replacing the single `NEON_POOLED_DSN`).

  Vault `secrets/minds/<tier>/neon-admin` now expects `NEON_ORG_ID`
  (instead of `NEON_PROJECT_ID`). The token must have project-create
  scope on the dev tier's Neon org.

  `mngr imbue_cloud admin pool create` and friends now auto-resolve
  `--database-url` from the activated minds env's `NEON_HOST_POOL_DSN`
  (or `MINDS_HOST_POOL_DSN` env var), so the standard dev-env flow no
  longer requires passing the DSN explicitly. Operators outside an
  activated env still pass `--database-url` directly.

  Staging / production keep the tier-shared single-DB model unchanged.

- Added a `secrets/minds/<tier>/ovh` Vault template (AK / AS / CK) and
  documented the manual provisioning step in
  `apps/minds/docs/vault-setup.md` and
  `apps/minds/docs/host-pool-setup.md`.

- `minds env deploy` is now actually idempotent against Neon. The
  Neon REST API does not 409 on duplicate project names within an
  organization -- POSTing `/projects` with a name that's already in
  use creates a second, distinct project with the same name and a
  different id. The previous `create_neon_project` assumed Neon would
  409 (the adopt-fallback path was never reached), so every dev-tier
  re-deploy silently leaked an entire Neon project (with its own
  host_pool + litellm_cost DBs + branches + endpoints). Several
  attempts at deploying dev-josh-1 during one session today left
  four projects named `minds-dev-josh-1` in the dev org. The same
  bug would have caused `minds env destroy` to delete the wrong
  project (always the first match from the list endpoint, i.e. the
  oldest, not the live one), leaving the live project stranded.
  `create_neon_project` and `delete_neon_project` now look up by
  name first via `_find_projects_by_name`, adopt when there's
  exactly one match, raise a `NeonProviderError` with every
  matching project id + creation timestamp + a copy-pasteable
  cleanup recipe when there are several. Refusing-loud is
  intentional: silently picking one would risk destroying the wrong
  project under a real name collision (e.g. two devs using the same
  env name cross-machine). A new `_select_one_or_raise_multi_match`
  pure helper carries the decision logic; the operator-facing error
  message is unit-tested.

Minds deploy safety overhaul (spec
`specs/minds-deploy-safety-overhaul/spec.md`):

- Shorter Modal app + function names so the deployed hostname stays
  under DNS's 63-char limit for every realistic env name:
  `remote-service-connector` -> `rsc`, `fastapi_app` -> `api`,
  `litellm-proxy` -> `llm`, `litellm_app` -> `proxy`. Modal workspaces
  rename to `minds-dev` / `minds-staging` / `minds-production`. URL
  is now exactly what we compute up front, so the deploy no longer
  runs a second-pass secret push or a connector redeploy. `DevEnvName`
  enforces a 40-char max so the hostname budget always fits.

- One `minds env deploy` path for every tier, driven by a new required
  `[lifecycle]` block in each tier's `deploy.toml` (flags:
  `creates_resources`, `modal_env_strategy`, `writes_local_state`,
  `tracks_generation`). dev / staging / production all execute the
  same code now; behavior diverges only via the flag matrix.
  `deploy_dev_env` + `deploy_tier_env` collapse into `deploy_env`.
  Inline best-effort rollback machinery (`_best_effort_rollback`,
  `_ROLLBACK_TABLE`, `_rollback_*`) deleted -- replaced by
  `minds env recover` (below). Production now `tracks_generation=true`
  for parity with staging (production destroy is hard-refused so the
  generation id is effectively immutable for the tier's lifetime).

- Pool-hosts schema migrations now backed by a real
  `schema_migrations(version, applied_at)` table instead of the old
  "replay every .sql with IF NOT EXISTS guards". New
  `apps/minds/imbue/minds/envs/migrations.py` owns the runner. Legacy
  files keep their `IF NOT EXISTS` guards so a backfill against an
  already-migrated DB is a no-op + records the row; new migrations
  land WITHOUT guards (the table is the source of truth).

- Every `minds env deploy` mints a fresh `MINDS_DEPLOY_ID` (UTC
  `YYYYMMDDTHHMMSSZ`) and pushes every Modal Secret under a new name
  `<svc>-<tier>-<deploy_id>` (no overwrites). The deployed Modal apps
  read `MINDS_DEPLOY_ID` at module load and pin every
  `Secret.from_name(...)` to the matching timestamped name. Hard-fails
  at module load if `MINDS_DEPLOY_ID` is missing (no fallback to
  unsuffixed names; manual `modal deploy` outside `minds env deploy`
  is unsupported). End-of-deploy GC keeps the last 10 timestamped
  secrets per `<svc>-<tier>`; shared-tier destroy deletes all
  timestamped secrets matching the tier.

- New `minds env recover` command + recover-target file at the
  monorepo root. Every deploy captures pre-deploy Modal app versions,
  creates a Neon snapshot branch (`pre-deploy-<deploy_id>` off the
  default branch -- COW so it's near-free), and writes
  `.minds-deploy-recover-target.json` (gitignored) atomically BEFORE
  touching any external state. On a failed deploy, the operator runs
  `minds env recover`; it idempotently runs every reversal step
  (`modal app rollback`, Neon branch-restore from the snapshot with
  the pre-restore state preserved under `pre-rollback-<deploy_id>`,
  delete orphan timestamped secrets, delete the recover-target file).
  Successful deploys delete the snapshot branch (best-effort cleanup).
  Every other `minds env *` command (`activate` / `deactivate` /
  `list` / `deploy` / `destroy`) refuses to run while a recover-
  target file exists.

  Snapshot/restore works for every tier (dev creates_resources=true
  and shared creates_resources=false). Shared tiers (staging /
  production) require `NEON_PROJECT_ID` in their
  `secrets/minds/<tier>/neon-admin` Vault entry; the deploy resolves
  the default branch on demand. Without `NEON_PROJECT_ID` shared-tier
  deploys log a warning and skip the snapshot (so recover can roll
  back Modal apps but not the DB).

- Post-deploy health check: `await_apps_healthy` polls
  `<connector>/docs` and `<litellm_proxy>/health` for up to 30s each
  (sequential), with cold-boot 5xx tolerance + immediate failure on
  4xx / 5xx-with-body / wrong-shape responses. Failure surfaces as
  `HealthCheckFailedError` and goes through the same "run
  `minds env recover`" guidance as any other deploy failure.

- Each deploy also gets a `[lifecycle].tracks_generation=true` tier
  generation id minted into the litellm-connector Modal Secret (no
  change for dev / staging; production now also gets one).

Operator-visible: re-deploys after any of the above are
backwards-compatible against the existing dev-tier resources. The
shared (`staging` / `production`) tiers' `deploy.toml` files now
require a `[lifecycle]` block; operators bringing up staging /
production for the first time need to populate the existing OAuth
client IDs as before plus ensure the `[lifecycle]` block matches the
defaults documented in the committed file.

Speed up local minds workspace creation by restructuring the `forever-claude-template` Dockerfile and deferring Playwright into a post-boot install. The bulk of this change lives in the `forever-claude-template` repo (see `mngr/faster-minds-build` over there); this monorepo PR carries the spec (`specs/faster-minds-build/concise.md`) and a one-line mention in `apps/minds/docs/design.md`.

What changes for end users:

- Cold (no Docker layer cache) image builds drop the Playwright + Chromium install from the Dockerfile entirely. That step was downloading ~280 MB of browser assets plus apt-installing system libraries on every cold build; it now runs once on first container boot via a new `deferred-install` service.
- Warm-cache rebuilds after a code-only edit (no manifest changes) no longer invalidate the heavy `uv sync --all-packages` and `npm ci` layers. The Dockerfile now copies dependency manifests in an early layer, runs `uv sync --frozen --no-install-workspace --no-install-local` to pre-warm the wheel cache + `npm ci` for the frontend, and only then does `COPY . /code/`. Post-`COPY` `uv sync` collapses to ~1.5s because the warmed cache covers every third-party wheel; `npm run build` similarly reuses cached `node_modules`.
- Drop the post-`COPY` recursive `chown -R root:root /code/` step. `COPY` without `--chown` already lands files as root:root, so the chown was a no-op walk over the entire (~250 MB, including `.git/`) source tree -- worth ~60s on every warm-cache rebuild. Measured warm-rebuild (single Python edit, all pre-`COPY` layers cached): **1m33s -> 30s**.
- Drop `mngr_modal` from the post-`COPY` `uv tool install -e apps/system_interface --with-editable ...` chain and from `mngr plugin add --path ...`. The FCT `.mngr/settings.toml` sets `providers.modal.is_enabled = false` and no Python in `apps/` or `libs/` imports `imbue.mngr_modal`, so the plugin was load-bearing for nothing. `mngr plugin add` shells out to a uv-tool inject per plugin, so trimming one plugin saves a measurable amount. Brings warm rebuild to **~25.6s** total.
- Playwright's Chromium browser installs asynchronously on first boot via a new `services.toml` entry `deferred-install` (running `scripts/deferred_install.sh`). The script is idempotent: per-package marker files under `/var/lib/minds/deferred-install/done.<package>` gate every install, so subsequent container restarts no-op in milliseconds and packages never silently upgrade between restarts. Container rebuilds wipe the marker so the install runs exactly once on a fresh image.
- The `forever-claude-template` `.dockerignore` is now a symlink to `.gitignore` (Docker reads the symlink target). `.gitignore` patterns were rewritten to start with `**/` (or contain a path separator) so the same patterns work in both formats; two new ratchets in `test_meta_ratchets.py` (`test_gitignore_patterns_use_double_star`, `test_dockerignore_is_symlink_to_gitignore`) keep the convention enforced.

If a process tries to use Playwright before the deferred install has finished, it will fail loudly -- that is acceptable. `forever-claude-template/CLAUDE.md` documents how to check the marker file or the `svc-deferred-install` tmux window before exercising browser automation in a fresh workspace.

Out of scope for this PR (kept for follow-ups): BuildKit cache mounts for the `uv` / `npm` wheel caches across image rebuilds; pulling the same restructuring into the lima provider's `.mngr/settings.toml` `create_templates.lima.extra_provision_command`; deferring other "nice but not required" packages (e.g. `modal` CLI, apt convenience tools); generalizing the deferred-install marker pattern into a small framework.

End-to-end fixes for the OVH-backed imbue_cloud pool flow (`minds pool create` -> `mngr imbue_cloud admin pool create` -> bake -> lease/adopt -> first-start). Discovered + fixed iteratively while smoke-testing the flow against a fresh dev env (`dev-josh-ovh`).

### `minds pool create` auto-injects tier secrets

- `minds pool create` reads the activated tier's OVH AK/AS/CK from Vault (`<vault_path_prefix>/ovh`) and injects them into the inner `mngr imbue_cloud admin pool create` subprocess. Operators no longer need to export `OVH_APPLICATION_KEY` / `OVH_APPLICATION_SECRET` / `OVH_CONSUMER_KEY` before baking pool hosts; activating a minds env is sufficient. Vault values win over any stale `OVH_*` in the shell so a session left over from a different tier's bake can't silently misroute the OVH order.
- `--management-public-key-file` is now optional. Default behavior derives the public key from the activated tier's `<vault_path_prefix>/pool-ssh.POOL_SSH_PRIVATE_KEY` Vault entry -- the same private key the deployed connector loads from its `pool-ssh-<tier>` Modal Secret. Closes the keypair-mismatch class of bakes that succeeded locally but failed every subsequent lease with "Authentication failed" at the connector's SSH-key-injection step (the operator's hand-rolled pub key didn't match the connector's stored priv key). The flag stays available as an operator escape hatch for one-off / non-vault setups.

Deploy-safety overhaul: three correctness fixes to `_deploy_env_locked` discovered while auditing PR #1671 (full audit in `DEPLOY_SAFETY_AUDIT.md`).

- **F1**: Neon snapshot + recover-target file write now happen BEFORE pool-hosts migrations run. Previously migrations ran first, so the snapshot captured the post-migration state and `minds env recover` could not roll back a bad migration -- especially dangerous for shared tiers (staging/production) where the operator-managed DB likely has live traffic. The new ordering: capture app versions → resolve Neon project → verify token scope (F2) → snapshot → write recover-target (with F4 cleanup-on-failure) → migrations.
- **F2**: `providers.verify_neon_token_has_restore_scope(...)` is now actually called as a preflight check, right after Neon project resolution and before snapshot creation. It was declared on the Providers bundle and wired to the real implementation but had zero callers in the deploy path. Stale/misconfigured Neon tokens now fail at the cheapest possible probe (a `GET /projects/{id}` call) before any mutation, instead of only surfacing at `minds env recover` time after the deploy had already started rolling forward.
- **F4**: `write_recover_target_atomic` is now wrapped in a `try/except (OSError, MindError)` that best-effort deletes the just-created Neon snapshot branch before re-raising. Closes a window where a successful snapshot followed by a failed local file write (disk full, ENOSPC, permission denied, fsync failure) would orphan the snapshot branch with no `recover-target` file pointing at it. Cleanup failure is logged loudly so the operator knows the branch needs manual deletion; the original write error still propagates as the user-visible exception.

Each fix has two new ratchet tests in `provisioning_test.py` pinning the invariant (snapshot-before-migration for dev + shared tier; verify-before-snapshot happy path + short-circuit on scope failure; snapshot cleanup on write failure + on compounded cleanup failure).

Spec + scaffolding: design and initial wiring for live integration / acceptance / release testing of the minds app, its deployed remote services, and the deployment process itself. Introduces an operator-invoked `just minds-test-deployment` orchestrator (plain-Python click CLI) that stands up shared dev envs and runs two pytest batches strictly sequentially via local `uv run pytest` (one per mark: `minds_deployment`, `minds_services`), and reliably cleans up every resource it creates via both a per-run ledger and a `ci-<timestamp>` name+age sweep. Offload-Modal parallelism is designed in but deferred to a follow-up. See `specs/minds-deployment-tests.md` for the full design.

`minds env deploy` now picks the Modal deploy strategy (rollover vs recreate) from context, with operator overrides via `--hard` / `--soft`. Default policy: recreate when a migration ran or the target tier is `dev` (covers personal dev envs + CI ephemeral envs), rollover for staging / production with no migration. Adopts Modal's `--strategy=recreate` flag from 1.4.x so the warm prior-version container no longer keeps serving traffic for several minutes after the swap on dev-tier deploys.

- Added `apps/minds/docs/staging-bringup.md`, an end-to-end checklist
  for standing up the `staging` minds tier from scratch (cloud
  account creation, Vault population, first-time `minds env deploy
  --yes-i-mean-staging`, and local smoke-test against the new tier).

Swap the `minds env destroy` walker from Vultr to OVH:

- New top-level `minds pool` CLI group (`create` / `list` / `destroy`). It requires an activated minds env, auto-injects `--tag minds_env=<active-env>`, and shells out 1:1 to `mngr imbue_cloud admin pool ...`.
- `minds env destroy` swaps its Vultr `/instances` walker for an OVH IAM v2 walker (matches by `tags["minds_env"] == <env>` and terminates via `OvhVpsClient.destroy_instance`). The dev-tier Vault path is now `<tier>/ovh` with `OVH_APPLICATION_KEY` / `OVH_APPLICATION_SECRET` / `OVH_CONSUMER_KEY`.

The orphaned `apps/minds/imbue/minds/cli/pool.py` duplicate (pre-`mngr_imbue_cloud`) and `apps/minds/imbue/minds/envs/providers/vultr_tags.py` are deleted in the same change. Existing Vultr-backed `pool_hosts` rows are not migrated automatically; operators destroy / drop them by hand after merge.

Move minds to multi-environment deploys (`dev`, `staging`, `production`) backed by HCP Vault, and reshape every env around a per-env data root. Each env now owns one directory: `~/.minds/` for production and `~/.minds-<env-name>/` for every other env (staging, plus any per-developer dynamic dev env). Each root holds that env's own mngr profile, agents, auth, logs, and (for dev envs) a split chmod-0600 `secrets.toml` next to a public `client.toml`. The pre-refactor shared `~/.devminds/` layout is gone -- `rm -rf ~/.devminds/` when convenient. `MINDS_ROOT_NAME` validation tightens to `minds(-<env-name>)?`; legacy values like `devminds` are silently treated as unset with a warning so a stale shell falls back to production rather than blowing up.

`minds env` is reorganized around explicit shell activation. New `minds env activate <name>` exports `MINDS_ROOT_NAME` + the derived `MNGR_*` vars + `MINDS_CLIENT_CONFIG_PATH` for `eval` (staging/production point at the in-repo committed `client.toml`; dev envs at the per-env `~/.minds-<name>/client.toml`); new `minds env deactivate` unsets them. A `--create` flag on `activate` idempotently mkdirs the env root for fresh dev envs so first-time bootstrap is one line: `eval "$(minds env activate --create <your-user>-dev)" && minds env deploy`. `minds env deploy` and `minds env destroy` no longer take a name argument -- they operate on the currently-activated env and refuse loudly when nothing is activated. `minds env destroy` supports staging (gated by `--yes-i-mean-staging`; stops the deployed Modal apps and removes the env root, leaving operator-managed Vault/Neon/SuperTokens state in place) and hard-refuses production regardless of any flag. `minds env list` globs `~/.minds*/` directly so every env on disk shows up regardless of deploy state.

All deploys flow through `minds env deploy`. The standalone `scripts/deploy_remote_service_connector.sh`, `scripts/deploy_litellm.sh`, and `scripts/push_modal_secrets.py` are removed; their work folds into the unified CLI. Tier deploys (staging / production) require a mandatory `--yes-i-mean-<tier>` flag, push Vault secrets straight to Modal, and run `modal deploy` for both apps -- writing nothing to disk because the committed in-repo `client.toml` is the source of truth for those tiers. Dev env deploys also write `~/.minds-<name>/{client.toml,secrets.toml}` so re-deploys can find their per-env state.

`minds run` (and `propagate_changes`, and every justfile recipe that touches mngr state) refuse without an activated env. No implicit fallback to a hardcoded dev `client.toml`; the dev tier's static `client.toml` is deleted entirely (only `dev/deploy.toml` remains). The packaged Electron build drops `MINDS_BUILD_TIER` in favor of explicit `MINDS_CLIENT_CONFIG_BUNDLE=<path>` + `MINDS_ROOT_NAME_BUNDLE=<minds(-<env-name>)?>`; the runtime exports `MINDS_ROOT_NAME` from the embedded value and passes `--config-file` from the embedded path so a beta or staging build never collides on disk with an installed production build. `just devminds-start` and `forward-{minds,devminds}-system-interface` are gone -- replaced by a single env-agnostic `just minds-start` and `forward-system-interface` that read the activated env from the shell.

`minds env destroy` now actually destroys everything `deploy` created, plus clears the env-specific data accumulated inside operator-managed shared resources (so the next deploy starts from a clean slate). For every env destroy: `mngr destroy` every agent under the env's mngr profile first, then walk the cloud-side teardown, and only `rmdir ~/.minds-<env-name>/` if every cloud step succeeded -- a partial failure leaves the env root in place so re-running picks up where things broke. Dev env destroy deletes the per-env Modal env (cascade-deletes apps/secrets/volumes), Neon DB, and SuperTokens app outright; the new staging tier destroy (gated by `--yes-i-mean-staging`) `modal app stop`s both apps, `modal secret delete`s every per-tier Secret, wipes the SuperTokens app's users via delete+recreate of the same `app_id`, and `DROP SCHEMA public CASCADE`s the Neon DB via psql. Both paths now also enumerate + delete Cloudflare tunnels tagged with `metadata.env=<env-name>` (set by `cf_create_tunnel` at create time when the connector reads the new `MINDS_ENV_NAME` env var) and delete Vultr instances tagged `minds_env=<env-name>` (renamed from the dev-only `minds_dev_env`).

A new per-tier generation id is minted at deploy time, stored at `secrets/minds/<tier>/generation` in Vault, exposed by the deployed connector at `GET /generation`. `minds env activate` fetches the id and compares it against a per-env `last_seen_generation` marker on disk -- on mismatch (i.e. the tier got destroyed + redeployed since the dev last activated) the activation auto-wipes the env's `mngr/` / `auth/` / `logs/` subdirs so the dev's next `mngr list` / `minds run` doesn't surface stale state pointing at the (now-gone) previous deploy.

Also: minds shutdown is cleaner now (terminates the `mngr forward` subprocess before draining the concurrency group, so reader threads no longer time out on every clean exit); the browser auto-open lands directly on the login URL with the one-time code instead of the bare origin; `list_agents`' ABORT-mode failures are now properly attributed to the failing provider so minds' auto-disable-on-auth-error handler actually fires; and `scripts/push_vault_from_file.py` pipes values as JSON on stdin to avoid the vault CLI's `@`-as-file sigil. New docs at `apps/minds/docs/environments.md` and `apps/minds/docs/vault-setup.md` walk through the new operator workflow.

## 2026-05-14

## minds: switch permission management to the latchkey 2.9.0 gateway extensions

Latchkey 2.9.0 ships two new gateway extensions that this branch wires
into the minds desktop client (in coordination with `mngr_latchkey`):

- `permission_requests.mjs` -- per-process pending-permission queue.
  Agents `POST /permission-requests` when they hit a blocked service;
  the desktop client consumes `GET /permission-requests?follow=true`
  to learn about new requests and `DELETE /permission-requests/<id>`
  to clear them once granted or denied.
- `permissions.mjs` -- a `permissions.json` editor that operates on any
  file path inside `LATCHKEY_EXTENSION_PERMISSIONS_ROOT`. Used by the
  desktop client to apply per-host permission grants via
  `POST /permissions/rules?path=<host_file>&rule_key=<scope>`.

### Minds desktop client

- `cli/run.py` now blocks on `_wait_for_gateway_port` (which polls
  `LatchkeyForwardInfo.gateway_port` for a non-None value) before the
  FastAPI app is built, then derives the gateway password and mints
  the admin JWT in-process and constructs a `LatchkeyGatewayClient`
  shared by every code path that talks to the gateway extensions.
- New `PermissionRequestsConsumer` daemon thread streams
  `GET /permission-requests?follow=true` and feeds each pending
  request into the existing `RequestInbox`. The legacy
  `events.jsonl` callback now ignores `LATCHKEY_PERMISSION` lines
  because the extension owns that flow; non-latchkey
  `PERMISSIONS` events still go through the JSONL channel
  unchanged.
- `LatchkeyPermissionGrantHandler` applies grants via the new
  `permissions` extension (`POST /permissions/rules?path=...&rule_key=...`)
  and clears the pending gateway record via `DELETE
  /permission-requests/<id>` on both grant and deny.
- New `gateway_client.py`, `permission_requests_consumer.py`, and
  `testing.py` modules support the above; corresponding unit-test
  files exercise the HTTP wire shape and the streaming/translation
  paths.

### Compatibility

Agents that still post `LATCHKEY_PERMISSION` request events via the
old `events.jsonl` channel will no longer reach the minds inbox.
Migrating agents to the gateway-side `POST /permission-requests`
endpoint is a follow-up.

**minds**: split the services agent from the initial chat agent. The "primary" agent in a minds workspace now runs only the bootstrap and background services (its window-0 command is `sleep infinity && claude`, so claude never actually starts) and is hidden from the agent list in the UI. On first container boot the bootstrap creates a real chat agent named after the host, sends it `/welcome`, and writes `CLAUDE_CONFIG_DIR` to the host env so every subsequent agent (chat, worktree, worker) shares the services agent's Claude config dir (auth, plugins, marketplaces, sessions). Destroying chat agents no longer tears down services, and restarting services no longer kills chat agents. The workspace_server `/api/agents/<id>/destroy` endpoint refuses to destroy `is_primary=true` agents as a server-side guard. Existing pre-change workspaces are not migrated — re-create them.

Minds: the "Name" field on the create-project form now sets the *host* name (validated via mngr's `HostName` regex), not the agent name. The agent is always called `system-services`. The imbue_cloud connector grows a required `host_name` on `/hosts/lease` and `/hosts`. Sister change in `forever-claude-template` (matching branch) drops the now-unused `MINDS_WORKSPACE_NAME` from `[commands.create].pass_env`.

## 2026-05-13

# Latchkey state per-host (minds side)

When minds creates an agent, the Latchkey-related env vars
(`LATCHKEY_GATEWAY`, `LATCHKEY_GATEWAY_PASSWORD`,
`LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE`, `LATCHKEY_DISABLE_COUNTING`)
are now passed to `mngr create` via `--host-env` instead of `--env`, so
every agent that ever runs on the host shares the same gateway URL,
password, JWT, and permissions.

The on-disk permissions metadata moves accordingly: minds now stores
the per-agent `latchkey_permissions.json` under
`<latchkey-dir>/mngr_latchkey/hosts/<host_id>/` instead of
`<latchkey-dir>/mngr_latchkey/agents/<agent_id>/`. After `mngr create`
returns, minds reads the canonical `host_id` from the trailing JSONL
`created` event and points the opaque permissions handle (referenced
by the JWT minted at create time) at the new host-keyed path.

The minds UI's grant flow now resolves the request event's `agent_id`
to its `host_id` via the backend resolver before writing the grant; if
the resolver hasn't seen the agent yet (or only reports the static
`"localhost"` placeholder), the grant POST returns 503 so the UI can
retry instead of silently writing the grant to the wrong file.

## 2026-05-12

### Minds-side cleanups for the mngr-latchkey package extraction

- `apps/minds/imbue/minds/desktop_client/ssh_tunnel.py`: removed the
  now-unused `SSHTunnelManager` and supporting types (`ReverseTunnelInfo`,
  `_TunnelFailureState`, `_ForwardedTunnelHandler`, relay imports,
  reverse-tunnel health-check / backoff constants, and the internal
  `_ssh_connection_*` helpers). Kept `RemoteSSHInfo`, `SSHTunnelError`,
  `open_ssh_client`, and `_create_ssh_client` -- still used by
  `backend_resolver.py`, `forward_cli.py`, and the `MindsRemoteSSHInfo`
  adapter in `cli/run.py`. The matching test files
  (`ssh_tunnel_test.py`, `test_ssh_tunnel_leak.py`) moved to the new
  package along with the manager.
- `cli/run.py` and `desktop_client/agent_creator.py` rewired to import
  the latchkey types and helpers from the plugin and wrap the
  raising plugin entry points (`prepare_agent_latchkey`,
  `finalize_agent_permissions`) in try/except blocks that log a
  warning and continue agent creation -- preserving the prior
  end-to-end behaviour where a misconfigured latchkey installation
  does not abort agent creation, but making the choice explicit at the
  call site rather than buried inside the library.
- Three minds `test_ratchets.py` snapshots tightened
  (`while_true 1->0`, `time_sleep 2->1`, `broad_exception_catch 1->0`)
  to reflect violations that went away with the deleted code.

### Minds: spawn `mngr latchkey forward` as a detached subprocess

`apps/minds/imbue/minds/cli/run.py` no longer constructs
`SSHTunnelManager` / `LatchkeyDiscoveryHandler` /
`LatchkeyDestructionHandler` in-process; it instead calls
`LatchkeyForwardSupervisor.ensure_running()` at startup, which spawns
the canonical `mngr latchkey forward` process detached. Minds does
*not* call `supervisor.stop()` on shutdown -- the supervisor keeps
running across desktop-client restarts and the next minds session
adopts it. This matches how minds already treated the underlying
`latchkey gateway` subprocess.

Side effect: the `_LatchkeyDiscoveryAdapter` class in `cli/run.py` is
gone, plus its supporting `MindsRemoteSSHInfo` / `AgentId` imports.

## 2026-05-09

- Fixed: the `minds run` process no longer pegs a CPU after agents or hosts come and go. Reverse-tunnel bookkeeping in the desktop client's `SSHTunnelManager` (used for Latchkey gateways) is now pruned when an agent is destroyed -- so paramiko transport threads can exit instead of being kept alive by repeated re-establishment attempts -- and the 30s health-check loop applies per-tunnel exponential backoff and drops a tunnel after 10 consecutive failed repair attempts.

- Changed: the desktop client's `SSHTunnelManager` reverse-tunnel health check now retries broken tunnels forever (capped at one attempt per 5 minutes via the existing exponential backoff) instead of giving up after 10 consecutive failures. This matches the user-visible expectation that going offline overnight should still result in working tunnels in the morning.

- Removed `LaunchMode.DEV` from minds. The web create form, `/create`, and
  `/api/create-agent` now offer only `LOCAL`, `LIMA`, `CLOUD`, and
  `IMBUE_CLOUD`; submitting `launch_mode=DEV` returns 400. The DEV-only
  latchkey gateway helper, the `MINDS_ALLOW_HOST_LOOPBACK` env var, and the
  `allow_host_loopback` field on `ForwardSubprocessConfig` are gone (the
  generic `mngr_forward --allow-host-loopback` CLI flag stays for
  non-minds consumers).

Companion changes live in the forever-claude-template repo on the
same-named branch (`mngr/tweak-template`): default `~/.tmux.conf`
provisioning, `--cap-add=SYS_PTRACE` for the docker template, removal of
the unused `events_processor/` project, removal of `[create_templates.dev]`,
and the crystallization Stop hook is disabled.

## 2026-05-08

Removed `apps/minds_workspace_server/` from the monorepo. The workspace server (the FastAPI + dockview UI service that runs inside each agent's container) has been migrated to forever-claude-template, where it now lives at `apps/system_interface/` and ships as the `minds-workspace-server` CLI. Consumers (the minds desktop client and mngr) pick it up at runtime from the consumer's vendored forever-claude-template checkout instead of from this repo. Build-time impact: the release Dockerfile no longer cross-references the workspace server's frontend, and the node/npm install step that existed only to build it has been dropped. The `apps/minds/scripts/propagate_changes` dev-loop script now rsyncs from `/code/apps/system_interface/frontend/` in the running agent. User-facing docs (`apps/minds/docs/overview.md`, `apps/minds/docs/workspace/getting_started.md`) and the historical specs that referenced the old path were updated.

## 2026-05-07

- minds now injects `LATCHKEY_DISABLE_COUNTING=1` into every workspace
  whenever latchkey is wired (alongside `LATCHKEY_GATEWAY`,
  `LATCHKEY_GATEWAY_PASSWORD`, and `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE`).
  The workspace-side `latchkey` CLI runs in client mode against the
  host-side gateway, so suppressing its daily goatcounter.com ping
  prevents every agent from being counted as a separate active user --
  the single host-side gateway already represents the one real user.

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
- Old per-agent gateway records left under
  `~/.minds/agents/<id>/latchkey_gateway.json` are cleaned up
  automatically on desktop-client startup. Agents that were created
  with earlier minds versions need to be re-created to pick up the new
  env vars; without them their `latchkey` CLI calls will be rejected by
  the now-password-protected gateway.

## 2026-05-06

`apps/minds/scripts/propagate_changes` now protects `.claude/settings.local.json` from `rsync --delete` when syncing the template into an agent's work_dir.

That file is generated per-agent at create time by mngr's `_configure_agent_hooks` and holds the `UserPromptSubmit` hook that signals `tmux wait-for -S "mngr-submit-..."`. Without it, every `send_message` hangs the 90-second submission-signal timeout while the prompt is actually delivered to Claude (so the UI shows the message and Claude responds normally, but the HTTP `/message` request times out).

Previously the script only protected `runtime/` and `.mngr/`, so iterating with `propagate_changes` reliably reproduced the hang -- and there was no easy way to recover short of recreating the agent.

Fix WebSocket broadcaster queue-full flood and hung-send pin: stuck WS clients are evicted after 50 consecutive queue-full broadcasts, and the broadcaster cancels the wedged handler's asyncio task to free a coroutine blocked in `await websocket.send_text(...)` on a half-dead TCP connection. The previous behaviour pegged a CPU core and filled tmux with `WebSocket client queue full, dropping message` warnings whenever a single client stopped draining its queue.

Adds a spec for backing up the gitignored `runtime/` folder of forever-claude-template (which now also contains `memory/` and `tickets/`) into the same private repo on a separate orphan branch, plus a periodic backup service and `GH_TOKEN`-based auto-push setup.

- minds desktop client: when a discovery error from the connector indicates a revoked SuperTokens session for a specific imbue_cloud account, the matching `[providers.imbue_cloud_<slug>]` block is automatically marked `is_enabled = false` and `mngr observe` is bounced so the dead account stops poisoning subsequent discovery cycles. Signing back in (email/password or OAuth) re-enables the provider. The Manage Accounts page shows a "Signed out" badge + "Sign in again" link for any account whose provider is currently disabled.
- minds desktop client now installs a grandparent-death watcher when the Python backend starts: if Electron crashes (or is otherwise killed without running its on-quit handler), the Python backend self-terminates within ~3 seconds, and the cascade brings down its `mngr observe`/`mngr events`/latchkey children via their own watchers. Previously a crashed Electron left an orphan tree alive across restarts.
- minds: SIGTERM that minds itself sends to `mngr observe` / `mngr event` subprocesses (during shutdown, observe restart, or events-stream sync after an agent leaves the discovery snapshot) no longer surfaces as a "subprocess failed" notification.

- minds: redesigned the "Create a Project" screen.
  - Removed the "Include .env file" checkbox.
  - Added an "AI provider" choice (`imbue_cloud`, `api_key`, `subscription`) that is independent from the compute provider, so any combination is valid as long as `imbue_cloud` is paired with a selected account.
  - Renamed the "Launch mode" dropdown to "Compute provider"; both compute and AI provider default to `imbue_cloud` when an account is selected.
  - Selecting `api_key` reveals a required Anthropic API key field; `subscription` injects no Anthropic credentials so the user can sign in interactively after the workspace starts.
  - Selecting `imbue_cloud` for either field with no account is rejected by both the form (with a warning) and the server (with a 400).
  - Added an optional `GH_TOKEN` field under Advanced settings that is forwarded to the agent host (or the agent in DEV mode).

Cleanup pass after splitting functionality out of minds into the `mngr_imbue_cloud` and `mngr_forward` plugins.

- The "Share" button in a workspace now opens a static informational modal that points the user at the desktop app, rather than writing a sharing-request event back to minds. Direct sharing editing from the desktop client (workspace settings page) is unchanged. Permissions / latchkey request flows are unchanged.
- Minds no longer persists `imbue_cloud` account identity (email, display_name) to disk. Only the workspace<->account association map lives in `~/.minds/workspace_associations.json`; identity is sourced on demand from the new `mngr imbue_cloud auth list` command and cached in memory.
- Destroyed agents now disappear from the projects index automatically without requiring the user to click into the destroying detail page first.

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
  - Slimmed `desktop_client/app.py`: deleted the host-header subdomain-forwarding middleware and many supporting helpers; `create_desktop_client(...)` no longer takes `tunnel_manager`, `latchkey`, or `stream_manager`; it gains `mngr_forward_port` + `mngr_forward_preauth_cookie` so server-to-server refresh broadcasts route through the plugin.
  - Rewired `_dispatch_refresh_broadcast` to POST through the plugin's per-agent subdomain (`<agent>.localhost:<plugin_port>/api/refresh-service/<svc>/broadcast`) with the preauth cookie, instead of opening its own SSH tunnel.
  - `supertokens_routes._bounce_mngr_observe` → `_bounce_forward_observe`: sends `SIGHUP` via `EnvelopeStreamConsumer.bounce_observe()`. Dropped the legacy `MngrStreamManager` fallback.
  - Templates and static JS now point `/goto/<agent>/` links at the plugin's port via a `mngr_forward_origin` Jinja variable / `data-mngr-forward-origin` attribute.
  - Electron's `backend.js` exposes a new `onMngrForwardStarted` callback; `main.js` consumes the `mngr_forward_started` event from `minds run` stdout and pre-sets the `mngr_forward_session=<preauth>` cookie on `localhost:<plugin_port>` (default + content session) before any agent-subdomain navigation.
  - Updated user-facing references to `minds forward` → `minds run` in `apps/minds/README.md` and `apps/minds/docs/{design,desktop-app,overview,workspace/getting_started,workspace/glossary}.md`.

## 2026-05-05

- Fixed: closing the last tab in a minds workspace no longer leaves a blank screen with no recovery path. The primary agent's chat tab is automatically reopened when the dockview becomes empty (whether by closing all tabs at runtime or restoring an empty saved layout).
