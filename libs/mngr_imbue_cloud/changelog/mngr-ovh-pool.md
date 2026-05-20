Swap the imbue-cloud pool bake walker from Vultr to OVH:

- `mngr imbue_cloud admin pool create` is now provider-generic. It drops the `MINDS_ROOT_NAME` env detection, adds a required `--region REGION` and repeatable `--tag KEY=VALUE`, lands on `--template main --template ovh` with `@host.ovh` + `--provider ovh`, appends `-b --vps-datacenter=<region>`, and installs + configures `ufw` on every leased VPS before the row hits `pool_hosts`. UFW failures abort the bake.
- `forever-claude-template` gains a `[create_templates.ovh]` block (no plan / datacenter baked in -- region flows in per-invocation, plan defaults from `OvhProviderConfig`). The `[create_templates.vultr]` block stays in place; `mngr_vultr` is still a registered provider for non-pool uses.
