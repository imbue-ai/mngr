/**
 * Shared rendering functions for transcript events.
 * Used by both MessageList and SubagentView.
 */

import m from "mithril";
import { MarkdownContent } from "../markdown";
import type { TranscriptEvent, ToolCall } from "../models/Response";

export function renderSubagentCard(toolCall: ToolCall, agentId: string): m.Vnode {
  const metadata = toolCall.subagent_metadata;
  if (!metadata) {
    return renderToolCallBlock(toolCall, null);
  }

  const description = metadata.description || "Sub-agent";
  const agentType = metadata.agent_type || "";
  const subagentUrl = `#!/agents/${encodeURIComponent(agentId)}/subagents/${encodeURIComponent(metadata.session_id)}`;

  return m("div", { class: "subagent-card" }, [
    m("div", { class: "subagent-card-header" }, [
      m("span", { class: "subagent-card-description" }, description),
      agentType ? m("span", { class: "subagent-card-type-badge" }, agentType) : null,
    ]),
    m(
      "a",
      {
        class: "subagent-card-link",
        href: subagentUrl,
        target: "_blank",
        rel: "noopener",
        onclick(e: Event) {
          e.stopPropagation();
        },
      },
      "View conversation \u2197",
    ),
  ]);
}

export function renderToolCallBlock(toolCall: ToolCall, toolResult: TranscriptEvent | null): m.Vnode {
  const headerText = `Tool: ${toolCall.tool_name}`;
  const inputText = toolCall.input_preview || "";
  const outputText = toolResult?.output || "";
  const isError = toolResult?.is_error === true;

  return m("div", { class: "tool-call-block" }, [
    m(
      "div",
      {
        class: "tool-call-header",
        onclick(e: Event) {
          const block = (e.currentTarget as HTMLElement).parentElement;
          if (block) {
            block.classList.toggle("tool-call-block--expanded");
          }
        },
      },
      [m("span", { class: "tool-call-chevron" }, "\u25B8"), m("span", headerText)],
    ),
    m("div", { class: "tool-call-details" }, [
      inputText ? m("div", { class: "tool-call-input" }, [m("pre", m("code", inputText))]) : null,
      outputText
        ? m("div", { class: isError ? "tool-call-output tool-call-output--error" : "tool-call-output" }, [
            m("pre", m("code", outputText)),
          ])
        : null,
    ]),
  ]);
}

/**
 * Render the children (text + tool calls) of an assistant message.
 * Used by both the stable (memoized) and simple assistant message renderers.
 */
export function renderAssistantMessageChildren(
  event: TranscriptEvent,
  toolResults: Map<string, TranscriptEvent>,
  agentId: string,
): m.Children[] {
  const textContent = event.text || "";
  const toolCalls = event.tool_calls || [];

  const children: m.Children[] = [];
  if (textContent) {
    children.push(m(MarkdownContent, { content: textContent }));
  }
  for (const toolCall of toolCalls) {
    if (toolCall.tool_name === "Agent" && toolCall.subagent_metadata) {
      children.push(renderSubagentCard(toolCall, agentId));
    } else {
      const result = toolResults.get(toolCall.tool_call_id) ?? null;
      children.push(renderToolCallBlock(toolCall, result));
    }
  }
  return children;
}
