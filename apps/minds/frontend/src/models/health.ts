// Per-workspace system-interface health, plus app-global discovery health.
//
// Deliberately state-only: the legacy chrome auto-redirected STUCK
// workspaces to the Recovery page (with per-connection redirect latches and
// 15s server re-asserts to survive lost one-shot events). That machinery is
// gone -- the snapshot-on-connect channel means a client always knows the
// current state, and the UI surfaces it as a banner + manual actions instead
// of navigating on the user's behalf.

import type { UiDiscoveryHealthMessage, UiHealthMessage } from "../channel/messages";

export type WorkspaceHealth = "healthy" | "stuck" | "restarting" | "restart_failed";

export type DiscoveryHealth = "healthy" | "reconnecting" | "blocked";

export class HealthStore {
  discoveryHealth: DiscoveryHealth = "healthy";

  private statusByAgentId = new Map<string, WorkspaceHealth>();
  private errorByAgentId = new Map<string, string>();

  /** Reconnect is resync: the snapshot only carries non-HEALTHY agents, so
   * the per-workspace state must be cleared before it is reapplied or an
   * agent that recovered while the socket was down stays stuck forever.
   * discoveryHealth is left alone -- the snapshot always includes a
   * discovery_health frame that overwrites it. */
  reset(): void {
    this.statusByAgentId.clear();
    this.errorByAgentId.clear();
  }

  applyHealthMessage(message: UiHealthMessage): void {
    if (message.status === "healthy") {
      this.statusByAgentId.delete(message.agent_id);
      this.errorByAgentId.delete(message.agent_id);
      return;
    }
    this.statusByAgentId.set(message.agent_id, message.status);
    if (message.error) this.errorByAgentId.set(message.agent_id, message.error);
    else this.errorByAgentId.delete(message.agent_id);
  }

  applyDiscoveryHealthMessage(message: UiDiscoveryHealthMessage): void {
    this.discoveryHealth = message.state;
  }

  /** A workspace with no tracked non-healthy status is healthy. */
  statusFor(agentId: string): WorkspaceHealth {
    return this.statusByAgentId.get(agentId) ?? "healthy";
  }

  errorFor(agentId: string): string | null {
    return this.errorByAgentId.get(agentId) ?? null;
  }

  isContentAssumedReady(agentId: string): boolean {
    return this.statusFor(agentId) === "healthy";
  }
}
