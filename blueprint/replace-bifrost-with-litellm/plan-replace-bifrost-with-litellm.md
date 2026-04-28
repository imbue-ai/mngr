# Replace bifrost with litellm on modal

## Overview

* Bifrost never worked on Modal; the litellm-on-modal service (`apps/modal_litellm`) is deployed and working -- switch to it for virtual key management
* Add key management endpoints (`/keys/*`) to the remote service connector that proxy to LiteLLM's admin API using the master key, keeping the master key server-side
* Modify the leased agent setup flow in the desktop client to: (1) create a LiteLLM virtual key via the remote connector, (2) sed-replace a placeholder `ANTHROPIC_API_KEY` in the agent env file and claude config, (3) inject `ANTHROPIC_BASE_URL` via append
* Pre-created pool hosts will ship with a realistic-looking placeholder `ANTHROPIC_API_KEY` (correct `sk-ant-api03-` prefix and length) so that mngr provisioning writes it into both the env file and `customApiKeyResponses.approved` in the claude config
* Delete the entire bifrost project (`apps/bifrost_service/`), its deploy script, spec, secrets template, and coverage config -- no backwards compatibility needed
* Support optional per-key budgets (`max_budget`, `budget_duration`) on key creation, with no limit if unset; the $100/day default comes from the client

## Expected behavior

* When a user creates a leased agent in the minds app, the desktop client calls the remote connector's `/keys/create` endpoint to generate a LiteLLM virtual key scoped to that user
* The remote connector returns the virtual key and the LiteLLM proxy base URL (read from its `LITELLM_PROXY_URL` env var)
* During lease setup, the placeholder `ANTHROPIC_API_KEY` on the host is sed-replaced with the real virtual key in both `/mngr/agents/{id}/env` and `{CLAUDE_CONFIG_DIR}/.claude.json`
* `ANTHROPIC_BASE_URL` is appended to the env file (pointing at the litellm proxy's `/anthropic` path)
* The agent starts with working Anthropic auth routed through the LiteLLM proxy -- no custom API key dialog prompt
* Users can list their keys, check spend/budget, update budgets, and delete keys via the remote connector's `/keys/*` endpoints
* All bifrost code, config, and deployment artifacts are removed
* The `modal_litellm` app itself is unchanged

## Changes

### Remote service connector (`apps/remote_service_connector/imbue/remote_service_connector/app.py`)

* Add `LITELLM_MASTER_KEY` and `LITELLM_PROXY_URL` to the connector's Modal secrets (new secret `litellm-{env}` or add to existing)
* Add request/response models: `CreateKeyRequest` (optional `key_alias`, `max_budget`, `budget_duration`), `CreateKeyResponse` (virtual key string, base URL), `KeyInfo`, `UpdateBudgetRequest`
* Add `POST /keys/create` -- authenticates the caller via SuperTokens, calls LiteLLM's `POST /key/generate` with `user_id` set to the SuperTokens user ID, optional `key_alias`, `max_budget`, `budget_duration`; returns the generated key and the proxy base URL
* Add `GET /keys` -- lists the caller's virtual keys by passing `user_id` to LiteLLM's `GET /key/list` (or equivalent)
* Add `GET /keys/{key_id}` -- get key info including spend and budget from LiteLLM
* Add `PUT /keys/{key_id}/budget` -- update `max_budget` and/or `budget_duration` for a key via LiteLLM's key update endpoint; verify ownership via `user_id`
* Add `DELETE /keys/{key_id}` -- delete a key via LiteLLM's key delete endpoint; verify ownership via `user_id`

### Desktop client -- new `LiteLLMKeyClient` (`apps/minds/imbue/minds/desktop_client/litellm_key_client.py`)

* New file, modeled after `HostPoolClient` (frozen pydantic model with `connector_url` and `timeout_seconds`)
* Methods: `create_key(access_token, key_alias?, max_budget?, budget_duration?) -> CreateKeyResult`, `list_keys(access_token) -> list[KeyInfo]`, `get_key_info(access_token, key_id) -> KeyInfo`, `update_budget(access_token, key_id, max_budget, budget_duration?)`, `delete_key(access_token, key_id)`
* `CreateKeyResult` frozen model with fields: `key` (the virtual key string), `base_url` (the proxy base URL)
* Error class `LiteLLMKeyError` inheriting from `MindError`

### Agent creator (`apps/minds/imbue/minds/desktop_client/agent_creator.py`)

* Add `litellm_key_client: LiteLLMKeyClient | None` field to `AgentCreator`
* In `_setup_and_start_leased_agent` (or `_setup_leased_agent`), before the rename step (step 0): call `litellm_key_client.create_key()` to get the virtual key and base URL, with default budget of $100/day passed from the client
* Extend the parallel step (step 2) to also sed-replace the placeholder `ANTHROPIC_API_KEY` in the env file and claude config, and append `ANTHROPIC_BASE_URL` to the env file
* The placeholder is a fixed string like `sk-ant-api03-PLACEHOLDER000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000` (correct prefix and ~108 char length)
* Sed command for env file: replace the placeholder with the real key
* Sed command for claude config: replace the placeholder string (which appears in the `customApiKeyResponses.approved` list) with the real key
* Append `ANTHROPIC_BASE_URL={base_url}` to the env file

### Pool host creation

* When creating pool hosts, pass the placeholder `ANTHROPIC_API_KEY` as an `--env` flag to `mngr create` so that provisioning writes it into the env file and the claude config (via `approve_api_key_for_claude`)
* This happens in whatever script/process creates pool hosts (likely in the forever-claude-template or the pool creation scripts)

### Secrets and deployment

* Add `LITELLM_MASTER_KEY` and `LITELLM_PROXY_URL` to the remote connector's Modal secrets (either extend the existing `neon-{env}` secret or create a new `litellm-{env}` one that the connector's function also mounts)
* Update `.minds/template/` to add any new template files needed for the connector's litellm secrets

### Bifrost removal

* Delete `apps/bifrost_service/` directory entirely
* Delete `scripts/deploy_bifrost_service.sh`
* Delete `specs/neon-bifrost-service/`
* Delete `.minds/template/bifrost.sh`
* Remove `"--cov=imbue.bifrost_service"` from root `pyproject.toml`
* Remove `apps/bifrost_service` from workspace members in root `pyproject.toml` (if listed)
