/**
 * SSE connection management for real-time agent events.
 * Connects to the backend's SSE stream and appends new events.
 *
 * Streams are keyed by agentId so multiple chat panels can subscribe
 * independently; each agent gets its own EventSource.
 */

import { apiUrl } from "../base-path";
import { appendEvents, fetchEvents, type TranscriptEvent } from "./Response";

const activeStreams = new Map<string, EventSource>();
// Tombstones for agents whose streams were explicitly closed via
// disconnectFromStream. Used by pending error-triggered reconnect timeouts
// to distinguish an intentional shutdown from a transient error.
const explicitlyDisconnectedAgents = new Set<string>();
// Per-agent buffer that captures SSE events arriving while a reconnect-time
// snapshot fetch is in flight. The fetch's response REPLACES
// eventsByAgent[agentId]; without buffering, deltas that landed during the
// fetch would be lost. Drained via appendEvents (which dedups) once the
// fetch settles.
const inFlightSnapshotBuffersByAgent = new Map<string, TranscriptEvent[]>();

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
    const pending = inFlightSnapshotBuffersByAgent.get(agentId);
    if (pending !== undefined) {
      pending.push(event);
    } else {
      appendEvents(agentId, [event]);
    }
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
          void reconnectWithSnapshot(agentId);
        }
      }, 3000);
    }
  };
}

async function reconnectWithSnapshot(agentId: string): Promise<void> {
  // The server no longer keeps an unbounded in-memory replay buffer of
  // session events (those are persisted in JSONL on disk). On a transient
  // SSE reconnect, refetch the JSONL snapshot so events that occurred
  // during the disconnect window are recovered from the source of truth,
  // then resubscribe to live deltas.
  //
  // Subscribe to SSE first (and buffer arriving events) so deltas that land
  // between the snapshot read and the EventSource being registered on the
  // server are not lost. Once the fetch settles, the buffered events are
  // re-applied via appendEvents, which dedups by event_id.
  inFlightSnapshotBuffersByAgent.set(agentId, []);
  connectToStream(agentId);
  try {
    await fetchEvents(agentId);
  } catch (error) {
    // Fetch failure is non-fatal; the next SSE error will trigger another
    // reconnect attempt. Log so the failure is still visible in devtools.
    console.warn(`Snapshot refetch failed for agent ${agentId} during SSE reconnect`, error);
  } finally {
    const buffered = inFlightSnapshotBuffersByAgent.get(agentId) ?? [];
    inFlightSnapshotBuffersByAgent.delete(agentId);
    // If the agent was explicitly disconnected (e.g. its panel was
    // unmounted) while the snapshot fetch was in flight, do not drain
    // buffered deltas into the global event store -- the user has moved
    // on and applying them would mutate state for a torn-down view.
    // Mirrors the guard that the onerror reconnect timeout already
    // applies before reviving a stream.
    if (buffered.length > 0 && !explicitlyDisconnectedAgents.has(agentId)) {
      appendEvents(agentId, buffered);
    }
  }
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
