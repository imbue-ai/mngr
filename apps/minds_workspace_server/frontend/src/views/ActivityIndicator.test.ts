import { describe, expect, it } from "vitest";
import type { TranscriptEvent } from "../models/Response";
import { deriveActivityLabel } from "./ActivityIndicator";

function userMsg(ts: string): TranscriptEvent {
  return { timestamp: ts, type: "user_message", event_id: `u-${ts}`, source: "test", content: "hi" };
}

function toolUse(ts: string, toolName: string, callId: string, input: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${callId}`,
    source: "test",
    text: "",
    tool_calls: [{ tool_call_id: callId, tool_name: toolName, input_preview: input }],
  };
}

function toolResult(ts: string, callId: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "tool_result",
    event_id: `r-${callId}`,
    source: "test",
    tool_call_id: callId,
    output: "result",
  };
}

function assistantText(ts: string, text: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${ts}`,
    source: "test",
    text,
    tool_calls: [],
  };
}

describe("deriveActivityLabel", () => {
  it("returns null on an empty event list", () => {
    expect(deriveActivityLabel([])).toBe(null);
  });

  it("returns 'Thinking…' when the latest event is a user_message", () => {
    expect(deriveActivityLabel([userMsg("2026-04-28T01:00:00Z")])).toBe("Thinking…");
  });

  it("returns null when the latest event is an assistant_message with no pending tools", () => {
    const events = [userMsg("2026-04-28T01:00:00Z"), assistantText("2026-04-28T01:00:01Z", "All done.")];
    expect(deriveActivityLabel(events)).toBe(null);
  });

  it("labels Read with the file basename", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Read", "tc1", '{"file_path":"src/themes/midnight.ts"}'),
    ];
    expect(deriveActivityLabel(events)).toBe("Reading midnight.ts");
  });

  it("labels Edit / MultiEdit with file basename", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Edit", "tc1", '{"file_path":"server/routes/reports.ts"}'),
    ];
    expect(deriveActivityLabel(events)).toBe("Editing reports.ts");
  });

  it("labels Bash with the (truncated) command", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Bash", "tc1", '{"command":"npm test"}'),
    ];
    expect(deriveActivityLabel(events)).toBe("Running npm test");
  });

  it("labels Grep with the pattern in quotes", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Grep", "tc1", '{"pattern":"registerTheme"}'),
    ];
    expect(deriveActivityLabel(events)).toBe('Searching "registerTheme"');
  });

  it("falls back to 'Running tool…' for unknown tools", () => {
    const events = [userMsg("2026-04-28T01:00:00Z"), toolUse("2026-04-28T01:00:01Z", "SomeNewTool", "tc1", "{}")];
    expect(deriveActivityLabel(events)).toBe("Running tool…");
  });

  it("returns 'Delegating to sub-agent…' for Agent / Task tools", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Agent", "tc1", '{"description":"do it"}'),
    ];
    expect(deriveActivityLabel(events)).toBe("Delegating to sub-agent…");
  });

  it("ignores tool calls that already have a matching tool_result", () => {
    // Latest assistant message had a tool call, but the result has already
    // come back. The agent is now idle (no more pending tools); label = null.
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Read", "tc1", '{"file_path":"x.ts"}'),
      toolResult("2026-04-28T01:00:02Z", "tc1"),
      assistantText("2026-04-28T01:00:03Z", "OK done."),
    ];
    expect(deriveActivityLabel(events)).toBe(null);
  });

  it("returns 'Thinking…' between a tool result and the next assistant message", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Read", "tc1", '{"file_path":"x.ts"}'),
      toolResult("2026-04-28T01:00:02Z", "tc1"),
    ];
    expect(deriveActivityLabel(events)).toBe("Thinking…");
  });
});
