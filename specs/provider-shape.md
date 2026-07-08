# `specs/provider-shape.md` — Core Provider Shape

**Status.** Forward-looking, prescriptive. Companion to `specs/provider-uniformity-review.md` (descriptive — what providers do today) and `specs/provider-release-tests.md` (release-test trip proposal). For dev-facing walkthrough of how to actually implement a new provider, see `specs/implementing-a-provider.md`.

---

## 0. Audience & purpose

This document is for the person implementing a new mngr provider, or maintaining an existing one. It describes the **observable shape** a provider must present to mngr users — the lifecycle a user can rely on, the defaults the provider must ship with, the errors it must raise, and the capability flags it must answer honestly. It does NOT describe internal architecture (see `libs/mngr_vps/README.md` for the most common shared-base pattern). It is intentionally normative: each section uses MUST / SHOULD / MAY in the RFC-2119 sense.

The single most important user expectation is that **`mngr` feels the same across providers**. A user who learns `mngr create … -p modal` should be able to switch to `-p aws` or `-p gcp` and have the command, the visible side effects, the cost story, and the error messages be predictable. Whenever that uniformity is impossible (Modal has no concept of an "instance"; Lima has no concept of a region), the provider MUST be **honest about the gap** — via capability flags, via curated error help text, or via a refused operation — never via a silent no-op.

---

## 1. The user contract

These are the behaviors a user MUST be able to rely on from every provider. The signature `mngr <verb> …` is identical; the side effects must be predictable; failure modes must classify into the same exception hierarchy.

### 1.1 `mngr create <name> -p <provider>`

**MUST.** Provision (or locate) a host, start an agent on it, and leave the user in a state where `mngr exec <name>` opens an interactive shell that they own. Every provider's create path therefore MUST: (1) allocate or acquire compute, (2) run the docker container (or sandbox) that hosts the agent, (3) write the certified host record so `mngr list` can find the host without re-querying the cloud API, (4) start exactly one *initial* agent unless `--no-agent` was passed.

**MUST.** Support N agents per host (see §1.9). The "exactly one initial agent" above is a default for `mngr create`, not a host-capacity claim. After create, the user can add a second agent via `mngr exec <host> --new-agent` (or equivalent), and the host MUST accept it; `persist_agent_data` and `list_persisted_agent_data_for_host` are explicitly per-agent on the interface (`libs/mngr/imbue/mngr/interfaces/provider_instance.py`, `ProviderInstanceInterface`).

**SHOULD.** Reject unknown build args at parse time with a migration-style error (`raise_if_unknown_provider_arg` in `libs/mngr_vps/imbue/mngr_vps/build_args.py`). The pattern AWS/GCP/Azure/Vultr/OVH share is the right one. Build-arg prefixes SHOULD be the vendor-canonical short string (`--aws-region=`, `--gcp-zone=`, `--azure-region=`, `--vultr-region=`, `--ovh-datacenter=`). Modal's bare unprefixed flags (`--cpu`, `--memory`, `--gpu`) are appropriate for that provider because they describe a generic container shape, not a vendor SKU.

**SHOULD.** Run `_validate_provider_args_for_create` BEFORE the first provider-side write. GCP's pattern (`libs/mngr_gcp/imbue/mngr_gcp/backend.py`, `_validate_provider_args_for_create`) is the model: pre-flight the firewall rule, warn the user about implicit project resolution, raise cleanly if a prerequisite is missing. A failed precondition should produce no leaked resources and no `Host creation failed, attempting cleanup...` path. See `libs/mngr_vps/imbue/mngr_vps/instance.py` (`VpsProvider.create_host`) for the contract.

**MUST.** Auto-snapshot at agent-create time if `supports_snapshots` is `True`. Modal does this via the `on_agent_created` hookimpl (`libs/mngr_modal/imbue/mngr_modal/backend.py`, `on_agent_created`) which calls `ModalProviderInstance.on_agent_created` -> `_create_initial_snapshot` (`libs/mngr_modal/imbue/mngr_modal/instance.py`). A provider that claims `supports_snapshots=True` and skips this hook will lose user work to a single hard crash — that is the difference between a snapshot that does what the user expects and a `docker commit` lying about being one.

**Isolation (`isolation=CONTAINER` vs `isolation=NONE`).** Agent placement on a booted VPS is now performed by a `HostRealizer` (`libs/mngr_vps/imbue/mngr_vps/interfaces.py`, `HostRealizer`), selected by `VpsProviderConfig.isolation` (`libs/mngr_vps/imbue/mngr_vps/config.py`, default `IsolationMode.CONTAINER`; the `IsolationMode` enum lives in `libs/mngr_vps/imbue/mngr_vps/primitives.py`). `CONTAINER` runs the agent inside a Docker container (`DockerRealizer`); `NONE` ("bare") runs it directly on the VPS OS with no container (`BareRealizer`). `VpsProvider._realizer_for_isolation` (and the `_realizer` property; `libs/mngr_vps/imbue/mngr_vps/instance.py`) picks the realizer.

**MUST (bare gate).** A provider that supports `isolation=NONE` MUST have a real machine stop/start lifecycle. A bare agent's idle action powers the *VM itself* off (there is no container layer to stop independently), so a provider that cannot restart the machine would strand a VM the user can never recover. Providers without that substrate MUST reject `isolation=NONE` up front — before any billable provisioning — via `BareIsolationNotSupportedError` (`libs/mngr_vps/imbue/mngr_vps/errors.py`). The base `VpsProvider.create_host` enforces this by gating on `_supports_bare_isolation` (default `False`; `libs/mngr_vps/imbue/mngr_vps/instance.py`, `VpsProvider._supports_bare_isolation`). Only AWS, GCP, and Azure override it to `True` today; Vultr and OVH reject bare. Bare placements report `supports_snapshots=False` (see §2.1).

### 1.2 `mngr list`

**MUST.** Show every host that has ever been created via this provider — RUNNING, STOPPED, CRASHED, and (with `--include-destroyed`) DESTROYED. The set of states MUST be exactly the `HostState` enum; the provider does not get to invent new ones. Stopped-host visibility is now provided by the base: a provider whose hosts can be stopped while their disk persists extends `OfflineCapableVpsProvider` (`libs/mngr_vps/imbue/mngr_vps/instance_offline.py`), which reconstructs a host (and its agents) from the cloud's instance listing whenever the on-volume SSH path raises `HostNotFoundError`. AWS, Azure, and GCP all extend `OfflineCapableVpsProvider` directly; the offline view is backed by an external `HostStateStore` (a state bucket on AWS/Azure, GCE metadata on GCP — see §1.8). A provider that grows VM-level stop MUST route through this base; inheriting the plain `VpsProvider` "drop anything without a current public IP" behavior is a regression waiting to happen.

**MUST.** Raise `ProviderUnavailableError` (NOT silently return `[]`) when credentials are missing or the API is unreachable. `ProviderEmptyError` is reserved for the case where the backend is reachable and authoritatively reports zero hosts — for example, Modal's per-user environment doesn't exist yet (`libs/mngr/imbue/mngr/errors.py`, `ProviderEmptyError`). The two are not interchangeable: the empty case is safe to skip, the unavailable case must be visible to the user.

**SHOULD.** Use a single cached read of the cloud listing endpoint per command. AWS/GCP/Azure use `_list_instances_cached` (`libs/mngr_vps/imbue/mngr_vps/instance.py`, `VpsProvider._list_instances_cached`).

### 1.3 `mngr stop <agent>` (without `--stop-host`)

**MUST.** Stop the agent's tmux session only. Compute keeps running. This is uniform across all nine providers today and that is good — do not innovate here.

**MUST.** Be idempotent. Running `mngr stop` on a stopped agent returns success without error.

### 1.4 `mngr stop <agent> --stop-host`

**MUST.** Either (a) stop the compute so the user stops paying for it, OR (b) refuse loudly via `HostShutdownNotSupportedError`. There is no acceptable third option. A `mngr stop --stop-host` that silently leaves the VM running while reporting "Stopped host: my-agent" is a cost leak that masquerades as success. AWS, GCP, and Azure all do real machine-level stop today. The base `VpsProvider.stop_host` (`libs/mngr_vps/imbue/mngr_vps/instance.py`) stops the *placement* (a docker-stop for `CONTAINER`, a no-op for bare via the realizer); `OfflineCapableVpsProvider.stop_host`/`start_host` (`libs/mngr_vps/imbue/mngr_vps/instance_offline.py`) layer the machine-level stop on top, calling the cloud hooks `_pause_cloud_instance`/`_resume_cloud_instance` that each cloud supplies. Vultr and OVH are the remaining container-only holdouts: they extend the plain `VpsProvider` and supply no cloud pause hook, so `--stop-host` stops only the container and the VPS keeps billing — a gap they MUST close (real stop, or flip `supports_shutdown_hosts` to `False`).

**Right (loud refusal).** Modal raises `HostShutdownNotSupportedError`: refusal is honest about a missing capability. The corresponding pattern: set `supports_shutdown_hosts=False` and let the CLI error before the work begins.

**Right (real stop).** The clouds do not override `stop_host`/`start_host`; they supply `_pause_cloud_instance`/`_resume_cloud_instance` into the base. AWS's pause hook (`libs/mngr_aws/imbue/mngr_aws/backend.py`, `_pause_cloud_instance`) calls the client `stop_instance` (`ec2:StopInstances`), preserving the EBS volume, and surfaces the stopped instance in `mngr list` via the offline reconstruction. GCP does the same with a real VM stop (`libs/mngr_gcp/imbue/mngr_gcp/backend.py`, `_pause_cloud_instance`), landing the instance in `TERMINATED`. Azure does a true `deallocate_instance` (billing stops, not just an OS-stop) via its pause hook (`libs/mngr_azure/imbue/mngr_azure/backend.py`, `_pause_cloud_instance`).

**SHOULD.** When `--stop-host` would stop *only* the container and leave the VM billing, the provider MUST NOT inherit the container-only path. Either extend `OfflineCapableVpsProvider` and supply `_pause_cloud_instance`/`_resume_cloud_instance` to also stop the VM (as AWS/GCP/Azure do), or override `supports_shutdown_hosts` to `False` and document the gap in the README (the standing requirement for Vultr/OVH).

### 1.5 `mngr start <agent>`

**MUST.** Be idempotent. If the host is already running, return success with no API call (or at most a cheap status check). If the host is stopped, start it; if the host was DESTROYED, raise `HostNotFoundError` (not `HostAlreadyExistsError` — destroyed is gone).

**SHOULD.** Re-bind the known-hosts file if the public IP changed during stop+start. This is generic in the base `OfflineCapableVpsProvider` (`_rebind_known_hosts` / `_rebind_known_hosts_pre_connect` in `libs/mngr_vps/imbue/mngr_vps/instance_offline.py`), run automatically on resume; providers whose stop preserves the IP can skip it.

**MUST.** If a `snapshot_id` is passed, EITHER restore from it OR raise `SnapshotsNotSupportedError`. Silent no-op (current AWS/Azure/GCP base path) is the worst option — the user thinks they restored work and lost it instead. See `libs/mngr/imbue/mngr/interfaces/provider_instance.py` (`ProviderInstanceInterface.start_host`) for the signature.

### 1.6 `mngr destroy <agent>`

**MUST.** Delete every billable resource attached to the host. For cloud providers: instance, attached disks (`DeleteOnTermination=True` on AWS EBS; `delete_option=Delete` on Azure NIC/IP/OS-disk; `auto_delete=True` on GCP boot disk), any public IP that was associated for our use. For VPS providers: terminate the VPS itself.

**MAY.** Preserve snapshots (provider's choice — Modal does, the cloud trio does not). If snapshots are preserved, the provider MUST surface them via `gc_snapshots` for age-based cleanup later.

**MUST.** Tell the user what was destroyed and what was left behind. CleanupFailedGroup (`libs/mngr/imbue/mngr/interfaces/cleanup_failures.py`) is the contract: aggregate every real failure into a single error so the user sees the punch list, not just the first traceback. See `libs/mngr/imbue/mngr/interfaces/provider_instance.py` (`ProviderInstanceInterface.destroy_host`) and `specs/cleanup-error-aggregation.md`.

**MUST.** Be idempotent on 404 / "already gone". A resource that was already absent is not a failure.

### 1.7 `mngr <provider> cleanup`

**MUST** exist for any provider that creates per-user backend resources during `prepare` (cloud providers: security groups, firewall rules, IAM roles, resource groups). It is the inverse of `mngr <provider> prepare`.

**MUST** refuse while user resources exist. AWS: `libs/mngr_aws/imbue/mngr_aws/cli.py` (`_refuse_cleanup_if_instances_exist`) refuses if any instance carrying `mngr-provider` tag exists. Same pattern at GCP `cli.py` (`cleanup`) and Azure `cli.py` (`_refuse_cleanup_if_vms_exist`). Error message MUST tell the user how to clean up first: "Refusing to clean up… destroy them first with `mngr destroy <agent>`".

**MUST** be tag-scoped. A `cleanup` that deletes any infrastructure NOT carrying a `mngr-*` tag is a footgun.

**MAY** be omitted for providers with no per-user backend resources (Modal's environment is auto-created; Lima has none; Docker has none; SSH has none). Such providers SHOULD document the gap rather than add a no-op for parity.

### 1.8 N agents on one host

The interface was designed for this from the start: the persist/list/remove signatures on `ProviderInstanceInterface` are per-agent (`libs/mngr/imbue/mngr/interfaces/provider_instance.py`); `remove_persisted_agent_data(host_id, agent_id)` is `(host_id, agent_id)`-keyed, while `list_persisted_agent_data_for_host(host_id)` is host-only and `persist_agent_data(host_id, agent_data)` writes a per-agent blob under the host. `HostInterface.get_agents() -> list[AgentInterface]` returns a list (`libs/mngr/imbue/mngr/interfaces/host.py`, `HostInterface.get_agents`); `start_agents(agent_ids: Sequence[AgentId])` (`HostInterface.start_agents`) and `stop_agents` accept multiple agents at once. The in-host data layout is uniformly per-agent: all state lives under `host_dir/agents/<agent_id>/` (`libs/mngr/imbue/mngr/hosts/common.py`, `libs/mngr/imbue/mngr/hosts/host.py`).

**MUST (live multi-agent).** A provider that returns a running host from `mngr create` MUST allow a second agent to be added to that host via `mngr exec <host> --new-agent` (or equivalent). For VPS-backed providers this means the live discovery scan MUST find in-placement agents that were not present at create time. This scan is now a realizer responsibility — `HostRealizer.read_live_listing` / `collect_listing_output` / `find_host_record` (`libs/mngr_vps/imbue/mngr_vps/interfaces.py`). The Docker realizer runs the listing script *inside* the container (`libs/mngr_vps/imbue/mngr_vps/docker_realizer.py`); the bare realizer reads the agent store on the VM's root disk directly (`libs/mngr_vps/imbue/mngr_vps/bare_realizer.py`). Either way, agents created on the host after create (e.g. by minds' chat flow) are visible immediately.

**MUST (persisted multi-agent).** `persist_agent_data` MUST be keyed per-agent. Modal: `/hosts/{host_id}/{agent_id}.json` on the Modal state volume (`libs/mngr_modal/imbue/mngr_modal/instance.py`, `ModalProviderInstance.persist_agent_data`). VPS family: per-agent on the on-VPS store via `VpsHostStore.persist_agent_data` (`libs/mngr_vps/imbue/mngr_vps/host_store.py`, `VpsHostStore.persist_agent_data`). A provider whose `persist_agent_data` packs multiple agents into a single blob or overwrites is broken.

**MUST (lifecycle preservation).** `stop_host` / `start_host` MUST preserve all N agents' state across the cycle. The container's `host_dir` holds per-agent subdirectories (work dir, state dir, conversation logs); the provider does not get to drop any of them. `destroy_host` MUST iterate all agents — `CleanupFailedGroup` (§1.6) covers the case where individual agent cleanups fail independently. Modal's `_destroy_agents_on_host` (`libs/mngr_modal/imbue/mngr_modal/instance.py`, `ModalProviderInstance._destroy_agents_on_host`) is the model.

**SHOULD (offline mirror).** When the host's compute is stopped or unreachable, the provider SHOULD continue to report all N agents in `mngr list` via an offline mirror in provider-side metadata. This is now provided by the base offline triad: the host record, the host's agent list, and the per-agent payloads are all reconstructed without the VM by `OfflineCapableVpsProvider` (`libs/mngr_vps/imbue/mngr_vps/instance_offline.py`). The backing is a single external `HostStateStore`, resolved per provider via the `_state_store` hook; the other per-provider hooks are `_offline_discovered_host_from_instance`, `_is_instance_offline`, and `_offline_agent_dicts_for`. On AWS/Azure the store is a required object-storage state bucket (`BucketHostStateStore` over a `StateBucket`, `libs/mngr_vps/imbue/mngr_vps/host_state_store.py`; concrete `S3StateBucket` on AWS in `libs/mngr_aws/imbue/mngr_aws/state_bucket.py` and `BlobStateBucket` on Azure in `libs/mngr_azure/imbue/mngr_azure/state_bucket.py`) — there is no degraded mode, and `missing_state_bucket_error` (`libs/mngr_vps/imbue/mngr_vps/host_state_store.py`) is raised if the bucket is unprovisioned. GCP overrides `_state_store` with a GCE-metadata-backed store. With the store configured, full records and full agent payloads are available offline, with no agent cap. Vultr and OVH still inherit no offline mirror; when they grow VM-level stop (which they MUST per §1.4), they MUST also grow one. The data is still safe on the per-host volume; it's just invisible in `mngr list` until the VM is up again.

**Anti-pattern.** A provider that hard-codes "one agent per host" anywhere — in the `create_agent_work_dir` path, in the persisted-agent format, in the discovery scan — silently breaks multi-tenancy with no visible error. The interface accepts the second `create_agent_*` call, the user sees no failure, and the second agent's data overwrites the first.

**Honest cap.** AWS and Azure require the state bucket and have no agent cap and no fallback: `_offline_agent_dicts_for` reads the state store directly. A provider that backs its offline mirror with a *capped* store (e.g. a per-field tag mirror bounded by a cloud's tag-count limit) SHOULD surface that limit rather than silently drop data — but AWS/Azure do not do this today, because they do not use a capped store.

**Per-provider tiers (today):**
- **Tier A (verified working, full offline view):** Modal, Lima, Docker, local — per-agent storage, tested. AWS and Azure via the required state bucket (full records and full agent payloads offline, no agent cap). GCP via the GCE metadata mirror.
- **Tier B (no offline view at all):** Vultr, OVH (no VM-level stop and no offline mirror; data is intact on the volume but invisible while unreachable). SSH — agents enumerated only via live SSH; if the host is unreachable, all agents fall out of discovery (`libs/mngr/imbue/mngr/providers/ssh/instance.py` FIXME).
- **Tier C (single-agent by construction):** none in tree.

**Out of scope.** "N hosts share one VM" (multi-container packing onto a single VM, e.g. the AWS README's `[future]` item) is a separate roadmap question that is **not** covered by §1.8. Every VPS provider documents in `VpsProvider`'s docstring that each host maps to exactly one VPS running exactly one placement (`libs/mngr_vps/imbue/mngr_vps/instance.py`, `VpsProvider`).

### 1.9 Error class for each failure mode (uniform contract)

| Failure | Class | When |
|---|---|---|
| Cloud creds missing / API down | `ProviderUnavailableError` | At backend construction |
| Backend reachable, zero hosts | `ProviderEmptyError` | Backend confirms empty |
| Host name doesn't exist | `HostNotFoundError` | Lookup |
| `mngr create` for already-existing name | `HostAlreadyExistsError` | Create |
| Operation requires running host, host stopped | `HostNotRunningError` | Stop/start/exec |
| Operation requires stopped host, host running | `HostNotStoppedError` | Start from stopped |
| Provider doesn't support shutdown | `HostShutdownNotSupportedError` | `--stop-host` on Modal et al. |
| Snapshot id not found | `SnapshotNotFoundError` | start_host/destroy_snapshot |
| Multi-resource cleanup partial failure | `CleanupFailedGroup` | destroy_host |
| `isolation=NONE` on a provider without a stop/start lifecycle | `BareIsolationNotSupportedError` | create |

Every cloud provider MUST pass curated `user_help_text` on `ProviderUnavailableError`. The default text (`libs/mngr/imbue/mngr/errors.py`) tells the user to "start Docker" — wrong advice for an AWS auth failure. All three clouds now do this: `_aws_unavailable_error` (`libs/mngr_aws/imbue/mngr_aws/backend.py`), `_gcp_unavailable_error` (`libs/mngr_gcp/imbue/mngr_gcp/backend.py`), and `_azure_unavailable_error` (`libs/mngr_azure/imbue/mngr_azure/backend.py`) each return provider-specific guidance (Azure: "run `az login`").

---

## 2. Capability flags — the honesty contract

The `supports_*` flags on `ProviderInstanceInterface` (`libs/mngr/imbue/mngr/interfaces/provider_instance.py`, `ProviderInstanceInterface`) are NOT advisory hints. They are contracts the CLI branches on. A `False` flag MUST mean "calling this method will raise a clear error"; a `True` flag MUST mean "calling this method will do the thing the user expects".

### 2.1 `supports_snapshots`

`True` means `create_snapshot`, `list_snapshots`, `delete_snapshot` all work AND that snapshots are useful for hard-crash recovery — i.e. either an `on_agent_created` hook auto-snapshots (Modal) or the user understands they must call `mngr snapshot create` manually.

For the VPS family the flag is now **realizer-derived**: `VpsProvider.supports_snapshots` returns `isinstance(self._realizer, SnapshotCapableRealizer)` (`libs/mngr_vps/imbue/mngr_vps/instance.py`, `VpsProvider.supports_snapshots`). `DockerRealizer` subclasses `SnapshotCapableRealizer` and implements `snapshot_placement` (`libs/mngr_vps/imbue/mngr_vps/docker_realizer.py`); `BareRealizer` has no `snapshot_placement` method at all (`libs/mngr_vps/imbue/mngr_vps/bare_realizer.py`). Per-host snapshot operations narrow the host's own realizer via `_require_snapshot_capable_realizer` (`libs/mngr_vps/imbue/mngr_vps/instance.py`), which raises `SnapshotsNotSupportedError` for a bare placement. So `isolation=NONE` honestly advertises no snapshots, while `isolation=CONTAINER` keeps the existing `docker commit` behavior.

**Anti-pattern (still real).** Under `isolation=CONTAINER`, AWS/Azure/GCP report `supports_snapshots=True` but `create_snapshot` is a `docker commit` of the container layer stored on the VPS's own disk (`DockerRealizer.snapshot_placement` → `commit_container`, `libs/mngr_vps/imbue/mngr_vps/docker_realizer.py`). It survives `mngr stop` but not `mngr destroy` and is not portable across hosts. This is a different product from a Modal snapshot. Open question (§11): should this be split into `supports_persistent_snapshots`?

### 2.2 `supports_shutdown_hosts`

`True` means `stop_host` actually stops compute billing. The base `VpsProvider.stop_host` stops only the *placement* (docker-stop for `CONTAINER`, no-op for bare). `OfflineCapableVpsProvider.stop_host`/`start_host` add the machine-level stop on top, driven by the cloud hooks `_pause_cloud_instance`/`_resume_cloud_instance`; a provider that wants to honestly claim `True` extends that base and supplies those hooks. AWS/GCP/Azure all do (`_pause_cloud_instance`/`_resume_cloud_instance` in `libs/mngr_aws/imbue/mngr_aws/backend.py`, `libs/mngr_gcp/imbue/mngr_gcp/backend.py`, `libs/mngr_azure/imbue/mngr_azure/backend.py`). Vultr and OVH inherit the base flag value `True` while doing no VM-level stop — they are the standing honesty gap (see §1.4, §8).

**Anti-pattern.** SSH provider at `libs/mngr/imbue/mngr/providers/ssh/instance.py` returns `True` but `stop_host` raises `NotImplementedError`. Any CLI branch that consults the flag will fail. Either flip the flag to `False`, or implement `stop_host` as a no-op (the BYO-host user expects to manage compute themselves).

### 2.3 `supports_volumes`

`True` means `list_volumes`, `delete_volume`, and `get_volume_for_host` all work and return real volumes. Local providers (Docker, SSH) where the user's own filesystem IS the volume return `False`. Modal returns `True` (volumes are first-class Modal objects).

**Anti-pattern (still real).** The VPS family returns `supports_volumes=True` (`libs/mngr_vps/imbue/mngr_vps/instance.py`, `VpsProvider.supports_volumes`) but `list_volumes` returns `[]` and `delete_volume` is a no-op (`libs/mngr_vps/imbue/mngr_vps/instance.py`) — inherited unchanged by AWS/GCP/Azure/Vultr/OVH. The state-bucket work did add a real `get_volume_for_host` on the base `OfflineCapableVpsProvider` (`libs/mngr_vps/imbue/mngr_vps/instance_offline.py`, `OfflineCapableVpsProvider.get_volume_for_host`), but `list_volumes` / `delete_volume` remain unimplemented and the flag was not split. So the flag still over-claims: a CLI branch that lists or deletes volumes gets a silent empty/no-op. SSH correctly reports `supports_volumes=False`.

### 2.4 `supports_mutable_tags`

`True` means `add_tags_to_host` / `remove_tags_from_host` / `set_host_tags` mutate the underlying tags after host creation. Docker (immutable container labels) returns `False`; AWS (EC2 tags are mutable) returns inherited `False` from the `VpsProvider` base; Lima (mutable on-disk file) returns `True`.

---

## 3. Default values that providers should share

The values below are how a provider can be opinionated *and* still feel uniform. Two providers can pick different default regions, but they MUST share the same network-default posture (open-by-default with a warning, key-only SSH as the control — §3.1) and they MUST land on roughly the same compute shape.

### 3.1 Security defaults — CIDR / SSH ingress

**Standard.** The cloud-trio default is `allowed_ssh_cidrs = ("0.0.0.0/0",)` with a runtime warning. SSH is key-only on every cloud-created host (password auth disabled in sshd + cloud-init), so opening tcp/22 to the world exposes the port but not a usable login — defense-in-depth, not the primary control.

**MUST.** SSH MUST be key-only on every cloud-created host. Password auth MUST be disabled in the sshd config and in cloud-init.

**MUST.** When `allowed_ssh_cidrs` resolves to exactly `0.0.0.0/0` or empty, the provider MUST log a WARNING at firewall-creation time naming the resolved range and pointing at the config key to tighten. All three clouds already do this via `_warn_about_cidrs_if_needed` (AWS, Azure, GCP — each in the provider's `client.py`). The default lives on `OfflineCapableVpsProviderConfig.allowed_ssh_cidrs` (`libs/mngr_vps/imbue/mngr_vps/config.py`), inherited uniformly. Silent open-by-default is the anti-pattern, not open-by-default itself.

**SHOULD.** Document the operator tightening path in the provider README: `[providers.<X>] allowed_ssh_cidrs = ["203.0.113.4/32"]` plus rerun `mngr <provider> prepare`. Production guidance: tighten before pointing the provider at production resources.

**SHOULD.** Map the container SSH port the same way as the VM SSH port — i.e. the cloud firewall is the security perimeter, NOT the container's own port mapping. The container's sshd binds to localhost on the VPS (`container_ssh_port: 2222`, `libs/mngr_vps/imbue/mngr_vps/config.py`, `VpsProviderConfig.container_ssh_port`) and is exposed via the same cloud-firewall ingress as VM port 22. Never expose the container directly on `0.0.0.0` on a public IP without going through the cloud firewall — the cloud firewall is the user's control point.

**Anti-pattern (local providers).** The Docker provider currently binds `-p :22` which Docker resolves to `0.0.0.0:<random>:22` on the host's *machine* (`libs/mngr/imbue/mngr/providers/docker/instance.py`, `DockerProviderInstance._build_docker_run_command`). This is the local analogue of the cloud-firewall issue: the agent's sshd is reachable from the host's LAN with no cloud-firewall in front of it. Local providers SHOULD bind to `127.0.0.1` by default and require an explicit opt-in for LAN reachability. The cloud trio's "open-but-key-only" rationale does NOT extend to local providers because they have no cloud-firewall as the perimeter.

**Open design question (§11).** The fail-open / fail-closed decision matters more for compliance contexts (HIPAA, SOC2) than for the typical developer-laptop user. A future revision MAY add a `security_profile = "developer" | "production"` config knob that flips the default per-profile. For now, the documented contract is open-by-default-with-warning across the cloud trio.

### 3.2 Idle defaults — activity-based self-stop

**SHOULD.** `default_idle_timeout = 800` seconds (the value at `libs/mngr_vps/imbue/mngr_vps/config.py`, `VpsProviderConfig.default_idle_timeout`) is a reasonable shared default. Providers MAY override; the user can override per agent.

**MUST.** When the idle timeout fires, the provider's behavior MUST match its `supports_shutdown_hosts` claim:
- `supports_shutdown_hosts=False`: idle stops the agent's tmux only. User pays for compute until manual destroy.
- `supports_shutdown_hosts=True`: idle stops compute. AWS's sentinel-file + systemd `.path` unit pattern is the model (the idle watcher on the inner host writes a sentinel; an outer-host systemd path-unit fires a guest `shutdown -P now`, and `InstanceInitiatedShutdownBehavior` decides stop vs terminate). GCP and Azure now do this too: GCP uses the same sentinel + systemd pattern (guest `shutdown -P now` lands the instance in `TERMINATED`, so no extra flag is needed); Azure's idle path runs an ARM self-deallocate via the VM's managed identity (an OS poweroff alone would NOT stop billing — see §3.3). For bare placements the realizer issues `shutdown -P now` directly (`BareRealizer.idle_shutdown_command`, `BareRealizer.idle_shutdown_stops_host`, `libs/mngr_vps/imbue/mngr_vps/bare_realizer.py`): the VM powers itself off with no sentinel indirection (on Azure bare, that still routes through the same ARM deallocate).

### 3.3 Auto-shutdown defaults — hard max-lifetime cap

`auto_shutdown_seconds` (`libs/mngr_vps/imbue/mngr_vps/config.py`, `VpsProviderConfig.auto_shutdown_seconds`) is a hard time-bomb on the host, distinct from activity-based idle. Default: `None` (off). When set:

**MUST** actually stop billing. On AWS, this works because the AMI runs `shutdown -P +N` and the instance has `InstanceInitiatedShutdownBehavior=terminate`. On GCP, it works via `scheduling.max_run_duration` + `instance_termination_action=DELETE`. On Azure, the idle/auto-stop path now runs an ARM self-deallocate via the VM's managed identity and an IMDS token (`_build_self_deallocate_script`, `libs/mngr_azure/imbue/mngr_azure/backend.py`) — billing actually stops, fixing the former footgun where `shutdown -P` left the VM "Stopped (not deallocated)" and still billing. The required role is created by `ensure_self_deallocate_role` and assigned per-VM via `assign_self_deallocate_role` (`libs/mngr_azure/imbue/mngr_azure/client.py`); if the operator lacks `roleAssignments/write`, a warning is logged and only manual `mngr stop` halts billing (graceful fallback).

**MUST** be testable. Add `test_create_instance_passes_auto_shutdown_to_user_data` (or equivalent) per provider that pins the value reaches the cloud API (currently only the pre-create gate is pinned).

### 3.4 Resource defaults — disk size

Field name varies by cloud convention and that's fine:
- AWS: `root_volume_size_gb` (`libs/mngr_aws/imbue/mngr_aws/config.py`, `AwsProviderConfig.root_volume_size_gb`)
- GCP: `boot_disk_size_gb` (`libs/mngr_gcp/imbue/mngr_gcp/config.py`, `GcpProviderConfig.boot_disk_size_gb`)
- Azure: `os_disk_size_gb` (`libs/mngr_azure/imbue/mngr_azure/config.py`, `AzureProviderConfig.os_disk_size_gb`)

**SHOULD** all default to `30` GB (current state; matches a typical agent's working set with a bookworm-slim image).

**SHOULD NOT** be standardized across providers — the field name is the field the cloud's own docs call it, and a user reading the AWS console expects `root_volume` not `boot_disk`. Cross-provider uniformity here would hurt readability of the per-cloud README.

### 3.5 Region / zone defaults

**MUST** document the default in the config field's `description=…` string (all four cloud providers do this).

**MUST** document the cost implications. A user picking the default should not be surprised by spot-vs-on-demand pricing or by data-egress tiers in a particular region.

**MUST** refuse cross-region `mngr create` with a clear error message. The standard error is `VpsApiError(400, "Cross-region create not supported")` (AWS `libs/mngr_aws/imbue/mngr_aws/client.py`, `AwsVpsClient.create_instance`, and equivalents); the error message SHOULD include a "use `--provider <other-region-provider>` instead" hint.

### 3.6 Instance / VM size defaults

**SHOULD** size for a typical agent workload (~2 vCPU, 2-8 GB RAM). Current state: GCP `e2-small` (~2 vCPU, 2 GB; `libs/mngr_gcp/imbue/mngr_gcp/config.py`, `GcpProviderConfig.default_machine_type`); Azure `Standard_B2s` (2 vCPU, 4 GB; `libs/mngr_azure/imbue/mngr_azure/config.py`, `AzureProviderConfig.default_vm_size`); AWS `t3.small` or similar. The B-series / e2 / t3 burstable families are the right pick because they are the most likely to have nonzero vCPU quota on a fresh subscription.

**SHOULD** be surfaced as the provider's vendor-canonical short flag: `--gcp-machine-type`, `--azure-vm-size`, `--aws-instance-type`.

### 3.7 Image / OS defaults

**MUST** support whatever bootstrap mechanism the cloud uses (cloud-init for AWS/Azure/Vultr/OVH; GCE startup-scripts for GCP since commit `a9bbd4725`). The cloud-trio fleet is now uniformly Debian 12 (AWS `libs/mngr_aws/imbue/mngr_aws/config.py`, Azure `libs/mngr_azure/imbue/mngr_azure/config.py`, GCP `libs/mngr_gcp/imbue/mngr_gcp/config.py`). The container image is uniformly `debian:bookworm-slim` (`libs/mngr_vps/imbue/mngr_vps/config.py`, `VpsProviderConfig.default_image`).

**MUST** pin a specific image SKU/version. Drifting "latest" defaults make `mngr create` non-reproducible and can break the install path silently.

**SHOULD** expose per-region image override if the cloud's image identifiers are regional (AWS AMIs are; GCE image families are global; Azure URNs are global). AWS's `default_ami_by_region` is the pattern; GCP gained `--gcp-image` per-host override in commit `8a0fd81de`.

### 3.8 Tagging conventions

**MUST** apply at least these three tags to every created resource:
- `mngr-host-id=<host_id>` (the host's stable UUID; used for tag-based lookup when the host is stopped)
- `mngr-provider=<provider_instance_name>` (so `mngr <provider> cleanup` can refuse while resources exist)
- `mngr-created-at=<ISO-8601>` (so the orphan scanner can age-gate; AWS `libs/mngr_aws/imbue/mngr_aws/client.py`, `AwsVpsClient.create_instance`)

**SHOULD** apply `mngr-pytest-launched=true` to test-created resources (`AWS_PYTEST_LAUNCHED_TAG`, `libs/mngr_aws/imbue/mngr_aws/client.py`). This is what the `pytest_sessionfinish` orphan scanner targets.

**SHOULD** mirror agent records onto host metadata so stopped-host discovery can reconstruct the agent list. For AWS/Azure this is the required state bucket (§1.8), which has no tag-count or value-length cap; GCP uses GCE metadata instead. Vultr/OVH still inherit no mirror.

**Convention.** Dashes, not underscores, in tag keys. Modal currently uses `mngr_host_id` (underscore, `TAG_HOST_ID` in `libs/mngr_modal/imbue/mngr_modal/instance.py`); everyone else uses `mngr-host-id`. Scripts walking tags currently need two code paths. Modal SHOULD switch to dashes; the rest are already consistent.

### 3.9 Per-host SSH key location and lifecycle

**MUST** generate per-provider-instance SSH keys, NOT per-host. Pattern: `mngr_ctx.profile_dir / "providers" / <backend> / <name> / "keys"` (`libs/mngr_vps/imbue/mngr_vps/instance.py`, `VpsProvider._key_dir`).

**SHOULD** generate four keypairs per provider instance: `vps_ssh_key` (VPS auth), `container_ssh_key` (container auth), `host_key` (VPS sshd host key), `container_host_key` (container sshd host key). The host-keys are injected via cloud-init so strict host-key checking works on first connect.

**MUST** preserve keys across `mngr stop` / `mngr start`. Destroying the provider instance (config-level) is the only operation that should rotate the keys.

### 3.10 Container exposure

**MUST.** Container sshd is reachable only via the cloud firewall's ingress allow-list. On the VPS, `docker run -p <random>:2222` MUST bind to localhost on the VPS — NOT to `0.0.0.0`. The VPS's cloud firewall is what authorizes the connection.

**MUST.** Local-host providers MUST bind to `127.0.0.1`, not `0.0.0.0`. The Docker provider already does this for the daemon-detection path (`libs/mngr/imbue/mngr/providers/docker/instance.py`, `_get_ssh_host_from_docker_config`). However, the container's port-22 binding currently uses `-p :22` which binds on all host interfaces — this is the lesson worth pulling forward into other local providers: a local provider's published ports MUST default to `127.0.0.1`.

---

## 4. Lifecycle hooks — what to override

A cloud provider's most common implementation shape is "subclass `VpsProvider` (`libs/mngr_vps/imbue/mngr_vps/instance.py`), override 4-6 hooks, get the rest for free". The shared base supplies the parallel-SSH discovery, the host-record cache, the snapshot machinery, the `mngr destroy` aggregation, and the build-args parser scaffolding.

**Two seams have been factored out of the provider since the original write-up:**

- **The realizer seam.** Agent *placement* on a booted VPS no longer lives in the provider. It is a `HostRealizer` (`libs/mngr_vps/imbue/mngr_vps/interfaces.py`) selected by `config.isolation`: `DockerRealizer` (default, `CONTAINER`) or `BareRealizer` (`NONE`). The realizer owns `realize_placement`, `stop_placement` / `start_placement` / `teardown_placement`, the live agent listing (`read_live_listing` / `collect_listing_output` / `find_host_record`), and the idle-shutdown command. `snapshot_placement` belongs to the `SnapshotCapableRealizer` subclass (`libs/mngr_vps/imbue/mngr_vps/interfaces.py`), not the base `HostRealizer`; no realizer carries a `supports_snapshots` attribute (the provider derives the flag via `isinstance`, §2.1). A provider that adds a new isolation level adds a realizer, not provider overrides. Most providers never touch the realizer — the default `CONTAINER` preserves the original behavior.
- **The offline base classes.** A provider whose hosts can be stopped while their disk persists subclasses `OfflineCapableVpsProvider` (`libs/mngr_vps/imbue/mngr_vps/instance_offline.py`) instead of `VpsProvider` directly; it gets the stopped-host reconstruction triad (host record, agent list, per-agent payloads) for free and supplies the per-provider hooks (`_offline_discovered_host_from_instance`, `_is_instance_offline`, `_offline_agent_dicts_for`, `_state_store`). AWS, Azure, and GCP all extend `OfflineCapableVpsProvider` directly; AWS/Azure back `_state_store` with a required state bucket, GCP overrides it with a GCE-metadata store.

To support bare (`isolation=NONE`), override the `_supports_bare_isolation` property to `True` (`libs/mngr_vps/imbue/mngr_vps/instance.py`, `VpsProvider._supports_bare_isolation`) — but only if the provider has a real machine stop/start lifecycle (§1.1); otherwise the base correctly rejects bare via `BareIsolationNotSupportedError`.

These are the override hooks, with the contract for each. The full list of hook points is at `libs/mngr_vps/README.md` and below.

### 4.1 `_fetch_provider_instances() -> list[dict[str, Any]]`

**Contract.** Return the raw instance dicts (one per active VM) from the cloud's list-instances endpoint, filtered to those carrying `mngr-provider=<self.name>`. Called at most once per command via `_list_instances_cached` (`libs/mngr_vps/imbue/mngr_vps/instance.py`, `VpsProvider._list_instances_cached`).

**Default.** `[]`. Subclasses without a tag-based listing API (OVH uses parallel-SSH probing instead, via `_list_provider_vps_hostnames`) can keep the default.

**MUST raise** `ProviderUnavailableError` on creds/API failure; MUST NOT swallow.

### 4.2 `_parse_build_args(build_args: Sequence[str] | None) -> ParsedVpsBuildOptions`

**Contract.** Compose the helpers in `libs/mngr_vps/imbue/mngr_vps/build_args.py` (`extract_single_value_arg`, `extract_git_depth`, `extract_presence_flag`, `raise_if_vps_migration_arg`, `raise_if_unknown_provider_arg`, `parse_vps_build_args`, returning `ParsedVpsBuildOptions`). The provider's vendor prefix MUST be passed as `provider_prefix` to `parse_vps_build_args` (e.g. `--aws-`, `--gcp-`, `--azure-`). Unknown flags MUST raise; the legacy `--vps-*` prefix MUST raise the migration error.

**Now `@abstractmethod`.** The previous "raises a `must override` error" pattern surfaced the contract only at runtime; the current `@abstractmethod` declaration surfaces it at construction.

### 4.3 `_create_vps_instance(...) -> VpsInstanceId`

**Contract.** Call the typed client's `create_instance` method. Override only if the provider needs to thread provider-specific knobs through (e.g. AWS threads `ami_id_override` from `ParsedAwsBuildOptions`). Default mirrors the previous direct call.

### 4.4 `_list_provider_vps_hostnames() -> list[str]`

**Contract.** Return SSH-reachable hostnames (public IPv4 or provider DNS name) for VPSes tagged `mngr-provider=<self.name>`. Used by the parallel-SSH host-record discovery in the base.

**Default.** `[]`. Most subclasses override.

### 4.5 `_validate_provider_args_for_create() -> None`

**Contract.** A cheap, local-or-single-read-only-API-call preflight that fires BEFORE the first provider-side write (`libs/mngr_vps/imbue/mngr_vps/instance.py`, `VpsProvider.create_host`). On failure, raise a `MngrError` subclass with curated `user_help_text`. AWS's pytest auto-shutdown guard and GCP's firewall-existence check are the two non-trivial examples; default is no-op.

**MUST be cheap.** This runs on every `mngr create`. A single read-only API call is fine; a multi-step probe is not.

### 4.6 `bootstrap_for_host_creation(name, config, mngr_ctx) -> None`

**Contract.** Idempotently create one-time per-user backend resources (Modal's environment is the motivating example, `libs/mngr/imbue/mngr/interfaces/provider_backend.py`, `ProviderBackendInterface.bootstrap_for_host_creation`). Called by the create-host path unconditionally before `build_provider_instance`. No other code path triggers a bootstrap. The default is a no-op; cloud providers whose `prepare` is the explicit one-time step usually don't need to override.

### 4.7 `_pause_cloud_instance` / `_resume_cloud_instance` (offline cloud hooks)

**Contract.** Supply these to get VM-level stop/start. The clouds do not override `stop_host`/`start_host` themselves; `OfflineCapableVpsProvider.stop_host`/`start_host` own the cycle and call these hooks (`_pause_cloud_instance` to stop the VM, `_resume_cloud_instance` to start it and return the new SSH address). See §1.4 and §1.5. Both MUST be idempotent.

---

## 5. Error classification contract

Every failure must classify into one of the existing exception classes (`libs/mngr/imbue/mngr/errors.py`). Inventing new ones is fine; widening the contract is not.

### 5.1 `ProviderEmptyError`

**Raise** when the backend is reachable and authoritatively reports zero hosts (Modal: per-user environment doesn't exist). Read paths (`mngr list`, `mngr gc`, discovery) silently skip this provider — the resulting listing is correct, not misleading.

### 5.2 `ProviderUnavailableError`

**Raise** when the backend state is *unknown* (credentials missing, API down, subscription unresolvable). Read paths warn-and-skip; the user sees the provider in the warning output. MUST pass curated `user_help_text` per backend; the default text tells the user to "start Docker" which is wrong for cloud auth failures. All three clouds now pass curated text: `_aws_unavailable_error` (`libs/mngr_aws/imbue/mngr_aws/backend.py`), `_gcp_unavailable_error` (`libs/mngr_gcp/imbue/mngr_gcp/backend.py`), `_azure_unavailable_error` (`libs/mngr_azure/imbue/mngr_azure/backend.py`).

### 5.3 `MngrError` subclasses

The full hierarchy (`errors.py`) covers the operational failures: `HostNotFoundError`, `HostAlreadyExistsError`, `HostNotRunningError`, `HostNotStoppedError`, `HostShutdownNotSupportedError`, `SnapshotNotFoundError`, `SnapshotsNotSupportedError`. Each carries a `user_help_text` that suggests the next action. Providers SHOULD NOT define new top-level error classes; subclass an existing one.

### 5.4 `CleanupFailedGroup`

**Raise** from `destroy_host` (and from `cleanup` commands) when partial cleanup left some resources behind. The contract is at `libs/mngr/imbue/mngr/interfaces/provider_instance.py` (`ProviderInstanceInterface.destroy_host`) and `specs/cleanup-error-aggregation.md`: aggregate every real failure into a single error, return success on benign "already gone" outcomes, attempt every teardown step even after one fails.

---

## 6. Operator commands (`mngr <provider> ...`)

The cloud trio (AWS, GCP, Azure) has converged on a `prepare` / `cleanup` operator-command group registered in the provider's CLI subgroup. New cloud providers SHOULD follow this.

### 6.1 `prepare`

**MUST be idempotent.** Re-running it MUST NOT fail if all resources already exist. Pattern: AWS `libs/mngr_aws/imbue/mngr_aws/cli.py` (`prepare`), GCP `libs/mngr_gcp/imbue/mngr_gcp/cli.py` (`prepare`), Azure `libs/mngr_azure/imbue/mngr_azure/cli.py` (`prepare`).

**MUST take `--allowed-ssh-cidr`** as the explicit operator control over network exposure. The trio defaults to the open `("0.0.0.0/0",)` with a warning (§3.1), so this flag is how an operator tightens it; the warning fires when the resolved range is `0.0.0.0/0` or empty.

**SHOULD** create only what is per-user — the security group / firewall rule / NSG, the IAM role / service account, the resource group on Azure. Per-host resources (instances, disks, public IPs) are created at `mngr create` time.

**SHOULD** document the IAM/RBAC scopes it requires in the README. AWS and GCP do; Azure's README is missing the RBAC section as of the review.

### 6.2 `cleanup`

**MUST be the inverse of `prepare`.** Same scope, opposite direction.

**MUST refuse-while-resources-exist** with a pointer to `mngr destroy <agent>`. Pattern: `Refusing to clean up… destroy them first with 'mngr destroy <agent>'`.

**MUST be tag-scoped.** A `cleanup` that touches infrastructure carrying no `mngr-*` tag is a footgun.

### 6.3 `list` (SHOULD for cloud providers with untagged-instance risk)

OVH ships `mngr ovh list` (`libs/mngr_ovh/imbue/mngr_ovh/cli.py`, `list_command`) as an operator inspection command. It surfaces all VPSes — including untagged ones — under the user's OVH account, so an operator can find a "pre-mngr" VPS that's billing without being visible in `mngr list`. AWS, GCP, Azure, Vultr would all benefit from a similar command. This SHOULD be a pluggy contract that any cloud provider implements.

---

## 7. Test coverage requirements

Every provider's test suite MUST pin the following. Tests SHOULD be parameterized cross-provider where possible (`pytest.mark.parametrize("provider_name", [...])`).

### 7.1 `mngr create` happy path

A release-tier test that creates an instance, asserts the host is RUNNING, exec'es a command, and destroys cleanly. AWS at `libs/mngr_aws/imbue/mngr_aws/test_release_aws.py` is the model.

### 7.2 Build-arg parsing unit test

Every supported flag round-trips through `_parse_build_args`. Unknown flags raise `raise_if_unknown_provider_arg`. Legacy `--vps-*` flags raise the migration error.

### 7.3 Credentials-error classification unit test

Missing creds raise `ProviderUnavailableError` (NOT `ProviderEmptyError`, NOT a bare exception). Curated `user_help_text` is present. Pattern: the credentials-error tests in `libs/mngr_azure/imbue/mngr_azure/backend_test.py`, `libs/mngr_aws/imbue/mngr_aws/backend_test.py`, `libs/mngr_gcp/imbue/mngr_gcp/backend_test.py`. Vultr and OVH currently lack this.

### 7.4 Networking-default unit test

`allowed_ssh_cidrs=()` logs a WARNING that the instance will be unreachable (it creates no ingress rule). `allowed_ssh_cidrs=("0.0.0.0/0",)` succeeds but logs a WARNING that SSH is open to the internet. Both warning branches MUST be pinned.

### 7.5 `pytest_sessionfinish` orphan scanner

Every cloud provider's `conftest.py` MUST include a `pytest_sessionfinish` that force-deletes instances tagged `mngr-pytest-launched=true` older than a TTL. Pattern: `pytest_sessionfinish` in `libs/mngr_aws/imbue/mngr_aws/conftest.py`, `libs/mngr_gcp/imbue/mngr_gcp/conftest.py`, `libs/mngr_azure/imbue/mngr_azure/conftest.py`. Vultr and OVH lack this; a killed release test leaks real billable VPSes.

### 7.6 Pytest gate for cost-safe creation in tests

`_validate_provider_args_for_create` SHOULD raise when running under pytest and `auto_shutdown_seconds` is unset. This makes "no auto-shutdown" a test-time error, not a billing surprise. Pattern: AWS `_validate_provider_args_for_create` (`libs/mngr_aws/imbue/mngr_aws/backend.py`).

### 7.7 Capability-flag pinning

One-line per-provider test that asserts each `supports_*` flag against a constant. Catches a future "I changed the base but forgot one subclass" regression. Lima, Docker, SSH already do this; AWS/Azure/GCP/Vultr/OVH SHOULD too.

### 7.8 Auto-shutdown wiring test

Currently NO provider pins that `auto_shutdown_seconds=N` actually reaches the cloud API call. Add `test_create_instance_passes_auto_shutdown_to_user_data` per provider (`client_test.py` unit-level, not release-level).

---

## 8. Anti-patterns observed in the current codebase

These are the concrete things to NOT do, with cites to current code that does them.

Still real:

- **Lying about capability.** SSH `supports_shutdown_hosts=True` at `libs/mngr/imbue/mngr/providers/ssh/instance.py` (`SSHProviderInstance.supports_shutdown_hosts`) while `stop_host` raises `NotImplementedError`. Either flip the flag or implement.
- **`supports_volumes=True` but `list_volumes` returns `[]` / `delete_volume` is a no-op.** VPS family base (`libs/mngr_vps/imbue/mngr_vps/instance.py`), inherited by AWS/GCP/Azure/Vultr/OVH. The state-bucket work added a real `get_volume_for_host` on the base `OfflineCapableVpsProvider` but left `list_volumes` / `delete_volume` unimplemented and did not split the flag. The flag still over-claims.
- **Claiming `supports_snapshots=True` under `isolation=CONTAINER`.** `docker commit` on a single VPS is not a portable snapshot; survives `mngr stop` but not `mngr destroy`. Either implement true disk snapshots, or split the flag into `supports_persistent_snapshots`, or document the gap. (Bare has no `snapshot_placement` at all and the provider raises `SnapshotsNotSupportedError` for it; §2.1.)
- **`start_host(snapshot_id=…)` / `create_host(snapshot=…)` silent no-op.** The VPS family accepts both kwargs and never references them (`libs/mngr_vps/imbue/mngr_vps/instance.py`, `VpsProvider.create_host` / `start_host`). The user thinks they restored work and lost it instead. Either honor or raise.
- **Discovery silently dropping a provider with bad creds in `mngr gc`.** `get_all_provider_instances` logs at DEBUG only (`libs/mngr/imbue/mngr/api/providers.py`). A `mngr gc` after expired AWS SSO reports "0 resources" with no warning. Bump to WARNING.
- **Tag-key style drift.** Modal uses `mngr_host_id` (underscore, `TAG_HOST_ID` in `libs/mngr_modal/imbue/mngr_modal/instance.py`); everyone else uses `mngr-host-id`. Scripts walking tags need two code paths.
- **Docker `-p :22` binding `0.0.0.0`.** Both the VPS/cloud container path (`libs/mngr_vps/imbue/mngr_vps/docker_realizer.py`) and the local docker provider (`-p :22`, `libs/mngr/imbue/mngr/providers/docker/instance.py`, `DockerProviderInstance._build_docker_run_command`). On a cloud host the cloud firewall is the perimeter, but local providers SHOULD bind `127.0.0.1`.
- **AWS `default_region` overriding `AWS_REGION` env var** because `boto3.Session(region_name=self.default_region)` is unconditional (`libs/mngr_aws/imbue/mngr_aws/config.py`). Defer to env first.
- **`--stop-host` / no offline mirror on Vultr and OVH.** They inherit `supports_shutdown_hosts=True` but do no VM-level stop, and have no stopped-host/offline-agent reconstruction. Real stop, or flip the flag and document the gap.

Resolved by the bare-providers merge (kept here as a record of what changed):

- **Auto-shutdown that doesn't stop billing (Azure).** RESOLVED. Azure idle/auto-stop now does an ARM self-deallocate via managed identity, so billing actually stops (§3.3).
- **`--stop-host` silently leaving the VM running (GCP/Azure).** RESOLVED. Both now do real machine-level stop (GCP `stop_host`, Azure `deallocate_instance`); §1.4.
- **Stopped-host discovery dropping providers without a public IP.** RESOLVED for AWS/Azure/GCP. The offline reconstruction triad was lifted into `OfflineCapableVpsProvider` plus the external state store (state bucket on AWS/Azure, GCE metadata on GCP; §1.8). Vultr/OVH still lack it.
- **Generic `ProviderUnavailableError` help text (AWS/GCP).** RESOLVED. All three clouds now pass curated `user_help_text` (`_aws_unavailable_error` / `_gcp_unavailable_error` / `_azure_unavailable_error`; §5.2).
- **Modal raising the wrong error class on missing creds.** RESOLVED. Missing creds now surface as `ProviderUnavailableError` with curated help text at construction; only a runtime auth error mid-discovery still raises `ModalAuthError` (`libs/mngr_modal/imbue/mngr_modal/backend.py`).

---

## 9. Local-vs-cloud taxonomy

| Provider | Category | Isolation | Stop semantics | Snapshots | Network exposure |
|---|---|---|---|---|---|
| modal | hosted-sandbox | n/a | terminate sandbox (rehydrates from snapshot) | yes, persistent | Modal-managed; user opts in via `--cidr-allowlist` |
| aws | cloud-VM | container + bare | real VM stop (`ec2:StopInstances`); bare powers VM off | container: docker-commit (single-host); bare: none | `0.0.0.0/0` default (with warning) |
| azure | cloud-VM | container + bare | real VM deallocate (billing stops); bare same via ARM | container: docker-commit (single-host); bare: none | `0.0.0.0/0` default (with warning) |
| gcp | cloud-VM | container + bare | real VM stop (→ `TERMINATED`); bare powers VM off | container: docker-commit (single-host); bare: none | `0.0.0.0/0` default (with warning) |
| vultr | cloud-VPS | container only | container only; VPS billing continues (gap) | docker-commit | public on boot (no managed firewall) |
| ovh | cloud-VPS | container only | container only; monthly billing (gap) | docker-commit (btrfs) | public on boot (no managed firewall) |
| lima | local-VM | container | `limactl stop` (real VM stop) | no | localhost only (no guest→host port-forward) |
| docker | local-container | container | `docker stop` (container) | yes (docker commit) | currently `0.0.0.0:rand:22` (anti-pattern, should be `127.0.0.1`) |
| ssh | BYO | n/a | NotImplementedError (anti-pattern, should be no-op) | no | user-managed |

The contractual differences across rows: hosted-sandbox providers have no "host" abstraction the user pays for separately from the agent (Modal). Cloud-VM and cloud-VPS providers have a billable host that outlives the agent — stop semantics matter for cost. The isolation column is the `IsolationMode` of the placement realizer: `container` (Docker) and/or `bare` (`isolation=NONE`, agent runs directly on the VM OS). Bare is offered only by providers with a real machine stop/start lifecycle — AWS/GCP/Azure; Vultr/OVH reject `isolation=NONE`. Local providers have no billing surface but MUST be safe by default (bind to localhost). BYO has neither a billing surface nor lifecycle ownership — it MUST be honest about that.

---

## 10. Provider implementation checklist

For an author building a new provider, a practical checklist:

- [ ] Subclass `VpsProvider` for a container-only provider, or `OfflineCapableVpsProvider` if hosts can be stopped while their disk persists (or `ProviderInstanceInterface` directly for a non-VPS shape).
- [ ] Implement `_parse_build_args` with `parse_vps_build_args(provider_prefix="--<name>-", ...)`. Reject unknown flags. Reject `--vps-*` migration flags.
- [ ] Implement `_fetch_provider_instances` returning instances tagged `mngr-provider=<self.name>`.
- [ ] Implement `_list_provider_vps_hostnames` returning SSH-reachable hostnames for those instances.
- [ ] Inherit the open `allowed_ssh_cidrs = ("0.0.0.0/0",)` default and log the open-CIDR warning when it resolves to `0.0.0.0/0` or empty. Key-only SSH is the actual control; the warning points operators at the tightening path.
- [ ] Tag every created resource with `mngr-host-id`, `mngr-provider`, `mngr-created-at`. Use dashes, not underscores.
- [ ] Implement `_validate_provider_args_for_create` to preflight any required per-user infra (firewall, IAM role, subnet).
- [ ] Add a `mngr <provider> prepare` CLI command that idempotently creates per-user infra; require `--allowed-ssh-cidr`.
- [ ] Add a `mngr <provider> cleanup` CLI command that refuses-while-resources-exist and is tag-scoped.
- [ ] Decide `supports_shutdown_hosts` honestly. If `True`, override `stop_host`/`start_host` to stop the VM; if `False`, accept that `--stop-host` will error loudly.
- [ ] Decide isolation support. Bare (`isolation=NONE`) requires a real machine stop/start lifecycle — override `_supports_bare_isolation` to `True` ONLY if you have it; otherwise the base correctly rejects bare via `BareIsolationNotSupportedError`. A bare placement reports `supports_snapshots=False` (its realizer is not snapshot-capable).
- [ ] Decide `supports_snapshots` honestly. It is realizer-derived for the VPS family (container → `True` via `docker commit`, bare → `False`). If you can't implement persistent disk snapshots for the container path, mark this clearly in the README until the `supports_persistent_snapshots` split lands.
- [ ] If hosts can be stopped, get the offline triad (host record, agent list, per-agent payloads) by extending `OfflineCapableVpsProvider` and supplying its hooks (`_offline_discovered_host_from_instance`, `_is_instance_offline`, `_offline_agent_dicts_for`, `_state_store`); back `_state_store` with a state bucket (S3/Blob) or other external store so there is no agent cap.
- [ ] Wire `auto_shutdown_seconds` so that it actually stops billing — verify in a unit test, not just the pre-create gate.
- [ ] Raise `ProviderUnavailableError` (NOT `ProviderEmptyError`, NOT a bare exception) on creds/API failure. Pass curated `user_help_text`.
- [ ] Add a `pytest_sessionfinish` orphan scanner targeting `mngr-pytest-launched=true`.
- [ ] Verify the multi-agent path: `persist_agent_data` is keyed per-agent, `list_persisted_agent_data_for_host` returns all agents, and a second `mngr exec --new-agent` doesn't clobber the first.
- [ ] Add a release test `test_create_stop_start_destroy` that exercises the full lifecycle.
- [ ] Add a release-test step for the second-agent case (`specs/provider-release-tests.md` Trip 1b).
- [ ] Add a capability-flag pinning test.
- [ ] Add a credentials-missing classification test.
- [ ] Add a build-arg parsing test (happy path + unknown-flag rejection).
- [ ] Write a README with: Setup, RBAC/IAM, Build args, Defaults, Caveats.

---

## 11. Open design questions

These are the things this spec doesn't take a position on yet. Resolving them MAY require more user research or more cross-provider discussion than a single document can drive.

1. **`supports_persistent_snapshots` flag?** Modal's snapshots and AWS/GCP/Azure's `docker commit` are different products. Either split the flag, or rename the existing `supports_snapshots` to `supports_local_snapshots` and add a new strict `supports_persistent_snapshots` for Modal. The current single-flag conflation has bitten the AWS README which contradicts the AWS code.
2. **Lift the agent-mirror pattern into `mngr_vps` base?** RESOLVED by the bare-providers merge. The per-agent tag mirror was removed entirely; the stopped-host reconstruction triad moved to `OfflineCapableVpsProvider`, backed by an external `HostStateStore`. AWS/Azure use a required state bucket (`BucketHostStateStore`, `libs/mngr_vps/imbue/mngr_vps/host_state_store.py`) with no agent cap; GCP overrides `_state_store` with a GCE metadata store. Vultr/OVH still have no offline mirror and remain open.
3. **Open-by-default ingress across the cloud trio.** RESOLVED. AWS/Azure/GCP all default `allowed_ssh_cidrs` to `("0.0.0.0/0",)` and all log the open-CIDR warning; key-only SSH is the actual control. There is no longer an AWS-vs-trio posture mismatch. (A future `security_profile` knob could still flip the default per-profile for compliance contexts — §3.1.)
4. **Cross-provider `--cpu` / `--memory` aliases?** Today only Modal accepts these as bare flags. Allowing cloud providers to accept them — resolving to the closest representative SKU — would let users move a `mngr create` command between providers without relearning the flag shape. This is a substantial API change, not a paper change.
5. **Modal lacks a `mngr modal cleanup` analog.** Add a no-op for parity, or document the gap? Modal has no per-user backend resources to clean up, so a no-op would be honest only if it printed "nothing to clean up".
6. **`mngr <provider> list` as a pluggy contract?** OVH ships one; the cloud trio would benefit. Could be a new method on `ProviderBackendInterface` or a separate plugin.
7. **For local providers (Lima, Docker, SSH), should `--auto-shutdown-seconds` be rejected at parse time** rather than silently ignored? The flag has no meaning when the user owns the compute.
8. **For SSH provider, should `mngr create --provider ssh` be rejected at config-validation time** rather than at command-execution time (then `NotImplementedError`)? A surfaced "this provider doesn't create hosts" error in `mngr create --help` would be friendlier.
9. **`supports_multi_agent_hosts` flag?** §1.8 says every provider MUST support N agents per host. The interface is plumbed for it (per-agent `persist_agent_data` / `list_persisted_agent_data_for_host` / `remove_persisted_agent_data`); Modal exercises it in production. The offline N-agent view is now much stronger: AWS/Azure (required state bucket) and GCP (metadata) reconstruct all agents while the host is stopped via `OfflineCapableVpsProvider`, with no agent cap. The remaining open part is Vultr/OVH (no offline view at all) and SSH (no offline view); a `supports_multi_agent_hosts` flag — or per-provider release-tests Trip 1b coverage — would let those providers be honest. The flag would be the parity escape hatch matching `supports_shutdown_hosts`.
