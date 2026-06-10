# Azure Provider

## Overview

* One new package, `libs/mngr_azure/`, that adds an `azure` provider backend. It is a thin adapter over the existing `VpsDockerProvider` base in `mngr_vps_docker` -- the same pattern as `mngr_aws`, `mngr_gcp`, and `mngr_vultr`. Agents run in a Docker container on an Azure Linux VM; the VM stays up, the container is the mngr "host".
* The only substantial new code is `AzureVpsClient` (implements the ~11-method `VpsClientInterface` against the Azure management SDK) plus a ~150-line `AzureProvider`/`AzureProviderBackend` and config/cli. Everything else (host lifecycle, SSH, cloud-init, discovery, listing, container snapshots, stop/start) is inherited unchanged.
* Auth via `azure-identity`'s `DefaultAzureCredential`, which transparently uses the developer's `az login` session locally and a service principal (`AZURE_*` env vars) in CI. Subscription id is the only required identifier in config; credentials are never stored in mngr config (matches the AWS/GCP convention).
* SDK packages: `azure-mgmt-compute`, `azure-mgmt-network`, `azure-mgmt-resource`, `azure-identity`. The first three are not yet vendored; `azure-identity`/`azure-core` already are.

## Key architectural choices

### Resource model: dedicated mngr-owned resource group, created by `mngr azure prepare`

Azure nests every resource in a *resource group* (RG) inside a *subscription*, with no AWS/GCP equivalent. The one-off, shared infrastructure -- RG, vnet, subnet, and an NSG (network security group) -- is created once by **`mngr azure prepare`**, mirroring `mngr aws prepare` / `mngr gcp prepare`. `prepare` also registers the `Microsoft.Compute` / `Microsoft.Network` / `Microsoft.Storage` resource providers (new subscriptions start unregistered) and polls until they are `Registered`.

* The NSG is attached to the **subnet** at prepare time (not per-NIC), so per-host create does no NSG work -- the NIC inherits subnet rules. NSG opens inbound tcp/22 and tcp/`container_ssh_port` to the configured `allowed_ssh_cidrs`, **fail-closed**: empty CIDRs => `prepare` refuses rather than create a wide-open rule (matches GCP).
* The hot `create_instance` path is **lookup-only** (resolve the existing RG/subnet/NSG), needing no network-write permissions -- same admin/developer split as AWS/GCP. A missing RG/subnet raises a `MngrError` pointing at `mngr azure prepare`.
* Defaults: region `westus`, RG `mngr`, vnet `mngr-vnet`, subnet `mngr-subnet`, NSG `mngr-nsg`. All overridable in config.

### Per-host create: public IP + NIC + VM, with delete-options cascade

`create_instance` (per host) creates, in order: a Standard-SKU Static **public IP**, a **NIC** bound to the prepared subnet + that public IP, then the **VM** referencing the NIC, the image, the VM size, the admin user + injected SSH public key, base64 cloud-init as `custom_data`, and tags.

* **Cascade on delete:** the VM is created with `os_disk.delete_option=Delete`, NIC `delete_option=Delete`, and public-IP `delete_option=Delete`. So `destroy_instance` deletes only the VM and the OS disk + NIC + public IP are reaped automatically -- no multi-resource teardown, no leaks. (This is the modern azure-mgmt-compute capability that makes Azure teardown as clean as AWS terminate.)
* **SSH keys:** injected inline at VM create via `os_profile.linux_configuration.ssh.public_keys` (no per-key Azure resource). `upload_ssh_key`/`list`/`delete` use an in-memory map exactly like `mngr_gcp` (Azure, like GCE, keeps keys only in per-VM config). The shared cloud-init also forwards the key into root's `authorized_keys`, so mngr's root SSH works regardless of the admin user.
* **Image:** Ubuntu 24.04 LTS gen2 (Canonical) by default -- Ubuntu runs cloud-init with the Azure datasource, so the shared `mngr_vps_docker` cloud-init flow works unchanged. Configurable via publisher/offer/sku/version fields.
* **Default VM size:** `Standard_B2s` (burstable, 2 vCPU / 4 GB), chosen because B-series is the family most likely to have nonzero quota on a fresh pay-as-you-go subscription.

### Status / IP / listing / snapshots / spot

* `get_instance_status`: VM instance-view power state -> `VpsInstanceStatus` (running->ACTIVE, deallocating/deallocated/stopped->HALTED, etc.); 404 -> UNKNOWN.
* `get_instance_ip`: read the VM's public-IP resource `ip_address`; raise `VpsProvisioningError` until assigned (drives `wait_for_instance_active`).
* `list_instances(provider_tag)`: list VMs in the RG, filter client-side on the `mngr-provider` tag (Azure has no server-side tag filter on VM list within an RG). Normalized to the same `{id, main_ip, state, tags}` dict shape the other providers return.
* **Snapshots:** managed-disk snapshots. `create_snapshot` makes a `Snapshot` with `creation_data.create_option=Copy`, `source_resource_id=<VM OS managed disk id>`; `list`/`delete` via the snapshots client. Same disk-snapshot model as GCP/AWS.
* **Spot (`--azure-spot`):** VM `priority=Spot`, `eviction_policy=Delete`, `billing.max_price=-1` (pay up to on-demand; evicted only on capacity, and *deleted* not stopped on eviction -- matching AWS spot's terminate-on-reclaim semantics). Presence-only build arg, plumbed via an `AzureProvider`-specific `ParsedAzureBuildOptions` + `_create_vps_instance` override, exactly like `--aws-spot`.

### Auto-shutdown: best-effort `shutdown -P`, matching Vultr (billing caveat documented)

Azure has no native "delete after N minutes" primitive (AWS `InstanceInitiatedShutdownBehavior=terminate`, GCP `max_run_duration`), and an OS-level `shutdown -P` leaves the VM **Stopped (not deallocated)**, which still bills for compute. This is exactly **Vultr's** situation, and Vultr's accepted behavior in this codebase is: `auto_shutdown_minutes` does the shared cloud-init `shutdown -P +N` (OS halts, billing continues until destroyed), with the per-test `finally: destroy` as the real backstop.

Azure adopts the same best-effort model, documented identically. Because Azure VMs *can* be force-deleted via the management API (unlike Vultr in its tests), Azure additionally gets the AWS/GCP-style **conftest session-end orphan scanner**: any VM tagged `mngr-pytest-launched` older than a TTL is force-deleted, so a killed pytest run cannot leak a billing VM. This is a strict improvement over Vultr's test backstop.

* **Future improvement (not in v1):** true parity via a system-assigned managed identity + scoped role + a cloud-init systemd timer that deletes the VM via IMDS+ARM after N minutes. Deferred because it adds a role-assignment step to `prepare` and meaningfully more moving parts; the scanner + best-effort shutdown covers the cost-safety need today.

## Expected behavior

* `mngr azure prepare --allowed-ssh-cidr <cidr>` registers resource providers and creates the RG / vnet / subnet / NSG once. Idempotent.
* `mngr create --provider azure` provisions a VM (public IP + NIC + VM), installs Docker via cloud-init, runs the agent container, and returns an online host reachable on `<vm-ip>:container_ssh_port`.
* `mngr stop` / `start` operate on the container (inherited); `mngr destroy` deletes the VM and cascades NIC/IP/disk.
* `mngr ls`, `mngr ssh`, `mngr exec`, host volumes, idle timeout, and container snapshots all work via inherited `VpsDockerProvider` behavior.
* `--azure-region` / `--azure-vm-size` / `--azure-spot` build args control provisioning; remaining build args pass through to `docker build`.

## Changes

* New package `libs/mngr_azure/` with `config.py` (`AzureProviderConfig`), `client.py` (`AzureVpsClient` + `prepare`/`ensure_network` logic), `backend.py` (`AzureProvider`, `AzureProviderBackend`, `register_provider_backend` + `register_cli_commands` hooks), `cli.py` (`mngr azure prepare`), `__init__.py` (pluggy `hookimpl` marker), plus tests (`*_test.py`, `test_ratchets.py`, `test_release_azure.py`), `conftest.py`, `testing.py` (fakes + credential gating + orphan scanner), `README.md`, and `changelog/`.
* `pyproject.toml`: name `imbue-mngr-azure`; deps `imbue-mngr`, `imbue-mngr-vps-docker`, `azure-identity`, `azure-mgmt-compute`, `azure-mgmt-network`, `azure-mgmt-resource`; entry point `[project.entry-points.mngr] azure = "imbue.mngr_azure.backend"`. Add to the uv workspace and the CI/test matrix alongside `mngr_aws`/`mngr_gcp`.
* Reuse, do not reimplement: `VpsDockerProvider` and all of `mngr_vps_docker`; `ssh_host_setup` / `ssh_utils` / `listing_utils` / `deploy_utils` from `imbue.mngr.providers`; `generate_cloud_init_user_data`.
