/**
 * SSE connection management for real-time agent events.
 * Connects to the backend's SSE stream and appends new events.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { appendEvents, type TranscriptEvent } from "./Response";

let activeEventSource: EventSource | null = null;
let activeAgentId: string | null = null;

export interface StreamingMessage {
  conversationId: string;
  userPrompt: string;
  model: string | null;
  assistantContent: string;
  finalized: boolean;
  error: string | null;
}

export function connectToStream(agentId: string): void {
  if (agentId === activeAgentId && activeEventSource !== null) {
    return;
  }
  disconnectFromStream();

  activeAgentId = agentId;
  const eventSource = new EventSource(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/stream`));
  activeEventSource = eventSource;

  eventSource.onmessage = (messageEvent: MessageEvent) => {
    const event = JSON.parse(messageEvent.data) as TranscriptEvent;
    appendEvents(agentId, [event]);
  };

  eventSource.onerror = () => {
    if (eventSource === activeEventSource) {
      disconnectFromStream();
      setTimeout(() => {
        if (activeAgentId === null && agentId) {
          connectToStream(agentId);
        }
      }, 3000);
    }
  };
}

export function disconnectFromStream(): void {
  if (activeEventSource !== null) {
    activeEventSource.close();
    activeEventSource = null;
    activeAgentId = null;
  }
}

// Compatibility shims
export function getStreamingMessage(_agentId: string): StreamingMessage | null {
  return null;
}

export function isStreaming(): boolean {
  return false;
}

export function clearStreamingMessage(): void {}

export function consumeLastFinalizedMessage(): StreamingMessage | null {
  return null;
}

export function startStreamingMessage(): void {}
export function appendStreamingDelta(): void {}
export function finalizeStreamingMessage(): void {}
export function markStreamingError(): void {}
