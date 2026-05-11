# mngr_imbue_cloud plugin

## Overview

- Refactor all `remote_service_connector`-coupled logic out of the minds app and into a new `mngr_imbue_cloud` plugin so every imbue-cloud interaction flows through `mngr` commands.
- The plugin lives at `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/` and exposes one provider backend (`imbue_cloud`) plus a `mngr imbue_cloud` CLI command group with subgroups: `auth`, `hosts`, `keys litellm <…>`, `tunnels`, `admin pool`.
- Users get hosts, agents, keys, and tunnels via plain `mngr` commands instead of going through the minds Electron app. `LaunchMode.LEASED` and the bespoke pool flow in `apps/minds/imbue/minds/desktop_client/agent_creator.py` go away — minds picks `provider = imbue_cloud_<account>` in the create template instead.
- Multi-account is modelled as multiple provider *instances* of the same `imbue_cloud` *backend*. Each signed-in account is its own `[providers.imbue_cloud_*]` entry in `~/.mngr/config.toml` with a required `account` field. There is no "current default"; every CLI subcommand and every minds invocation picks an instance explicitly. `mngr list` aggregates across all configured `imbue_cloud_*` instances naturally.
- Today's minds clients (`auth_backend_client.py`, `host_pool_client.py`, `litellm_key_client.py`, `cloudflare_client.py`, and the parts of `session_store.py` covering signin/refresh) are deleted. Minds invokes `mngr imbue_cloud …` via subprocess for everything; no in-process plugin API is exposed.
- The connector schema is updated: `pool_hosts.version` is replaced with a flexible `attributes JSONB` column, and `/hosts/lease` matches with `attributes @> request_attributes` so callers can specify repo, branch/tag, cpus, memory, and gpu count.
- The leased-agent claim step (rename + relabel + env injection) is consolidated to **2 SSH round trips** to match the recently-merged minds optimization (`9eb5356a5`, `b65f52ac4`): one host-side `data.json` write via `OnlineHostInterface.rename_agent(labels_to_merge=…)`, and one bash `&&`-chained `mngr exec` for env writes. No reverting to the older parallel `mngr exec` layout.

## Expected Behavior

### CLI surface

- `mngr imbue_cloud auth signin --account <email>` — email/password flow; emits `{user_id, email, display_name}` JSON on stdout, exits 0 on success; non-zero with JSON error body on failure. Persists tokens under `<default_host_dir>/providers/imbue_cloud/sessions/<user_id>.json`.
- `mngr imbue_cloud auth signup --account <email>` — same shape as signin but creates a new account.
- `mngr imbue_cloud auth oauth google --account <email>` — opens system browser at the connector's `/auth/oauth/authorize` URL. The CLI itself runs a localhost listener for the OAuth callback, exchanges the code, persists tokens, and emits the same JSON. The full callback dance lives in the CLI; minds never handles tokens.
- `mngr imbue_cloud auth signout --account <email>` — calls `/auth/session/revoke` and removes local tokens.
- `mngr imbue_cloud auth status --account <email>` — prints session info as JSON.
- `mngr imbue_cloud auth refresh --account <email>` — manually force a token refresh.
- `mngr imbue_cloud hosts list --account <email>` — list current leased hosts (calls `/hosts`).
- `mngr imbue_cloud hosts release <lease-id> --account <email>` — manual escape hatch for an orphaned lease.
- `mngr imbue_cloud keys litellm create --account <email> [--alias <name>] [--max-budget <usd>] [--budget-duration <str>]` — emits `{key, base_url}` JSON on stdout.
- `mngr imbue_cloud keys litellm list/show/delete/budget` — list, inspect, delete, and update budget on a key.
- `mngr imbue_cloud tunnels create <agent> --account <email>` — create a Cloudflare tunnel for an agent.
- `mngr imbue_cloud tunnels list/delete --account <email>` — list and delete tunnels.
- `mngr imbue_cloud tunnels services add/list/remove --account <email>` — manage forwarded services on a tunnel.
- `mngr imbue_cloud tunnels auth get/set --account <email>` — get/set the default auth policy on a tunnel or service.
- `mngr imbue_cloud admin pool create/list/destroy` — operator-only commands; talk directly to Vultr + Neon DB from the operator's machine (same model as today's `apps/minds/imbue/minds/cli/pool.py`); the connector is not involved in pool provisioning.

### Provider backend behavior

- `mngr create --provider imbue_cloud_<account> …` selects that account's instance. The provider's `create_host` calls `/hosts/lease` with attributes derived from the user's request (repo url + branch/tag from git context; cpus, memory, gpu from build args).
- If no pool host matches the requested attributes, the connector returns 503 and `mngr create` fails with the connector's error message.
- On a successful lease, the plugin generates an SSH keypair, sends the public key in the lease body, and the connector injects it on the leased VPS (existing behavior).
- The provider returns an `ImbueCloudHost`. When `mngr create`'s flow calls `host.create_agent(…)`, the host short-circuits the normal "create a new agent" path: it looks up the pre-baked agent by the `agent_id` returned in the lease response (stashed on the host object), then renames + relabels + updates env on that agent rather than provisioning a fresh one.
- `mngr destroy <agent>` stops the docker container on the leased VPS only — the lease, host volumes, and on-disk data remain. `mngr start <agent>` brings the container back on the same VPS.
- `mngr delete <agent>` (or destroy followed by `delete_host`) calls `/hosts/{id}/release` and drops all on-disk plugin state for that host. `supports_snapshots = False` on day one.
- `discover_hosts` only returns the configured account's leases. To see all accounts, minds (or the user) configures one provider instance per account and `mngr list` aggregates them.
- If the configured account's tokens are missing or the refresh token is dead, the provider behaves like modal-without-auth: any operation that needs the connector fails with `HostAuthenticationError`, hosts under it appear in `UNAUTHENTICATED` state in `mngr list`. The user is prompted to re-run `mngr imbue_cloud auth signin --account <email>`. If the access token is merely expired but the refresh token is good, the plugin refreshes transparently before the operation.

### Minds desktop client behavior (post-refactor)

- Minds owns *zero* HTTP clients for the connector. Every operation goes through `subprocess.run(["mngr", "imbue_cloud", …, "--account", email])`.
- When a user signs in via the desktop UI, minds runs `mngr imbue_cloud auth oauth google --account <email>` (or signin variant) and parses the JSON it emits on stdout. It then writes a `[providers.imbue_cloud_<email-slug>]` entry into `~/.mngr/config.toml` with `backend = "imbue_cloud"` and `account = "<email>"`.
- When a user signs out, minds runs `mngr imbue_cloud auth signout --account <email>` and removes the corresponding provider instance entry.
- Agent creation in minds is reordered: minds first calls `mngr imbue_cloud keys litellm create --account <email>` to mint a virtual key, then runs `mngr create --provider imbue_cloud_<account> … --env ANTHROPIC_API_KEY=<key> --env ANTHROPIC_BASE_URL=<url>`. The "create then patch env file" flow disappears.
- The `WELCOME_INITIAL_MESSAGE` (`/welcome`) bake-in moves from `agent_creator.py` into `[create_templates.main]` of forever-claude-template's `.mngr/settings.toml`.
- Cloudflare tunnel actions in the Servers/Share UI become `mngr imbue_cloud tunnels …` subprocess calls; minds caches results between renders if needed.

### Connector behavior (server-side)

- `pool_hosts.version` is replaced with `attributes JSONB`. Migration drops the column and adds the new one.
- `LeaseHostRequest` adds an `attributes: dict` field; existing top-level fields shrink. SQL changes `WHERE version = %s` to `WHERE attributes @> %s::jsonb`.
- `LeaseHostResponse` is unchanged shape-wise; the response continues to carry `agent_id`, `host_id`, etc. needed by the plugin to claim the pre-baked agent.
- Other endpoints (`/auth/*`, `/keys/*`, `/tunnels/*`, `/hosts`, `/hosts/{id}/release`) are unchanged.

## Implementation Plan

### New plugin package `libs/mngr_imbue_cloud/`

- `pyproject.toml` — declares the entry point `[project.entry-points.mngr] imbue_cloud = "imbue.mngr_imbue_cloud.plugin"`. Depends on `imbue-mngr`, `imbue-mngr-vps-docker`, `httpx`, `loguru`, `pluggy`, `pydantic`, `paramiko` (for OAuth listener + admin pool SSH).
- `imbue/mngr_imbue_cloud/__init__.py` — only the `hookimpl = pluggy.HookimplMarker("mngr")` line.
- `imbue/mngr_imbue_cloud/plugin.py` — `@hookimpl` functions: `register_provider_backend()` returns `(ImbueCloudProviderBackend, ImbueCloudProviderConfig)`; `register_cli_commands()` returns the top-level `imbue_cloud` click group.

### Primitives, errors, data types

- `primitives.py` — `ImbueCloudAccount(NonEmptyStr)` (email-validated); `SuperTokensUserId(NonEmptyStr)`; `LiteLLMVirtualKey(SecretStr)`; `LeaseId(RandomId)` (or pass through DB UUID); enum `ImbueCloudKeyType` with `LITELLM = auto()` (room to add more).
- `errors.py` — `ImbueCloudError(MngrError)` base; subclasses `ImbueCloudAuthError`, `ImbueCloudLeaseUnavailableError` (maps to 503), `ImbueCloudConnectorError`, `ImbueCloudKeyError`, `ImbueCloudTunnelError`, `PoolHostNotMatchedError`. Inherit from the appropriate built-ins where relevant per the style guide.
- `data_types.py` — `LeaseAttributes` (FrozenModel: `repo_url`, `repo_branch_or_tag`, `cpus`, `memory_gb`, `gpu_count`, all optional; only set fields are sent to the connector); `LeaseResult` (FrozenModel mirroring `LeaseHostResponse`); `LiteLLMKeyMaterial` (`key: SecretStr`, `base_url: AnyUrl`); `TunnelInfo`, `ServiceInfo`, `AuthPolicy` mirrored from the connector's response shapes.

### Config

- `config.py` — `ImbueCloudProviderConfig(ProviderInstanceConfig)`: required `backend = "imbue_cloud"`, required `account: ImbueCloudAccount`, optional `connector_url: AnyUrl` (falls back to baked-in prod URL plus `MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL` env override resolved by mngr's standard env-from-settings convention).
- `_DEFAULT_CONNECTOR_URL` constant lives here. Adjusting it is a one-line change.

### Connector HTTP client

- `client.py` — `ImbueCloudConnectorClient(MutableModel)` wrapping `httpx`. Holds the `connector_url` and a reference to the session store (next bullet) so it can transparently refresh tokens before each request.
- Methods organized by concern:
  - **Auth:** `signup`, `signin`, `oauth_authorize`, `oauth_callback`, `refresh_session`, `revoke_session`, `get_user`, `send_verification_email`, `is_email_verified`, `forgot_password`, `reset_password`.
  - **Hosts:** `lease_host(attributes, ssh_public_key)`, `release_host(host_db_id)`, `list_hosts()`.
  - **Keys (litellm):** `create_key(metadata, max_budget, budget_duration, alias)`, `list_keys`, `get_key`, `update_budget`, `delete_key`.
  - **Tunnels:** `create_tunnel(agent_id, default_auth_policy)`, `list_tunnels`, `delete_tunnel`, `add_service`, `list_services`, `remove_service`, `get_tunnel_auth`, `set_tunnel_auth`, `get_service_auth`, `set_service_auth`, `create_service_token`, `list_service_tokens`.
- All methods take an `access_token: SecretStr` arg; the client never reaches into the session store directly — that's handled in a thin `_authenticated` wrapper that the CLI commands call.

### Session store

- `session_store.py` — `ImbueCloudSessionStore(MutableModel)` with `provider_data_dir: Path`. Persists per-user-id JSON files at `<provider_data_dir>/sessions/<user_id>.json` containing access_token, refresh_token, user_id, email, display_name. Multi-account by construction.
- Methods: `load_by_account(email) -> Session | None`, `save(session)`, `delete_by_account(email)`, `get_active_token(account)` which auto-refreshes via the connector client when expiry < 60s buffer; raises `ImbueCloudAuthError` if refresh fails.
- The provider's `provider_data_dir` resolves to `<default_host_dir>/providers/imbue_cloud/` per the standard convention (see `libs/mngr/imbue/mngr/providers/local/instance.py:111-117`). Sessions are *shared* across all `imbue_cloud_*` provider instances (keyed by user_id, not by instance name) — multiple instances of the same backend pointing at the same account share their tokens.

### Provider backend & instance

- `backend.py` — `ImbueCloudProviderBackend(ProviderBackendInterface)` with `get_name() -> "imbue_cloud"`, `get_description()`, `get_config_class() -> ImbueCloudProviderConfig`, `get_build_args_help()`, `get_start_args_help()`, `build_provider_instance()` returning an `ImbueCloudProvider`.
- `instance.py` — `ImbueCloudProvider(VpsDockerProvider)` (inherit from the existing VPS+docker provider, override the bits that matter):
  - `__init__` stores a `ImbueCloudConnectorClient` and the configured `account`.
  - `create_host(name, image, tags, build_args, start_args, lifecycle, …)` collects the user's request into `LeaseAttributes`, generates an SSH keypair to a temp path, calls `client.lease_host(attributes, ssh_public_key)`, renames the temp keypair into `<provider_data_dir>/hosts/<host_id>/ssh_key` once the lease succeeds. Returns an `ImbueCloudHost` carrying the `lease_result.agent_id` so `create_agent` can match it.
  - `destroy_host(host)` runs `docker stop` + `docker rm` on the leased VPS via SSH (keeps the VPS lease and on-disk data intact).
  - `delete_host(host)` calls `client.release_host(lease_id)`, drops the SSH keypair dir under `provider_data_dir/hosts/<host_id>/`, and lets the parent class clean up records.
  - `discover_hosts(cg, include_destroyed)` only consults `client.list_hosts()` for *this instance's* account.
  - `supports_snapshots = False`.
- `host.py` — `ImbueCloudHost(VpsDockerHost)` adds:
  - `pre_baked_agent_id: AgentId` (set by the provider after `create_host`).
  - `create_agent(options)` — when `options.id == self.pre_baked_agent_id` (or whenever `pre_baked_agent_id is not None` and the user didn't pass an explicit conflicting id), short-circuits to `_claim_pre_baked_agent(options)`. Otherwise raises `PoolHostNotMatchedError` (a leased imbue_cloud host is meant for exactly one agent, the pre-baked one).
  - `_claim_pre_baked_agent(options)` — the consolidated 2-round-trip claim step:
    1. Build the labels-to-merge dict from `options.labels`. Call `self.rename_agent(name=options.name, labels_to_merge={...})`. This is the single atomic data.json write that lands rename + labels together (uses the `9eb5356a5` capability).
    2. Build a single `&&`-chained bash command for env writes (mirrors today's `b65f52ac4` shape in `agent_creator.py:1456-1469`): MINDS_API_KEY (if requested), ANTHROPIC_API_KEY/BASE_URL (if `litellm_key`/`litellm_base_url` env vars are present in `options.env`), MNGR_PREFIX (if set), and the claude-config patch (when both keys are present). Run it via a single SSH exec.
    - The plugin does not call `mngr exec` as a subprocess; it uses the already-online host's exec API directly so there's exactly 2 host operations.

### CLI commands

- `cli/__init__.py` — top-level `imbue_cloud = click.Group()` with `auth`, `hosts`, `keys`, `tunnels`, `admin` subgroups.
- `cli/auth.py` — `signin`, `signup`, `oauth`, `signout`, `refresh`, `status`. The `oauth` command spawns a localhost HTTP listener on a free port, opens the browser at the connector's authorize URL, blocks until callback, exchanges code for tokens, persists, and prints user JSON. Implementation reuses `aiohttp`/`http.server` from stdlib for the listener.
- `cli/hosts.py` — `list`, `release`. Both take `--account` and use the session store + connector client.
- `cli/keys.py` — `keys litellm create/list/show/delete/budget`. Subgroup nested under `litellm` so adding new key types later is purely additive.
- `cli/tunnels.py` — `tunnels create/list/delete`, `tunnels services add/list/remove`, `tunnels auth get/set`. Mirrors the connector's endpoint surface.
- `cli/admin.py` — `admin pool create/list/destroy`. Pool-create is a near-port of `apps/minds/imbue/minds/cli/pool.py`: provisions Vultr VPS via `mngr create`, runs the same SSH key install + DB insert, but renamed to live under the plugin. Reuses Neon `DATABASE_URL`.
- All CLI commands follow the style in `libs/mngr/imbue/mngr/cli/` (use `click_option_group`, `add_common_options`, `setup_command_context`, `CommandHelpMetadata`, `add_pager_help_option`).

### Connector schema migration

- `apps/remote_service_connector/imbue/remote_service_connector/migrations/<n>_attributes_jsonb.sql` (or scripted equivalent) — drops `version`, adds `attributes JSONB NOT NULL DEFAULT '{}'::jsonb`, indexed with `GIN(attributes)` for `@>` queries. Existing rows are migrated by encoding their `version` into `attributes` (e.g. `{"version": "v1.2.3"}`), then the `version` column is dropped.
- `app.py` — `LeaseHostRequest` body adds `attributes: dict[str, Any]`. SQL updated to `WHERE status = 'available' AND attributes @> %s::jsonb ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED`. The pool-creation script (in the plugin) writes attributes when inserting rows.
- The corresponding admin-pool CLI in the plugin sends attributes when inserting.

### Removals / minds simplification

- Delete `apps/minds/imbue/minds/desktop_client/auth_backend_client.py` and its tests.
- Delete `apps/minds/imbue/minds/desktop_client/host_pool_client.py` and tests.
- Delete `apps/minds/imbue/minds/desktop_client/litellm_key_client.py` and tests.
- Delete `apps/minds/imbue/minds/desktop_client/cloudflare_client.py` and tests.
- Delete `apps/minds/imbue/minds/cli/pool.py` (port to plugin's `admin pool`).
- Trim `apps/minds/imbue/minds/desktop_client/session_store.py` to only what minds still needs (e.g. UI-side knowledge of which account is signed in for routing); the auth/refresh logic moves entirely into the plugin.
- Remove `LaunchMode.LEASED` from `apps/minds/imbue/minds/primitives.py`. `agent_creator.py`'s `_lease_host_synchronously`, `_setup_leased_agent`, `_setup_and_start_leased_agent`, `_cleanup_failed_lease`, dynamic-hosts toml writing, `_load_or_create_leased_host_keypair`, lease-info save/load, and the `WELCOME_INITIAL_MESSAGE` constant all go away.
- `runner.py` stops constructing `HostPoolClient`/`LiteLLMKeyClient`/`CloudflareClient`/`AuthBackendClient`. The `app.state` references are removed.
- `supertokens_routes.py` is rewritten to call `mngr imbue_cloud auth …` via subprocess instead of using the auth client.
- `WELCOME_INITIAL_MESSAGE` value moves into `forever-claude-template/.mngr/settings.toml` under `[create_templates.main]`.

### Tests / fixtures

- `libs/mngr_imbue_cloud/conftest.py` — fixtures that build a `MockImbueCloudConnector` (an in-process FastAPI fake) plus a `temp_imbue_cloud_session_store`.
- `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/testing.py` — `make_test_session()` factory + `mock_lease_response()`, `mock_litellm_key_response()` helpers.

## Implementation Phases

Each phase produces a working (possibly incomplete) system.

### Phase 1: Plugin skeleton + auth

- Stand up the package, `pyproject.toml`, `__init__.py`, `plugin.py` registering an empty backend stub.
- Implement `config.py`, `primitives.py`, `errors.py`, `data_types.py`.
- Implement `client.py`'s auth methods only.
- Implement `session_store.py`.
- Implement `cli/auth.py` (signin, signup, oauth, signout, refresh, status).
- Tests: round-trip a fake SuperTokens session through the store; run the OAuth listener against a mock authorize endpoint.
- Result: a user can `mngr imbue_cloud auth signin --account alice@example.com` and get a session file. Nothing else works yet.

### Phase 2: Connector schema migration + lease/release

- Apply the `attributes JSONB` migration (in a separate connector branch).
- Update `app.py`'s `LeaseHostRequest` and matching SQL.
- Add the host endpoints to `client.py` (lease_host, release_host, list_hosts).
- Implement `cli/hosts.py` (`list`, `release`).
- Tests: a release test against a staging Modal deployment plus integration tests against a fake connector. Verify `attributes @> request_attributes` matches as expected.
- Result: users can lease/release pool hosts via CLI. The provider doesn't exist yet.

### Phase 3: Provider + ImbueCloudHost + create flow

- Implement `backend.py` and `instance.py` (`ImbueCloudProvider` extends `VpsDockerProvider`).
- Implement `host.py`'s `ImbueCloudHost` with `pre_baked_agent_id` and the `_claim_pre_baked_agent` 2-round-trip path.
- Wire `register_provider_backend` to return the backend.
- Tests: integration test that fakes a lease, creates a host, calls `host.create_agent(options)` with a matching id, and asserts that exactly one `rename_agent` call (with merged labels) and exactly one SSH exec (with combined env writes) happen.
- Result: `mngr create --provider imbue_cloud_<account>` works end-to-end against a real lease pool.

### Phase 4: Keys + tunnels CLI

- Add key methods to `client.py`; implement `cli/keys.py`.
- Add tunnel methods to `client.py`; implement `cli/tunnels.py`.
- Tests: CLI tests that invoke each command and assert the correct connector calls.
- Result: minds (and any other consumer) has the full surface to mint keys and configure tunnels via CLI.

### Phase 5: Pool admin CLI

- Port `apps/minds/imbue/minds/cli/pool.py` to `cli/admin.py`'s `pool create/list/destroy`. Reuse Vultr+Neon flow as-is; just change the entry point.
- Tests: same shape as the existing pool tests (mocked subprocess + DB).
- Result: operators provision pool hosts with `mngr imbue_cloud admin pool create …`.

### Phase 6: Minds cutover

- Switch minds' lease/key/tunnel/auth flows to subprocess calls into `mngr imbue_cloud …`.
- Add `[providers.imbue_cloud_<email-slug>]` writer/remover in minds (a small TOML mutator) that hooks into signin/signout.
- Reorder minds' agent-creation flow to mint the LiteLLM key first, then `mngr create … --env ANTHROPIC_API_KEY=…`.
- Move `WELCOME_INITIAL_MESSAGE` into the forever-claude-template `[create_templates.main]` section.
- Delete the four dead clients, `cli/pool.py`, the `LaunchMode.LEASED` plumbing, and lease-related code in `agent_creator.py`. Trim `session_store.py`.
- Tests: e2e tests against the minds desktop client + a mock connector confirming the new flow.
- Result: minds is meaningfully smaller, all imbue-cloud access flows through the plugin, `mngr create --provider imbue_cloud_<account>` and the desktop UI both work.

### Phase 7: Documentation + plugin docs

- Add `libs/mngr_imbue_cloud/README.md` with setup, config, command reference.
- Update `apps/minds/docs/host-pool-setup.md` to reflect the new admin CLI.
- Update `libs/mngr/docs/concepts/providers.md` (if it lists built-ins) to mention `imbue_cloud`.
- Result: discoverable plugin from a clean clone.

## Testing Strategy

### Unit tests (`*_test.py`)

- `client_test.py` — for each connector method: fake the HTTP layer with `httpx.MockTransport` and assert the right URL, headers, body. Cover refresh-on-401 retry semantics for the auth wrapper.
- `session_store_test.py` — concurrent saves/loads, expiry detection, refresh path, missing-token error path. Use `temp_host_dir` for isolation.
- `host_test.py` — `ImbueCloudHost.create_agent` with mocked SSH host: assert exactly one `rename_agent(labels_to_merge=…)` call and one exec call, with the expected combined bash command. Cover the `PoolHostNotMatchedError` path when no `pre_baked_agent_id` is set.
- `data_types_test.py` — `LeaseAttributes` serialization (only set fields are included), `LiteLLMKeyMaterial` round-trip.
- `cli/*_test.py` — Click test runner driving each command against a mock connector + temp session store. Cover stdout JSON shape, exit code on failure, `--account` resolution.

### Integration tests (`test_*.py`, no mark)

- `test_imbue_cloud_lease.py` — full create flow against a fake in-process connector + a real `mngr_vps_docker` host fixture. Assert the host comes online, the pre-baked agent is renamed to the requested name, and labels appear atomically.
- `test_imbue_cloud_destroy_vs_delete.py` — `destroy_host` keeps the lease and on-disk data; `delete_host` calls `/hosts/{id}/release` exactly once. `mngr start` after destroy brings the container back.
- `test_imbue_cloud_oauth_listener.py` — drive the localhost listener with a synthetic browser request; ensure tokens land in the session store.
- `test_imbue_cloud_unauthenticated.py` — provider behavior when the configured account has expired refresh tokens: `discover_hosts` returns hosts in `UNAUTHENTICATED` state; other ops raise `ImbueCloudAuthError`.

### Acceptance tests (`@pytest.mark.acceptance`)

- `test_imbue_cloud_release.py::test_full_lease_create_destroy_delete_cycle` — against a real Modal-deployed staging connector and a real (but small) Vultr pool. Confirms wire compatibility end-to-end.
- `test_minds_cutover_acceptance.py::test_subprocess_path_creates_agent` — minds desktop client is invoked headlessly, signs in, leases a host, mints a key, runs `mngr create`, and the agent comes online.

### Release tests (`@pytest.mark.release`)

- `test_imbue_cloud_release.py::test_pool_admin_provisioning` — operator-side flow: `mngr imbue_cloud admin pool create --count 1 …` provisions a real Vultr VPS, inserts a row, then a separate process leases it, runs through, and the operator destroys it.

### Edge cases to cover explicitly

- Lease returns 503 (no matching pool host) — `mngr create` exits non-zero with the connector's message.
- Refresh fails — every operation surfaces `ImbueCloudAuthError`; `mngr list` shows `UNAUTHENTICATED`.
- SSH keypair rename collides (host_id reuse from an old destroyed-not-deleted lease) — overwrite is safe.
- Multiple imbue_cloud_* instances pointing at the *same* account share the underlying session file (keyed by user_id).
- Pool DB has `attributes` rows authored before the request schema is widened — `@>` semantics let the request omit fields the row constrains, so older rows still match if the request happens to set the matching subset.
- Manual `mngr imbue_cloud hosts release` of a lease that mngr still has live records for — provider should reconcile on next `discover_hosts`.

### Ratchets

- Add a ratchet preventing the import of `httpx` from the minds desktop_client (post-cutover; ensures we don't reintroduce direct connector calls).
- Add a ratchet preventing direct `from imbue.minds.desktop_client.host_pool_client …` style imports outside the plugin (after deletion, this is enforced by file absence; ratchet ensures it stays gone).

## Open Questions

1. **Single-round-trip claim step.** Phase 3 implements 2 SSH round trips (matching the recently-merged minds optimization). A more aggressive 1-round-trip implementation would emit one bash heredoc that edits both the agent's `data.json` (rename+labels) and env files in a single SSH exec. This trades the type-safe `OnlineHostInterface.rename_agent(labels_to_merge=…)` API for a brittle direct-filesystem write. The user's explicit guidance is "consolidate logic into the minimal number of round trips, even at the expense of a bit more brittleness." Should we go to 1 round trip in Phase 3, or land at 2 first and follow up?
2. **Connector schema migration mechanics.** The connector currently has no migration tooling visible in the repo. Do we add a tiny `migrations/` directory + a script run via `modal run`, or apply the schema change once via `psql "$NEON_DB_DIRECT" -f migrations/001.sql` and skip the framework? Given there's only one migration coming, scripted-once feels right.
3. **Account → user_id mapping.** When `mngr imbue_cloud auth signin --account alice@example.com` succeeds, we get back a `user_id` from the connector. Sessions are keyed by `user_id`. But provider config uses `account = "alice@example.com"`. To resolve a session from `account`, the plugin must either (a) lookup user_id from email locally each time (we cache the mapping during signin), or (b) call the connector's `/auth/users/<id>` to confirm. (a) is simpler and what this spec assumes; calling out for confirmation.
4. **Concurrent leases by the same account.** Today the connector locks one row at a time with `FOR UPDATE SKIP LOCKED`; nothing prevents one user from leasing many hosts at once. Should the plugin enforce a per-account quota (config field) and short-circuit before hitting the connector, or rely on the connector to add quota enforcement later?
5. **Tunnel ownership when an account is removed.** If a user signs out (provider instance removed), their existing tunnels stay alive on Cloudflare and accumulate cost. Should `mngr imbue_cloud auth signout` also tear down any tunnels owned by the account? Or is that opt-in via a flag? Today's behavior (in minds) is no auto-teardown; calling it out as something to decide.
6. **Where does the plugin's auth listener bind?** OAuth callback needs a localhost port. Pin `127.0.0.1:0` and emit the chosen port (the connector's authorize URL has to know it ahead of time, so we'd need to register a fixed port range with the connector's OAuth client config). Check whether we need to coordinate with the connector's allowed redirect URLs.
7. **Forever-claude-template welcome-message migration timing.** Moving `WELCOME_INITIAL_MESSAGE` to `[create_templates.main]` is a forever-claude-template change. It needs to ship in coordination with this PR (template-server pin), not after, otherwise users who upgrade minds without a fresh template lose the welcome message. Confirm coordination plan.
8. **`mngr create --provider imbue_cloud_<account>` UX when the account isn't signed in.** Should the plugin auto-spawn `mngr imbue_cloud auth signin --account <email>` when there's a TTY, or always fail with a "run signin first" message? The "behave like modal" rule says fail; the user-facing UX may want better hand-holding.
