/**
 * Agent discovery -- compatibility layer.
 * Delegates to AgentManager for state, kept for plugin/hook backward compatibility.
 */

import { getAgents as getAgentManagerAgents, type AgentState } from "./AgentManager";

export interface Agent {
  id: string;
  name: string;
  state: string;
}

// Keep Conversation interface for hook compatibility
export interface Conversation {
  id: string;
  name: string;
  model: string;
  latest_response_datetime_utc: string | null;
}

function toAgent(a: AgentState): Agent {
  return { id: a.id, name: a.name, state: a.state };
}

export function getAgents(): Agent[] {
  return getAgentManagerAgents().map(toAgent);
}

export function getAgentsLoaded(): boolean {
  return true;
}

export function getLoadingError(): string | null {
  return null;
}

export async function fetchAgents(): Promise<void> {
  // No-op: agent state comes from the WebSocket via AgentManager
}

// Compatibility shim for hooks/slots that expect conversations
export function getConversations(): Conversation[] {
  return getAgentManagerAgents().map((a) => ({
    id: a.id,
    name: a.name,
    model: a.state,
    latest_response_datetime_utc: null,
  }));
}

// Keep fetchConversations as an alias for fetchAgents
export const fetchConversations = fetchAgents;
