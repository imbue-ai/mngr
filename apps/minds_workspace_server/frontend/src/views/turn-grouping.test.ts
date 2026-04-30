import { describe, expect, it } from "vitest";
import type { TranscriptEvent } from "../models/Response";
import { buildTaskRecords, buildTurns, eventsInTaskWindow } from "./turn-grouping";

function userMsg(ts: string, content: string, eventId: string = `u-${ts}`): TranscriptEvent {
  return {
    timestamp: ts,
    type: "user_message",
    event_id: eventId,
    source: "test",
    content,
  };
}

function assistantMsg(ts: string, text: string, eventId: string = `a-${ts}`): TranscriptEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: eventId,
    source: "test",
    text,
    tool_calls: [],
  };
}

function toolUse(ts: string, toolName: string, callId: string, input: string = "{}"): TranscriptEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${callId}`,
    source: "test",
    text: "",
    tool_calls: [{ tool_call_id: callId, tool_name: toolName, input_preview: input }],
  };
}

function taskEvent(
  ticketId: string,
  status: "open" | "in_progress" | "closed",
  ts: string,
  extras: Partial<TranscriptEvent> = {},
): TranscriptEvent {
  return {
    timestamp: ts,
    type: "task_event",
    event_id: `${ticketId}-${status}`,
    source: "tk",
    ticket_id: ticketId,
    title: extras.title ?? "Some task",
    status,
    created_at: extras.created_at ?? ts,
    summary: extras.summary ?? null,
    summary_at: extras.summary_at ?? null,
  };
}

describe("buildTaskRecords", () => {
  it("folds three events for one ticket into a single record with all timestamps", () => {
    const events = [
      taskEvent("t1", "open", "2026-04-28T01:00:00Z", { created_at: "2026-04-28T01:00:00Z" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:01:00Z"),
      taskEvent("t1", "closed", "2026-04-28T01:02:00Z", {
        summary: "Did the thing.",
        summary_at: "2026-04-28T01:01:50Z",
      }),
    ];
    const records = buildTaskRecords(events);
    const record = records.get("t1");
    expect(record).toBeDefined();
    expect(record?.created_at).toBe("2026-04-28T01:00:00Z");
    expect(record?.started_at).toBe("2026-04-28T01:01:00Z");
    expect(record?.closed_at).toBe("2026-04-28T01:02:00Z");
    expect(record?.summary).toBe("Did the thing.");
    expect(record?.final_status).toBe("closed");
  });

  it("ignores non-task events", () => {
    const events = [userMsg("2026-04-28T01:00:00Z", "hi"), assistantMsg("2026-04-28T01:00:01Z", "hello")];
    const records = buildTaskRecords(events);
    expect(records.size).toBe(0);
  });

  it("falls back to the event timestamp when created_at is an empty string", () => {
    // Malformed ticket missing the `created:` frontmatter line: the watcher
    // emits an empty created_at. Without the `||` fallback, the resulting
    // TaskRecord has created_at="" and the task gets silently dropped from
    // every turn in buildTurns (empty string fails the window check).
    const events = [taskEvent("t1", "open", "2026-04-28T01:00:00Z", { created_at: "" })];
    const records = buildTaskRecords(events);
    expect(records.get("t1")?.created_at).toBe("2026-04-28T01:00:00Z");
  });
});

describe("buildTurns", () => {
  it("returns no turns when there are no user messages", () => {
    const events = [assistantMsg("2026-04-28T01:00:00Z", "stray reply")];
    expect(buildTurns(events)).toEqual([]);
  });

  it("groups assistant messages and tool_results into the right turn", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "first"),
      assistantMsg("2026-04-28T01:00:30Z", "first reply"),
      userMsg("2026-04-28T01:01:00Z", "second"),
      assistantMsg("2026-04-28T01:01:30Z", "second reply"),
    ];
    const turns = buildTurns(events);
    expect(turns).toHaveLength(2);
    expect(turns[0].body_events.map((e) => e.event_id)).toEqual(["a-2026-04-28T01:00:30Z"]);
    expect(turns[1].body_events.map((e) => e.event_id)).toEqual(["a-2026-04-28T01:01:30Z"]);
  });

  it("plain turn (no task_events) has empty tasks array", () => {
    const events = [userMsg("2026-04-28T01:00:00Z", "hi"), assistantMsg("2026-04-28T01:00:01Z", "hello")];
    const turns = buildTurns(events);
    expect(turns[0].tasks).toEqual([]);
  });

  it("attributes a task to the turn it was created in", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "fix the thing"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", {
        created_at: "2026-04-28T01:00:10Z",
        title: "Look at the thing",
      }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      taskEvent("t1", "closed", "2026-04-28T01:00:50Z", {
        summary: "Found the thing",
        summary_at: "2026-04-28T01:00:45Z",
      }),
      assistantMsg("2026-04-28T01:00:55Z", "Done."),
    ];
    const turns = buildTurns(events);
    expect(turns).toHaveLength(1);
    expect(turns[0].tasks).toHaveLength(1);
    expect(turns[0].tasks[0]).toMatchObject({
      ticket_id: "t1",
      title: "Look at the thing",
      status: "done",
      summary: "Found the thing",
      is_carryover: false,
    });
  });

  it("carries over an unfinished task to the next turn as a fresh entry, leaving the old one frozen", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "fix one"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Step 1" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      // user message arrives while t1 is still in_progress
      userMsg("2026-04-28T01:01:00Z", "any update?"),
      taskEvent("t1", "closed", "2026-04-28T01:01:30Z", {
        summary: "Wrapped up step 1",
        summary_at: "2026-04-28T01:01:25Z",
      }),
    ];
    const turns = buildTurns(events);
    expect(turns).toHaveLength(2);
    // First turn: ticket appears as "active" (frozen at end of turn 1).
    expect(turns[0].tasks).toHaveLength(1);
    expect(turns[0].tasks[0]).toMatchObject({
      ticket_id: "t1",
      status: "active",
      summary: null,
      is_carryover: false,
    });
    // Second turn: same ticket as a CARRYOVER, now "done".
    expect(turns[1].tasks).toHaveLength(1);
    expect(turns[1].tasks[0]).toMatchObject({
      ticket_id: "t1",
      status: "done",
      summary: "Wrapped up step 1",
      is_carryover: true,
    });
  });

  it("does not carry over a task that was closed before the next turn", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "fix one"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Step 1" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      taskEvent("t1", "closed", "2026-04-28T01:00:50Z", {
        summary: "Done",
        summary_at: "2026-04-28T01:00:45Z",
      }),
      userMsg("2026-04-28T01:01:00Z", "another thing"),
    ];
    const turns = buildTurns(events);
    expect(turns[0].tasks).toHaveLength(1);
    expect(turns[1].tasks).toHaveLength(0);
  });

  it("orders carryover tasks above own tasks in a turn", () => {
    const events: TranscriptEvent[] = [
      userMsg("2026-04-28T01:00:00Z", "first"),
      taskEvent("t1", "open", "2026-04-28T01:00:10Z", { created_at: "2026-04-28T01:00:10Z", title: "Carryover" }),
      taskEvent("t1", "in_progress", "2026-04-28T01:00:20Z"),
      userMsg("2026-04-28T01:01:00Z", "second"),
      taskEvent("t2", "open", "2026-04-28T01:01:10Z", { created_at: "2026-04-28T01:01:10Z", title: "Fresh" }),
    ];
    const turns = buildTurns(events);
    expect(turns[1].tasks.map((t) => t.title)).toEqual(["Carryover", "Fresh"]);
    expect(turns[1].tasks[0].is_carryover).toBe(true);
    expect(turns[1].tasks[1].is_carryover).toBe(false);
  });
});

describe("eventsInTaskWindow", () => {
  it("returns only events between a task's started_at and closed_at", () => {
    const tasksByTime = {
      ticket_id: "t1",
      title: "Step 1",
      status: "done" as const,
      summary: "Did it",
      is_carryover: false,
      active_window_start: "2026-04-28T01:00:20Z",
      active_window_end: "2026-04-28T01:00:50Z",
    };
    const body = [
      assistantMsg("2026-04-28T01:00:15Z", "before start"),
      toolUse("2026-04-28T01:00:25Z", "Read", "tc1"),
      toolUse("2026-04-28T01:00:45Z", "Edit", "tc2"),
      assistantMsg("2026-04-28T01:00:55Z", "after end"),
    ];
    const result = eventsInTaskWindow(tasksByTime, body);
    expect(result.map((e) => e.event_id)).toEqual(["a-tc1", "a-tc2"]);
  });

  it("returns events through end of turn when active_window_end is null", () => {
    const task = {
      ticket_id: "t1",
      title: "Active",
      status: "active" as const,
      summary: null,
      is_carryover: false,
      active_window_start: "2026-04-28T01:00:20Z",
      active_window_end: null,
    };
    const body = [toolUse("2026-04-28T01:00:25Z", "Read", "tc1"), toolUse("2026-04-28T01:00:45Z", "Edit", "tc2")];
    expect(eventsInTaskWindow(task, body)).toHaveLength(2);
  });
});
