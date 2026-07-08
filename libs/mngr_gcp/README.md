# mngr GCP Provider [experimental]

GCP Compute Engine provider backend plugin for mngr. Runs agents in Docker containers on Google Compute Engine (GCE) VMs.

> This plugin is **experimental**. The shared `mngr_vps` machinery underneath it is well-tested, but GCP-specific defaults and the IAM permission set may change. Treat the security defaults (see "GCP-specific configuration") as a starting point: review the firewall rule, image choice, service account, and `auto_shutdown_seconds` before pointing this at production resources.

See `mngr_vps` for the base architecture and shared infrastructure.

## Setup

Credentials are resolved via Google [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials) (ADC); they are not configurable in `mngr.toml`. Any of the following works:

- `gcloud auth application-default login`
- `GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account-key.json`
- Attached service account / metadata server (when running on GCE / Cloud Run / GKE)

No config fields are strictly required. The one identifier the provider needs is `project_id` — a plain, non-secret value — and when omitted it falls back to the project ADC resolves (the active `gcloud config set project` or the `GOOGLE_CLOUD_PROJECT` env var). Set `project_id` explicitly to pin a specific project; `mngr create` logs which project it inferred when relying on the fallback.

```toml
[providers.gcp]
backend = "gcp"

project_id = "my-gcp-project"      # optional; falls back to the gcloud/ADC default
default_region = "us-west1"
default_zone = "us-west1-a"        # GCE VMs are zonal
default_machine_type = "e2-small"  # machine type (~2 vCPU / 2GB)
# default_source_image defaults to the global Debian 12 family; override only if needed:
# default_source_image = "projects/debian-cloud/global/images/family/debian-12"

# Inbound CIDRs for tcp/22 and the container SSH port on the firewall rule.
# Default '0.0.0.0/0' (a warning is logged; tighten for production).
# Use [] for no ingress (no rule created; instance unreachable).
allowed_ssh_cidrs = ["203.0.113.4/32"]

# Optional boot-disk sizing
boot_disk_size_gb = 30
boot_disk_type = "pd-balanced"
```

### One-time firewall setup: `mngr gcp prepare`

GCE firewall creation is privileged. The rule is created once by an operator, so the regular `mngr create` path needs no firewall-management role. Run once per project + network:

```bash
mngr gcp prepare --project my-gcp-project --allowed-ssh-cidr 203.0.113.4/32
```

This creates a network-scoped, tag-targeted rule (`mngr-gcp-ssh` by default) opening tcp/22 and the container SSH port to the given CIDRs for instances tagged `mngr-ssh`. It is idempotent. With no `--allowed-ssh-cidr`, it falls back to the config's `allowed_ssh_cidrs` (default `0.0.0.0/0`) and logs a warning. After this, `mngr create --provider gcp` resolves the rule read-only and errors with a pointer back to `prepare` if it is missing. Setting `allowed_ssh_cidrs = []` creates no rule, leaving the instance unreachable from outside its VPC.

`prepare` and `cleanup` read their defaults from the `[providers.<name>]` block selected with `--provider` (default `gcp`), and CLI flags override that. For example, with a `[providers.gcp-eu]` block pinning a network and CIDRs:

```bash
mngr gcp prepare --provider gcp-eu   # uses that block's network + CIDRs, no flags needed
```

### Teardown: `mngr gcp cleanup`

`mngr gcp cleanup` deletes the `mngr-gcp-ssh` firewall rule, returning the project to its pre-`prepare` state.

```bash
mngr gcp cleanup --project my-gcp-project
```

It refuses (deletes nothing) if any mngr-managed instance still exists anywhere in the project, so it can never strand a running agent's SSH access. Destroy those first with `mngr destroy <agent>`, then re-run. It is idempotent.

### Multiple zones / regions

Each provider instance is bound to a single zone. To work across zones, configure one provider instance per zone and pick the right one at create time:

```toml
[providers.gcp-west]
backend = "gcp"
project_id = "my-gcp-project"
default_zone = "us-west1-a"
allowed_ssh_cidrs = ["203.0.113.4/32"]

[providers.gcp-central]
backend = "gcp"
project_id = "my-gcp-project"
default_zone = "us-central1-a"
allowed_ssh_cidrs = ["203.0.113.4/32"]
```

```bash
mngr create my-west-agent --provider gcp-west
mngr create my-central-agent --provider gcp-central
```

## Usage

```bash
mngr create my-agent --provider gcp
mngr create my-agent --provider gcp -b --gcp-machine-type=e2-medium -b --gcp-zone=us-west1-b
mngr create my-agent --provider gcp -b --gcp-spot   # run on (preemptible) GCE Spot capacity
mngr create my-agent --provider gcp -b --gcp-image=projects/my-proj/global/images/family/custom   # per-host boot image
mngr list
mngr exec my-agent "echo hello"
mngr stop my-agent
mngr start my-agent
mngr destroy my-agent
```

GCE VMs are zonal, so the placement knob is `--gcp-zone=` (e.g. `us-west1-b`), not a region. When `default_region` is set explicitly, the chosen zone must belong to it; otherwise the region is derived from the zone.

`mngr stop` stops the agent's container and the GCE VM, so a paused agent costs only disk storage — compute billing ends and the boot disk (with all state) persists. `mngr start` resumes it, rebinding to the fresh ephemeral external IP. An idle agent self-stops the same way. A stopped VM still appears in `mngr list` and resolves by name. `mngr destroy` deletes the VM.

## GCP-specific configuration

These fields extend the base `VpsProviderConfig` (see `mngr_vps`):

<!-- BEGIN GENERATED CONFIG TABLE (scripts/make_cli_docs.py) -->
| Field | Default | Description |
|---|---|---|
| `backend` | `gcp` | Provider backend (always 'gcp' for this type) |
| `project_id` | gcloud/ADC default | GCP project ID (a plain identifier, not a credential). When unset, the project is taken from Application Default Credentials (the active 'gcloud config set project' or the GOOGLE_CLOUD_PROJECT env var); set it explicitly to pin a specific project. |
| `default_region` | derived from zone | GCE region (e.g., 'us-west1'). Used only to validate the resolved zone; when unset, derived from the resolved zone. Set it to catch a mismatched default_zone typo. |
| `default_zone` | gcloud `compute/zone`, else `us-west1-a` | Zone for new instances (GCE VMs are zonal). When unset, taken from the active 'gcloud config get compute/zone'. Must lie in default_region when both are set explicitly. |
| `default_machine_type` | `e2-small` | GCE machine type. |
| `default_source_image` | `projects/debian-cloud/global/images/family/debian-12` | GCE VM boot-disk image (distinct from the base default_image, the Docker container image run inside the VM). |
| `boot_disk_size_gb` | `30` | Boot disk size in GB. |
| `boot_disk_type` | `pd-balanced` | Boot disk type. |
| `network` | `default` | VPC network for the instance NIC and firewall rule. |
| `subnetwork` | `None` | Optional explicit subnetwork (required for custom-mode VPCs); None lets GCE pick for auto-mode networks. |
| `firewall_name` | `mngr-gcp-ssh` | Name of the network-scoped firewall rule `mngr gcp prepare` creates to allow SSH ingress. |
| `firewall_target_tag` | `mngr-ssh` | Network tag bound to the firewall rule; every instance is tagged with it. |
| `associate_external_ip` | `true` | Assign an ephemeral external IPv4 to instances. Required for the current mngr-from-developer-laptop SSH access model; for a more secure deployment, set to False and run mngr from a bastion inside the VPC. |
| `service_account_email` | `None` | Optional service account email attached to launched instances. When None, GCE applies its normal default for an unspecified service account. |
| `service_account_scopes` | `("https://www.googleapis.com/auth/cloud-platform",)` | OAuth scopes for the attached service account (only used when service_account_email is set). |
| `state_bucket_name` | `None` | GCS bucket where mngr stores a stopped instance's offline host_dir mirror so it is readable without starting the instance. When None, named 'mngr-state-<project_id>'. The bucket is provisioned by `mngr gcp prepare` and only used when `is_offline_host_dir_enabled` is on; the host + agent records still live in GCE instance metadata regardless. |
| `is_offline_host_dir_enabled` | `true` | When on (default), a stopped instance's host_dir is readable without starting it, so `mngr event` / `mngr transcript` / `mngr file` work against it. `mngr gcp prepare` sets up the GCS bucket it needs. Set False to turn it off. |
| `allowed_ssh_cidrs` | `("0.0.0.0/0",)` | Inbound CIDR blocks allowed on tcp/22 and the container SSH port in the security group / NSG / firewall rule the provider's `prepare` command creates. Default ('0.0.0.0/0',) allows any IP; use e.g. ('203.0.113.4/32',) to restrict to your own, or () for no ingress (no rule is created, so the instance is unreachable from outside its network). A warning is logged when the effective range is 0.0.0.0/0 or empty. Replaced, not merged, across config layers. |
| `auto_shutdown_seconds` | `None` | When set, the host OS halts itself after about this many seconds (rounded up to whole minutes, the granularity `shutdown` accepts) -- a hard max-lifetime cap, distinct from the activity-based default_idle_timeout. Whether the halt stops, terminates, or deletes the instance is provider-specific (see the provider's README). |
<!-- END GENERATED CONFIG TABLE -->

## Required IAM permissions

`mngr gcp prepare` (operator, once per project):

```
compute.firewalls.get, compute.firewalls.create,
storage.buckets.create, storage.buckets.get
```

`mngr gcp cleanup` (operator, teardown), additionally:

```
compute.instances.list, compute.firewalls.delete,
storage.buckets.get, storage.buckets.delete,
storage.objects.list, storage.objects.delete
```

`mngr create --provider gcp` (developer, per host):

```
compute.instances.create, compute.instances.delete, compute.instances.get,
compute.instances.list, compute.instances.stop, compute.instances.start,
compute.firewalls.get, compute.zoneOperations.get
```

When `is_offline_host_dir_enabled` is on (default), the runtime stop/start
path additionally needs `storage.objects.create`, `storage.objects.get`,
`storage.objects.list`, and `storage.objects.delete` on the state bucket so
`mngr stop` can write the captured `host_dir` and offline reads can serve
files from it.

If `service_account_email` is set, the caller also needs `iam.serviceAccounts.actAs` on that service account.

## Limitations

- No host snapshot workflow: restore from a fresh `mngr create` rather than rehydrating a killed host.
- A stopped VM releases its ephemeral external IP and gets a fresh one on resume (mngr rebinds known_hosts automatically); there is no stable static-IP option yet.
- Spot VMs (`--gcp-spot`) can be preempted by GCE at any time with ~30s notice and are deleted (not stopped) on preemption. Use for ephemeral / experimental agents.
