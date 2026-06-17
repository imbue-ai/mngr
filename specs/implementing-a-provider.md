# Implementing a new `mngr` provider

High-level guide for adding a new provider plugin. Use alongside `specs/provider-shape.md` (the prescriptive contract — read first), `specs/provider-uniformity-review.md` (current-state cross-provider behavior), and `specs/provider-release-tests.md` (release-test trips).

The guide is organized around the **user-visible behaviors** a provider must deliver. Each section names the behavior, the contract the user expects, and where to put the code. Backend-shape specifics — cloud VPS vs hosted sandbox vs local — only matter for "where the code lives," not for the contract.

## Before you start

A provider is a pluggable backend that allocates compute, runs an agent on it, and lets the user `mngr exec`, `mngr list`, `mngr stop`, `mngr start`, `mngr destroy`, and (if it has per-user backend resources) `mngr <yourname> prepare` / `cleanup`. The single most important user expectation is that `mngr` feels the same across providers. Where uniformity is impossible, be loud about the gap (raise, or flip a capability flag); silent no-op is the worst option.

Three common backend shapes, each with a reference implementation:

- Cloud VPS / VM (Debian on a public-IP VM). Subclass `VpsProvider` (or one of its offline-capable subclasses — see below). Reference: `libs/mngr_aws/imbue/mngr_aws/`.
- Hosted sandbox (provider-managed compute, no VM lifecycle exposed). Implement `ProviderInstanceInterface` directly. Reference: `libs/mngr_modal/imbue/mngr_modal/`.
- Local / BYO. Reference: Lima, Docker, SSH providers in-tree.

For each behavior below, the contract is identical; the implementation hooks differ by shape.

### The realizer seam (VPS shape)

A VPS provider no longer hard-codes "Debian + Docker." Once the VM is up, *how* the agent is placed on it is decided by a `HostRealizer` (`libs/mngr_vps/imbue/mngr_vps/interfaces.py`, `HostRealizer`), chosen from `config.isolation` (`IsolationMode`, `primitives.py`):

- `IsolationMode.CONTAINER` (default) → `DockerRealizer` (`docker_realizer.py`): the agent runs in a Docker container with a btrfs-backed volume; supports snapshots (`docker commit`).
- `IsolationMode.NONE` → `BareRealizer` (`bare_realizer.py`): the agent runs directly on the VM OS. No Docker, no btrfs, no snapshots; the host store lives on the root disk at `BARE_HOST_STORE_DIR` (`/var/lib/mngr-host`).

`VpsProvider._build_realizer` (`instance.py`) maps the mode to a realizer instance, and capability flags / lifecycle methods delegate to it. The realizer owns everything placement-shaped: `realize_placement`, `find_host_record`, `read_live_listing`, `collect_listing_output`, `stop_placement` / `start_placement`, `teardown_placement`, `snapshot_placement`, `idle_shutdown_command`. When adding a provider you generally do *not* write a realizer — you inherit the two shipped ones and wire your cloud's machine lifecycle around them.

### Class hierarchy (VPS shape)

- `VpsProvider` (`instance.py`) — the shared base (this is what the old "extract a base" became; there is no `BaseVpsProvider`).
- `OfflineCapableVpsProvider(VpsProvider)` — adds stopped-host reconstruction (rebuild a host record while its VM is stopped). GCP extends this directly and supplies the offline data from GCE metadata.
- `TagMirrorVpsProvider(OfflineCapableVpsProvider)` — offline mirror via cloud resource tags. AWS and Azure extend this.
- `MinimalVpsProvider(VpsProvider)` — the externally-managed path (imbue_cloud).

## Deliver: `mngr create`

User contract: provisions a host, starts one agent (unless `--no-agent`), leaves the user able to `mngr exec` into it. Build args validated; unknown / migration-flag rejected loudly. Pre-create gate refuses if a required prerequisite is missing (operator hasn't run `prepare`; pytest cost-safety not configured).

Where to put the code (VPS shape): `_parse_build_args` (compose `parse_vps_build_args(provider_prefix="--<yourname>-")` + the `extract_*` helpers; reject unknown via `raise_if_unknown_provider_arg`; reject migration flags via `raise_if_vps_migration_arg`); `_create_vps_instance`; `_validate_provider_args_for_create` (model: `libs/mngr_gcp/imbue/mngr_gcp/backend.py` — firewall preflight + project-resolution warning + pytest gate).

Where to put the code (sandbox shape): `create_host` directly. Modal does build/snapshot wiring + Volume-backed host record in `libs/mngr_modal/imbue/mngr_modal/instance.py`.

If the request asks for `isolation=NONE` and your provider can't honestly support bare placement, `create_host` rejects it via `BareIsolationNotSupportedError` (see "Deliver: isolation mode" below).

Contract spec: `provider-shape.md` §1.1.

## Deliver: isolation mode

User contract: `--<yourname>-isolation none` runs the agent directly on the VM OS instead of in a container, and either works end-to-end or is refused loudly. The default is `IsolationMode.CONTAINER`.

Bare isolation has a hard prerequisite: because there is no container to stop, the *only* way to pause billing is a real machine stop/start lifecycle. A provider must therefore not claim bare support unless its `stop_host` / `start_host` actually stop and resume the VM. `VpsProvider.create_host` enforces this — when `isolation is IsolationMode.NONE and not self._supports_bare_isolation`, it raises `BareIsolationNotSupportedError`. `_supports_bare_isolation` defaults to `False`; AWS, GCP, and Azure override it to `True` (`mngr_aws/backend.py`, `mngr_gcp/backend.py`, `mngr_azure/backend.py`). Vultr and OVH, which have no VM-level stop, leave it `False` and so reject `isolation=NONE`.

Where to put the code: override `_supports_bare_isolation` to return `True` only after your `stop_host`/`start_host` genuinely stop the machine. The placement behavior itself comes from `BareRealizer` for free — you do not write it. On bare, idle self-stop powers the VM off directly (`BareRealizer.idle_shutdown_command = "shutdown -P now"`, `idle_shutdown_stops_host = True`), so there is no sentinel-watcher indirection; on Azure the bare path runs the same managed-identity deallocate as the container path, since OS poweroff alone wouldn't stop Azure billing.

Contract spec: `provider-shape.md` §1.1.

## Deliver: `mngr list`

User contract: shows every host the user has created, in every state — RUNNING, STOPPED, CRASHED, DESTROYED (with `--include-destroyed`). Credentials missing raises `ProviderUnavailableError`, NOT a silent empty list. Per-command API hit; cached for the duration of one command.

Where to put the code (VPS shape): `_fetch_provider_instances` returning instance dicts filtered to `mngr-provider=<self.name>`; `_list_provider_vps_hostnames` returning SSH-reachable hostnames. The shared discovery flow (SSH-into-each-VPS, offline fallback) lives in `VpsProvider`.

Stopped-host visibility requires an offline mirror, and the reconstruction machinery is now in the base. Extend `OfflineCapableVpsProvider` (or `TagMirrorVpsProvider` for the tag-based variant) and supply the offline hooks — `_offline_discovered_host_from_instance`, `_is_instance_offline`, `_persisted_agent_dicts_from_instance`, `_offline_host_from_instance`. AWS and Azure get this via `TagMirrorVpsProvider` (rebuild from resource tags); GCP via `OfflineCapableVpsProvider` + GCE metadata. Without one of these, a stopped VM falls out of `mngr list` (Vultr/OVH have no offline mirror).

Contract spec: `provider-shape.md` §1.2.

## Deliver: `mngr stop` and `mngr stop --stop-host`

User contract for `mngr stop` (no flag): stops the agent's tmux session only. Compute keeps running. Uniform across all providers — this is at the API layer, not your provider.

User contract for `mngr stop --stop-host`: either (a) stop compute so the user stops paying, OR (b) refuse loudly via `HostShutdownNotSupportedError`. Silent leave-VM-running while reporting "Stopped host" is a cost leak masquerading as success.

Real machine stop has now landed on all three full clouds: AWS (EC2 `StopInstances`), GCP (`stop`/`start` the instance), and Azure (true `begin_deallocate`, which stops billing rather than just powering off the OS). Vultr and OVH remain container-only — they inherit the base `stop_host`, which pauses the placement (`docker stop`) but does not stop the VM.

How the layers fit: base `VpsProvider.stop_host` calls `self._realizer.stop_placement(...)` (a `docker stop` for the container realizer, a no-op for bare), then writes the record (optionally with `stop_reason`) and mirrors externally. Machine-level stop is layered on top by the cloud subclass overriding `stop_host`/`start_host`.

Where to put the code: override `stop_host`/`start_host` to add the VM-level stop, calling `super()` for the placement-stop + record-write. AWS pattern: `libs/mngr_aws/imbue/mngr_aws/backend.py` — `stop_instance`/`start_instance` on the client, EBS preserved. If you can't honestly stop compute, leave `supports_shutdown_hosts=False` and let the CLI refuse before the work begins (note the base `VpsProvider` defaults it to `True`, so a container-only provider that can't stop the VM must override it).

Contract spec: `provider-shape.md` §1.3, §1.4.

## Deliver: `mngr start`

User contract: idempotent; resumes a stopped host. If `--snapshot <id>` was passed, either restore from it or raise `SnapshotsNotSupportedError`. Silent no-op (current VPS-family behavior on `snapshot_id`) is the worst option.

Where to put the code: override `start_host` if your provider has VM-level stop (now done by AWS, GCP, and Azure). AWS additionally re-binds known_hosts for the new public IP after resume and re-launches the activity watcher.

Contract spec: `provider-shape.md` §1.5.

## Deliver: `mngr destroy` and `mngr <provider> cleanup`

User contract for `mngr destroy`: deletes every billable resource attached to the host. Idempotent on 404. Raises `CleanupFailedGroup` if any real resource was left behind, so the user sees the punch list. May preserve snapshots; if so, `gc_snapshots` handles them.

Where to put the code: the shared `destroy_host` in `VpsProvider` covers most VPS shapes via cloud-native cascades (`DeleteOnTermination`, `delete_option=Delete`, `auto_delete=True`); the placement-teardown steps (`remove_container`, `remove_volume`, `delete_btrfs_subvolume`) live in `DockerRealizer.teardown_placement`. Your client's `destroy_instance` is the one new method.

User contract for `mngr <provider> cleanup`: only if your provider creates per-user backend resources (security group, firewall rule, IAM role). Inverse of `prepare`; refuses while user resources exist; tag-scoped (never deletes infrastructure lacking a `mngr-*` tag). Register via the `register_cli_commands` hookimpl. If your provider has no per-user resources (Modal, local), skip this — don't add a no-op for parity.

Contract spec: `provider-shape.md` §1.6, §1.7.

## Deliver: capability flags

`supports_snapshots`, `supports_shutdown_hosts`, `supports_volumes`, `supports_mutable_tags`. These are honesty contracts the CLI branches on. `True` means the method does what users expect; `False` means it raises clearly. `True` with a no-op implementation is the worst option.

`supports_snapshots` is now derived from the realizer: `VpsProvider.supports_snapshots = self._realizer.supports_snapshots`. The container realizer reports `True` (via `docker commit`); the bare realizer reports `False` (and `snapshot_placement` raises `SnapshotsNotSupportedError`). So selecting `isolation=NONE` automatically and honestly turns the flag off — you don't set it by hand.

Lies to avoid: SSH's `supports_shutdown_hosts=True` while `stop_host` raises `NotImplementedError`; VPS-family `supports_volumes=True` while `list_volumes()` returns `[]` and `delete_volume` is a no-op (true even though AWS/Azure now have a per-host `get_volume_for_host` via the state bucket — the listing flag was never flipped); container snapshots being a `docker commit`, not a portable snapshot that survives `destroy_host`.

Contract spec: `provider-shape.md` §2.

## Deliver: error classification

User contract: every failure mode classifies into the right exception:

- Cloud creds missing / API down → `ProviderUnavailableError` with curated `user_help_text`. Default text says "start Docker" — wrong for cloud auth. Pattern: `_azure_unavailable_error` in `libs/mngr_azure/imbue/mngr_azure/backend.py`.
- Backend reachable, zero hosts → `ProviderEmptyError`. Used only when the backend has authoritatively confirmed empty (Modal: "the per-user environment doesn't exist yet").
- Host name doesn't resolve → `HostNotFoundError`.
- Operation requires capability the provider lacks → the specific error (`HostShutdownNotSupportedError`, `SnapshotsNotSupportedError`).
- Multi-resource cleanup partial failure → `CleanupFailedGroup`.

Common error path mistakes today: Vultr/OVH silently return `[]` for missing creds (should raise `ProviderUnavailableError`); Modal raises `ModalAuthError` (a `PluginMngrError`, doesn't satisfy the contract); AWS/GCP fall through to default help text.

Contract spec: `provider-shape.md` §1.9, §5.

## Deliver: N agents per host

User contract: a second `mngr exec <host> --new-agent` succeeds; both agents survive `mngr stop` / `mngr start`; `mngr list` shows both. The interface is `(host_id, agent_id)`-keyed: `persist_agent_data`, `list_persisted_agent_data_for_host`, `remove_persisted_agent_data`. Per-agent storage MUST be keyed per-agent (no single-blob packing).

Where to put the code: live discovery is a realizer method — `HostRealizer.read_live_listing` + `collect_listing_output` (and `find_host_record`) in `libs/mngr_vps/imbue/mngr_vps/interfaces.py`, implemented by `DockerRealizer` (scan inside the container) and `BareRealizer` (read the root-disk store directly). You inherit it; you don't reimplement it.

Offline mirror (showing N agents while the VM is stopped) is supplied by the base offline classes. For AWS and Azure, when a shared state bucket is configured (`BucketHostStateStore` over S3/Blob — see `host_state_store.py`), the full per-agent records live in the bucket with no size cap and the per-agent `mngr-agent-<id>-*` tags are bypassed. The per-field tag mirror (`mngr-agent-<id>-name` / `-type` / `-labels`) survives only as the no-bucket fallback, and only in that fallback does the EC2 tag-count wall (~16 agents) apply. If your cloud has a similar per-field-tag limit and no bucket, surface it with a clear `NotImplementedError` at the cap.

Contract spec: `provider-shape.md` §1.8.

## Deliver: cost safety

Cost leaks are the most expensive bug class. The user contract: `auto_shutdown_seconds` actually stops billing; idle hosts self-stop if `supports_shutdown_hosts=True`; pytest can't leak resources.

Three mechanisms, all roughly required:

- Pytest gate: `_validate_provider_args_for_create` raises when `PYTEST_CURRENT_TEST` is set and `auto_shutdown_seconds` isn't. Model: `libs/mngr_aws/imbue/mngr_aws/backend.py` (`_validate_provider_args_for_create`).
- Orphan scanner: `pytest_sessionfinish` in `conftest.py` force-deletes `mngr-pytest-launched=true` resources older than a TTL. Model: `libs/mngr_aws/imbue/mngr_aws/conftest.py`. AWS, Azure, and GCP all have one; Vultr and OVH skipped this and leak real VPSes.
- `auto_shutdown_seconds` actually terminates billing. Verify with a cloud-API probe in a release test, not just the pre-create gate.

Idle watcher (if `supports_shutdown_hosts=True`): this now works on all three full clouds, with the mechanism differing per substrate.

- AWS: container path uses an in-host watcher writing a sentinel; an outer-host systemd `.path` unit fires `shutdown -P now`, and `InstanceInitiatedShutdownBehavior` decides stop vs terminate (`_create_shutdown_script`, `_install_idle_watcher` in `mngr_aws/backend.py`).
- GCP: same sentinel + systemd pattern; the guest `shutdown -P now` lands the instance in `TERMINATED` (no billing), so no extra flag is needed (`mngr_gcp/backend.py`).
- Azure: an OS poweroff would *not* stop billing, so the idle path runs an ARM self-deallocate via the VM's managed identity + IMDS token (`_build_self_deallocate_script`, `_install_idle_watcher` in `mngr_azure/backend.py`). The self-deallocate role is created/assigned during `prepare`/`create`; if `roleAssignments/write` is missing, a warning is logged and only a manual `mngr stop` halts billing.
- Bare (`isolation=NONE`) on any cloud skips the sentinel indirection: the VM powers itself off directly via `BareRealizer.idle_shutdown_command`. On AWS/GCP that is an instance stop; Azure bare reuses the same managed-identity deallocate.

Vultr and OVH still have no idle self-stop and no orphan scanner.

Contract spec: `provider-shape.md` §3.3.

## Deliver: shared defaults

The cross-provider conventions the user relies on:

- `default_idle_timeout = 800` seconds.
- 30 GB default disk.
- `allowed_ssh_cidrs = ("0.0.0.0/0",)` with a runtime warning (key-only SSH is the actual control).
- `debian:bookworm-slim` default container image; pin a specific OS image SKU.
- Tag every resource with `mngr-host-id`, `mngr-provider`, `mngr-created-at`, `mngr-pytest-launched`. Dashes, not underscores (Modal uses underscores; don't copy that).
- Per-host SSH key stored under `<profile>/providers/<yourname>/<instance-name>/keys/`.
- Container ports never exposed directly on `0.0.0.0` of a public IP without the cloud firewall in front.

Contract spec: `provider-shape.md` §3.

## Tests

- Unit tests: config parsing; build-arg parsing (happy path + unknown-flag + `--vps-*` migration); capability-flag pinning; credentials-error classification; cross-region refusal; networking warnings; `auto_shutdown_seconds` flowing through to the cloud API.
- Release tests (`test_release_<yourname>.py`, `@pytest.mark.release`): follow the trip structure in `specs/provider-release-tests.md`. Trip 1 = lifecycle + sketchy-kill + gc; Trip 1b = second agent; Trip 2 = auto-shutdown; Trip 3 = snapshot survives destroy; Trip 4 = error classification.
- Mock fidelity: stub at your client class's surface, not at the cloud SDK. Pattern: `_FakeEc2Client` in `libs/mngr_aws/imbue/mngr_aws/testing.py`.

## Documentation

`libs/mngr_<yourname>/README.md`: Setup (credentials), Build args, RBAC/IAM scopes for `prepare` / `create` / `cleanup`, Multi-region behavior, Defaults, Caveats (anywhere you diverge from the shape doc — be explicit).

Changelog: `libs/mngr_<yourname>/changelog/<branch-name>.md` (slashes → dashes). CI fails without it.

## Common gotchas

- `boto3.Session(region_name=self.default_region)` silently overrides `AWS_REGION`. Defer to env first.
- Disk-size field naming varies (`root_volume_size_gb` / `os_disk_size_gb` / `boot_disk_size_gb`). Use the cloud's own term.
- `start_host(snapshot_id=…)` and `create_host(snapshot=…)` are silently ignored everywhere except Modal and Docker. Honor or raise `SnapshotsNotSupportedError`.
- Vultr/OVH have no managed firewall and no `allowed_ssh_cidrs` field — VPS is public-internet-reachable as soon as it boots. New cloud providers should ship managed-firewall integration (AWS has `allowed_ssh_cidrs` on `AwsProviderConfig`, threaded to the security group).
- AWS's per-agent tag mirror hits the EC2 50-tag wall around 16 agents and raises `NotImplementedError` — but only in the no-bucket fallback. With the S3/Blob state bucket configured (the default on AWS/Azure), per-agent records go in the bucket and this cap does not apply. If your cloud has no bucket and uses per-field tags, surface the limit the same way.

## References

- `specs/provider-shape.md` — the contract.
- `specs/provider-uniformity-review.md` — current-state cross-provider behavior.
- `specs/provider-release-tests.md` — release-test trip proposal.
- `specs/bare-providers/` — the realizer seam, `IsolationMode`, and bare placement.
- `specs/provider-state-bucket/` — the shared S3/Blob offline state bucket (AWS/Azure).
- `libs/mngr_aws/imbue/mngr_aws/` — reference cloud-VPS provider.
- `libs/mngr_modal/imbue/mngr_modal/` — reference hosted-sandbox provider.
- `libs/mngr_vps/` — shared base (`VpsProvider`, `HostRealizer`, realizers, host state store).
