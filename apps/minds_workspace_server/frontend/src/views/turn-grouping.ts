/**
 * Group transcript events into turns and attribute tasks.
 *
 * A "turn" starts at a user_message and ends at the next user_message
 * (or now). Within a turn we collect:
 *   - The user_message itself
 *   - All assistant_message + tool_result events whose timestamp falls
 *     in the turn's window
 *   - Tasks attributed to this turn (created during this turn's window
 *     OR carried over from a prior turn while still unfinished)
 *
 * Task attribution is two-step:
 *   1. Fold task_event events by ticket_id into TaskInfo (the latest
 *      status wins; closed > in_progress > open). Track each transition
 *      timestamp (created_at / started_at / closed_at).
 *   2. Each task is "owned" by the turn whose window contains its
 *      created_at. Tasks whose status was not closed at the END of
 *      their owning turn appear ALSO in the next turn as a carryover
 *      entry. The owning turn renders the task in its state-as-of-turn-end
 *      (frozen); the carryover-receiving turn renders the same task in
 *      its state-as-of-that-turn-end (live).
 */

import type { TranscriptEvent, TaskEventStatus } from "../models/Response";

export type TaskUiStatus = "pending" | "active" | "done";

const STATUS_RANK: Record<TaskEventStatus, number> = {
  open: 0,
  in_progress: 1,
  closed: 2,
};

const TASK_UI_STATUS: Record<TaskEventStatus, TaskUiStatus> = {
  open: "pending",
  in_progress: "active",
  closed: "done",
};

/** Folded view of a tk ticket's full event history. */
export interface TaskRecord {
  ticket_id: string;
  title: string;
  created_at: string;
  started_at: string | null;
  closed_at: string | null;
  summary: string | null;
  /** Final status seen so far across all events. */
  final_status: TaskEventStatus;
}

/** A task as it should be rendered inside a specific turn. */
export interface TaskInTurn {
  ticket_id: string;
  title: string;
  /** UI-mapped status of the task as of the END of THIS turn. */
  status: TaskUiStatus;
  /** Summary text only when status === "done" (rendered under the task). */
  summary: string | null;
  /** True if this task was first created in a prior turn; rendered with a
   *  faint "carryover" marker so the user can tell it's continuing work. */
  is_carryover: boolean;
  /** Inclusive lower bound of the active window for tool-call attribution. */
  active_window_start: string | null;
  /** Inclusive upper bound of the active window. null = still active at
   *  end of turn. */
  active_window_end: string | null;
}

export interface Turn {
  user_event: TranscriptEvent;
  /** Inclusive: timestamp of the user_message itself. */
  start_ts: string;
  /** Exclusive: timestamp of the next user_message, or "" (treated as
   *  +infinity) if this is the latest turn. */
  end_ts: string;
  /** Assistant messages and tool_results inside the window, in order. */
  body_events: TranscriptEvent[];
  /** Tasks rendered inside this turn's progress block. Empty list means
   *  this is a "plain" turn (no progress UI). */
  tasks: TaskInTurn[];
}

/** Fold task_event events into per-ticket TaskRecord. Latest status
 *  (by STATUS_RANK) wins; transitions track each timestamp. */
export function buildTaskRecords(events: TranscriptEvent[]): Map<string, TaskRecord> {
  const records = new Map<string, TaskRecord>();
  for (const e of events) {
    if (e.type !== "task_event" || !e.ticket_id || !e.status) continue;
    const existing = records.get(e.ticket_id);
    if (existing === undefined) {
      records.set(e.ticket_id, {
        ticket_id: e.ticket_id,
        title: e.title ?? e.ticket_id,
        created_at: e.created_at ?? e.timestamp,
        started_at: e.status === "in_progress" || e.status === "closed" ? e.timestamp : null,
        closed_at: e.status === "closed" ? e.timestamp : null,
        summary: e.status === "closed" ? (e.summary ?? null) : null,
        final_status: e.status,
      });
      continue;
    }
    // title and created_at are written by the watcher with the SAME value
    // on every event for a given ticket (frontmatter `created` field, H1
    // line in the body). We don't update them after the first event --
    // doing so risks reordering tickets if a later event happens to carry
    // a different fallback value (e.g. ticket_id when no H1 is present).

    if (e.status === "in_progress" && existing.started_at === null) {
      existing.started_at = e.timestamp;
    }
    if (e.status === "closed") {
      existing.closed_at = e.timestamp;
      if (e.summary !== undefined && e.summary !== null) {
        existing.summary = e.summary;
      }
    }
    if (STATUS_RANK[e.status] >= STATUS_RANK[existing.final_status]) {
      existing.final_status = e.status;
    }
  }
  return records;
}

/** Group events into turns and attribute tasks per turn. */
export function buildTurns(events: TranscriptEvent[]): Turn[] {
  // Identify turn boundaries by user_message timestamps, in order.
  const userMessages: TranscriptEvent[] = events
    .filter((e) => e.type === "user_message")
    .slice()
    .sort((a, b) => a.timestamp.localeCompare(b.timestamp));

  if (userMessages.length === 0) {
    return [];
  }

  const taskRecords = buildTaskRecords(events);

  const turns: Turn[] = [];
  for (let i = 0; i < userMessages.length; i++) {
    const userEvent = userMessages[i];
    const start_ts = userEvent.timestamp;
    const end_ts = i + 1 < userMessages.length ? userMessages[i + 1].timestamp : "";

    const body_events: TranscriptEvent[] = [];
    for (const e of events) {
      if (e === userEvent) continue;
      if (e.type !== "assistant_message" && e.type !== "tool_result") continue;
      if (e.timestamp < start_ts) continue;
      if (end_ts !== "" && e.timestamp >= end_ts) continue;
      body_events.push(e);
    }
    body_events.sort((a, b) => a.timestamp.localeCompare(b.timestamp));

    turns.push({
      user_event: userEvent,
      start_ts,
      end_ts,
      body_events,
      tasks: [], // filled below
    });
  }

  // Attribute each task record to its owning turn (created during) and
  // any carryover turn (next turn after the owning one if not closed).
  for (const record of taskRecords.values()) {
    for (let i = 0; i < turns.length; i++) {
      const turn = turns[i];
      const inWindow = record.created_at >= turn.start_ts && (turn.end_ts === "" || record.created_at < turn.end_ts);
      if (!inWindow) continue;
      // Owning turn entry.
      turn.tasks.push(makeTaskInTurn(record, turn, /* is_carryover */ false));

      // Carryover entry on the next turn if this task wasn't closed
      // before the next turn started.
      if (i + 1 < turns.length) {
        const next = turns[i + 1];
        const closedBeforeNext = record.closed_at !== null && record.closed_at < next.start_ts;
        if (!closedBeforeNext) {
          next.tasks.unshift(makeTaskInTurn(record, next, /* is_carryover */ true));
        }
      }
      break;
    }
  }

  // Within a turn, sort own (non-carryover) tasks by created_at (carryovers
  // already at the top from unshift).
  for (const turn of turns) {
    const carry = turn.tasks.filter((t) => t.is_carryover);
    const own = turn.tasks
      .filter((t) => !t.is_carryover)
      .sort((a, b) => (a.active_window_start ?? "").localeCompare(b.active_window_start ?? ""));
    turn.tasks = [...carry, ...own];
  }

  return turns;
}

function makeTaskInTurn(record: TaskRecord, turn: Turn, is_carryover: boolean): TaskInTurn {
  // Status as of THIS turn's end. Determined by walking the record's
  // transitions: if closed_at is before turn end, status is done; else
  // if started_at is before turn end, status is active; else pending.
  const turnEnd = turn.end_ts;
  let status: TaskUiStatus = "pending";
  if (record.started_at !== null && (turnEnd === "" || record.started_at < turnEnd)) {
    status = "active";
  }
  if (record.closed_at !== null && (turnEnd === "" || record.closed_at < turnEnd)) {
    status = "done";
  }
  return {
    ticket_id: record.ticket_id,
    title: record.title,
    status,
    // Summary only renders for done tasks. Carryover entries show summary
    // too if they got closed during this turn.
    summary: status === "done" ? record.summary : null,
    is_carryover,
    active_window_start: record.started_at ?? record.created_at,
    active_window_end: status === "done" ? record.closed_at : null,
  };
}

/** Pick out the body_events that fall inside a task's active window.
 *  Used to populate the expanded panel for a given task. */
export function eventsInTaskWindow(task: TaskInTurn, body_events: TranscriptEvent[]): TranscriptEvent[] {
  const start = task.active_window_start ?? "";
  const end = task.active_window_end ?? "";
  if (start === "") return [];
  return body_events.filter((e) => {
    if (e.timestamp < start) return false;
    if (end !== "" && e.timestamp > end) return false;
    return true;
  });
}

/** Status mapping helper exposed for views. */
export function uiStatusFromTk(status: TaskEventStatus): TaskUiStatus {
  return TASK_UI_STATUS[status];
}
