# mngr Azure Provider [experimental]

Azure provider backend plugin for mngr. Runs agents in Docker containers on Azure Virtual Machines.

> This plugin is **experimental**. The shared `mngr_vps` machinery underneath it is well-tested, but Azure-specific defaults and the role/permission set may change. Treat the security defaults (see "Azure-specific configuration") as a starting point: review the NSG ingress CIDRs, image choice, VM size, and `auto_shutdown_seconds` before pointing this at production resources.

See `mngr_vps` for the base architecture and shared infrastructure.

## Setup

Credentials are resolved via Azure's `DefaultAzureCredential`; they are not configurable in `mngr.toml`. Any of the following works:

- `az login` (developer laptop) — uses your Azure CLI session
- Service principal env vars: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET` (CI)
- A managed identity (when running on an Azure VM / Container App)

The subscription is resolved automatically from your `az` login, so after `az login` (and optionally `az account set --subscription <id>`), `--provider azure` works with no config at all. Resolution order: `providers.azure.subscription_id` > `AZURE_SUBSCRIPTION_ID` env var > the Azure CLI's active subscription.

A `[providers.azure]` block is optional. Configure one only to pin a non-default subscription or override defaults:

```toml
[providers.azure]
backend = "azure"

subscription_id = "00000000-0000-0000-0000-000000000000"  # optional
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

# Inbound CIDRs for tcp/22 and the container SSH port. Default '0.0.0.0/0'
# (a warning is logged; tighten for production). SSH auth is key-only, so an
# open NSG exposes the port but not a usable login. Use [] for no SSH allow rule.
allowed_ssh_cidrs = ["203.0.113.4/32"]

# Optional OS disk sizing
os_disk_size_gb = 30
os_disk_type = "StandardSSD_LRS"
```

## One-time setup: `mngr azure prepare`

Azure nests every resource in a *resource group*, and a fresh subscription has no default vnet. `mngr azure prepare` does the one-time privileged setup: it registers the required resource providers and creates the resource group, vnet, subnet, and NSG (tagged `managed-by=mngr`). It also creates the private **state storage account** that holds offline control-plane state (see "Offline state storage account" below) and creates a least-privilege custom role assigned per-VM so idle agents can deallocate themselves to halt billing; if you lack permission to assign roles, the role step is skipped with a warning and idle self-deallocate is disabled (`mngr stop` still halts billing). After `prepare` succeeds, `mngr create --provider azure` needs only VM/NIC/IP-create permissions, so you can run it with limited credentials.

```bash
mngr azure prepare --allowed-ssh-cidr 203.0.113.4/32
```

With no `--allowed-ssh-cidr`, `prepare` falls back to the config's `allowed_ssh_cidrs` (default `0.0.0.0/0`) and logs a warning prompting you to tighten it. SSH auth is key-only, so an open NSG exposes the port but not a usable login. Setting `allowed_ssh_cidrs = []` creates no SSH allow rule, leaving instances unreachable from outside the vnet. Idempotent — re-running is a no-op when everything already exists.

`prepare` and `cleanup` read their defaults from the `[providers.<name>]` block selected with `--provider` (default `azure`), and CLI flags override that. For example, with a `[providers.azure-west]` block pinning a region, resource group, and CIDRs:

```bash
mngr azure prepare --provider azure-west   # uses that block's region / RG / CIDRs, no flags needed
```

### Teardown: `mngr azure cleanup`

The safe inverse of `prepare`. Deletes the mngr-owned resource group (and its vnet/subnet/NSG), but **refuses** while any mngr-managed VM still exists in the group, and only deletes a group it owns (tagged `managed-by=mngr`). It also deletes the state storage account; since the VM check has passed, any remaining state is orphaned offline state, so cleanup **refuses** to delete a non-empty account unless you pass `--force`. Idempotent.

```bash
mngr azure cleanup
```

### Offline state storage account

`mngr azure prepare` creates a private Azure Storage account and a `mngr-state` Blob container holding mngr's control-plane state (the host record and per-agent records) keyed by host id. The mngr host writes these with your own Azure credentials on create and on stop, so a **deallocated** VM's state is readable without SSH; this is the only offline store. The account name defaults to a derived `mngrst<hash>` (override with `state_storage_account_name`); the container is always `mngr-state`.

Blob data-plane access uses AAD, so reading/writing state needs the **`Storage Blob Data Contributor`** role on the account, which Azure does *not* grant to the account creator automatically. `mngr azure prepare` therefore also grants this role to your own principal (needs `Microsoft.Authorization/roleAssignments/write`, i.e. Owner or User Access Administrator). Each additional operator needs the same grant (re-run `prepare` as them). The account is **required** infrastructure with no fallback: a missing storage permission fails `prepare`, and when the account is absent mngr raises an actionable error pointing at `mngr azure prepare` on both the write and offline-read paths.

### Offline `host_dir` capture

When `is_offline_host_dir_enabled` is on (the default) and the state account exists, `mngr stop` reads the VM's `host_dir` off the box and uploads it to the state container with your own credentials, so `mngr event` / `mngr transcript` / `mngr file` work against a stopped agent. There is no on-box sync daemon or VM identity for this. Capture happens **only at `mngr stop`**: a VM that idle-self-deallocates or crashes is not captured (its offline `host_dir` reflects its last `mngr stop`, or is empty). Set `is_offline_host_dir_enabled = false` to disable the capture (offline host metadata still works via the account).

## Multiple regions

Each provider instance is bound to a single region (and resource group). To work across regions, configure one instance per region and pick the right one at create time:

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
discovery reads its state from the state storage account; without one, offline
reads raise an actionable error pointing at `mngr azure prepare`). `mngr destroy`
deletes the VM, and the NIC, public IP and
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
  cloud-init with the Azure datasource, so the shared `mngr_vps` bootstrap
  works unchanged). Configurable via `image_publisher` / `image_offer` /
  `image_sku` / `image_version`.
- **No snapshot workflow:** the Azure client exposes no managed-disk-snapshot surface, so a hard-killed host cannot be rehydrated.
- **Spot** (`--azure-spot`): `priority=Spot`, `eviction_policy=Delete`,
  `max_price=-1` — evicted only on capacity, and deleted (not stopped) on
  eviction, matching AWS spot's terminate-on-reclaim.
- VMs are tagged `mngr-provider`, `mngr-host-id`, `mngr-created-at`,
  `managed-by=mngr`, and `mngr-host-name`; discovery filters the resource group's
  VM list by `mngr-provider`. Offline discovery identifies deallocated/stopped
  VMs from those cheap index tags and reads their full host record + per-agent
  records from the **state storage account** (see "Offline state storage
  account"). When it does not exist (older `prepare` / no storage permission),
  offline host state is unavailable and the read raises an actionable error
  pointing at `mngr azure prepare` (no per-agent VM-tag mirror is written). Power
  state for a not-SSH-reachable VM is confirmed with a per-VM
  `get_instance_status` call (Azure rejects `expand=instanceView` on a
  resource-group VM list).
- **Stop/start = deallocate/start:** `mngr stop` deallocates the VM
  (`virtual_machines.begin_deallocate`) to halt compute billing; `mngr start`
  re-allocates it (`begin_start`). The static public IP and on-disk SSH host keys
  persist, so resume needs no IP/known_hosts fixup. Mirrors `mngr_aws`/`mngr_gcp`;
  the shared `mngr_vps` base is untouched.
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

## Limitations

- No host snapshot workflow: restore from a fresh `mngr create` rather than rehydrating a killed host.
