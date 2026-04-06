import m from "mithril";
import { isSlotClaimed } from "../slots";
import {
  fetchEvents,
  fetchBackfillEvents,
  getEventsForAgent,
  isConversationNotFound,
  isBackfillComplete,
  type TranscriptEvent,
  type ToolCall,
} from "../models/Response";
import { connectToStream, disconnectFromStream } from "../models/StreamingMessage";
import { getAgents } from "../models/Conversation";
import { MarkdownContent } from "../markdown";
import { EmptySlot } from "./EmptySlot";
import { MessageInput } from "./MessageInput";

const SCROLL_BOTTOM_THRESHOLD_PX = 40;

function isNearBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight < SCROLL_BOTTOM_THRESHOLD_PX;
}

function scrollToBottom(element: HTMLElement): void {
  element.scrollTop = element.scrollHeight;
}

function getAgentName(agentId: string | null): string {
  if (!agentId) {
    return "";
  }
  const agents = getAgents();
  const agent = agents.find((a) => a.id === agentId);
  return agent?.name || "Unknown agent";
}

function getAgentState(agentId: string | null): string {
  if (!agentId) {
    return "";
  }
  const agents = getAgents();
  const agent = agents.find((a) => a.id === agentId);
  return agent?.state || "";
}

function renderUserMessage(event: TranscriptEvent): m.Vnode {
  return m("div", { class: "message message-user", key: event.event_id }, [
    m("div", { class: "message-user-bubble" }, [
      m("div", { class: "message-content whitespace-pre-wrap" }, event.content || ""),
    ]),
  ]);
}

function renderToolCallBlock(toolCall: ToolCall, toolResult: TranscriptEvent | null): m.Vnode {
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

function renderAssistantMessage(
  event: TranscriptEvent,
  toolResults: Map<string, TranscriptEvent>,
): m.Vnode {
  const textContent = event.text || "";
  const toolCalls = event.tool_calls || [];

  const children: m.Children[] = [];

  if (textContent) {
    children.push(m(MarkdownContent, { content: textContent }));
  }

  for (const toolCall of toolCalls) {
    const result = toolResults.get(toolCall.tool_call_id) ?? null;
    children.push(renderToolCallBlock(toolCall, result));
  }

  return m(
    "div",
    {
      id: event.event_id,
      class: "message message-assistant",
      key: event.event_id,
    },
    children,
  );
}

export function MessageList(): m.Component<{ agentId: string | null }> {
  let loading = false;
  let loadingError: string | null = null;
  let currentAgentId: string | null = null;
  let userScrolledUp = false;
  let previousScrollTop = 0;
  let backfillStarted = false;

  async function loadAgent(agentId: string): Promise<void> {
    loading = true;
    loadingError = null;

    try {
      await fetchEvents(agentId);
      if (agentId === currentAgentId) {
        loading = false;
        loadingError = null;
      }
    } catch (error) {
      if (agentId === currentAgentId) {
        loading = false;
        loadingError = (error as Error).message ?? String(error);
      }
    }
  }

  function manageStreamConnection(agentId: string | null): void {
    if (agentId !== null) {
      if (!isConversationNotFound(agentId)) {
        connectToStream(agentId);
      } else {
        disconnectFromStream();
      }
    } else if (currentAgentId !== null) {
      disconnectFromStream();
    }
  }

  function ensureAgentLoaded(agentId: string): void {
    if (agentId === currentAgentId) {
      return;
    }

    currentAgentId = agentId;
    previousScrollTop = 0;
    userScrolledUp = false;
    backfillStarted = false;
    loadAgent(agentId);
  }

  async function runBackfillLoop(agentId: string): Promise<void> {
    while (!isBackfillComplete(agentId) && agentId === currentAgentId) {
      await fetchBackfillEvents(agentId);
      m.redraw();
    }
  }

  function startBackfill(agentId: string): void {
    if (backfillStarted || isBackfillComplete(agentId)) {
      return;
    }
    backfillStarted = true;
    runBackfillLoop(agentId);
  }

  function applyScrollPosition(element: HTMLElement): void {
    if (!userScrolledUp) {
      scrollToBottom(element);
      previousScrollTop = element.scrollTop;
    }
  }

  function handleScrollEvent(event: Event): void {
    const element = event.target as HTMLElement;
    const currentScrollTop = element.scrollTop;
    const didScrollUp = currentScrollTop < previousScrollTop;

    previousScrollTop = currentScrollTop;

    if (didScrollUp) {
      userScrolledUp = true;
      return;
    }

    if (isNearBottom(element)) {
      userScrolledUp = false;
    }
  }

  function renderMainContent(agentId: string | null): m.Vnode {
    if (!agentId) {
      return m(
        "div",
        { class: "message-list-empty flex items-center justify-center h-full" },
        m("p", { class: "text-text-secondary" }, "Select an agent to view its conversation."),
      );
    }

    ensureAgentLoaded(agentId);

    if (isConversationNotFound(agentId)) {
      return m("div", { class: "message-list-not-found flex flex-col items-center justify-center h-full gap-2" }, [
        m("p", { class: "text-2xl font-semibold text-text-primary" }, "404"),
        m("p", { class: "text-text-secondary" }, "Agent not found."),
      ]);
    }

    if (loading) {
      return m(
        "div",
        { class: "message-list-loading flex items-center justify-center h-full" },
        m("p", { class: "text-text-secondary" }, "Loading events..."),
      );
    }

    if (loadingError) {
      return m(
        "div",
        { class: "message-list-error flex items-center justify-center h-full" },
        m("p", { class: "text-red-500" }, `Error: ${loadingError}`),
      );
    }

    const events = getEventsForAgent(agentId);

    if (events.length === 0) {
      return m(
        "div",
        { class: "message-list-empty flex items-center justify-center h-full" },
        m("p", { class: "text-text-secondary" }, "No events yet for this agent."),
      );
    }

    // Start backfill in background
    startBackfill(agentId);

    // Build a map of tool_call_id -> tool_result event for matching
    const toolResults = new Map<string, TranscriptEvent>();
    for (const event of events) {
      if (event.type === "tool_result" && event.tool_call_id) {
        toolResults.set(event.tool_call_id, event);
      }
    }

    // Render events, skipping tool_result events (they are shown inline with assistant messages)
    const messageNodes: m.Vnode[] = [];
    for (const event of events) {
      if (event.type === "user_message") {
        messageNodes.push(renderUserMessage(event));
      } else if (event.type === "assistant_message") {
        messageNodes.push(renderAssistantMessage(event, toolResults));
      }
    }

    return m("div", { class: "message-list-wrapper" }, [
      m(
        "div",
        { class: "message-list mx-auto w-full max-w-(--width-message-column) flex flex-col py-6" },
        messageNodes,
      ),
    ]);
  }

  return {
    view(vnode) {
      const agentId = vnode.attrs.agentId;
      manageStreamConnection(agentId);

      const agentNotFound = agentId !== null && isConversationNotFound(agentId);
      const showFooter = agentId !== null && !agentNotFound;

      const agentName = getAgentName(agentId);
      const agentState = getAgentState(agentId);

      const titleBar = agentId
        ? m(
            "header",
            {
              class: "app-header",
              "data-slot": "header",
            },
            isSlotClaimed("header")
              ? null
              : [
                  m("h1", { class: "app-header-title" }, agentName),
                  agentState ? m("span", { class: "app-header-model-badge" }, agentState.toLowerCase()) : null,
                  m(EmptySlot, { name: "header-actions" }),
                ],
          )
        : null;

      const footerElement = showFooter
        ? m(
            "footer",
            { class: "app-footer", "data-slot": "conversation-footer" },
            isSlotClaimed("conversation-footer")
              ? null
              : [m(EmptySlot, { name: "conversation-before-input" }), m(MessageInput, { agentId })],
          )
        : null;

      return m("div", { class: "app-content-wrapper flex-1 flex flex-col min-h-0" }, [
        titleBar,
        m(EmptySlot, { name: "conversation-after-header" }),
        m(
          "main",
          {
            class: "app-content flex-1 overflow-y-auto px-8 py-6",
            "data-slot": "conversation-content",
            onscroll: handleScrollEvent,
            oncreate: (mainVnode: m.VnodeDOM) => {
              applyScrollPosition(mainVnode.dom as HTMLElement);
            },
            onupdate: (mainVnode: m.VnodeDOM) => {
              applyScrollPosition(mainVnode.dom as HTMLElement);
            },
          },
          isSlotClaimed("conversation-content") ? null : renderMainContent(agentId),
        ),
        footerElement,
      ]);
    },
  };
}
