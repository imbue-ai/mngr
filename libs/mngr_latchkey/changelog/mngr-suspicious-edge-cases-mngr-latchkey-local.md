Hardened suspicious edge-case handling across the latchkey package:

- `DiscoveryStreamConsumer` now subscripts `_provider_by_agent_id` directly
  (rather than defaulting to `"unknown"`) so a broken host/provider tracking
  invariant surfaces loudly instead of injecting a synthetic provider name.
- `link_opaque_permissions_to_host` now refuses to overwrite the canonical
  host permissions path when it is unexpectedly a symlink, instead of silently
  clobbering it.
- The `services.json` generator (`generate_services_json.py`) now raises on a
  detent schema whose `required`/`properties` field is present but wrong-typed,
  rather than coercing it to empty and skewing the scope/permission catalog.
- Added clarifying comments documenting why several deliberately-defensive
  handlers behave as they do (broad callback-isolation catch in the discovery
  stream, the gateway-start log-and-continue path, malformed-record recovery in
  `load_forward_info`, the stderr-substring auth-browser retry gate, the
  re-checked latchkey binary guard, and the type-narrowing `isinstance` filter
  in `_parse_auth_options`).

No user-facing behavior change in normal flows; the new raises only fire on
previously-silent "shouldn't happen" states.
