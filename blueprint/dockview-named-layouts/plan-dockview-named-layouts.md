# Plan: Named dockview layouts for the default workspace template

## Overview

- Replace the single implicit dockview layout (`workspace_layout/layout.json` in the system_interface of default-workspace-template) with multiple named layouts stored as separate JSON files, plus a small amount of workspace-level layout state (display names, last-active tracking).
- The template ships two default layout names, `desktop` and `mobile`. Defaults are lazy: the names always exist, and a layout with no saved content triggers the existing auto-open-welcome-chat behavior on the client, whose autosave then materializes the file. An existing `layout.json` is migrated to become `desktop`.
- Each browser client keeps its own "active layout" (persisted in localStorage, first-time default chosen by user agent: mobile vs desktop). All existing autosave/restore behavior is unchanged except that it targets the active layout's file.
- The "+" menu gains a bottom section with "Save layout...", "Load layout...", and "Delete layout..." dialogs, plus live cross-client sync (re-apply on save by another client, auto-switch on delete).
- Agents learn which client/layout a request came from via a new workspace-level append-only `events.jsonl` (message + layout-switch events) and a new `layout.py context` subcommand; mutating `layout.py` ops become layout-targeted (`--layout`, required) and only work when a connected client has that layout active — no server-side JSON mutation of layouts.

## Expected behavior

### Layout storage and defaults

- Layouts are named, server-persisted dockview states (same `SavedLayout` shape as today: dockview JSON + panelParams), one JSON file per layout under the primary agent's `workspace_layout/` dir. Filenames are slugs; display names are free-form and preserved. A save whose slug collides with a *different* existing layout's slug is rejected with a clear error.
- `desktop` and `mobile` always exist as layout names, even before any content is saved. Loading a layout with no saved content behaves exactly like today's "no saved layout" path: the client auto-opens the initial welcome chat tab and its autosave materializes the file.
- On first request after upgrade, a legacy `workspace_layout/layout.json` is migrated to be the `desktop` layout's content; `mobile` starts fresh. No other migration.

### Client behavior

- On first connect ever (no localStorage selection), the client picks `mobile` if the user agent is mobile (`navigator.userAgentData?.mobile`, falling back to a UA-string regex), else `desktop`. If that layout name no longer exists, it falls back to the first existing layout.
- The selection persists per browser in localStorage; every subsequent connect restores that layout (falling back as above if it was deleted).
- Every dockview change auto-persists (existing 1.5 s debounce) to the client's active layout. Switching layouts (load, save-as, delete-fallback) flushes any pending autosave of the old layout first, so no changes are lost.
- The "+" menu (both the group-header button and the empty-state overlay) gains a divider and three items at the bottom:
  - "Save layout...": dialog with the list of existing layouts and a free-text name field, prefilled with the active layout's name (one-click save-over-current). Saving under a new name creates it and switches the client's active layout to it.
  - "Load layout...": dialog listing all layouts; selecting one applies it and makes it the autosave target.
  - "Delete layout...": dialog listing all layouts; the only guard is that the last remaining layout cannot be deleted.
  - All three dialogs mark the client's active layout with "(current)".
- Live cross-client sync: when a client saves a layout, other connected clients with that same layout active re-apply it. When a layout is deleted, clients with it active automatically switch to the first remaining layout and show a brief notice.

### Event recording (agent context)

- A workspace-level append-only events file lives next to the layouts (e.g. `workspace_layout/events/client_activity/events.jsonl`), following the repo's standard event envelope (timestamp, type, event_id, source). No rotation in v1.
- Every message sent through the UI appends a message event: message text, time, target agent, client id (uuid minted per browser in localStorage), and the client's active layout at send time.
- Every layout switch appends a switch event (client id, old/new layout), regardless of initiator — user action, agent-driven `layout.py load`, or delete-fallback.
- Messages sent outside the UI (e.g. directly in tmux) have no metadata; the skill instructs the agent to fall back to the last active layout, mutate all layouts, or ask the user.

### Agent-facing layout ops (`scripts/layout.py`)

- Mutating ops (`open`, `split`, `close`, `move`, `rename`, `focus`, `maximize`, `restore`, `replace-url`) require an explicit `--layout <name>`; omitting it is an error, and the skill instructs the agent to always pass it. The op broadcasts only to connected clients whose active layout matches; if none, it fails with a clear error the agent can relay (no server-side JSON mutation).
- If multiple clients share the target layout, all of them apply the op (they converge via autosave + live sync).
- Read-only `inspect` / `list` / `where` accept `--layout` and read that named layout's persisted file directly (works with no client connected). Default resolution when omitted: the last active layout.
- New `layout.py load <name>`: switches the requesting client (resolved from message metadata; falls back to all clients when the requester is unknown) onto that layout, so the agent can then mutate it.
- New `layout.py context`: prints, per known client — client id, device kind from the UA (mobile/desktop), current layout, last-seen time, and the last ~5 messages (long messages truncated) — so the agent can figure out which client/layout a request refers to.
- The `manage-layout` skill is updated to document layout targeting, `context`, `load`, the `--layout` requirement, and the no-metadata fallback guidance.

## Changes

All changes land in the `default-workspace-template` repo (worked on via an `.external_worktrees/` checkout per monorepo convention), primarily `apps/system_interface`; a changelog entry is added there. No mngr monorepo code changes are expected beyond this blueprint (and vendor sync happens through the normal release flow).

- **Server (`imbue/system_interface/server.py` + a new layouts module)**
  - Replace the single-file GET/POST `/api/layout` with named-layout endpoints: list layouts (slug, display name, current-content flag), get/save a named layout, delete a named layout (guarding the last one), all rooted in the existing `workspace_layout/` dir.
  - Slug validation/derivation from display names; reject slug conflicts on save.
  - Legacy `layout.json` migration to `desktop` on first access.
  - Track last-active layout and connected clients' active layouts (clients report their client id + active layout on WS connect and on switch).
  - Append message events (in the message-send endpoint) and layout-switch events to the workspace-level `events.jsonl` per the standard envelope.
  - WS broadcasts for live sync: "layout saved" (clients on it re-apply) and "layout deleted" (clients on it switch + notice), plus the agent-driven "load layout" op routed to the requesting client.

- **Layout broadcast path (`imbue/system_interface/layout_ops.py`)**
  - Mutating ops carry the target layout and are delivered only to matching clients; error when no client has the layout active.
  - `inspect`/`list` resolve a named layout file (defaulting to last active).
  - New `context` op assembled from `events.jsonl` (per-client recent messages, current layout, device kind, last-seen).
  - New `load` op (switch requesting client, fall back to all clients).

- **Frontend (`frontend/src/views/DockviewWorkspace.ts` + new dialog view(s), `models/AgentManager.ts`, `views/MessageInput.ts` or message-send path)**
  - Client id (uuid) + active layout name in localStorage; UA-based first-time default with fall-back to first existing layout.
  - Load/save against named-layout endpoints; autosave targets the active layout; flush pending autosave before any switch.
  - "+" menu bottom section with the three dialogs (Save prefilled with current name; "(current)" markers; last-layout delete guard surfaced from the server).
  - Handle new WS messages: re-apply on same-layout save by another client, auto-switch + notice on delete, agent-driven load, and layout-targeted layout ops (apply only when the target layout is active).
  - Send client id + active layout with every chat message; report active layout over WS on connect and on switch.

- **Agent tooling (`scripts/layout.py`, `.agents/skills/manage-layout/SKILL.md`)**
  - Required `--layout` on mutating subcommands; optional on read-only ones; new `load` and `context` subcommands.
  - Skill doc updates: layout targeting workflow (context → load if needed → mutate), no-metadata fallback guidance (last active layout / all layouts / ask).

- **Tests**
  - Server: named-layout CRUD, slug conflicts, migration, last-layout delete guard, event appends, last-active tracking.
  - Layout ops: `--layout` routing/error paths, `context` assembly, `load` targeting.
  - Frontend unit tests where the existing suite has coverage (e.g. UA default choice, slug/display handling in dialogs); e2e layout pipeline test updated for named layouts.
