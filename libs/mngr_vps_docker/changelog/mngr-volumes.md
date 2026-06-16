Added two no-op extension hooks to `VpsDockerProvider` -- `_persist_host_record_externally` and `_delete_host_record_externally` -- so a provider can mirror the authoritative on-volume host record to an external store (e.g. an object-storage bucket) for offline reads. The base provider calls them right after every on-volume host-record write (create, stop, snapshot, rename, certified-data sync) and on host destroy/delete. Default behavior is unchanged for providers that do not override them (Vultr, OVH, imbue_cloud).

Added a `HostStateStore` abstract base (`host_state_store.py`): the uniform interface for a provider's external host/agent-record mirror (persist/delete host record, persist/remove/list agent records, read host record). The AWS and Azure providers select one implementation -- bucket-backed or instance/VM-tag-backed -- and call it instead of branching on bucket-vs-tags at every site. This is an internal refactor; an operational store error during offline discovery still propagates to the existing per-provider `--on-error` handling rather than being swallowed.

Unified the AWS/Azure/GCP offline-capable providers' shared machinery into `mngr_vps_docker` (internal refactor, no behavior change):

- `_find_instance_for_host` and the idle-sentinel / host_dir outer-path helpers (`_idle_sentinel_path_on_outer`, `_host_dir_path_on_outer`) now live on `OfflineCapableVpsDockerProvider`, with `IDLE_SENTINEL_FILENAME` as one shared constant.

- New `state_keys` module holds the object-key layout (`hosts/<id>/host_state.json`, `agents/`, `host_dir/`) shared by the S3 and Azure Blob buckets.

- New generic `BucketHostStateStore` (over a `StateBucket` protocol) replaces the per-provider bucket-backed `HostStateStore` implementations.

- New intermediate `TagMirrorVpsDockerProvider` holds the tag-based host/agent reconstruction shared by AWS and Azure (the only per-provider knob is `_host_name_tag_key`); GCP keeps its metadata-based reconstruction by extending `OfflineCapableVpsDockerProvider` directly.

- New `testing.seed_stopped_host_record` helper, shared by the provider test suites.
