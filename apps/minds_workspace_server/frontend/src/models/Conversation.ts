/**
 * Agent discovery -- replaces the conversation model.
 * Fetches mngr-managed agents from the backend on page load.
 */

import m from "mithril";
import { getSelectedAgentId, selectAgent } from "../navigation";
import { apiUrl } from "../base-path";

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

interface AgentListResponse {
  agents: Agent[];
}

let agents: Agent[] = [];
let loadingError: string | null = null;
let agentsLoaded = false;

export function getAgents(): Agent[] {
  return agents;
}

export function getAgentsLoaded(): boolean {
  return agentsLoaded;
}

export function getLoadingError(): string | null {
  return loadingError;
}

export async function fetchAgents(): Promise<void> {
  try {
    const response = await m.request<AgentListResponse>({
      method: "GET",
      url: apiUrl("/api/agents"),
    });
    agents = response.agents;
    loadingError = null;
    agentsLoaded = true;
    if (!getSelectedAgentId()) {
      if (agents.length > 0) {
        selectAgent(agents[0].id);
      }
    }
  } catch (error) {
    loadingError = (error as Error).message;
    agentsLoaded = true;
  }
}

// Compatibility shim for hooks/slots that expect conversations
export function getConversations(): Conversation[] {
  return agents.map((a) => ({
    id: a.id,
    name: a.name,
    model: a.state,
    latest_response_datetime_utc: null,
  }));
}

// Keep fetchConversations as an alias for fetchAgents
export const fetchConversations = fetchAgents;
