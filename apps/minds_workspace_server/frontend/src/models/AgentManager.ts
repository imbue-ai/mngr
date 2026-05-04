/**
 * Unified WebSocket-based agent and application state manager.
 * Receives real-time updates for agents, applications, and proto-agents.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

export interface AgentState {
  id: string;
  name: string;
  state: string;
  labels: Record<string, string>;
  work_dir: string | null;
  // Per-agent chat activity. THINKING/TOOL_RUNNING/WAITING_ON_PERMISSION/IDLE,
  // or null when the workspace server has no per-agent activity tracking
  // available (e.g. remote agents whose state directory is not present on
  // this host, proto-agents, non-Claude agent types).
  activity_state?: string | null;
}

export interface ApplicationEntry {
  name: string;
  url: string;
}

export interface ProtoAgent {
  agent_id: string;
  name: string;
  creation_type: "worktree" | "chat";
  parent_agent_id: string | null;
}

type WsEvent =
  | { type: "agents_updated"; agents: AgentState[] }
  | { type: "applications_updated"; applications: ApplicationEntry[] }
  | {
      type: "proto_agent_created";
      agent_id: string;
      name: string;
      creation_type: string;
      parent_agent_id: string | null;
    }
  | { type: "proto_agent_completed"; agent_id: string; success: boolean; error: string | null }
  | { type: "refresh_service"; service_name: string };

export type RefreshServiceListener = (serviceName: string) => void;

let agents: AgentState[] = [];
let applications: ApplicationEntry[] = [];
let protoAgents: ProtoAgent[] = [];
let refreshListeners: RefreshServiceListener[] = [];
let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let connected = false;

const RECONNECT_DELAY_MS = 3000;

function getWsUrl(): string {
  const base = apiUrl("/api/ws");
  const loc = window.location;
  const protocol = loc.protocol === "https:" ? "wss:" : "ws:";
  if (base.startsWith("http")) {
    return base.replace(/^http/, "ws");
  }
  return `${protocol}//${loc.host}${base}`;
}

function connect(): void {
  if (ws !== null) {
    return;
  }

  const url = getWsUrl();
  ws = new WebSocket(url);

  ws.onopen = () => {
    connected = true;
    m.redraw();
  };

  ws.onmessage = (event: MessageEvent) => {
    const data = JSON.parse(event.data as string) as WsEvent;
    handleEvent(data);
    m.redraw();
  };

  ws.onclose = () => {
    ws = null;
    connected = false;
    scheduleReconnect();
    m.redraw();
  };

  ws.onerror = () => {
    ws?.close();
  };
}

function scheduleReconnect(): void {
  if (reconnectTimer !== null) {
    return;
  }
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, RECONNECT_DELAY_MS);
}

function handleEvent(event: WsEvent): void {
  switch (event.type) {
    case "agents_updated":
      agents = event.agents;
      break;

    case "applications_updated":
      applications = event.applications;
      break;

    case "proto_agent_created":
      protoAgents.push({
        agent_id: event.agent_id,
        name: event.name,
        creation_type: event.creation_type as "worktree" | "chat",
        parent_agent_id: event.parent_agent_id,
      });
      break;

    case "proto_agent_completed": {
      protoAgents = protoAgents.filter((p) => p.agent_id !== event.agent_id);
      break;
    }

    case "refresh_service":
      for (const listener of refreshListeners) {
        listener(event.service_name);
      }
      break;
  }
}

export function initAgentManager(): void {
  connect();
}

export function isConnected(): boolean {
  return connected;
}

export function getAgents(): AgentState[] {
  return agents;
}

export function getAgentById(id: string): AgentState | undefined {
  return agents.find((a) => a.id === id);
}

export function removeAgentLocally(agentId: string): void {
  agents = agents.filter((a) => a.id !== agentId);
}

export function getApplications(): ApplicationEntry[] {
  return applications;
}

export function getProtoAgents(): ProtoAgent[] {
  return protoAgents;
}

export function addRefreshServiceListener(listener: RefreshServiceListener): void {
  refreshListeners.push(listener);
}

export function removeRefreshServiceListener(listener: RefreshServiceListener): void {
  refreshListeners = refreshListeners.filter((l) => l !== listener);
}
