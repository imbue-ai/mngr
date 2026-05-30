Added a new design spec under `specs/vps-docker-unified-volume/concise.md`
that documents the docker_vps provider's move from a two-volume layout
(per-user state container + per-host data volume) to a single unified
per-host Docker volume on the VPS. The spec captures the rationale,
expected on-volume layout (`host_state.json`, `agents/<agent_id>.json`,
`host_dir/`), discovery behavior (find the volume via the agent
container's `com.imbue.mngr.host-id` label), and the breaking-change
caveat that pre-existing docker_vps hosts cannot be discovered after
upgrade.
