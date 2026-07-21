# Spec uncertainties

Conflicts or potentially-outdated information found while writing specs, to be
resolved later. Each entry: the conflict, where it is, and the assumption made.

## Workspace identity: agent_id-as-logical-id vs host_id-keyed workspaces

- **Where:** `specs/workspace-sync/spec.md` (Implemented) models `agent_id` as the
  logical workspace identity: the `workspace_records` migration adds a partial
  unique index on `(user_id, agent_id) WHERE state = 'active'`, and a restored
  workspace keeps its `agent_id` on a new `host_id`.
- **Conflict:** `specs/host-scoped-agent-identity/spec.md` decides that
  `agent_id` is only unique per host and migrates minds workspace identity
  (URLs, routes, registries) to `host_id`, demoting `agent_id` to a per-host
  detail. Under that model the `(user_id, agent_id)` active-unique index is
  wrong (two independent hosts may legitimately carry the same `agent_id`) and
  restore lineage flows through the existing `restored_from_host_id` column
  instead.
- **Assumption made:** the host-scoped spec is the current direction; the
  workspace-sync spec is accurate as a record of what is implemented today.
  When the minds phase lands, update `specs/workspace-sync/spec.md` to point at
  the new identity model.

## VPS Docker "single mode of operation" vs the bare/docker realizer axis

- **Where:** `specs/vps-docker-provider/spec.md` ("Single mode of operation"
  section) asserts the VPS providers have one mode: the VPS always runs and the
  Docker container is the host, with `docker stop`/`docker commit` as the
  stop/snapshot primitives.
- **Conflict:** `specs/bare-providers/spec.md` introduces a *bare* realization
  (agent directly on the VM, no container) selected by `config.mode`, and the
  instance-stop lifecycle (`specs/aws-ec2-stop-start-lifecycle/spec.md`) already
  added a machine-stop path that the "single mode" framing predates.
- **Assumption made:** the "single mode" statement describes the original Docker
  shape, not an invariant. The bare spec treats realization as an explicit axis and
  the Docker shape as one (default) point on it. When the bare work lands, update
  `specs/vps-docker-provider/spec.md` to reference the realizer axis.
