# mngr GCP Provider [experimental]

GCP Compute Engine provider backend plugin for mngr. Runs agents in Docker containers on Google Compute Engine (GCE) VMs.

> This plugin is **experimental** — it has not been exercised in a production setting at the same scale as `mngr_modal` or `mngr_vultr`. The shared `mngr_vps_docker` machinery underneath it is well-tested, but GCP-specific defaults and the IAM permission set may change. Treat the security defaults (see "GCP-specific configuration" below) as a starting point: review the firewall rule, image choice, service account, and `auto_shutdown_seconds` before pointing this at production resources.

See `mngr_vps_docker` for the base architecture and shared infrastructure.

## Setup

Credentials are resolved exclusively via Google [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials) (ADC) — they are deliberately not configurable in `mngr.toml` (matching the Modal and AWS provider convention). Any of the following works:

- `gcloud auth application-default login` (writes `~/.config/gcloud/application_default_credentials.json`)
- `GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account-key.json`
- Attached service account / metadata server (when running on GCE / Cloud Run / GKE)

No config fields are strictly required. The one identifier the provider needs is `project_id` — a plain, non-secret value — and when it is omitted it falls back to the project that ADC resolves from the environment: the active `gcloud config set project` or the `GOOGLE_CLOUD_PROJECT` env var. Set `project_id` explicitly to pin a specific project (recommended when you have access to several); `mngr create` logs which project it inferred when relying on the fallback.

### One-time firewall setup

GCE firewall creation is privileged (`compute.firewalls.create`). Like AWS's
`mngr aws prepare`, the firewall rule is created once by an operator, so the
regular `mngr create` path only needs instance create/get/list permissions (no
firewall-management role). Run once per project + network:

```bash
mngr gcp prepare --project my-gcp-project --allowed-ssh-cidr 203.0.113.4/32
```

This creates a network-scoped, tag-targeted rule (`mngr-gcp-ssh` by default)
opening tcp/22 and the container SSH port to the given CIDRs for instances
tagged `mngr-ssh`. It is idempotent (a no-op when the rule already exists). Like
AWS, it is fail-open: with no `--allowed-ssh-cidr`, it falls back to the provider
config's `allowed_ssh_cidrs` (default `0.0.0.0/0`, open to the internet) and logs
a warning prompting you to tighten it. After this, `mngr create --provider gcp`
resolves the rule read-only and errors with a pointer back to `prepare` if it is
missing. Setting `allowed_ssh_cidrs = []` opts out entirely: no rule is created
and the instance is unreachable from outside its VPC.

`prepare` and `cleanup` read their defaults from your `[providers.<name>]`
settings.toml block, selected with `--provider` (default `gcp`), so the rule
lands in the same project / network / zone the runtime `mngr create --provider
<name>` path will use. CLI flags override the resolved config, which in turn
overrides class defaults. For example, with a `[providers.gcp-eu]` block pinning
`network = "custom-net"` and `allowed_ssh_cidrs = ["203.0.113.4/32"]`:

```bash
mngr gcp prepare --provider gcp-eu   # uses that block's network + CIDRs, no flags needed
```

### Teardown: `mngr gcp cleanup`

`mngr gcp cleanup` is the inverse of `prepare`: it deletes the `mngr-gcp-ssh`
firewall rule so the project returns to its pre-`prepare` state (useful when
retiring a provider or testing the first-run experience).

```bash
mngr gcp cleanup --project my-gcp-project
```

It is **safe by design**: it refuses (non-zero exit, deletes nothing) if any
mngr-managed instance still exists anywhere in the project (checked across all
zones, because the firewall rule is network-global), so it can never strand a
running agent's SSH access. Destroy those first with `mngr destroy <agent>`,
then re-run. It is idempotent -- a no-op when the rule is already gone. It needs
`compute.instances.list` (aggregated), `compute.firewalls.get`, and
`compute.firewalls.delete`. It does **not** delete per-host SSH keys: those live
only in per-instance metadata and die with the VM, not with `prepare`.

```toml
[providers.gcp]
backend = "gcp"

project_id = "my-gcp-project"      # optional; falls back to the gcloud/ADC default. no credential material
default_region = "us-west1"
default_zone = "us-west1-a"        # GCE VMs are zonal
default_machine_type = "e2-small"  # machine type (~2 vCPU / 2GB)
# default_source_image (the GCE VM image) defaults to the global Debian 12 family; override only if needed:
# default_source_image = "projects/debian-cloud/global/images/family/debian-12"

# Every CIDR allowed inbound on tcp/22 and the container SSH port of the
# firewall rule `mngr gcp prepare` creates. Defaults to the wide-open '0.0.0.0/0' (fail-open,
# matching the AWS provider; a warning is logged -- tighten for production).
# Use a tight range like ['203.0.113.4/32'], or [] for no ingress at all (no
# rule is created and the instance is unreachable from outside its VPC).
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
mngr create my-agent --provider gcp -b --gcp-spot   # run on (preemptible) GCE Spot capacity
mngr create my-agent --provider gcp -b --gcp-image=projects/my-proj/global/images/family/custom   # per-host boot image
mngr list
mngr exec my-agent "echo hello"
mngr stop my-agent
mngr start my-agent
mngr destroy my-agent
```

Note: GCE VMs are zonal, so the placement knob is `--gcp-zone=` (e.g. `us-west1-b`), not a region. When `default_region` is set explicitly, the chosen zone must belong to it; otherwise the region is derived from the zone. The boot-disk image can be overridden per host with `--gcp-image=` (otherwise the config's `default_source_image` is used).

`mngr stop` stops the agent's container **and the GCE VM** (`instances.stop`), so a paused agent costs only disk storage -- compute billing ends and the boot disk (with all state) persists. `mngr start` resumes it (`instances.start`), rebinding to the fresh ephemeral external IP. An idle agent self-stops the same way: a guest poweroff lands a GCE VM in `TERMINATED` (stopped, disk preserved, no compute billing), so the in-container idle watcher just powers the box off -- no API call or extra IAM. A stopped VM still shows in `mngr list` and resolves by name (offline discovery via instance metadata). See "How it works".

## GCP-specific configuration

These fields extend the base `VpsDockerProviderConfig` (see `mngr_vps_docker`):

| Field | Default | Description |
|-------|---------|-------------|
| `project_id` | gcloud/ADC default | GCP project ID. A plain identifier, not a credential. Falls back to the active `gcloud config set project` / `GOOGLE_CLOUD_PROJECT` when empty. |
| `default_region` | derived from zone | GCE region. Used only to validate the resolved zone. When unset it is derived from the zone; set it to assert a region and catch a mismatched `default_zone` typo. |
| `default_zone` | gcloud `compute/zone`, else `us-west1-a` | Zone for new instances (GCE VMs are zonal). When unset, taken from the active `gcloud config get compute/zone` if the gcloud CLI is available, otherwise `us-west1-a`. |
| `default_machine_type` | `e2-small` | GCE machine type. |
| `default_source_image` | `projects/debian-cloud/global/images/family/debian-12` | GCE VM boot-disk image (distinct from the base `default_image`, which is the Docker *container* image run inside the VM). GCE image families are global, so no per-region map is needed. Debian 12 matches the rest of the mngr fleet; GCP bootstraps via the GCE `startup-script` (run by the google-guest-agent on every image), so it does not require the image to ship cloud-init. |
| `boot_disk_size_gb` | `30` | Boot disk size in GB. |
| `boot_disk_type` | `pd-balanced` | Boot disk type (`pd-balanced`, `pd-ssd`, `pd-standard`). |
| `network` | `default` | VPC network for the instance NIC and firewall rule. |
| `subnetwork` | `None` | Optional explicit subnetwork (required for custom-mode VPCs). |
| `allowed_ssh_cidrs` | `("0.0.0.0/0",)` | Tuple of inbound CIDRs for tcp/22 and tcp/`container_ssh_port`. Defaults open to the internet (fail-open, like AWS); warned at prepare/create time. Set `()` for no ingress (no rule created; instance unreachable). |
| `firewall_target_tag` | `mngr-ssh` | Network tag bound to the firewall rule `mngr gcp prepare` creates; every instance is tagged with it. |
| `associate_external_ip` | `True` | Assign an ephemeral external IPv4 to instances. |
| `service_account_email` | `None` | Optional service account attached to launched instances. When `None` the field is omitted from the create request, so GCE applies its normal default for an unspecified service account. |
| `service_account_scopes` | `("https://www.googleapis.com/auth/cloud-platform",)` | OAuth scopes for the attached service account (only used when `service_account_email` is set). |
| `auto_shutdown_seconds` | `None` | When set, instances launch with `scheduling.max_run_duration` + `instance_termination_action=DELETE` so the VM self-deletes after N seconds. Leave `None` for normal long-lived behavior; useful for ephemeral test / scratch hosts. |

## Required IAM permissions

Split into the one-time privileged `prepare` step and the regular `create` path,
so developers can run with a reduced role:

`mngr gcp prepare` (operator, once per project):

```
compute.firewalls.get, compute.firewalls.create
```

`mngr gcp cleanup` (operator, teardown; in addition to `compute.firewalls.get`):

```
compute.instances.list, compute.firewalls.delete
```

`mngr create --provider gcp` (developer, per host):

```
compute.instances.create, compute.instances.delete, compute.instances.get,
compute.instances.list, compute.firewalls.get,
compute.zoneOperations.get
```

If `service_account_email` is set, the caller also needs `iam.serviceAccounts.actAs` on that service account.

## Implementation details

- Uses the `google-cloud-compute` SDK (`compute_v1`) for the Compute Engine API.
- Instances are labeled `mngr-provider=<name>`, `mngr-host-id=<id>`, and `mngr-created-at=<...>` for discovery and cleanup-tracking. GCE label values are restricted to `[a-z0-9_-]`, so the provider lowercases values before applying them and applies the same transform to the discovery filter (two provider instances whose names differ only by case would collide — name them distinctly). The human host name is mirrored into instance *metadata* (key `mngr-host-name`) so a stopped VM still resolves by name; the full host record and per-agent records (which don't fit the label charset) live in metadata too — the complete `VpsDockerHostRecord` JSON under `mngr-host-state` and one full agent-record JSON per agent under `mngr-agent-<id>`. This metadata store is the GCP analog of the AWS/Azure object-storage state bucket, behind the same `HostStateStore` interface; GCE metadata is large and permissive enough (256 KB per value, 512 KB per instance) to hold these records, so GCP needs no separate bucket.
- SSH key auth: there is no per-key GCE resource (unlike an EC2 KeyPair). The client holds the per-host public key in memory and writes it into the instance's `ssh-keys` metadata as `ubuntu:<pub>` at create time. The key lives only in per-instance metadata and dies with the VM. OS Login and project-wide SSH keys are disabled per instance (`enable-oslogin=FALSE`, `block-project-ssh-keys=TRUE`).
- The first-boot bootstrap is delivered via the GCE `startup-script` metadata key, run by the google-guest-agent on every image (including the default Debian 12, which ships no cloud-init -- so the `user-data` flow the other backends use would be ignored here). It renders the same shared host-setup steps and writes the provider key into root's authorized_keys. It installs the SSH host key and restarts sshd as its first action; since that happens after sshd booted with a random key, the provisioner polls the live host key until it matches before strict-checking (`_wait_for_expected_host_key`).
- Discovery: `instances.list` filtered by the `mngr-provider` label, then SSH to each VPS to read host records from the state volume.
- Firewall: GCE firewalls are network-scoped and tag-targeted (not per-instance like an EC2 security group). The rule (`mngr-gcp-ssh` by default) is created once by `mngr gcp prepare` (privileged) and reused across hosts; the hot `create_host` path only resolves it read-only and errors with a `prepare` pointer if it's missing. The rule is not deleted on `destroy_host`; run `mngr gcp cleanup` to delete it when retiring a provider (it refuses while any mngr-managed instance still exists in the project).
- Auto-delete: when `auto_shutdown_seconds` is set, `scheduling.max_run_duration` + `instance_termination_action=DELETE` makes the VM self-delete from the inside even if the orchestrating process is killed (the GCE-native analog of AWS `InstanceInitiatedShutdownBehavior=terminate`).
- Spot capacity: the per-host `--gcp-spot` build arg launches the VM with `scheduling.provisioning_model=SPOT` (and `instance_termination_action=DELETE`, so a preempted Spot VM is deleted rather than left stopped -- mngr has no VM-level resume yet). It composes with `auto_shutdown_seconds` (both land on one `Scheduling`). GCE can preempt Spot VMs at any time with ~30s notice, so it is opt-in only: good for ephemeral / experimental agents, risky for long-lived ones. Mirrors the AWS `--aws-spot` flag.
- Per-host image override: the `--gcp-image=<image>` build arg boots a single VM from the given GCE source image (a full image or family URL) instead of the config's `default_source_image`. Unlike the other VPS providers, where image selection is config-only, GCP exposes this per-host knob; an unset flag falls back to `default_source_image`. Note the cloud-init caveat above -- an image that does not run cloud-init with the GCE datasource will silently ignore the `user-data` bootstrap.
- **No snapshot workflow**: unlike `mngr_modal`, where every sandbox is snapshotted at create time so a hard-killed host can be rehydrated, this provider has no host snapshot workflow today. The GCP client exposes no disk-snapshot surface (the speculative `create_snapshot` / `list_snapshots` / `delete_snapshot` client methods had no consumers and are not part of `VpsClientInterface`). Restore from a fresh `mngr create` instead.
- **VM-level stop/start (idle-pause + resume)**: `mngr stop` stops the agent's container and then the GCE VM (`instances.stop`), so a paused agent costs only disk storage and the boot disk preserves all state; `mngr start` finds the stopped instance by its `mngr-host-id` label (it has no external IP while stopped), starts it (`instances.start`), and rebinds known_hosts to the fresh ephemeral external IP before resuming the container. Needs `compute.instances.stop` / `compute.instances.start`. Mirrors `mngr_aws`; the shared `mngr_vps_docker` base is untouched.
- **Idle self-stop (no API call)**: the in-container idle watcher touches a sentinel on the shared volume; a host-side systemd `.path` unit fires a oneshot that runs `shutdown -P now`. On GCE a guest poweroff lands the VM in `TERMINATED` (stopped, disk preserved, no compute billing) by default -- there is no GCE analog to AWS's `InstanceInitiatedShutdownBehavior` and none is needed, so idle self-stop needs no extra IAM. (Orthogonal to the `auto_shutdown_seconds` time-cap, which *deletes* the VM.)
- **Offline discovery**: the full host record (`mngr-host-state`) and per-agent records (`mngr-agent-<id>`) are mirrored into instance *metadata* (GCE labels are too restricted -- lowercased `[a-z0-9_-]`, 63 chars -- to hold them), so a stopped VM still appears in `mngr list` and resolves by name without SSH. `discover_hosts_and_agents` identifies stopped (`TERMINATED`/`STOPPING`) hosts from the cheap `mngr-host-id` label and reads their full record + agents back from the metadata store (the GCP arm of the shared `HostStateStore`, uniform with the AWS/Azure buckets).

## Release tests and cost

Release tests provision real GCE instances and cost money. They are double-gated:

```bash
MNGR_GCP_RELEASE_TESTS=1 PYTEST_MAX_DURATION_SECONDS=1800 \
  uv run pytest --no-cov -n 0 -m release \
  libs/mngr_gcp/imbue/mngr_gcp/test_release_gcp.py
```

The `PYTEST_MAX_DURATION_SECONDS=1800` matters: the two lifecycle tests each boot
a real GCE VM serially, exceeding the default ~600s budget. That env var sets the
pytest global-lock deadline; once it passes, a concurrent pytest run kills this
one, SIGTERMing the suite mid-test (and potentially leaking a VM). Run `uv run
pytest` directly rather than `just test`, whose recipe hardcodes 600s. (Other long
real-resource release tests use the same pattern.)

Three layers of damage control limit leaks from killed-mid-run tests:

1. Every test's `finally` calls `mngr destroy --force`.
2. A `pytest_sessionfinish` hook in `imbue/mngr_gcp/conftest.py` scans for any test-tagged GCE instance older than the TTL at session end, force-deletes leaks, and fails the session.
3. Release tests point `mngr` at a tmp-path settings.toml (via `MNGR_PROJECT_CONFIG_DIR`) that sets `[providers.gcp] auto_shutdown_seconds`. This launches each test instance with `max_run_duration` + `instance_termination_action=DELETE`, so the VM auto-deletes after N seconds even if pytest is killed before any cleanup runs.

Production code enforces this: `GcpProvider._validate_provider_args_for_create` refuses to launch a GCE instance when `PYTEST_CURRENT_TEST` is set unless `auto_shutdown_seconds` is configured (positive). Mirrors the pattern used by `mngr_aws` and `mngr_modal`.

## Future improvements

- GPU instances with accelerator configs.
- Stable external addressing via reserved static IPs: VM-level stop/start works today, but a stopped GCE VM releases its ephemeral external IP and gets a fresh one on resume (mngr rebinds known_hosts automatically). A reserved static IP would keep the address stable across stop/start.
