The `mngr imbue_cloud admin` group is removed outright (no stub): the operator surface (pool/server provisioning, paid lists, account/workspaces/sweep/relays admin, repair-keys) moved to Imbue's private `minds-admin` CLI, together with the `bake/` package and the operator-only slices modules (bare_metal_db, bare_metal_prep, ordering, pricing, key_repair, autostart_backfill).

The public plugin keeps the user-facing surface only: auth, account, hosts, keys, bucket, shares, sync, and the slice provider backend. Minor version bump to 0.2.0.
