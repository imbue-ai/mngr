# Bare Providers and the Substrate x Realizer Architecture

Status: **Design proposal.** No code yet. Captures the architecture for running
agents *directly on a cloud VM* (no Docker container) and the broader refactor
that makes "with Docker" vs "without Docker" an explicit, reusable axis shared by
every provider. Target branch: `mngr/bare-providers` (base: `mngr/volumes`).

Audience: contributors to the `mngr` provider layer.

## Motivation

Today every cloud provider (`aws`, `gcp`, `azure`, plus `vultr`/`ovh` and
`imbue_cloud`) runs the agent inside a Docker container on the provisioned VM.
Docker is used "purely as a consistent provisioning mechanism"
(`specs/vps-docker-provider/spec.md`): it guarantees a uniform environment
regardless of the VM's base OS, and it gives a cheap in-place stop/start
(`docker stop`) and snapshot (`docker commit`).

We want a second shape for these providers: run the agent **directly on the VM's
operating system**, with no container. The motivations are:

- **Native performance and full-host access.** Trusted agents that want the whole
  machine -- no container overhead, no namespace quirks, direct access to devices
  and the host network.
- **Drop the Docker dependency.** No Docker Engine install on the VM means a
  simpler, faster boot and fewer moving parts.

Bare is explicitly *not* strictly better than Docker -- it trades away container
isolation (including the gVisor `runsc` option) and arbitrary-image portability.
It is the right shape for trusted agents; Docker remains the right shape when
isolation matters. We therefore want **both shapes available per provider**,
selected by configuration, not a replacement.

The key observation that makes this cheap: **the "outer" host is already a bare
host.** Every cloud provider already SSHes into the VM itself -- exposed as an
`OuterHost` via `outer_host_for(host_id)` (`specs/expose-outer-host/concise.md`)
-- to run Docker commands. The Docker shape then reaches the agent at a *second*
SSH endpoint, the container's `sshd` on `vps_ip:container_ssh_port`. A bare
provider simply **promotes the outer to be the agent host** and deletes the
container layer.

## Conceptual model: two orthogonal axes

Provider behavior decomposes into two independent axes. Every existing provider is
a point in the resulting grid.

- **Substrate** -- *where the machine is and how its lifecycle works.* Provision a
  machine, expose it as an `OuterHost`, and manage *machine*-level lifecycle
  (stop-the-machine, start-the-machine, destroy). Examples: your local computer,
  a cloud VM (AWS/GCP/Azure/Vultr/OVH), a Lima VM, a pre-existing SSH box.
- **Realization** -- *how the agent sits on that machine and how its lifecycle
  works.* Place the agent on the machine and manage *agent*-level lifecycle (stop
  the placement, snapshot it). Two realizations: **bare** (agent on the OS) and
  **docker** (agent in a container on the machine).

|                       | **bare** (agent on the OS)        | **docker** (agent in a container)            |
| --------------------- | --------------------------------- | -------------------------------------------- |
| **your computer**     | `local`                           | `docker` (local daemon)                      |
| **a local VM**        | `lima`                            | --                                           |
| **a pre-existing box**| `ssh`                             | `docker` (remote daemon via `DOCKER_HOST`)   |
| **a cloud VM**        | aws/gcp/azure **bare** (proposed) | aws/gcp/azure (today), vultr, ovh, imbue_cloud |

Read this way, the proposed work is *not* a new provider -- it is the missing
"bare" column for the cloud-VM row. And the user-visible relationship
`local : docker :: aws-bare : aws-docker` is literal: local-vs-docker is the same
realization axis, applied to the "your computer" substrate.

## What already exists (so this design is grounded, not green-field)

The codebase has already built most of the substrate axis and the
instance-lifecycle primitives that bare mode needs. This spec composes them; it
does not reinvent them.

- **`OuterHost` / `outer_host_for`** (`specs/expose-outer-host/concise.md`):
  `OuterHost` is the base class with the safe subset of host operations
  (`execute_*_command`, `read_file`/`write_file`, locking, env vars, SSH info,
  `is_local`). `Host IS-A OuterHost`. `ProviderInstanceInterface.outer_host_for(host_id)`
  yields the machine's outer host or `None`. This is exactly the substrate-side
  "give me the machine" primitive; for bare, the agent host *is* the outer host.
- **Instance stop/start** (`specs/aws-ec2-stop-start-lifecycle/spec.md`,
  `specs/gcp-azure-stop-start-lifecycle/spec.md`): `stop_instance` /
  `start_instance` on the cloud clients (AWS landed; GCP/Azure in progress),
  with `start_instance` returning the possibly-new public IP and `start_host`
  rebinding `known_hosts` to it. Bare mode's stop/start *is* instance stop/start.
- **Host-side systemd idle poweroff** (same specs): a host-side systemd `.path`
  unit watches a sentinel and powers the box off when idle; the cloud's
  shutdown-behavior turns the poweroff into a stop (not a terminate). Bare reuses
  this verbatim -- only the *writer* of the sentinel moves out of the container.
- **Tag-based offline discovery** (same specs): host + per-agent metadata is
  mirrored into instance tags so a stopped, SSH-unreachable box still appears in
  `mngr list`, resolves by name, and resumes. Realizer-independent; bare inherits it.
- **`host_setup.py`** (`build_host_setup_steps`, `apply_host_setup_on_outer`):
  idempotent, ordered host-provisioning steps run both via cloud-init at first
  boot and over SSH on an existing box. Bare adds an agent-runtime step and drops
  the Docker-install step.
- **`VpsClientInterface`**: already the cloud axis as composition (AWS/GCP/Azure
  inject a concrete client). Bare needs nothing new here beyond the stop/start
  methods that the lifecycle specs already add.

The one piece that is genuinely *not* yet shared: there are **two separate Docker
implementations.** The local `docker` provider drives the daemon through the
Python Docker SDK; `mngr_vps_docker` drives it as `docker ...` commands over the
outer host's SSH. Unifying them is Stage 2 work (below), not a prerequisite for
bare cloud providers.

## Target architecture

Introduce a `HostRealizer` seam and treat realization as a component injected into
the provider, the same way the `VpsClient` already is. The provider composes a
substrate (which it largely already is) with a realizer.

### `HostRealizer`

A realizer owns everything about *placing the agent on a machine and managing the
agent placement's lifecycle*. It is defined against `OuterHostInterface` only --
never against VPS- or cloud-specific types -- so the same realizer can later run on
any substrate (a local outer, an SSH outer, a VPS outer).

```python
class HostRealizer(ABC):
    """Places an agent on a machine (reached via its outer host) and manages
    the agent placement's lifecycle. Substrate-agnostic: talks only to
    OuterHostInterface plus the machine-capability hooks passed in."""

    @abstractmethod
    def realize_host(self, ctx: RealizeContext) -> RealizedHost:
        """Given a booted machine (its outer host) plus host id/name/image/keys,
        make the agent reachable and return how to construct its Host object
        (SSH endpoint + key) and any realizer-owned record fields."""

    @abstractmethod
    def stop_placement(self, outer: OuterHostInterface, record: HostRecord) -> None:
        """Stop the agent placement *on* the machine (docker: `docker stop`;
        bare: no-op -- the machine stop is what pauses a bare agent)."""

    @abstractmethod
    def start_placement(self, outer: OuterHostInterface, record: HostRecord) -> None:
        """Resume the placement on a running machine (docker: `docker start` +
        re-exec sshd + relaunch watcher; bare: ensure the agent systemd unit is up)."""

    @abstractmethod
    def teardown_placement(self, outer: OuterHostInterface, record: HostRecord) -> None:
        """Remove the placement and its per-host storage (docker: container +
        volume + subvolume; bare: the agent data dir / systemd unit)."""

    @property
    @abstractmethod
    def placement_supports_snapshots(self) -> bool: ...
```

`DockerRealizer` is the current `mngr_vps_docker` container logic
(`_setup_container_on_vps`, container stop/start, `docker commit`, the btrfs
unified volume, the snapshot helper) moved behind this interface, unchanged in
behavior. `BareRealizer` is the new implementation (next section).

`RealizeContext` carries the outer host, host id/name, the resolved image, the
agent SSH keys, and a handle to the substrate's machine-capability hooks (so a
realizer that needs a machine-level operation -- e.g. bare's future disk snapshot
-- asks the substrate rather than reaching for a cloud client directly).

### Substrate / machine capabilities

For Stage 1 the substrate stays as it is today: `BaseVpsProvider` (extracted from
`VpsDockerProvider`) owns VPS provisioning, the outer host (`outer_host_for`),
key/known-hosts management, state-bucket persistence, discovery/tagging, and
machine-level stop/start/destroy (delegating to the `VpsClient`). We do **not**
promote substrate to a standalone interface in Stage 1; we only ensure the
realizer never reaches around it.

Machine-level vs placement-level lifecycle is the crucial split:

| Operation | Owner | Docker shape | Bare shape |
| --- | --- | --- | --- |
| stop the **machine** | substrate | optional (AWS already does) | **primary** stop mechanism (instance stop) |
| stop the **placement** | realizer | `docker stop` | no-op |
| snapshot | realizer (may call substrate) | `docker commit` + btrfs | deferred (cloud disk snapshot) |
| destroy machine | substrate | destroy VPS | destroy VPS |
| teardown placement | realizer | container + volumes | agent dir / unit |

`stop_host` composes them: `realizer.stop_placement(...)` then, when configured,
the substrate's machine stop. For bare, `stop_placement` is a no-op and the machine
stop does the real work -- which is precisely the instance stop/start the
lifecycle specs already implement.

### Provider composition

`AwsProvider` / `GcpProvider` / `AzureProvider` remain the cloud subclasses that
own their `VpsClient`, state bucket, config, and the small cloud-specific hooks
(`_create_vps_instance`, `_validate_provider_args_for_create`, `_parse_build_args`,
`_fetch_provider_instances`). They gain one responsibility: construct themselves
with the realizer their config selects.

```python
realizer = BareRealizer(...) if config.isolation is IsolationMode.NONE else DockerRealizer(...)
```

This keeps the grid to **3 clients x 2 realizers composed at config time**, not a
3x2 class matrix. Adding a fourth cloud, or a third realization, stays O(1).

### Capability composition

`supports_snapshots` / `supports_shutdown_hosts` stop being constants and become a
function of (substrate, realizer): e.g. `supports_snapshots =
realizer.placement_supports_snapshots or substrate.supports_machine_snapshots`.
For Stage 1: docker realizer -> snapshots yes; bare realizer -> no (deferred);
machine stop -> yes on all three clouds.

## The `BareRealizer` (the concrete near-term work)

Because the motivations are native performance + dropping Docker, and snapshots are
out of scope for v1, the bare realizer gets to *remove* the three hardest pieces of
the Docker realizer rather than reimplement them.

### Agent placement

- The agent runs as a process tree on the VM's OS as `root`, reached over SSH at
  `vps_ip:22` -- i.e. the outer host *is* the agent host. `Host` construction
  reuses the existing `_create_host_object` path, pointed at port 22 with the VPS
  key instead of `container_ssh_port` with the container key. (A dedicated non-root
  agent user / separate `sshd` is a deliberate non-goal for v1.)
- `host_dir` becomes a path on the VM's root disk (e.g. `/var/lib/mngr`) instead
  of `/mngr` inside a container-mounted volume.
- A **bare host store** reads/writes `host_state.json` and agent records directly
  in that directory, replacing the Docker-volume-bind-path resolution
  (`open_host_store` / `docker volume inspect`) the Docker realizer uses. Online
  discovery reads this store from the running VM (no `docker inspect`); offline
  discovery for a stopped VM reuses the realizer-independent instance-tag channel.

### Host setup: one addition, several removals

`BareRealizer` controls the host-level provisioning steps:

- **Add**: install mngr's own host needs on the VM -- the `ssh_host_setup` packages
  (tmux, etc.), `sshd` config, the activity watcher, and the shutdown script -- as a
  `HostSetupStep` run via the existing cloud-init / `apply_host_setup_on_outer`
  machinery. (Agent-specific deps come from the VM image and/or the post-create
  setup script, not from this base step -- see Decisions.)
- **Add**: run the user's optional **post-create setup script** on the VM after the
  base setup, via the same `apply_host_setup_on_outer` path. This is the bare analog
  of a `Dockerfile`.
- **Add**: a systemd unit that owns the **idle/activity watcher**, so it survives an
  instance stop/start and a reboot. This replaces the Docker realizer's `docker
  exec`-backgrounded watcher. The agent reuses the VM's existing port-22 `sshd`
  (no separate agent `sshd`). The host-side systemd `.path` poweroff unit from the
  lifecycle specs is reused unchanged; only the sentinel *writer* moves from the
  container to this unit.
- **Remove**: the Docker Engine install step, the gVisor step, the btrfs loop FS +
  per-host subvolume + bind-volume, the snapshot helper, and the
  snapshot-trigger volume. None are needed without a container or v1 snapshots.

### Lifecycle

| mngr op | Bare behavior |
| --- | --- |
| `create_host` | Provision VM (substrate), install agent runtime + systemd unit (realizer), write `host_state.json` to the root disk, construct `Host` at `vps_ip:22`. |
| `stop_host` | `stop_placement` is a no-op; substrate stops the **instance** (compute billing ends; root disk persists). Reuses the landed instance-stop path. |
| `start_host` | Substrate starts the instance, rebinds `known_hosts` to the new IP; systemd brings the agent + watcher back. |
| `destroy_host` | Substrate destroys the VM. No container/volume/subvolume cleanup. |
| idle timeout | Host-side systemd poweroff -> instance stop (existing mechanism). |

### Configuration surface

```python
class VpsProviderConfig(ProviderInstanceConfig):  # renamed from VpsDockerProviderConfig in Stage 1
    isolation: IsolationMode = Field(default=IsolationMode.CONTAINER, ...)  # CONTAINER | NONE
```

`isolation` is per provider-instance, so a user can configure both an `aws`
(`isolation = "container"`) and an `aws-bare` (`isolation = "none"`) instance.
`IsolationMode` names the isolation *level* as the axis, which leaves clean room
for a future `GVISOR`/`SANDBOXED` value that folds today's separate
`docker_runtime = "runsc"` knob into the same enum; Stage 1 ships only
`CONTAINER` | `NONE`. Inputs whose meaning is Docker-specific must fail fast and
clearly when `isolation = none`:

- `image` changes meaning: in Docker mode it is a Docker image; in bare mode there
  is no container, so a Docker image reference or `docker build` args are rejected
  with a clear error. The bare analog is the **VM image** (a cloud-specific build
  arg such as `--aws-ami=`, exactly as today) optionally layered with a
  **post-create setup script** (see Decisions) -- together the bare equivalent of
  "base image + `Dockerfile`".
- gVisor / `docker_runtime` / `default_start_args` (passed to `docker run`) are
  rejected in bare mode.

### What bare drops (explicit non-capabilities for v1)

- **No container isolation** (and no gVisor). Trusted-agent use only.
- **No arbitrary image / `docker build`.** The agent runs on the VM's OS image.
- **No snapshots in v1.** `supports_snapshots = False` for bare; `stop_host` skips
  the pre-stop snapshot. Cloud disk snapshots are a follow-up.

## Per-cloud work

Thanks to the landed/in-progress instance-lifecycle work, the per-cloud surface for
bare is small:

- **AWS**: `stop_instance` / `start_instance` / tag discovery / systemd poweroff
  already landed. Bare needs only the agent-runtime step and the bare host store
  wired through the realizer.
- **GCP / Azure**: instance stop/start + offline discovery are in progress on the
  stop/start-lifecycle branch; bare depends on those landing but adds nothing new
  to the clients.

## Staged rollout

The design target is the full grid, but it ships in stages so each PR is
reviewable.

**Stage 1 -- bare cloud providers (this branch).**
1. Extract `BaseVpsProvider` from `VpsDockerProvider`: VPS provisioning, outer
   host, key/known-hosts, state bucket, discovery/tagging, machine stop/start/destroy.
2. Introduce the `HostRealizer` seam; move the existing container logic into
   `DockerRealizer` with no behavior change. `vultr`, `ovh`, and `imbue_cloud`
   also extend `VpsDockerProvider` (and `imbue_cloud` overrides the create path to
   rebuild containers on leased pool VMs), so this step must preserve their Docker
   behavior; they stay `mode=docker` and gain only the seam.
3. Implement `BareRealizer` (agent runtime install, systemd unit, bare host store,
   no snapshots).
4. Add `isolation: container | none` config; reject Docker-only inputs when `isolation = none`.
5. Wire `aws` / `gcp` / `azure` to select the realizer from config; land bare on
   all three (no snapshots).
6. Tests across all three clouds x both shapes; docs; per-project changelogs.

Recommended within Stage 1: land the seam + AWS bare end-to-end first as a vertical
slice (AWS already has the full instance-lifecycle stack), then GCP/Azure, to prove
the seam before fanning out.

**Stage 2 -- the grand unification (follow-up).**
1. Promote "substrate" to an explicit interface (provision/outer/machine-lifecycle).
2. Retrofit `local`, `lima`, and `ssh` as substrates paired with `BareRealizer`;
   `local`/`docker` literally become `(local, bare)` vs `(local, docker)`.
3. Consolidate the two Docker implementations into the single `DockerRealizer`,
   standardizing on `docker ...` over the outer host (works for a local outer and
   an SSH outer alike) and retiring the local Python-SDK path.

Stage 2 is high-value (kills the duplication, turns the matrix into
O(substrates) + O(realizers)) but high-churn; it is deliberately deferred until the
realizer seam is proven by Stage 1.

## Tradeoffs and non-goals

- **Isolation regression is intentional and scoped.** Bare is for trusted agents.
  The Docker shape remains the default and the only isolation-bearing option; this
  spec does not remove or weaken it.
- **Not cost-motivated, but cost-aligned.** Bare's instance-stop pauses compute
  billing (vs docker-stop, which keeps the VM billed) -- a side benefit, not the
  driver.
- **Stage 2 is not promised by Stage 1.** Stage 1 must leave `local`/`docker`/
  `lima`/`ssh` untouched and working.

## Decisions

- **Rename `mngr_vps_docker` -> `mngr_vps`.** "docker" no longer names the shared
  VPS substrate once a bare realizer exists. Done **bundled with the realizer
  work** (the package dir, imports across every cloud provider,
  `VpsDockerProviderConfig` -> `VpsProviderConfig`, entry points, and docs all move
  in the Stage 1 PR alongside the seam).
- **Bare agent identity: `root` over the VM's existing port-22 `sshd`.** The agent
  host is the outer host; no dedicated agent user or second `sshd` in v1. Matches
  the container's root and keeps placement to "construct a `Host` at `vps_ip:22`
  with the VPS key."
- **Bare dependency provisioning: prebaked image *and* a post-create setup
  script.** The two together are the bare analog of "base image + `Dockerfile`":
  - The user may point at a prebaked VM image (`--aws-ami=` etc.) with their
    agent's deps baked in (fast boot).
  - A user-supplied post-create **setup script** runs on the VM via the existing
    host-setup machinery (`apply_host_setup_on_outer`) after the base setup -- the
    bare analog of a `Dockerfile`, layerable on top of any base image.
  - Base host setup still installs only mngr's own needs (`sshd`, tmux, the
    activity watcher + shutdown script via `ssh_host_setup`); everything
    agent-specific comes from the image and/or the setup script.

## Open questions

1. **Snapshot follow-up shape** (not a Stage 1 blocker -- snapshots are deferred).
   When snapshots arrive for bare, prefer cloud disk snapshots (durable, slow,
   per-cloud) or btrfs-on-root (fast, local, lost on VM destroy)? Affects whether
   bare provisions a separate data disk up front.

## Related specs

- `specs/vps-docker-provider/spec.md` -- the Docker shape this generalizes. Note its
  "Single mode of operation" framing is superseded by the realizer axis here (see
  `specs/uncertainties.md`).
- `specs/expose-outer-host/concise.md` -- the `OuterHost` / `outer_host_for`
  substrate primitive bare builds on.
- `specs/aws-ec2-stop-start-lifecycle/spec.md`,
  `specs/gcp-azure-stop-start-lifecycle/spec.md` -- the instance stop/start, systemd
  idle poweroff, and tag offline-discovery that bare's machine lifecycle reuses.
- `specs/lima-provider/concise.md` -- a bare-in-a-VM provider that Stage 2 folds
  into the `(local-VM substrate, bare realizer)` cell.
