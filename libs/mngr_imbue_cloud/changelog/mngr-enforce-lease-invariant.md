- Connector client: new `admin_release_workspace` (operator release of one workspace regardless of owner) and `admin_run_lease_record_sweep` (on-demand lease-vs-record sweep, with `dry_run` and `grace_seconds`) admin-key methods.

- `mngr imbue_cloud sync records delete` (and the client's `delete_sync_record` / `delete_sync_record_by_workspace`) is now refused by the connector with 409 `lease_active` while the workspace still holds its cloud lease; destroy the workspace instead.

- Slice teardown (`LimaSliceVpsClient.destroy_instance`) no longer mistakes the shell's `limactl: command not found` for the instance or disk already being absent; such a failure now raises instead of being skipped.

- `SyncWorkspaceRecord.provider_kind` is documented as what it has always carried: the mngr provider *instance* name (`imbue_cloud_<account-slug>` for cloud rows), which the connector's lease-time record stub now derives from the account email.
