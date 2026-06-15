# Provider Uniformity Review

Current state of user-visible behavior across all `mngr` provider plugins. Companion to `specs/provider-shape.md` (prescriptive contract) and `specs/provider-release-tests.md` (proposed release-test trips).

**Scope.** Nine providers:
- `mngr_modal` — hosted Modal sandboxes
- `mngr_aws`, `mngr_azure`, `mngr_gcp` — cloud VMs (Debian 12 cloud-init / GCE startup-script)
- `mngr_vultr`, `mngr_ovh` — cloud VPS
- `mngr_lima` — local macOS VM
- `mngr.providers.docker` — local Docker
- `mngr.providers.ssh` — BYO host

Shared base: `libs/mngr_vps_docker/`. Interfaces: `libs/mngr/imbue/mngr/interfaces/{provider_instance,host,provider_backend}.py`.

---

## TL;DR — open findings ranked

| # | Finding | Severity | Category |
|---|---|---|---|
| 1 | Azure/GCP/Vultr/OVH `mngr stop --stop-host` silently leaks compute (container only; VM keeps billing) | high | Stop |
| 2 | Azure `auto_shutdown_seconds` halts OS but VM stays "Stopped (not deallocated)" — still bills | high | Idle/cost |
| 3 | No auto-snapshot on AWS/Azure/GCP/Vultr/OVH create — hard-crash recovery is Modal-only | high | Snapshots |
| 4 | Idle-driven self-stop only on Modal and AWS — Azure/GCP idle agents bill forever | high | Idle/cost |
| 5 | Vultr/OVH have no `pytest_sessionfinish` orphan scanner — release-test crash leaks billable VPS | high (cost) | Tests |
| 6 | AWS/GCP `ProviderUnavailableError` falls through to default "start Docker" help text (Azure curated only) | medium-high | Credentials |
| 7 | Stopped-host visibility asymmetric: Modal+AWS show stopped hosts in `mngr list`; Azure/GCP/Vultr/OVH cannot | high | Discovery |
| 8 | SSH `supports_shutdown_hosts=True` but `stop_host` raises `NotImplementedError` — user sees stack trace | medium | Capability |
| 9 | `supports_volumes=True` on VPS-Docker family but `list_volumes()` returns `[]` | medium | Capability |
| 10 | `start_host(snapshot_id=…)` silently no-ops on AWS/Azure/GCP/Vultr/OVH; `create_host(snapshot=…)` same | medium | Snapshots |
| 11 | Vultr/OVH `mngr stop --stop-host` and `mngr start` aren't implemented; release tests named for them are misleading | medium | Lifecycle |
| 12 | Vultr/OVH have no `allowed_ssh_cidrs` field — VPS is public-internet-reachable as soon as it boots | high (security) | Networking |
| 13 | OVH `mngr destroy` cancels at billing-cycle end, not immediately — VPS keeps running until expiration | medium | Lifecycle |
| 14 | Vultr `build_provider_instance` silently swallows missing-creds `ValueError`; OVH never raises `ProviderUnavailableError`; Modal raises wrong class (`PluginMngrError`) | high | Errors |
| 15 | Docker provider `-p :22` binds `0.0.0.0:<random>:22` on host's LAN | medium (security) | Networking |
| 16 | `auto_shutdown_seconds` wired through to cloud API is not pinned by any test | medium | Tests |
| 17 | GCP lowercases mngr-provider label — mixed-case provider names silently collide | medium | Discovery |
| 18 | Container-shape knobs (`--cpu`/`--memory`/`--gpu`) are Modal-only; no cross-provider "~2 vCPU/4GB" alias | medium | Create UX |
| 19 | Modal has no `mngr modal cleanup` analog (cloud trio + OVH-list all do) | low-medium | Destroy |
| 20 | Modal underscore tag keys (`mngr_host_id`) vs dash elsewhere (`mngr-host-id`) | low | Tagging |

**Headline uniform strengths.** `auto_shutdown_seconds` field name shared across the VPS family; `CleanupFailedGroup` honored uniformly at API + CLI; cloud-trio `_validate_provider_args_for_create` identical shape; Modal + AWS + Azure + GCP all have `pytest_sessionfinish` orphan scanners; `default_idle_timeout = 800 s` on 8/9 providers; `debian:bookworm-slim` container image uniform; cloud-trio `allowed_ssh_cidrs` now uniformly `("0.0.0.0/0",)` with warning (SSH is key-only — see shape doc §3.1). Live multi-agent discovery is no longer Modal-only — VPS family reads in-container agents at `libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py:1506-1565`.

---

## Lifecycle matrices

What each provider actually does for each verb, with concrete code locations.

### `mngr create`

| Provider | What happens | Cite |
|---|---|---|
| modal | Generate `HostId`; build Modal image (or load from named snapshot); create sandbox; SSH-tunnel start; write host record to Modal Volume; deploy `snapshot_and_shutdown`; start activity watcher; `on_agent_created` triggers an "initial" snapshot. | `mngr_modal/instance.py:1687-1865`, `backend.py:506-527` |
| aws | Base path; AWS contributes `_create_vps_instance` (RunInstances + SG + EBS DeleteOnTermination + IMDSv2 + spot + AMI + IAM self-stop profile), sentinel-file shutdown script, `_on_host_finalized` (systemd `.path`/`.service` self-stop units), pytest gate. | base `mngr_vps_docker/instance.py:668-763`; `mngr_aws/backend.py:185-210, 264-311, 518-564` |
| azure | Base path; Azure contributes `_create_vps_instance` (orphan NIC/IP reclaim sweep, vnet/subnet resolution, VM with cascade-delete on NIC/IP/OS-disk, cloud-init via base64 `custom_data`, spot+eviction-Delete), pytest gate. No idle watcher. | `mngr_azure/client.py:357-665`; `backend.py:89-116` |
| gcp | Base path; GCP contributes `_create_vps_instance` (CE insert + `max_run_duration` + `instance_termination_action=DELETE`, spot+same-DELETE), `_validate_provider_args_for_create` (firewall pre-flight + project warning + pytest gate). GCE startup-script bootstrap (not cloud-init). No idle watcher. | `mngr_gcp/backend.py:101-216`; `client.py:318-451` |
| vultr | Base path; Vultr contributes only `VultrVpsClient.create_instance` HTTP POST. Cloud-init `shutdown -P` wired but doesn't stop hourly billing. | `mngr_vultr/client.py:95-125`; `backend.py:54-62` |
| ovh | Overrides `_provision_vps`: pending-orders reconcile → recycle-pool claim → order-and-wait → IAM tagging → SSH-key bootstrap → `apply_host_setup_on_outer(is_qemu_purge_enabled=True)`. | `mngr_ovh/backend.py:335-574`; ordering, recycle, bootstrap modules |
| lima | Direct on `BaseProviderInstance`. Generate `lima.yaml`; btrfs additional disk if exposed; `limactl_start_new`; wait cloud-init; SSH; install shutdown script; activity watcher. Cleanup-on-fail deletes VM + orphaned disk. | `mngr_lima/instance.py:461-683` |
| docker | Direct on `BaseProviderInstance`. Build per-host image; `docker run -p :22` (binds **all interfaces**); SSH setup via `docker exec`. | `mngr/providers/docker/instance.py:862, 1090-1246` |
| ssh | `raise NotImplementedError`. | `mngr/providers/ssh/instance.py:170-182` |

### `mngr stop my-agent` (no flag)

Uniform across all nine: stops the agent's tmux session only. Never reaches `ProviderInstanceInterface.stop_host`. Agent-layer `stop_agents` path uses `collecting_cleanup_failures`.

### `mngr stop --stop-host`

| Provider | Behavior | Cost | Cite |
|---|---|---|---|
| modal | Raises `HostShutdownNotSupportedError` — `supports_shutdown_hosts=False`. | None (error before side effects). | `mngr_modal/instance.py:420-421` |
| aws | EC2 `StopInstances` with EBS preserved; `super().stop_host(stop_reason=STOPPED)` writes record. Idempotent. | Compute billing stops; EBS storage continues. | `mngr_aws/backend.py:335-365`; base `mngr_vps_docker/instance.py:1345-1400` |
| azure | **Inherited** base = container only. VM stays running. | **Silent cost leak.** | base only |
| gcp | **Inherited** = container only. GCE stays running. | **Silent cost leak.** | base only |
| vultr | **Inherited** = container only. VPS bills hourly. | **Silent cost leak (hourly).** | base only |
| ovh | **Inherited** = container only. VPS bills monthly (no proration). | Stop saves nothing this month; eventual expiry if not recycled. | base only |
| lima | `limactl stop` — real VM pause. `create_snapshot=True` parameter ignored (Lima has no snapshots). | None. | `mngr_lima/instance.py:702-737` |
| docker | `docker stop` — native container stop; state preserved. Optional `docker commit` snapshot first. | None. | `mngr/providers/docker/instance.py:1248-1296` |
| ssh | `raise NotImplementedError` **despite** `supports_shutdown_hosts=True`. CLI gate lets the call through. | User sees internal traceback. | `mngr/providers/ssh/instance.py:105-106, 184-190` |

### `mngr start`

| Provider | Behavior | Idempotent | Honors `--snapshot`? |
|---|---|---|---|
| modal | If running, return; else build sandbox from most-recent-or-named snapshot; re-attach volumes. | Yes | **Yes** (warns if running + snapshot_id) |
| aws | Locate stopped instance by `mngr-host-id` tag; `StartInstances`; rebind known_hosts with preserved host keys; remove idle sentinel; relaunch in-container activity watcher. | Yes | **No** (silent no-op) |
| azure/gcp/vultr/ovh | Inherited: `docker start`, re-exec sshd. VM was never stopped → container restart only. | Yes | **No** |
| lima | Read record; `limactl_start_existing`; fresh SSH config (port may change); restart watcher. | Yes | **No** (param accepted but unused) |
| docker | Three paths: running → return; `snapshot_id` → remove + recreate from snapshot image; else `docker start` + reinit sshd. | Yes | **Yes** |
| ssh | `raise NotImplementedError`. | n/a | n/a |

Base `start_host` now relaunches the activity watcher on resume (`libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py:1290-1321`) — fixes the idle-host re-stop bug Vultr/OVH used to hit.

### `mngr destroy`

| Provider | Destroyed | Preserved | Cite |
|---|---|---|---|
| modal | Sandbox terminated (snapshot=False); per-host agent records; host record → `DESTROYED`; host Volume deleted. | **Snapshot records preserved** for `gc_snapshots` age-gating. | `mngr_modal/instance.py:2076-2148` |
| aws | Base teardown → `TerminateInstances` → EBS auto-deleted; EC2 SSH key removed. | None AWS-specific. | base `mngr_vps_docker/instance.py:1327-1476` |
| azure | Base teardown → `begin_delete()` VM; NIC + public IP + OS disk cascade. SSH key deleted. | None Azure-specific. | base + `mngr_azure/client.py:667-677` |
| gcp | Base teardown → GCE DELETE; `auto_delete=True` on boot disk. | None GCP-specific. | base + `mngr_gcp/client.py:453-467` |
| vultr | Base teardown → `DELETE /instances/{id}`. SSH key removed. | None. | base + `mngr_vultr/client.py:127-129` |
| ovh | Base teardown → `PUT serviceInfos` with `renew.deleteAtExpiration=true`. **VPS keeps running until billing-cycle boundary; marked for cancellation, not deleted now.** May be recycled by next `mngr create`. | VPS itself, IAM tags, disk through end of billing cycle. | OVH treats destroy as "mark for cancellation" |
| lima | `limactl_delete(force=True)` + disk delete if separate. Already-gone is benign. | None. | `mngr_lima/instance.py:805-881` |
| docker | `container.remove(force=True)`; untag per-host build image; mark `DESTROYED`. | **Snapshots and host-volume directory preserved** for `gc_snapshots`. | `mngr/providers/docker/instance.py:1458-1542` |
| ssh | `raise NotImplementedError`. | n/a | `mngr/providers/ssh/instance.py:199-200` |

### `mngr <provider> cleanup`

| Provider | Exists? | Scope | Refusal |
|---|---|---|---|
| modal | **No CLI module.** | n/a | n/a |
| aws | Yes — deletes `mngr-aws` SG + `mngr-aws` self-stop IAM instance profile. | Region (SG) + account-wide (IAM). | Refuses if any mngr-tagged instance exists. |
| azure | Yes — deletes the managed RG (cascade vnet/subnet/NSG). | Per-RG (`managed-by=mngr` tag-gated). | Refuses if any mngr-tagged VM exists. |
| gcp | Yes — deletes the firewall rule. | Project. | Refuses if any tagged mngr instance exists across all zones (`aggregatedList`). |
| vultr | **No CLI module.** | n/a | n/a |
| ovh | `mngr ovh list` only (read-only). | n/a | n/a |
| lima/docker/ssh | Use global `mngr cleanup` (no per-provider verb). | n/a | n/a |

---

## `CleanupFailedGroup` adoption

Contract (`libs/mngr/imbue/mngr/interfaces/provider_instance.py`): `destroy_host` raises `CleanupFailedGroup` if any real infrastructure resource was left behind. Downstream consumers honor the new contract: `mngr/api/gc.py:421-426`, `mngr/api/cleanup.py:188-192, 244-249`, `mngr/cli/headless_runner.py:88-92`. `mngr gc` now exits with cause-specific non-zero codes (`LOCAL_STATE_REMAINS` / `HOST_RESOURCE_REMAINS` / `PROVIDER_INACCESSIBLE` / `OTHER`).

| Provider | Uses `collecting_cleanup_failures`? | Raises group for own resources? | Notes |
|---|---|---|---|
| modal | **Yes** (`mngr_modal/instance.py:2093, 2147, 2159, 2170`) | Yes — sandbox-terminate, agent-records, host-record, host-volume classified separately. | Native adopter. |
| aws/azure/gcp/vultr/ovh | No (no override) | Indirectly via base aggregation. | Azure `_reclaim_orphaned_network_resources` does NOT feed aggregation — reclaim failure logs and moves on. |
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
| `auto_shutdown_seconds` effect | sandbox timeout | terminate (no bill) | **OS halt; VM still bills** | delete (no bill) | OS halt; VPS bills | OS halt; VPS bills | n/a | n/a | n/a |
| Pytest gate | env-name pattern | yes | yes | yes | **no** | **no** | n/a | n/a | n/a |
| `pytest_sessionfinish` scanner | yes | yes | yes (+ NIC reclaim) | yes | **no** | **no** | local | local | n/a |
| Default region/zone | None (Modal chooses) | `us-east-1` | `westus` | `us-west1`/`us-west1-a` | `ewr` | `US-EAST-VA` | host | host | host |
| Default shape | `cpu=1`, `memory=1 GB` | `t3.small` | `Standard_B2s` | `e2-small` | `vc2-2c-4gb` | `vps-2025-model1` | inherits | host | n/a |
| Default OS image | `debian_slim` | Debian 12 (per-region AMI map) | Debian 12 (`debian-12:12-gen2`) | Debian 12 family (global) | Debian 12 x64 | Debian 12 - Docker | aarch64/x86_64 qcow2 | n/a | n/a |
| Default container image | n/a | `debian:bookworm-slim` | same | same | same | same | n/a | `debian:bookworm-slim` | n/a |
| Default disk | n/a | `root_volume_size_gb=30` | `os_disk_size_gb=30` | `boot_disk_size_gb=30` | bundled | bundled | `host_data_disk_size=100 GiB` | shared host volume | n/a |
| Public IP default | n/a | True | True | True | always | always | host-only | n/a | n/a |
| `supports_snapshots` | True | True (inherited) | True | True | True | True | **False** | True | **False** |
| `supports_shutdown_hosts` | **False** | True (with real EC2 stop) | True (silent: container only) | True (silent) | True (silent) | True (silent) | True (real `limactl stop`) | True (real `docker stop`) | **True (lying — raises NotImplementedError)** |
| `supports_volumes` | True | **True (lies: returns `[]`)** | **lies** | **lies** | **lies** | **lies** | True | True | **False** |
| `supports_mutable_tags` | True | **False** | False | False | False | False | True | **False** | False |

Disk-size knob name differs three ways across the cloud trio (`root_volume_size_gb` / `os_disk_size_gb` / `boot_disk_size_gb`) — all default 30 GB. Per-host SSH key is per-instance on the VPS family; Modal stores one key per profile name (shared between two named Modal instances).

---

## Tag conventions

| Concept | modal | aws | azure | gcp | vultr | ovh | docker |
|---|---|---|---|---|---|---|---|
| host id | `mngr_host_id` (underscore) | `mngr-host-id` | `mngr-host-id` | `mngr-host-id` (label, lowercased) | n/a (SSH-only discovery) | IAM v2 tag | `com.imbue.mngr.host-id` |
| provider | `mngr_user_<prefix>` | `mngr-provider` | `mngr-provider` | `mngr-provider` (label) | `mngr-provider=<name>` (flat) | IAM v2 tag | `com.imbue.mngr.provider` |
| host name | `mngr_host_name` | `Name=mngr-<name>` (cascade) | from VM name | from instance name | n/a | n/a | `com.imbue.mngr.host-name` |
| agent records | on Volume | per-field tags `mngr-agent-<id>-name`/`-type`/`-labels` (~16 cap) | n/a | n/a | n/a | n/a | `com.imbue.mngr.tags` JSON |
| Managed-by | n/a | n/a | `managed-by=mngr` on RG | n/a | n/a | n/a | n/a |

GCP `to_gce_label_value()` lowercases all values — mixed-case provider name silently collides with lowercased twin.

---

## Errors and credential classification

Contract: `ProviderUnavailableError` for "state unknown" (creds missing, API down); `ProviderEmptyError` for "state known empty" (Modal env doesn't exist yet); never silently `return []`.

| Failure | modal | aws | azure | gcp | vultr | ovh | lima | docker | ssh |
|---|---|---|---|---|---|---|---|---|---|
| Creds missing | `ProviderEmptyError` if env absent; **`ModalAuthError` (PluginMngrError — wrong class)** if token absent | `ProviderUnavailableError` (default help: "start Docker") | `ProviderUnavailableError` (**curated help**) | `ProviderUnavailableError` (default help) | **silent `[]` + WARN** | **silent `[]`** | `LimaNotInstalledError` | `ProviderUnavailableError` | n/a |
| Bad creds | `ModalAuthError` → wrapped `ProviderDiscoveryError` | `ProviderUnavailableError` | `ProviderUnavailableError` (curated) | `ProviderUnavailableError` | silent `[]` | propagates as `ProviderDiscoveryError` | `LimaVersionError` | `ProviderUnavailableError` (transport only) | n/a |
| Backend reachable, empty | n/a | normal `[]` from `list_instances` | normal `[]` | normal `[]` | normal `[]` | normal `[]` | normal `[]` | normal `[]` | n/a |

Only Azure supplies curated `user_help_text` (`_azure_unavailable_error` at `libs/mngr_azure/imbue/mngr_azure/backend.py:36-53`). AWS and GCP fall through to the default "start Docker" message (wrong advice for cloud auth failure). Modal raises `ModalAuthError`, a `PluginMngrError`, which doesn't satisfy the `ProviderUnavailable/Empty` contract — `mngr list` and `mngr gc` see a generic `ProviderDiscoveryError` instead of a clean classification.

`mngr gc` (no `--provider`) catches `ProviderUnavailableError` and logs at DEBUG only (`libs/mngr/imbue/mngr/api/providers.py:211-213`) — but `mngr gc` itself now exits with cause-specific non-zero codes on any failed sweep.

---

## Discovery and offline visibility

| State | modal | aws | azure | gcp | vultr | ovh | lima | docker | ssh |
|---|---|---|---|---|---|---|---|---|---|
| RUNNING | shown | shown | shown | shown | shown | shown | shown | shown | shown |
| STOPPED (after `--stop-host`) | shown (record on Volume) | shown (rebuilt from EC2 tags) | N/A — no override | N/A | N/A | N/A | shown | shown | N/A |
| CRASHED / unreachable | shown | shown (tag fallback) | shown if cached | same | same | same | shown | shown | **always "RUNNING"** (hard-coded) |
| DESTROYED (`--include-destroyed`) | shown | shown | shown | shown | shown | shown | shown | shown | n/a |
| Live multi-agent (in-container) | shown | shown | shown | shown | shown | shown | shown | shown | shown (over SSH) |

Live agent discovery lifted into VPS-Docker base at `libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py:1506-1565` — in-container agents are visible across the VPS family. Offline mirror still only on Modal (per-agent JSON files on state Volume) and AWS (per-agent EC2 tags up to ~16 agents). Azure/GCP/Vultr/OVH stopped hosts will silently lose agent visibility if/when VM-level stop lands.

Name resolution for `mngr exec <name>` works while stopped on Modal and AWS; fails with `HostNotFoundError` on Azure/GCP if the VM is deallocated.

---

## Snapshots

Modal auto-snapshots on every agent create (`is_snapshotted_after_create=True`, `on_agent_created` hook at `libs/mngr_modal/imbue/mngr_modal/instance.py:1858-1865`). Everyone else uses `docker commit` of the container layer on the host's own disk — survives `mngr stop` but **not** `mngr destroy`. The cloud-trio `VpsClientInterface` disk-snapshot methods were deleted entirely; user-facing `mngr snapshot create` everywhere except Modal is `docker commit`.

| Capability | modal | aws | azure | gcp | vultr | ovh | lima | docker | ssh |
|---|---|---|---|---|---|---|---|---|---|
| `supports_snapshots` | True | True | True | True | True | True | **False** | True | **False** |
| `mngr snapshot create` | `sandbox.snapshot_filesystem()` | `docker commit` | same | same | same | same | raises | `docker commit` | raises |
| Auto on create | Yes | No | No | No | No | No | n/a | No | n/a |
| `start_host(snap_id)` honored | Yes | **ignored** | ignored | ignored | ignored | ignored | ignored | Yes | n/a |
| `create_host(snapshot=)` honored | Yes | **ignored** | ignored | ignored | ignored | ignored | n/a | ignored | n/a |
| Survives `destroy_host` | Yes | No | No | No | No | No | n/a | No | n/a |

`supports_snapshots=True` means different things on Modal (persistent, portable) vs cloud-VPS (single-host docker layer, dies with VPS). No `supports_persistent_snapshots` distinction today.

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

Roughly half of in-scope user-visible behaviors have a provider-specific test pin. Top holes:

1. **Vultr/OVH `pytest_sessionfinish` orphan scanner missing.** Easy: copy `mngr_aws/conftest.py:106-134` pattern.
2. **`auto_shutdown_seconds` flow-through to cloud API.** Pre-create gate fires; no test asserts the value reaches `shutdown -P +60` in user_data on AWS/Vultr/OVH or `max_run_duration` in GCP scheduling or `customData` shutdown line on Azure.
3. **Stopped-host discovery on Azure/GCP/Vultr/OVH/Lima.** AWS pins exhaustively (`backend_test.py:231-300`); none of the others have it.
4. **Capability-flag pinning** for AWS/Azure/GCP/Vultr/OVH (Modal/Lima/Docker/SSH pin; cloud trio doesn't).
5. **Networking warning on open CIDR for GCP/Azure.** AWS pins; cloud trio's now-uniform default deserves the same warning pin.
6. **Vultr/OVH stop/start lifecycle tests** named `test_create_stop_start_destroy` but providers don't implement stop/start — name lies.
7. **Pytest gate missing on Modal/Vultr/OVH/Lima.** Copy the AWS/Azure/GCP pattern.
8. **Cross-region/zone refusal on Vultr/OVH.** AWS/Azure/GCP pin; the other two don't.
9. **SSH credentials error classification.** Empty config should raise `ProviderEmptyError`; not pinned.
10. **`CleanupFailedGroup` raise-on-partial-failure tests.** Zero hits across all providers' `_test.py` files. Belongs in unit tests with mocked destroy helpers.
11. **Multi-agent (shape §1.8) release-test coverage.** No provider pins the second-agent case; see release-tests Trip 1b.

Test-style differences worth aligning: AWS uses `botocore.Stubber`; Azure hand-rolled mocks; GCP `mock_compute_v1`; Vultr `respx`; OVH custom client stub. Per-provider release-test gate env vars also diverge (`MNGR_AWS_RELEASE_TESTS=1` etc. for the cloud trio; Vultr/OVH gate on credential presence). Lifecycle release-test name (`test_provider_lifecycle_create_exec_and_destroy`) is uniform across the cloud trio — preserve this when consolidating into the proposed shared trip harness.

---

## Recommendations

Ordered by impact × ease.

### Single-line correctness fixes

1. **Flip SSH `supports_shutdown_hosts` to `False`.** `mngr/providers/ssh/instance.py:104-106`.
2. **Bump `mngr gc` log level on `ProviderUnavailableError`** from DEBUG to WARNING (`libs/mngr/imbue/mngr/api/providers.py:211-213`).
3. **Override `supports_volumes` to `False` on the VPS-Docker family** until `list_volumes`/`delete_volume` actually work.

### Curated `user_help_text` cleanup

4. **Add `_aws_unavailable_error` and `_gcp_unavailable_error`** mirroring `_azure_unavailable_error`. Or hoist into `ProviderUnavailableError` as a per-backend hook.
5. **Make Modal `ModalAuthError` raise `ProviderUnavailableError`** so it joins the contract.
6. **Replace Vultr/OVH silent-empty-on-missing-creds with `ProviderUnavailableError`.**

### Lifecycle and cost safety

7. **Override `stop_host` on Azure/GCP/Vultr** to actually stop the VM — OR set `supports_shutdown_hosts = False` until that lands.
8. **Implement Azure managed-identity self-delete** so `auto_shutdown_seconds` actually stops billing on Azure.
9. **Add `pytest_sessionfinish` orphan scanner to Vultr/OVH.** OVH is high-cost (monthly billing).
10. **Override `_validate_provider_args_for_create` on Vultr/OVH** to require `auto_shutdown_seconds` in pytest.
11. **Port AWS idle watcher (sentinel + systemd `.path` unit)** to GCP and Azure.

### Snapshots and capability honesty

12. **Honor `start_host(snapshot_id=…)` and `create_host(snapshot=…)` on the VPS family, or raise `SnapshotsNotSupportedError`.** Silent no-op is the worst option.
13. **Add a `supports_persistent_snapshots` flag** to honestly distinguish Modal (snapshots survive destroy) from VPS-family `docker commit`.

### Networking and security defaults

14. **Build firewall integration for Vultr/OVH**, or document loudly that VPS is internet-reachable as soon as it boots.
15. **Bind Docker provider `-p :22` to `127.0.0.1::22`** by default.
16. **Warn at provider load when two GCP-targeted provider names lowercase-fold to the same string.**

### Discovery and visibility

17. **Lift AWS's `_discovered_host_from_tags` + `_offline_host_from_tags` + `discover_hosts_and_agents` triad into `VpsDockerProvider`** as overridable hooks. Future VM-level stop on Azure/GCP/Vultr/OVH inherits stopped-host visibility automatically.
18. **Adopt OVH's `mngr <provider> list` operator-CLI pattern** uniformly: ship `mngr aws list`, `mngr azure list`, `mngr gcp list`, `mngr vultr list`.

### Multi-agent (shape §1.8)

19. **Lift the AWS per-field tag-mirror pattern into VPS base** so Azure/GCP/Vultr/OVH grow offline mirror with the same shape (with the per-cloud metadata limit documented).
20. **Document the AWS ~16-agent cap** in the AWS README and surface it in `mngr exec --new-agent` before the cap is hit.

### Conventions / consolidation

21. **Standardize disk-size knob name** as `root_disk_size_gb` across the cloud trio, or document the alias.
22. **Migrate Modal tag keys from underscores to dashes** with a backward-compat read path.
23. **Add `default_idle_timeout` to `SSHProviderConfig`**, or document SSH's "always-on" policy.

### Tests (pin the above)

24. **Add `pytest_sessionfinish` orphan scanner** to Vultr/OVH conftest.
25. **Add per-provider capability-flag pinning tests** — one 4-line `test_provider_capabilities` per provider.
26. **Add `test_create_instance_passes_auto_shutdown_to_*`** for AWS/Azure/GCP/Vultr/OVH/Modal.
27. **Add `CleanupFailedGroup` raise-on-partial-failure tests** per provider.
28. **Promote one happy-path lifecycle test per provider from release-tier to acceptance-tier** so default CI exercises `mngr create --provider <X>`.
29. **Adopt the shared release-test trips** at `specs/provider-release-tests.md`.

---

## Open questions for the human reviewer

1. **Should `VpsDockerProvider.supports_shutdown_hosts` default to `False`** so subclasses must explicitly opt in once they implement real VM-level stop?
2. **Should `CleanupFailedGroup` cover create-time rollback** as well as destroy? Today the contract is destroy-only; Lima/OVH/Azure NIC-IP have create-time partial failures that bypass it.
3. **Should `supports_persistent_snapshots` exist as a separate flag** to distinguish Modal-style portable snapshots from cloud-VPS docker-commit?
4. **Vultr is the "battle-tested" precedent for VPS-family.** Should Vultr grow a managed-firewall integration, or stay "your VPS, your firewall"?
5. **Should `_validate_provider_args_for_create` move into `ProviderInstanceInterface`** so every provider must opt in or explicitly opt out?
6. **For local providers (Lima, Docker, SSH), should `--auto-shutdown-seconds` be rejected at parse time** rather than silently ignored?
7. **For SSH provider, should `mngr create --provider ssh` be rejected at config-validation time** rather than at command-execution time (then `NotImplementedError`)?
8. **Should there be a `supports_multi_agent_hosts` capability flag** so a provider that hasn't verified the N-agent path opts out cleanly?
