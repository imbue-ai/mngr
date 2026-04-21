/**
 * Chat panel for dockview. Contains the main message list and message input
 * for an agent, mounted as a tab within the dockview workspace.
 *
 * If the agent is still being created (a proto-agent), shows the creation
 * log stream instead. Automatically switches to the chat view when creation
 * completes.
 */

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
} from "../models/Response";
import { connectToStream, disconnectFromStream } from "../models/StreamingMessage";
import { getAgentById, getProtoAgents } from "../models/AgentManager";
import { apiUrl } from "../base-path";
import { EmptySlot } from "./EmptySlot";
import { MessageInput } from "./MessageInput";
import { renderUserMessage, renderAssistantMessage } from "./message-renderers";
import { getTerminalUrl, openIframeTabForAgent } from "./DockviewWorkspace";

function getAgentTerminalUrl(agentId: string): string {
  const baseUrl = getTerminalUrl();
  const separator = baseUrl.includes("?") ? "&" : "?";
  // The ttyd dispatch script is invoked as `bash -c "$SCRIPT" <args...>` where
  // the first trailing arg becomes $0 (not $1). The dispatch reads KEY="$1",
  // so we prepend a dummy "_" to land the real key in $1. That matches the
  // pattern used by the existing workdir deep-link in DockviewWorkspace.ts.
  // Passing the agent name as $2 lets agent.sh attach to that agent's tmux
  // session ("${MNGR_PREFIX}<name>") rather than the primary agent's. If the
  // agent isn't in the local cache yet, fall back to no name arg and let
  // agent.sh attach to the ambient session.
  const agent = getAgentById(agentId);
  const args = agent?.name ? `arg=_&arg=agent&arg=${encodeURIComponent(agent.name)}` : "arg=_&arg=agent";
  return `${baseUrl}${separator}${args}`;
}

function openAgentTerminalTab(agentId: string): void {
  const agent = getAgentById(agentId);
  const title = agent?.name ? `${agent.name} terminal` : "agent terminal";
  openIframeTabForAgent(agentId, getAgentTerminalUrl(agentId), title);
}

const SCROLL_BOTTOM_THRESHOLD_PX = 40;

function isNearBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight < SCROLL_BOTTOM_THRESHOLD_PX;
}

function scrollToBottom(element: HTMLElement): void {
  element.scrollTop = element.scrollHeight;
}

function isProtoAgent(agentId: string): boolean {
  return getProtoAgents().some((p) => p.agent_id === agentId);
}

export function ChatPanel(): m.Component<{ agentId: string }> {
  let loading = false;
  let loadingError: string | null = null;
  let currentAgentId: string | null = null;
  let userScrolledUp = false;
  let previousScrollTop = 0;
  let backfillStarted = false;

  // Screen capture state (shown when agent has no conversation)
  let screenContent: string | null = null;
  let screenError: string | null = null;
  let screenLoading = false;
  let screenAgentId: string | null = null;

  // Proto-agent log state
  let logWs: WebSocket | null = null;
  let logLines: string[] = [];
  let logDone = false;
  let logSuccess = false;
  let logError: string | null = null;
  let logAgentId: string | null = null;

  async function fetchScreenCapture(agentId: string): Promise<void> {
    if (screenAgentId === agentId && (screenContent !== null || screenLoading)) {
      return;
    }
    screenAgentId = agentId;
    screenLoading = true;
    screenContent = null;
    screenError = null;
    try {
      const result = await m.request<{ screen: string | null; error?: string }>({
        method: "GET",
        url: apiUrl("/api/agents/:agentId/screen"),
        params: { agentId, scrollback: "true" },
      });
      screenContent = result.screen;
      screenError = result.error ?? null;
    } catch {
      screenError = "Failed to capture screen";
    } finally {
      screenLoading = false;
      m.redraw();
    }
  }

  function connectLogWs(agentId: string): void {
    if (logWs !== null) {
      logWs.close();
    }
    logLines = [];
    logDone = false;
    logSuccess = false;
    logError = null;
    logAgentId = agentId;

    const base = apiUrl(`/api/proto-agents/${encodeURIComponent(agentId)}/logs`);
    const loc = window.location;
    const protocol = loc.protocol === "https:" ? "wss:" : "ws:";
    let url: string;
    if (base.startsWith("http")) {
      url = base.replace(/^http/, "ws");
    } else {
      url = `${protocol}//${loc.host}${base}`;
    }

    logWs = new WebSocket(url);

    logWs.onmessage = (event: MessageEvent) => {
      const data = JSON.parse(event.data as string) as
        | { line: string }
        | { done: true; success: boolean; error: string | null };

      if ("line" in data) {
        logLines.push(data.line);
      } else if ("done" in data) {
        logDone = true;
        logSuccess = data.success;
        logError = data.error;
      }
      m.redraw();
    };

    logWs.onclose = () => {
      logWs = null;
    };

    logWs.onerror = () => {
      logWs?.close();
    };
  }

  function disconnectLogWs(): void {
    if (logWs !== null) {
      logWs.close();
      logWs = null;
    }
    logAgentId = null;
  }

  function renderBuildLog(agentId: string): m.Vnode {
    if (logAgentId !== agentId) {
      connectLogWs(agentId);
    }

    return m("div", { style: "display: flex; flex-direction: column; height: 100%; padding: 16px;" }, [
      m(
        "div",
        { style: "font-weight: 600; margin-bottom: 8px; font-size: 0.9em; color: #666;" },
        logDone ? (logSuccess ? "Agent created successfully" : "Agent creation failed") : "Creating agent...",
      ),
      logError ? m("div", { style: "color: red; margin-bottom: 8px; font-size: 0.85em;" }, logError) : null,
      m(
        "div",
        {
          style:
            "flex: 1; overflow-y: auto; background: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: 0.8em; padding: 12px; border-radius: 4px; white-space: pre-wrap; word-break: break-all;",
          onupdate(vnode: m.VnodeDOM) {
            const el = vnode.dom as HTMLElement;
            el.scrollTop = el.scrollHeight;
          },
        },
        logLines.map((line, i) => m("div", { key: i, style: "line-height: 1.5;" }, line)),
      ),
    ]);
  }

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

  function manageStreamConnection(agentId: string): void {
    if (!isConversationNotFound(agentId)) {
      connectToStream(agentId);
    } else {
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

  function renderMessages(agentId: string): m.Vnode {
    // If this agent is still being created, show the build log
    if (isProtoAgent(agentId)) {
      return renderBuildLog(agentId);
    }

    // Agent finished creating -- disconnect log WebSocket and force reload
    if (logAgentId === agentId) {
      disconnectLogWs();
      currentAgentId = null;
    }

    ensureAgentLoaded(agentId);
    manageStreamConnection(agentId);

    if (isConversationNotFound(agentId)) {
      fetchScreenCapture(agentId);
      return m("div", { class: "message-list-not-found flex flex-col items-center justify-center h-full gap-4 p-8" }, [
        m("p", { class: "text-lg font-semibold text-text-primary" }, "No conversation data"),
        m("p", { class: "text-text-secondary" }, "This agent has no Claude session. It may have crashed on startup."),
        screenLoading
          ? m("p", { class: "text-text-secondary" }, "Loading terminal output...")
          : screenContent
            ? m(
                "pre",
                {
                  class:
                    "text-sm bg-gray-900 text-gray-100 p-4 rounded-lg overflow-auto w-full max-h-96 font-mono whitespace-pre",
                },
                screenContent,
              )
            : screenError
              ? m("p", { class: "text-text-secondary text-sm" }, `Could not capture terminal: ${screenError}`)
              : null,
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

    startBackfill(agentId);

    const toolResults = new Map<string, TranscriptEvent>();
    for (const event of events) {
      if (event.type === "tool_result" && event.tool_call_id) {
        toolResults.set(event.tool_call_id, event);
      }
    }

    const messageNodes: m.Vnode[] = [];
    for (const event of events) {
      if (event.type === "user_message") {
        const userNode = renderUserMessage(event);
        if (userNode !== null) {
          messageNodes.push(userNode);
        }
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
    onremove() {
      disconnectLogWs();
    },

    view(vnode) {
      const agentId = vnode.attrs.agentId;

      return m("div", { class: "chat-panel flex flex-col h-full" }, [
        m(
          "main",
          {
            class: "app-content flex-1 overflow-y-auto px-8 py-6",
            onscroll: handleScrollEvent,
            oncreate: (mainVnode: m.VnodeDOM) => {
              applyScrollPosition(mainVnode.dom as HTMLElement);
            },
            onupdate: (mainVnode: m.VnodeDOM) => {
              applyScrollPosition(mainVnode.dom as HTMLElement);
            },
          },
          isSlotClaimed("conversation-content") ? null : renderMessages(agentId),
        ),
        // Only show message input when not in proto-agent mode
        isProtoAgent(agentId)
          ? null
          : m("footer", { class: "app-footer" }, [
              m(EmptySlot, { name: "conversation-before-input" }),
              m(MessageInput, { agentId }),
              m("div", { class: "chat-agent-terminal-link" }, [
                m(
                  "button",
                  {
                    type: "button",
                    onclick: () => openAgentTerminalTab(agentId),
                  },
                  "Open agent terminal",
                ),
              ]),
            ]),
      ]);
    },
  };
}
