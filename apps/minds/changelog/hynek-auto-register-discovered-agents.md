# Auto-register newly-discovered agents in the host's latchkey permissions

Sibling agents created from inside a workspace via the system_interface's
"new chat" / "new worktree" buttons were never added to the host's
`latchkey_permissions.json` allowed-agent list, so the latchkey gateway's
`minds-api-proxy` extension rejected every `/api/v1/agents/<new_id>/...`
request from them. Only top-level agents created through
`AgentCreator` (the create-project form / `/api/create-agent`) were
being registered, because that path called the registration helper
explicitly.

`minds run` now wires a `LatchkeyAutoRegister` callback onto the
`MngrCliBackendResolver` discovery stream. Every newly-discovered
`(host_id, agent_id)` pair on a minds-managed host (i.e. a host whose
permissions file already exists from creation time) is appended to the
host's `not.anyOf` allowed-agent list automatically. The dedup is
in-memory so the steady-state callback no-ops without touching disk,
and the underlying `register_agent_for_host` is itself idempotent.

`AgentCreator` no longer registers explicitly after `mngr create`
returns. The auto-register callback is now the single registration
path: it covers the create-project form, the "new chat" / "new
worktree" buttons, manual `mngr create` invocations, and any other
path that lands an agent on a minds-managed host.
