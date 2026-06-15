# Provider Uniformity Review (Round 2) -- Lifecycle: Create / Stop / Start / Destroy / Cleanup

**Scope.** All nine `mngr` provider plugins on branch `mngr/reviewer-providers` after the 2026-06-11 baseline review, and after the recent merges of `mngr/separate-snapshots`, `mngr/fix-discovery-provider`, `mngr/azure`, and `mngr/gcp` into `ev/main`.

Providers reviewed at equal depth:
- `mngr_modal` -- hosted sandbox
- `mngr_aws` -- new cloud VM
- `mngr_azure` -- new cloud VM
- `mngr_gcp` -- new cloud VM
- `mngr_vultr` -- battle-tested cloud VPS
- `mngr_ovh` -- battle-tested cloud VPS (monthly billing)
- `mngr_lima` -- local macOS VM
- `mngr/providers/docker` -- local Docker
- `mngr/providers/ssh` -- BYO host

This round is a focused re-pass on **lifecycle**: what `mngr create` actually does, what `mngr stop` (with and without `--stop-host`) does, what `mngr start` does, what `mngr destroy` actually destroys, what region/account `cleanup` does, how `CleanupFailedGroup` adoption changes the user-visible partial-failure picture, and where idle-driven self-stop exists.

---

## 1. One-paragraph summary -- has lifecycle converged?

**Partially.** Three things meaningfully changed since 2026-06-11: (a) `auto_shutdown_minutes` is now `auto_shutdown_seconds` across the whole VPS-Docker family (`libs/mngr_vps_docker/CHANGELOG.md:13`, used at `mngr_vps_docker/instance.py:1684`); (b) `CleanupFailedGroup` is now the *contract* for `destroy_host` in the interface (`libs/mngr/imbue/mngr/interfaces/provider_instance.py:404`) and has been adopted by Modal, VPS-Docker, Lima, and Docker; (c) Azure no longer raises a bare `ValueError` for unresolvable subscription -- it raises `AzureSubscriptionError` (`libs/mngr_azure/imbue/mngr_azure/errors.py:12`, `config.py:224`), preserving `ProviderUnavailableError` wrapping. But the *core* asymmetries in user-visible lifecycle behavior are intact: `mngr stop --stop-host` still silently leaks compute on Azure/GCP/Vultr/OVH (no override, so the inherited `VpsDockerProvider.stop_host` only halts the container); idle-driven self-stop is still only on Modal and AWS; Lima and Docker stop/start are still the only "honest, native, no caveats" stop on the chart; SSH still lies about its capability flag. The newly-adopted `CleanupFailedGroup` is honored downstream (`mngr/api/gc.py:421-426`, `mngr/api/cleanup.py:188-192`, `mngr/cli/headless_runner.py:88-92`) but has only been *plumbed into provider code* on three providers -- Modal, VPS-Docker (the base), and Lima/Docker -- so the cloud VPS providers benefit from base aggregation but cannot record their own provider-specific cleanup failures. So: the contract got tighter, the rename happened, and Azure's error gained structure -- but cost-leak `--stop-host`, missing idle watchers on Azure/GCP/Vultr/OVH, and the SSH capability lie are all still present.

---

## 2. Lifecycle matrix

Cells contain user-visible behavior plus the *concrete code location* (override point or inherited-from). Where a method is inherited, I cite the base location, not the empty subclass slot.

### 2a. `mngr create my-agent` end-to-end

| Provider | What actually happens | Cite |
|---|---|---|
| modal | Generate `HostId`; build Modal image (or load from named snapshot); create sandbox with timeout buffer + volumes; SSH-tunnel start; write host record to a Modal Volume; deploy `snapshot_and_shutdown`; start activity watcher; `on_agent_created` triggers an "initial" snapshot if `is_snapshotted_after_create=True`. | `mngr_modal/instance.py:1687-1856`, `:1858-1865` (auto-snapshot hook), `backend.py:506-527` (`bootstrap_for_host_creation` for per-user env) |
| aws | Inherited base path; AWS contributes `_create_vps_instance` (RunInstances with SG, EBS `DeleteOnTermination=True`, IMDSv2 hardening, IMDS-hop-1, `InitiatedShutdownBehavior=terminate`, optional spot, AMI override, IAM instance profile for self-stop), `_create_shutdown_script` (sentinel-file write instead of `kill -TERM 1`), `_on_host_finalized` (install systemd `.path` + `.service` self-stop units), pytest validation in `_validate_provider_args_for_create`. | base `mngr_vps_docker/instance.py:668-763`; AWS overrides `mngr_aws/backend.py:264-311`, `:518-536`, `:538-564`, `:185-210`; client `mngr_aws/client.py:542-689` |
| azure | Inherited base path; Azure contributes `_create_vps_instance` (orphaned NIC/IP reclaim sweep, vnet/subnet resolution, VM with `delete_option=Delete` on NIC/IP/OS-disk for cascade, cloud-init via base64 `custom_data`, optional spot with eviction-Delete), pytest gate. No idle watcher install. | base `mngr_vps_docker/instance.py:668-763`; Azure `client.py:357-434`, `:457-492`, `:494-556`; orphan reclaim `client.py:620-665`; pytest gate `backend.py:89-116` |
| gcp | Inherited base path; GCP contributes `_create_vps_instance` (Compute Engine insert with `max_run_duration` + `instance_termination_action=DELETE`, optional spot with same DELETE termination), `_validate_provider_args_for_create` (firewall pre-flight via `resolve_firewall()`, project resolution warning, pytest gate). No idle watcher. | base `mngr_vps_docker/instance.py:668-763`; GCP `backend.py:185-216`, `:101-149`; client `mngr_gcp/client.py:318-451`, especially `:429-438` for auto-shutdown wiring |
| vultr | Inherited base path; Vultr contributes only the `VultrVpsClient.create_instance` HTTP POST. Cloud-init `shutdown -P` is wired but as the base config docs explicitly note (config docs:64-66 of base), OS-level halt does not stop hourly Vultr billing. | base `mngr_vps_docker/instance.py:668-763`; Vultr `client.py:95-125`; build-args `backend.py:54-62` |
| ovh | OvhProvider **overrides `_provision_vps`** (the only non-Modal/non-Lima provider with a deep create-time override): pending-orders reconcile -> recycle-pool claim -> order-and-wait (async pipeline with `OvhOrderDeliveryTimeoutError` -> pending-order marker) -> IAM tagging -> rebuild with SSH key -> root-key bootstrap -> `apply_host_setup_on_outer(is_qemu_purge_enabled=True)`. | OVH `backend.py:335-574`; ordering `ordering.py:17-100`; pending markers `pending_orders.py:80-119`; recycle `recycle.py:79-158`; bootstrap `bootstrap.py:47-202` |
| lima | `LimaProviderInstance` lives directly on `BaseProviderInstance`. Generate `lima.yaml`; pre-create btrfs additional disk if `is_host_data_volume_exposed=False`; `limactl_start_new` with timeout; wait cloud-init; SSH; build `Host`; install shutdown script; start activity watcher. On error, `_cleanup_failed_lima_instance` deletes the VM + the orphaned disk. | `mngr_lima/instance.py:461-683`; cleanup-on-fail `:581-600` |
| docker | `DockerProviderInstance` on `BaseProviderInstance`. Build per-host image (unless `--image`), `docker run` with `-p :<22>` (binds **all interfaces** on the host), SSH setup via `docker exec`. Failed host records persisted. | `mngr/providers/docker/instance.py:1090-1246`; port binding `:862` |
| ssh | `raise NotImplementedError("SSH provider does not support creating hosts")`. | `mngr/providers/ssh/instance.py:170-182` |

### 2b. `mngr stop my-agent` (no flag) -- agent-only stop

Uniform across all nine: this is a tmux-level operation in the API layer, not a provider-instance method. Every provider sees the same flow because `mngr stop` (no flag) never reaches `ProviderInstanceInterface.stop_host`. **This part is fully symmetric.** Inherited via the agent-layer `stop_agents` path (which itself uses `collecting_cleanup_failures` -- `libs/mngr/imbue/mngr/api/cleanup.py:244-249`).

### 2c. `mngr stop --stop-host my-agent` -- host stop

| Provider | Behavior | Cost implication | Cite |
|---|---|---|---|
| modal | Raises `HostShutdownNotSupportedError` because `supports_shutdown_hosts = False`. | None -- error before any side effects. | `mngr_modal/instance.py:420-421`; gate `mngr/cli/stop.py:70-73`; error `mngr/errors.py` |
| aws | EC2 `StopInstances` with EBS preservation; calls `super().stop_host(..., stop_reason=STOPPED)`; base writes `stop_reason` in a single record write (commit `dec33516a` folded the AWS-only `_record_stop_reason` into the base param). Idempotent via `_wait_for_instance_state`. | Compute billing stops; EBS storage billing continues. (User-visible: correct.) | `mngr_aws/backend.py:335-365`; base `mngr_vps_docker/instance.py:1345-1400` |
| azure | **Inherited** `VpsDockerProvider.stop_host` -> halts only the Docker container; the Azure VM stays running. | **Silent cost leak: VM bills indefinitely** (compute + storage). | base `mngr_vps_docker/instance.py:1250-1284`; Azure has no override |
| gcp | **Inherited** -- same as Azure. Container only; GCE instance stays running. | **Silent cost leak.** | base `mngr_vps_docker/instance.py:1250-1284`; GCP has no override |
| vultr | **Inherited** -- same. Container only; Vultr VPS keeps billing hourly. | **Silent cost leak** (hourly meter). | base `mngr_vps_docker/instance.py:1250-1284`; Vultr has no override |
| ovh | **Inherited** -- same. Container only; OVH VPS continues to be paid for the month. | Cost leak only matters at month boundary because OVH bills monthly with no proration; the VPS will *expire* if nothing recycles it. Still: a user expecting "stop = saves money" sees no immediate effect. | base `mngr_vps_docker/instance.py:1250-1284`; OVH has no override |
| lima | `limactl stop` -- a **real VM pause**. `stop_reason=STOPPED` written. The `create_snapshot=True` parameter is **ignored** (Lima has no snapshots; param is vestigial). | None (local). | `mngr_lima/instance.py:702-737` |
| docker | `docker stop` -- native container stop with state preserved. Optional snapshot via `docker commit` first. | None (local). | `mngr/providers/docker/instance.py:1248-1296` |
| ssh | `raise NotImplementedError("SSH provider does not support stopping hosts")` **despite** `supports_shutdown_hosts = True`. The gate at `mngr/cli/stop.py:72-73` lets the call through because the flag lies, then the call explodes inside the provider. | None (SSH owns no infra) but the error path is broken -- user gets an internal traceback, not a clean `HostShutdownNotSupportedError`. | `mngr/providers/ssh/instance.py:105-106` (flag), `:184-190` (raise) |

### 2d. `mngr start my-agent` -- host start

| Provider | Behavior | Idempotent | Honors `--snapshot`? | Cite |
|---|---|---|---|---|
| modal | If sandbox still running, return existing (idempotent). Else read host record, pick most-recent snapshot (or named), build sandbox from snapshot image, re-attach volumes, re-do SSH setup. | Yes | **Yes** (and warns if running + snapshot_id given) | `mngr_modal/instance.py:1929-2073`, idempotency `:1952-1962`, warning `:1956-1961` |
| aws | Locate stopped instance by `mngr-host-id` tag; `StartInstances` and wait `running`; clear cached IP; wait SSH on new IP; `_rebind_known_hosts` with preserved EBS-stored host keys; remove idle sentinel; update `vps_ip` in record; re-launch in-container activity watcher. | Yes (waits for terminal state) | **No** (silent no-op -- `snapshot_id` parameter ignored) | `mngr_aws/backend.py:367-424`; rebind `:488-512`; relaunch `:426-450` |
| azure | Inherited `VpsDockerProvider.start_host`: `docker start` the container, re-exec sshd. VM was never stopped so this is a container restart only. | Yes | **No** (silent no-op) | base `mngr_vps_docker/instance.py:1290-1321` |
| gcp | Same as Azure (inherited). | Yes | **No** | base `mngr_vps_docker/instance.py:1290-1321` |
| vultr | Same as Azure (inherited). | Yes | **No** | base `mngr_vps_docker/instance.py:1290-1321` |
| ovh | Same as Azure (inherited). | Yes | **No** | base `mngr_vps_docker/instance.py:1290-1321` |
| lima | Read host record; `limactl_start_existing`; fresh SSH config (port may change); restart activity watcher; clear `stop_reason`. | Yes | **No** (parameter accepted but unused) | `mngr_lima/instance.py:738-803` |
| docker | Three paths: container running -> return; `snapshot_id` -> remove old container + create new from snapshot image (`_start_from_snapshot`); otherwise `docker start` + reinit sshd. | Yes | **Yes** | `mngr/providers/docker/instance.py:1298-1379`, snapshot path `:1335`, idempotency `:1314-1323` |
| ssh | `raise NotImplementedError`. | n/a | n/a | `mngr/providers/ssh/instance.py:192-197` |

### 2e. `mngr destroy my-agent`

| Provider | What's destroyed | What's preserved | Cite |
|---|---|---|---|
| modal | Sandbox terminated (snapshot=False); per-host agent records (`/hosts/<id>/` cleared); host record set to `stop_reason=DESTROYED`; host Volume deleted if enabled. | **Snapshot records intentionally preserved** for `gc_snapshots` age-gating. | `mngr_modal/instance.py:2076-2148`; preservation docstring `:2079-2083`; delete-host volume `:2143-2148` |
| aws | Inherited base teardown -> `TerminateInstances` -> EBS auto-deleted via `DeleteOnTermination=True` set at create; SSH key from EC2 removed. | Local known_hosts (cosmetic) cleaned. AWS doesn't override destroy; no AWS-specific resource leaks because no per-host shared infra. | base `mngr_vps_docker/instance.py:1327-1476`; AWS contributes only `client.py:757-760` for `TerminateInstances` |
| azure | Inherited base teardown -> `begin_delete()` VM; NIC + public IP + OS disk cascade via `delete_option=Delete` at create. SSH key deleted. | None Azure-specific. | base `mngr_vps_docker/instance.py:1327-1476`; cascade documentation `mngr_azure/client.py:667-677` |
| gcp | Inherited base teardown -> GCE DELETE on instance; `auto_delete=True` on boot disk handles disk. | None GCP-specific. | base `mngr_vps_docker/instance.py:1327-1476`; GCP `client.py:453-467` |
| vultr | Inherited base teardown -> `DELETE /instances/{id}`. SSH key removed. | None. | base `mngr_vps_docker/instance.py:1327-1476`; Vultr `client.py:127-129` |
| ovh | Inherited base teardown -> `PUT /vps/{serviceName}/serviceInfos` with `renew.deleteAtExpiration=true`. **VPS keeps running until the billing-cycle boundary; it is *marked* for cancellation, not deleted now.** Subsequent `mngr create` may recycle it. | The VPS itself, the IAM tags, and the disk persist for the rest of the billing cycle. | base `mngr_vps_docker/instance.py:1327-1476`; OVH treats destroy as "mark for cancellation" |
| lima | `limactl_delete(force=True)`; `limactl_disk_delete(force=True)` if a separate data disk exists; host record `stop_reason=DESTROYED`. Errors classified via `_is_lima_not_found_error` so already-gone is benign. | None. | `mngr_lima/instance.py:805-881` |
| docker | `stop_host` (no snapshot) -> `container.remove(force=True)` -> untag per-host build image -> mark record DESTROYED. | **Snapshots and host-volume directory preserved** for `gc_snapshots`. | `mngr/providers/docker/instance.py:1458-1542` |
| ssh | `raise NotImplementedError`. | n/a | `mngr/providers/ssh/instance.py:199-200` |

### 2f. `mngr <provider> cleanup` -- region/account-wide

| Provider | Command exists? | Scope | Refusal semantics | Cite |
|---|---|---|---|---|
| modal | **No CLI module at all** (`ls mngr_modal/` has no `cli.py`). | n/a | n/a | absence confirmed by directory listing |
| aws | Yes -- `mngr aws cleanup`: deletes auto-created `mngr-aws` SG and the `mngr-aws` IAM self-stop instance profile. | Per-region (the SG is region-scoped) + account-wide for IAM. | Lists mngr-managed instances via tag filter and refuses with `ClickException` if any exist. | `mngr_aws/cli.py:253-335`, refuse `:138-160` |
| azure | Yes -- `mngr azure cleanup`: deletes the managed resource group (cascading vnet/subnet/NSG). | Per-RG (`managed-by=mngr` tag-gated). | Lists tag-filtered VMs and refuses with `ClickException` if any exist. Checks RG tag before delete. | `mngr_azure/cli.py:143-194`, refuse `:60-77`; tag-gated delete `client.py:758-785` |
| gcp | Yes -- `mngr gcp cleanup`: deletes firewall rule. | Project-wide (firewall is project-level). | Refuses if any tagged mngr instances exist via `aggregatedList`. | `mngr_gcp/cli.py:172-230`, refuse `:71-78`; scope `client.py:539-552` |
| vultr | **No CLI module at all** (`ls mngr_vultr/` has no `cli.py`). | n/a | n/a | absence confirmed |
| ovh | **`mngr ovh list` only**, no `mngr ovh cleanup`. | List shows SERVICENAME / PLAN / DATACENTER / STATE / EXPIRATION / CANCEL? / mngr tags / recycling-by. | n/a (read-only). | `mngr_ovh/cli.py:33-77`; no `cleanup` group anywhere in the file (grep showed only `list_command`) |
| lima | No `mngr lima cleanup`. Uses global `mngr cleanup` like ssh / docker. | n/a | n/a | absence confirmed |
| docker | No `mngr docker cleanup`. Uses global `mngr cleanup`. | n/a | n/a | absence confirmed |
| ssh | No `mngr ssh cleanup`. | n/a | n/a | absence confirmed |

---

## 3. `CleanupFailedGroup` adoption matrix

The interface contract (`libs/mngr/imbue/mngr/interfaces/provider_instance.py:404`) now states that `destroy_host` "raises `CleanupFailedGroup` if any real infrastructure resource was left behind." This is a tightening: the prior behavior was to log warnings or return without signal. Downstream consumers honor the new contract: `mngr/api/gc.py:421-426`, `mngr/api/cleanup.py:188-192` and `:244-249`, `mngr/cli/headless_runner.py:88-92`, `mngr/api/create.py:87-89` (the "create failed, rollback also leaked" case).

| Provider | Uses `collecting_cleanup_failures` directly? | Raises `CleanupFailedGroup` for own resources? | What user sees on partial failure |
|---|---|---|---|
| modal | **Yes** -- `mngr_modal/instance.py:2093, 2147, 2159, 2170` (destroy_host and `gc_snapshots`) | Yes -- sandbox-terminate, agent-records-removal, host-record write, host-volume delete each classified separately | `CleanupFailedGroup` with one or more `[HOST_RESOURCE_REMAINS]` or `[OTHER]` leaves; exit code via `headless_runner.py:88-92` |
| aws | **No** (no override) -- inherits base aggregation | Indirectly: base's `collecting_cleanup_failures` aggregates `TerminateInstances` and `delete_ssh_key` failures | Base-aggregated group; no AWS-specific record types |
| azure | **No** -- inherits base | Indirectly via base | Same as AWS; no Azure-specific failure record (notably: the `_reclaim_orphaned_network_resources` cleanup sweep at `client.py:620-665` does not feed into the aggregation -- if reclaim fails, it logs and moves on) |
| gcp | **No** -- inherits base | Indirectly via base | Same as AWS |
| vultr | **No** -- inherits base | Indirectly via base | Same as AWS |
| ovh | **No** -- inherits base | Indirectly via base. The OVH-specific recycle / pending-orders cleanup happens in `_provision_vps` create-time error path, not `destroy_host`, so failures there raise immediately as `MngrError` rather than aggregating | Base group from `destroy_host`; OVH create-time orphans (`_terminate_orphaned_fresh_order`, `backend.py:576-596`) raise outside the aggregation |
| lima | **Yes** -- `mngr_lima/instance.py:817-881` | Yes -- VM delete, data-disk delete, and host-record write each classified separately (`HOST_RESOURCE_REMAINS` vs `OTHER`) | Per-step group |
| docker | **Yes** -- `mngr/providers/docker/instance.py:1476` | Yes -- container remove, image untag, host record DESTROYED-mark each classified; benign `docker.errors.NotFound` filtered out | Per-step group |
| ssh | **No** -- `destroy_host` raises `NotImplementedError`, never reaches aggregation | n/a | n/a |
| vps_docker (base) | **Yes** -- `instance.py:1356` | Yes -- container remove, btrfs subvolume delete, named volume remove, snapshot-trigger volume remove, `vps_client.destroy_instance`, `vps_client.delete_ssh_key` each appended as `CleanupFailure(category=HOST_RESOURCE_REMAINS)` when non-benign | Per-step group, surfaced to all VPS subclasses |

**Bottom line:** `CleanupFailedGroup` adoption is *good* for users of Modal, Docker, Lima, and AWS/Azure/GCP/Vultr/OVH (via base inheritance). It is *unused* in SSH (because SSH owns no infra). The notable gap is that **provider-specific create-time partial-failure cleanup** (Azure's NIC/IP reclaim, OVH's recycle-lock-release, OVH's pending-order marker write) is *not* funneled through `CleanupFailedGroup` -- it raises `MngrError` or just logs. So a user who sees "creation failed; please check for orphaned resources" cannot tell from the exit type whether the rollback itself left anything behind.

---

## 4. Findings extending or contradicting the prior review

Severity in `{high, medium, low}`; flagged `[NEW]` if new since 2026-06-11, `[CONFIRMED]` if the prior review's finding still holds verbatim, `[REFINED]` if the wording needs to change.

### F-L-1 [CONFIRMED] (high, cost) -- Azure / GCP / Vultr `--stop-host` silently leaks compute
Verified: none of these providers override `stop_host`, and the base only stops the container (`mngr_vps_docker/instance.py:1250-1284`). The CLI does not gate this -- there is no `supports_native_host_stop` distinction, so `supports_shutdown_hosts=True` is interpreted as "yes, you can `--stop-host`" even though the underlying VM/VPS keeps billing. The prior review flagged this for Azure/GCP only; this round confirms it equally applies to Vultr (hourly meter keeps running).

### F-L-2 [REFINED] (high, cost) -- OVH `--stop-host` cost picture is different and should be documented separately
OVH monthly billing with no proration means `--stop-host` is not a *daily* cost leak the way Vultr/Azure/GCP are. But it is a *user-expectation* leak: the user typed `mngr stop --stop-host` and the VPS continues to consume its monthly slot, and the *next* `mngr create` may even recycle this VPS rather than reusing the stopped one. Prior review undervalued this: it's not "no cost leak", it's "different cost model -- but `mngr start` after `mngr stop --stop-host` will not actually resume the same VPS because the container restart never happens on a different IP and the recycle path may interpose."

### F-L-3 [NEW] (medium-high) -- Lima `stop_host(create_snapshot=...)` parameter is vestigial
`mngr_lima/instance.py:702-737` accepts the `create_snapshot` parameter but never reads it. Lima has no snapshot support (`supports_snapshots = False` at `:138-139`, enforced by `SnapshotsNotSupportedError` at `:1095-1100`). Callers that pass `create_snapshot=True` (the default!) silently get no snapshot. *Fix:* drop the parameter or raise `SnapshotsNotSupportedError` if `create_snapshot=True` is passed explicitly.

### F-L-4 [NEW] (medium-high) -- AWS `start_host(snapshot_id=...)` is documented in the interface but silently no-ops
The interface (`provider_instance.py:386-391`) says `start_host` "optionally restoring from a specific snapshot." AWS' override (`mngr_aws/backend.py:367-424`) accepts the parameter but never reads it -- the body locates the instance by `mngr-host-id` tag and `StartInstances`, with no branch that uses `snapshot_id`. Same is true for the inherited base on Azure/GCP/Vultr/OVH and for Lima. The prior review flagged this for AWS/Azure/GCP; this round extends to Lima and re-emphasizes for Vultr/OVH.

### F-L-5 [CONFIRMED] (medium) -- SSH `supports_shutdown_hosts = True` is a lie
`mngr/providers/ssh/instance.py:105-106` returns True; `:184-190` raises `NotImplementedError`. The CLI gate at `mngr/cli/stop.py:70-73` does the wrong thing: it sees `True` and lets `stop_host` be called, which then raises `NotImplementedError` instead of the cleaner `HostShutdownNotSupportedError`. *Fix:* return `False`.

### F-L-6 [NEW] (medium) -- OVH `destroy_host` semantics are "cancel at expiration", not "destroy now"
`mngr/cli/destroy.py` users expect `mngr destroy` to terminate things now. For OVH, inherited base `destroy_host` calls `vps_client.destroy_instance` which on OVH translates to `PUT /vps/.../serviceInfos` with `renew.deleteAtExpiration=true`. The VPS keeps running until end of month; recycle path on next `mngr create` may pick it back up. This is intentional behavior (lets the recycle pool work) but should be **documented in the destroy CLI help text for OVH**, because it's the only provider where `destroy` does not actually deprovision.

### F-L-7 [NEW] (medium) -- Azure orphan NIC/IP reclaim is best-effort and silent on failure
`mngr_azure/client.py:620-665` runs as part of `create_instance` (called at `:393`), sweeps unattached NIC/IP older than 240s. If reclaim fails (e.g., RBAC missing for delete on Network resources), the sweep logs at warning level and continues -- the create still proceeds. This is the right product behavior, but it means a user can rack up orphaned NICs/IPs that never get cleaned because the cleanup is never escalated. *Fix:* expose reclaim failures to `mngr azure cleanup` (or `mngr <provider> list` per round-1 F-OTHER-3). Currently the only escalation path is the pytest sessionfinish scanner at `conftest.py:122-158`, which only runs during tests.

### F-L-8 [CONFIRMED] (high, cost) -- Idle self-stop only on Modal + AWS
Verified: Azure / GCP / Vultr / OVH have no in-VM idle watcher. Azure's `auto_shutdown_seconds` halts the OS via `shutdown -P +N` but does not deallocate. GCP uses `max_run_duration + instance_termination_action=DELETE` which *will* delete the VM at the cap -- this is a "max lifetime" not an idle-driven self-stop. Vultr/OVH: cloud-init halts OS, billing continues.

### F-L-9 [NEW] (medium) -- `auto_shutdown_seconds` semantics differ by provider but the field is shared
The base `VpsDockerProviderConfig.auto_shutdown_seconds` (`mngr_vps_docker`, see `CHANGELOG.md:13`) plumbed through `_get_effective_auto_shutdown_seconds` (`instance.py:1684-1691`) and into cloud-init. Concrete result:
- **AWS:** terminate at cap (because `InitiatedShutdownBehavior=terminate`).
- **GCP:** delete at cap (via `instance_termination_action=DELETE`), independent of the cloud-init `shutdown -P`.
- **Azure:** OS halt only (no deallocate); VM bills until destroyed.
- **Vultr/OVH:** OS halt only; VPS bills.

Same field, four different cost outcomes. Prior round captured this for Azure; this round confirms the *uniform field across five providers* makes the asymmetry worse, not better.

### F-L-10 [NEW] (medium) -- `_provision_vps` is now a deep extension point for OVH only
The base's `_provision_vps` hook exists for subclasses to plug in. OVH is the only subclass that actually overrides it (`backend.py:335-574`), to handle the ordering pipeline and recycle pool. AWS/Azure/GCP/Vultr extend the *inner* `_create_vps_instance` (called from base `_provision_vps`) but leave `_provision_vps` itself alone. If any future provider has comparable async / pending-order pipelines (OVH-style), they'll need to follow OVH's lead. The current call graph is consistent but undocumented in the interface.

### F-L-11 [NEW] (low-medium) -- Modal lacks a `cleanup` command; no operator inspection across providers
Same as prior round (F-DESTROY-1) but worth restating in lifecycle terms: there is no provider-agnostic way to ask "what mngr-tagged resources exist that I forgot about?" except by running pytest sessionfinish hooks. The prior review's suggestion to lift OVH's `mngr ovh list` pattern to all providers remains the clean fix and was not adopted.

### F-L-12 [CONTRADICTS] (low-medium) -- "`mngr destroy` semantics are uniform from CLI surface" is mostly true but `--include-destroyed` interaction with OVH is unique
Prior review (section 4) said the CLI surface is uniform. Verified for `--force`, `--dry-run`, `--gc`, `--remove-created-branch`. But the OVH "destroy = cancel at expiration" semantics interact with `mngr list --include-destroyed`: an OVH host marked DESTROYED is *still actually running on OVH's side* until end of month, so the listing's "DESTROYED" badge gives the user incorrect intuition. (Round 1 didn't tease this apart because it lumped OVH into VPS-Docker generic.)

### F-L-13 [NEW] (medium) -- `CleanupFailedGroup` does not capture cross-provider partial-failure scenarios that bypass `destroy_host`
- Azure's `_reclaim_orphaned_network_resources` sweep failures (`client.py:620-665`) are not aggregated. They log and pass.
- OVH's `_terminate_orphaned_fresh_order` (`backend.py:576-596`) raises `MngrError` directly, not `CleanupFailedGroup`.
- OVH's pending-order marker write happens *during the create's raising path*, not via cleanup. The pending order is then reconciled on next `mngr create`. Reconciliation failures log but do not raise `CleanupFailedGroup` either.

These are not bugs *per se* -- the contract is that `destroy_host` raises the group -- but the broader user-visible "did my create rollback leak something?" question doesn't have a uniform answer.

### F-L-14 [CONFIRMED] (medium) -- Vultr / OVH have no pytest `sessionfinish` orphan scanner
Verified: `mngr_vultr/` has no `conftest.py` with a scanner; `mngr_ovh/conftest.py` only registers the shared common hooks. Compare AWS `conftest.py:134-183`, Azure `conftest.py:175-223`, GCP `conftest.py:122-163`, Modal `conftest.py:619-687`. Lima has none -- justified for a local-VM provider with no billing -- but Vultr/OVH remain at risk of real-money leaks from killed release tests.

### F-L-15 [NEW] (low) -- Lima's `host_data_disk_name` "already gone" handling is correct, but `_cleanup_failed_lima_instance` happens **before** `CleanupFailedGroup` is structured
`mngr_lima/instance.py:581-600` cleans up on create failure inside `create_host`, raising `MngrError`. This is similar to OVH's pattern: create-time rollback is *not* expressed as `CleanupFailedGroup`. So Modal/Docker get aggregation for *destroy*, but no provider gets it for *create*. The `mngr/api/create.py:87-89` `except (MngrError, OSError, CleanupFailedGroup) as destroy_error` is ready to receive the latter form, but providers don't emit it on create-time rollback.

---

## 5. Symmetric strengths newly emerged or surfaced more clearly

1. **`CleanupFailedGroup` contract at the interface layer.** The `destroy_host` docstring (`provider_instance.py:401-406`) and the parallel `Host.destroy_agent` docstring (`interfaces/host.py:626, 642`) document the same contract -- "best-effort and aggregate-and-continue." Downstream code uses the same `except CleanupFailedGroup` pattern across `gc.py`, `cleanup.py`, `create.py`, and `headless_runner.py`. This is *good* convergence even though provider adoption is uneven.

2. **`auto_shutdown_seconds` field name.** All five VPS-family providers now share the same field name and the same plumbing through `_get_effective_auto_shutdown_seconds` (`mngr_vps_docker/instance.py:1684-1691`). Even though the *effect* differs by provider (F-L-9), the *config surface* is now uniform.

3. **Cloud-trio create-time validation hook.** AWS / Azure / GCP all override `_validate_provider_args_for_create` with the same pytest-time guard for `auto_shutdown_seconds`, raising the same `MngrError` shape ("Refusing to create a <provider> VM during pytest without auto_shutdown_seconds set..."). See `mngr_aws/backend.py:185-210`, `mngr_azure/backend.py:89-116`, `mngr_gcp/backend.py:101-149`. This is a clean symmetric extension point.

4. **Idempotent destroy across the cloud trio.** All three cloud `destroy_instance` paths treat HTTP 404/410 as benign (`mngr_aws/client.py:757-760`, `mngr_azure/client.py:667-677`, `mngr_gcp/client.py:453-467`). The base aggregation logic relies on this and gets it for free.

5. **`AzureSubscriptionError` properly wraps as `ProviderUnavailableError`.** The new error subclasses `(MngrError, ValueError)` (`mngr_azure/errors.py:12`), so the existing `backend.py` `except ValueError` continues to wrap it into `ProviderUnavailableError` while making the failure category structured for downstream consumers. Clean migration.

6. **Modal `gc_snapshots` is the only provider with an age-gated destroyed-host sweep tied to its own provider machinery** (`mngr_modal/instance.py:2156-2160`). Other providers reach the same goal via the global `mngr gc` path but Modal is wired more directly.

---

## 6. Open questions for the human reviewer

1. **Should the VPS-Docker base default `supports_shutdown_hosts = False`?** The base property returns `True` (`mngr_vps_docker/instance.py:403-404`). Today this means Azure/GCP/Vultr/OVH all report "shutdown supported" but their inherited `stop_host` only halts the container -- a documented "leak by inheritance." If the base were `False`, each subclass would have to opt-in by overriding both the flag and `stop_host`, which would naturally surface the cost asymmetry. Trade-off: existing users of `mngr stop --stop-host` on Vultr/OVH would suddenly get `HostShutdownNotSupportedError`.

2. **Should `CleanupFailedGroup` cover create-time rollback?** Today create-time rollback (Lima's `_cleanup_failed_lima_instance`, OVH's `_terminate_orphaned_fresh_order`, Azure's orphaned NIC/IP) does not raise `CleanupFailedGroup`. The downstream consumer in `mngr/api/create.py:87-89` is *ready* for it. Worth lifting create-time partial-failure into the same aggregation model?

3. **Should `auto_shutdown_seconds` semantics be normalized to "deallocate / terminate the VM"?** Today it's "halt OS" on Azure/Vultr/OVH and "terminate VM" on AWS/GCP. Either we standardize to terminate (consistent with cost-safety) or rename it per provider (more honest).

4. **Should Lima's vestigial `create_snapshot` and `snapshot_id` parameters be removed from the signature?** Right now Lima ignores them silently. Either implement snapshots via `limactl` clone, or drop the parameters from the override.

5. **Should OVH's "destroy = cancel at expiration" semantics get its own CLI verb?** `mngr destroy` is widely understood to mean "tear down now". OVH's behavior is closer to `mngr cancel` or `mngr retire`. Worth either renaming or adding a `--immediate` flag that *actually* deletes (OVH does have an immediate-termination endpoint, used by `_terminate_orphaned_fresh_order`).

6. **Should the SSH provider's capability flags be re-derived from its method bodies via a test, not declared?** A test that asserts "for every provider, if `supports_shutdown_hosts` is True then `stop_host` does not raise `NotImplementedError` on a synthetic host" would prevent the SSH-lies regression class. This is a ratchet-style guard.

7. **Should the Modal-only `bootstrap_for_host_creation` pattern (`mngr_modal/backend.py:506-527`) be lifted into the base?** Right now any backend with one-time per-user resources has to override this. OVH's recycle pool is a candidate -- the pool itself is a one-time per-user resource that today is implicit. If lifted, would the OVH pending-orders state directory become a `bootstrap_for_host_creation` thing rather than a `_provision_vps` thing?

8. **For Vultr and OVH, should there be a `mngr <provider> cleanup` command for parity with AWS/Azure/GCP?** Today there isn't (Vultr has no CLI module at all). The shared infra to clean up is smaller (no SG, no firewall) but the pytest-orphan-scanner gap is real and a `cleanup` command would be the natural place to put it.

---

## Appendix: methodology

This review re-read each provider's instance/backend module top-to-bottom rather than relying on round-1 findings, and cross-checked the prior review's high-level claims against current code on `mngr/reviewer-providers` (post-merge with `ev/main`, `mngr/azure`, `mngr/gcp`, `mngr/separate-snapshots`, `mngr/fix-discovery-provider`). Six parallel subagent passes covered: Modal, AWS, Azure, GCP, Vultr, OVH, Lima, Docker, SSH, and the shared `mngr_vps_docker` base. All cites are line-numbered against current files; where a method is inherited (no override), the citation points to the base implementation, with the absence-of-override called out explicitly.
