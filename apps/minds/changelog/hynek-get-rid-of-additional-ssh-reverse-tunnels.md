# Minds API access: gateway-only, single key, per-agent URL prefix

The minds desktop client used to expose its `/api/v1/...` REST API to
workspaces over a per-agent reverse SSH tunnel, writing the resulting
URL to `$MNGR_AGENT_STATE_DIR/minds_api_url` and injecting a per-agent
UUID4 `MINDS_API_KEY` into each new host's env file. None of that is
how agents actually reach the Minds API anymore -- the latchkey
gateway's `minds-api-proxy` extension already handled it -- so the
machinery is gone:

- `minds run` no longer asks the `mngr forward` plugin for a
  `--reverse 0:<port>` tunnel and no longer registers any
  `on_reverse_tunnel_established` callback. The `MindsApiUrlWriter`
  and `LocalAgentDiscoveryHandler` classes (and their tests) have
  been removed from `forward_cli.py`.
- `agent_creator.py` no longer generates a per-agent `MINDS_API_KEY`,
  no longer adds `--host-env MINDS_API_KEY=...` to `mngr create`, and
  no longer stores any per-agent `api_key_hash` file. Workspaces no
  longer carry the env var at all.
- The `apps/minds/imbue/minds/desktop_client/api_key_store.py` module
  has been rewritten around a single central key:
  `load_or_create_minds_api_key(<data_dir>)` lazily creates
  `<data_dir>/minds_api_key` (mode 0o600) on first use and reuses it
  across desktop-client restarts.
- The `/api/v1/...` bearer-auth gate (used by both `api_v1.py` and the
  WebDAV mount under `/api/v1/files`) now compares the inbound
  `Authorization: Bearer <key>` against that single value with a
  constant-time check. Routes that need an agent id take it from the
  URL path -- the auth dependency itself returns `None`.
- The notifications endpoint moved from `POST /api/v1/notifications`
  to `POST /api/v1/agents/<agent_id>/notifications`, matching the
  Telegram routes. Every `/api/v1` route is now per-agent.
- Every agent created by minds gets a per-agent rule + schemas
  installed in its host's latchkey permissions file at
  finalize-host-permissions time. The rule narrows what the gateway
  will let that agent reach through `minds-api-proxy` to its own
  `/api/v1/agents/<agent_id>/...` subtree.
- The agent baseline (in `mngr_latchkey/agent_setup.py`) additionally
  grants every agent a blanket
  `POST /minds-api-proxy/api/v1/agents/<...>/notifications` permission
  so notifications work without any extra per-agent grant.
- The `inject_tunnel_token_into_agent` helper moved out of
  `api_v1.py` into its own module so it can be imported without
  pulling the FastAPI router in.

Documentation:
[`apps/minds/docs/latchkey-permissions.md`](docs/latchkey-permissions.md)
now has a "Minds API access through the gateway" section describing
the new model; [`specs/minds-rest-api/spec.md`](../../specs/minds-rest-api/spec.md)
has a banner pointing out which parts are superseded.
