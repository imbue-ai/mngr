# Unified per-host volume for the docker_vps provider

## Overview

- The `docker_vps` provider currently splits a single host's data across two Docker volumes per VPS: a per-user state volume (`<prefix>docker-state-<user_id>`, mounted at `/mngr-state` in a separate Alpine "state container") that holds only JSON metadata, and a per-host volume (`mngr-host-vol-<hex>`, mounted at `/mngr-vol` in the agent container) that holds the `host_dir` contents.
- The VPS architecture is 1:1 (one host per VPS, per `libs/mngr_vps_docker/README.md`), so the two-volume split provides no isolation benefit and makes future backup harder.
- This spec consolidates everything for one host onto the single `mngr-host-vol-<hex>` volume, deletes the Alpine state container entirely, and reads/writes metadata directly from the volume's mountpoint on the VPS over SSH.
- Motivation: enables a future single-volume backup story (one `docker run --rm -v <vol>:/data ...` captures everything), and removes a layer of indirection that exists only for historical symmetry with the local-docker provider.
- Breaking change with no migration path; existing hosts must be destroyed and recreated. The `<prefix>docker-state-<user_id>` volume is no longer created or read.
- Snapshot semantics are unchanged: `docker commit` still captures only the container's writable layer, never the named-volume mount. With everything-of-value now living on the volume, restoring a snapshot does not restore `host_dir` or metadata -- worth being explicit about even though the underlying behavior is the same as today.

## Expected Behavior

- A newly created `docker_vps` host has exactly **one** mngr-related Docker volume on the VPS: `mngr-host-vol-<host_id_hex>`.
- The unified volume's layout (visible at `/var/lib/docker/volumes/<volume_name>/_data/` on the VPS, and at `/mngr-vol/` inside the agent container):
  - `host_state.json` -- the host's metadata record (formerly `host_state/<host_id>.json`).
  - `agents/<agent_id>.json` -- per-agent metadata (formerly `host_state/<host_id>/<agent_id>.json`).
  - `host_dir/` -- the agent's host directory contents (formerly the root of the separate host volume).
- Inside the agent container, `/mngr` continues to be a symlink to `/mngr-vol/host_dir` (i.e. `host_dir.name` is appended to the mount path). The symlink target is `mkdir -p`'d before the symlink is created so it always exists.
- The Alpine state container (`<prefix>docker-state-<user_id>`) is no longer created. No code path looks for it.
- Listing hosts (`mngr list`, `_discover_host_records`) enumerates VPSes via `vps_client`/`_list_provider_vps_hostnames` as before, then for each VPS SSHes in, derives the single expected volume name from the VPS's mngr-labeled container (`mngr-host-vol-<host_id_hex>` where `host_id` comes from the container's `LABEL_HOST_ID`), and reads `host_state.json` (and any `agents/*.json`) directly from that volume's mountpoint. Exactly one host volume per VPS by construction; no `docker volume ls` enumeration.
- The volume's mountpoint path on the VPS is discovered dynamically via `docker volume inspect <name> --format '{{.Mountpoint}}'` once per host store and cached. This stays correct after we move Docker's data root in a follow-up.
- Metadata writes happen through the outer host (SSH as root) using `cat`/`tee`/`rm` against the discovered mountpoint. No atomicity primitives (matching today's behavior).
- The agent container can read/write `host_state.json` and `agents/` because the full volume is mounted at `/mngr-vol`. This is accepted as no worse than the existing trust boundary -- the agent already runs as root inside the container.
- Failed-host records are still written: the unified volume is created **before** any image pull/build so it always exists when a write is attempted.
- Destroying a host removes the single `mngr-host-vol-<hex>` volume; no per-user state container or state volume to clean up.
- `mngr` commands that worked against an existing host yesterday will fail to find that host after this change is deployed -- users must `mngr destroy` and recreate. This is called out explicitly in the plugin's changelog entry.

## Changes

- **`libs/mngr_vps_docker/imbue/mngr_vps_docker/host_store.py`**
  - Delete `ensure_state_container`, `STATE_CONTAINER_IMAGE`, `STATE_VOLUME_MOUNT_PATH`, `CONTAINER_ENTRYPOINT_CMD`, and the `_FILE_SEP` batched-read helper.
  - Rework `VpsDockerHostStore` to take `(outer: OuterHostInterface, volume_name: str)` instead of `(outer, state_container_name)`. Internally resolve and cache the volume's mountpoint via `docker volume inspect`.
  - Replace the three path helpers with `host_state.json` (root) and `agents/<agent_id>.json`. The `host_id` is no longer part of any path (1:1 invariant).
  - Replace `_exec_in_state_container` with `_exec_on_outer`, which runs simple shell commands against the resolved mountpoint via `OuterHostInterface.execute_idempotent_command`.
  - Adjust `write_host_record`, `read_host_record`, `delete_host_record`, `persist_agent_data`, `list_persisted_agent_data_for_host`, `remove_persisted_agent_data` to use direct file ops on the mountpoint.
  - Drop `list_all_host_records_with_agents`'s batched-shell trick; with one record per VPS, just `cat host_state.json` and (if present) iterate `agents/*.json` -- preserve a `(records, agents_by_host_id)` return shape so callers stay simple.
  - `VpsDockerHostRecord` itself is unchanged (same Pydantic schema).
- **`libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py`**
  - Delete `_state_container_name`, `_get_host_store`, `_get_existing_host_store`.
  - Add a `_get_host_store(outer, volume_name)` (or equivalent) that returns a `VpsDockerHostStore` bound to a specific host's volume; callers always have a `host_id` (or a known volume name from the discovered host record) by the time they need a store.
  - Move the host volume creation earlier in `create_host`: allocate `host_id`, create the unified volume `mngr-host-vol-<host_id_hex>`, `mkdir -p host_dir/` (and any required parent dirs) on the mountpoint, *then* proceed with image pull/build and container start. This guarantees the volume exists for failed-host record writes.
  - Update `_setup_container_on_vps` to remove the "set up state container on VPS" step. The host volume creation moves out of this method.
  - Update `_finalize_host_creation` to call the per-host store via the new constructor signature.
  - Update `destroy_host` to drop any reference to the state container / state volume; only the unified `mngr-host-vol-<hex>` is removed.
  - Update `_discover_host_records_with_agents` / `_read_records_from_vps`: instead of calling `_get_existing_host_store(outer)` (which probed the state container), find the VPS's single mngr-labeled container, read its `LABEL_HOST_ID`, derive the expected volume name `mngr-host-vol-<host_id_hex>`, and read through a `VpsDockerHostStore` bound to that volume. No `docker volume ls` enumeration; the 1:1 invariant means there is always exactly one host volume per VPS.
  - Update `_on_certified_host_data_updated` and any other call sites that constructed a store via the removed `_get_existing_host_store` helper.
- **`libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py` -- `HOST_VOLUME_MOUNT_PATH`**
  - Keep the constant value (`/mngr-vol`) but document its new role: the full unified volume is mounted there, with `host_dir/` as a subdirectory.
- **`libs/mngr/imbue/mngr/providers/ssh_host_setup.py`**
  - Adjust `build_check_and_install_packages_command` so that when `host_volume_mount_path` is provided, the script does `mkdir -p <host_volume_mount_path>` (here interpreted as the full path to `host_dir`, e.g. `/mngr-vol/host_dir`) *before* `ln -sfn ... <mngr_host_dir>`. The existing in-container directory-removal step (`[ -L ... ] || rm -rf ...`) stays.
  - Update the docstring to describe the new "symlink to a subdirectory of the mounted volume" semantics. Local `docker` provider call site continues to pass its per-host symlink target unchanged (`/mngr-state/volumes/vol-<hex>`) -- this is also a subdirectory of a shared volume, so the same `mkdir -p` logic applies cleanly there.
- **`libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py` -- callers of the changed `build_check_and_install_packages_command`**
  - The VPS provider passes `/mngr-vol/host_dir` (i.e. the full path including the new `host_dir` subdir) as `host_volume_mount_path`, so the symlink resolves into the volume.
- **`libs/mngr_vps_docker/imbue/mngr_vps_docker/host_store_test.py`**
  - Replace tests that exercised `docker exec`-into-state-container behavior with tests that exercise `VpsDockerHostStore` against an in-memory / temp-dir `OuterHostInterface` fake. The fake simulates `docker volume inspect` and a local directory that serves as the mountpoint. Cover the same cases (write/read/delete host record, persist/list/remove agent data, empty volume, malformed JSON).
- **`libs/mngr_vps_docker/imbue/mngr_vps_docker/instance_test.py`** and **`_outer_helpers_test.py`**
  - Update fixtures and any direct references to state-container setup. Remove tests that assert on state-container existence; add/adjust tests that assert on the unified volume layout and on `mkdir -p host_dir` happening before container startup.
- **`libs/mngr_vps_docker/imbue/mngr_vps_docker/test_ratchets.py`**
  - Re-run ratchets after the refactor; let `inline_snapshot=update` adjust counts. No intentional new violations.
- **`libs/mngr_vps_docker/README.md`**
  - Update the "State on the VPS" key-design-decision bullet to describe the single unified volume.
  - Update the ASCII architecture diagram to show one volume per host instead of "Docker named volume" + "State container + volume".
  - Add a brief note in "Host lifecycle" that destroying a host removes the single unified volume.
- **`libs/mngr_vps_docker/changelog/mngr-vps-on-docker-host-data.md`** (per CLAUDE.md: branch is `mngr/vps-on-docker-host-data`)
  - User-visible note: existing hosts created before this change cannot be discovered or managed after upgrade; they must be `mngr destroy`'d and recreated. The Alpine state container and per-user state volume are no longer created.
- **No changes** to the local `docker` provider, to `VpsDockerHostRecord`/`VpsHostConfig` schemas, to `mngr_vultr`/`mngr_ovh` subclasses (they only override discovery hostname enumeration, which is unaffected), or to any other plugin.
