- Bumped `latchkey` to 2.8.0 and switched minds to running a single shared
  `latchkey gateway` subprocess for every agent instead of one per agent.
  The gateway is now password-protected via
  `LATCHKEY_GATEWAY_LISTEN_PASSWORD` (the password is derived
  deterministically from the desktop client's Latchkey encryption key by
  hashing a JWT minted with `latchkey gateway create-jwt`, so it survives
  restarts without being persisted in plaintext).
- Each agent still has its own `latchkey_permissions.json`. At
  agent-creation time minds calls `latchkey gateway create-jwt` against
  the agent's permissions file and injects the resulting JWT as
  `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE` (alongside the gateway URL and
  password) so every gateway request the agent makes is evaluated
  against that file. The gateway's own default permissions config
  (`~/.minds/latchkey_default_permissions.json`) is materialized empty
  (deny-all) so requests that bypass the JWT mechanism cannot reach any
  service.
- Old per-agent gateway records left under
  `~/.minds/agents/<id>/latchkey_gateway.json` are cleaned up
  automatically on desktop-client startup. Agents that were created with
  earlier minds versions need to be re-created to pick up the new
  password / JWT environment variables; without them their `latchkey`
  CLI calls will be rejected by the now-password-protected gateway.
