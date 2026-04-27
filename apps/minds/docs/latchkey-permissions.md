# Latchkey permissions

Minds-managed agents access third-party services (Slack, GitHub, Google Drive,
...) through [Latchkey](https://github.com/imbue-ai/latchkey). This page
describes how the desktop client surfaces permission decisions to the user
and how the agent receives the answer.

## End-to-end flow

1. **Agent makes a call.** The agent issues an HTTP request to the per-agent
   `latchkey gateway` (or to `latchkey curl` directly).
2. **Gateway responds with success, no-credentials, or not-permitted.**
   * 200: success, nothing to do.
   * 400 with `Error: No credentials found for <svc>` (or `... are expired`):
     the user has not yet authenticated to the service.
   * 403 with `Error: Request not permitted by the user.`: the user has
     authenticated but has not allowed this kind of request.
3. **Agent writes a request event.** On any of the blocked outcomes, the
   agent appends a `LatchkeyPermissionRequestEvent` to
   `$MNGR_AGENT_STATE_DIR/events/requests/events.jsonl` with the latchkey
   service name and a one-paragraph rationale, then ends its turn and goes
   idle.
4. **Desktop notifies the user.** The desktop client tails the agent's
   request events file via `mngr events --follow`, adds a card to the
   right-side requests inbox panel, and surfaces a notification.
5. **User opens the dialog.** Clicking the card opens
   `/requests/<event_id>`, which renders a single-service permission dialog:
   * The list of [Detent](https://github.com/imbue-ai/detent) permission
     schemas the user can grant for that service, sourced from
     [`apps/minds/imbue/minds/desktop_client/latchkey/services.toml`](../imbue/minds/desktop_client/latchkey/services.toml).
   * The widest defaults (`-read-all` / `-write-all` by heuristic, or an
     explicit override per service) are pre-checked.
   * Already-granted permissions for that service replace the heuristic
     pre-check, so the dialog also acts as a revocation UI.
   * The Approve button stays disabled while zero boxes are checked.
6. **User approves.** The desktop client:
   1. Runs `latchkey services info <svc>` to read `credentialStatus`.
   2. If `missing` / `invalid` / `unknown`, runs `latchkey auth browser <svc>`
      synchronously; cancellation/failure produces an `AUTH_FAILED` outcome.
   3. Atomically rewrites the agent's `permissions.json` so the gateway
      enforces the chosen schemas on the next request.
   4. Appends a `GRANTED` (or `AUTH_FAILED`) response event to
      `~/.minds/events/requests/events.jsonl`.
   5. Sends the agent a plain-English `mngr message` describing the
      decision; the agent wakes up and decides whether to retry.
7. **User denies.** The desktop client appends a `DENIED` response event
   and sends the agent a plain-English denial message. `permissions.json`
   is not touched.

## Per-agent isolation

Each spawned `latchkey gateway` subprocess receives its own
`LATCHKEY_PERMISSIONS_CONFIG=~/.minds/agents/<agent_id>/permissions.json`
environment variable. The file is created lazily on the first grant; before
that, latchkey treats the missing file as `allow all`, so the very first
permission-blocked call for a service is always case (b) (no credentials).

When an agent is destroyed, its `permissions.json` is also removed so a
future agent reusing the same id starts with a clean slate.

`LATCHKEY_DIRECTORY` -- where credentials live -- stays shared across all
agents on the same machine for now. Per-account isolation is a possible
future extension.

## Service catalog

The catalog lives at
[`apps/minds/imbue/minds/desktop_client/latchkey/services.toml`](../imbue/minds/desktop_client/latchkey/services.toml)
and lists every latchkey service together with:

* `display_name`, `description` -- shown in the dialog header.
* `scope_schemas` -- detent scope schemas the service owns; used as
  rule keys in `permissions.json`.
* `permission_schemas` -- detent permission schemas the dialog offers as
  checkboxes.
* `default_permissions` -- optional override for which schemas are
  pre-checked when the dialog opens. When omitted, the runtime heuristic
  pre-checks any permission schema name ending in `-read-all` or
  `-write-all`, falling back to the full list.

To add a new service, copy an existing entry, swap in the schema names
listed for that service in detent's
[`docs/builtin-schemas.md`](https://github.com/imbue-ai/detent/blob/main/docs/builtin-schemas.md),
and pick a sensible default subset. Schemas must already exist in detent;
minds does not register custom schemas.

## Agent-side responsibilities

Agents are expected to:

* Detect the three blocked outcomes from the gateway response.
* Append a `LatchkeyPermissionRequestEvent` (with `service_name` and a
  short `rationale`) to the agent's own `events/requests/events.jsonl`.
* Stop the turn and wait. The agent will receive an `mngr message` from
  the desktop with the decision and can decide whether to retry.

The detection-and-wait logic for Claude Code lives in the
`forever-claude-template` repository's latchkey skill, not in this
monorepo.
