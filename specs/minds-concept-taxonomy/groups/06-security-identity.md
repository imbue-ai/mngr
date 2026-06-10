# Group 6: security, identity & access

Taxonomy of security, identity, and access concepts in the Minds codebase.
All citations are code-grounded; docs are cited as secondary and flagged when they diverge from implementation.

---

## 1. Secrets

### 1.1 Canonical Definition

A **secret** in Minds is a named environment-variable file placed at `runtime/secrets/<name>.env` inside a running agent's workspace.
Each file is owned by exactly one writer and exports one or more shell variables read by a background service watching that path.

Canonical location of the concept: `apps/minds/imbue/minds/desktop_client/tunnel_token_injection.py:28`
```python
_TUNNEL_TOKEN_FILE: Final[str] = "runtime/secrets/cloudflare_tunnel.env"
```
The module docstring at `tunnel_token_injection.py:1-16` defines the convention explicitly:
> "The token lives at `runtime/secrets/cloudflare_tunnel.env` inside the agent. `runtime/secrets/` is a directory of per-secret `*.env` files (this token, `restic.env` for backups, `telegram.env` for the bot); each writer owns its own file so they never clobber one another."

Named secret files in the codebase:
- `runtime/secrets/cloudflare_tunnel.env` — Cloudflare tunnel token (`tunnel_token_injection.py:28`)
- `runtime/secrets/restic.env` — restic backup credentials (referenced in `tunnel_token_injection.py:14`, `primitives.py:76-85` via `BackupProvider`)
- `runtime/secrets/telegram.env` — Telegram bot token (referenced in `tunnel_token_injection.py:15`)

A separate, orthogonal meaning of "secret" exists in the **deployment layer**: timestamped Modal Secrets (cloud key-value stores) holding environment data pushed by `minds env deploy`. These are governed by `apps/minds/imbue/minds/envs/secret_lifecycle.py`, which defines `DeployId`, `timestamped_secret_name`, and `gc_old_per_tier_secrets`. The term "Secret" here is Modal's own nomenclature (a hosted KV namespace), not the `runtime/secrets/*.env` convention.

A third meaning: **Vault KV-v2 secrets** read by deploy operators via `apps/minds/imbue/minds/envs/vault_reader.py:42-143`. These are operator-held secrets (Cloudflare API tokens, SuperTokens admin API keys, restic repo passwords) at `secrets/minds/<tier>/<service>` in HashiCorp Vault. Kept in process memory only; never written to disk by this code.

### 1.2 All Usages

| Location | What kind of "secret" |
|---|---|
| `tunnel_token_injection.py:28-51` | Write/clear `cloudflare_tunnel.env` via `mngr exec` |
| `onboarding.py:59` | Constant for `PERMISSIONS_PREFERENCES_REMOTE_PATH` mentions `runtime/memory/` not `runtime/secrets/`; onboarding does not use secrets |
| `envs/secret_lifecycle.py` | Modal Secret naming, GC, timestamped deploy IDs |
| `envs/vault_reader.py` | Vault KV read/write/delete via `vault` CLI |
| `primitives.py:66-85` | `BackupProvider` enum references injecting a `runtime/secrets/restic.env` |
| `mngr_imbue_cloud/session_store.py` | SuperTokens session tokens (access/refresh JWT) stored at `<profile_dir>/providers/imbue_cloud/sessions/<user_id>.json`; not called "secrets" in code |
| `sharing_handler.py:159` | `tunnel.token.get_secret_value()` — Pydantic `SecretStr` wrapper |
| `api_key_store.py` | In-memory `MINDS_API_KEY`, not persisted |
| `auth.py:131` | `secrets.token_urlsafe` — Python stdlib for generating signing key |
| `supertokens_app.py:33` | `api_key: SecretStr` — Pydantic field type for SuperTokens admin API key |
| `cloudflare_tunnels.py:39` | `api_token: SecretStr` — Pydantic field type for CF API token |

### 1.3 Competing/Multiple Definitions

Three distinct secret concepts share the same word:
1. **Agent runtime secrets** — `runtime/secrets/*.env` files inside the container, watched by services.
2. **Modal Secrets** — cloud-hosted KV bundles for deployed infra, named `<service>-<tier>-<deploy_id>`.
3. **Vault KV secrets** — operator secrets in HashiCorp Vault, e.g. Cloudflare API tokens.

Additionally, Pydantic's `SecretStr` type is used throughout to wrap sensitive string values in-memory (`CookieSigningKey`, API keys, `api_token`), which is a fourth meaning (a type-system protection, not a storage location).

### 1.4 Terminology Variants

- **secret** — `runtime/secrets/*.env` files (tunnel_token_injection, onboarding module docstring)
- **Secret** (uppercase) — Modal Secret KV namespace (`secret_lifecycle.py`)
- **secret** — HashiCorp Vault KV entry (`vault_reader.py`)
- **SecretStr** — Pydantic field type for in-memory secret strings
- **token** — used interchangeably with "secret" for the Cloudflare tunnel token (`tunnel.token`, `CLOUDFLARE_TUNNEL_TOKEN`)
- **key** — used for API keys, signing keys, encryption keys (not called "secrets" in code)

### 1.5 Ambiguities/Inconsistencies

- The word "secret" is overloaded across three entirely different storage systems (filesystem, Modal, Vault). Reading "secrets" in a file name or variable requires context to determine which system is meant.
- `CLOUDFLARE_TUNNEL_TOKEN` is the env-var name inside `cloudflare_tunnel.env` but the file itself is called "tunnel token" not "tunnel secret" in comments.
- `restic.env` exports are called "backup credentials" in `primitives.py:76-85` but the file is stored under `runtime/secrets/`.
- The `MINDS_API_KEY` (`api_key_store.py`) is ephemeral/in-memory but is called a "key" not a "secret."

### 1.6 DOC/CODE Divergences

- `latchkey-permissions.md:125-128` describes the opaque permissions handle at `~/.minds/latchkey/permissions/<uuid>.json`, but the code (`store.py:74`) uses `<latchkey_directory>/mngr_latchkey/permissions/<uuid>.json`. The docs use `~/.minds/latchkey/` which matches the user-visible path only if `LATCHKEY_DIRECTORY=~/.minds/latchkey`; the code uses a `PLUGIN_DATA_SUBDIR_NAME = "mngr_latchkey"` subdirectory inside `LATCHKEY_DIRECTORY` (`store.py:65`), so the canonical internal path is `<LATCHKEY_DIRECTORY>/mngr_latchkey/permissions/<uuid>.json`.

### 1.7 Recommended Canonical Term + Definition

Use **"runtime secret"** for `runtime/secrets/*.env` files to distinguish them from Modal Secrets and Vault secrets. In code: keep `_TUNNEL_TOKEN_FILE` and the docstring pattern, but consider adding a module-level comment in `tunnel_token_injection.py` calling them "runtime secrets." The Vault and Modal usages already have their own namespacing and need no renaming.

---

## 2. Credentials

### 2.1 Canonical Definition

A **credential** in Minds means a set of third-party service authentication materials managed by the `latchkey` CLI and stored in `LATCHKEY_DIRECTORY`. These are distinct from secrets: secrets are Minds-managed env files; credentials are latchkey-managed per-service auth state (OAuth tokens, API keys set via `latchkey auth set`, browser-acquired tokens via `latchkey auth browser`).

Canonical definition of credential status: `libs/mngr_latchkey/imbue/mngr_latchkey/core.py:142-153`
```python
class CredentialStatus(UpperCaseStrEnum):
    """Latchkey-reported credential state for a service.

    Mirrors detent's ``ApiCredentialStatus`` enum (``missing``, ``valid``,
    ``invalid``, ``unknown``) but normalized to the project's enum convention.
    """
    MISSING = auto()
    VALID = auto()
    INVALID = auto()
    UNKNOWN = auto()
```

The `LatchkeyServiceInfo` model at `core.py:171-190` models the parsed output of `latchkey services info <service>`, including `credential_status`, `auth_options`, and `set_credentials_example`.

Two sub-flows for obtaining credentials:
- **Browser flow**: `latchkey auth browser <service>` — `core.py:167` (`LATCHKEY_AUTH_OPTION_BROWSER`)
- **Manual set flow**: `latchkey auth set` — `core.py:168` (`LATCHKEY_AUTH_OPTION_SET`)

### 2.2 All Usages

| Location | Usage |
|---|---|
| `latchkey/handlers/predefined.py:334` | `self.latchkey.services_info(service_info.name)` — probe credential status |
| `latchkey/handlers/predefined.py:367` | `self.latchkey.auth_browser(service_info.name)` — acquire credentials via browser |
| `latchkey/handlers/predefined.py:130-144` | `_fallback_set_credentials_example`, `_prepend_latchkey_directory` — manual credential setup |
| `core.py:142-196` | `CredentialStatus`, `LatchkeyServiceInfo`, `LATCHKEY_AUTH_OPTION_BROWSER`, `LATCHKEY_AUTH_OPTION_SET` |
| `gateway_client.py:195-200` | `AvailablePermission` — permission schemas tied to credentials scope |
| `latchkey-permissions.md:76-95` | Describes the credential-check-then-grant flow (matches code) |

The term "credential" also appears in FCT (`claude_auth.py`) in a different context: Claude Code's own authentication credentials (`ANTHROPIC_API_KEY`, OAuth tokens from `claude auth login`). These are not latchkey credentials; they are Claude-specific auth materials. `claude_auth.py:1-47` documents both paths (API key and OAuth).

### 2.3 Competing/Multiple Definitions

- **Latchkey credentials**: third-party service credentials (Slack, GitHub, etc.) managed by latchkey.
- **Claude credentials**: `ANTHROPIC_API_KEY` or OAuth tokens for Claude itself, written to host env file by `claude_auth.py:192-206`.
- **SuperTokens session tokens**: access/refresh JWTs for the Minds cloud account, stored by `mngr_imbue_cloud/session_store.py`. These are sometimes called "session" not "credentials" but they are authentication materials.
- **Cloudflare API token**: used for tunnel management in `cloudflare_tunnels.py:39`, called `api_token: SecretStr`.
- **SuperTokens API key**: used for admin operations in `supertokens_app.py:33`, called `api_key: SecretStr`.

### 2.4 Terminology Variants

- **credential** / **credential_status** — latchkey service credentials (`core.py`, `predefined.py`)
- **credentials** — manual set instructions (`set_credentials_example`, `_prepend_latchkey_directory`)
- **api_key** — Anthropic API key (`claude_auth.py`), SuperTokens admin key (`supertokens_app.py`), Cloudflare API token (`cloudflare_tunnels.py`), Minds API key (`api_key_store.py`)
- **token** — Cloudflare tunnel token (`tunnel_token_injection.py`), SuperTokens access/refresh JWTs (`session_store.py`)
- **session** — SuperTokens session (`session_store.py`, `supertokens_routes.py`)
- **signing_key** — cookie signing key (`auth.py:25`, `primitives.py:134`)

### 2.5 Ambiguities/Inconsistencies

- "Credentials" in `latchkey` context means third-party service auth, but "credentials" in `supertokens_routes.py:302` means the email+password pair submitted to sign in. These are entirely different things sharing the same term.
- `api_key` is used for four semantically different things: Anthropic inference key, SuperTokens admin key, Cloudflare REST API token, and the Minds internal API key.
- The latchkey doc (`latchkey-permissions.md:75-78`) says "credentials are not valid" (matching `CredentialStatus.VALID`), consistent with code.

### 2.6 DOC/CODE Divergences

None found for credentials specifically. The `latchkey-permissions.md` accurately describes `CredentialStatus` and the two auth flows.

### 2.7 Recommended Canonical Term + Definition

- **service credential** — latchkey-managed per-service auth materials (OAuth tokens, `latchkey auth set` values)
- **account credential** — SuperTokens session (access/refresh tokens) for the Minds cloud account
- **API key** — reserve for explicitly key-based auth (Anthropic, Cloudflare, SuperTokens admin)
- **signing key** — cookie signing key (already consistent in code)

---

## 3. Permissions

### 3.1 Canonical Definition

A **permission** in Minds is a named detent schema string that an agent requests and a user grants to allow a specific category of access to a third-party service. Permissions are organized into **scopes** (detent scope schemas, e.g. `slack-api`), and each scope can have multiple permission schemas (e.g. `slack-read-all`, `any`).

The permission model is defined in `libs/mngr_latchkey/imbue/mngr_latchkey/store.py:206-236`:
```python
class LatchkeyPermissionsConfig(FrozenModel):
    """In-memory representation of a Latchkey/Detent permissions config file."""
    rules: tuple[dict[str, list[str]], ...] = Field(...)  # scope -> [permission, ...]
    schemas: dict[str, JsonValue] = Field(...)
```

Per-host permission files live at:
`<LATCHKEY_DIRECTORY>/mngr_latchkey/hosts/<host_id>/latchkey_permissions.json` (`store.py:244-251`)

Three special permission files:
- **Default (deny-all)**: `latchkey_default_permissions.json` (`store.py:254-262`) — empty rules, consulted when no JWT is present
- **Admin (wildcard)**: `latchkey_admin_permissions.json` (`store.py:265-286`) — `{"any": ["any"]}` rule
- **Per-agent opaque**: `<LATCHKEY_DIRECTORY>/mngr_latchkey/permissions/<uuid>.json` (`store.py:289-323`) — initially deny-all, symlinked to host canonical path after `mngr create`

The term **"detent"** refers to the upstream open-source access-control framework that Latchkey is built on. Detent defines the schema system (scope schemas, permission schemas, the `any` wildcard) that minds uses to express what access is being granted. Detent is not developed inside this monorepo; minds consumes it through the latchkey CLI and gateway. Detent terminology is: **scope** (e.g. `slack-api`) = the service namespace; **permission** (e.g. `slack-read-all`, `any`) = a specific access grant within that scope.

### 3.2 All Usages

| Location | Usage |
|---|---|
| `store.py:206-236` | `LatchkeyPermissionsConfig` — permission file model |
| `store.py:254-286` | Default and admin permissions files |
| `store.py:289-323` | Opaque per-agent permissions handle |
| `gateway_client.py:93-157` | `PredefinedRequestPayload`, `PermissionEffect`, `StreamedPermissionRequest` — gateway wire types |
| `gateway_client.py:195-233` | `AvailablePermission`, `AvailableServiceEntry` — catalog types |
| `gateway_client.py:575-652` | `get_granted_permissions_for_scopes`, `set_permission_rule` — permission read/write via gateway |
| `services_catalog.py:56-108` | `ServicePermissionInfo`, `_service_info_from_entry` — dialog-facing catalog |
| `permission_requests_consumer.py` | Streaming gateway-side pending requests |
| `latchkey/handlers/predefined.py:237-744` | `LatchkeyPermissionGrantHandler` — full grant/deny flow |
| `latchkey/handlers/file_sharing.py` | `FileSharingGrantHandler` — file-sharing grant/deny flow |
| `api_key_auth.py` | `MINDS_API_KEY` bearer auth for `/api/v1/...` — a separate, simpler auth layer, not detent-based |
| `cookie_manager.py` | Session cookie auth — also not detent-based |
| `latchkey_auto_register.py:28-80` | `LatchkeyAutoRegister` — auto-register newly-discovered agents in host permissions file |
| `agent_setup.py` (mngr_latchkey) | `register_agent_for_host` — referenced by auto-register |
| `core.py:106-119` | `_ENV_EXTENSION_PERMISSIONS_ROOT`, `_GATEWAY_EXTENSIONS_SUBDIR` — environment constraining the gateway extension |

### 3.3 Competing/Multiple Definitions

Two distinct permission systems coexist:

1. **Detent/Latchkey permissions** — the full scope/permission schema system for third-party service access. Stored in `latchkey_permissions.json`. User-visible grant/deny flow.

2. **API bearer auth** — `MINDS_API_KEY` bearer token for `/api/v1/...` endpoints (`api_key_auth.py`). Not detent-based; binary (valid key = full access to that endpoint). The latchkey gateway's per-host permissions file provides per-agent scoping at a coarser level.

3. **Session cookie auth** — `minds_session` cookie for the bare-origin desktop UI (`cookie_manager.py`). Also not detent-based.

4. **Latchkey gateway password** — `X-Latchkey-Gateway-Password` header for all gateway requests (`gateway_client.py:54`). A shared secret, not a permission.

5. **Latchkey permissions-override JWT** — `X-Latchkey-Gateway-Permissions-Override` header (`gateway_client.py:55`), which directs the gateway to a specific permissions file per agent. This is an access-control mechanism but is called "override" not "permission."

### 3.4 Terminology Variants

- **permission** — a detent permission schema name string (e.g. `slack-read-all`, `any`)
- **permissions** — the plural set of permission schemas granted for a scope; also the file `latchkey_permissions.json`
- **scope** — a detent scope schema name (e.g. `slack-api`); the namespace under which permissions are granted
- **rule** — one `{scope: [permission, ...]}` mapping in `latchkey_permissions.json` (`store.py:225`)
- **rule_key** — the scope name used as the key when calling `set_permission_rule` (`gateway_client.py:621`)
- **permissions_preference** — the onboarding Q3 free-text preference written to `runtime/memory/permissions_preferences.md` (`onboarding.py:99`) — completely different from detent permissions
- **permission_request** — a pending agent request for a permission grant (gateway extension endpoint)
- **permissions-override JWT** — the per-agent JWT controlling which permissions file the gateway reads

### 3.5 Ambiguities/Inconsistencies

- `permissions_preference` in onboarding (`onboarding.py:99`) has nothing to do with detent permissions — it is a free-text user instruction written into Claude's memory. The name is misleading.
- The word "permissions" without qualification can mean: (a) detent permission schemas, (b) the `latchkey_permissions.json` file, (c) the gateway HTTP extension `/permissions/...`, or (d) the onboarding `permissions_preference` field.
- `scope` in `PredefinedRequestPayload.scope` (`gateway_client.py:96`) is a detent scope schema name. `scope` in `PermissionEffect` rules (`gateway_client.py:143`) has the same meaning. But `scope` in Python stdlib / OAuth has a different meaning; no collision risk internally, but documentation readers need context.
- The admin permissions file content is `{"any": ["any"]}` — here `any` in the scope position means "match all requests" (a detent wildcard scope); `any` in the permissions position means "match all permission requirements." This double `any` wildcard is noted in `store.py:284` but not explained for readers unfamiliar with detent.

### 3.6 DOC/CODE Divergences

- `latchkey-permissions.md:125` says `~/.minds/latchkey/permissions/<uuid>.json`; code uses `<LATCHKEY_DIRECTORY>/mngr_latchkey/permissions/<uuid>.json` (`store.py:298`). These are equivalent only if `LATCHKEY_DIRECTORY = ~/.minds/latchkey` (the default). The doc omits the `mngr_latchkey/` subdirectory layer.
- `latchkey-permissions.md:135-138` says "minds replaces the opaque file with a symlink pointing at `~/.minds/agents/<agent_id>/latchkey_permissions.json`" — but the code (`store.py:244-251`) shows the canonical path is `<plugin_data_dir>/hosts/<host_id>/latchkey_permissions.json`, not `~/.minds/agents/<agent_id>/...`. DOC says it's keyed by `agent_id`; CODE says it's keyed by `host_id`. This is a significant divergence — multiple agents on the same host share one permissions file.

### 3.7 Recommended Canonical Term + Definition

- **permission** (detent sense): a named detent permission schema string granted under a scope
- **scope** (detent sense): a detent scope schema name grouping a set of permissions for one service
- **permissions rule**: one `{scope: [permission, ...]}` entry in a permissions config
- **permissions file**: `latchkey_permissions.json` (per-host) or the special default/admin variants
- Rename `permissions_preference` to `workspace_autonomy_preference` or `agent_trust_preference` to avoid collision with the detent permission concept

---

## 4. Accounts

### 4.1 Canonical Definition

An **account** is a SuperTokens user identity on the Minds cloud (the connector), represented by a `(user_id, email, display_name)` tuple. The on-disk association between accounts and workspaces is stored in `~/.minds/workspace_associations.json` (keyed `user_id -> [agent_id, ...]`). Account identity itself is sourced from the `mngr_imbue_cloud` plugin's session store.

Canonical model: `apps/minds/imbue/minds/desktop_client/session_store.py:50-62`
```python
class AccountSession(FrozenModel):
    user_id: SuperTokensUserId
    email: str
    display_name: str | None
    workspace_ids: list[str]
```

`SuperTokensUserId` is defined at `session_store.py:38-41` as a UUID v4.

The plugin-side session store is `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/session_store.py`, which persists SuperTokens access/refresh tokens at `<profile_dir>/providers/imbue_cloud/sessions/<user_id>.json` and maintains an `accounts.json` email→user_id index.

### 4.2 All Usages

| Location | Usage |
|---|---|
| `session_store.py:38-385` | `SuperTokensUserId`, `AccountSession`, `UserInfo`, `MultiAccountSessionStore` — full account model |
| `supertokens_routes.py:49-68` | `AuthUser`, `AuthResult` — sign-in/sign-up result carrying user identity |
| `supertokens_routes.py:350-376` | `signout_user_via_plugin` — sign out flow |
| `sharing_handler.py:105-124` | `resolve_account_email_for_workspace` — account lookup for sharing |
| `mngr_imbue_cloud/session_store.py` | SuperTokens session persistence (access/refresh JWTs) |
| `mngr_imbue_cloud/primitives.py` | `ImbueCloudAccount` (an email address type), `SuperTokensUserId` |
| `mngr_imbue_cloud/data_types.py` | `PaidListEntry` — connector-side paid-access table |
| `supertokens_app.py:68-102` | `SuperTokensAppRecord` — per-dev-env SuperTokens app (multi-tenant) |
| `minds_config.py` (referenced) | `get_default_account_id`, `set_default_account_id` |

### 4.3 Competing/Multiple Definitions

- **Account** in minds context: a signed-in Minds cloud user with a SuperTokens `user_id`, associated workspaces, and a LiteLLM virtual key. Represented by `AccountSession`.
- **Account** in `mngr_imbue_cloud` plugin context: `ImbueCloudAccount` (just a typed email string, `primitives.py`) — the identifier the plugin uses for multi-account config sections `[providers.imbue_cloud_<slug>]`.
- **Account** in connector context: a paying customer entry in the `PaidListEntry` table.
- **Account** in SuperTokens multi-tenancy context: a SuperTokens "app" (what SuperTokens calls a tenant); `supertokens_app.py:68` calls this a `SuperTokensAppRecord` with `app_id`.

### 4.4 Terminology Variants

- **account** — the signed-in user entity
- **user** — same concept in `AuthUser` (`supertokens_routes.py:49`) and `UserInfo` (`session_store.py:65`)
- **session** — the SuperTokens session (token pair) that authenticates a signed-in account; also `AccountSession` (the joined view of identity + workspace associations, despite the name)
- **user_id** — SuperTokens UUID (`SuperTokensUserId`); also `user_id_prefix` (first 16 hex chars, used for tunnel naming)
- **identity** — the `ImbueCloudAuthAccount` from `auth_list()` (email, display_name, user_id); what `session_store.py` calls "identity"

### 4.5 Ambiguities/Inconsistencies

- `AccountSession` (`session_store.py:50`) is poorly named: it is not a session (an ephemeral token pair) but rather a joined view of account identity with workspace associations. The actual session tokens live in the plugin. This confuses readers who expect "Session" to mean a token-based auth session.
- `MultiAccountSessionStore` (`session_store.py:83`) manages workspace associations, not sessions — despite "Session" in the name. The docstring notes this: "Joins plugin-owned auth identity with minds-local workspace associations."
- `ImbueCloudAccount` in `mngr_imbue_cloud/primitives.py` is just an email address (a `NonEmptyStr` subtype), while `AccountSession` includes the full identity + workspaces.

### 4.6 DOC/CODE Divergences

None found for accounts specifically.

### 4.7 Recommended Canonical Term + Definition

- **account**: the signed-in Minds cloud user entity, identified by SuperTokens `user_id`, with associated email, display name, and workspace list. One process can have multiple signed-in accounts simultaneously.
- Rename `AccountSession` -> `AccountProfile` or `AccountRecord` to remove the "Session" confusion.
- Rename `MultiAccountSessionStore` -> `AccountStore` or `WorkspaceAccountStore`.

---

## 5. Sharing / Global Access

### 5.1 Canonical Definition

**Sharing** in Minds means exposing a named service from an agent's workspace to external users via a Cloudflare tunnel. The user configures sharing through the Share modal at `/sharing/{agent_id}/{service_name}`. Cloudflare Access policies (email allowlists) gate who can reach the shared URL.

The sharing enablement function is `apps/minds/imbue/minds/desktop_client/sharing_handler.py:127-183`:
```python
def enable_sharing_via_cloudflare(
    request: Request,
    agent_id: AgentId,
    service_name: ServiceName,
    emails: Sequence[str],
    backend_resolver: BackendResolverInterface,
) -> TunnelInfo:
```

The flow:
1. Look up the account associated with the workspace (`resolve_account_email_for_workspace`)
2. Call `cli.create_tunnel(account, agent_id)` — idempotent; returns existing tunnel or creates a new one
3. Call `cli.add_service(account, tunnel_name, service_name, service_url)` — registers the service
4. If `emails` is non-empty, call `cli.set_service_auth(account, tunnel_name, service_name, policy={"emails": [...]})` — Cloudflare Access policy

The tunnel token is injected into the agent via `inject_tunnel_token_into_agent` (`tunnel_token_injection.py:31-50`), which writes `runtime/secrets/cloudflare_tunnel.env`.

Readiness detection: `sharing_handler.py:38-54` — `is_share_ready_from_edge_response` checks for a redirect to `*.cloudflareaccess.com`, which signals the Access application is live.

### 5.2 All Usages

| Location | Usage |
|---|---|
| `sharing_handler.py:38-183` | Core sharing functions: readiness probe, SSRF check, `enable_sharing_via_cloudflare` |
| `tunnel_token_injection.py:31-73` | Write/clear `cloudflare_tunnel.env` in agent |
| `envs/providers/cloudflare_tunnels.py` | List/delete tunnels by env metadata during `minds env destroy` |
| `mngr_imbue_cloud` (ImbueCloudCli) | `create_tunnel`, `add_service`, `set_service_auth` — connector API calls |
| `primitives.py:140` | `ServiceName` type — named service within an agent |

### 5.3 Competing/Multiple Definitions

- **Sharing** (user-facing): exposing a workspace service to specific external users via Cloudflare tunnel + Cloudflare Access email allowlist.
- **Global access**: not a defined code term. The concept of "public" vs "restricted" sharing is entirely handled by whether `emails` is empty or non-empty in `enable_sharing_via_cloudflare` (empty → no Access policy applied, which means the tunnel is reachable by anyone who has the URL, since the function does not explicitly block unauthenticated access if no policy is set).
- **File sharing** (`RequestType.FILE_SHARING_PERMISSION`): a distinct concept — granting an agent access to a local file path on the desktop host via WebDAV, not Cloudflare tunnels. Handled by `latchkey/handlers/file_sharing.py`.

### 5.4 Terminology Variants

- **sharing** — Cloudflare tunnel-based access for external users
- **tunnel** — the Cloudflare tunnel itself (`TunnelInfo`)
- **service** — the named endpoint being shared (e.g. `web`, `api`) via `ServiceName`
- **access policy** — Cloudflare Access email allowlist (`set_service_auth` with `policy={"emails": [...]}`)
- **file sharing** / **file-sharing** — WebDAV-based file access grant (completely different mechanism)

### 5.5 Ambiguities/Inconsistencies

- **File sharing** (WebDAV/latchkey) and **workspace sharing** (Cloudflare tunnel) use "sharing" but are entirely different mechanisms and user flows. `RequestType.FILE_SHARING_PERMISSION` (`gateway_client.py:106-131`) deals with WebDAV; the Share modal deals with Cloudflare.
- `FileSharingAccess` at `gateway_client.py:106` is READ/WRITE access to a local file, not related to global URL sharing.
- The variable `emails` in `enable_sharing_via_cloudflare` is the Access email allowlist. An empty list means no policy is set, which in Cloudflare Access means unrestricted public access — this is not documented in the function signature or docstring, creating a silent behavior change.

### 5.6 DOC/CODE Divergences

The docs reference a "sharing-request event" flow that agents no longer use. `sharing_handler.py:1-13` notes: "Agents no longer write sharing-request events back into the inbox." This represents a removed feature the doc no longer describes (no divergence found in the audit docs provided).

### 5.7 Recommended Canonical Term + Definition

- **workspace sharing**: exposing a workspace service via Cloudflare tunnel + Cloudflare Access (rename from plain "sharing" to distinguish)
- **file access grant**: WebDAV-based per-path file access (rename from "file sharing" to avoid overlap)
- Explicitly document that empty `emails` = no Access policy = anyone with URL can reach the service.

---

## 6. Onboarding / Data Preferences

### 6.1 Canonical Definition

**Onboarding** in Minds is a three-question dialog shown to the user while a workspace is being created. Each question maps to a side effect applied asynchronously after workspace creation.

The three questions and their side effects are defined at `apps/minds/imbue/minds/desktop_client/onboarding.py:83-113`:

```python
class OnboardingAnswers(FrozenModel):
    data_preference: UserDataPreference | None   # Q1: local scan
    initial_problem: str                          # Q2: message to chat agent
    permissions_preference: str                   # Q3: written to workspace memory
```

Q1 is the **data preference**: how much the workspace agent may learn about the user. Defined in `primitives.py:89-105`:
```python
class UserDataPreference(UpperCaseStrEnum):
    """How much the workspace agent may learn about the user during onboarding."""
    CONVENIENCE = auto()  # import as much local context as possible
    PRIVACY = auto()      # gather minimal data, kept on user's machine
    CONTROL = auto()      # gather nothing (scan skipped)
```

- `CONVENIENCE` and `PRIVACY`: triggers a local user-context scan (git user.name, OS full name, or login username) written to `~/.minds/user_context/<creation_id>.json` (`onboarding.py:318-326`).
- `CONTROL`: no scan.

Q3 is the **permissions preference** (free-text): written to `runtime/memory/permissions_preferences.md` via `mngr exec` (`onboarding.py:307-363`, constant at `onboarding.py:59`).

### 6.2 All Usages

| Location | Usage |
|---|---|
| `onboarding.py:83-384` | Full `OnboardingApplier` and `OnboardingAnswers` implementation |
| `primitives.py:89-105` | `UserDataPreference` enum |
| `onboarding.py:49-59` | Constants: `USER_CONTEXT_DIR_NAME`, `PERMISSIONS_PREFERENCES_REMOTE_PATH` |
| `onboarding.py:125-128` | `build_user_context_document` — `{name, details}` dict |
| `onboarding.py:144-158` | `resolve_local_user_name` — git config, GECOS, getpass |

### 6.3 Competing/Multiple Definitions

- **data_preference** (`UserDataPreference`) is clearly scoped to the onboarding Q1 choice.
- **permissions_preference** in `OnboardingAnswers.permissions_preference` is onboarding Q3 free text; confusingly named alongside the detent permissions system (see Section 3).
- The term "onboarding" in `claude_auth.py:252-263` refers to Claude Code's own first-launch onboarding dialogs (`complete_onboarding`), not Minds' workspace-creation onboarding. These are entirely separate flows.

### 6.4 Terminology Variants

- **data_preference** / **UserDataPreference** — the Q1 privacy/control choice
- **CONVENIENCE** / **PRIVACY** / **CONTROL** — the three data preference values
- **permissions_preference** — Q3 free-text agent instruction (badly named; not detent permissions)
- **user_context** — the output of Q1 scan (`USER_CONTEXT_DIR_NAME`, `build_user_context_document`)
- **initial_problem** — Q2 message sent to the chat agent

### 6.5 Ambiguities/Inconsistencies

- The `USER_CONTEXT_PLACEHOLDER_DETAILS` constant (`onboarding.py:54`: `"couldn't find any details"`) is hardcoded as the `details` field in the user context document. The module docstring says "the seed of a feature we will extend later" — the feature is partially implemented.
- `permissions_preference` (Q3) is stored in Claude's memory at `runtime/memory/permissions_preferences.md` — a markdown file read by Claude as part of its context, not a machine-parseable permissions config. The name conflates "preferences" (a natural language instruction) with the detent "permissions" concept.
- `PRIVACY` triggers a scan while `CONTROL` does not; but the distinction between `CONVENIENCE` and `PRIVACY` (both scan) is currently only in the enum docstring — the code at `onboarding.py:105-107` only distinguishes `CONTROL` from everything else via `is_scan_requested`. The `CONVENIENCE` vs `PRIVACY` distinction is therefore currently a no-op in the scan path (both produce the same `{name, details}` document).

### 6.6 DOC/CODE Divergences

None found in provided docs. The onboarding behavior is entirely implementation-internal.

### 6.7 Recommended Canonical Term + Definition

- **data preference** (`UserDataPreference`): the Q1 choice controlling how much local context is scanned at workspace creation.
- Rename `permissions_preference` (Q3) → `agent_instruction_preference` or `agent_autonomy_text` to avoid the detent permissions naming collision.
- The `CONVENIENCE`/`PRIVACY` distinction should eventually trigger different scan behavior or the `PRIVACY` variant should be removed until it is meaningfully different from `CONVENIENCE`.

---

## Cross-Cutting Headline Inconsistencies

### A. Secret / credential / token / key overlap
Four different things are all called "key" or "secret" or "token" in close proximity:
- `CookieSigningKey` — cookie HMAC signing secret
- `MINDS_API_KEY` — bearer token for `/api/v1/...`
- `CLOUDFLARE_TUNNEL_TOKEN` — tunnel runtime secret stored in `runtime/secrets/cloudflare_tunnel.env`
- `api_key: SecretStr` — Cloudflare REST API token, SuperTokens admin key, Anthropic inference key (all different things, same field name pattern)
- Latchkey service **credentials** vs SuperTokens session **tokens** vs Cloudflare API **token** vs Minds API **key** — all are auth materials, all different concepts

### B. "Session" naming overload
- `AccountSession` (`session_store.py:50`) = account identity + workspace associations (not a session)
- `MultiAccountSessionStore` = workspace-account association store (not session tokens)
- SuperTokens "session" = actual access/refresh JWT pair (plugin-side)
- `SESSION_COOKIE_NAME = "minds_session"` = browser session cookie

### C. "Permissions" naming collision between detent permissions and onboarding permissions_preference
- Detent permissions: machine-enforced access control schemas applied at the gateway
- `permissions_preference`: free-text Q3 onboarding instruction written to Claude's memory
These are unrelated but share the word "permissions" in code adjacent to each other.

### D. "Sharing" naming collision
- Workspace sharing: Cloudflare tunnel + Access for external users
- File sharing: WebDAV-based local file access grant (`FileSharingAccess`, `FileSharingRequestPayload`, `FileSharingGrantHandler`)
These are entirely different mechanisms with entirely different UX, but both use the word "sharing."

### E. DOC/CODE divergence: per-host permissions path
`latchkey-permissions.md:135-138` says permissions files are keyed by `agent_id` at `~/.minds/agents/<agent_id>/...`. The code (`store.py:244-251`) keys them by `host_id` at `<LATCHKEY_DIRECTORY>/mngr_latchkey/hosts/<host_id>/latchkey_permissions.json`. Multiple agents on the same host share one permissions file — the doc implies each agent has its own.

### F. `AccountSession` name misleads readers about what is "session" state vs what is "account" state
The class holds workspace associations and identity, not authentication tokens. The real sessions (tokens) live in the plugin. This confusion propagates to `MultiAccountSessionStore`, which is an account/workspace registry, not a session token store.
