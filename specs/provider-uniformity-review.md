# Provider Uniformity Review

Current state of user-visible behavior across all `mngr` provider plugins. Companion to `specs/provider-shape.md` (prescriptive contract) and `specs/provider-release-tests.md` (proposed release-test trips).

**Scope.** Nine providers:
- `mngr_modal` — hosted Modal sandboxes
- `mngr_aws`, `mngr_azure`, `mngr_gcp` — cloud VMs (Debian 12 cloud-init / GCE startup-script)
- `mngr_vultr`, `mngr_ovh` — cloud VPS
- `mngr_lima` — local macOS VM
- `mngr.providers.docker` — local Docker
- `mngr.providers.ssh` — BYO host

Each cloud provider now has **two shapes**, selected by the `isolation` config knob (`VpsProviderConfig.isolation`, default `CONTAINER`): the agent runs inside a Docker container (`IsolationMode.CONTAINER`) or directly on the VM OS with no container (`IsolationMode.NONE`, "bare"). `IsolationMode` is defined in `libs/mngr_vps/imbue/mngr_vps/primitives.py`. Bare is gated per-provider — only AWS/GCP/Azure accept `isolation=NONE`; Vultr/OVH reject it. See "Isolation modes / realizer seam" below.

Shared base: `libs/mngr_vps/`. Interfaces: `libs/mngr/imbue/mngr/interfaces/{provider_instance,host,provider_backend}.py`.

---

## TL;DR — open findings ranked

| # | Finding | Severity | Category |
|---|---|---|---|
| 1 | Vultr/OVH `mngr stop --stop-host` silently leaks compute (container only; VPS keeps billing) | high (cost) | Stop |
| 2 | No auto-snapshot on AWS/Azure/GCP/Vultr/OVH create — hard-crash recovery is Modal-only | high | Snapshots |
| 3 | Vultr/OVH have no `pytest_sessionfinish` orphan scanner — release-test crash leaks billable VPS | high (cost) | Tests |
| 4 | Vultr/OVH have no `allowed_ssh_cidrs` field — VPS is public-internet-reachable as soon as it boots | high (security) | Networking |
| 5 | Vultr/OVH idle-driven self-stop still OS-halt-bills (container `shutdown -P` doesn't stop hourly/monthly billing) | high (cost) | Idle/cost |
| 6 | Stopped-host visibility absent on Vultr/OVH — Modal+AWS+Azure+GCP show stopped hosts in `mngr list`; Vultr/OVH cannot | medium-high | Discovery |
| 7 | SSH `supports_shutdown_hosts=True` but `stop_host` raises `NotImplementedError` — user sees stack trace | medium | Capability |
| 8 | `supports_volumes=True` on VPS family but `list_volumes()` returns `[]` | medium | Capability |
| 9 | `start_host(snapshot_id=…)` silently no-ops on AWS/Azure/GCP/Vultr/OVH; `create_host(snapshot=…)` same | medium | Snapshots |
| 10 | Vultr/OVH `mngr stop --stop-host` and `mngr start` aren't implemented; release tests named for them are misleading | medium | Lifecycle |
| 11 | Vultr `build_provider_instance` silently swallows missing-creds `ValueError`; OVH never raises `ProviderUnavailableError` | high | Errors |
| 12 | OVH `mngr destroy` cancels at billing-cycle end, not immediately — VPS keeps running until expiration | medium | Lifecycle |
| 13 | Docker provider `-p :22` binds `0.0.0.0:<random>:22` on host's LAN | medium (security) | Networking |
| 14 | `auto_shutdown_seconds` wired through to cloud API is not pinned by any test | medium | Tests |
| 15 | GCP lowercases mngr-provider label — mixed-case provider names silently collide | medium | Discovery |
| 16 | Container-shape knobs (`--cpu`/`--memory`/`--gpu`) are Modal-only; no cross-provider "~2 vCPU/4GB" alias | medium | Create UX |
| 17 | Modal has no `mngr modal cleanup` analog (cloud trio + OVH-list all do) | low-medium | Destroy |
| 18 | Modal underscore tag keys (`mngr_host_id`) vs dash elsewhere (`mngr-host-id`) | low | Tagging |

**Recently resolved (no longer open).** The `mngr/bare-providers` merge closed several findings that this review previously ranked high:
- **`--stop-host` compute leak on AWS/GCP/Azure** — now a real machine-level stop/deallocate. The base `OfflineCapableVpsProvider.stop_host` (`libs/mngr_vps/imbue/mngr_vps/instance_offline.py`) calls each cloud's `_pause_cloud_instance` hook: AWS → client `stop_instance` (EBS preserved); GCP → instance stop landing in `TERMINATED`; Azure → client `deallocate_instance` (true deallocate, billing stops). Still open only for Vultr/OVH (finding #1).
- **Azure `auto_shutdown_seconds` "Stopped (not deallocated)" billing leak** — the idle path now runs an ARM self-deallocate via the VM's managed identity + IMDS token (`_build_self_deallocate_script` in `libs/mngr_azure/imbue/mngr_azure/backend.py`), so compute billing actually stops.
- **Idle-driven self-stop only on Modal+AWS** — GCP and Azure now self-stop too (GCP guest `shutdown -P now` lands `TERMINATED`; Azure managed-identity deallocate).
- **Stopped-host visibility asymmetric (Modal+AWS only)** — now also on Azure and GCP. AWS/Azure use a shared S3/Blob state bucket (full records); GCP uses a GCE metadata mirror. Reconstruction logic lifted into the `OfflineCapableVpsProvider` base class.
- **AWS/GCP `ProviderUnavailableError` fell through to default "start Docker" help text** (was finding #7) — all three clouds now curate help text (`_aws_unavailable_error`, `_gcp_unavailable_error`, `_azure_unavailable_error` in each provider's `backend.py`).
- **Modal raised the wrong class on missing creds** (was finding #12) — Modal now raises `ProviderUnavailableError` with curated help text at construction (`ModalProviderBackend._construct_modal_provider` catching `ModalProxyAuthError`). Only a runtime auth error mid-discovery still surfaces as `ModalAuthError`.

**Headline uniform strengths.** Across the cloud trio (AWS/GCP/Azure), `mngr stop --stop-host` + `mngr start` (real machine stop/start), idle-driven self-stop, and offline (stopped-host) visibility are now uniform — the three pillars that were previously AWS-only or Modal-only. `auto_shutdown_seconds` field name shared across the VPS family; `CleanupFailedGroup` honored uniformly at API + CLI; cloud-trio `_validate_provider_args_for_create` identical shape; Modal + AWS + Azure + GCP all have `pytest_sessionfinish` orphan scanners; `default_idle_timeout = 800 s` on 8/9 providers; `debian:bookworm-slim` container image uniform; cloud-trio `allowed_ssh_cidrs` now uniformly `("0.0.0.0/0",)` with warning (SSH is key-only — see shape doc §3.1); curated `ProviderUnavailableError` help text uniform across the trio (`_aws_unavailable_error`/`_gcp_unavailable_error`/`_azure_unavailable_error`). Live multi-agent discovery is no longer Modal-only — the VPS family reads in-VM/in-container agents through the realizer (`HostRealizer.read_live_listing`, `libs/mngr_vps/imbue/mngr_vps/interfaces.py`; Docker impl `docker_realizer.py`, bare impl `bare_realizer.py`).

---

## Isolation modes / realizer seam

The `mngr/bare-providers` merge split placement realization behind a `HostRealizer` seam (`libs/mngr_vps/imbue/mngr_vps/interfaces.py`). `VpsProviderConfig.isolation` (`config.py`, default `IsolationMode.CONTAINER`; `IsolationMode` enum in `libs/mngr_vps/imbue/mngr_vps/primitives.py`) selects the realizer in `VpsProvider._realizer_for_isolation` (cached behind the `_realizer` property, `instance.py`):

| Mode | Realizer | Where the agent runs | `supports_snapshots` | Idle self-stop |
|---|---|---|---|---|
| `CONTAINER` (default) | `DockerRealizer` (`docker_realizer.py`) | Inside a Docker container on the VM; SSH on `0.0.0.0:<port>` → container `:22`; btrfs/gVisor as configured | `True` (`docker commit`) | sentinel + systemd `.path`/`.service`, then `shutdown -P now` on the host |
| `NONE` (bare) | `BareRealizer` (`bare_realizer.py`) | Directly on the VM OS at `vps_ip:22`, no Docker, systemd-owned; host store on root disk at `/var/lib/mngr-host` | **`False`** (structurally not a `SnapshotCapableRealizer`; no `snapshot_placement` method — the provider raises `SnapshotsNotSupportedError` via `_require_snapshot_capable_realizer`) | `idle_shutdown_command = "shutdown -P now"`, `idle_shutdown_stops_host = True` — the bare VM powers itself off directly (no sentinel indirection) |

`HostRealizer` carries `config/mngr_ctx/key_dir/host_dir/provider_name` and abstracts the per-mode operations: `realize_placement`, `stop_placement`, `start_placement`, `teardown_placement`, `snapshot_placement`, live-listing (`read_live_listing`/`collect_listing_output`/`find_host_record`), and idle wiring (`start_activity_watcher`/`idle_shutdown_command`/`idle_shutdown_stops_host`). The container path's original logic moved wholesale into `DockerRealizer`; the bare path reads the root-disk host store directly.

**Bare gating.** `VpsProvider.create_host` raises `BareIsolationNotSupportedError` (`libs/mngr_vps/imbue/mngr_vps/errors.py`) when `isolation is NONE` and `not self._supports_bare_isolation`. `_supports_bare_isolation` defaults `False` (`VpsProvider`) and is overridden `True` only by AWS, GCP, and Azure (each provider's `backend.py`). Vultr and OVH inherit the default and reject `isolation=NONE` — bare needs a machine stop/start lifecycle (idle agent powers the VM off, `mngr start` boots it again), which those providers don't have. On Azure, bare runs the same managed-identity ARM deallocate as the container path.

A bare host record carries `None` for `container_name`/`container_id`/`volume_name`/`container_ssh_host_public_key` (`RealizedPlacement` fields are nullable; bare returns an empty `RealizedPlacement()`).

---

## Lifecycle matrices

What each provider actually does for each verb, with concrete code locations.

### `mngr create`

| Provider | What happens | Cite |
|---|---|---|
| modal | Generate `HostId`; build Modal image (or load from named snapshot); create sandbox; SSH-tunnel start; write host record to Modal Volume; deploy `snapshot_and_shutdown`; start activity watcher; the `on_agent_created` hookimpl triggers an "initial" snapshot. | `mngr_modal/instance.py` (`create_host`); `mngr_modal/backend.py` (`on_agent_created` hookimpl) |
| aws | Base path; AWS contributes `_create_vps_instance` (RunInstances + SG + EBS DeleteOnTermination + IMDSv2 + spot + AMI), sentinel-file shutdown script, `_on_host_finalized` (systemd `.path`/`.service` self-stop units), pytest gate, `_supports_bare_isolation=True`. | base `mngr_vps/instance.py` (`create_host`); `mngr_aws/backend.py` |
| azure | Base path; Azure contributes `_create_vps_instance` (vnet/subnet resolution, VM with cascade-delete on NIC/IP/OS-disk, cloud-init via base64 `custom_data`, spot+eviction-Delete; on VM-create failure deletes its own just-created NIC/IP via `_delete_nic_and_public_ip`), pytest gate, managed-identity self-deallocate idle watcher, `_supports_bare_isolation=True`. (The orphan sweep `reclaim_orphaned_network_resources` runs at GC time, not on create.) | `mngr_azure/client.py` (`create_instance`); `backend.py` |
| gcp | Base path; GCP contributes `_create_vps_instance` (CE insert + `max_run_duration` + `instance_termination_action=DELETE`, spot+same-DELETE), `_validate_provider_args_for_create` (firewall pre-flight + project warning + pytest gate), sentinel + systemd self-stop idle watcher, `_supports_bare_isolation=True`. GCE startup-script bootstrap (not cloud-init). | `mngr_gcp/backend.py`; `client.py` |
| vultr | Base path; Vultr contributes only `VultrVpsClient.create_instance` HTTP POST. Cloud-init `shutdown -P` wired but doesn't stop hourly billing. | `mngr_vultr/client.py` (`create_instance`); `backend.py` (`build_provider_instance`) |
| ovh | Overrides `_provision_vps`: pending-orders reconcile → recycle-pool claim → order-and-wait → IAM tagging → SSH-key bootstrap → `apply_host_setup_on_outer(is_qemu_purge_enabled=True)`. | `mngr_ovh/backend.py` (`_provision_vps`); ordering, recycle, bootstrap modules |
| lima | Direct on `BaseProviderInstance`. Generate `lima.yaml`; btrfs additional disk if exposed; `limactl_start_new`; wait cloud-init; SSH; install shutdown script; activity watcher. Cleanup-on-fail deletes VM + orphaned disk. | `mngr_lima/instance.py` (`create_host`) |
| docker | Direct on `BaseProviderInstance`. Build per-host image; `docker run -p :22` (binds **all interfaces**); SSH setup via `docker exec`. | `mngr/providers/docker/instance.py` (`create_host`) |
| ssh | `raise NotImplementedError`. | `mngr/providers/ssh/instance.py` (`create_host`) |

### `mngr stop my-agent` (no flag)

Uniform across all nine: stops the agent's tmux session only. Never reaches `ProviderInstanceInterface.stop_host`. Agent-layer `stop_agents` path uses `collecting_cleanup_failures`.

### `mngr stop --stop-host`

| Provider | Behavior | Cost | Cite |
|---|---|---|---|
| modal | Raises `HostShutdownNotSupportedError` — `supports_shutdown_hosts=False`. | None (error before side effects). | `mngr_modal/instance.py` (`stop_host`) |
| aws | Base `stop_host` calls realizer `stop_placement` (docker-stop / bare no-op), then the `_pause_cloud_instance` hook → client `stop_instance` with EBS preserved; base writes record + mirrors externally. Idempotent. | Compute billing stops; EBS storage continues. | `mngr_aws/backend.py` (`_pause_cloud_instance`); base `mngr_vps/instance_offline.py` (`stop_host`) |
| azure | Base `stop_host` → realizer `stop_placement`, then `_pause_cloud_instance` → client `deallocate_instance` (true deallocate — billing stops, not OS-stop). | Compute billing stops; OS disk continues. | `mngr_azure/backend.py` (`_pause_cloud_instance` → `deallocate_instance`) |
| gcp | Base `stop_host` → realizer `stop_placement`, then `_pause_cloud_instance` → instance stop (instance → `TERMINATED`). | Compute billing stops; boot disk continues. | `mngr_gcp/backend.py` (`_pause_cloud_instance`) |
| vultr | **Inherited** base = realizer `stop_placement` (container only). VPS stays running. | **Silent cost leak (hourly).** | base only |
| ovh | **Inherited** = realizer `stop_placement` (container only). VPS bills monthly (no proration). | Stop saves nothing this month; eventual expiry if not recycled. | base only |
| lima | `limactl stop` — real VM pause. `create_snapshot=True` parameter ignored (Lima has no snapshots). | None. | `mngr_lima/instance.py` (`stop_host`) |
| docker | `docker stop` — native container stop; state preserved. Optional `docker commit` snapshot first. | None. | `mngr/providers/docker/instance.py` (`stop_host`) |
| ssh | `raise NotImplementedError` **despite** `supports_shutdown_hosts=True`. CLI gate lets the call through. | User sees internal traceback. | `mngr/providers/ssh/instance.py` (`stop_host`) |

### `mngr start`

| Provider | Behavior | Idempotent | Honors `--snapshot`? |
|---|---|---|---|
| modal | If running, return; else build sandbox from most-recent-or-named snapshot; re-attach volumes. | Yes | **Yes** (warns if running + snapshot_id) |
| aws | Locate stopped instance by `mngr-host-id` tag; `StartInstances`; rebind known_hosts with preserved host keys; remove idle sentinel; relaunch activity watcher. | Yes | **No** (silent no-op) |
| azure | `start_instance` (boots the deallocated VM); rebind, restart watcher. | Yes | **No** |
| gcp | `start_instance` (boots the `TERMINATED` VM); rebind, restart watcher. | Yes | **No** |
| vultr/ovh | Inherited: realizer `start_placement` (`docker start`, re-exec sshd). VPS was never stopped → container restart only. | Yes | **No** |
| lima | Read record; `limactl_start_existing`; fresh SSH config (port may change); restart watcher. | Yes | **No** (param accepted but unused) |
| docker | Three paths: running → return; `snapshot_id` → remove + recreate from snapshot image; else `docker start` + reinit sshd. | Yes | **Yes** |
| ssh | `raise NotImplementedError`. | n/a | n/a |

Base `start_host` now relaunches the activity watcher on resume (`libs/mngr_vps/imbue/mngr_vps/instance_offline.py`, `start_host`) — fixes the idle-host re-stop bug Vultr/OVH used to hit. Like `stop_host`, machine-level start is layered in via each cloud's `_resume_cloud_instance` hook (not a subclass `start_host` override); Vultr/OVH supply no such hook and inherit container-only.

### `mngr destroy`

| Provider | Destroyed | Preserved | Cite |
|---|---|---|---|
| modal | Sandbox terminated (snapshot=False); per-host agent records; host record → `DESTROYED`; host Volume deleted. | **Snapshot records preserved** for `gc_snapshots` age-gating. | `mngr_modal/instance.py` (`destroy_host`) |
| aws | Base teardown (`DockerRealizer.teardown_placement` for container) → `TerminateInstances` → EBS auto-deleted; EC2 SSH key removed. | None AWS-specific. | base `mngr_vps/instance.py` (`destroy_host`); `docker_realizer.py` (`teardown_placement`) |
| azure | Base teardown → `begin_delete()` VM; NIC + public IP + OS disk cascade. SSH key deleted. | None Azure-specific. | base + `mngr_azure/client.py` |
| gcp | Base teardown → GCE DELETE; `auto_delete=True` on boot disk. | None GCP-specific. | base + `mngr_gcp/client.py` |
| vultr | Base teardown → `DELETE /instances/{id}`. SSH key removed. | None. | base + `mngr_vultr/client.py` |
| ovh | Base teardown → `PUT serviceInfos` with `renew.deleteAtExpiration=true`. **VPS keeps running until billing-cycle boundary; marked for cancellation, not deleted now.** May be recycled by next `mngr create`. | VPS itself, IAM tags, disk through end of billing cycle. | OVH treats destroy as "mark for cancellation" (`set_renew_at_expiration` in `mngr_ovh/client.py`) |
| lima | `limactl_delete(force=True)` + disk delete if separate. Already-gone is benign. | None. | `mngr_lima/instance.py` (`destroy_host`) |
| docker | `container.remove(force=True)`; untag per-host build image; mark `DESTROYED`. | **Snapshots and host-volume directory preserved** for `gc_snapshots`. | `mngr/providers/docker/instance.py` (`destroy_host`) |
| ssh | `raise NotImplementedError`. | n/a | `mngr/providers/ssh/instance.py` (`destroy_host`) |

### `mngr <provider> cleanup`

| Provider | Exists? | Scope | Refusal |
|---|---|---|---|
| modal | **No CLI module.** | n/a | n/a |
| aws | Yes — deletes the `mngr-aws` SG (plus state bucket + identity). | Region. | Refuses if any mngr-tagged instance exists. |
| azure | Yes — deletes the managed RG (cascade vnet/subnet/NSG). | Per-RG (`managed-by=mngr` tag-gated). | Refuses if any mngr-tagged VM exists. |
| gcp | Yes — deletes the firewall rule. | Project. | Refuses if any tagged mngr instance exists across all zones (`aggregatedList`). |
| vultr | **No CLI module.** | n/a | n/a |
| ovh | `mngr ovh list` only (read-only). | n/a | n/a |
| lima/docker/ssh | Use global `mngr cleanup` (no per-provider verb). | n/a | n/a |

---

## `CleanupFailedGroup` adoption

Contract (`libs/mngr/imbue/mngr/interfaces/provider_instance.py`): `destroy_host` raises `CleanupFailedGroup` if any real infrastructure resource was left behind. Downstream consumers honor the new contract: `mngr/api/gc.py`, `mngr/api/cleanup.py`, `mngr/cli/headless_runner.py`. `mngr gc` now exits with cause-specific non-zero codes (`LOCAL_STATE_REMAINS` / `HOST_RESOURCE_REMAINS` / `PROVIDER_INACCESSIBLE` / `OTHER`).

For the VPS family, the container destroy steps (`remove_container`, `remove_volume`, `delete_btrfs_subvolume`) now run inside `DockerRealizer.teardown_placement` (`libs/mngr_vps/imbue/mngr_vps/docker_realizer.py`), which the base `destroy_host` (`mngr_vps/instance.py`) invokes before the cloud subclass deletes the instance and SSH key. Bare placements have no container teardown.

| Provider | Uses `collecting_cleanup_failures`? | Raises group for own resources? | Notes |
|---|---|---|---|
| modal | **Yes** (`mngr_modal/instance.py`, `destroy_host`) | Yes — sandbox-terminate, agent-records, host-record, host-volume classified separately. | Native adopter. |
| aws/azure/gcp/vultr/ovh | No (no override) | Indirectly via base aggregation. | Azure `reclaim_orphaned_network_resources` does NOT feed aggregation — reclaim failure logs and moves on. |
| lima | Yes | Yes — VM delete + disk delete classified separately. | Native adopter. |
| docker | Yes | Yes — container + volume + image classified separately. | Native adopter. |
| ssh | n/a (`NotImplementedError`) | n/a | Out of scope. |

---

## Cross-provider defaults

| Field | modal | aws | azure | gcp | vultr | ovh | lima | docker | ssh |
|---|---|---|---|---|---|---|---|---|---|
| `allowed_ssh_cidrs` | n/a | `("0.0.0.0/0",)` | `("0.0.0.0/0",)` | `("0.0.0.0/0",)` | **no field** | **no field** | n/a | n/a | n/a |
| `default_idle_timeout` | 800 | 800 | 800 | 800 | 800 | 800 | 800 | 800 | **absent** |
| `auto_shutdown_seconds` field | n/a (`default_sandbox_timeout=900`) | `None` | `None` | `None` | `None` (inherited but unused) | `None` (inherited but unused) | absent | absent | absent |
| `auto_shutdown_seconds` effect | sandbox timeout | stop/terminate (no bill) | **deallocate (no bill; managed-identity ARM)** | terminate (no bill) | OS halt; VPS bills | OS halt; VPS bills | n/a | n/a | n/a |
| Pytest gate | env-name pattern | yes | yes | yes | **no** | **no** | n/a | n/a | n/a |
| `pytest_sessionfinish` scanner | yes | yes | yes (+ NIC reclaim) | yes | **no** | **no** | local | local | n/a |
| Default region/zone | None (Modal chooses) | `us-east-1` | `westus` | `us-west1`/`us-west1-a` | `ewr` | `US-EAST-VA` | host | host | host |
| Default shape | `cpu=1`, `memory=1 GB` | `t3.small` | `Standard_B2s` | `e2-small` | `vc2-2c-4gb` | `vps-2025-model1` | inherits | host | n/a |
| Default OS image | `debian_slim` | Debian 12 (per-region AMI map) | Debian 12 (`debian-12:12-gen2`) | Debian 12 family (global) | Debian 12 x64 | Debian 12 - Docker | aarch64/x86_64 qcow2 | n/a | n/a |
| Default container image | n/a | `debian:bookworm-slim` | same | same | same | same | n/a | `debian:bookworm-slim` | n/a |
| Default disk | n/a | `root_volume_size_gb=30` | `os_disk_size_gb=30` | `boot_disk_size_gb=30` | bundled | bundled | `host_data_disk_size=100 GiB` | shared host volume | n/a |
| Public IP default | n/a | True | True | True | always | always | host-only | n/a | n/a |
| `supports_snapshots` | True | realizer-derived: True (container) / **False (bare)** | same | same | True | True | **False** | True | **False** |
| `supports_shutdown_hosts` | **False** | True (real EC2 stop) | True (real deallocate) | True (real GCE stop) | True (silent: container only) | True (silent: container only) | True (real `limactl stop`) | True (real `docker stop`) | **True (lying — raises NotImplementedError)** |
| `supports_volumes` | True | **True (lies: returns `[]`)** | **lies** | **lies** | **lies** | **lies** | True | True | **False** |
| `supports_mutable_tags` | True | **False** | False | False | False | False | True | **False** | False |

Disk-size knob name differs three ways across the cloud trio (`root_volume_size_gb` / `os_disk_size_gb` / `boot_disk_size_gb`) — all default 30 GB. Per-host SSH key is per-instance on the VPS family; Modal stores one key per profile name (shared between two named Modal instances).

`supports_volumes` is still `True`-but-`[]` on the whole VPS family: `list_volumes` returns `[]` (`mngr_vps/instance.py`, `list_volumes`) and `delete_volume` is a no-op, inherited unchanged by AWS/GCP/Azure/Vultr/OVH. The state bucket added a `get_volume_for_host` on the base `OfflineCapableVpsProvider` (S3Volume/BlobVolume on AWS/Azure), but `list_volumes`/`delete_volume` remain unimplemented and the flag was not flipped.

---

## Tag conventions

| Concept | modal | aws | azure | gcp | vultr | ovh | docker |
|---|---|---|---|---|---|---|---|
| host id | `mngr_host_id` (underscore) | `mngr-host-id` | `mngr-host-id` | `mngr-host-id` (label, lowercased) | n/a (SSH-only discovery) | IAM v2 tag | `com.imbue.mngr.host-id` |
| provider | `mngr_user_<prefix>` | `mngr-provider` | `mngr-provider` | `mngr-provider` (label) | `mngr-provider=<name>` (flat) | IAM v2 tag | `com.imbue.mngr.provider` |
| host name | `mngr_host_name` | `Name=mngr-<name>` (cascade) | from VM name | from instance name | n/a | n/a | `com.imbue.mngr.host-name` |
| agent records | on Volume | in S3 state bucket | in Blob state bucket | in GCE metadata | n/a | n/a | `com.imbue.mngr.tags` JSON |
| Managed-by | n/a | n/a | `managed-by=mngr` on RG | n/a | n/a | n/a | n/a |

GCP `to_gce_label_value()` lowercases all values — mixed-case provider name silently collides with lowercased twin.

---

## Errors and credential classification

Contract: `ProviderUnavailableError` for "state unknown" (creds missing, API down); `ProviderEmptyError` for "state known empty" (Modal env doesn't exist yet); never silently `return []`.

| Failure | modal | aws | azure | gcp | vultr | ovh | lima | docker | ssh |
|---|---|---|---|---|---|---|---|---|---|
| Creds missing | `ProviderEmptyError` if env absent; `ProviderUnavailableError` (curated help) if token absent | `ProviderUnavailableError` (curated help) | `ProviderUnavailableError` (curated help) | `ProviderUnavailableError` (curated help) | **silent `[]` + WARN** | **silent `[]`** | `LimaNotInstalledError` | `ProviderUnavailableError` | n/a |
| Bad creds | `ModalAuthError` → wrapped `ProviderDiscoveryError` (runtime, mid-discovery) | `ProviderUnavailableError` (curated) | `ProviderUnavailableError` (curated) | `ProviderUnavailableError` (curated) | silent `[]` | propagates as `ProviderDiscoveryError` | `LimaVersionError` | `ProviderUnavailableError` (transport only) | n/a |
| Backend reachable, empty | n/a | normal `[]` from `list_instances` | normal `[]` | normal `[]` | normal `[]` | normal `[]` | normal `[]` | normal `[]` | n/a |

All three clouds supply curated `user_help_text`: `_aws_unavailable_error`, `_gcp_unavailable_error`, and `_azure_unavailable_error` (each in the provider's `backend.py`). The default "start Docker" message in `libs/mngr/imbue/mngr/errors.py` survives only for providers that don't curate. Modal's creds-missing path now raises `ProviderUnavailableError` (curated) at construction (`ModalProviderBackend._construct_modal_provider` catching `ModalProxyAuthError`); `ModalAuthError` (a `PluginMngrError`) still exists but only fires on a runtime `ModalProxyAuthError` mid-discovery, where `mngr list`/`mngr gc` see a generic `ProviderDiscoveryError`.

`mngr gc` (no `--provider`) catches `ProviderUnavailableError` and logs at DEBUG only (`libs/mngr/imbue/mngr/api/providers.py`) — but `mngr gc` itself now exits with cause-specific non-zero codes on any failed sweep.

---

## Discovery and offline visibility

| State | modal | aws | azure | gcp | vultr | ovh | lima | docker | ssh |
|---|---|---|---|---|---|---|---|---|---|
| RUNNING | shown | shown | shown | shown | shown | shown | shown | shown | shown |
| STOPPED (after `--stop-host`) | shown (record on Volume) | shown (S3 state bucket) | shown (Blob state bucket) | shown (GCE metadata mirror) | N/A — no machine stop | N/A — no machine stop | shown | shown | N/A |
| CRASHED / unreachable | shown | shown (state bucket) | shown (state bucket) | shown (metadata) | shown if cached | shown if cached | shown | shown | **always "RUNNING"** (hard-coded) |
| DESTROYED (`--include-destroyed`) | shown | shown | shown | shown | shown | shown | shown | shown | n/a |
| Live multi-agent (in-container) | shown | shown | shown | shown | shown | shown | shown | shown | shown (over SSH) |

Live agent discovery is now a realizer method (`HostRealizer.read_live_listing` / `collect_listing_output` / `find_host_record`, `libs/mngr_vps/imbue/mngr_vps/interfaces.py`) — Docker impl in `docker_realizer.py`, bare impl in `bare_realizer.py` (reads the root-disk store directly). In-VM/in-container agents are visible across the VPS family.

Stopped-host (offline) reconstruction is lifted into the base class `OfflineCapableVpsProvider` (`libs/mngr_vps/imbue/mngr_vps/instance_offline.py`), which all three clouds (`AwsProvider`, `AzureProvider`, `GcpProvider`) extend directly. Per-provider hooks: `_offline_discovered_host_from_instance`, `_is_instance_offline`, `_offline_agent_dicts_for`, `_state_store`. Offline state is backed by a single external `HostStateStore`. The offline mirror is no longer Modal+AWS only:
- AWS, Azure: a state bucket (`BucketHostStateStore` over `StateBucket`; concrete `S3StateBucket`/`BlobStateBucket` in each provider's `state_bucket.py`) holds full host *and* agent records. The bucket is **required** — there is no degraded/no-bucket mode; an unprovisioned bucket raises `missing_state_bucket_error` (`libs/mngr_vps/imbue/mngr_vps/host_state_store.py`).
- GCP: deliberately no bucket — uses a GCE-metadata-backed store (extends `OfflineCapableVpsProvider` directly).
- Modal: per-agent JSON files on the state Volume (unchanged).
- Vultr/OVH/Lima/docker/ssh: no offline mirror.

Name resolution for `mngr exec <name>` works while stopped on Modal, AWS, Azure, and GCP; Vultr/OVH have no stopped state.

---

## Snapshots

Modal auto-snapshots on every agent create (`is_snapshotted_after_create=True`; the `on_agent_created` hookimpl in `libs/mngr_modal/imbue/mngr_modal/backend.py`, plus `on_agent_created` → `_create_initial_snapshot` in `instance.py`). On the VPS family `supports_snapshots` is now **realizer-class-derived** (`VpsProvider.supports_snapshots` returns `isinstance(self._realizer, SnapshotCapableRealizer)`, `mngr_vps/instance.py`): container placements (`DockerRealizer`, a `SnapshotCapableRealizer`) report `True` and snapshot via `docker commit` of the container layer on the host's own disk — survives `mngr stop` but **not** `mngr destroy`; bare placements (`BareRealizer`) report `False` (no `snapshot_placement` method; the provider raises `SnapshotsNotSupportedError` at its boundary via `_require_snapshot_capable_realizer`). The cloud-trio `VpsClientInterface` disk-snapshot methods were deleted entirely; user-facing `mngr snapshot create` everywhere except Modal is `docker commit` (container) or unsupported (bare).

| Capability | modal | aws | azure | gcp | vultr | ovh | lima | docker | ssh |
|---|---|---|---|---|---|---|---|---|---|
| `supports_snapshots` | True | True (container) / **False (bare)** | same | same | True | True | **False** | True | **False** |
| `mngr snapshot create` | `sandbox.snapshot_filesystem()` | `docker commit` / **raises (bare)** | same | same | same | same | raises | `docker commit` | raises |
| Auto on create | Yes | No | No | No | No | No | n/a | No | n/a |
| `start_host(snap_id)` honored | Yes | **ignored** | ignored | ignored | ignored | ignored | ignored | Yes | n/a |
| `create_host(snapshot=)` honored | Yes | **ignored** | ignored | ignored | ignored | ignored | n/a | ignored | n/a |
| Survives `destroy_host` | Yes | No | No | No | No | No | n/a | No | n/a |

`supports_snapshots=True` means different things on Modal (persistent, portable) vs cloud-VPS container (single-host docker layer, dies with VPS); bare reports `False`. No `supports_persistent_snapshots` distinction today.

---

## Build args

| Concept | modal | aws | azure | gcp |
|---|---|---|---|---|
| Region/placement | `--region=NAME` | `--aws-region` | `--azure-region` | `--gcp-zone` (zonal!) |
| Shape | `--cpu` + `--memory` + `--gpu` | `--aws-instance-type` | `--azure-vm-size` | `--gcp-machine-type` |
| Image override | `--image=NAME` | `--aws-ami=AMI-ID` | n/a | `--gcp-image` (`8a0fd81de`) |
| Dockerfile | `--file=PATH` | passthrough | passthrough | passthrough |
| Spot | n/a | `--aws-spot` | `--azure-spot` | `--gcp-spot` |
| Git depth | n/a | `--git-depth=N` | `--git-depth=N` | `--git-depth=N` |
| Timeout | `--timeout=SEC` | use `auto_shutdown_seconds` | use `auto_shutdown_seconds` | use `auto_shutdown_seconds` |
| Block outbound | `--offline` | n/a | n/a | n/a |
| CIDR allowlist | `--cidr-allowlist` | n/a | n/a | n/a |
| Secret env | `--secret=VAR` | n/a | n/a | n/a |
| Volume mount | `--volume=NAME:PATH` | n/a | n/a | n/a |

Modal's bare-flag shorthand (`cpu=2 offline`) is Modal-only. Cloud providers require the `--<provider>-` prefix.

Cross-region/zone create from the wrong-region client: uniform `VpsApiError(400, "Cross-region create not supported")` across AWS/Azure/GCP.

---

## Test coverage gaps

Roughly half of in-scope user-visible behaviors have a provider-specific test pin. AWS/GCP/Azure release coverage now runs the shared trip harness `run_provider_release_trip{1..4}` (`libs/mngr/imbue/mngr/providers/provider_release_testing.py`), with `test_provider_release_trip1..4` in each provider's `test_release_<name>.py`, parametrized over both `IsolationMode.CONTAINER` and `IsolationMode.NONE` (bare) for trips 1-3 (Trip 1 = lifecycle + sketchy-kill + gc; Trip 2 = idle auto-shutdown + stop/start-resume; Trip 3 = snapshot-survives behavior; Trip 4 = error classification). So bare *and* machine stop/start are covered across the whole cloud trio, not just AWS. Plus unit tests (`mngr_vps/bare_realizer_test.py`) and S3 volume round-trips (`mngr_aws/s3_volume_test.py`). Top remaining holes:

1. **Vultr/OVH `pytest_sessionfinish` orphan scanner missing.** Easy: copy `mngr_aws/conftest.py` pattern.
2. **`auto_shutdown_seconds` flow-through to cloud API.** Pre-create gate fires; no test asserts the value reaches `shutdown -P +60` in user_data on AWS/Vultr/OVH or `max_run_duration` in GCP scheduling or the customData/managed-identity deallocate line on Azure.
3. **Stopped-host discovery on Vultr/OVH/Lima.** AWS pins exhaustively; GCP/Azure now have stop/start + offline reconstruction (bucket/metadata); Vultr/OVH/Lima don't.
4. **Capability-flag pinning** for AWS/Azure/GCP/Vultr/OVH (Modal/Lima/Docker/SSH pin; cloud trio doesn't). Should cover both isolation shapes (`supports_snapshots` flips with the realizer).
5. **Networking warning on open CIDR for GCP/Azure.** AWS pins; cloud trio's now-uniform default deserves the same warning pin.
6. **Vultr/OVH stop/start lifecycle tests** named `test_create_stop_start_destroy` but providers don't implement stop/start — name lies.
7. **Pytest gate missing on Modal/Vultr/OVH/Lima.** Copy the AWS/Azure/GCP pattern.
8. **Cross-region/zone refusal on Vultr/OVH.** AWS/Azure/GCP pin; the other two don't.
9. **SSH credentials error classification.** Empty config should raise `ProviderEmptyError`; not pinned.
10. **`CleanupFailedGroup` raise-on-partial-failure tests.** Zero hits across all providers' `_test.py` files. Belongs in unit tests with mocked destroy helpers.
11. **Multi-agent (shape §1.8) release-test coverage.** No provider pins the second-agent case; see release-tests Trip 1b.

Test-style differences worth aligning: AWS uses `botocore.Stubber`; Azure hand-rolled mocks; GCP `mock_compute_v1`; Vultr `unittest.mock` over `requests`; OVH custom client stub. Per-provider release-test gate env vars also diverge (`MNGR_AWS_RELEASE_TESTS=1` etc. for the cloud trio; Vultr/OVH gate on credential presence). Lifecycle release-test name (`test_provider_lifecycle_create_exec_and_destroy`) is uniform across the cloud trio — preserve this when consolidating into the proposed shared trip harness.

---

## Recommendations

Ordered by impact × ease.

### Single-line correctness fixes

1. **Flip SSH `supports_shutdown_hosts` to `False`.** `mngr/providers/ssh/instance.py` (`supports_shutdown_hosts`).
2. **Bump `mngr gc` log level on `ProviderUnavailableError`** from DEBUG to WARNING (`libs/mngr/imbue/mngr/api/providers.py`).
3. **Override `supports_volumes` to `False` on the VPS family** until `list_volumes`/`delete_volume` actually work (the state bucket added `get_volume_for_host`, but the flag still over-promises).

### Curated `user_help_text` cleanup

4. ~~Add `_aws_unavailable_error` and `_gcp_unavailable_error` mirroring `_azure_unavailable_error`.~~ **Done** — all three clouds now curate help text.
5. ~~Make Modal's missing-creds path raise `ProviderUnavailableError` so it joins the contract.~~ **Done** — Modal now raises `ProviderUnavailableError` at construction (only a runtime auth error mid-discovery still surfaces as `ModalAuthError`).
6. **Replace Vultr/OVH silent-empty-on-missing-creds with `ProviderUnavailableError`.**

### Lifecycle and cost safety

7. **Override `stop_host` on Vultr** (and decide OVH) to actually stop the VPS — OR set `supports_shutdown_hosts = False` for those two until that lands. (AWS/GCP/Azure now do real machine stop/deallocate.)
8. **Add `pytest_sessionfinish` orphan scanner to Vultr/OVH.** OVH is high-cost (monthly billing).
9. **Override `_validate_provider_args_for_create` on Vultr/OVH** to require `auto_shutdown_seconds` in pytest.

### Snapshots and capability honesty

10. **Honor `start_host(snapshot_id=…)` and `create_host(snapshot=…)` on the VPS family, or raise `SnapshotsNotSupportedError`.** Silent no-op is the worst option.
11. **Add a `supports_persistent_snapshots` flag** to honestly distinguish Modal (snapshots survive destroy) from VPS-family `docker commit` (and bare, which has none).

### Networking and security defaults

12. **Build firewall integration for Vultr/OVH**, or document loudly that VPS is internet-reachable as soon as it boots.
13. **Bind Docker provider `-p :22` to `127.0.0.1::22`** by default.
14. **Warn at provider load when two GCP-targeted provider names lowercase-fold to the same string.**

### Discovery and visibility

15. **Adopt OVH's `mngr <provider> list` operator-CLI pattern** uniformly: ship `mngr aws list`, `mngr azure list`, `mngr gcp list`, `mngr vultr list`. (The offline-reconstruction logic has already been lifted into `OfflineCapableVpsProvider`; Vultr/OVH would inherit stopped-host visibility automatically once they gain VM-level stop.)

### Multi-agent (shape §1.8)

16. **Pin the multi-agent path on the cloud trio.** Agent records now live in the state bucket (AWS/Azure) / GCE metadata (GCP) with no per-agent tag cap, but no release test exercises the second-agent case (see hole #11 below).

### Conventions / consolidation

17. **Standardize disk-size knob name** as `root_disk_size_gb` across the cloud trio, or document the alias.
18. **Migrate Modal tag keys from underscores to dashes** with a backward-compat read path.
19. **Add `default_idle_timeout` to `SSHProviderConfig`**, or document SSH's "always-on" policy.

### Tests (pin the above)

20. **Add `pytest_sessionfinish` orphan scanner** to Vultr/OVH conftest.
21. **Add per-provider capability-flag pinning tests** — one 4-line `test_provider_capabilities` per provider, covering both isolation shapes.
22. **Add `test_create_instance_passes_auto_shutdown_to_*`** for AWS/Azure/GCP/Vultr/OVH/Modal.
23. **Add `CleanupFailedGroup` raise-on-partial-failure tests** per provider.
24. **Promote one happy-path lifecycle test per provider from release-tier to acceptance-tier** so default CI exercises `mngr create --provider <X>`.
25. **Adopt the shared release-test trips** at `specs/provider-release-tests.md`.

---

## Open questions for the human reviewer

1. **Should `VpsProvider.supports_shutdown_hosts` default to `False`** so subclasses must explicitly opt in once they implement real VM-level stop? (AWS/GCP/Azure now do; Vultr/OVH inherit `True` but only stop the container.)
2. **Should `CleanupFailedGroup` cover create-time rollback** as well as destroy? Today the contract is destroy-only; Lima/OVH/Azure NIC-IP have create-time partial failures that bypass it.
3. **Should `supports_persistent_snapshots` exist as a separate flag** to distinguish Modal-style portable snapshots from cloud-VPS docker-commit?
4. **Vultr is the "battle-tested" precedent for VPS-family.** Should Vultr grow a managed-firewall integration, or stay "your VPS, your firewall"?
5. **Should `_validate_provider_args_for_create` move into `ProviderInstanceInterface`** so every provider must opt in or explicitly opt out?
6. **For local providers (Lima, Docker, SSH), should `--auto-shutdown-seconds` be rejected at parse time** rather than silently ignored?
7. **For SSH provider, should `mngr create --provider ssh` be rejected at config-validation time** rather than at command-execution time (then `NotImplementedError`)?
8. **Should there be a `supports_multi_agent_hosts` capability flag** so a provider that hasn't verified the N-agent path opts out cleanly?
