import m from "mithril";
import { isSlotClaimed } from "../slots";
import {
  fetchEvents,
  fetchBackfillEvents,
  getEventsForAgent,
  getFirstEventId,
  isConversationNotFound,
  isBackfillComplete,
  type TranscriptEvent,
  type ToolCall,
} from "../models/Response";
import { connectToStream, disconnectFromStream } from "../models/StreamingMessage";
import { getAgents } from "../models/Conversation";
import { EmptySlot } from "./EmptySlot";
import { MessageInput } from "./MessageInput";
import { renderAssistantMessageChildren } from "./message-renderers";

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
  return agent?.name || "Loading...";
}

function getAgentState(agentId: string | null): string {
  if (!agentId) {
    return "";
  }
  const agents = getAgents();
  const agent = agents.find((a) => a.id === agentId);
  return agent?.state || "";
}

/**
 * Stable message component that skips re-rendering when the event hasn't changed.
 * Session file events are immutable once written, so we only need to render once.
 */
function isCollapsibleUserMessage(content: string): { label: string } | null {
  if (content.startsWith("Stop hook feedback:\n")) {
    return { label: "Stop hook feedback" };
  }
  if (content.startsWith("Base directory for this skill:")) {
    const match = content.match(/skills\/([^\n/]+)/);
    return { label: match ? `Skill: ${match[1]}` : "Skill expansion" };
  }
  return null;
}

function StableUserMessage(): m.Component<{ event: TranscriptEvent }> {
  let renderedEventId: string | null = null;
  return {
    onbeforeupdate(vnode) {
      return vnode.attrs.event.event_id !== renderedEventId;
    },
    view(vnode) {
      const event = vnode.attrs.event;
      renderedEventId = event.event_id;
      const content = event.content || "";
      const collapsible = isCollapsibleUserMessage(content);

      if (collapsible) {
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
            [m("span", { class: "tool-call-chevron" }, "\u25B8"), m("span", collapsible.label)],
          ),
          m("div", { class: "tool-call-details" }, [
            m("div", { class: "tool-call-input" }, [m("pre", m("code", content))]),
          ]),
        ]);
      }

      return m("div", { class: "message-user-bubble" }, [
        m("div", { class: "message-content whitespace-pre-wrap" }, content),
      ]);
    },
  };
}

function renderUserMessage(event: TranscriptEvent): m.Vnode {
  const content = event.content || "";
  const collapsible = isCollapsibleUserMessage(content);
  const messageClass = collapsible ? "message message-system-collapsed" : "message message-user";
  return m("div", { class: messageClass, key: event.event_id }, [m(StableUserMessage, { event })]);
}

function countResolvedToolResults(
  toolCalls: ToolCall[] | undefined,
  toolResults: Map<string, TranscriptEvent>,
): number {
  if (!toolCalls) return 0;
  let count = 0;
  for (const tc of toolCalls) {
    if (toolResults.has(tc.tool_call_id)) count++;
  }
  return count;
}

function StableAssistantMessage(): m.Component<{
  event: TranscriptEvent;
  toolResults: Map<string, TranscriptEvent>;
  agentId: string;
}> {
  let renderedEventId: string | null = null;
  let renderedToolResultCount = 0;
  return {
    onbeforeupdate(vnode) {
      const { event, toolResults } = vnode.attrs;
      const currentToolResultCount = countResolvedToolResults(event.tool_calls, toolResults);
      return event.event_id !== renderedEventId || currentToolResultCount !== renderedToolResultCount;
    },
    view(vnode) {
      const event = vnode.attrs.event;
      const toolResults = vnode.attrs.toolResults;
      const agentId = vnode.attrs.agentId;
      renderedEventId = event.event_id;
      renderedToolResultCount = countResolvedToolResults(event.tool_calls, toolResults);

      return m("div", renderAssistantMessageChildren(event, toolResults, agentId));
    },
  };
}

function renderAssistantMessage(
  event: TranscriptEvent,
  toolResults: Map<string, TranscriptEvent>,
  agentId: string,
): m.Vnode {
  return m(
    "div",
    {
      id: event.event_id,
      class: "message message-assistant",
      key: event.event_id,
    },
    m(StableAssistantMessage, { event, toolResults, agentId }),
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
    const MAX_STALLED_RETRIES = 5;
    const BACKOFF_BASE_MS = 1000;
    const BACKOFF_CAP_MS = 30000;
    let stalledCount = 0;

    while (!isBackfillComplete(agentId) && agentId === currentAgentId) {
      const firstIdBefore = getFirstEventId(agentId);
      await fetchBackfillEvents(agentId);
      m.redraw();

      if (isBackfillComplete(agentId)) {
        break;
      }

      const firstIdAfter = getFirstEventId(agentId);
      if (firstIdAfter === firstIdBefore) {
        stalledCount++;
        if (stalledCount >= MAX_STALLED_RETRIES) {
          break;
        }
        const delayMs = Math.min(BACKOFF_BASE_MS * 2 ** (stalledCount - 1), BACKOFF_CAP_MS);
        await new Promise((resolve) => setTimeout(resolve, delayMs));
      } else {
        stalledCount = 0;
      }
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
        messageNodes.push(renderAssistantMessage(event, toolResults, agentId));
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
