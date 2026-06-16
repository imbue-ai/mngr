# Stage 1 extraction design: the `HostRealizer` seam

Implementation-level companion to `spec.md`. Defines the exact contract and
per-method migration for splitting `VpsDockerProvider` into a substrate
(`VpsDockerProvider`, soon `mngr_vps`) plus a swappable `HostRealizer`, with no
behavior change for the existing Docker path. All line numbers refer to
`libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py` unless noted.

## State ownership: what moves vs. what stays

The provider's per-instance state derives from `_key_dir()`
(`profile_dir/providers/<backend>/<name>/keys`). The split:

| Concern | Owner | Source |
| --- | --- | --- |
| VPS keypair / host keypair / `vps_known_hosts` | **substrate** | `_get_vps_ssh_keypair` 497, `_get_vps_host_keypair` 505, `_vps_known_hosts_path` 513 |
| `_make_outer_for_vps_ip`, `record_outer_host_key` | **substrate** | 538, 516 |
| VPS provisioning, cloud-init, instance lifecycle | **substrate** | `_provision_vps` 985, `_create_vps_instance` 924, `_validate_provider_args_for_create` 474 |
| host-record persistence, discovery, tags | **substrate** | host store + discovery helpers |
| container keypair / container host keypair / `container_known_hosts` | **realizer** | `_get_container_ssh_keypair` 501, `_get_container_host_keypair` 509, `_container_known_hosts_path` 531 |
| agent Host construction (SSH endpoint + key) | **realizer** | `_create_host_object` 578 |
| container build/run/ssh, btrfs, snapshot helper | **realizer** | `_setup_container_on_vps` 1073, `_setup_container_ssh` 665, `_prepare_btrfs_on_outer` 695 |
| container stop/start/rm, sshd re-exec, activity watcher | **realizer** | `stop_container`/`start_container`/`remove_container`, `_start_activity_watcher` 1408, `_wait_for_container_sshd` 1300 |
| snapshot create/delete (`docker commit`/`rmi`) | **realizer** | `create_snapshot` 2202, `delete_snapshot` 2258 |

The realizer is constructed with what it needs: `config`, `mngr_ctx`, and the
`key_dir` path (so it can load its own container keypairs). It does **not** hold a
back-reference to the provider.

## Realizer construction: internal, config-selected, not injected

The base provider builds its own realizer from `config.isolation`; subclasses
(`aws`/`gcp`/`azure`/`vultr`/`ovh`) are **unchanged** for the Docker path -- they
do not pass a realizer. This keeps Stage-1's blast radius to `instance.py`, the new
realizer files, and `config.py` (add `isolation`).

```python
# in VpsDockerProvider, a cached_property or model_validator:
def _build_realizer(self) -> HostRealizer:
    match self.config.isolation:
        case IsolationMode.CONTAINER:
            return DockerRealizer(config=self.config, mngr_ctx=self.mngr_ctx, key_dir=self._key_dir())
        case IsolationMode.NONE:
            # Stage 1 keystone ships only the Docker path; BareRealizer lands in step 3.
            raise BareIsolationNotYetSupportedError(...)
        case _ as unreachable:
            assert_never(unreachable)
```

`IsolationMode` (`CONTAINER` | `NONE`) goes in `primitives.py`; `isolation:
IsolationMode = Field(default=IsolationMode.CONTAINER, ...)` goes on the provider
config. Default `CONTAINER` => no behavior change. (`IsolationMode` names the
isolation *level*, leaving room for a future `GVISOR`/`SANDBOXED` value that folds
the current `docker_runtime = "runsc"` knob into the same enum.)

## `HostRealizer` interface

`MutableModel, ABC` (per style guide). Methods mirror the existing seam so the
Docker implementation is a near-verbatim move. Signatures:

```python
class HostRealizer(MutableModel, ABC):
    """Places an agent on a booted VPS and manages the agent placement lifecycle."""

    config: VpsDockerProviderConfig = Field(frozen=True, ...)
    mngr_ctx: MngrContext = Field(frozen=True, ...)
    key_dir: Path = Field(frozen=True, ...)

    @property
    @abstractmethod
    def supports_snapshots(self) -> bool:
        """Whether this realizer can snapshot a placement."""

    @abstractmethod
    def realize_placement(self, ctx: RealizePlacementContext) -> RealizedPlacement:
        """Build/run the agent placement on the booted VPS; return record + host-build data."""

    @abstractmethod
    def build_agent_host(self, host_id: HostId, host_name: HostName, vps_ip: str,
                         placement: RealizedPlacement) -> Host:
        """Construct the agent Host object (SSH endpoint + key) for a placement."""

    @abstractmethod
    def stop_placement(self, outer: OuterHostInterface, record: VpsDockerHostRecord) -> None:
        """Pause the placement on the machine (docker: `docker stop`; bare: no-op)."""

    @abstractmethod
    def start_placement(self, outer: OuterHostInterface, record: VpsDockerHostRecord) -> None:
        """Resume the placement on a running machine (docker: start + re-exec sshd + watcher)."""

    @abstractmethod
    def teardown_placement(self, outer: OuterHostInterface, host_id: HostId,
                           record: VpsDockerHostRecord) -> None:
        """Remove the placement and its per-host storage (no VPS-client calls)."""

    @abstractmethod
    def snapshot_placement(self, outer: OuterHostInterface, record: VpsDockerHostRecord,
                           name: SnapshotName) -> SnapshotId:
        """Create a placement snapshot; raise SnapshotsNotSupportedError if unsupported."""

    @abstractmethod
    def delete_snapshot_placement(self, outer: OuterHostInterface, snapshot_id: SnapshotId) -> None:
        """Delete a placement snapshot."""
```

Seam data types (frozen, in `data_types.py`):

- `RealizePlacementContext` -- the inputs `_setup_container_on_vps` needs today:
  `outer, host_id, name, vps_ip, base_image, effective_start_args,
  docker_build_args, git_depth, tags, known_hosts, authorized_keys`.
- `RealizedPlacement` -- what the realizer returns for the base to (a) build the
  record and (b) construct the Host. Carries the agent SSH endpoint
  (`agent_ssh_port`, `agent_ssh_user`) and the realizer-owned record fields
  (`container_name`, `container_id`, `volume_name` for docker; all `None` for
  bare). The base copies these into `VpsHostConfig` / `VpsDockerHostRecord`.

## Host-record evolution

Keep the single `VpsDockerHostRecord` / `VpsHostConfig`
(`host_store.py:36-57`) for Stage 1 -- do **not** split into a discriminated union
yet (that would churn the S3/Blob state buckets and tag offline-discovery). Make
the placement-specific fields nullable so a bare record is representable:

- `VpsHostConfig.container_name: str` -> `str | None = None`
- `VpsHostConfig.volume_name: str` -> `str | None = None`

`image` (43) and `container_id` (57) are already `| None`. The Docker realizer
asserts these are non-None where it reads them (it always sets them). A
discriminated-union `placement_config` is a noted future cleanup, not Stage 1.

## Per-method migration

| Today (provider) | After |
| --- | --- |
| `create_host` 714 | unchanged orchestration; still calls `create_host_on_existing_vps` |
| `create_host_on_existing_vps` 812 | builds `RealizePlacementContext`, calls `realizer.realize_placement`, then `_finalize_host_creation` |
| `_setup_container_on_vps` 1073 | -> `DockerRealizer.realize_placement` |
| `_setup_container_ssh` 665, `_prepare_btrfs_on_outer` 695, `_wait_for_container_sshd` 1300, `_start_activity_watcher` 1408 | -> private methods on `DockerRealizer` |
| `_create_host_object` 578 | -> `realizer.build_agent_host` (docker: `container_ssh_port` + container key; bare: 22 + vps key) |
| `_finalize_host_creation` 1189 | stays on base; takes `RealizedPlacement`, calls `realizer.build_agent_host` |
| `stop_host` 1358 | base writes record; the container stop becomes `realizer.stop_placement`. AWS/GCP/Azure overrides (`super().stop_host()` + `stop_instance`) unchanged |
| `start_host` 1417 | base reads record + rebuilds host via `realizer.build_agent_host`; container start/sshd/watcher become `realizer.start_placement` |
| `destroy_host` 1479 | base does VPS-client + known_hosts + record cleanup; container/btrfs/volume removal becomes `realizer.teardown_placement` |
| `teardown_container_on_existing_vps` 885 | -> `realizer.teardown_placement` (imbue_cloud slow-path caller updated) |
| `create_snapshot` 2202 / `delete_snapshot` 2258 | -> `realizer.snapshot_placement` / `delete_snapshot_placement` |
| `supports_snapshots` 430 | returns `self._realizer.supports_snapshots` |

`imbue_cloud` calls `create_host_on_existing_vps` and
`teardown_container_on_existing_vps` directly (its slow path rebuilds containers on
leased pool VMs); those entry points keep their signatures, so imbue_cloud is
unaffected.

## Commit sequence (intra-PR)

1. Add `IsolationMode` + `isolation` config + `HostRealizer` interface + seam data
   types + `DockerRealizer` (verbatim move of the container logic) + base
   delegation (the `IsolationMode.NONE` arm raises until step 3). Existing tests
   green, zero behavior change. **Keystone.**
2. Make `VpsHostConfig.container_name`/`volume_name` nullable; assert-non-None in
   `DockerRealizer`.
3. `BareRealizer`: agent-runtime host-setup step, systemd watcher unit, bare host
   store, `build_agent_host` at `vps_ip:22`, `realize_placement` with no container.
4. Reject Docker-only inputs when `isolation=NONE` (image/build args, gVisor,
   `docker_runtime`, `default_start_args`).
5. Wire AWS bare (vertical slice) + tests; then GCP/Azure.
6. Mechanical `mngr_vps_docker` -> `mngr_vps` rename (package, 73 import sites,
   `VpsDockerProviderConfig` -> `VpsProviderConfig`, pyproject/workspace, docs).

Step 1 is the only one that touches the shared 2400-line file structurally; it is
behavior-preserving and fully covered by the existing `mngr_vps_docker` unit tests
plus each provider's suite.

## Implementation notes (as landed in step 1)

A few refinements over the sketch above, forced by the imbue_cloud override
surface (the slice provider rebuilds containers on leased VMs through the base
setup path and customizes the *connect* side via dynamically forwarded ports):

- **`agent_endpoint` instead of `build_agent_host`.** The realizer returns an
  `AgentEndpoint` (host/port/key/known_hosts/user); the provider still owns
  `_create_host_object` (Host construction, the cache, and the certified-data
  callback). This keeps `_create_host_object` an override point -- the imbue_cloud
  slice provider overrides it for its forwarded port -- and avoids giving the
  realizer a provider back-reference.
- **`_wait_for_container_sshd` stays on the provider.** The wait moved *out* of the
  realizer's `realize_placement` and up into `create_host_on_existing_vps`, so the
  slice provider's override (which waits on a dynamically forwarded port) is still
  honored. The container's `run_container` port mapping uses
  `config.container_ssh_port` (VM-internal) even for slices, so that part moved
  into the realizer safely.
- **Host-record fields stayed required (step 2 deferred).** Making
  `VpsHostConfig.container_name`/`volume_name` nullable cascades `str | None`
  through every `open_host_store`/container call site, so it is kept as its own
  step. `_finalize_host_creation` asserts the realizer set them (the Docker
  realizer always does).
- **`teardown_container_on_existing_vps` left on the provider** (Docker-only
  imbue_cloud slow-path helper); routing it through the realizer is deferred to
  when a bare equivalent is needed.
- **Container key-file names** (`container_ssh_key` / `container_host_key` /
  `container_known_hosts`) are now module constants in `docker_realizer.py`; the
  provider's `_get_container_*` accessors (kept for the slice override) and the
  realizer both reference them, so there is a single source of truth.
