# Bare Providers -- Concise

**Goal:** run agents *directly on a cloud VM* (no Docker container) as a second
shape of the `aws`/`gcp`/`azure` providers, selected by config. Motivated by native
performance / full-host access and dropping the Docker dependency. Docker remains
the default and the only isolation-bearing shape.

**Key insight:** the "outer" host is already a bare host. Providers already SSH into
the VM (`outer_host_for(host_id)`) to run Docker commands; bare just promotes that
outer to be the agent host and deletes the container layer.

**Two orthogonal axes** -- every provider is a grid point:

- *Substrate* = where the machine is + machine lifecycle (local computer, cloud VM,
  Lima VM, SSH box).
- *Realization* = how the agent sits on it + placement lifecycle: **bare** vs
  **docker**.

So `local : docker :: aws-bare : aws-docker`.

**Architecture:** add a `HostRealizer` seam (defined against `OuterHostInterface`
only), injected like the existing `VpsClient`. `DockerRealizer` = today's container
logic moved behind it, unchanged. `BareRealizer` = agent runs on the VM OS, reached
at `vps_ip:22`; `host_dir` on the root disk; a systemd unit owns the agent + idle
watcher. Provider picks the realizer from `config.isolation` (`container | none`,
enum `IsolationMode`). Keeps the
grid to 3 clients x 2 realizers composed at config time -- no class matrix.

**Bare drops** (because motivations are perf + no-Docker, and v1 has no snapshots):
the Docker install, gVisor, btrfs unified volume + snapshot helper. **Bare adds:**
an agent-runtime host-setup step, a systemd unit, and a direct-on-disk host store.

**Reuses, already built:** `OuterHost`/`outer_host_for`; instance stop/start
(`stop_host` = instance stop, no `docker stop`); host-side systemd idle poweroff;
tag-based offline discovery. (AWS landed; GCP/Azure in progress.)

**Lifecycle:** stop = stop the instance (placement stop is a no-op); start = start
the instance + systemd brings the agent back; destroy = destroy the VM (no
container cleanup); snapshots deferred (`supports_snapshots = False`).

**Rollout:**
- Stage 1 (this branch): extract `BaseVpsProvider`, add the realizer seam, build
  `BareRealizer`, add `isolation`, wire bare on aws/gcp/azure (no snapshots). Land
  AWS-bare first as a vertical slice. Preserve vultr/ovh/imbue_cloud docker behavior.
- Stage 2 (follow-up): promote substrate to an interface; fold `local`/`lima`/`ssh`
  into the grid; consolidate the two Docker implementations into one `DockerRealizer`.

**Decisions:** rename `mngr_vps_docker` -> `mngr_vps` bundled into the Stage 1 PR;
bare agent runs as `root` on the VM's port-22 `sshd` (no separate agent user); bare
deps come from a prebaked VM image and/or an optional post-create setup script (the
bare analog of "base image + Dockerfile"). **Only open question:** the bare snapshot
shape (cloud disk vs btrfs-on-root), deferred past v1. See `spec.md` for detail.
