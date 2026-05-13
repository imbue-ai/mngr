# Minds workspace identity = host name, not agent name

## Overview

- The minds create-project form's `Name` field currently feeds the *agent* name; the host then derives from it as `<agent-name>-host`. Going forward, that field feeds the *host name directly* and the agent name becomes a hardcoded constant.
- Every minds-created agent is named `system-services`. There is one agent per host, so the agent name carries no information for the user; the workspace is identified entirely by its host.
- The host name is the literal string the user typed in the form, validated by mngr's existing `HostName` regex. Invalid input re-renders the form with an inline error; the JSON API returns a 400 with the same message.
- The `remote_service_connector` (in `apps/remote_service_connector/`) grows a required `host_name` field on lease + listing payloads so the leased pool agent's workspace identity is durable across the connector boundary (and a rename can land later without further schema work). One-shot deploy; no backwards-compat for legacy clients or pre-existing pool rows.
- On imbue_cloud lease-adoption we stop rewriting the pre-baked agent's name. We still merge in minds-supplied labels (including `workspace=<host_name>`) and still patch claude config + env for the new API key. Host rename itself is out of scope for this PR — the data model is the deliverable.

## Expected Behavior

- The create-project form continues to show a single field labeled `Name` with placeholder `assistant`; the HTML input and `/api/create-agent` JSON field are now both called `host_name`. The submitted value becomes the host name on every launch mode.
- `MINDS_WORKSPACE_NAME` (env var, default `"assistant"`) keeps its spelling and default value, but now drives the form's default *host* name instead of the agent name. It is no longer forwarded into the agent's environment (dropped from FCT's `[commands.create].pass_env`).
- For LOCAL / LIMA / CLOUD launch modes, `mngr create` is invoked as `system-services@<host_name>.<provider>` with `--reuse --update`; re-submitting the form with an existing host name re-attaches to the existing host (deliberately — "reset my workspace"). The provider's own collision behavior governs `--new-host` failures.
- For IMBUE_CLOUD launch mode, the plugin's `create_host` sends `host_name` on the lease request; the connector rejects names that don't pass mngr's `HostName` regex. The leased pool agent retains its baked name (no longer renamed to user input). The plugin still injects the LiteLLM API key, the gateway wiring, and the workspace label.
- The branch minds asks `mngr create` to use is `mngr/<host_name>` (passed explicitly via `--branch`), not the core `default_branch_name(agent_name)` default. On imbue_cloud lease-adoption the pre-baked `data.json:created_branch_name` is overwritten to match.
- The landing page workspace cards display each workspace's `labels.workspace` value (the host name the user typed). They previously rendered the agent's `name`, which would now uniformly read `system-services` — useless to the user.
- `mngr list` filtering from minds is unchanged in shape (`include = ["has(labels.workspace)"]`); the label's value is the host name on all newly-created agents, so the filter still picks up exactly the minds-owned agents.
- Existing user workspaces (created under the old convention, with `labels.workspace = <agent_name>` and host = `<agent_name>-host`) are left exactly as they are; no migration runs at startup or on demand. They continue to appear on the landing page under their old (agent-derived) names.
- `mngr imbue_cloud` host listings (`mngr list` against the imbue_cloud provider) show the lease's `host_name` rather than the lease's `host_id` as the friendly name.
- `ImbueCloudProvider.rename_host` continues to raise `NotImplementedError`; nothing in the minds UI exposes a rename action. Adding a rename command/UI is a follow-up that can build on the new connector field.

## Changes

- **Form + handlers (apps/minds/imbue/minds/desktop_client)**
    - Rename the HTML input and template-renderer parameter from `agent_name` to `host_name` in `create.html`, `templates.py:render_create_form`, and `_handle_create_page` / `_handle_create_form_submit` / `_handle_create_agent_api` / `_re_render_with_error`.
    - Keep the visible label `"Name"`; keep the placeholder driven by `MINDS_WORKSPACE_NAME` (default `"assistant"`).
    - Validate the submitted value via `HostName(...)`; catch `InvalidName` and re-render the form (HTML) or return 400 JSON (API) with the validation message.
    - Update all `test_desktop_client.py` POST bodies and `agent_name=...` arguments accordingly.
- **Agent creator (apps/minds/imbue/minds/desktop_client/agent_creator.py)**
    - Introduce a module-level constant for the hardcoded agent name (e.g. `_DEFAULT_AGENT_NAME = AgentName("system-services")`); use it everywhere `mngr create` needs an agent name.
    - Replace the existing `_DEFAULT_AGENT_NAME` env-driven default (the one feeding the form) with a `_DEFAULT_HOST_NAME` env-driven default that continues to read `MINDS_WORKSPACE_NAME` and default to `"assistant"`.
    - Drop `_make_host_name` and stop computing addresses as `<agent_name>-host`. Build addresses as `system-services@<host_name>.<provider>` per launch mode.
    - Pass `--branch mngr/<host_name>` and `--label workspace=<host_name>` (instead of `workspace=<agent_name>`) to `mngr create`.
    - Update LiteLLM key metadata to `{"host_name": host_name}` (the prior `agent_name` key was for human discoverability of the lease's owner — same meaning, new name).
    - Adjust `start_creation` / `_create_agent_background` signatures to take `host_name` instead of `agent_name`; the `extract_repo_name` fallback (when the user submits empty) feeds the host name. Validate the resolved string via `HostName(...)` once at the boundary.
- **Landing page (apps/minds/imbue/minds/desktop_client)**
    - In whichever code path populates `agent_names` for `render_landing_page`, source the value from each agent's `labels.workspace` (falling back to the agent's `name` for legacy / non-minds agents that lack the label) so cards display the host name the user typed.
- **mngr_imbue_cloud plugin (libs/mngr_imbue_cloud)**
    - Add a required `host_name: HostName` field to `LeaseAttributes` (request side) and `LeaseResult` / `LeasedHostInfo` (response side); plumb it through `ImbueCloudConnectorClient.lease_host` and `list_hosts`.
    - In `ImbueCloudProvider.create_host`, validate the inbound `name` argument with `HostName(...)`, pass it on the lease request, and use the connector's echo as the authoritative value when constructing `ImbueCloudHost`.
    - In `ImbueCloudHost.create_agent_state`, stop rewriting `data.json:name`. Still merge in `options.label_options.labels` (which now contains `workspace=<host_name>`) and overwrite `data.json:created_branch_name` to `mngr/<host_name>`. Delete the line `merged_labels["workspace"] = str(new_name)` — it duplicates a label minds already passes and was tied to the now-removed agent-rename behavior.
    - In `_build_host_details_from_raw` and `discover_hosts(_and_agents)`, use the lease's `host_name` instead of the lease's `host_id` as the friendly host name.
    - Delete `host_label_for_agent`.
- **remote_service_connector (apps/remote_service_connector)**
    - New SQL migration `apps/remote_service_connector/migrations/002_host_name.sql` adding a non-null `host_name TEXT` column to `pool_hosts`, backfilling existing rows with `host_name = host_id`, and adding an index for lookups.
    - `LeaseHostRequest` in `app.py` gains a required `host_name: str` field; validate it against mngr's `HostName` regex inline (re-define the pattern locally — the connector should not import from `imbue.mngr`); reject non-conforming names with a 400.
    - `LeaseHostResponse` and the `/hosts` listing entries (`LeasedHostInfo` shape) include `host_name`; the `INSERT` / `UPDATE` in `lease_host` and the `SELECT` in `list_leased_hosts` are extended accordingly.
- **forever-claude-template**
    - Drop `MINDS_WORKSPACE_NAME` from `[commands.create].pass_env` in `.mngr/settings.toml`; no in-agent code reads it.
- **mngr core (libs/mngr)** — *no changes required*. The `default_branch_name(agent_name)` helper stays untouched; minds-driven branch naming is achieved entirely via the `--branch` flag.
- **Tests**
    - Update minds unit + integration tests (`test_desktop_client.py`, `agent_creator_test.py`, `mngr_imbue_cloud/*_test.py`) for the renamed form fields, the new constant agent name, the connector schema change, and the new label/branch wiring.
    - Add a unit test covering the form's `HostName` validation error path (HTML re-render + JSON 400).
    - Add a unit test for `ImbueCloudHost.create_agent_state` asserting `data.json:name` is preserved and `data.json:created_branch_name` is rewritten to `mngr/<host_name>`.
