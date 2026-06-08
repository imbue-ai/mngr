# Plan: Run the minds workspace in a Docker container inside the Lima VM

## Refined prompt

> within minds, we want to refactor our usage of the mngr_lima provider so that it ends up running the mind worksapce inside of a *docker* container inside of the lima VM (rather than running the code directly inside of the lima vm)
>
> * Add an opt-in `is_host_in_docker` flag to the Lima provider (defaults to `False` = current direct-in-VM behavior); when `True`, run the workspace in a docker container inside the VM.
> * `is_host_in_docker` is config-only via `providers.lima.*` (set through the FCT template's `setting__extend`); no new CLI flag or build-arg.
> * Keep the docker-in-VM logic inside `mngr_lima`, but reuse as much `mngr_vps_docker` code as possible by refactoring shared logic into pure functions/scripts.
> * `mngr_lima` takes a direct dependency on `mngr_vps_docker` and imports the refactored shared functions + `snapshot_helper.sh`.
> * mngr must treat the docker container as the host (ssh and all agent work happen inside it); the Lima VM becomes an "outer" that is mostly invisible to mngr, like the VM in vps_docker mode. Lima forwards the container's sshd out to the host's localhost for direct connection.
> * Use the simplified data/metadata model: skip vps_docker's loop-file (the Lima additional disk is already btrfs), create one per-host subvolume on it and bind-mount that into the container as host_dir, and keep host/agent metadata in host_dir via the container-as-host (no separate outer-host metadata store).
>
> This will allow us to remove most of the entries in .mngr/settings.toml in the forever-claude-template for the "extra_provision_command__extend" key, ie, where we're effectively duplicating most of the installation instructions from the dockerfile.
> As it is today, this is bad because all of the other providers for minds use the same dockerfile (and are thus in sync).
> The whole point of this change is to move to a place where, even when running within lima, we use docker (so that all of the dependencies get installed in the same way, we don't need sudo anymore, everything is consistent, etc)
> We should be able to do that by effectively replacing those extra provision commands with something that creates the docker container properly inside of the lima VM)
>
> * Build the FCT `Dockerfile` inside the Lima VM (rsync context + `docker build`, like vps_docker) and delete the duplicated `extra_provision_command__extend` toolchain block from the `lima` template.
> * Deliver the FCT `settings.toml` change via a `.external_worktrees/forever-claude-template` worktree on the same branch name, converging the `lima` template to mirror the `docker` template.
> * In minds, keep direct-in-VM as the Lima default and point the `--template lima` wiring at the new docker-in-VM mode via the flag.
> * Treat the minds side as config-only for behavior, but include create/start timeout bumps in this branch (cold create now builds the image in-VM; start now boots the VM + relaunches the container).
> * Accept the slow cold in-VM `docker build` for now (match vps_docker); no image caching/prebake in this branch.
>
> One of the trickier bits here will be backups (ensuring that the btrfs consistent snapshotting stuff can still be done correctly from within the docker container).
> Take a look at how we do this for the vps docker setup (we'll need a similar "trigger a snapshot" script, and you'll need to be really careful about data paths, volumes, etc so that everything ends up in the right place)
>
> * Reuse Lima's existing in-VM btrfs additional disk as the snapshot store; the per-host subvolume + docker volume live on it. Setting `is_host_in_docker=True` forces this btrfs additional-disk layout.
> * Replicate the vps_docker systemd `snapshot_helper` daemon + request/result IPC (shared docker volume) inside the Lima VM so the in-container agent can trigger consistent btrfs snapshots; reuse the existing helper code/script.
> * Reuse the existing minds restic backup contract/paths unchanged (`/mngr-snapshot`, `/mngr-snapshots`) so backups work identically across docker/lima/vultr.
> * `mngr stop` stops the whole Lima VM (frees local RAM); `start` boots the VM then relaunches the container + snapshot helper; `destroy` removes everything.
> * Leave the VM's base provisioning as-is for now (only add docker + the snapshot helper); defer trimming it to the "simpler OS" followup.
> * Offline reads (`mngr event` / `transcript`) against a stopped lima-docker host stay broken until started — documented as expected behavior, matching the current btrfs Lima mode.
>
> One of the nice things about moving to this setup is that it *should* enable us to get away with using a much simpler operating system for the lima VM (but we'll do that in followup work, not in this branch)

---

## Overview

- Add an opt-in `is_host_in_docker` flag to the Lima provider (default `False` = today's direct-in-VM behavior). When `True`, the Lima VM becomes an invisible "outer" that runs the workspace inside a single Docker container built from the FCT `Dockerfile` — the same image every other minds provider already uses.
- This eliminates the duplicated `extra_provision_command__extend` toolchain block in the FCT `lima` template, which today re-installs by hand (with `sudo`) everything the `Dockerfile` already installs. After the change, Lima installs dependencies exactly like docker/vultr — one source of truth, always in sync, no `sudo`.
- mngr treats the **container as the host** (ssh + all agent work happen inside it), exactly like vps_docker treats its container. The Lima VM is provisioned only with docker + btrfs + the snapshot helper, and Lima forwards the container's sshd to the local machine so mngr connects straight to the container.
- Backups stay consistent: the Lima VM already has a real btrfs additional disk, so we skip vps_docker's loop-file entirely — we create one per-host btrfs **subvolume** on that disk, bind-mount it into the container as `host_dir`, and replicate vps_docker's systemd `snapshot_helper` daemon + request/result IPC so the in-container agent can trigger `btrfs subvolume snapshot -r`. The minds restic contract (`/mngr-snapshot`, `/mngr-snapshots`) is reused unchanged.
- Shared container/btrfs/snapshot logic is refactored out of `mngr_vps_docker` into reusable pure functions + scripts; `mngr_lima` takes a direct dependency on `mngr_vps_docker` and imports them. This is a deliberate, incremental step toward a much simpler Lima OS (deferred to follow-up work).

## Expected behavior

- A minds workspace created in LIMA mode runs the agent inside a Docker container in the Lima VM instead of directly in the VM. The user-visible workspace (chat, services, terminal, system interface) behaves identically to docker/vultr modes.
- Dependencies in the Lima workspace now come entirely from the FCT `Dockerfile` (same versions of node, uv, gh, ttyd, latchkey, claude, system interface build, etc. as the other providers). They no longer drift from the Dockerfile.
- `is_host_in_docker` defaults to `False`; any existing direct-in-VM Lima usage (CLI users, other repos) is unchanged. The new behavior is opt-in via `providers.lima.is_host_in_docker=true` (config only, no new CLI flag/build-arg).
- When `is_host_in_docker=True`, the btrfs additional-disk layout is required and forced; combining it with the 9p bind-mount layout is rejected with a clear error.
- `mngr connect` / `mngr exec` / ssh land directly inside the container (the host), via a Lima-forwarded localhost port to the container's sshd. The VM itself is not something mngr or minds interacts with directly.
- `mngr create` in LIMA mode is slower on a cold run, because the FCT `Dockerfile` is now built inside the VM (VM boot + docker install + image build). The minds LIMA create timeout is raised to accommodate this.
- `mngr stop` stops the whole Lima VM (freeing local RAM/CPU). `mngr start` boots the VM, then relaunches the container and the snapshot helper. `mngr destroy` removes the container, the VM, and the btrfs disk. Start is slower than today's direct-in-VM start; minds start/recovery timeouts are adjusted accordingly.
- Backups work identically across docker / lima / vultr: the in-container agent writes a snapshot request to `/mngr-snapshot`, the in-VM helper takes a consistent read-only btrfs snapshot of the per-host subvolume, and the agent's restic reads it at `/mngr-snapshots`.
- Offline reads (`mngr event` / `mngr transcript`) against a *stopped* lima-docker host do not work until it is started — same documented limitation as today's btrfs Lima mode (the data lives inside the stopped VM).
- The Lima VM's base OS provisioning is unchanged apart from adding docker + the snapshot helper; trimming it to a minimal OS is explicitly out of scope for this branch.

## Changes

### `mngr_vps_docker` (refactor for reuse)
- Extract the btrfs / docker-volume / container / snapshot-helper logic that is currently Lima-agnostic but VPS-coupled into reusable pure functions and shared scripts, so both `mngr_vps_docker` and `mngr_lima` can call them.
- Make the shared helpers parameterized over where the btrfs filesystem comes from, so a caller can supply an *already-mounted* btrfs path (Lima) instead of provisioning a loop-file (VPS).
- Expose `snapshot_helper.sh` + its systemd unit as shared resources usable by both providers (same request/result IPC contract and on-disk paths).
- Keep the existing VPS behavior byte-for-byte identical (loop-file path remains the VPS-specific implementation of "provide a btrfs filesystem").

### `mngr_lima` (new docker-in-VM mode)
- Add an `is_host_in_docker` config field (default `False`) to the Lima provider config, settable via `providers.lima.is_host_in_docker`.
- When `is_host_in_docker=True`: require/force the btrfs additional-disk layout (reject the 9p bind-mount layout with a clear error), referencing the existing `is_host_data_volume_exposed` mechanism.
- VM provisioning gains: install Docker, install the snapshot-helper script + systemd service (reused from `mngr_vps_docker`), and create one per-host btrfs subvolume on the existing additional disk.
- Build the FCT `Dockerfile` inside the VM (rsync the build context into the VM + `docker build`), reusing the refactored vps_docker build path; then run a single container with the per-host subvolume bind-mounted as `host_dir` plus the snapshot trigger/read volumes.
- Treat the container as the mngr host: configure the container's sshd, have Lima forward that port to the local machine, and point the provider's `Host` connector at the forwarded port (container-as-host), mirroring vps_docker's "container is the host" model.
- Keep host existence/config/SSH tracked in the existing local Lima host record; keep host/agent metadata in `host_dir` on the subvolume (no separate outer-host metadata store).
- Lifecycle: `stop` powers off the VM; `start` boots the VM then relaunches the container + snapshot helper; `destroy` tears down container + VM + btrfs disk. Direct-in-VM mode keeps its current lifecycle untouched.
- Declare the dependency on `mngr_vps_docker` in `mngr_lima`'s package metadata.

### forever-claude-template (delivered via `.external_worktrees/forever-claude-template`, same branch)
- In `.mngr/settings.toml`, converge `[create_templates.lima]` toward `[create_templates.docker]`:
  - Add `setting__extend = ["providers.lima.is_host_in_docker=true", ...]` (keeping `is_host_data_volume_exposed=false`).
  - Build the image from the `Dockerfile` and reuse the docker template's first-boot seed (`fct-seed`) / `pass_host_env` pattern.
  - Delete the duplicated `extra_provision_command__extend` toolchain block (apt packages, node, latchkey, uv, ttyd, gh, system-interface build, playwright, tk symlink, bashrc env sourcing, `/code` + `/worktree` symlinks).
- Keep the shared `[create_templates.main]` tmux-config provision command (provider-agnostic).

### minds (config-and-timeouts only)
- No structural source changes to provider selection: `--template lima` and the `.lima` address stay the same; the flag is set entirely through the FCT template.
- Raise the LIMA create timeout (`onboarding.py` per-mode timeout map) to cover a cold in-VM image build.
- Adjust start/recovery timeouts where they assume the fast direct-in-VM start, since start now boots the VM + relaunches the container.

### Documentation
- Update `mngr_lima` README/docs to describe the `is_host_in_docker` mode, the container-as-host model, the btrfs-subvolume snapshot path, and the stopped-host offline-read limitation.
- Add changelog entries for each touched project (`libs/mngr_lima`, `libs/mngr_vps_docker`, `apps/minds`, and the FCT repo's own changelog).

### Out of scope (follow-up)
- Trimming the Lima VM to a minimal OS.
- Image caching / prebake to speed up repeated cold Lima creates.
- Making offline reads work against a stopped lima-docker host.
