# Unabridged Changelog - mngr_azure

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/mngr_azure/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-23

SSH host keys are now unique per host (inherited from the shared VPS provider): each host gets its own VPS/VM-root and container sshd host keypair at create time rather than sharing one keypair across every host the provider instance created. Pause/resume of hosts created before this change still works via a fallback to the legacy provider-global key.

## 2026-06-22

Report an unauthenticated Azure provider consistently with the other cloud providers, and validate credentials eagerly.

A missing subscription or unusable credential now raises the shared `ProviderNotAuthorizedError` (still a `ProviderUnavailableError`, so read paths treat it as unavailable). In `mngr list` this surfaces as one consistent error line and a non-zero exit.

Azure previously validated only the subscription id: `DefaultAzureCredential` authenticates lazily, so an unauthenticated environment surfaced as a confusing API error on the first real call. The provider now eagerly requests a management-scope token at construction so the failure is reported up front, matching how AWS/GCP resolve credentials at construction.

## 2026-06-19

Renamed the `_AGENT_TAG_FIELDS` constant imported from `mngr_vps` to the
public `AGENT_TAG_FIELDS` (matching its sibling `AGENT_TAG_PREFIX`), so the
Azure tag-mirror code no longer imports a private name across modules. No
behavior change.


Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename and the
accompanying class renames (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerError` -> `VpsError`, etc.). Import-only; no behavior
change.


Enabled bare placement (`isolation=NONE`): an Azure OS shutdown does not halt
compute billing, so the bare agent's idle `shutdown.sh` runs the ARM
self-deallocate directly (the same call the container idle watcher uses), keeping
the self-deallocate role assignment and skipping the host-side sentinel watcher.

Added bare-placement (`isolation=NONE`) release tests, and fixed a resume bug they
caught: `start_host` read the host record via the Docker volume, which a bare host
does not have, so it now resolves the store through the realizer.

Bugfix: `mngr start` of a deallocated Azure host now re-mirrors the resumed host
record to the external (Blob bucket) store, so offline / `mngr list` reads no
longer report a just-resumed Azure VM as STOPPED until the next mirroring write.

``stop_host`` / ``start_host`` moved to the shared base ``OfflineCapableVpsProvider``; Azure now supplies only the deallocate/start hooks plus the static-IP known_hosts rebind no-ops. The shared base is what now guarantees the resume-mirror above happens on every provider.

Updated the host_dir sync to call the realizer's `host_dir_path_on_outer`
directly after the redundant `_host_dir_path_on_outer` forwarder was removed
from the shared VPS provider. No behavior change.

The idle-watcher install, the host_dir-to-bucket sync daemon install/before-pause, and the best-effort `_on_host_finalized` step runner all moved to the shared `OfflineCapableVpsProvider`. Azure now supplies only its hooks: the `Azure VM` display name; the ARM self-deallocate `.service` body and the curl-plus-deallocate-script outer prep (`_idle_watcher_service_unit` / `_prepare_idle_watcher_outer`); the bare-placement self-deallocate `shutdown.sh` (`_write_bare_idle_shutdown_script`); the sync gate (config flag + bucket + managed identity present), azcopy install, `azcopy sync` `.service` body, and blob target URL; and the self-deallocate role assignment prepended via `_post_finalize_steps`. The host-side systemd unit names changed from `mngr-azure-idle-watcher` / `mngr-azure-host-dir-sync` to the shared `mngr-idle-watcher` / `mngr-host-dir-sync`. Behavior-preserving otherwise.

Updated the VPS build-arg parsing imports to point at the new `imbue.mngr_vps.build_args` module (moved out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.

Updated imports for `TagMirrorVpsProvider`, `AGENT_TAG_PREFIX`, `AGENT_TAG_FIELDS`, and the host_dir-sync unit symbols to the new `imbue.mngr_vps.instance_offline` module (split out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.

The shared offline read-side reconstruction moved up into the new `KeyValueMirrorVpsProvider` base that `TagMirrorVpsProvider` now extends, so the Azure provider's host-name hook was renamed `_host_name_tag_key` -> `_host_name_key` and its tag-mirror agent-record write call now invokes the renamed `_agent_field_items` (formerly `_agent_field_tags`). The 256-char tag-value cap is still applied (the base reads it from the new `_max_value_len` hook). Internal refactor; no user-visible behavior change.

The host_dir-sync daemon now runs its `azcopy sync` command from an installed `/usr/local/sbin/mngr-host-dir-sync.sh` script (referenced directly by the oneshot `.service`'s `ExecStart`) instead of an inline `ExecStart=/bin/sh -c '...'`, removing a layer of systemd + shell quoting around the host_dir path and blob URL; the MSI `Environment=` lines stay on the unit. The sync and self-deallocate `.service` units are now rendered via the shared `render_systemd_unit` helper. No behavior change.

`BlobVolume` is now a thin subclass of the shared `BaseObjectStoreVolume` (in
`mngr_vps.state_bucket_base`), supplying only the Azure Blob primitives and an
error seam (`_translate_errors` / `_is_not_found` / `_bucket_error_type`); the
listing / existence / read / write / delete logic it duplicated with the AWS
`S3Volume` now lives on the base. `BlobStateBucket`'s `_get_object` /
`_delete_object` / `_prefix_has_objects` likewise moved to `BaseStateBucket`,
leaving the bucket with just its raw Blob primitives and the seam. The
one-at-a-time blob delete (Blob storage has no batch delete) stays Azure-specific.
No user-visible behavior change.

`mngr azure prepare` / `cleanup` now resolve their `[providers.<name>]` block and refuse-on-existing-VMs via the shared `mngr_vps.cli_helpers`, and `AzureProviderConfig` lifts `allowed_ssh_cidrs` / `associate_public_ip` into shared config bases instead of carrying Azure-local copies. The cleanup refusal when VMs still exist now raises the unified `ManagedResourcesExistError` (previously `AzureProviderError`) so the message matches the other clouds. `allowed_ssh_cidrs` is now typed `ScalarStrTuple` (matching AWS) rather than a plain tuple, so a higher-precedence config layer that sets it replaces the whole list rather than being flagged as narrowing; the config key and default are unchanged.

Further internal dedup against the shared offline layer (no user-visible behavior change): the Azure VM-tag `HostStateStore` (`_VmTagHostStateStore`) is gone in favor of the shared `TagHostStateStore`, with Azure supplying only a `_remove_instance_tags` hook; the `_state_store` selection now comes from the base via new `_bucket_error_type` / `_bucket_label` hooks (`_state_bucket` is unchanged); the host_dir-sync daemon install now returns a `HostDirSyncInstallPlan` (returning `None` to skip when the bucket-write managed identity is absent, preserving the prior skip behavior) consumed by the shared `BucketHostDirBackend.install_sync` skeleton; `_list_provider_vps_hostnames` is inherited from the shared base (the deallocated-VM-keeps-its-Static-IP rationale moved to that base method); and `_create_vps_instance` uses the shared `_require_parsed` helper.

Localized cleanup (no user-visible behavior change): the bare-placement `_write_bare_idle_shutdown_script` now writes its ARM self-deallocate `shutdown.sh` via the shared `VpsProvider._write_shutdown_script` plumbing instead of repeating the mkdir/write/chmod sequence.

Integrated the `mngr/volumes` offline-store simplification (commit `f8bb5c0a5`): the per-agent instance-tag mirror is removed in favor of a single uniform external `HostStateStore` per provider -- AWS/Azure use their object-storage state bucket as the sole offline store (a stopped host's offline metadata now requires the bucket; the provider's `_state_store` raises an actionable `missing_state_bucket_error` pointing at `mngr <cloud> prepare` when the bucket is absent), and GCP uses a lossless instance-metadata-backed store (full host record + one JSON value per agent). AWS/Azure/GCP now extend `OfflineCapableVpsProvider` directly. This supersedes the earlier-on-this-branch tag-mirror dedup (the lifted `TagHostStateStore` / `KeyValueMirrorVpsProvider` / `TagMirrorVpsProvider` are gone); the realizer architecture, the systemd-unit hardening, and the cli/config/state-bucket dedup are retained. No behavior change for container hosts beyond the offline-metadata-requires-bucket consequence noted above.

Bugfix: a running bare (`isolation=NONE`) host is now discoverable and reachable
with the default provider config -- `mngr conn`/`list`/`stop`/`start`/`destroy`
no longer need `-S providers.<name>.isolation=NONE` at connect time. Instances
now carry a `mngr-isolation` tag stamped at create (alongside `mngr-host-id` /
`mngr-provider`), so discovery reads the host's placement from the cloud API
without SSH and probes it with the matching realizer. Pre-existing hosts have no
tag and default to container, preserving prior behavior.

Behavior-preserving dedup against the shared offline layer. The Azure `_state_store` / `_host_dir_backend` cached properties are now thin wrappers over the shared `OfflineCapableVpsProvider._select_bucket_store` / `_select_bucket_host_dir_backend` (supplying only the resolved Blob bucket, its label, and `mngr azure prepare`). The near-identical `_offline_discovered_host_from_instance` is dropped in favor of the shared default; Azure now sets only the `mngr-host-name` host-name tag key via the new `_host_name_tag_key()` hook. No user-visible behavior change.


Bugfix: `rename_host` now re-stamps the cheap `mngr-host-name` VM tag that offline
discovery reads (previously stamped only at create), so a host renamed and then
stopped lists under its new name rather than its old one. The re-stamp merges into
the VM's existing tags rather than replacing them, preserving the other index tags
(`mngr-host-id`, `mngr-provider`, etc.).

Doc: removed a stale README note about speculative `create_snapshot` /
`list_snapshots` / `delete_snapshot` client methods that no longer exist.

Internal cleanup (no behavior change): renamed `_build_self_deallocate_script`'s
`sentinel_on_outer` parameter to `sentinel_to_remove` (clearer that `None` means no
sentinel to delete, i.e. the bare path), and dropped the now-unused
`sentinel_on_outer` parameter from the `_idle_watcher_service_unit` override.

Trimmed the README to user-relevant content (removed internal implementation details, release-test instructions, and roadmap notes) and tightened it for concision.

Documented the offline state storage account (the `Storage Blob Data Contributor` requirement, `state_storage_account_name`, and the `prepare`/`cleanup` behavior) and the offline `host_dir` capture.

Added `test_provider_release_trip1` to the Azure release suite: a single-boot full-lifecycle trip (create, exec, stop, real `--stop-host` deallocate, start, persistence, snapshot, out-of-band kill, gc, backend-clean) parametrized over container and bare isolation, built on the shared provider release harness. Also added `test_provider_release_trip3` (snapshot survives destroy); on Azure the docker-commit snapshot is not portable, so the trip asserts that documented divergence (the snapshot is gone after destroy).

Retired the old per-step Azure lifecycle release tests now that the trips supersede them: `test_provider_lifecycle_create_exec_and_destroy`, `test_provider_lifecycle_create_stop_start_destroy`, and `test_bare_provider_lifecycle_create_exec_and_destroy`. The bare-shape check the bare test owned (the agent shell is the VM's own root -- `/var/lib/mngr-host` present, no `/.dockerenv`) now runs inside Trip 1 for the NONE-isolation parametrization. `test_provider_create_builds_dockerfile_on_vm` (the remote Dockerfile-build-on-VM path, not covered by any trip) is kept.

Also added `test_provider_release_trip4` (error classification): a no-boot CLI trip asserting `mngr create` with no resolvable subscription surfaces the contract `ProviderUnavailableError`, and that a `--vps-*` build arg is rejected with the migration hint. Azure is the one provider with curated help text, so the trip asserts the guidance points at `az login` / the subscription setup steps.

Also added `test_provider_release_trip2` (idle auto-shutdown contract), parametrized over container and bare isolation: it creates an idle host, polls until the Azure VM is deallocated (billing stops), then resumes via `mngr start` and asserts a pre-shutdown marker survived.

Azure also opts into Trip 1's offline-host_dir read (`supports_offline_host_dir`): with `MNGR_RELEASE_TEST_OFFLINE_HOST_DIR=1`, the trip asserts a stopped host's host_dir marker is served from the Blob state bucket via `mngr file get --relative-to host`.

## 2026-06-18

Replaced the VM tag mirror with a required Azure Blob **state bucket** (a private Storage account + container) as the offline store for Azure hosts. A deallocated VM's offline host/agent records (so `mngr list` / `mngr start` / `mngr event` work while it is stopped) now live in the bucket instead of `mngr-agent-<id>-*` VM tags. This removes the tag mirror's silent 256-char `labels` drop and lets a stopped VM's *full* host record (config, IP, host keys) be reconstructed rather than a lossy subset. The bucket is **required**, with no VM-tag fallback: mngr raises an actionable error pointing at `mngr azure prepare` when it is absent -- on the `mngr create` / `mngr label` write path as well as on offline reads -- and transient Blob errors on a mirror read/write propagate.

- `mngr azure prepare` creates a private Storage account + Blob container ("state bucket") that holds the full host record and per-agent records, written by the mngr host machine using the operator's own `DefaultAzureCredential` (no storage keys; the operator needs `Storage Blob Data Contributor` on the account plus the account create/delete permission). The account name defaults to a deterministic `mngrst<hash>` (override with `state_storage_account_name` in `[providers.azure]`); the container is `mngr-state`. The bucket is prepare's primary job: a missing storage permission or any create failure fails the command.

- `mngr azure prepare` now also grants **the operator's own principal** (the user or service principal running `prepare`) the `Storage Blob Data Contributor` role scoped to just the state account. Azure splits control plane from data plane -- creating the account (or holding Owner/Contributor) does not include data-plane blob access -- so without this grant the operator's offline reads/writes (`mngr list` / `mngr start` on a deallocated host) failed with `AuthorizationPermissionMismatch`. The operator principal is resolved from the `oid` claim of a management-scope token (no Microsoft Graph dependency); the grant needs `Microsoft.Authorization/roleAssignments/write` and fails prepare if denied -- with an actionable error (the account was already created) telling the operator to grant the role out of band, or re-run `prepare` as a principal that can assign roles, rather than just the bare `AuthorizationFailed` message. It grants only the prepare-runner: other operators need their own grant (re-run `prepare` as them, or assign the role out of band). AWS and GCP are unaffected (their offline stores have no control/data-plane split). Fixes a bug surfaced by the Azure provider release test (stop-host -> start offline reconstruct).

- `mngr azure cleanup` deletes the account (and, with the new `--force` flag, its leftover orphaned state -- otherwise it refuses to delete a non-empty account).

Added an offline `host_dir`, **on by default** (new `is_offline_host_dir_enabled` provider config field): a deallocated VM's `host_dir` is readable without SSH, so `mngr event` / `mngr transcript` work against a stopped agent.

- Capture is **operator-driven** -- it needs no VM managed identity. At `mngr stop`, mngr (already SSH-connected and holding the bucket credentials) reads the VM's `host_dir` off the box and uploads it to the bucket's `hosts/<host_id>/host_dir/` prefix with the operator's own credentials (the operator's `Storage Blob Data Contributor` role on the state account, granted by `prepare`). So `mngr azure prepare` provisions no host-dir managed identity, VM create attaches none, and `cleanup` deletes none. Offline reads serve `host_dir` back from the bucket.

- Limitation: capture happens only at `mngr stop`. A VM that idle-self-deallocates (or crashes) is **not** captured -- its offline `host_dir` then reflects its last `mngr stop` (or is empty if never stopped that way); the state *records* are unaffected (always operator-written). An empty `host_dir` prefix reads as no volume. Set `is_offline_host_dir_enabled = false` to disable the capture entirely.

Test-only: added unit coverage for the host-identity raise-on-failure paths (provisioning and cleanup) via a `delete_error` failure-injection seam on the managed-identity fake.

Follow-up cleanup: removed the now-orphaned `AzureVpsClient.add_tags` / `AzureVpsClient.remove_tags` client methods (and their unit tests). They only ever existed to push per-agent records into VM tags for the old tag mirror, which the state bucket replaces; nothing reachable called them.

## 2026-06-17

Added a VM-level stop/start lifecycle for Azure hosts. `mngr stop` now **deallocates** the VM (`AzureVpsClient.deallocate_instance`) -- which actually halts compute billing, unlike an OS-level shutdown that leaves the VM "Stopped (not deallocated)" still billing -- preserving the OS disk so a paused agent costs only disk storage; `mngr start` re-allocates it (`start_instance`). The public IP is static, so it (and the SSH host keys) survive the stop, and resume needs no known_hosts rebind.

A deallocated VM stays discoverable: its host name and per-agent records are mirrored into VM tags, so `mngr list` and `mngr start <agent>` keep working while the VM is deallocated.

Idle agents self-deallocate (true cost parity with AWS/GCP). Because an Azure guest shutdown does not halt billing, each VM is created with a system-assigned managed identity and the in-VM idle watcher calls the ARM `deallocate` API (via its IMDS token) when idle. `mngr azure prepare` creates a least-privilege custom role (`mngr-self-deallocate`) and each VM gets a role assignment scoped to itself. This degrades gracefully: if the operator lacks the role-assignment privilege (Owner / User Access Administrator), idle self-deallocate is disabled with a clear warning, and `mngr stop`/`start` remain the way to halt billing. On a refused deallocate the in-VM watcher logs and exits rather than powering off, since an Azure OS shutdown does not halt billing (it would only strand the VM unreachable while it keeps billing).

Adds the `azure-mgmt-authorization` dependency (for the custom role + role assignment).

`deallocate_instance` / `start_instance` now honor their `timeout_seconds`: the long-running operation is bounded and raises `VpsProvisioningError` if it outlasts the deadline (matching the AWS/GCP clients), instead of blocking indefinitely.

Internal: Azure's stopped-host offline discovery and resolution, plus its deallocate/start lifecycle and idle-watcher install, now come from the shared `OfflineCapableVpsDockerProvider` base instead of Azure-specific copies; Azure supplies only its specifics as hooks (deallocate/start the VM, no-op known_hosts rebind for its static IP, the self-deallocate idle action + role assignment). No behavior change. The `_HOST_NAME_PREFIX` constant is renamed `_HOST_NAME_TAG_PREFIX` to match AwsProvider.

## 2026-06-16

## Azure provider

- New `azure` provider backend (`mngr_azure`) that runs agents in Docker containers on Azure Virtual Machines. A thin adapter over the shared `mngr_vps_docker` base, like `mngr_aws` / `mngr_gcp` / `mngr_vultr`.

- Credentials are resolved exclusively via Azure's `DefaultAzureCredential` (an `az login` session, a service principal via `AZURE_*` env vars, or a managed identity) — `[providers.azure]` config has no credential fields, matching the Modal / AWS / GCP convention.

- The subscription is resolved automatically (config `subscription_id` > `AZURE_SUBSCRIPTION_ID` env > the Azure CLI's active subscription, read from `azureProfile.json`), so `--provider azure` works with no config after `az login` — the same way the GCP provider uses the active gcloud project. A `[providers.azure]` block is optional.

- New `mngr azure prepare` CLI command (registered via `register_cli_commands` hookimpl) does the one-time privileged setup: it registers the `Microsoft.Compute` / `Microsoft.Network` / `Microsoft.Storage` resource providers (new subscriptions start unregistered) and creates the mngr-owned resource group + vnet + subnet + NSG (the NSG is attached to the subnet, opening tcp/22 and the container SSH port to `allowed_ssh_cidrs`). The hot path in `AzureVpsClient.create_instance` is lookup-only (`resolve_subnet_id`), so `mngr create --provider azure` needs only VM/NIC/IP-create permissions (no network-management permissions); a missing subnet points the user at `mngr azure prepare`.

- New `mngr azure cleanup` CLI command, the safe inverse of `prepare`: it deletes the mngr-owned resource group (cascading the vnet/subnet/NSG in one `begin_delete`). It refuses (deletes nothing) while any mngr-managed VM still exists in the group, so it cannot strand a running agent, and only deletes a group it owns (tagged `managed-by=mngr` by `prepare`). Idempotent. Backed by `AzureVpsClient.delete_managed_resource_group()` and `list_mngr_managed_vms()`.

- `mngr azure prepare` and `mngr azure cleanup` read their defaults from the user's `[providers.<name>]` settings.toml block (selected with `--provider`, default `azure`), matching `mngr aws prepare` and `mngr gcp prepare`, so the resource group / region / vnet / subnet / NSG names line up with what the runtime `mngr create` path resolves. CLI flags override the resolved config, which overrides class defaults. A warning is logged if the named `--provider` block exists but is not an Azure backend.

- **Per-host create with delete-options cascade**: each create makes a Standard-SKU static public IP + a NIC bound to the prepared subnet + a VM. The OS disk, NIC, and public IP are all created with `delete_option=Delete`, so `destroy_instance` deletes only the VM and the rest is reaped automatically — no orphaned resources. (Azure API payloads are built with the typed azure-mgmt SDK models, not plain dicts, because the compute SDK does not remap snake_case dict bodies to the ARM wire format.)

- **Failed-create cleanup**: the public IP + NIC are created before the VM, so a VM create that fails (e.g. `SkuNotAvailable` / quota) would orphan them. `create_instance` deletes them in a `finally` when the VM create did not succeed. Azure reserves a NIC for its would-be VM for 180s after a capacity failure, so when the immediate delete is blocked the orphan is reclaimed at GC time: `reclaim_orphaned_network_resources` deletes unattached, mngr-tagged NIC/IPs older than the reservation window — an age gate that never disturbs an in-flight concurrent create — and is invoked by `mngr gc` (which also runs after every `mngr destroy`) via the provider's `gc_provider_resources` hook. The session-end test scanner likewise reclaims orphaned NIC/IPs so a capacity-failed release-test create leaks nothing.

- **SSH keys** are injected inline at VM create (`os_profile.linux_configuration.ssh`); Azure has no per-key resource, so `upload/list/delete_ssh_key` use an in-memory map (like `mngr_gcp`). The shared cloud-init also forwards the key into root's `authorized_keys`, so mngr's root SSH works regardless of the admin user. Cloud-init `custom_data` is base64-encoded as Azure requires.

- **Image**: Debian 12 by default (`Debian:debian-12:12-gen2`), matching the Debian-12 default of the other mngr providers (aws / gcp / ovh / vultr). It runs cloud-init with the Azure datasource so the shared bootstrap works unchanged. Configurable via `image_publisher` / `image_offer` / `image_sku` / `image_version`. **Default VM size** `Standard_B2s` (B-series is the family most likely to have nonzero quota on a fresh pay-as-you-go subscription).

- **Snapshots** are managed-disk snapshots of the VM's OS disk (`create_option=Copy`).

- **Spot capacity opt-in**: presence-only `--azure-spot` build arg flows through `ParsedAzureBuildOptions(ParsedVpsBuildOptions)` and a `_create_vps_instance` override to set `priority=Spot`, `eviction_policy=Delete`, `billing.max_price=-1`. Azure may reclaim on capacity pressure; the host is deleted (not stopped) on eviction, matching AWS spot's terminate-on-reclaim. The shared `VpsClientInterface.create_instance` contract is unchanged — the spot kwarg lives on the Azure-specific client signature.

- **Azure build args use the `--azure-` prefix** (`--azure-region=`, `--azure-vm-size=`, `--azure-spot`); the generic `--vps-*` forms raise a migration error pointing at them.

- **Auto-shutdown caveat (Azure-specific)**: `auto_shutdown_seconds` schedules cloud-init `shutdown -P +N`, but an OS shutdown on Azure leaves the VM "Stopped (not deallocated)", which still bills for compute — Azure has no native delete-after-duration like AWS/GCP. This matches the Vultr provider's documented behavior. The real cost backstop for tests is the session-end orphan scanner (below). A future improvement is true self-deletion via a managed identity + a cloud-init systemd timer.

- VMs are tagged `mngr-provider`, `mngr-host-id`, `mngr-created-at`, and `managed-by=mngr`; discovery filters the resource group's VM list by `mngr-provider` (client-side, since Azure has no server-side tag filter on the VM list).

- When Azure is unresolvable (no subscription, or an unusable credential), `build_provider_instance` raises `ProviderUnavailableError` with Azure-specific, actionable guidance (set `AZURE_SUBSCRIPTION_ID` / run `az login` / run `mngr azure prepare`) rather than the generic provider-unavailable help text. Because Azure's state is then *unknown* (agents may still exist on a subscription we transiently couldn't read), `mngr list` prints a warning rather than silently dropping the azure provider and its agents from the listing.

- `read_az_cli_default_subscription` retries a torn read of `azureProfile.json`. The az CLI rewrites that file in place (not atomically) on token refresh and on most `az` commands, so a read racing a write can momentarily get a truncated/undecodable file; the read is retried a few times (spaced by a short sleep so the concurrent writer can finish) before giving up. A genuinely absent file short-circuits to `None` with no retries.

- `AzureProviderConfig.get_subscription_id` raises the custom `AzureSubscriptionError` (in the `mngr_azure.errors` module) when no subscription can be resolved. It subclasses both `MngrError` and `ValueError`, so the backend's `except ValueError` (which wraps the failure into `ProviderUnavailableError`) still catches it.

- `allowed_ssh_cidrs` defaults to `0.0.0.0/0` and is fail-open, matching the AWS and GCP providers. `mngr azure prepare` with no `--allowed-ssh-cidr` falls back to that default and creates a world-open NSG allow rule, logging a warning prompting you to tighten it for production (a `0.0.0.0/0` range is also warned at create time). SSH auth is key-only (`disable_password_authentication=True`), so an open NSG exposes the port but not a usable login. Setting `allowed_ssh_cidrs = []` opts out entirely: the NSG is created with no SSH allow rule, so its implicit default-deny leaves instances unreachable from outside the vnet (the analog of AWS's zero-ingress security group; an Azure security rule with an empty source is API-rejected, so "no ingress" is the absence of the rule).

- The `mngr create --provider azure` pre-create hook runs a read-only subnet pre-flight (`resolve_subnet_id`) before uploading the SSH key or creating the VM, so a first-time user who skipped `mngr azure prepare` gets the clean "run mngr azure prepare" message immediately rather than mid-create under a "Host creation failed, attempting cleanup..." line. Mirrors the GCP firewall pre-flight.

- `mngr azure prepare` and `mngr azure cleanup` output is `--format`-aware (matching `mngr aws` / `mngr gcp`): a single result line in human mode, a structured object in `--format json`, and a `prepared` / `cleaned_up` event in `--format jsonl`. The structured forms carry a `created` / `deleted` boolean so callers can tell a first-run create from an idempotent no-op (`ensure_network` returns an `AzureNetworkPrepareResult` with `was_created`, derived from a resource-group existence check).

- The `mngr azure cleanup` refusal (a VM still exists) raises the typed `AzureProviderError`. Since `AzureProviderError` is an `MngrError` (a `ClickException` subclass) it renders as a clean CLI message, while the core `_perform_cleanup` stays independent of the click runtime and testable against the domain type. The `prepare` / `cleanup` callbacks wrap the azure SDK's `AzureError` into a typed error; an `AzureSubscriptionError` (and the `VpsApiError` from `ensure_network` / `delete_managed_resource_group`) propagates with its specific type. Mirrors the `mngr gcp` error typing.

- `_make_vm_name` returns a validated `AzureVmName` (a `NonEmptyStr` subtype, in `mngr_azure.client`). Its constructor re-asserts the coerced name satisfies Azure's VM-name shape (`[a-z0-9-]`, no leading/trailing dash, at most 64 chars), raising `InvalidAzureIdentifierError` if not, so a regression in the name coercion fails fast in mngr rather than as an opaque Azure API error. Mirrors `mngr_gcp`'s `GceInstanceName`. (Azure tags accept nearly any string, so there is no label-value analog of `GceLabelValue`.)

## Tests

- Release tests are triple-gated by `MNGR_AZURE_RELEASE_TESTS=1`, credential presence, and a resolvable subscription. A Modal-style `pytest_sessionfinish` hook in `conftest.py` scans the resource group for any VM tagged `mngr-pytest-launched` older than 1h at session end, force-deletes leaks, and fails the session. `AzureProvider` refuses to create a VM under pytest without `auto_shutdown_seconds` set so the scanner's TTL is well-defined.

- The session-end leak cleanup awaits its delete operations (`begin_delete(...).result()`) for leaked VMs and orphaned NICs / public IPs, so a server-side delete failure surfaces instead of being silently dropped (`begin_delete` returns immediately). Matches the production `destroy_instance` path and the analogous GCP conftest.

- A release test (`test_provider_create_builds_dockerfile_on_vm`) covers the remote Dockerfile-build path the `-t azure` template uses: it builds a small Dockerfile on a real Azure VM (native `docker build`) and asserts the agent container runs FROM the built image via a baked-in marker. Verified passing end-to-end against a real subscription.

- The release-test "Run manually" command invokes `uv run pytest` directly with `PYTEST_MAX_DURATION_SECONDS=1200`; the suite-time budget is a wall-clock guard (not a per-test timeout) sized for the ~13-minute full run.

- Release tests skip only when the opt-in (`MNGR_AZURE_RELEASE_TESTS=1`) is unset; opting in but lacking resolvable credentials or a subscription *fails* loudly (with actionable guidance) rather than reporting as "skipped", so a run the user explicitly requested but that cannot reach Azure is visible. The release-test name prefix is `mngr-test-`. The test-only `azure_credentials_available` / `get_default_subscription_id` helpers route through `AzureProviderConfig` (the same credential + subscription resolution production runs, including the active-`az`-subscription fallback) so the gate and production agree on what is reachable. Mirrors the analogous `mngr gcp` / `mngr aws` test infra.

Removed the dead VPS client methods `create_snapshot`, `delete_snapshot`, `list_snapshots`, and `list_ssh_keys` (and the now-unused `_os_disk_id` helper) from `AzureVpsClient`. These had no production callers and are being dropped from the shared `VpsClientInterface`. The corresponding unit and release tests, plus the now-unused `FakeSnapshotsOperations` test helper, were removed as well.

`AzureProviderConfig.get_subscription_id` now raises the custom `AzureSubscriptionError` (in the new `mngr_azure.errors` module) instead of a bare `ValueError` when no subscription can be resolved. It subclasses both `MngrError` and `ValueError`, so the backend's `except ValueError` (which wraps the failure into `ProviderUnavailableError`) is unaffected.


The `mngr_azure` README's snapshot note now states the Azure client exposes no managed-disk-snapshot surface (rather than describing the removed `create_snapshot` / `list_snapshots` / `delete_snapshot` methods).
