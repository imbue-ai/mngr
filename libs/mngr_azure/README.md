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

`mngr stop` stops the container and then **deallocates** the VM, which halts compute billing (an OS-level shutdown alone only powers it off — "Stopped (not deallocated)" — and keeps billing); the OS disk and all state persist, so a paused agent costs only disk storage. `mngr start` re-allocates it. The static public IP and SSH host keys survive the stop, and a deallocated VM still appears in `mngr list` and resolves by name (offline discovery reads its state from the state storage account; without one, offline reads raise an actionable error pointing at `mngr azure prepare`). An idle agent deallocates its own VM the same way (via its managed identity), so billing stops even if the orchestrating `mngr` process is gone. `mngr destroy` deletes the VM; the NIC, public IP, and OS disk are reaped automatically. If a `mngr create` fails after the IP/NIC are provisioned but before the VM (e.g. an Azure `SkuNotAvailable` capacity error), those are cleaned up automatically. A `SkuNotAvailable` error means the chosen VM size has no capacity in the region right now; pick another size with `-b --azure-vm-size=...` or another region.

## Limitations

- No host snapshot workflow: restore from a fresh `mngr create` rather than rehydrating a killed host.
