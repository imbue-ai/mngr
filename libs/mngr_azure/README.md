# mngr Azure Provider [experimental]

Azure provider backend plugin for mngr. Runs agents in Docker containers on Azure Virtual Machines.

> This plugin is **experimental** — it has not been exercised in a production setting at the same scale as `mngr_modal` or `mngr_vultr`. The shared `mngr_vps_docker` machinery underneath it is well-tested, but Azure-specific defaults and the role/permission set may change. Treat the security defaults (see "Azure-specific configuration" below) as a starting point: review the NSG ingress CIDRs, image choice, VM size, and `auto_shutdown_seconds` before pointing this at production resources.

See `mngr_vps_docker` for the base architecture and shared infrastructure.

## Setup

Credentials are resolved exclusively via Azure's `DefaultAzureCredential` — they
are deliberately not configurable in `mngr.toml` (matching the Modal / AWS / GCP
provider convention). Any of the following works:

- `az login` (developer laptop) — the credential transparently uses your Azure CLI session
- Service principal env vars: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET` (CI)
- A managed identity (when running on an Azure VM / Container App)

The subscription is resolved automatically from your `az` login — after `az login`
(and optionally `az account set --subscription <id>`), `--provider azure` works
with **no config at all**, the same way the GCP provider uses your active gcloud
project. Resolution order: `providers.azure.subscription_id` in config >
`AZURE_SUBSCRIPTION_ID` env var > the Azure CLI's active subscription.

So a `[providers.azure]` block is entirely optional. Configure one only to pin a
non-default subscription or override defaults:

```toml
[providers.azure]
backend = "azure"

subscription_id = "00000000-0000-0000-0000-000000000000"  # optional; defaults to your `az` active subscription
default_region = "westus"
default_vm_size = "Standard_B2s"            # 2 vCPU / 4GB; B-series is quota-friendly on new subs

# One-off infrastructure names (created by `mngr azure prepare`)
resource_group = "mngr"
vnet_name = "mngr-vnet"
subnet_name = "mngr-subnet"
nsg_name = "mngr-nsg"

# Optional override for the offline-state storage account (default: a derived
# 'mngrst<hash>' from subscription + resource group). 3-24 lowercase alphanumeric.
state_storage_account_name = "mngrstmyteam"

# Inbound CIDRs for tcp/22 and the container SSH port on the NSG. Defaults to
# the wide-open '0.0.0.0/0' (fail-open, matching the AWS / GCP providers; a
# warning is logged -- tighten for production). SSH auth is key-only (passwords
# disabled), so 0.0.0.0/0 exposes the port but not a usable login. Use a tight
# range like ['203.0.113.4/32'], or [] for no SSH allow rule (the NSG default
# deny then leaves instances unreachable from outside the vnet).
allowed_ssh_cidrs = ["203.0.113.4/32"]

# Optional OS disk sizing
os_disk_size_gb = 30
os_disk_type = "StandardSSD_LRS"
```

## One-time setup: `mngr azure prepare`

Azure nests every resource in a *resource group*, and a fresh subscription has no
default vnet. `mngr azure prepare` does the one-time privileged setup: it
registers the `Microsoft.Compute` / `Microsoft.Network` / `Microsoft.Storage`
resource providers and creates the resource group, vnet, subnet, and NSG (tagged
`managed-by=mngr`). It also creates a private **state storage account + Blob
container** (the "state bucket"; see "Offline state storage account" below) that
holds mngr's control-plane state. After it succeeds, `mngr create --provider
azure` needs only VM/NIC/IP-create permissions, not the network-management
permissions that build the vnet/subnet/NSG — it just resolves the existing subnet,
so you can run it with limited credentials.

```bash
mngr azure prepare --allowed-ssh-cidr 203.0.113.4/32
```

Like AWS and GCP, `prepare` is fail-open: with no `--allowed-ssh-cidr` it falls
back to the provider config's `allowed_ssh_cidrs` (default `0.0.0.0/0`, open to
the internet) and logs a warning prompting you to tighten it. SSH auth is
key-only (passwords disabled), so an open NSG exposes the port but not a usable
login. Setting `allowed_ssh_cidrs = []` opts out entirely: the NSG is created
with no SSH allow rule, so its default-deny leaves instances unreachable from
outside the vnet.

Idempotent — re-running is a no-op when everything already exists.

`prepare` and `cleanup` read their defaults from your `[providers.<name>]`
settings.toml block, selected with `--provider` (default `azure`), so the
resource group / vnet / subnet / NSG land with the same names the runtime `mngr
create --provider <name>` path will resolve. CLI flags override the resolved
config, which in turn overrides class defaults. For example, with a
`[providers.azure-west]` block pinning `default_region = "westus"`,
`resource_group = "mngr-westus"`, and `allowed_ssh_cidrs = ["203.0.113.4/32"]`:

```bash
mngr azure prepare --provider azure-west   # uses that block's region / RG / CIDRs, no flags needed
```

### Teardown: `mngr azure cleanup`

The safe inverse of `prepare`. Deletes the mngr-owned resource group (cascading
its vnet/subnet/NSG), but **refuses** while any mngr-managed VM still exists in
the group (destroy those first with `mngr destroy <agent>`), and only deletes a
group it owns (tagged `managed-by=mngr`). It also deletes the state storage
account; because the VM check above has already passed, any remaining state is
**orphaned** offline state (from hosts no longer running as VMs), so it
**refuses** to delete a non-empty account rather than silently dropping records
you may still want -- pass `--purge-state` to delete the account and its
remaining state. Idempotent.

```bash
mngr azure cleanup
```

### Offline state storage account

`mngr azure prepare` creates a private Azure Storage account and a `mngr-state`
Blob container holding mngr's control-plane state — the full host record and the
per-agent records — keyed by host id. The mngr host machine writes these with
**your own Azure credentials** (no keys stored on the box) whenever it writes
state (on create and on stop), so a **deallocated** VM's full state is readable
without SSH. This replaces the VM-tag mirror, which (a) silently dropped
per-agent `labels` larger than the 256-char Azure tag value limit and (b) could
only reconstruct a lossy subset of the host record while the VM was stopped.

The storage-account name defaults to a deterministic `mngrst<hash>` derived from
your subscription + resource group (storage-account names are globally unique,
3–24 lowercase alphanumeric); override it with `state_storage_account_name` in
`[providers.azure]`. The container is always `mngr-state`.

Blob data-plane access uses AAD (the same `DefaultAzureCredential` the provider
already uses), so to read/write host state you need the **`Storage Blob Data
Contributor`** role on the state storage account, in addition to the
storage-account create/delete permission `prepare`/`cleanup` use
(`Microsoft.Storage/storageAccounts/write` + `delete`). A missing storage
permission during `prepare` is **not fatal**: it degrades to a warning, the
network prepare still succeeds, and offline state falls back to the VM-tag
mirror. Existing deployments that never re-ran the new `prepare` keep working on
that fallback.

### Offline `host_dir` (Lima-style, on by default)

A deallocated VM's `host_dir` is also readable without SSH, so `mngr event` /
`mngr transcript` work against a stopped agent (matching Lima's
`is_host_data_volume_exposed`). When `is_host_dir_synced_to_bucket` is on (the
default) and the state bucket exists, an on-box systemd oneshot + timer daemon
syncs the VM's `host_dir` to the state container's `hosts/<host_id>/host_dir/`
prefix every 60s, and once more on `mngr stop` just before the VM deallocates
(so the offline copy is current). The sync runs `azcopy sync` authenticating as
the VM's managed identity via MSI (`--auth-mode login`; no storage keys on the
box), excluding large transient caches (`*.tmp`, `__pycache__`, `node_modules`).
Set `is_host_dir_synced_to_bucket = false` in `[providers.azure]` to disable it
(offline host metadata still works via the bucket).

The instance-push needs a cloud identity, which `prepare` provisions:

```bash
mngr azure prepare --use-offline-host-dir yes   # fail if the identity can't be created
```

`--use-offline-host-dir {yes,auto,no}` (default `auto`) provisions a
**user-assigned managed identity** plus a **`Storage Blob Data Contributor`**
role assignment scoped to **just the state storage account** (least privilege --
never the resource group or subscription). `auto` warns and continues on a
permission failure (the network + bucket prepare still succeed; only offline
`host_dir` is unavailable); `require` fails the command; `skip` does not attempt
it. `mngr azure cleanup` deletes the identity. `mngr create` attaches it to the
VM when the feature is on and the identity exists (an operator-supplied identity
takes precedence).

Provisioning the identity needs `Microsoft.ManagedIdentity/userAssignedIdentities/write`
+ `Microsoft.Authorization/roleAssignments/write` (Owner or User Access
Administrator). When offline `host_dir` is requested for a host whose VM has no
attached managed identity, mngr logs a non-fatal diagnostic pointing at
`mngr azure prepare --use-offline-host-dir yes` rather than returning an empty
volume.

### Quota note

New pay-as-you-go subscriptions start with **low or zero vCPU quota** per region
and per VM family. The default `Standard_B2s` (B-series) is the family most
likely to have nonzero quota; if `mngr create` fails with a quota error, request
an increase in the Azure portal (Subscriptions → Usage + quotas) or pick a region
with available quota (`az vm list-usage --location westus -o table`).

## Multiple regions

Each provider instance is bound to a single region (and resource group). To work
across regions, configure one instance per region and pick the right one at
create time:

```toml
[providers.azure-west]
backend = "azure"
subscription_id = "..."
default_region = "westus"
resource_group = "mngr-westus"
allowed_ssh_cidrs = ["203.0.113.4/32"]

[providers.azure-east]
backend = "azure"
subscription_id = "..."
default_region = "eastus"
resource_group = "mngr-eastus"
allowed_ssh_cidrs = ["203.0.113.4/32"]
```

```bash
mngr azure prepare --provider azure-west   # reads region / RG / CIDRs from [providers.azure-west]
mngr create my-west-agent --provider azure-west
```

## Usage

```bash
mngr create my-agent --provider azure
mngr create my-agent --provider azure -b --azure-vm-size=Standard_D2s_v5 -b --azure-region=eastus
mngr create my-agent --provider azure -b --azure-spot                       # run on Azure Spot capacity
mngr list
mngr exec my-agent "echo hello"
mngr stop my-agent
mngr start my-agent
mngr destroy my-agent
```

`mngr stop` stops the container and then **deallocates** the VM, which actually
halts compute billing (an OS-level shutdown would only power it off — "Stopped
(not deallocated)" — and keep billing); the OS disk and all state persist, so a
paused agent costs only disk storage. `mngr start` re-allocates it. The public IP
is static, so it and the SSH host keys survive the stop (no known_hosts rebind on
resume). A deallocated VM still shows in `mngr list` and resolves by name (offline
discovery reads its state from the state storage account, or the VM tag mirror as
a fallback). `mngr destroy` deletes the VM, and the NIC, public IP and
OS disk are reaped automatically via their `delete_option=Delete` (no orphaned
resources).

If a `mngr create` fails *after* the public IP + NIC are provisioned but before
the VM (e.g. an Azure `SkuNotAvailable` capacity error), those are cleaned up —
immediately when possible, or otherwise reclaimed at GC time by `mngr gc` (which
also runs after every `mngr destroy`) (Azure reserves the NIC for the would-be VM
for 180s, so immediate deletion can be briefly blocked). A `SkuNotAvailable` error means the chosen VM
size has no capacity in the region right now; pick another size with
`-b --azure-vm-size=...` or another region.

## How it works

- **Per-host create:** a Standard-SKU static public IP + a NIC bound to the
  prepared subnet + a VM. The OS disk, NIC, and public IP are all created with
  `delete_option=Delete`, so deleting the VM cascades all four — `destroy` is a
  single VM delete.
- **SSH keys** are injected inline at VM create (`os_profile.linux_configuration.ssh`);
  Azure has no per-key resource. Cloud-init also forwards the key into root's
  `authorized_keys`, so mngr's root SSH works.
- **Image:** Debian 12 by default (matching the other mngr providers; runs
  cloud-init with the Azure datasource, so the shared `mngr_vps_docker` bootstrap
  works unchanged). Configurable via `image_publisher` / `image_offer` /
  `image_sku` / `image_version`.
- **No snapshot workflow:** the Azure client exposes no managed-disk-snapshot surface (the speculative `create_snapshot` / `list_snapshots` / `delete_snapshot` client methods are not part of `VpsClientInterface`). Restore from a fresh `mngr create` instead.
- **Spot** (`--azure-spot`): `priority=Spot`, `eviction_policy=Delete`,
  `max_price=-1` — evicted only on capacity, and deleted (not stopped) on
  eviction, matching AWS spot's terminate-on-reclaim.
- VMs are tagged `mngr-provider`, `mngr-host-id`, `mngr-created-at`,
  `managed-by=mngr`, and `mngr-host-name`; discovery filters the resource group's
  VM list by `mngr-provider`. Offline discovery reconstructs deallocated/stopped
  VMs from those index tags and reads their full host record + per-agent records
  from the **state storage account** (see "Offline state storage account") when
  it exists. When it does not (older `prepare` / no storage permission), it falls
  back to the per-agent VM-tag mirror (`mngr-agent-<id>-<field>`), which is
  capped at the 256-char Azure tag value limit. Either way, power state for a
  not-SSH-reachable VM is confirmed with a per-VM `get_instance_status` call
  (Azure rejects `expand=instanceView` on a resource-group VM list).
- **Stop/start = deallocate/start:** `mngr stop` deallocates the VM
  (`virtual_machines.begin_deallocate`) to halt compute billing; `mngr start`
  re-allocates it (`begin_start`). The static public IP and on-disk SSH host keys
  persist, so resume needs no IP/known_hosts fixup. Mirrors `mngr_aws`/`mngr_gcp`;
  the shared `mngr_vps_docker` base is untouched.
- **Idle self-deallocate (managed identity):** each VM is created with a
  system-assigned managed identity. The in-container idle watcher touches a
  sentinel; a host-side systemd path unit runs a script that uses the VM's IMDS
  token to call the ARM `deallocate` API on itself (the only in-guest way to halt
  Azure compute billing — an OS shutdown does not). `mngr azure prepare` creates a
  least-privilege custom role (`mngr-self-deallocate`, just
  `Microsoft.Compute/virtualMachines/deallocate/action` + `read`), and each VM
  gets a role assignment scoped to itself. **Graceful fallback:** if the operator
  lacks `Microsoft.Authorization/roleAssignments`/`roleDefinitions` write (Owner /
  User Access Administrator), the role steps are skipped with a clear warning and
  idle self-deallocate is disabled; on a refused deallocate the in-VM script just
  logs and exits (it does not poweroff — an Azure OS shutdown would only strand the
  VM unreachable while it keeps billing). `mngr stop`/`start` still deallocate
  normally, and remain the only way to halt billing on such a host.

## Auto-shutdown and cost safety

Two independent mechanisms:

- **Idle self-deallocate** (the primary, cost-parity path): an idle agent
  deallocates its own VM via its managed identity (see "How it works"), genuinely
  halting compute billing — even if the orchestrating `mngr` process is gone.
  Requires the operator to have granted the role assignment (otherwise it is
  disabled and only `mngr stop` halts billing — an in-VM OS shutdown does not).
- **`auto_shutdown_seconds`** schedules cloud-init `shutdown -P +N` as a coarse
  time cap. **Caveat (Azure specific):** this OS-level shutdown alone leaves the VM
  "Stopped (not deallocated)", which still bills for compute. For test isolation
  the real backstop is the session-end orphan scanner in `conftest.py`, which
  force-deletes any VM tagged `mngr-pytest-launched` older than the TTL.

## Future improvements

- Custom-image baking (skip the per-create cloud-init Docker install).
- Azure Resource Graph for cross-region listing.
