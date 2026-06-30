# Plan: Let minds agents find past agents' transcripts

Enable minds agents to discover and read the chat history of any past (or live) agent, so they can recover "old stuff" a user references. Today an agent has no idea where destroyed agents' transcripts go, so this knowledge is added to the minds agents' default instructions and backed by a real access path through the minds backend.

> **As-built note (reconciled to the implementation).** Two things changed from the original design during code-reading and CI:
> 1. `mngr transcript` / `resolve_events_target` read from the agent's **host volume** (`agents/<id>/events`), **not** from `~/.mngr/preserved/`. So for a fully destroyed agent the durable preserved copy is the only source, and shelling out to `mngr transcript` does not work for it. The backend therefore reads the preserved copy **in-process** (via a new public `imbue.mngr.api.transcript` module + preservation helpers) for destroyed agents, and only falls back to subprocess `mngr transcript` for **live** agents.
> 2. The FCT side reuses the existing **`minds-api` skill** (which already documents the gateway + `minds-workspaces-read` permission flow) instead of a standalone skill that re-documents it; the new `find-past-transcripts` skill defers to it for the mechanics. Implemented in [imbue-ai/mngr#2344](https://github.com/imbue-ai/mngr/pull/2344) and [forever-claude-template#232](https://github.com/imbue-ai/forever-claude-template/pull/232).

## Overview

- When an agent is destroyed, its transcripts are preserved on the **controller/host** at `~/.mngr/preserved/{agent_name}--{agent_id}/` (see `get_preserved_agent_dir`, [preservation.py:239](libs/mngr/imbue/mngr/api/preservation.py:239)). Preserved files always live on the local/controller machine, **not** inside the agent VM ([preservation.py:247](libs/mngr/imbue/mngr/api/preservation.py:247)).
- A minds agent runs inside a remote workspace VM and cannot see the controller's filesystem. It reaches the backend only through the latchkey gateway (`$LATCHKEY_GATEWAY/minds-api-proxy/api/v1/...`), permission-gated ([agent_setup.py:62](libs/mngr_latchkey/imbue/mngr_latchkey/agent_setup.py:62)).
- The minds backend exposes a versioned REST API under `/api/v1`, with cross-workspace read endpoints shaped as `GET /workspaces/<agent_id>/<sub-resource>`, all gated by the `minds-workspaces` scope ([api_v1.py:1373](apps/minds/imbue/minds/desktop_client/api_v1.py:1373)). There is currently **no** endpoint that returns a transcript.
- **The durable preserved copy is the only source for a fully destroyed agent.** `mngr transcript` / `resolve_events_target` read events from the agent's host volume ([events.py](libs/mngr/imbue/mngr/api/events.py)), which is gone once the host is fully destroyed; `find_one_agent` uses `include_destroyed=False`. So the backend reads `~/.mngr/preserved/{name}--{id}/events/<source>/common_transcript/events.jsonl` directly (in-process) for destroyed agents, and uses `mngr transcript` only as a live fallback.
- **Permission model (verified against the latchkey gateway):** the `minds-workspaces-read` verb is `path.kind: tree`, whose JSON-Schema pattern (`^/minds-api-proxy/api/v1/workspaces(/|$)`, partial-match) authorizes **every** GET under `/workspaces` ([permission_requests.mjs:1175-1181](libs/mngr_latchkey/imbue/mngr_latchkey/extensions/permission_requests.mjs:1175)). So the two new GET endpoints need **no new gateway permission entry**, and reading is **cross-agent** (per-caller self-scoping applies only to `/agents/<id>/...`, never `/workspaces/...`). Caveat: `minds-workspaces` is **not** in the default agent baseline — the user must grant it (the same grant that lets an agent list/manage workspaces) before either endpoint is reachable ([agent_setup.py:122-129](libs/mngr_latchkey/imbue/mngr_latchkey/agent_setup.py:122)).
- The change is **access/awareness**, not search: surface where transcripts live and give agents a working way to fetch them — no grep/full-text search over transcripts.
- Delivered end-to-end across two repos: backend endpoints + permission wiring in this monorepo (`apps/minds`), and the default-instruction pointer + a `find-past-transcripts` skill in the external **forever-claude-template (FCT)** repo (minds agents' default context is the FCT-baked `/welcome`/`CLAUDE.md`, [agent_creator.py:599](apps/minds/imbue/minds/desktop_client/agent_creator.py:599)).

## Expected behavior

- A minds agent, by default, knows that destroyed agents' chat history is preserved and that it can be retrieved via the `find-past-transcripts` skill (referenced from the FCT `CLAUDE.md`).
- The agent can **list** every past agent that has a preserved transcript on the controller, newest-first, with enough metadata (agent name, id, and `preserved_at` — the preserved directory's mtime, which approximates the destroy time; a separate created-at is not preserved) to map a vague user reference ("the agent that set up auth") to a specific id.
- The agent can **fetch** any agent's transcript by id — both **live** and **destroyed/preserved** agents — formatted the same way `mngr transcript` renders it, with optional `role`/`head`/`tail` filtering to keep the response focused.
- Discovery is authoritative against the on-disk preserved set under `~/.mngr/preserved/`, so genuinely-old agents (long gone from live discovery) are still found — not just recently-destroyed ones still lingering in `GET /workspaces`.
- Access requires **no new gateway permission entry**: both endpoints are GETs under `/workspaces`, already covered by the existing `minds-workspaces-read` tree verb. It is **not** automatic, though — the user must have granted the `minds-workspaces` scope (the same grant that enables listing/managing workspaces); without it the gateway denies the call, since `minds-workspaces` is not in the agent baseline.
- Fetching an unknown / never-preserved `agent_id` returns a clear **404**, consistent with existing `/workspaces/<agent_id>` handlers.
- Nothing changes for users who never ask about old work; the capability is latent until an agent needs it.

## Changes

### mngr library (`libs/mngr`)

- New public module `imbue.mngr.api.transcript`: the transcript rendering (`get_event_role`, `parse_transcript_events`, `format_event_human`, `render_transcript_to_string`, `apply_head_or_tail`) extracted from `imbue.mngr.cli.transcript` so it can be reused programmatically. The CLI now imports these; its behavior is unchanged. Pure-function unit tests moved to `api/transcript_test.py`.
- New preservation helpers in `imbue.mngr.api.preservation` (next to `get_preserved_agents_root_dir`/`get_preserved_agent_dir`, single-sourcing the `{name}--{id}` layout): `PreservedAgentInfo`, `list_preserved_agents(host_dir)` (newest-first by dir mtime; skips unparseable dirs; splits on the last `--` since `AgentId` never contains `--`), `find_preserved_agent_by_id`, and `read_preserved_common_transcript` (globs `events/*/common_transcript/events.jsonl`).
- New `render_preserved_agent_transcript(host_dir, agent_id, roles, head, tail, output_format)` in `api.transcript` ties them together and returns `None` when the agent has no preserved transcript.

### minds backend (`apps/minds`)

- Add `GET /api/v1/workspaces/preserved`: in-process `list_preserved_agents(state.mngr_host_dir)`, returned newest-first (no pagination) as `{agent_id, agent_name, preserved_at}`. Authorized by the existing `minds-workspaces-read` tree verb — no new gateway entry.
- Add `GET /api/v1/workspaces/<agent_id>/transcript`: tries `render_preserved_agent_transcript` first (the durable copy, in-process); if not preserved, falls back to the live agent via subprocess `mngr transcript <id> --format ... [--role/--head/--tail]` (consistent with how minds reaches agent state). Returns `{agent_id, format, is_preserved, content}`. Accepts `format`/`role`/`head`/`tail` query params; both formatters share the same rendering code. Unknown id (not preserved and not a known live workspace) → 404; malformed id → 400; invalid `format` / both `head`+`tail` → 400.
- Register the literal `/workspaces/preserved` route before the `<agent_id>` route (Werkzeug ranks static above dynamic regardless; ordering it first keeps intent explicit). The `/workspaces/<agent_id>/transcript` route has an extra segment and does not collide.
- No change to `workspace_permissions.json` or the gateway: the `minds-workspaces-read` tree verb already covers both GETs (verified — see Overview).
- Add response models `PreservedAgentSummary`, `PreservedAgentsResponse`, `WorkspaceTranscriptResponse` in `api_models.py`, and **register both routes in the published API schema** (`api_schema.py` `_ROUTE_MODELS`) — the schema test enforces that documented response models match the handlers' enforced models.
- Do not add an app-level self-only check in the transcript handler: cross-agent reads are intended, and the gateway already permits any `agent_id` under `minds-workspaces-read`. (Consideration for reviewers: granting `minds-workspaces` now also exposes every agent's transcript, not just metadata — consistent with the existing cross-workspace read surface, but transcripts are more sensitive.)
- Add `apps/minds/changelog/<branch>.md` and `libs/mngr/changelog/<branch>.md` entries.

### Default instructions + skill (forever-claude-template, external repo)

- Add a `.claude/skills/find-past-transcripts/` skill that performs the flow (list `GET /workspaces/preserved`, match by name/time, read `GET /workspaces/<id>/transcript` with `format`/`role`/`head`/`tail`). It **reuses the existing `minds-api` skill** for the gateway address and the `minds-workspaces-read` permission-request flow rather than re-documenting them, and notes the 403-then-request-grant path.
- Add the two endpoints to the `minds-api` skill's read section with a cross-reference to `find-past-transcripts`.
- Add a short "Finding past work" section to the FCT `CLAUDE.md` so agents know by default that earlier agents' chat history is preserved and retrievable via the skill.
- Land FCT changes in an `.external_worktrees/` checkout on the same branch name, committed in that repo (its own changelog).

### Testing

- mngr unit tests: the rendering functions (`api/transcript_test.py`) and the preservation enumeration/read helpers (`preservation_test.py`), using real on-disk preserved directories under a temp `host_dir`.
- minds integration tests through the Flask test client (`api_v1_test.py`): preserved-listing order, preserved transcript retrieval with `role`/`head` filters, the live fallback via a fake `mngr` binary, and the 400/404/401 cases; plus the api-schema test now covering the new routes.
- A manual-test plan (`manual-test-plan.md`) for the parts that need a running stack (a real destroyed agent, the latchkey gateway, the agent-in-VM skill flow).
