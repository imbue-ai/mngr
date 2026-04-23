# Bifrost Service: Serverless LLM Gateway on Modal + Neon

## Overview

- Agents running in containers currently receive raw `ANTHROPIC_API_KEY` values, giving them unlimited access with no per-agent budget control or usage tracking.
- This spec introduces a new Modal app (`bifrost-<env>`) that runs the [Bifrost](https://github.com/maximhq/bifrost) LLM gateway as a serverless service, backed by a Neon.tech PostgreSQL database for persistent state.
- Bifrost provides virtual keys (`sk-bf-*`) that act as budget-controlled proxies to real provider API keys. Each agent gets its own virtual key with configurable spend limits (default $100/day).
- The service exposes two Modal Functions in a single app:
  - **Inference Function**: runs the bifrost Go binary directly via `@modal.web_server(port=8080)`, serving OpenAI-compatible `/v1/*` routes for LLM traffic.
  - **Management Function**: a FastAPI app that authenticates users via SuperTokens and proxies CRUD operations to bifrost's `/api/governance/virtual-keys` admin API.
- This mirrors the deployment pattern of `apps/remote_service_connector/` -- self-contained `app.py`, environment-scoped Modal secrets via `.minds/template/*.sh`, and a deploy script.
- This PR is standalone: no `mngr create` or Claude agent integration. That comes in a later PR.

## Expected Behavior

### Inference (agent-facing)

- Agents send LLM requests to the inference Function's Modal URL with `Authorization: Bearer sk-bf-*` (their virtual key).
- Bifrost validates the virtual key, checks budget/rate limits, and forwards the request to Anthropic using the real `ANTHROPIC_API_KEY`.
- If the virtual key is over budget, bifrost returns HTTP 402. If the key is invalid or inactive, bifrost returns HTTP 403.
- Streaming (`/v1/chat/completions` with `stream: true`) works transparently since `@modal.web_server()` forwards the raw HTTP connection.
- Multiple inference containers can run simultaneously, all sharing the same Neon DB for consistent virtual key and budget state.
- Containers stay warm for 5 minutes (`container_idle_timeout=300`) to reduce cold starts. Requests have a 10-minute max (`timeout=600`).

### Management (user-facing)

- All management endpoints require a valid SuperTokens JWT in the `Authorization: Bearer <access_token>` header.
- The authenticated user's identity is derived as a 16-char hex prefix of their SuperTokens user ID (same as the remote-service-connector).
- Virtual key names are prefixed with this user ID to enforce ownership scoping.
- Available operations:
  - **Create virtual key** (`POST /keys`): creates a new bifrost virtual key with an optional budget (default: $100/day, reset daily). The key name is prefixed with the user's ID. Returns the `sk-bf-*` key value (only available at creation time).
  - **List virtual keys** (`GET /keys`): lists all virtual keys owned by the authenticated user (filtered by name prefix).
  - **Get virtual key budget** (`GET /keys/{key_id}/budget`): returns current usage and remaining budget for a key.
  - **Update virtual key budget** (`PUT /keys/{key_id}/budget`): changes the budget limit for a key.
  - **Delete virtual key** (`DELETE /keys/{key_id}`): deactivates and deletes a virtual key.
- All management operations verify ownership: a user can only manage keys whose names start with their user ID prefix.
- The management Function runs its own bifrost subprocess internally. It proxies admin requests to `http://localhost:8080/api/governance/virtual-keys` using the `BIFROST_ADMIN_TOKEN` bearer token. This avoids cross-function networking while sharing the same Neon DB for consistent state.

### Deployment

- `scripts/deploy_bifrost_service.sh <env>` deploys the app, mirroring `scripts/deploy_remote_service_connector.sh`.
- Secrets are managed via `.minds/<env>/bifrost.sh` and pushed with `scripts/push_modal_secrets.py`.
- The bifrost `config.json` is generated at container startup from environment variables -- no static config is baked into the image.
- Bifrost's own admin API auth is enabled (bearer token), since the inference Function exposes bifrost directly to the internet via `@modal.web_server()`.

## Implementation Plan

### New files

#### `apps/bifrost_service/imbue/bifrost_service/app.py`

Self-contained Modal app (no monorepo imports, same pattern as `remote_service_connector/app.py`). Contains:

- **`_DEPLOY_ENV`**: read from `MNGR_DEPLOY_ENV` env var at module level (default: `"production"`).
- **`_BIFROST_PORT`**: constant `8080`.
- **`_BIFROST_BINARY_PATH`**: path to the downloaded bifrost binary (e.g. `/usr/local/bin/bifrost`).
- **`_BIFROST_APP_DIR`**: directory for bifrost's runtime data (e.g. `/app/bifrost`).
- **`_BIFROST_VERSION`**: pinned version string (e.g. `"v1.4.1"`, whatever is latest stable at implementation time).
- **`_DEFAULT_BUDGET_DOLLARS`**: `100.0`.
- **`_DEFAULT_BUDGET_RESET_DURATION`**: `"1d"`.
- **`_USER_ID_PREFIX_LENGTH`**: `16` (matching remote-service-connector).

**Image build**:
- `modal.Image.debian_slim()` with `.run_commands(...)` to download the pinned bifrost release binary from GitHub to `/usr/local/bin/bifrost`.
- `.pip_install("fastapi[standard]", "httpx", "supertokens-python")` for the management Function dependencies.

**`_generate_bifrost_config()`**: generates `config.json` at `_BIFROST_APP_DIR/config.json`. The config:
- `config_store`: type `"postgres"`, reads host/port/user/password/db_name from env vars (`NEON_CONFIG_HOST`, `NEON_CONFIG_PORT`, `NEON_CONFIG_USER`, `NEON_CONFIG_PASSWORD`, `NEON_CONFIG_DB`), `ssl_mode: "require"`.
- `logs_store`: type `"postgres"`, reads from separate env vars (`NEON_LOGS_HOST`, `NEON_LOGS_PORT`, `NEON_LOGS_USER`, `NEON_LOGS_PASSWORD`, `NEON_LOGS_DB`), `ssl_mode: "require"`.
- `encryption_key`: reads from `BIFROST_ENCRYPTION_KEY` env var.
- `auth_config`: bearer token from `BIFROST_ADMIN_TOKEN` env var.
- `providers`: Anthropic provider with key from `ANTHROPIC_API_KEY` env var.
- `client.allowed_origins`: `["*"]` (Modal handles external access control).

**`_start_bifrost_subprocess()`**: starts bifrost as a subprocess (`subprocess.Popen`) with `-app-dir`, `-port`, `-host 0.0.0.0`, and `-log-level info`. Returns the `Popen` object.

**`_wait_for_bifrost_ready()`**: polls `http://localhost:{_BIFROST_PORT}/health` until it returns 200, with a timeout (e.g. 30 seconds).

**SuperTokens initialization**: same `_init_supertokens()` pattern as the remote-service-connector, but only the session recipe is needed (no signup/signin/email-verification -- management only validates existing JWTs).

**`_authenticate_supertokens(token)`**: validates a SuperTokens JWT and returns the 16-char user ID prefix. Same logic as `_authenticate_supertokens` in the remote-service-connector.

**FastAPI management endpoints** (mounted on a `web_app = FastAPI()` instance):

- `POST /keys` -- body: `CreateKeyRequest(name: str, budget_dollars: float | None, budget_reset_duration: str | None)`. Prefixes `name` with user ID, creates virtual key via bifrost admin API, sets budget. Returns key info including the `sk-bf-*` value.
- `GET /keys` -- lists virtual keys filtered by user ID prefix via bifrost admin API.
- `GET /keys/{key_id}/budget` -- fetches virtual key details from bifrost and returns budget info.
- `PUT /keys/{key_id}/budget` -- body: `UpdateBudgetRequest(budget_dollars: float, budget_reset_duration: str | None)`. Verifies ownership, updates budget via bifrost admin API.
- `DELETE /keys/{key_id}` -- verifies ownership, deletes virtual key via bifrost admin API.

Each management endpoint:
1. Extracts and validates the SuperTokens JWT.
2. Derives the user ID prefix.
3. Verifies ownership (key name starts with user prefix).
4. Proxies to `http://localhost:{_BIFROST_PORT}/api/governance/virtual-keys/...` with `Authorization: Bearer {BIFROST_ADMIN_TOKEN}`.

**Pydantic models** (request/response, defined in the same file):
- `CreateKeyRequest`, `UpdateBudgetRequest`
- `VirtualKeyInfo`, `BudgetInfo`

**Modal app definition**:
```python
app = modal.App(name=f"bifrost-{_DEPLOY_ENV}", image=image)
```

**Inference Function**:
```python
@app.function(
    secrets=[
        modal.Secret.from_name(f"bifrost-{_DEPLOY_ENV}"),
        modal.Secret.from_dict({"MNGR_DEPLOY_ENV": _DEPLOY_ENV}),
    ],
    timeout=600,
    container_idle_timeout=300,
)
@modal.web_server(port=_BIFROST_PORT)
def bifrost_inference():
    _generate_bifrost_config()
    _start_bifrost_subprocess()
    _wait_for_bifrost_ready()
```

**Management Function**:
```python
@app.function(
    secrets=[
        modal.Secret.from_name(f"bifrost-{_DEPLOY_ENV}"),
        modal.Secret.from_name(f"supertokens-{_DEPLOY_ENV}"),
        modal.Secret.from_dict({"MNGR_DEPLOY_ENV": _DEPLOY_ENV}),
    ],
    timeout=600,
    container_idle_timeout=300,
)
@modal.asgi_app()
def bifrost_management() -> FastAPI:
    _generate_bifrost_config()
    _start_bifrost_subprocess()
    _wait_for_bifrost_ready()
    _init_supertokens()
    return web_app
```

#### `apps/bifrost_service/pyproject.toml`

- Minimal project metadata following the `remote_service_connector` pattern.
- Dependencies: `fastapi[standard]`, `httpx`, `modal`, `pydantic`, `supertokens-python`.

#### `.minds/template/bifrost.sh`

Secret template declaring all required env vars:
- `ANTHROPIC_API_KEY` -- real Anthropic API key for bifrost to forward requests.
- `BIFROST_ENCRYPTION_KEY` -- AES key for encrypting virtual key values at rest.
- `BIFROST_ADMIN_TOKEN` -- bearer token for bifrost's admin API.
- `NEON_CONFIG_HOST`, `NEON_CONFIG_PORT`, `NEON_CONFIG_USER`, `NEON_CONFIG_PASSWORD`, `NEON_CONFIG_DB` -- Neon connection details for the config store.
- `NEON_LOGS_HOST`, `NEON_LOGS_PORT`, `NEON_LOGS_USER`, `NEON_LOGS_PASSWORD`, `NEON_LOGS_DB` -- Neon connection details for the logs store.

#### `scripts/deploy_bifrost_service.sh`

Shell script mirroring `scripts/deploy_remote_service_connector.sh`:
- Takes `<env-name>` as argument.
- Sets `MNGR_DEPLOY_ENV`.
- Runs `modal deploy` on the `app.py` file.

### Modified files

#### `scripts/push_modal_secrets.py`

- No code changes expected. The script already discovers all `.sh` files in `.minds/template/` and `.minds/<env>/` automatically. Adding `.minds/template/bifrost.sh` is sufficient.

## Implementation Phases

### Phase 1: Skeleton and inference Function

- Create `apps/bifrost_service/` directory structure with `pyproject.toml` and blank `__init__.py`.
- Create `app.py` with the Modal image build (download bifrost binary), config generation, subprocess management, and the inference Function (`@modal.web_server`).
- Create `.minds/template/bifrost.sh` with all env var declarations.
- Create `scripts/deploy_bifrost_service.sh`.
- **Verification**: deploy to a test environment, confirm bifrost starts and `/health` returns 200 on the inference Function's Modal URL. Confirm `/v1/chat/completions` works with a manually-created virtual key (created via direct bifrost admin API call using curl).

### Phase 2: Management Function with SuperTokens auth

- Add SuperTokens initialization (session recipe only).
- Add `_authenticate_supertokens()` for JWT validation.
- Add the FastAPI `web_app` with `POST /keys` and `GET /keys` endpoints.
- Wire up the management Function (`@modal.asgi_app()`).
- **Verification**: deploy, create a virtual key via the management API using a valid SuperTokens JWT, confirm it appears in the list, confirm it works for inference.

### Phase 3: Full CRUD and budget management

- Add `GET /keys/{key_id}/budget`, `PUT /keys/{key_id}/budget`, and `DELETE /keys/{key_id}` endpoints.
- Add ownership verification (key name prefix check) to all endpoints.
- **Verification**: full round-trip -- create key, check budget, update budget, use key for inference until budget exceeded (402), delete key.

### Phase 4: Testing and deployment tooling

- Add unit tests for config generation, naming/ownership helpers, and request/response models.
- Add integration test that exercises the management API against a real bifrost subprocess (marked `@pytest.mark.acceptance`).
- Add `conftest.py`, `test_ratchets.py`, and other standard project files.
- Verify `scripts/push_modal_secrets.py` correctly discovers and pushes `bifrost-<env>` secrets.

## Testing Strategy

### Unit tests (`app_test.py`)

- `_generate_bifrost_config()` produces valid JSON with correct structure and env var references.
- Virtual key name prefixing: `_make_key_name(user_prefix, name)` correctly combines user prefix and key name.
- Ownership verification: keys with matching prefix pass, mismatched prefix raises 403.
- `CreateKeyRequest` validation: budget defaults applied correctly, custom values respected.
- SuperTokens auth extraction: mock session returns correct user ID prefix.

### Integration tests (`test_bifrost_service.py`)

- Full lifecycle test (marked `@pytest.mark.acceptance`):
  1. Start a bifrost subprocess locally (requires the binary to be available).
  2. Create a virtual key via the management endpoint.
  3. Verify it appears in the list.
  4. Check budget shows correct defaults.
  5. Update budget.
  6. Delete the key.
  7. Verify it no longer appears in the list.

### Edge cases to test

- Creating a key with the same name twice (should be rejected by bifrost as names are unique).
- Attempting to manage a key owned by a different user (should return 403).
- Inference request with an invalid/expired virtual key (should return 403).
- Inference request with a virtual key that has exceeded its budget (should return 402).
- Bifrost subprocess crash detection (management endpoint should return 503 if bifrost is not healthy).

### What is NOT tested in this PR

- Integration with `mngr create` (deferred to a later PR).
- Multi-provider routing (only Anthropic is configured).

## Open Questions

- **Neon connection pooling**: Neon has its own connection pooler. Should bifrost's `max_open_conns` be tuned for Neon's pooler, or left at defaults? Neon's free tier has connection limits that could be relevant.
- **Bifrost version pinning strategy**: should we track bifrost releases and update the pin periodically, or only update when we need a specific feature?
- **Cold start latency**: how long does bifrost take to start and run migrations against Neon? If it's too slow, we may need to increase `container_idle_timeout` or pre-warm containers.
- **Rate limiting**: should we configure bifrost rate limits on virtual keys in addition to budgets? The current spec only sets budgets.
- **Inference Function auth**: bifrost's `auth_config` bearer token protects the admin API (`/api/*`), but the inference routes (`/v1/*`) are protected by virtual keys. Is there any concern about the `/health`, `/metrics`, or other non-authed routes being publicly accessible?
