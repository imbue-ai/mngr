# Plan: Refactor creating-page phase status to use the enum as the single source of truth

> Refactor the creating-page phase status mechanism to use the existing `AgentCreationStatus` enum / `_STATUS_TEXT_*` maps as the single source of truth, per the architecture reviewer's recommendation.
>
> * Replace the current 4-value enum with a fully renamed set: `INITIALIZING`, `CLONING_REPO`, `CHECKING_OUT_BRANCH`, `PROVISIONING_AI`, `CREATING_WORKSPACE`, `WAITING_FOR_READY`, `DONE`, `FAILED`. All phases live in the enum unconditionally — modes/configs that don't apply just skip the phase and `_statuses` jumps to the next applicable one.
> * `start_creation()` sets the initial status to `INITIALIZING`; the worker thread updates to `CLONING_REPO` as its first action. The user briefly sees a generic "Starting..." caption between thread-start and the first emission.
> * Drop `PHASE_PREFIX` / `emit_phase` and the `__PHASE__:` log-queue multiplexing entirely. The worker thread updates `_statuses` under `self._lock` at each transition.
> * `_stream_creation_logs` polls `get_creation_info(creation_id)` once per loop iteration (every ~1s, the existing keepalive cadence) and emits a status SSE event whenever the status changes since last seen. ~1s caption-update latency is acceptable.
> * SSE wire: `{"_type": "status", "status": "<ENUM_VALUE>", "status_text": "..."}` — isomorphic to the existing `_handle_creation_status_api` response.
> * Caption resolution lives in `templates.py` next to `_STATUS_TEXT_DEFAULT` / `_STATUS_TEXT_IMBUE_CLOUD`. The "Checking out branch '<name>'..." interpolation is dropped — caption becomes the static "Checking out branch...".
> * `creating.js` swaps the `data._type === 'phase'` branch for a `data._type === 'status'` branch that sets `#status-text` to `data.status_text`.
> * The new test for SSE status dispatch mutates `_statuses` (via `_lock`) between SSE poll iterations and asserts the resulting `{"_type": "status", ...}` events.
> * `test_sse_redirect.py` is updated to seed with the new initial enum value.
> * The `/api/create-agent/{id}/status` wire is allowed to change (rename `CLONING` → `CLONING_REPO`, `CREATING` → `CREATING_WORKSPACE`, etc.); no external consumers known.

## Overview

- The current `gabriel/creation-status` branch adds a *second* "phase" concept on top of the existing `AgentCreationStatus` enum, with phase strings constructed inline in the worker thread and multiplexed onto the log queue via a `__PHASE__:` prefix.
- The architecture reviewer flagged this as a missed opportunity: the existing enum + `_STATUS_TEXT_*` maps in `templates.py` are already the natural place for phase state and UI copy, and the new parallel mechanism makes `get_creation_info()` lie about what the worker is actually doing.
- This refactor folds the phase concept into `AgentCreationStatus` directly, so `_statuses` is the single source of truth.
- UI captions move back into `templates.py` text maps. The "Checking out branch '<name>'..." interpolation is dropped (no simple way to keep it without re-introducing a parallel detail channel; static "Checking out branch..." is acceptable since the user just typed the branch into the form).
- Wire format on the SSE stream switches from `{"_type": "phase", ...}` to `{"_type": "status", "status": "<ENUM>", "status_text": "..."}`, mirroring the existing `_handle_creation_status_api` JSON response.

## Expected behavior

- The "Creating your project" page caption advances through the same five phases the current branch already exposes: cloning → branch checkout (if applicable) → AI access provisioning (IMBUE_CLOUD only) → workspace creation → waiting for workspace to be ready → done.
- IMBUE_CLOUD launch-mode wording (`"Connecting to host..."`, `"Setting up agent..."`) is preserved for the corresponding phases via `_STATUS_TEXT_IMBUE_CLOUD`.
- Caption-update latency: up to ~1s, because `_stream_creation_logs` polls `get_creation_info` on the same cadence it already uses for keepalives. In practice each backend phase takes much longer than 1s, so this is imperceptible.
- The initial caption (between `start_creation()` returning and the worker's first phase emission) shows a new generic "Starting..." caption mapped from the new `INITIALIZING` enum value.
- The branch-name is no longer interpolated into the "Checking out branch..." caption; the caption is static.
- `GET /api/create-agent/{id}/status` returns the new enum values (`CLONING_REPO`, `CHECKING_OUT_BRANCH`, etc.) instead of the previous `CLONING` / `CREATING`. No external consumers known, so this is treated as a free rename.
- `creating.js` continues to handle the `done` event for the redirect/failure path; it now handles a `status` event for caption updates instead of a `phase` event.
- All log-line streaming behavior on the SSE channel is unchanged.

## Changes

- Expand `AgentCreationStatus` in `apps/minds/imbue/minds/desktop_client/agent_creator.py` to: `INITIALIZING`, `CLONING_REPO`, `CHECKING_OUT_BRANCH`, `PROVISIONING_AI`, `CREATING_WORKSPACE`, `WAITING_FOR_READY`, `DONE`, `FAILED`. Remove the old `CLONING` and `CREATING` values.
- Delete `PHASE_PREFIX` and `emit_phase()` from `agent_creator.py`.
- In `AgentCreator.start_creation()`, set the initial `_statuses[cid_str]` to `INITIALIZING` instead of `CLONING`.
- In `AgentCreator._create_agent_background()`, replace each `emit_phase(...)` call with a `_statuses[cid_str] = <new value>` mutation under `self._lock`, at the same five transition points the current branch covers (start of cloning, branch checkout, IMBUE_CLOUD credential mint, start of `mngr create`, start of workspace-readiness probe). Conditional phases are simply skipped when they don't apply (e.g. `PROVISIONING_AI` only fires for `ai_provider == IMBUE_CLOUD`).
- In `apps/minds/imbue/minds/desktop_client/templates.py`, extend `_STATUS_TEXT_DEFAULT` and `_STATUS_TEXT_IMBUE_CLOUD` to cover every non-terminal enum value with the same copy the current branch uses (default vs IMBUE_CLOUD launch-mode wording where they diverge). The `INITIALIZING` value maps to a short "Starting..." (or equivalent) caption.
- In `apps/minds/imbue/minds/desktop_client/app.py`, rewrite `_stream_creation_logs` so each loop iteration: (1) drains any new log lines into `data: {"log": "..."}\n\n` SSE frames as today; (2) calls `agent_creator.get_creation_info(creation_id)`, compares `info.status` to the last-emitted status, and emits a `data: {"_type": "status", "status": "<ENUM>", "status_text": "..."}\n\n` frame whenever it changes. Resolve `status_text` via the same `_STATUS_TEXT_*` mapping (factor out a small helper in `templates.py` if convenient — e.g. `status_text_for(status, launch_mode)`). Drop the `PHASE_PREFIX` import/handling.
- In `apps/minds/imbue/minds/desktop_client/static/creating.js`, replace the `data._type === 'phase' && data.status_text` branch with a `data._type === 'status' && data.status_text` branch that sets `statusTextEl.textContent = data.status_text`. The `done` event handling is unchanged.
- Update the unit test `test_creation_logs_sse_emits_phase_events` in `apps/minds/imbue/minds/desktop_client/test_desktop_client.py` to drive a real status transition: seed the queue with a regular log line, mutate `agent_creator._statuses[creation_id]` to a new phase under `_lock`, then assert the SSE stream emits the corresponding `{"_type": "status", ...}` event with the right `status_text`. Rename the test (e.g. `test_creation_logs_sse_emits_status_events`) and update its docstring. Imports drop `PHASE_PREFIX`.
- Update `apps/minds/test_sse_redirect.py` to seed `_statuses[agent_id] = AgentCreationStatus.INITIALIZING` (or `CLONING_REPO`, whichever matches the new initial state contract) instead of `AgentCreationStatus.CLONING`.
- Update `apps/minds/imbue/minds/desktop_client/agent_creator_test.py` and any other test/source files that reference `AgentCreationStatus.CLONING` or `AgentCreationStatus.CREATING` to use the new enum values.
- The HTTP handlers in `app.py` (`_handle_creating_page`, `_handle_creation_status_api`) need no logic changes — they already stringify whatever `info.status` is. Their wire output changes implicitly to the new enum values.
- Update the changelog entry at `changelog/gabriel-creation-status.md` to mention that the status enum was expanded (the user-visible spinner-update behavior is unchanged from the current branch, but the API wire format changed).
- No template HTML changes; the initial caption in `creating.html` is still resolved server-side via `render_creating_page` from the (new) initial enum value.

✓ Explore  ✓ Plan  ● Write  ○ Refine
