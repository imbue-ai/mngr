# Docker host volume isolation

## Overview

- The Docker provider currently mounts a single shared named volume (`<prefix>docker-state-<user_id>`) into every host container and symlinks `host_dir` to a per-host sub-folder; the entire shared volume is therefore visible inside each host container, so any agent on host A can read host B's `host_dir` contents.
- Cross-host isolation within a single user was never the intent — the shared mount exists only because the state container needs to serve as a file server for offline reads.
- Docker Engine 25.0 (January 2024) introduced `--mount ... volume-subpath=...`, which lets a container mount a sub-tree of a named volume. This gives per-host isolation for the host container while leaving the state container unchanged (it still mounts the parent volume).
- Add an opt-in `isolate_host_volumes` boolean to the Docker provider config. `None`/`False` keep today's behavior; `True` switches to subpath-mounting `host_dir` directly, removing the symlink. The flag's effective value is persisted into the `HostRecord` so it remains stable across stop/start/snapshot-restore for that host's lifetime.
- The default will flip to `True` in a future release. While the flag is left at its default (`None`), the provider emits one deprecation warning per `mngr` invocation telling the user how to lock in either behavior.

## Expected Behavior

- New provider config field `isolate_host_volumes: bool | None = None` on `DockerProviderConfig`.
- When `isolate_host_volumes` is `None` or `False`, `mngr create --provider docker` produces a container identical to today: shared volume mounted at `/mngr-state`, `host_dir` symlinked to `/mngr-state/volumes/vol-<hex>/`.
- When `isolate_host_volumes` is `True`, `mngr create --provider docker` produces a container that mounts only `volumes/vol-<hex>/` of the shared named volume directly at `host_dir` (no `/mngr-state` mount, no symlink). The container cannot see sibling `vol-*` directories.
- When `isolate_host_volumes` is `True` and the Docker daemon is older than 25.0, `mngr create` fails with a clear "your Docker daemon is too old, you need 25.0+" error before any container is created.
- When `isolate_host_volumes` is `True` and `is_host_volume_created` is `False`, the provider config fails validation at load time with a clear error.
- When `isolate_host_volumes` is `None` (left at its default), each `mngr` invocation emits exactly one warning at provider config load time: "isolate_host_volumes default will change to True in a future release; set it explicitly to False to keep the current shared-volume behavior."
- When `isolate_host_volumes` is explicitly `False`, no warning is emitted.
- The state container, `get_volume_for_host`, `list_volumes`, and `mngr events <stopped-host>` all continue to work exactly as today regardless of the flag — they go through the state container, which still mounts the parent volume.
- Snapshot create/restore work as today: `docker commit` excludes volume contents in both modes, and snapshot restore replays the same effective isolation as the original create.
- A host created when `isolate_host_volumes` was `True` continues to mount with subpath isolation on every subsequent start/restart/snapshot-restore even if the provider config is later flipped to `False`, and vice versa. Pre-existing hosts (created before this change shipped) deserialize their record with the new field defaulting to `False` and therefore keep their current shared-mount layout for the rest of their lifetime.
- The "git fetch from `/var/lib/docker/volumes/...`" and `docker cp <state-container>:/mngr-state/volumes/...` workflows documented in `docker_usage.md` continue to work unchanged in both modes — neither goes through the host container's mount.

## Changes

- Add `isolate_host_volumes: bool | None = None` to `DockerProviderConfig` with help text describing isolation, the daemon-version requirement, and the planned default flip.
- Add a config-load-time validator on `DockerProviderConfig` that errors when `is_host_volume_created=False` and `isolate_host_volumes=True`.
- Emit a one-shot deprecation warning at provider config load time when `isolate_host_volumes is None`. The warning fires once per `mngr` invocation (module-level guard or similar) and only for the `None` case.
- Add a new persisted field `is_isolated_host_volume: bool = False` to `ContainerConfig` inside the `HostRecord`. Pydantic default of `False` so existing records without the field continue to deserialize and behave exactly as today.
- At `create_host` time, resolve the effective isolation value (`isolate_host_volumes is True`), write it into the new `ContainerConfig` field, and use it to choose the mount strategy. On every `start_host` / `_start_from_snapshot`, read the value from the persisted record (not from current config) and apply it.
- When the effective isolation value is `True`, perform a daemon-version preflight against Docker Engine 25.0; raise a clear error if older. The check can be cached on the provider instance to avoid repeating the API call across many creates.
- Switch the container-run path for isolated hosts from `-v <vol>:/mngr-state:rw` to `--mount type=volume,source=<vol>,target=<host_dir>,volume-subpath=volumes/vol-<hex>`. Continue to ensure the per-host volume sub-directory exists on the state volume before the container starts (today's `_ensure_host_volume_dir` already does this and is required because `volume-subpath` fails if the path is missing).
- Drop the symlink setup for isolated hosts: when the new mount targets `host_dir` directly, `build_check_and_install_packages_command` should take the `mkdir -p` branch, not the symlink branch. This likely means threading the isolation choice through the helper or letting the caller signal "host_dir is already a real mount, do not symlink".
- Rewrite the "Persistent host volume" section of `libs/mngr/docs/concepts/docker_usage.md` to lead with the isolated-by-default future behavior. Sub-bullet the legacy shared-volume mode and the conditions that select each. Update `libs/mngr/imbue/mngr/providers/docker/README.md` to describe both layouts and the persistence-via-`HostRecord` semantics.
- Add the per-PR changelog entry under `libs/mngr/changelog/<branch>.md`.
- No CLI flag, no template field, no migration path for existing hosts.
- Out of scope: any change to `mngr_vps_docker`; any change to the `is_host_volume_created` config field; removal of the shared-volume mode itself (that comes when the default flips).
