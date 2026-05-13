# Latchkey state is now keyed per-host instead of per-agent

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

The minds UI's grant flow now resolves the request event's `agent_id`
to its `host_id` via the backend resolver before writing the grant; if
the resolver hasn't seen the agent yet (or only reports the static
`"localhost"` placeholder), the grant POST returns 503 so the UI can
retry instead of silently writing the grant to the wrong file.
