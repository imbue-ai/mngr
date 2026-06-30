Agents can now find and read the chat history of past agents -- including ones that have been destroyed -- so they can recover "old stuff" a user references.

Two new cross-workspace API endpoints (gated by the existing `minds-workspaces` read permission, no new grant needed):

- `GET /api/v1/workspaces/preserved` lists every agent whose transcript was preserved when it was destroyed, newest-first, with each agent's name, id, and preservation time.

- `GET /api/v1/workspaces/<agent_id>/transcript` returns an agent's transcript -- the durable preserved copy for a destroyed agent, falling back to the live agent otherwise. It accepts the same `format` (human/json/jsonl), `role`, `head`, and `tail` filters as `mngr transcript`.
