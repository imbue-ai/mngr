# Plan: smolvm provider for mngr (minds workspaces on smolvm microVMs)

## Overview

- Add a new mngr provider backed by smolvm microVMs (libkrun: KVM on Linux, Hypervisor.framework on macOS), modeled closely on `mngr_lima`, capable of running a full minds workspace.
- The load-bearing requirement is btrfs at `/mngr` inside the VM (FCT's host_backup takes `btrfs subvolume snapshot`s). It is provided by a dedicated, agent-managed btrfs data disk — no extra privileges anywhere: host stays unprivileged, the workload container stays unprivileged (snapshot/create/delete are unprivileged btrfs ioctls under `user_subvol_rm_allowed`).
- Five workstreams, all in scope and tested together: the libkrunfw kernel (re-enable `CONFIG_BTRFS_FS`, which upstream already ships), smolvm features (data disks, archive import, shutdown hook), the `libs/mngr_smolvm` plugin, the FCT smolvm create template, and the minds SMOLVM launch mode. smolvm/libkrunfw changes are made in local clones (via `.external_worktrees/`), structured as clean upstreamable commits.
- Motivation relative to lima: sub-second boots from the same OCI image docker mode uses (eliminating FCT's parallel cloud-init provisioning path), elastic memory and shared layer cache for many concurrent local workspaces, built-in egress filtering, and no external limactl/qemu dependency.
- Acceptance bar: a full minds workspace on Linux x86_64 first (FCT boots in a smolvm host, agents run over SSH/tmux, host_backup takes a real btrfs snapshot and restic backup), then the same on macOS aarch64 (kernel build + local signing documented).

## Expected behavior

- `mngr create my-agent@.smolvm` creates a smolvm host and starts an agent on it, like any other provider. The provider expects `smolvm` on PATH and runs a version + capability check; btrfs-dependent features fail with a clear "requires smolvm >= X with btrfs support" message on a stock build, while the default exposed-volume mode works even on stock smolvm.
- Default host (no `--image`): bare VM mode — the Alpine agent rootfs only, no image pull. The provider apk-installs sshd plus mngr base packages (tmux, git, rsync, jq) at provision time. With `--image`, sshd is injected via `smolvm exec` (auto-detecting apk/apt), so arbitrary OCI images work.
- SSH: the provider injects a pre-generated host key (lima-style), publishes the guest sshd on a localhost port via smolvm port forwarding, and mngr talks to the host over SSH/pyinfra exactly as with lima. Ports are re-allocated on each start and host records are updated.
- Resource and arg mapping: defaults are 4 CPU / 4 GiB / 100 GiB sparse btrfs data disk. `start_args` pass through to `smolvm machine create` (`--cpus`, `--mem`, `--storage`, ...); `build_args` select the image source (`--image`, `--image-archive`, `--from`, or a Dockerfile path).
- FCT image pipeline: given a Dockerfile, `create_host` runs docker build → docker save → smolvm archive import, cached by content hash. Pulling a published image from a registry is the later "fast mode".
- Two storage modes via `is_host_data_volume_exposed` (lima parity):
  - Exposed (default): host directory shared into the VM via virtiofs at the host_dir; offline file reads work when the VM is stopped; `get_volume_for_host` returns the local volume.
  - btrfs data disk (what FCT/minds opts into): host_dir lives on the btrfs disk; `get_volume_for_host` returns None; unprivileged btrfs subvolume snapshot/create/delete work inside the workspace.
- Networking: on by default with full egress; mngr's `-b offline` / `-b cidr-allowlist` map onto smolvm's egress filters (first VM provider to honor them).
- Lifecycle: stop/start preserves all state (overlay + data disk). Idle hosts stop themselves: mngr's standard shutdown script writes a sentinel file that the smolvm guest agent watches, triggering its existing graceful shutdown (bare-VM hosts just run `poweroff`). `supports_snapshots = False`, rename unsupported, destroy deletes the machine + disks, delete removes records — all lima parity.
- minds: a SMOLVM launch mode appears in the desktop client alongside DOCKER/LIMA/CLOUD/IMBUE_CLOUD from day one; the capability-check error is the guard for users without the custom smolvm build. Lima mode stays; deprecation is revisited once smolvm mode is proven.
- Tests: unit tests run everywhere; release tests skip cleanly when /dev/kvm (or HVF) is absent and run on dev machines.
- Non-goals: provider-level snapshots, `--forkable` golden machines / instant forking, smolvm binary distribution and managed install, Windows hosts, GPU, `.smolmachine` pack-ecosystem integration, and lima-mode deprecation.

## Changes

Ordered by milestone; each external-repo change is a clean, self-contained, upstreamable commit.

- **libkrunfw (fork of smol-machines/libkrunfw)**: re-enable `CONFIG_BTRFS_FS=y` in both arch configs (reverting the fork's deliberate slimming cut; upstream containers/libkrunfw already ships it; deps auto-select via olddefconfig; loop is already enabled). Rebuild the bundled kernel via smolvm's existing `build-libkrunfw` task.
- **smolvm**:
  - Expose the launcher's existing `extra_disks` support as persistent named data disks on `machine create`/`update` (`--data-disk size,fs=btrfs,target=...`), persisted in the machine record; the guest agent does first-boot mkfs, mounts with `user_subvol_rm_allowed`, and bind-mounts the target into the workload container; add btrfs-progs to the agent rootfs.
  - Archive import: `pack create --from-archive <tarball>` accepting docker-archive and OCI-archive (docker save / podman output), converting to the existing packed-layers layout; `machine create --image-archive` as sugar over it. Reuses the proven virtiofs packed-layers path and `machine create --from`.
  - Guest-triggerable shutdown: agent watches a sentinel path shared into the main container and runs its existing graceful-shutdown on touch.
  - Regression test locking in persistent container overlays for `--from`/packed machines across exec and stop/start.
- **`libs/mngr_smolvm` (new project)**: provider plugin mirroring `mngr_lima` file-for-file — backend, instance, config, host_store, errors, and a sync CLI wrapper around the `smolvm` binary (not the async SDK). Includes: version/capability probe with per-feature gating, sshd injection + host-key handling, both storage modes, bare-VM apk provisioning, the Dockerfile→archive-import pipeline with content-hash caching, `-b` flag mapping to egress filters, idle watcher + sentinel shutdown wiring, unit tests, a KVM/HVF-gated release test (lima-btrfs-release equivalent), ratchets, and a changelog entry. The unified spec lands at `libs/mngr_smolvm/docs/spec.md` with the project skeleton, covering the external-repo changes as sections.
- **forever-claude-template**: add a `smolvm` create template (btrfs data-disk mode, resource overrides, env parity with the lima template) and btrfs-progs to the image; host_backup's existing `btrfs_local` method works unchanged.
- **apps/minds**: add `LaunchMode.SMOLVM`, agent_creator wiring to the provider (template selection, image pipeline args), fully visible in the desktop client.
- **macOS milestone (last)**: aarch64 kernel build, HVF-entitled signed smolvm binaries, and documentation of the local build/sign workflow; same provider code, validated end-to-end on a Mac.
