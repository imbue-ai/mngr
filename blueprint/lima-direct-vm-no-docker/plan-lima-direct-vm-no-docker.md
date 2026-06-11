# Plan: Lima direct-in-VM execution (drop docker-in-Lima)

## Overview

- Delete the Lima provider's `is_host_in_docker=True` path entirely and run the agent **directly in the VM**, removing the nested Docker daemon, the outer-host/container-SSH/port-forward machinery, the per-container snapshot helper, and the two-level (VM + docker-layer) caching problem.
- Reach parity with Dockerfile-built workspaces by running the agent **as root** in the VM (explicit opt-in `is_run_as_root`), so a coding agent can `apt install` with no sudo exactly as it can inside a container. The VM is the isolation boundary.
- Make "the same setup runs everywhere" real by **consolidating** forever-claude-template's (FCT) Dockerfile `RUN` steps into shared, idempotent, base-agnostic scripts that the Dockerfile `RUN`s (docker/vultr/imbue_cloud/OVH unchanged) and that Lima runs directly after content sync.
- Drive Lima setup from the **FCT lima template** (config), not new provider code: generic work_dir sync delivers the repo, then the after-sync provision hook runs the shared scripts as root. Code delivery and setup execution need no Lima-provider changes.
- Preserve minds workspace creation and btrfs backups: with agent=root and `/mngr` on btrfs, FCT bootstrap auto-selects `btrfs_local` snapshots — no snapshot helper needed. Image baking/caching for speed is an explicit follow-up, out of scope here.

## Expected behavior

- `mngr create --provider lima` (default 9p layout, no opt-in) behaves as today: agent runs as the Lima user with passwordless sudo, host_dir readable from the host machine while stopped.
- With `is_run_as_root=true` (btrfs layout), the Lima agent runs as **root**; `apt install`, writes to `/usr/local`, `/etc`, etc. succeed with no sudo — identical to docker/VPS workspaces.
- Setting `is_run_as_root=true` together with the 9p bind-mount layout (`is_host_data_volume_exposed=true`) is rejected **at provider-construction time** with a clear error (root can't traverse the reverse-sshfs mount).
- A Lima workspace built from FCT runs the **exact same** `setup_system.sh` / `install_dependencies.sh` / `build_workspace.sh` steps as a Dockerfile-built workspace; the resulting toolchain (uv-provided Python 3.12, Node, claude, workspace build) matches despite the different base.
- docker / vultr / imbue_cloud / OVH workspaces are unchanged: still built via `docker build`, byte-for-byte the same image (the Dockerfile now just `RUN`s the shared scripts at the same `COPY` boundaries, preserving layer caching).
- FCT `host-backup` on a direct-Lima host uses `btrfs_local` (root runs `btrfs subvolume snapshot -r` directly); no `/mngr-snapshot` trigger volume or outer helper involved. `runtime-backup` (git-based) is unaffected.
- Lima idle/shutdown is the VM lifecycle (`poweroff` + in-VM activity watcher); the container `kill -TERM 1` shutdown path is gone.
- Existing docker-mode Lima hosts (records with `is_host_in_docker=True`) are no longer startable; they must be destroyed and recreated (accepted break, no migration).
- Configs that still set removed Lima docker-mode fields surface a clear config error (hard removal, no silent ignore).
- minds is unchanged: it keeps passing `--template main --template lima`; all behavior shifts live in the FCT template and the Lima provider.

## Changes

### mngr — `libs/mngr_lima`

- Remove the entire docker-in-VM path: `_create_docker_host`, `_provision_docker_container`, `_start_docker_host`, `_finalize_docker_host`, `_create_container_host_object`, `_create_docker_shutdown_script`, `_make_outer_for_vm`, the container keypair/known-hosts/port-forward helpers, and the docker-mode branch in `create_host` / `start_host`.
- Add an explicit opt-in `is_run_as_root` to `LimaProviderConfig`; remove the docker-mode config fields (`is_host_in_docker`, `container_ssh_port`, `default_image`, `builder`, `docker_install_timeout`, `container_ssh_connect_timeout`, `image_build_timeout_seconds`, `default_container_run_args`, `docker_runtime`, `install_gvisor_runtime`).
- Validate `is_run_as_root` against layout at provider construction: error if `is_run_as_root=true` and `is_host_data_volume_exposed=true`.
- When `is_run_as_root=true`: provision the VM for key-based root SSH (inject root authorized key, `PermitRootLogin prohibit-password`) and create the agent's Host with `ssh_user=root` on the btrfs-disk layout. Reuse the existing root-key/host-key provisioning blocks; drop the docker-only and gVisor blocks.
- Remove the docker-mode Lima YAML generation (the Docker-install block, container port-forward rules, gVisor block) from `lima_yaml.py`; keep the direct-VM provisioning (package install, sshd tuning/host key, btrfs disk format+mount, `/mngr` symlink).
- Remove Lima's imports/calls into `mngr_vps_docker.container_setup`; fully sever the `mngr_lima → mngr_vps_docker` dependency in `pyproject.toml` if nothing remains genuinely shared (do not duplicate code to retain it).
- Leave `mngr_vps_docker`'s shared snapshot-helper / container / btrfs helpers intact for vps_docker/OVH.

### forever-claude-template (FCT)

- Add shared setup scripts split on the Dockerfile's two `COPY` boundaries:
  - `setup_system.sh` — repo-independent toolchain (apt packages, ttyd/cloudflared/gh binaries, uv, Node 20 + latchkey, claude CLI, modal).
  - `install_dependencies.sh` — manifests-only deps (`uv sync --no-install-workspace`, `npm ci`).
  - `build_workspace.sh` — full-source build (`uv sync --all-packages`, `npm run build`, `uv tool install` mngr + plugins).
  - Scripts are idempotent and base-agnostic (guard installs; rely on uv for Python 3.12); sequential now, structured so internal parallelization (python ∥ js) is a later drop-in.
- Refactor the `Dockerfile` to `COPY` each script at its layer boundary and `RUN` it (base image, the `COPY`s, and the `/docker_build_code` relocation + `fct-seed` stay Dockerfile-only); produces the same image with caching preserved.
- Update `.mngr/settings.toml` `[providers.lima]` and the lima create template: remove `is_host_in_docker` / docker-mode settings, set `is_run_as_root=true`, keep the btrfs layout, drop `--file=Dockerfile`, and run the three scripts in order via the after-sync agent-provision hook (three commands, no wrapper). Keep `target_path=/mngr/code`.
- `fct-seed` / `/docker_build_code` relocation remains for docker/VPS; it is a no-op or skipped for Lima (code lands directly at `/mngr/code`).
- Verify (manually) FCT bootstrap selects `btrfs_local` when `/mngr` resolves to a btrfs mount via the `/mnt/lima-<disk>` symlink; adjust bootstrap snapshot detection only if the symlink trips `findmnt`.

### Docs

- Update `mngr_lima` README/docs and `LimaProviderConfig` docstrings: remove docker-mode, document direct-in-VM, `is_run_as_root`, and the root/9p validation rule.

### Tests

- Remove the Lima docker-mode tests and fixtures (e.g. `test_lima_docker_release.py`, docker-mode helpers in `testing.py`/`conftest.py`).
- Manually (not in CI — Lima is too slow) verify: agent runs as root and can `apt install` a package with no sudo; FCT `btrfs_local` backups work on a direct-Lima host.
- Add a changelog entry per touched project (`libs/mngr_lima`, and any other touched mngr project) and the corresponding FCT change in that repo.
