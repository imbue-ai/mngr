Consolidated the `docker_vps` provider's two-volume layout (per-user state container
volume + per-host data volume) into a single per-host Docker volume on the VPS. The
unified volume `mngr-host-vol-<host_id_hex>` now holds `host_state.json`,
`agents/<agent_id>.json`, and `host_dir/` side by side, mounted at `/mngr-vol` inside
the agent container with `/mngr` symlinked to `/mngr-vol/host_dir`. mngr now reads
and writes metadata directly on the VPS filesystem via the volume's docker mountpoint
(discovered with `docker volume inspect`); the dedicated Alpine "state container" and
the per-user `docker-state-<user_id>` volume are no longer created or read.

This makes future single-volume backup of a host straightforward (one
`docker run --rm -v <volume>:/data ...` captures everything) and removes a layer of
indirection that existed only for historical symmetry with the local `docker` provider.

**Breaking change:** existing `docker_vps` hosts created before this release cannot
be discovered or managed after upgrade. Destroy and recreate them.
