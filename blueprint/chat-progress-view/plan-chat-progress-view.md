# Minds Chat — Progress View

## Refined prompt

> Fetch this design file, read its readme, and implement the relevant aspects of the design. https://api.anthropic.com/v1/design/h/5tMCxah6DGHF9wqFoEpoGg?open_file=Minds+Chat+-+Progress+View.html
> Implement: Minds Chat - Progress View.html
>
> it's relevant to also look at ~/utilities/mngr/.external_worktrees/forever-claude-template-main - i think there will be necessary updates there two. basically this requires a two-fold change:
> 1. very strong prompting for the agent around task management (in the template)
> 2. handling of the task management to render in the new ui (on the mngr side)
>
> right now agents tend to use claude code's built in todo tool for task management. you can research online about it (or just think about it since you should have access to that tool). i don't think that that tool supports the sort of final summary message that the design calls for, though. so we may need to switch to a different tool or figure out another way to handle this. we've explored using https://github.com/wedow/ticket in the template; it's possible it could handle this function? but i'm open to suggestions.
>
> but let's plan out the spec for the template- and mngr-side changes to make this improved progress view work
>
> * Use the already-vendored `tk` ticket tracker as the agent's task primitive (no TodoWrite + sidecar combination — too much per-task overhead).
> * Use tk as-is for now (no fork); the agent records the per-task summary by running `tk add-note <id> "<summary>"` immediately before `tk close <id>`.
> * Disable Claude Code's built-in TodoWrite via `.claude/settings.json` so the agent can't dual-track.
> * Map tk status vocabulary to the design's vocabulary: `open` → pending, `in_progress` → active, `closed` → done. There is no failed state.
> * Tasks persist across turns until the agent closes them; the agent always closes a ticket with *some* result reported to the user (no abandonment).
> * A start-of-turn hook injects a system reminder listing every incomplete ticket's id and title so the agent can decide whether to keep, replace, or close them.
> * A stop-hook reminder calls out any tickets still open at end-of-turn but does NOT block or auto-close — the agent is free to ignore it (carryover handles the rest).
> * If a task is unfinished at the end of a turn, it appears as a fresh entry at the top of the next turn's progress block; the original entry in the prior turn is left unresolved (never backfilled).
> * Workspace server attributes tickets to turns by creation timestamp: a ticket belongs to the turn whose user-message timestamp range covers its `created_at`.
> * Render the Timeline variant from the design as production default, with no expand-by-default, expanding allowed (per-task chevron reveals raw events), and a per-turn final-message footer below the task list.
> * The expanded raw events panel reuses the existing `tool-call-block` chrome inside `.markdown-content`, interleaved with the assistant's prose between tool calls — matching how the current chat already renders an assistant turn.
> * Replace the existing `agent-activity-indicator` with a single bottom-of-chat strip whose label is derived purely client-side from the most recent tool call: "Reading X" / "Editing X" / "Writing X" / "Running X" for known tools, "Running tool…" for unknown tools, "Thinking…" when no tool is active. No task-title context.
> * Turns where the agent uses no tk tickets render exactly as today's chat (assistant text + inline tool blocks); progress UI is conditional on the presence of tickets in that turn.
> * Don't worry about the conflict between in-turn progress tickets and longer-lived "real" tk tickets for now — every ticket created within a turn is treated as a progress task.
> * For `launch-task` subagents: the parent represents the whole delegation as a single progress task in its own chat; the subagent's own tickets live only in the subagent's chat (better subagent rendering can come later).

## Overview

- Make the Minds chat readable for non-technical users by hiding the raw tool-call stream behind a per-turn "progress view" — a clean Timeline of plain-English task titles and on-completion summaries, with raw work tucked behind a per-task expand affordance.
- Use the already-vendored `tk` ticket tracker as the agent's task primitive; disable Claude Code's built-in TodoWrite so there's exactly one source of truth for task state. Per-task summaries ride on `tk add-note` followed by `tk close`.
- The agent's prompt and a pair of session hooks (start-of-turn reminder + soft stop-of-turn nag) keep the task list honest across turns. Tasks persist until the agent closes them; unfinished tasks carry over to the top of the next turn's block, with the original entry in the prior turn left frozen as-is.
- The workspace server gains a `.tickets/`-watching parser that emits new `task_*` events into the same transcript stream the chat already consumes; turn ↔ ticket attribution is done passively by `created_at` timestamp.
- The existing "Running tool…" activity indicator is replaced by a smarter, client-derived strip ("Reading X" / "Editing X" / "Running X" / "Thinking…") that reads the latest assistant tool call directly from the transcript — no agent or server changes needed.

## Expected behavior

### From the user's perspective

- Every turn where the agent does meaningful work shows a clean Timeline of plain-English task steps under the user's message, with the agent's final summary message below the timeline.
- Each task's status is visually obvious: pending (dashed circle), active (spinner, breathing), done (green check), and only those three — there is no "failed" state.
- Completed tasks carry a one-line, user-facing summary written by the agent ("Found a midnight theme in your settings file…") rather than a tool log.
- A user who wants to see what the agent actually did clicks a chevron on the task and gets the existing tool-call-block chrome inline, interleaved with the agent's prose for that step.
- Short turns where the agent doesn't open any tickets (chitchat, one-shots, "yes please") render exactly as today — no empty progress block, no forced ceremony.
- A single activity strip just above the composer always reflects what the agent is currently doing — "Editing src/themes/index.ts", "Running npm test", or "Thinking…" — with no per-task context cluttering it.
- If the agent leaves a task unfinished at end of turn, the next turn opens with that task at the top of its progress block as a fresh entry; the original turn's block keeps showing the task as it was when the turn ended.
- Existing chat history (sessions with no tk tickets) renders exactly as before — no broken legacy view.

### From the agent's perspective

- TodoWrite is unavailable; the agent uses `tk` for all in-turn task tracking.
- At the start of each turn, the agent receives a system reminder listing every still-open ticket (id + title) so it can decide whether to continue, replace, or close them.
- At the end of each turn, if any tickets the agent touched remain open, it gets a soft, ignorable reminder noting them — no blocking, no auto-close. The agent is expected to close every ticket it started with a real summary, but isn't forced to.
- Subagents launched via `launch-task` use their own `.tickets/` directory; their task list does not bleed into the parent's chat. The parent treats the whole delegation as a single ticket in its own progress.

### Edge cases

- Multiple new turns can arrive while a task is still in_progress; the unfinished task carries over only to the next turn (not duplicated through every subsequent turn — once it carries forward, the new turn becomes its "home").
- A ticket created before any user message in the conversation (e.g. by an event-processor or background activity) attaches to no turn and does not render in chat.
- A ticket the agent creates and closes within the same turn renders as a fully-resolved entry.
- If the agent closes a ticket without first calling `tk add-note`, the rendered summary falls back to the ticket title (plus a subtle indicator that no summary was written) so the UI degrades gracefully.

## Changes

### Forever-claude-template (agent side)

- **CLAUDE.md**: add a strong, prominent task-management section that:
  - declares `tk` the only allowed task tracker for in-turn progress, and TodoWrite as forbidden;
  - dictates user-facing tone for ticket titles (plain English, abstract over implementation, no jargon, no file names, no tool names);
  - dictates user-facing tone for summaries on close (one-line answer to "what did you actually do for this step?", written for a non-technical reader);
  - prescribes the lifecycle: `tk create` → `tk start` → work → `tk add-note` (summary) → `tk close`;
  - explains the carryover behavior so the agent reasons correctly about old open tickets;
  - covers the no-failed-state rule: every started ticket must terminate as closed, with a summary that honestly reports whatever the agent achieved (including partial / negative results).
- **`.claude/settings.json`**: explicitly disable the built-in TodoWrite tool so the agent can't dual-track; ensure `tk` and `ticket` are on the agent's allowed Bash invocations.
- **Session hooks** (added in `settings.json`):
  - **UserPromptSubmit / SessionStart-of-turn hook**: queries `tk` for open tickets and injects a system reminder listing them (id + title); stays silent if none are open.
  - **Stop hook**: queries `tk` for tickets still open at end of turn that the current turn touched, surfaces them as soft feedback the agent can read on next wake. Never blocks. Never closes. Never reopens.
- **Skills**:
  - Update `launch-task` to make subagents use their own `.tickets/` dir (or no `.tickets/` at all) and to instruct the parent agent to model the whole delegation as a single ticket in its own list.
  - Optionally add a tiny `progress` (or similar) skill whose body is a concise restatement of the task-management protocol, to give Claude a scannable reference when it needs one.
- **No fork of `tk`** for v1 — use the upstream behavior verbatim. If the no-summary fallback proves too lossy in practice, revisit later.

### Mngr / minds_workspace_server (rendering side)

- **Backend ticket-watching layer**: add a watcher analogous to the existing session JSONL watcher that observes the agent's `.tickets/` directory, parses the YAML frontmatter and notes from each `<id>.md`, and synthesizes a stream of new transcript events:
  - a `task_event` for each ticket state transition (created, started, closed) carrying ticket id, title, status, summary (most recent note before close), and `created_at` / `updated_at` timestamps;
  - events flow through the same API endpoints / WebSocket stream the frontend already consumes, with deterministic event_ids so deduplication works.
- **Turn attribution**: server-side, when serving events, group `task_event`s into turns by checking which user-message timestamp window each ticket's `created_at` falls within. Tickets that don't fall in any window are tagged "carryover" and attached to the *next* turn after their last update.
- **Frontend `Response.ts` model**: add `task_event` to the `TranscriptEvent` discriminated union and expose a per-agent task-state map that's kept in sync as events arrive (mirroring `eventsByAgent`).
- **Frontend `ChatPanel.ts` rendering**: split the per-turn message list into "progress turns" and "plain turns":
  - A progress turn (any turn with one or more task_events) renders the user message followed by a Timeline-variant progress block, then the agent's final-message text below the block. Raw tool calls stop being inline children and instead live behind each task's chevron.
  - A plain turn (no task_events) renders exactly as today.
- **New view module**: a `ProgressBlock` component (Mithril, matching existing patterns in `views/`) that renders the Timeline variant from the design — task list with status icons, per-task expand affordance reusing `renderToolCallBlock` and `MarkdownContent` for the expanded body.
- **Per-task event grouping**: when expanding a task, gather the assistant text + tool calls that occurred between that ticket's `started` and `closed` events (or between started and now, if active) and render them in transcript order, matching how the current `renderAssistantMessageChildren` interleaves prose and tool blocks.
- **Activity indicator rewrite**: replace the current `.agent-activity-indicator` with a derived strip whose label is computed each redraw from the latest assistant tool call without a matching tool_result — verb table covering Read / Edit / Write / Bash / etc., a generic "Running tool…" fallback, "Thinking…" when no tool is in flight.
- **Styles**: port `progress-styles.css` (Timeline section + shared base) into the Vite frontend's stylesheet. Keep the existing `tool-call-block` styles intact since the expanded view re-uses them verbatim.
- **No subagent rendering changes** for v1 beyond ensuring subagent tabs continue to render with today's chat (since subagents have no `.tickets/` exposed up to the parent).
- **Legacy / no-ticket sessions**: untouched. The new code path only activates when `task_event`s are present for a turn, so existing agents, snapshots, and event-processor traces keep rendering exactly as before.

### Out of scope for v1 (explicitly deferred)

- Distinguishing "real backlog" tk tickets from in-turn progress tickets (every ticket within a turn is treated as progress).
- Forking tk to add a first-class `--summary` flag on `close`.
- A better cross-rendering of subagent / background-agent task progress in the parent's chat.
- Auto-closing or auto-failing incomplete tasks (we explicitly chose not to).
- Per-user toggle to fall back to the technical view (the new view is the new default; the toggle in the design's Tweaks panel exists only for design exploration and is not shipped).
