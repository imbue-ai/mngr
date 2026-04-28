/**
 * Smart activity strip that sits just above the message input.
 *
 * Replaces the generic "Running tool…" indicator with a label derived
 * from the most recent assistant tool call without a matching tool_result.
 * Vocabulary:
 *   - "Reading <basename>"  for Read
 *   - "Editing <basename>"  for Edit / MultiEdit
 *   - "Writing <basename>"  for Write
 *   - "Running <command>"   for Bash (truncated)
 *   - "Searching <pattern>" for Grep / Glob
 *   - "Delegating to sub-agent"  for Agent / Task
 *   - "Running tool…"       for any other unmapped tool
 *   - "Thinking…"           when the latest event is a user_message or a
 *                           tool_result with no follow-up assistant_message
 *   - hidden                when the latest assistant_message has no
 *                           pending tool calls (agent is idle / done)
 *
 * Pure derivation from the transcript -- no agent or server state needed.
 */

import m from "mithril";
import type { ToolCall, TranscriptEvent } from "../models/Response";

const VERB_BY_TOOL: Record<string, string> = {
  Read: "Reading",
  Edit: "Editing",
  MultiEdit: "Editing",
  Write: "Writing",
  Bash: "Running",
  Grep: "Searching",
  Glob: "Searching",
  Task: "Delegating",
  Agent: "Delegating",
};

const MAX_TARGET_LEN = 60;

function basename(p: string): string {
  const idx = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  return idx >= 0 ? p.slice(idx + 1) : p;
}

function shorten(s: string, max: number): string {
  s = s.replace(/\s+/g, " ").trim();
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

function targetForToolCall(tc: ToolCall): string | null {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(tc.input_preview) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== "object") return null;

  const filePath = typeof parsed.file_path === "string" ? parsed.file_path : null;
  if (filePath !== null) return basename(filePath);
  const path = typeof parsed.path === "string" ? parsed.path : null;
  if (path !== null) return basename(path);
  const command = typeof parsed.command === "string" ? parsed.command : null;
  if (command !== null) return shorten(command, MAX_TARGET_LEN);
  const pattern = typeof parsed.pattern === "string" ? parsed.pattern : null;
  if (pattern !== null) return `"${shorten(pattern, MAX_TARGET_LEN)}"`;
  const description = typeof parsed.description === "string" ? parsed.description : null;
  if (description !== null) return shorten(description, MAX_TARGET_LEN);
  return null;
}

function labelForToolCall(tc: ToolCall): string {
  if (tc.tool_name === "Agent" || tc.tool_name === "Task") {
    return "Delegating to sub-agent…";
  }
  const verb = VERB_BY_TOOL[tc.tool_name];
  const target = targetForToolCall(tc);
  if (verb !== undefined && target !== null) return `${verb} ${target}`;
  if (verb !== undefined) return `${verb}…`;
  return "Running tool…";
}

/**
 * Find the most recent assistant tool call whose tool_call_id has no
 * matching tool_result event. Returns null if none.
 */
function pendingToolCall(events: TranscriptEvent[]): ToolCall | null {
  const resolved = new Set<string>();
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "tool_result" && e.tool_call_id) {
      resolved.add(e.tool_call_id);
      continue;
    }
    if (e.type === "assistant_message" && e.tool_calls && e.tool_calls.length > 0) {
      for (let j = e.tool_calls.length - 1; j >= 0; j--) {
        const tc = e.tool_calls[j];
        if (!resolved.has(tc.tool_call_id)) {
          return tc;
        }
      }
    }
  }
  return null;
}

/** The activity label given the current event list. null = hidden. */
export function deriveActivityLabel(events: TranscriptEvent[]): string | null {
  if (events.length === 0) return null;

  const pending = pendingToolCall(events);
  if (pending !== null) return labelForToolCall(pending);

  // No pending tools. Decide between "thinking" and idle by looking at
  // the latest event.
  const last = events[events.length - 1];
  if (last.type === "assistant_message") {
    // Most recent message from the agent has no pending tools -- idle.
    return null;
  }
  // last event is user_message or tool_result with no follow-up
  // assistant_message -> agent is processing.
  return "Thinking…";
}

interface ActivityIndicatorAttrs {
  events: TranscriptEvent[];
}

export function ActivityIndicator(): m.Component<ActivityIndicatorAttrs> {
  return {
    view(vnode) {
      const label = deriveActivityLabel(vnode.attrs.events);
      if (label === null) return null;
      return m("div.agent-activity-indicator", { role: "status", "aria-live": "polite" }, [
        m("span.agent-activity-indicator__dot"),
        m("span.agent-activity-indicator__label", label),
      ]);
    },
  };
}
