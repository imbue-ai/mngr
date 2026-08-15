Multi-relay sharing, phase 1 (blueprint/multi-relay), client side.

`ShareInfo` carries a `relay_endpoints` list (relay id + endpoint) instead of a single `relay_endpoint`; the relays map is region -> endpoint list with no default region (matching the connector's new wire shapes; no back-compat needed pre-deploy).

New `mngr imbue_cloud admin relays list/add/remove` commands manage the connector's relay fleet inventory (MINDS_ADMIN_KEY authenticated).

`mngr imbue_cloud shares status` surfaces the connector's per-relay tunnel login stamps (`relays: [{relay_id, last_login_at}]`), so operators can see which relays a share's tunnel reached.
