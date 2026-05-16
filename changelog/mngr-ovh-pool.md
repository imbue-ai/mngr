Swap the imbue-cloud pool bake (and the `minds env destroy` walker) from Vultr to OVH:

- `mngr imbue_cloud admin pool create` is now provider-generic. It drops the `MINDS_ROOT_NAME` env detection, adds a required `--region REGION` and repeatable `--tag KEY=VALUE`, lands on `--template main --template ovh` with `@host.ovh` + `--provider ovh`, appends `-b --vps-datacenter=<region>`, and installs + configures `ufw` on every leased VPS before the row hits `pool_hosts`. UFW failures abort the bake.
- New top-level `minds pool` CLI group (`create` / `list` / `destroy`). It requires an activated minds env, auto-injects `--tag minds_env=<active-env>`, and shells out 1:1 to `mngr imbue_cloud admin pool ...`.
- `mngr_ovh.OvhProvider` now honors `MNGR_VPS_EXTRA_TAGS=k1=v1,k2=v2` and attaches each entry as an OVH IAM v2 tag alongside `mngr-provider` / `mngr-host-id`. Parsing is strict with local IAM-key validation so typos fail before the API call.
- `minds env destroy` swaps its Vultr `/instances` walker for an OVH IAM v2 walker (matches by `tags["minds_env"] == <env>` and terminates via `OvhVpsClient.destroy_instance`). The dev-tier Vault path is now `<tier>/ovh` with `OVH_APPLICATION_KEY` / `OVH_APPLICATION_SECRET` / `OVH_CONSUMER_KEY`.
- `OvhProviderConfig.recycle_safety_margin_hours` default drops 24 -> 2 so same-day destroy + create reclaims the cancelled VPS instead of ordering a fresh month.
- `forever-claude-template` gains a `[create_templates.ovh]` block (no plan / datacenter baked in -- region flows in per-invocation, plan defaults from `OvhProviderConfig`). The `[create_templates.vultr]` block stays in place; `mngr_vultr` is still a registered provider for non-pool uses.
- `mngr_ovh` README plan-size info is updated: `vps-2025-model1` is 1 vCPU / 8 GB RAM / 80 GB SSD at ~$7.99/mo (the previous README claim of 2 GB / $7.60 was stale).

The orphaned `apps/minds/imbue/minds/cli/pool.py` duplicate (pre-`mngr_imbue_cloud`) and `apps/minds/imbue/minds/envs/providers/vultr_tags.py` are deleted in the same change. Existing Vultr-backed `pool_hosts` rows are not migrated automatically; operators destroy / drop them by hand after merge.
