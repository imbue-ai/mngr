# Workspace creation onboarding

Refactor the minds "Create New Workspace" screen to match the `~/Downloads/create-workspace.html` prototype: a minimal name-first form, then 3 onboarding questions answered *while* the workspace is created in the background, then a styled loading screen shown only if creation hasn't finished. The prototype is a guide for flow and copy, not exact code.

## Overview

- Split workspace creation into two perceived phases that overlap in time: the user answers 3 short onboarding questions while `mngr create` runs in the background, so the wait is hidden behind useful interaction.
- Collapse the current dense create form to a name-first layout (Name + Create up front; "Configure…" reveals Compute/AI/Backup; "Show advanced" reveals Repo/branch/GH_TOKEN; account moves to a top-right menu). The existing POST handler and validation are preserved -- this is a presentation change plus a new question flow on top.
- Each of the 3 questions is optional via a built-in no-op path: Q1 "full control" gathers nothing, an empty Q2 sends no message, an empty Q3 writes no file. These are the seeds of features we will extend later; for now each does the minimal thing.
- Q1 kicks off a local background scan (the user's name from the machine) and writes a small JSON file; Q2's text is sent to the chat agent as a follow-up message after it comes online; Q3's text is written into the workspace's Claude memory as a permissions-preferences file.
- The loading screen appears only if creation isn't done by the time the user finishes Q3; its progress bar is a client-side time-based animation tuned per compute provider. Ideally the user never sees it.
- All changes are scoped to `apps/minds` -- `/welcome` is preserved, so no forever-claude-template change is required.

## Expected behavior

### Form

- Visiting `/create` shows a minimal form: a **Name** field and a **Create** button.
- A "Configure…" disclosure reveals the **Compute provider**, **AI provider**, and **Backup provider** dropdowns; their conditional sub-fields (Anthropic API key, restic env, encryption method, master password) still appear exactly as today when their parent option is selected.
- A "Show advanced" disclosure (inside Configure) reveals **Repository** (git URL or local path), **Branch**, and **GH_TOKEN**.
- The **account** selector moves from an inline form row to a top-right menu (matching the prototype), and still drives the existing "imbue_cloud requires a selected account" validation.
- Submitting the form continues to post to the existing handler with the same fields and same server-side validation; validation errors re-render the form as today.

### Question flow (web UI)

- Clicking **Create** immediately starts background creation (as today) AND advances the UI to question 1 of 3 -- the user does not wait on a separate progress page first.
- The header of each question screen shows a "Creating workspace" pulse indicator and a "N of 3" step counter (prototype copy).
- **Q1 -- "Is it OK if I get to know you?"** Options: convenience / privacy / control (privacy pre-selected, matching the prototype). Choosing any option except "full control" kicks off the local background scan. "Full control" does nothing.
- **Q2 -- "What should we start with?"** A free-text "describe the problem" option (selected by default, empty) plus editable template presets (task management, inbox triage, news digest, dashboard). The selected option's text becomes the Q2 answer.
- **Q3 -- "How do you want to deal with permissions?"** Two editable presets (safety pre-selected, convenience), each an editable textarea. The selected option's text becomes the Q3 answer.
- Each question has a sensible default pre-selected, so the user can advance with a single click; Back/Next navigation matches the prototype.
- After Q3:
  - If creation has already completed, the user goes straight into the workspace (no intermediate screen).
  - If creation is still running, the styled loading screen is shown and auto-redirects into the workspace the moment creation completes. There is no explicit "Workspace ready / Open workspace" screen.

### Loading screen

- Shows a title, a rotating hint line, a progress bar, the current stage caption, and a "Show details" toggle that expands the existing streamed creation log.
- The progress bar is a client-side time-based animation: it eases toward ~80% over the expected duration for the selected compute provider (DOCKER 30s, LIMA 300s, CLOUD 300s, IMBUE_CLOUD 30s; fallback 60s if unknown), then asymptotically approaches the last 20% if creation runs long, and snaps to 100% on completion.
- The stage caption and the detail log continue to reflect real creation status/events streamed over the existing SSE channel; only the bar itself is time-based.
- Rotating hints are a fresh, minds-accurate set (backups, privacy, account switching, telegram, sharing), not the prototype's verbatim copy.

### Side effects of the answers

- **Q1 (data preference):** When not "full control," a background process on the user's machine resolves the user's name (git `user.name`, then OS full name / GECOS, then login username) and writes `{name, details: "couldn't find any details"}` to `~/.minds/user_context/<creation-id>.json`. Nothing consumes this file yet. "Full control" writes nothing.
- **Q2 (initial problem):** When non-empty, the selected text is delivered to the bootstrap-created chat agent (named `<host_name>`) as a follow-up `mngr message`, leaving the baked-in `/welcome` intact (welcome first, then the user's problem statement). Delivery waits for an agent named `<host_name>` to appear, then sends; on timeout it logs and gives up. Empty text sends nothing.
- **Q3 (permissions preference):** When non-empty, after the workspace is ready the selected (editable) text is written to `runtime/memory/permissions_preferences.md` inside the workspace via `mngr exec` against the services agent's canonical `AgentId` (create/overwrite, creating the memory dir if missing). Empty text writes nothing.

### API

- `POST /api/create-agent` accepts three new optional fields: `user_data_preference` (`CONVENIENCE` | `PRIVACY` | `CONTROL`), `initial_problem` (string), `permissions_preference` (string).
- Absent or empty fields map to the no-op path for each, so existing API callers and tests are unaffected (current behavior unchanged when the fields are omitted).
- No extra persistence of the answers beyond the side effects above.

## Changes

### Create form UI

- Restructure `desktop_client/templates/create.html` into the name-first layout with a "Configure…" disclosure (Compute/AI/Backup + their existing conditional sub-fields) and a nested "Show advanced" disclosure (Repo/branch/GH_TOKEN), styled to match the prototype.
- Move the account selector into a top-right menu while keeping it wired to the existing imbue_cloud account-requirement validation.
- Keep the existing POST target, field names, and server-side validation in `_handle_create_form_submit` unchanged.

### Question flow

- Add the 3 question screens (Q1/Q2/Q3) using the prototype's copy, defaults, editable presets, and Back/Next navigation, shown after the user clicks Create.
- Drive the screens client-side, starting background creation on Create and tracking creation status so that finishing Q3 either enters the workspace directly or falls through to the loading screen.
- Collect the three answers client-side and submit them so the backend can apply the side effects.

### Loading screen

- Replace the current minimal `creating.html` progress UI with the prototype-style loading screen: progress bar, rotating hints, stage caption, and a "Show details" toggle over the existing streamed log.
- Make the progress bar a client-side time-based animation parameterized by the selected provider's expected duration, independent of the SSE stage events (which still drive the caption and detail log).
- Keep the existing auto-redirect-on-completion behavior; drop any explicit "ready" screen.

### Backend wiring

- Extend the creation request/handling path (`_handle_create_form_submit`, `_handle_create_agent_api`, and `agent_creator.start_creation` / its background worker) to accept and thread through the three optional answers.
- Add the Q1 local user-name scan as a background process that writes the per-creation JSON under `~/.minds/user_context/`, gated on the data preference (skipped for "full control").
- Add Q2 delivery: after creation, wait for the chat agent named `<host_name>` and send the initial-problem text via the existing `mngr message` helper (no-op when empty).
- Add Q3 application: after the workspace is ready, write the permissions-preference text to `runtime/memory/permissions_preferences.md` via `mngr exec` against the services agent id (no-op when empty).
- Add the three optional fields to the `POST /api/create-agent` request parsing, mapping absent/empty values to the no-op paths.

### Supporting changes

- Add new primitives/enums as needed for the data preference and for the answers passed through creation (following the minds `primitives.py` conventions).
- Add a per-provider expected-duration mapping for the loading-bar timing (DOCKER 30s, LIMA 300s, CLOUD 300s, IMBUE_CLOUD 30s, fallback 60s).
- Add the new static JS/CSS needed for the question flow and the restyled loading screen (extending the existing `static/creating.js` patterns).
- Add a changelog entry under `apps/minds/changelog/` for this branch.

### Tests

- Unit-test the Q1 name-resolution fallback order and JSON output, the per-provider duration mapping, and the no-op handling for each empty/absent answer.
- Extend the desktop client tests to cover the new optional API fields (present and absent), Q2 message delivery (including the wait-for-agent + timeout path), and Q3 file write via `mngr exec`.
- Verify the form still validates and creates as before, and that the question flow short-circuits straight into the workspace when creation finishes before Q3.
