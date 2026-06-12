# smolvm provider spec

This is the unified spec for running mngr hosts (and ultimately minds workspaces) on smolvm microVMs. It covers all five workstreams; the external-repo changes live in local clones under `.external_worktrees/` (branch `mngr/smolmachines`) until upstreamed.

## Overview

- A new mngr provider backed by smolvm (libkrun: KVM on Linux, Hypervisor.framework on macOS), modeled closely on `mngr_lima`, capable of running a full minds workspace.
- The load-bearing requirement is btrfs at `/mngr` inside the VM (FCT's host_backup takes `btrfs subvolume snapshot`s). It is provided by a dedicated, agent-managed btrfs data disk -- no extra privileges anywhere: the host stays unprivileged and the workload container stays unprivileged (snapshot/create/delete are unprivileged btrfs ioctls under `user_subvol_rm_allowed`; deleting a read-only snapshot requires toggling its `ro` property first, which is also unprivileged).
- Motivation relative to lima: sub-second boots from the same OCI image docker mode uses (eliminating FCT's parallel cloud-init provisioning path), elastic memory and a shared layer cache for many concurrent local workspaces, built-in egress filtering, and no external limactl/qemu dependency. The trade-off: smolvm requires /dev/kvm on Linux (no TCG fallback), so KVM-less CI environments cannot run it.
- Acceptance bar: a full minds workspace on Linux x86_64 first (FCT boots in a smolvm host, agents run over SSH/tmux, host_backup takes a real btrfs snapshot and restic backup), then the same on macOS aarch64.

## Workstreams

1. **libkrunfw kernel** (fork of smol-machines/libkrunfw): re-enable `CONFIG_BTRFS_FS=y` on both arches (reverting the fork's deliberate slimming cut; upstream containers/libkrunfw already ships it). Loop devices were already enabled; modules are off so btrfs must be built-in (~2 MB). Rebuilt via smolvm's `build-libkrunfw` task and bundled as `libkrunfw.so.5.4.0`.
2. **smolvm**: four features, each an upstreamable commit:
   - Persistent data disks (`machine create --data-disk size=GiB,target=/path[,fs=btrfs]`): host allocates a sparse raw image (grown, never shrunk); the guest agent formats on first boot (btrfs superblock probe + mkfs signature check so foreign content is never clobbered), mounts with `user_subvol_rm_allowed`, grows the filesystem after host-side enlargement, and bind-mounts the target into every workload container.
   - Guest-side poweroff sentinel (`/run/smolvm/poweroff`, bind-mounted into containers): the agent syncs and exits PID 1 (the microVM kernel has no power-off backend). Enables mngr's idle self-stop convention.
   - Archive import (`pack create --from-archive`, docker-archive + OCI layouts): a host-side two-pass streaming layer merge with full whiteout semantics; locally built docker images run without a registry. Known limitation: PAX xattrs (e.g. file capabilities) are not carried over.
   - btrfs-progs in the agent rootfs.
3. **`libs/mngr_smolvm`**: provider plugin mirroring `mngr_lima` (backend/instance/config/host_store/sync CLI wrapper). sshd is provisioned over the smolvm exec channel (apk/apt detect, pre-injected ed25519 host key, root client key, dedicated sshd config with internal-sftp) and mngr connects via a forwarded localhost port (virtio-net backend; smolvm's default TSI backend is outbound-only). Capability-gated: the default virtiofs-exposed layout works on stock smolvm; the btrfs layout probes for `--data-disk` and errors clearly without it. `supports_snapshots = False` and rename unsupported (lima parity).
4. **forever-claude-template**: a `smolvm` create template (btrfs layout, image via the archive-import pipeline) and btrfs-progs in the image; host_backup's `btrfs_local` method works unchanged, except snapshot deletion must clear the `ro` property first when running unprivileged.
5. **minds**: `LaunchMode.SMOLVM` wired through agent_creator, visible alongside the other modes; the provider capability check is the guard.

## Expected behavior

See the provider README (`libs/mngr_smolvm/README.md`) for the user-facing surface. Key decisions:

- Default host (no `--image`): bare VM mode -- the Alpine agent rootfs only, with mngr base packages (sshd, tmux, git, rsync, jq, curl) apk-installed at provision time. Re-provisioning runs on every start (idempotent) because sshd does not survive a VM stop.
- Resource defaults mirror lima: 4 CPUs / 4 GiB / 100 GiB sparse btrfs data disk. `start_args` pass through to `smolvm machine create`; `build_args` select the image source.
- SSH ports are allocated per host at create time and persist on the host record (smolvm pins port forwards in its machine record). Networking is always on with full egress; egress filters can be applied via pass-through start args (`--allow-cidr`, `--allow-host`).
- Tests: unit tests run everywhere; the release test (`test_smolvm_btrfs_release.py`) skips cleanly without /dev/kvm or a data-disk-capable smolvm build and drives the full lifecycle (~8 s) on developer machines.

## Non-goals

Provider-level snapshots, `--forkable` golden machines / instant forking, smolvm binary distribution and managed install, Windows hosts, GPU, `.smolmachine` pack-ecosystem integration, and lima-mode deprecation.
