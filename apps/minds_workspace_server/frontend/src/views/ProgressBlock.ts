/**
 * Per-turn progress block: Timeline-variant rendering of the agent's
 * tk-tracked task list for a single user turn. Each task is a node on
 * a vertical thread, with status icon + title + (when done) summary.
 *
 * Each task can be expanded via its chevron to reveal the raw assistant
 * text + tool_call_blocks that occurred during the task's active window.
 * The expanded panel reuses the existing `tool-call-block` chrome so the
 * raw view matches the rest of the chat.
 */

import m from "mithril";
import { MarkdownContent } from "../markdown";
import type { TranscriptEvent } from "../models/Response";
import { renderToolCallBlock, renderSubagentCard } from "./message-renderers";
import type { TaskInTurn, TaskUiStatus } from "./turn-grouping";
import { eventsInTaskWindow } from "./turn-grouping";

interface ProgressBlockAttrs {
  tasks: TaskInTurn[];
  body_events: TranscriptEvent[];
  /** Final assistant message text for this turn (rendered below the
   *  Timeline). */
  final_message: string | null;
  agentId: string;
}

function statusIcon(status: TaskUiStatus): m.Vnode {
  if (status === "done") {
    return m(
      "svg.pv-icon.pv-icon--done",
      { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none" },
      m.trust(
        '<circle cx="8" cy="8" r="7" fill="currentColor"/><path d="M4.5 8L7 10.5L11.5 6" stroke="white" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
      ),
    );
  }
  if (status === "active") {
    return m("span.pv-icon.pv-icon--active", m("span.pv-spinner"));
  }
  return m(
    "svg.pv-icon.pv-icon--pending",
    { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none" },
    m.trust('<circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1" stroke-dasharray="2 2"/>'),
  );
}

function renderExpandedTaskBody(events: TranscriptEvent[], agentId: string): m.Vnode {
  if (events.length === 0) {
    return m("div.pv-expanded-empty", "No raw work captured for this step.");
  }

  // Collect tool_results so we can match them back to tool_use entries.
  const toolResults = new Map<string, TranscriptEvent>();
  for (const e of events) {
    if (e.type === "tool_result" && e.tool_call_id) {
      toolResults.set(e.tool_call_id, e);
    }
  }

  const children: m.Children[] = [];
  for (const e of events) {
    if (e.type !== "assistant_message") continue;
    if (e.text) {
      children.push(m(MarkdownContent, { content: e.text }));
    }
    for (const tc of e.tool_calls ?? []) {
      if (tc.tool_name === "Agent" && tc.subagent_metadata) {
        children.push(renderSubagentCard(tc, agentId));
      } else {
        children.push(renderToolCallBlock(tc, toolResults.get(tc.tool_call_id) ?? null));
      }
    }
  }

  return m("div.pv-expanded.markdown-content", children);
}

export function ProgressBlock(): m.Component<ProgressBlockAttrs> {
  // Per-task expand state, keyed by ticket_id. Reset across instances
  // so each turn's progress block has its own state.
  const expanded = new Set<string>();

  function toggle(ticket_id: string): void {
    if (expanded.has(ticket_id)) {
      expanded.delete(ticket_id);
    } else {
      expanded.add(ticket_id);
    }
  }

  return {
    view(vnode) {
      const { tasks, body_events, final_message, agentId } = vnode.attrs;
      if (tasks.length === 0) {
        // Defensive: callers should not mount ProgressBlock when there
        // are no tasks. Fall back to no-op.
        return null;
      }

      return m("div.progress-block", [
        m("div.pv.pv--timeline", [
          m("div.pv-timeline-thread", { "aria-hidden": "true" }),
          tasks.map((task, idx) => {
            const isLast = idx === tasks.length - 1;
            const taskEvents = eventsInTaskWindow(task, body_events);
            // A task is "expandable" only if there are raw events to show.
            // Expanding is allowed for done/active tasks; pending tasks
            // typically have no events anyway.
            const canExpand = taskEvents.length > 0;
            const isExpanded = expanded.has(task.ticket_id);
            const nodeClasses = ["pv-tl-node", `pv-tl-node--${task.status}`, isLast ? "pv-tl-node--last" : ""]
              .filter(Boolean)
              .join(" ");

            return m("div", { class: nodeClasses, key: task.ticket_id + (task.is_carryover ? "-carry" : "") }, [
              m("div.pv-tl-bullet", statusIcon(task.status)),
              m("div.pv-tl-body", [
                m(
                  "button",
                  {
                    type: "button",
                    class: "pv-tl-title",
                    disabled: !canExpand,
                    onclick: canExpand ? () => toggle(task.ticket_id) : undefined,
                  },
                  [
                    task.title,
                    task.is_carryover
                      ? m("span.pv-carryover-tag", { title: "Continued from a previous turn" }, "continued")
                      : null,
                    canExpand
                      ? m("span", { class: `pv-chev ${isExpanded ? "pv-chev--open" : ""}` }, m.trust("&rsaquo;"))
                      : null,
                  ],
                ),
                task.status === "done" && task.summary ? m("div.pv-tl-summary", task.summary) : null,
                isExpanded ? m("div.pv-tl-expanded", renderExpandedTaskBody(taskEvents, agentId)) : null,
              ]),
            ]);
          }),
        ]),
        final_message ? m("div.pv-final", m(MarkdownContent, { content: final_message })) : null,
      ]);
    },
  };
}
