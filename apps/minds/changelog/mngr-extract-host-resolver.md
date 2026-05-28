- `apps/minds`: latchkey permission grant handler falls back to `mngr list
  --format json --on-error continue` when the in-memory discovery cache
  hasn't yet seen the agent. Previously, clicking Approve on a permission
  dialog for a freshly-created agent could silently 503 -- the streaming
  `mngr observe` cache populated by the desktop client lags for agents
  created after subscription start, and the grant code short-circuited on
  the cache miss without ever writing the per-host
  `latchkey_permissions.json` rule. The fallback resolves the agent's
  `host_id` directly from `mngr list`, after which the grant proceeds
  normally. `_resolve_host_id` and `_resolve_host_id_via_mngr_list` are
  now methods on `LatchkeyPermissionGrantHandler` so the unit suite can
  override the fallback via a concrete `_RecordingHandler` subclass
  instead of monkeypatching, and the implementation runs the fallback
  through a self-contained `ConcurrencyGroup.run_process_to_completion`
  (instead of `subprocess.run`) to respect the
  `PREVENT_DIRECT_SUBPROCESS` ratchet.
