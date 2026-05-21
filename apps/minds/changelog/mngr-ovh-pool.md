Swap the `minds env destroy` walker from Vultr to OVH:

- New top-level `minds pool` CLI group (`create` / `list` / `destroy`). It requires an activated minds env, auto-injects `--tag minds_env=<active-env>`, and shells out 1:1 to `mngr imbue_cloud admin pool ...`.
- `minds env destroy` swaps its Vultr `/instances` walker for an OVH IAM v2 walker (matches by `tags["minds_env"] == <env>` and terminates via `OvhVpsClient.destroy_instance`). The dev-tier Vault path is now `<tier>/ovh` with `OVH_APPLICATION_KEY` / `OVH_APPLICATION_SECRET` / `OVH_CONSUMER_KEY`.

The orphaned `apps/minds/imbue/minds/cli/pool.py` duplicate (pre-`mngr_imbue_cloud`) and `apps/minds/imbue/minds/envs/providers/vultr_tags.py` are deleted in the same change. Existing Vultr-backed `pool_hosts` rows are not migrated automatically; operators destroy / drop them by hand after merge.
