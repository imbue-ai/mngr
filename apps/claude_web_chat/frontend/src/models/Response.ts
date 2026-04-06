/**
 * Event store for common transcript events.
 * Replaces the LLM response model with events fetched from session files.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

export interface ToolCall {
  tool_call_id: string;
  tool_name: string;
  input_preview: string;
}

export interface TranscriptEvent {
  timestamp: string;
  type: "user_message" | "assistant_message" | "tool_result";
  event_id: string;
  source: string;
  message_uuid: string;

  // user_message fields
  role?: string;
  content?: string;

  // assistant_message fields
  model?: string;
  text?: string;
  tool_calls?: ToolCall[];
  stop_reason?: string | null;
  usage?: {
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens?: number | null;
    cache_write_tokens?: number | null;
  } | null;

  // tool_result fields
  tool_call_id?: string;
  tool_name?: string;
  output?: string;
  is_error?: boolean;
}

// For hook compatibility
export interface ResponseItem {
  id: string;
  model: string;
  prompt: string | null;
  system: string | null;
  response: string;
  conversation_id: string;
  datetime_utc: string;
  duration_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
}

interface EventsResponse {
  events: TranscriptEvent[];
}

const eventsByAgent: Record<string, TranscriptEvent[]> = {};
const notFoundAgentIds = new Set<string>();
const backfillComplete: Record<string, boolean> = {};

export function isConversationNotFound(agentId: string): boolean {
  return notFoundAgentIds.has(agentId);
}

export function getEventsForAgent(agentId: string): TranscriptEvent[] {
  return eventsByAgent[agentId] ?? [];
}

export function getFirstEventId(agentId: string): string | null {
  const events = eventsByAgent[agentId];
  if (!events || events.length === 0) {
    return null;
  }
  return events[0].event_id;
}

export function isBackfillComplete(agentId: string): boolean {
  return backfillComplete[agentId] === true;
}

export function appendEvents(agentId: string, newEvents: TranscriptEvent[]): void {
  const existing = eventsByAgent[agentId] ?? [];
  const existingIds = new Set(existing.map((e) => e.event_id));
  const deduped = newEvents.filter((e) => !existingIds.has(e.event_id));
  if (deduped.length > 0) {
    eventsByAgent[agentId] = [...existing, ...deduped];
    m.redraw();
  }
}

export function prependEvents(agentId: string, olderEvents: TranscriptEvent[]): void {
  const existing = eventsByAgent[agentId] ?? [];
  const existingIds = new Set(existing.map((e) => e.event_id));
  const deduped = olderEvents.filter((e) => !existingIds.has(e.event_id));
  if (deduped.length > 0) {
    eventsByAgent[agentId] = [...deduped, ...existing];
    m.redraw();
  } else {
    backfillComplete[agentId] = true;
  }
}

export async function fetchEvents(agentId: string): Promise<TranscriptEvent[]> {
  notFoundAgentIds.delete(agentId);

  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId },
    });
    eventsByAgent[agentId] = result.events;
    return result.events;
  } catch (error) {
    const requestError = error as { code?: number; message?: string };
    if (requestError.code === 404) {
      notFoundAgentIds.add(agentId);
    }
    throw error;
  }
}

export async function fetchBackfillEvents(agentId: string): Promise<void> {
  if (backfillComplete[agentId]) {
    return;
  }

  const firstEventId = getFirstEventId(agentId);
  if (!firstEventId) {
    backfillComplete[agentId] = true;
    return;
  }

  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId, before: firstEventId, limit: "50" },
    });
    if (result.events.length === 0) {
      backfillComplete[agentId] = true;
    } else {
      prependEvents(agentId, result.events);
    }
  } catch {
    // Backfill failure is non-fatal
  }
}

export async function sendMessage(agentId: string, message: string): Promise<void> {
  if (!message.trim()) {
    return;
  }

  await m.request({
    method: "POST",
    url: apiUrl("/api/agents/:agentId/message"),
    params: { agentId },
    body: { message: message.trim() },
  });
}

// Compatibility shims
export class ConversationNotFoundError extends Error {
  constructor(agentId: string) {
    super(`Agent not found: ${agentId}`);
    this.name = "ConversationNotFoundError";
  }
}

export function getResponsesForConversation(agentId: string): ResponseItem[] {
  return [];
}

export function getAllResponses(): Record<string, ResponseItem[]> {
  return {};
}

export function getLastResponseModel(_agentId: string): string | null {
  return null;
}

export function appendSyntheticResponse(): void {}

export async function insertResponseItem(): Promise<void> {}

export function fetchResponses(agentId: string): Promise<ResponseItem[]> {
  return fetchEvents(agentId).then(() => []);
}
