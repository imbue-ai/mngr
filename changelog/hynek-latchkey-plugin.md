## mngr-latchkey: new plugin

Added a new `mngr-latchkey` plugin (`imbue-mngr-latchkey`) that owns
the shared `latchkey gateway` lifecycle, per-agent latchkey wiring,
and the reverse SSH tunnel that bridges the host-side gateway into
remote agents. Exposes:

- A new CLI subcommand: `mngr latchkey ensure-gateway` -- idempotently
  starts (or adopts) the shared `latchkey gateway` subprocess and prints
  its host/port/pid as a JSON line. Default state lives at
  `<profile>/latchkey`.

- Python APIs: `imbue.mngr_latchkey.core.Latchkey` (gateway lifecycle,
  JWT minting, services info, auth-browser),
  `imbue.mngr_latchkey.agent_setup.prepare_agent_latchkey` (env vars +
  opaque permissions handle for a new agent),
  `imbue.mngr_latchkey.agent_setup.finalize_agent_permissions` (link
  the opaque handle to the canonical agent path once the agent id is
  known), `imbue.mngr_latchkey.discovery.LatchkeyDiscoveryHandler`
  (discovery callback that ensures the gateway is up and reverse-tunnels
  it into remote agents), and the latchkey-permissions persistence
  helpers in `imbue.mngr_latchkey.store`.

- Reverse-tunnel manager: `imbue.mngr_latchkey.ssh_tunnel.SSHTunnelManager`,
  duplicated from the minds desktop client (no behaviour change).

The minds desktop client now imports these from the plugin and is left
with only its own UI-layer code (permission dialog, service catalog,
HTML templates).

No user-visible behaviour change in the minds desktop app itself.
