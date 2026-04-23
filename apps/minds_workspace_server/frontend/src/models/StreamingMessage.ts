/**
 * SSE connection management for real-time agent events.
 * Connects to the backend's SSE stream and appends new events.
 *
 * Streams are keyed by agentId so multiple chat panels can subscribe
 * independently; each agent gets its own EventSource.
 */

import { apiUrl } from "../base-path";
import { appendEvents, type TranscriptEvent } from "./Response";

const activeStreams = new Map<string, EventSource>();
// Tombstones for agents whose streams were explicitly closed via
// disconnectFromStream. Used by pending error-triggered reconnect timeouts
// to distinguish an intentional shutdown from a transient error.
const explicitlyDisconnectedAgents = new Set<string>();

export interface StreamingMessage {
  conversationId: string;
  userPrompt: string;
  model: string | null;
  assistantContent: string;
  finalized: boolean;
  error: string | null;
}

export function connectToStream(agentId: string): void {
  if (activeStreams.has(agentId)) {
    return;
  }

  // A fresh connect supersedes any prior explicit-disconnect tombstone.
  explicitlyDisconnectedAgents.delete(agentId);

  const eventSource = new EventSource(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/stream`));
  activeStreams.set(agentId, eventSource);

  eventSource.onmessage = (messageEvent: MessageEvent) => {
    const event = JSON.parse(messageEvent.data) as TranscriptEvent;
    appendEvents(agentId, [event]);
  };

  eventSource.onerror = () => {
    // Close this specific stream and schedule a reconnect. Reconnect is
    // skipped if another caller already reconnected this agent, or if the
    // agent was explicitly disconnected (e.g. its panel was unmounted) while
    // this timeout was pending.
    if (activeStreams.get(agentId) === eventSource) {
      eventSource.close();
      activeStreams.delete(agentId);
      setTimeout(() => {
        const wasExplicitlyDisconnected = explicitlyDisconnectedAgents.delete(agentId);
        if (!wasExplicitlyDisconnected && !activeStreams.has(agentId)) {
          connectToStream(agentId);
        }
      }, 3000);
    }
  };
}

export function disconnectFromStream(agentId: string): void {
  // Always record the intent, even if no stream is currently active. A
  // pending error-triggered reconnect timeout for this agent must see the
  // tombstone so it does not revive the stream.
  explicitlyDisconnectedAgents.add(agentId);
  const eventSource = activeStreams.get(agentId);
  if (eventSource !== undefined) {
    eventSource.close();
    activeStreams.delete(agentId);
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
