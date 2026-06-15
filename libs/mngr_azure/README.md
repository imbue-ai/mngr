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
`managed-by=mngr`). After it succeeds, `mngr create --provider azure` runs with a
restricted role — it only resolves the existing subnet, no network-write
permission.

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
group it owns (tagged `managed-by=mngr`). Idempotent.

```bash
mngr azure cleanup
```

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

`mngr stop` / `start` operate on the Docker container inside the VM (the VM keeps
running); `mngr destroy` deletes the VM, and the NIC, public IP and OS disk are
reaped automatically via their `delete_option=Delete` (no orphaned resources).

If a `mngr create` fails *after* the public IP + NIC are provisioned but before
the VM (e.g. an Azure `SkuNotAvailable` capacity error), those are cleaned up —
immediately when possible, or otherwise reclaimed at the start of the next
`mngr create` (Azure reserves the NIC for the would-be VM for 180s, so immediate
deletion can be briefly blocked). A `SkuNotAvailable` error means the chosen VM
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
- **Image:** Ubuntu 24.04 LTS by default (runs cloud-init with the Azure
  datasource, so the shared `mngr_vps_docker` bootstrap works unchanged).
  Configurable via `image_publisher` / `image_offer` / `image_sku` / `image_version`.
- **No snapshot workflow:** the Azure client exposes no managed-disk-snapshot surface (the speculative `create_snapshot` / `list_snapshots` / `delete_snapshot` client methods are not part of `VpsClientInterface`). Restore from a fresh `mngr create` instead.
- **Spot** (`--azure-spot`): `priority=Spot`, `eviction_policy=Delete`,
  `max_price=-1` — evicted only on capacity, and deleted (not stopped) on
  eviction, matching AWS spot's terminate-on-reclaim.
- VMs are tagged `mngr-provider`, `mngr-host-id`, `mngr-created-at`, and
  `managed-by=mngr`; discovery filters the resource group's VM list by
  `mngr-provider`.

## Auto-shutdown and cost safety

`auto_shutdown_seconds` schedules cloud-init `shutdown -P +N`. **Caveat (Azure
specific):** an OS-level shutdown leaves the VM "Stopped (not deallocated)",
which still bills for compute — Azure has no native "delete after N minutes" like
AWS/GCP. This matches the Vultr provider's behavior. The real cost backstop for
tests is the session-end orphan scanner in `conftest.py`, which force-deletes any
VM tagged `mngr-pytest-launched` older than the TTL. A future improvement could
add true self-deletion via a managed identity + a cloud-init systemd timer.

## Future improvements

- Managed-identity self-delete after `auto_shutdown_seconds` (true cost parity
  with AWS/GCP, stopping compute billing even if the orchestrator is killed).
- Custom-image baking (skip the per-create cloud-init Docker install).
- Azure Resource Graph for cross-region listing.
