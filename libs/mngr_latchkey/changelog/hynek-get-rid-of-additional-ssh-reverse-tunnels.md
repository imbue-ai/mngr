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
- New library helper: `imbue.mngr_latchkey.agent_setup.allow_agent_for_host(plugin_data_dir, host_id, agent_id)`.
  Reads the host's permissions file (or starts from the baseline if it
  doesn't yet exist), extracts the existing allowed-agent list out of
  the `anyOf` block, appends a new entry if not already there, and
  writes back atomically. Idempotent.
- New CLI: `mngr latchkey allow-agent --host-id ID --agent-id ID` wraps
  the helper for operators. Documented in the README's "Wiring a new
  agent using the CLI interface" section.
- `imbue.mngr_latchkey.store.load_permissions` is the new public
  reader that `allow_agent_for_host` uses; symmetric with `save_permissions`.
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
