# Plan: Let minds agents find past agents' transcripts

Enable minds agents to discover and read the chat history of any past (or live) agent, so they can recover "old stuff" a user references. Today an agent has no idea where destroyed agents' transcripts go, so this knowledge is added to the minds agents' default instructions and backed by a real access path through the minds backend.

## Overview

- When an agent is destroyed, its transcripts are preserved on the **controller/host** at `~/.mngr/preserved/{agent_name}--{agent_id}/` (see `get_preserved_agent_dir`, [preservation.py:239](libs/mngr/imbue/mngr/api/preservation.py:239)). Preserved files always live on the local/controller machine, **not** inside the agent VM ([preservation.py:247](libs/mngr/imbue/mngr/api/preservation.py:247)).
- A minds agent runs inside a remote workspace VM and cannot see the controller's filesystem. It reaches the backend only through the latchkey gateway (`$LATCHKEY_GATEWAY/minds-api-proxy/api/v1/...`), permission-gated ([agent_setup.py:62](libs/mngr_latchkey/imbue/mngr_latchkey/agent_setup.py:62)).
- The minds backend exposes a versioned REST API under `/api/v1`, with cross-workspace read endpoints shaped as `GET /workspaces/<agent_id>/<sub-resource>`, all gated by the `minds-workspaces` scope ([api_v1.py:1373](apps/minds/imbue/minds/desktop_client/api_v1.py:1373)). There is currently **no** endpoint that returns a transcript.
- **Permission model (verified against the latchkey gateway):** the `minds-workspaces-read` verb is `path.kind: tree`, whose JSON-Schema pattern (`^/minds-api-proxy/api/v1/workspaces(/|$)`, partial-match) authorizes **every** GET under `/workspaces` ([permission_requests.mjs:1175-1181](libs/mngr_latchkey/imbue/mngr_latchkey/extensions/permission_requests.mjs:1175)). So the two new GET endpoints need **no new gateway permission entry**, and reading is **cross-agent** (per-caller self-scoping applies only to `/agents/<id>/...`, never `/workspaces/...`). Caveat: `minds-workspaces` is **not** in the default agent baseline — the user must grant it (the same grant that lets an agent list/manage workspaces) before either endpoint is reachable ([agent_setup.py:122-129](libs/mngr_latchkey/imbue/mngr_latchkey/agent_setup.py:122)).
- The change is **access/awareness**, not search: surface where transcripts live and give agents a working way to fetch them — no grep/full-text search over transcripts.
- Delivered end-to-end across two repos: backend endpoints + permission wiring in this monorepo (`apps/minds`), and the default-instruction pointer + a `find-past-transcripts` skill in the external **forever-claude-template (FCT)** repo (minds agents' default context is the FCT-baked `/welcome`/`CLAUDE.md`, [agent_creator.py:599](apps/minds/imbue/minds/desktop_client/agent_creator.py:599)).

## Expected behavior

- A minds agent, by default, knows that destroyed agents' chat history is preserved and that it can be retrieved via the `find-past-transcripts` skill (referenced from the FCT `CLAUDE.md`).
- The agent can **list** every past agent that has a preserved transcript on the controller, newest-first, with enough metadata (agent name, id, created/destroyed timestamps) to map a vague user reference ("the agent that set up auth") to a specific id.
- The agent can **fetch** any agent's transcript by id — both **live** and **destroyed/preserved** agents — formatted the same way `mngr transcript` renders it, with optional `role`/`head`/`tail` filtering to keep the response focused.
- Discovery is authoritative against the on-disk preserved set under `~/.mngr/preserved/`, so genuinely-old agents (long gone from live discovery) are still found — not just recently-destroyed ones still lingering in `GET /workspaces`.
- Access requires **no new gateway permission entry**: both endpoints are GETs under `/workspaces`, already covered by the existing `minds-workspaces-read` tree verb. It is **not** automatic, though — the user must have granted the `minds-workspaces` scope (the same grant that enables listing/managing workspaces); without it the gateway denies the call, since `minds-workspaces` is not in the agent baseline.
- Fetching an unknown / never-preserved `agent_id` returns a clear **404**, consistent with existing `/workspaces/<agent_id>` handlers.
- Nothing changes for users who never ask about old work; the capability is latent until an agent needs it.

## Changes

### Backend (`apps/minds`, this monorepo)

- Add `GET /api/v1/workspaces/preserved`: lists the on-disk preserved agent set by scanning `get_preserved_agents_root_dir(...)` ([preservation.py:64](libs/mngr/imbue/mngr/api/preservation.py:64)). Returns all entries newest-first (no pagination), each with agent name, id, and created/destroyed timestamps. Authorized by the existing `minds-workspaces-read` tree verb — no new gateway entry.
- Add `GET /api/v1/workspaces/<agent_id>/transcript`: returns the agent's transcript for both live and preserved agents, reusing the existing `mngr transcript` formatting/resolution ([transcript.py:217](libs/mngr/imbue/mngr/cli/transcript.py:217)) and accepting `mngr transcript`-style `role`/`head`/`tail` query params. Unknown id → 404. Authorized by the same `minds-workspaces-read` tree verb.
- Register the literal `/workspaces/preserved` route so it resolves to its own handler (Werkzeug ranks static routes above the `<agent_id>` dynamic route; the existing `<agent_id>` detail handler also 404s unknown ids, so a stray `preserved` could never be mis-served). Verify this resolution during implementation. The `/workspaces/<agent_id>/transcript` route has an extra segment and does not collide.
- No change to `workspace_permissions.json` or the gateway: the `minds-workspaces-read` tree verb already covers both GETs (verified — see Overview). Do **not** add a per-endpoint permission entry.
- Add response models for the new endpoints alongside the existing `/api/v1` models (e.g. a preserved-listing response and a transcript response), following the current spectree-validated response pattern.
- Reuse existing preserved-agent path helpers in `libs/mngr` rather than duplicating the `~/.mngr/preserved/{name}--{id}` layout; if a "list all preserved agents" helper does not yet exist, add one next to `get_preserved_agents_root_dir` / `get_preserved_agent_dir` so the path structure stays single-sourced.
- Do not add an app-level self-only check in the transcript handler: cross-agent reads are intended, and the gateway already permits any `agent_id` under `minds-workspaces-read`. (Consideration to flag for reviewers: this means granting `minds-workspaces` now also exposes every agent's transcript, not just metadata — consistent with the existing cross-workspace read surface, but transcripts are more sensitive.)
- Add an `apps/minds/changelog/<branch>.md` entry describing the new endpoints.

### Default instructions + skill (forever-claude-template, external repo)

- Add a short section to the FCT `CLAUDE.md` telling the agent that past agents' chat history is preserved and pointing it at the `find-past-transcripts` skill (do not inline the mechanics into `CLAUDE.md`).
- Add a `.claude/skills/find-past-transcripts/` skill that documents and performs the flow: call `GET /workspaces/preserved` via `$LATCHKEY_GATEWAY/minds-api-proxy/api/v1/...` (using the gateway auth the agent already has) to discover candidates, then `GET /workspaces/<agent_id>/transcript` to read the chosen one, with `role`/`head`/`tail` to scope large transcripts. The skill must handle the not-granted case: if `minds-workspaces` has not been granted, the gateway denies the call — the skill should surface that and prompt the user to grant the scope (the same one used to list/manage workspaces), then retry.
- Land FCT changes via an `.external_worktrees/` worktree on the same branch name, committed in that repo (it carries its own changelog, separate from this monorepo's).

### Testing

- Unit tests for the preserved-dir listing logic (enumeration, newest-first ordering, name/id/timestamp extraction) and the transcript-fetch resolution (live vs preserved, unknown-id 404, `role`/`head`/`tail` handling).
- An acceptance test exercising both endpoints through the minds API: create/destroy an agent so a transcript is preserved, then assert it appears in `GET /workspaces/preserved` and is readable via `GET /workspaces/<agent_id>/transcript`.
- Use existing minds API test fixtures rather than introducing new harness code.
