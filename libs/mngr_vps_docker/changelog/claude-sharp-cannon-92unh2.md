Removed the instance-tag offline mirror entirely: the offline host/agent-record store is now uniform across every offline-capable provider, backed solely by an external `HostStateStore`. AWS/Azure use the object-storage bucket; GCP uses an instance-metadata-backed store. This is an internal refactor with one user-visible consequence for AWS/Azure (see those changelogs): a stopped host's offline metadata now requires the state bucket.

- Deleted the `TagMirrorVpsDockerProvider` intermediate and all per-agent tag-mirror machinery (the `mngr-agent-*` tag read/write, the host-record-from-tags reconstruction, and the `_agent_dicts_from_tags` / `_persisted_agent_dicts_from_instance` / `_offline_host_from_instance` hooks). AWS and Azure now extend `OfflineCapableVpsDockerProvider` directly, like GCP.

- The `_state_store` abstract property and the store-backed offline read/write paths (`_mirror_agent_record`, `_remove_mirrored_agent_record`, `_offline_agent_dicts_for`, `_persist_host_record_externally`, `_delete_host_record_externally`, and the store-aware `to_offline_host`) now live on `OfflineCapableVpsDockerProvider`, since all three providers select a single store.

- `BucketHostStateStore` no longer takes a tag-store `fallback`; `read_host_record` reads the bucket's `host_state.json` directly.

- Added `MissingBucketHostStateStore`: the store a provider selects when its required object-storage bucket has not been provisioned. Mirror writes are no-ops (a running host stays fully usable) but offline reads raise an actionable error pointing at the provider's `prepare` command, instead of silently making a stopped host vanish.

- Added shared module helpers `normalized_tags_to_dict` and `host_name_from_tags` for reading a stopped instance's cheap `mngr-*` identity tags during discovery (the base identity tags `mngr-host-id` / name are still stamped at create; only the per-agent record mirror moved off tags).
