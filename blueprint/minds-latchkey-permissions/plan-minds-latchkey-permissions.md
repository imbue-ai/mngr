# Minds Latchkey Permission System

## Overview

* Adds a per-agent permission flow that lets an agent ask the user to authorize a Latchkey service it cannot currently use, and resume work after a decision arrives.
* Reuses the existing request-inbox infrastructure (request events, inbox panel, notifications) introduced for the sharing flow; adds a new request type and a service-permission dialog on top of it.
* Per-agent isolation is achieved by spawning each `latchkey gateway` with its own `LATCHKEY_PERMISSIONS_CONFIG` env var pointing at a per-agent `permissions.json` on the desktop host. Latchkey's existing detent-backed permission engine does the actual enforcement; minds only edits the file.
* The desktop client owns the credential side too: when no credentials exist yet for the requested service, it runs `latchkey auth browser <service>` locally against the shared `LATCHKEY_DIRECTORY` before persisting the new rules.
* Responses flow back to the agent as plain-English `mngr message` calls, so the agent's normal idle-then-active cycle handles the "wait" with no special state machine.
* Agent-side surface area is intentionally minimal: the agent only emits `(service_name, rationale)`. The user (helped by sensible defaults) chooses which detent permission schemas to grant.
* Mapping of latchkey service name to detent scope/permission schemas is desktop-only data (`apps/minds/imbue/minds/desktop_client/latchkey/services.toml`); the agent never needs it.
* `LATCHKEY_DIRECTORY` stays shared across all agents on the user's machine; this is by design for v1.

## Expected behavior

### Agent side

* When an agent's call through the Latchkey gateway returns:
  - HTTP 400 with `Error: No credentials found for <svc>` -> case (b) "no credentials".
  - HTTP 400 with `Error: Credentials for <svc> are expired` -> treated as case (b).
  - HTTP 403 with `Error: Request not permitted by the user.` -> case (c) "not permitted".
  - HTTP 200 -> success, no permission flow.
* On (b) or (c), the agent (per the latchkey skill in the forever-claude template):
  - Writes a `LatchkeyPermissionRequestEvent` to `$MNGR_AGENT_STATE_DIR/events/requests/events.jsonl` containing the latchkey service name and a one-paragraph rationale (no URL, no failure-kind, no requested-permissions).
  - Ends its turn and goes idle.
* When `mngr message` later delivers a plain-English response (e.g. "Your permission request for Slack was granted; please retry."), the agent becomes active again and decides its next action from message content alone (retry, give up, ask follow-up, etc.). No retry helper, no wrapper library.

### Desktop client side

* The new request appears in the inbox panel as a "Permission" card with the service display name (resolved from the desktop-side mapping), the rationale, and a "Review" action.
* Clicking the card opens a dialog scoped to that single service:
  - Lists only the detent permission schemas relevant to the service (from `services.toml` plus runtime `detent dump` cross-reference for descriptions).
  - Pre-checks the "widest" defaults (heuristic: any permission schema name ending in `-read-all` or `-write-all`; explicit override per service in `services.toml`).
  - Shows currently-granted permissions for that service as already-checked, so the same dialog can revoke (uncheck) them.
  - Has Approve and Deny buttons. Approve is disabled while zero boxes are checked; the user must either pick at least one permission or click Deny.
* On Approve:
  - The desktop runs `latchkey services info <service>` and reads `credentialStatus`. If it is `missing` or `invalid`, the desktop runs `latchkey auth browser <service>` synchronously, surfacing progress in the dialog. Cancel/failure -> response status `AUTH_FAILED`. If it is `valid`, the browser step is skipped. `unknown` is treated like `missing` (best-effort).
  - The agent's `permissions.json` is rewritten with one `rule` per scope schema mapping to the chosen permission schemas (replacing any previous rule for that scope).
  - A response event with status `GRANTED` is appended to `~/.minds/events/requests/events.jsonl`.
  - `mngr message <agent-id> "<plain-english summary>"` is invoked.
* On Deny:
  - No browser launch, no `permissions.json` change.
  - Response event with status `DENIED` is appended.
  - `mngr message` delivers a plain-English denial.
* On `latchkey auth browser` failure:
  - Response event with status `AUTH_FAILED`.
  - `mngr message` delivers a plain-English explanation including any error.
* The request is removed from the inbox once a response event referencing its `event_id` is observed (existing behavior).
* Notifications fire on new requests with the existing infrastructure (notification with click-to-navigate to the dialog page).

### Gateway lifecycle integration

* Each spawned `latchkey gateway` gets `LATCHKEY_PERMISSIONS_CONFIG=~/.minds/agents/<agent_id>/permissions.json` in its environment.
* The file is created lazily on the first grant. While absent, latchkey skips the permission check (status quo). This means an agent must always go through case (b) once before case (c) can ever fire for that service.

## Implementation plan

### Data types and storage

* `apps/minds/imbue/minds/desktop_client/request_events.py` -- modify:
  - Add new value to `RequestType`: `LATCHKEY_PERMISSION = auto()`.
  - Add new value to `RequestStatus`: `AUTH_FAILED = auto()`.
  - Add `LatchkeyPermissionRequestEvent(RequestEvent)` with fields `service_name: str` and `rationale: str`.
  - Add factory `create_latchkey_permission_request_event(agent_id, service_name, rationale)`.
  - Update `parse_request_event()` to dispatch the new `request_type` to `LatchkeyPermissionRequestEvent`.
  - Update `_dedup_key()` so the new event uses `(agent_id, service_name, request_type)`.
  - Leave the existing `PermissionsRequestEvent` in place (it stays as a placeholder for non-latchkey permission flows).

* `apps/minds/imbue/minds/desktop_client/latchkey/permissions_store.py` (new):
  - `PermissionsConfig(FrozenModel)` -- in-memory representation of latchkey's `permissions.json`: `schemas: dict[str, dict] | None`, `rules: list[dict[str, list[str]]]`, `include: list[str] | None`.
  - `load_permissions(path: Path) -> PermissionsConfig` -- returns empty config if file is absent.
  - `save_permissions(path: Path, config: PermissionsConfig) -> None` -- atomic write with `0o600`.
  - `granted_permissions_for_service(config, scope_schemas) -> dict[str, list[str]]` -- returns `{scope_schema: [permission_schemas]}` for the listed scopes.
  - `set_permissions_for_service(config, scope_schemas, granted) -> PermissionsConfig` -- replaces (or removes) per-scope rules; returns updated config. Approving with empty list still writes a rule with `[]` so the gateway explicitly denies, instead of falling through to other rules.
  - `permissions_path_for_agent(data_dir: Path, agent_id: AgentId) -> Path` -- returns `data_dir / "agents" / <agent_id> / "permissions.json"`.

* `apps/minds/imbue/minds/desktop_client/latchkey/services.toml` (new, desktop-only):
  - One table per latchkey service:
    ```toml
    [services.slack]
    display_name = "Slack"
    scope_schemas = ["slack-api"]
    permission_schemas = [
      "slack-read-all", "slack-write-all",
      "slack-read-messages", "slack-write-messages",
      ...
    ]
    # Optional override; default is heuristic (-read-all / -write-all).
    default_permissions = ["slack-read-all", "slack-write-all"]
    description = "Send and receive Slack messages on your behalf."
    ```
  - Initial coverage: all services in latchkey's `src/services/` (slack, discord, github, dropbox, linear, google-*, notion, mailchimp, gitlab, zoom, telegram, sentry, aws, stripe, figma, calendly, yelp, coolify, umami).

* `apps/minds/imbue/minds/desktop_client/latchkey/services_catalog.py` (new):
  - `ServicePermissionInfo(FrozenModel)`: `name: str`, `display_name: str`, `description: str`, `scope_schemas: tuple[str, ...]`, `permission_schemas: tuple[str, ...]`, `default_permissions: tuple[str, ...]`.
  - `load_services_catalog(toml_path: Path) -> dict[str, ServicePermissionInfo]` -- parses the TOML; applies the `-read-all` / `-write-all` heuristic when `default_permissions` is omitted.
  - `get_service_info(catalog, service_name) -> ServicePermissionInfo | None`.
  - Module-level singleton accessor; loaded once at startup.

### Gateway integration

* `apps/minds/imbue/minds/desktop_client/latchkey/_spawn.py`:
  - `spawn_detached_latchkey_gateway()` gains a `permissions_config_path: Path | None` parameter; when non-None, sets `LATCHKEY_PERMISSIONS_CONFIG=<path>` in the child env.

* `apps/minds/imbue/minds/desktop_client/latchkey/gateway.py`:
  - `LatchkeyGatewayManager._spawn_gateway()` computes `permissions_path_for_agent(data_dir, agent_id)` and passes it through to `spawn_detached_latchkey_gateway`.
  - The path is *not* pre-created -- latchkey treats a missing file as "no rules, allow all". Creation happens on the first grant.
  - Liveness reconciliation in `start()` is unchanged; the env var is only read at gateway startup so adoption of pre-existing gateways still works.
  - When an agent is destroyed (`stop_gateway_for_agent`), a follow-up cleanup deletes `permissions.json` for that agent if present. (New helper `delete_permissions_for_agent` in `permissions_store.py`.)

### Desktop app routes and flow

* `apps/minds/imbue/minds/desktop_client/latchkey/permission_flow.py` (new):
  - `LatchkeyServicesInfoProbe(MutableModel)` -- wraps `subprocess.run(["latchkey", "services", "info", "--json", service])` (or whatever JSON-output flag latchkey provides; otherwise parses stdout) with `LATCHKEY_DIRECTORY` injected; returns the parsed `credentialStatus: Literal["missing", "valid", "invalid", "unknown"]`. Treats CLI failure as `unknown`.
  - `LatchkeyAuthBrowserRunner(MutableModel)` -- wraps `subprocess.run(["latchkey", "auth", "browser", service])` with `LATCHKEY_DIRECTORY` injected; surfaces stdout/stderr; classifies cancellation vs other failures. Uses the project's `ConcurrencyGroup` per the existing latchkey package conventions.
  - `MngrMessageSender(MutableModel)` -- wraps `subprocess.run([mngr_binary, "message", agent_id, text])` and structured-logs failures.
  - `PermissionGrantHandler(MutableModel)` -- ties everything together:
    - `grant(request_id, agent_id, service_name, granted_permissions)`:
      1. Asserts `granted_permissions` is non-empty (defence in depth; UI already blocks the empty case).
      2. Looks up service info from `services_catalog`.
      3. Calls `LatchkeyServicesInfoProbe.probe(service)`. If `credentialStatus` is `missing`, `invalid`, or `unknown`, calls `LatchkeyAuthBrowserRunner.run(service)`. On failure -> writes `AUTH_FAILED` response and returns.
      4. Loads per-agent `permissions.json`, applies `set_permissions_for_service`, saves atomically.
      5. Writes `GRANTED` response event.
      6. Sends `mngr message` with a templated plain-English summary.
    - `deny(request_id, agent_id, service_name)`:
      1. Writes `DENIED` response event.
      2. Sends `mngr message` with a templated plain-English denial.

* `apps/minds/imbue/minds/desktop_client/app.py` -- modify:
  - Inject `services_catalog`, `permission_grant_handler`, and the data dir into the app at construction (mirror existing dependency wiring).
  - New routes:
    - `GET /requests/<request_id>/permission` -- renders the permission dialog HTML (uses the catalog + currently-granted set from `permissions.json`).
    - `POST /requests/<request_id>/permission/grant` -- form-encoded list of permission schema names; calls `PermissionGrantHandler.grant`; renders a result page (or returns JSON for the inbox panel to refresh).
    - `POST /requests/<request_id>/permission/deny` -- calls `PermissionGrantHandler.deny`.
  - Update the existing `/requests/<request_id>` dispatch to route `LATCHKEY_PERMISSION` requests to the new dialog page.

* `apps/minds/imbue/minds/desktop_client/templates.py` -- modify:
  - Add `_LATCHKEY_PERMISSION_DIALOG_TEMPLATE`: form with checkboxes for each permission schema (pre-checked per heuristic / catalog default; pre-checked for already-granted), service display name + description header, rationale block, Approve/Deny buttons. Approve is disabled (greyed-out) whenever zero boxes are checked; a small client-side script toggles its `disabled` state on every checkbox change. Approve runs an XHR with progress updates so the user sees the browser-auth step.
  - Update `_REQUESTS_PANEL_TEMPLATE` to render `LATCHKEY_PERMISSION` cards (service icon/name + rationale + "Review" link).

* `apps/minds/imbue/minds/desktop_client/runner.py` -- modify:
  - Load `services_catalog` at startup.
  - Build the `PermissionGrantHandler` with paths/dependencies (data_dir, latchkey_directory, mngr_binary).
  - Pass them into the FastAPI app construction.

### Agent-side surface

* `apps/minds_workspace_server` and `apps/minds/imbue/minds/desktop_client/request_events.py` already provide the request-event-write path; the agent simply appends a JSONL line directly.
* No changes to `host.py`'s touch list -- the response side does not use a per-agent file (responses come via `mngr message`). The desktop response file (`~/.minds/events/requests/events.jsonl`) already exists for the sharing flow.
* The forever-claude template's latchkey skill (out of repo) will be updated separately to teach the agent to:
  - Detect the three failure shapes from the gateway response.
  - Write `LatchkeyPermissionRequestEvent` with `service_name` and `rationale`.
  - Stop the turn and wait.
  - On `mngr message` resume, parse "granted" / "denied" / "auth_failed" from text and act accordingly.

### Misc

* `pyproject.toml` for `apps/minds`: no new runtime deps -- `tomllib` (stdlib, py3.12+) handles `services.toml`. Latchkey is already an external CLI dep, not a Python lib.

## Implementation phases

### Phase 1: Per-agent permissions.json wiring (no UI yet)

* Add `permissions_store.py` with load/save/`set_permissions_for_service` helpers.
* Thread `permissions_config_path` through `_spawn.py` and `gateway.py`.
* Hook gateway destruction to file cleanup.
* Unit tests: round-trip permissions config, scope replacement, atomic write, gateway env var injection.

### Phase 2: Service catalog and request event type

* Hand-write `services.toml` covering the current latchkey service set.
* Add `services_catalog.py` with the heuristic-and-override default-permissions logic.
* Extend `request_events.py` with the new event type, status, parser dispatch, dedup key.
* Unit tests: catalog parsing, heuristic correctness for representative services (Slack, AWS sub-services, GitHub), event serialization round-trip, dedup behavior.

### Phase 3: Permission grant handler (no UI)

* Add `permission_flow.py` with `LatchkeyServicesInfoProbe`, `LatchkeyAuthBrowserRunner`, `MngrMessageSender`, and `PermissionGrantHandler`.
* Unit tests: grant happy path (`credentialStatus=missing` -> browser ran; `credentialStatus=valid` -> browser skipped), deny path, auth-failed path, write-then-mngr-message ordering, services-info probe failure -> treated as `unknown` -> still triggers browser flow.

### Phase 4: Dialog UI and routes

* Add the dialog template and the three routes in `app.py`.
* Update inbox-panel template to render `LATCHKEY_PERMISSION` cards.
* Wire the runner to load the catalog and construct the handler.
* Manual verification with a real agent making a real failing call (Slack first because credentials are easy).
* Integration tests covering: dialog render, grant POST -> file change + response event + message, deny POST -> response event + message, auth-failed surfacing.

### Phase 5: Documentation and skill updates

* Update `apps/minds/docs/` with a short page describing the permission flow.
* Coordinate with the forever-claude template repo to add the latchkey-skill instructions for agents (out of this monorepo).
* End-to-end manual test: spawn an agent with no Slack creds, have it attempt a Slack call, observe inbox notification, approve with default perms, watch the agent receive `mngr message` and continue.

## Testing strategy

### Unit tests

* `permissions_store_test.py`: empty-config defaults, round-trip preserves unknown keys, `set_permissions_for_service` replaces only matching scope rules, `0o600` permissions on save, missing file -> empty config.
* `services_catalog_test.py`: TOML parsing, heuristic default selection (`slack-read-all` / `slack-write-all` chosen, AWS sub-services chosen via override, services with no `-all` schemas), display-name resolution.
* `request_events_test.py`: `LatchkeyPermissionRequestEvent` (de)serialization, `parse_request_event` routing, `RequestInbox.get_pending_requests` dedup including the new type, `AUTH_FAILED` status round-trip.
* `permission_flow_test.py` (with mocked subprocess): grant happy path writes file then response event then message, in that order; `services info` returning `valid` skips the browser invocation entirely; `services info` returning `missing` triggers the browser flow; failure of `latchkey auth browser` produces `AUTH_FAILED` and does NOT modify `permissions.json`; deny path skips file write entirely; mngr-message failure is logged but does not raise; UI-bypass attempt with empty `granted_permissions` raises before any subprocess is spawned.
* `gateway_test.py`: spawning passes `LATCHKEY_PERMISSIONS_CONFIG`; adoption of an existing gateway still works (env var only matters at startup).

### Integration tests

* Full inbox flow with a fake agent: write a `LatchkeyPermissionRequestEvent` to a tmpdir-backed `events.jsonl`, observe it in `RequestInbox`, drive the FastAPI route, assert the per-agent `permissions.json` is updated and a `mngr message` invocation is recorded.
* End-to-end with a real `latchkey gateway` subprocess (acceptance-marked): start a gateway with a tmp `LATCHKEY_PERMISSIONS_CONFIG`, write a deny-all rule, assert HTTP 403 from the gateway; rewrite to allow, assert HTTP success.
* Inbox panel render: dialog page rendered with currently-granted permissions pre-checked; revoke flow (uncheck a previously-granted item) actually removes it from `permissions.json`.

### Edge cases

* `permissions.json` missing or malformed -> treated as empty config; one log warning, no crash.
* User cancels `latchkey auth browser` (closes window) -> `AUTH_FAILED` response and message; `permissions.json` unchanged.
* Two requests for the same service while one is in flight -> dedup by `(agent_id, service_name, request_type)` keeps only the latest in the inbox; both get the same response event applied.
* Approve with zero permissions checked -> not reachable through the UI (button disabled). The handler asserts on this server-side too; the assertion failure is logged but does not write any response event (the user can simply pick something or click Deny).
* `latchkey services info` reports `credentialStatus=invalid` (e.g. expired token) -> treated like `missing`: re-run `latchkey auth browser` to refresh the credential before applying the grant.
* Service name in the request doesn't appear in the catalog -> dialog falls back to a "no permissions configured for this service" message and only Deny is enabled; logs a warning so we know to extend `services.toml`.
* `latchkey` binary missing -> grant attempt surfaces a clear UI error; no response event is written until the user retries.

## Open questions

* **Default-permissions heuristic vs explicit list (Q19)**: not yet confirmed. Plan currently uses (c): heuristic (`-read-all` / `-write-all`) with `services.toml` override. Switching to (a) (always explicit) is a one-line toggle in `services_catalog.py`.
* **Source of scope/permission split (Q20)**: plan assumes (a) hand-authored TOML. Risks drift from upstream detent built-ins; a small linter test that cross-checks TOML schema names against `detent dump` output at test time would mitigate. Worth deciding.
* **Sync mechanism for the response message (Q21)**: plan assumes (a) shelling out to `mngr message`. If a Python API is preferred, it's a localized swap inside `MngrMessageSender`.
* **Permissions-file pre-creation (Q22)**: plan assumes (a) lazy creation on first grant. The implication is that case (c) can only fire for services that already had at least one approve cycle; before that, latchkey allows everything (status quo today). If we want every request to go through a permission check from day one, we'd need (b) -- pre-create with `{"rules": []}` at agent-create time.
* **Event-type reuse vs new type (Q23)**: plan picks (b) -- a fresh `LatchkeyPermissionRequestEvent` rather than repurposing the existing `PermissionsRequestEvent`. Confirm.
* **Concurrency**: what happens when two agents request permission for the same service simultaneously? The shared `LATCHKEY_DIRECTORY` would serialize fine for credentials, but the dialog UI doesn't currently coordinate. Assume single-user, single-active-dialog for v1.
* **Revocation outside the prompt context**: a future "Manage permissions" page is mentioned but explicitly deferred. Capture as a follow-up.
* **Per-account scoping**: out of scope for v1 by user decision. The `workspace-accounts-and-request-inbox` spec's per-workspace-account model does not affect this flow yet.
* **Skill-level changes in forever-claude-template**: the agent-side detection-and-wait logic lives outside this repo. Need a separate PR there with matching wording for the gateway error patterns.
