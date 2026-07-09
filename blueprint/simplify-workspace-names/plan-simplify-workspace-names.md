# Simplify workspace names

Follow-up issue (deferred pending-rename): https://github.com/imbue-ai/mngr/issues/2342

## Overview

- Establish one clean naming model with a single canonical home per datum: `host_id` is the immutable identity (never derived from any name); `host_name` is a normalized, mutable `SafeName` slug; the human-readable workspace name is an arbitrary string stored as a `workspace_display_name` label on the workspace's `system-services` agent.
- Make workspaces renamable: a rename updates the `host_name` slug and the display label together so the two never drift (the confusing "named Blah, host stays blah" state goes away). The UI shows the human-readable name.
- Add host rename to the providers that lack it: `imbue_cloud` (new connector endpoint + `rename_host`) and `lima` (decouple the VM instance name from `host_name` by keying it on `host_id`). `ssh` stays user-owned ("edit your config"). `local`/`docker`/`modal`/`vps` already work.
- Remove the `workspace` label entirely: it duplicated `host_name` and was used as a pseudo-identity. Workspace grouping keys off `host_id` (an agent's workspace is the `system-services` agent on its host); per-provider uniqueness keys off the actual `host_name`. This spans both the mngr monorepo and the forever-claude-template, done backwards-compatibly.
- Scope is end-to-end with an ultra-basic rename UI; the offline/unreachable "pending rename" mechanism is deferred to the follow-up issue above.

## Expected behavior

### Naming model

- Every host keeps an immutable `host_id`. No provider derives identity from a name (fixes `ssh`'s `uuid5(name)` and `lima`'s instance-name coupling).
- `host_name` is a lowercase slug, validated by mngr's `HostName` (strict: rejects invalid/over-long input, raises `InvalidName`, never silently mangles), capped at 63 characters (the DNS-label limit). minds applies a smaller 32-character target to the slugs it derives.
- The human-readable name is fully arbitrary (only required to be non-empty after trimming). minds converts it to a slug (lowercase, strip disallowed chars, truncate to 32). If the conversion yields an empty slug (e.g. all-emoji input), minds rejects it with "include at least one letter or number".

### Create

- The create form accepts an arbitrary name; minds derives the slug for `host_name` and stores the arbitrary name in the `workspace_display_name` label on the `system-services` agent (symmetric with rename).
- Per-provider uniqueness is enforced against `host_name` (case-folded, active workspaces only, scoped to the target provider instance), reusing the existing `taken_host_names_on_provider` + `GET /api/v1/desktop/host-name-available` machinery (now sourced from `host_name`). A collision is rejected with an inline error naming the conflicting existing workspace (its display name + slug).

### Rename

- A new rename field in an existing per-workspace settings/detail view, backed by `POST /api/v1/desktop/workspace/<agent_id>/rename` (keyed on the `system-services` agent id).
- minds orchestrates: call `mngr rename --host` first, then write the `workspace_display_name` label. The operation is idempotently re-runnable; on partial failure it surfaces an error and re-running completes it (mngr's existing resumable-rename behavior).
- Display-only rename fast path: when the new name normalizes to the *same* slug, no `rename_host` and no collision check happen — it is just a display-label write, so it works on every provider (including `ssh`) and offline.
- Failure semantics: slug collision -> 409 naming the conflict; invalid/empty-normalized name -> 400 with the validation message. The UI is minimal (submit then show the error; no live availability pre-check, unlike the create form).
- On a slug-changing rename, the git branch (`mngr/<old-slug>`) and the chat-agent name stay at the original slug; this internal drift is accepted (not user-visible; chat agents get their own renaming later).
- v1 applies a rename only when the provider's name store is writable now (`imbue_cloud`/`modal`/`vps` always; `docker`/`lima` when the local daemon/store is reachable). `ssh` host rename is unsupported with an "edit your config" message (a display-only rename still works). Offline/unreachable queuing is deferred (follow-up issue).

### Provider host rename

- `imbue_cloud`: rename works online or offline (the connector is always reachable). Identity remains the lease `host_db_id`; `host_name` is updated in the connector DB. The LiteLLM key no longer carries the mutable name in its metadata (it was only a passive discoverability hint; the key is minted before `host_id` exists).
- `lima`: newly-created VMs name their limactl instance from `prefix + host_id`; discovery recognizes both the new and legacy (`prefix + host_name`) schemes. `host_name` becomes a pure logical field in the local `LimaHostStore` record. Rename is supported only for new `host_id`-named VMs; legacy-scheme VMs reject rename with a clear message.
- `ssh`: unchanged (rename unsupported; names are user-owned).

### Workspace identity and the removed `workspace` label

- A workspace is identified by its controlling `system-services` agent's `agent_id`. Any agent maps to its workspace via `host_id` -> the `system-services` agent on that host (existing `get_system_services_agent_id` / `_find_system_services_agent`).
- minds workspace discovery keys off `is_primary` (+ ids) instead of the `workspace` label.
- Worker and chat agents no longer carry a `workspace` label; they belong to their workspace purely via `host_id`.
- In-container `mngr list` (FCT) no longer filters on `has(labels.workspace)`; inside a workspace container all agents on the host are workspace agents.
- Display name resolution: when `workspace_display_name` is absent (legacy workspaces), display falls back to `host_name`. This fallback is purely backwards-compat and is marked with an inline comment as removable around September 2026.
- Already-baked imbue_cloud pool hosts keep a stale `workspace` label until re-baked; this is harmless because nothing reads it after the switch.

## Changes

### mngr core (`libs/mngr`)

- Add a 63-character (DNS-label limit) max-length cap to `HostName` only (leave the shared `SafeName` base and `AgentName`/`ProviderInstanceName` uncapped so existing long names such as `imbue_cloud_<email-slug>` are not retroactively invalidated). minds applies its own smaller 32-character target to derived slugs.
- Implement the `mngr rename --host <current> <new>` CLI flag (currently `[future]`): rename strictly the host (wired to `provider.rename_host`); agent rename stays the separate existing behavior. Regenerate the auto-generated rename doc.

### imbue_cloud (`libs/mngr_imbue_cloud` + `apps/remote_service_connector`)

- Implement `ImbueCloudProvider.rename_host` (replace the `NotImplementedError`): update `host_name` via the connector and refresh the local lease cache; works online and offline.
- Add `ImbueCloudConnectorClient.rename_host` calling a new `POST /hosts/{host_db_id}/rename` connector endpoint (`UPDATE pool_hosts SET host_name`). The connector code lands in this PR; the connector deploy is a separate coordinated step.
- Stop writing the `workspace` label in the bake path (`bake/pool_bake.py`); the lease-adoption label merge in `hosts/host.py` simply stops carrying it once writers are removed. Update the related comment.

### lima (`libs/mngr_lima`)

- Derive new VMs' limactl instance name from `prefix + host_id` instead of `prefix + host_name`; make discovery recognize both the new and legacy schemes and read the name from the persisted record.
- Implement `rename_host` for new `host_id`-named VMs (logical record write in the local `LocalVolume`-backed `LimaHostStore`); reject rename on legacy-scheme VMs with a clear message.

### minds (`apps/minds`)

- Create flow: accept an arbitrary name, derive the slug for `host_name`, and set the `workspace_display_name` label on the `system-services` agent; stop writing the `workspace` label.
- Rename flow: add `POST /api/v1/desktop/workspace/<agent_id>/rename` and an ultra-basic rename field in a per-workspace settings/detail view; orchestrate `mngr rename --host` then the label write; implement the display-only fast path and the 409/400 failure semantics.
- Split `get_workspace_name` into (a) a display accessor that reads `workspace_display_name` and falls back to `host_name` (used by `_serialize_workspace`, the desktop tray `running_workspace_entries`, and the landing-page `agent_names`), and (b) a `host_name` source read from the discovered agent (used by the `create_helpers` collision/uniqueness checks). Change the `_serialize_workspace` fallback from `agent_name` to `host_name`. Mark the legacy fallback with the "removable ~Sept 2026" comment.
- Switch workspace discovery filters to `is_primary` (`backend_resolver.list_known/active/restorable_workspace_ids`) and the `forward_cli` default agent-include filter; remove `workspace`-label reads.
- Stop minting the LiteLLM key with `host_name` metadata.
- Normalizer: add the arbitrary-text -> slug helper (lowercase, strip, truncate to 32, reject-if-empty) and wire collision rejection (inline error naming the conflict) into both create and rename.

### forever-claude-template (separate repo, vendored)

- Stop writing the `workspace` label: `bootstrap/manager.py` (chat agent), `create_worker.py` (workers), `system_interface/agent_manager.py` (both create paths).
- Remove the `workspace` readers: the bootstrap propagation read (`manager.py`), the system_interface label-propagation (`agent_manager.py`), and the `.mngr/settings.toml` `include = has(labels.workspace)` `mngr list` filter. Verify in-workspace behavior still works.
- Update the cosmetic doc/example references (`create_worker.py` docstring; and in the monorepo, the `mngr_forward` CLI help example).

### Out of scope / deferred

- Offline/unreachable "pending rename" queuing (follow-up issue #2342).
- `ssh` host rename (names stay user-owned).
- Renaming chat agents and moving the per-host git branch on rename.

### Changelog

- Per-project entries for `mngr`, `mngr_imbue_cloud`, `mngr_lima`, `minds`, and `dev`; plus the forever-claude-template's own changelog in its repo.
