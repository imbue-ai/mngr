# mngr GCP Provider [experimental]

GCP Compute Engine provider backend plugin for mngr. Runs agents in Docker containers on Google Compute Engine (GCE) VMs.

> This plugin is **experimental** — it has not been exercised in a production setting at the same scale as `mngr_modal` or `mngr_vultr`. The shared `mngr_vps_docker` machinery underneath it is well-tested, but GCP-specific defaults and the IAM permission set may change. Treat the security defaults (see "GCP-specific configuration" below) as a starting point: review the firewall rule, image choice, service account, and `auto_shutdown_minutes` before pointing this at production resources.

See `mngr_vps_docker` for the base architecture and shared infrastructure.

## Setup

Credentials are resolved exclusively via Google [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials) (ADC) — they are deliberately not configurable in `mngr.toml` (matching the Modal and AWS provider convention). Any of the following works:

- `gcloud auth application-default login` (writes `~/.config/gcloud/application_default_credentials.json`)
- `GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account-key.json`
- Attached service account / metadata server (when running on GCE / Cloud Run / GKE)

The only required config field is `project_id` — a plain, non-secret identifier.

```toml
[providers.gcp]
backend = "gcp"

project_id = "my-gcp-project"      # required; no credential material
default_region = "us-west1"
default_zone = "us-west1-a"        # GCE VMs are zonal
default_machine_type = "e2-small"  # machine type (~2 vCPU / 2GB)
# default_image defaults to the global Debian 12 image family; override only if needed:
# default_image = "projects/debian-cloud/global/images/family/debian-12"

# Required (fail-closed): every CIDR allowed inbound on tcp/22 and the
# container SSH port of the auto-created firewall rule. Empty default
# refuses to auto-create the rule -- you must opt in to either a tight
# range like ['203.0.113.4/32'] or the wide-open '0.0.0.0/0'.
allowed_ssh_cidrs = ["203.0.113.4/32"]

# Optional boot-disk sizing
boot_disk_size_gb = 30
boot_disk_type = "pd-balanced"
```

### Multiple zones / regions

Each provider instance is bound to a single zone (the underlying `GcpVpsClient` is built for one project + zone at construction time). To work across zones, configure one provider instance per zone and pick the right one at create time:

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
mngr list
mngr exec my-agent "echo hello"
mngr stop my-agent
mngr start my-agent
mngr destroy my-agent
```

Note: GCE VMs are zonal, so the placement knob is `--gcp-zone=` (e.g. `us-west1-b`), not a region. The chosen zone must belong to the provider's `default_region`.

## GCP-specific configuration

These fields extend the base `VpsDockerProviderConfig` (see `mngr_vps_docker`):

| Field | Default | Description |
|-------|---------|-------------|
| `project_id` | (required) | GCP project ID. A plain identifier, not a credential. |
| `default_region` | `us-west1` | GCE region. Used to validate that the chosen zone belongs to it. |
| `default_zone` | `us-west1-a` | Zone for new instances (GCE VMs are zonal). |
| `default_machine_type` | `e2-small` | GCE machine type. |
| `default_image` | `projects/debian-cloud/global/images/family/debian-12` | Source image. GCE image families are global, so no per-region map is needed. |
| `boot_disk_size_gb` | `30` | Boot disk size in GB. |
| `boot_disk_type` | `pd-balanced` | Boot disk type (`pd-balanced`, `pd-ssd`, `pd-standard`). |
| `network` | `default` | VPC network for the instance NIC and firewall rule. |
| `subnetwork` | `None` | Optional explicit subnetwork (required for custom-mode VPCs). |
| `allowed_ssh_cidrs` | `()` | Tuple of inbound CIDRs for tcp/22 and tcp/`container_ssh_port`. Empty (fail-closed): the auto-firewall path raises unless you list a CIDR or pre-create the rule. |
| `firewall_target_tag` | `mngr-ssh` | Network tag bound to the auto-created firewall rule; every instance is tagged with it. |
| `associate_external_ip` | `True` | Assign an ephemeral external IPv4 to instances. |
| `service_account_email` | `None` | Optional service account attached to launched instances. |
| `service_account_scopes` | `("https://www.googleapis.com/auth/cloud-platform",)` | OAuth scopes for the attached service account (only used when `service_account_email` is set). |
| `auto_shutdown_minutes` | `None` | When set, instances launch with `scheduling.max_run_duration` + `instance_termination_action=DELETE` so the VM self-deletes after N minutes. Leave `None` for normal long-lived behavior; useful for ephemeral test / scratch hosts. |

## Required IAM permissions

The minimal role set needed (roughly `roles/compute.instanceAdmin.v1` plus a service-account user role if attaching one):

```
compute.instances.create, compute.instances.delete, compute.instances.get,
compute.instances.list, compute.instances.setMetadata,
compute.firewalls.create, compute.firewalls.get,
compute.disks.createSnapshot, compute.snapshots.create,
compute.snapshots.delete, compute.snapshots.list,
compute.images.get, compute.zoneOperations.get
```

If `service_account_email` is set, the caller also needs `iam.serviceAccounts.actAs` on that service account.

## Implementation details

- Uses the `google-cloud-compute` SDK (`compute_v1`) for the Compute Engine API.
- Instances are labeled `mngr-provider=<name>`, `mngr-host-id=<id>`, and `mngr-created-at=<iso8601>` for discovery and cleanup-tracking. GCE label values are restricted to `[a-z0-9_-]`, so the provider lowercases values before applying them and applies the same transform to the discovery filter (two provider instances whose names differ only by case would collide — name them distinctly).
- SSH key auth: there is no per-key GCE resource (unlike an EC2 KeyPair). The client holds the per-host public key in memory and writes it into the instance's `ssh-keys` metadata as `debian:<pub>` at create time. The key lives only in per-instance metadata and dies with the VM. OS Login and project-wide SSH keys are disabled per instance (`enable-oslogin=FALSE`, `block-project-ssh-keys=TRUE`).
- cloud-init is delivered via the `user-data` metadata key (Debian cloud images ship cloud-init with the GCE datasource). The shared cloud-init copies the `debian` user's authorized_keys into root's, so mngr's root SSH works unchanged.
- Discovery: `instances.list` filtered by the `mngr-provider` label, then SSH to each VPS to read host records from the state volume.
- Firewall: GCE firewalls are network-scoped and tag-targeted (not per-instance like an EC2 security group). The rule (`mngr-gcp-ssh` by default) is auto-created on first `create_host` and reused across hosts; it is not deleted on `destroy_host` — clean up manually when retiring a provider.
- Auto-delete: when `auto_shutdown_minutes` is set, `scheduling.max_run_duration` + `instance_termination_action=DELETE` makes the VM self-delete from the inside even if the orchestrating process is killed (the GCE-native analog of AWS `InstanceInitiatedShutdownBehavior=terminate`).
- **No automatic snapshot-on-create**: unlike `mngr_modal`, this provider does not snapshot GCE instances automatically. `GcpVpsClient.create_snapshot` / `list_snapshots` / `delete_snapshot` are implemented (boot-disk snapshots); you can call them manually via `mngr snapshot`, or write a plugin that hooks `on_host_created`.

## Release tests and cost

Release tests provision real GCE instances and cost money. They are double-gated:

```bash
MNGR_GCP_RELEASE_TESTS=1 \
  just test libs/mngr_gcp/imbue/mngr_gcp/test_release_gcp.py
```

Three layers of damage control limit leaks from killed-mid-run tests:

1. Every test's `finally` calls `mngr destroy --force`.
2. A `pytest_sessionfinish` hook in `imbue/mngr_gcp/conftest.py` scans for any test-tagged GCE instance older than the TTL at session end, force-deletes leaks, and fails the session.
3. Release tests point `mngr` at a tmp-path settings.toml (via `MNGR_PROJECT_CONFIG_DIR`) that sets `[providers.gcp] auto_shutdown_minutes`. This launches each test instance with `max_run_duration` + `instance_termination_action=DELETE`, so the VM auto-deletes after N minutes even if pytest is killed before any cleanup runs.

Production code enforces this: `GcpProvider._validate_provider_args_for_create` refuses to launch a GCE instance when `PYTEST_CURRENT_TEST` is set unless `auto_shutdown_minutes` is configured (positive). Mirrors the pattern used by `mngr_aws` and `mngr_modal`.

## Future improvements

- `--vps-image=<image>` build-arg for per-host image override.
- Spot / preemptible VMs via `scheduling.provisioning_model`.
- GPU instances with accelerator configs.
- Stable external addressing via reserved static IPs across stops/starts.
