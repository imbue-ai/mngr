# Unabridged Changelog - mngr_azure

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/mngr_azure/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
