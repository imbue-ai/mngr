// Per-workspace system-interface health, plus app-global discovery health.
//
// Deliberately state-only: the legacy chrome auto-redirected STUCK
// workspaces to the Recovery page (with per-connection redirect latches and
// 15s server re-asserts to survive lost one-shot events). That machinery is
// gone -- the snapshot-on-connect channel means a client always knows the
// current state, and the UI surfaces it as a banner + manual actions instead
// of navigating on the user's behalf.

import type { UiDiscoveryHealthMessage, UiEnvironmentMessage, UiHealthMessage } from "../channel/messages";

export type WorkspaceHealth = "healthy" | "stuck" | "restarting" | "restart_failed";

export type DiscoveryHealth = "healthy" | "reconnecting" | "blocked";

/**
 * Why a machine cannot be reached, when the answer is about this device rather
 * than about the machine: it has no network at all, or it is on a network that
 * blocks the connection Minds uses (SSH).
 *
 * Carried alongside the health state rather than replacing it. The machine
 * really is unreachable -- what this adds is that the machine is not the thing
 * that is wrong, so the surfaces must not narrate a restart, and there is no
 * restart to offer while it holds.
 */
export type EnvironmentBlock = "NONE" | "OFFLINE" | "SSH_BLOCKED";

export class HealthStore {
  discoveryHealth: DiscoveryHealth = "healthy";

  private statusByAgentId = new Map<string, WorkspaceHealth>();
  private errorByAgentId = new Map<string, string>();
  private deviceEnvironment: EnvironmentBlock = "NONE";
  private isRestartANoOpByAgentId = new Set<string>();
  // A map rather than a set, because the absent case is a third answer: the
  // frame reports the shape of a restart only while one is running, and "no
  // restart to describe" is not the same as "a restart that skips the stop".
  private isRestartStartOnlyByAgentId = new Map<string, boolean>();

  /** Reconnect is resync: the snapshot only carries non-HEALTHY agents, so
   * the per-workspace state must be cleared before it is reapplied or an
   * agent that recovered while the socket was down stays stuck forever.
   * discoveryHealth is left alone -- the snapshot always includes a
   * discovery_health frame that overwrites it. */
  reset(): void {
    this.statusByAgentId.clear();
    this.errorByAgentId.clear();
    // deviceEnvironment is left alone, like discoveryHealth: the snapshot
    // always carries an environment frame, which overwrites it.
    this.isRestartANoOpByAgentId.clear();
    this.isRestartStartOnlyByAgentId.clear();
  }

  applyHealthMessage(message: UiHealthMessage): void {
    if (message.status === "healthy") {
      this.statusByAgentId.delete(message.agent_id);
      this.errorByAgentId.delete(message.agent_id);
      this.isRestartANoOpByAgentId.delete(message.agent_id);
      this.isRestartStartOnlyByAgentId.delete(message.agent_id);
      return;
    }
    this.statusByAgentId.set(message.agent_id, message.status);
    if (message.error) this.errorByAgentId.set(message.agent_id, message.error);
    else this.errorByAgentId.delete(message.agent_id);
    if (message.is_restart_a_no_op) this.isRestartANoOpByAgentId.add(message.agent_id);
    else this.isRestartANoOpByAgentId.delete(message.agent_id);
    if (message.is_restart_start_only === null || message.is_restart_start_only === undefined) {
      this.isRestartStartOnlyByAgentId.delete(message.agent_id);
    } else {
      this.isRestartStartOnlyByAgentId.set(message.agent_id, message.is_restart_start_only);
    }
  }

  applyDiscoveryHealthMessage(message: UiDiscoveryHealthMessage): void {
    this.discoveryHealth = message.state;
  }

  applyEnvironmentMessage(message: UiEnvironmentMessage): void {
    this.deviceEnvironment = message.state;
  }

  /** A workspace with no tracked non-healthy status is healthy. */
  statusFor(agentId: string): WorkspaceHealth {
    return this.statusByAgentId.get(agentId) ?? "healthy";
  }

  errorFor(agentId: string): string | null {
    return this.errorByAgentId.get(agentId) ?? null;
  }

  /**
   * The device-level condition to speak for the app as a whole, or "NONE".
   *
   * Reported by the server as one app-global fact. It holds whether or not any
   * machine has been convicted -- an app opened on a dead network has nothing
   * convicted, because nothing has been asked to load, and that is exactly when
   * the user most needs telling.
   */
  appEnvironmentBlock(): EnvironmentBlock {
    return this.deviceEnvironment;
  }

  /** Whether this workspace's dispatched start reported it booted nothing, so
   * there is no failed restart to name -- only a machine that never answered. */
  isRestartANoOpFor(agentId: string): boolean {
    return this.isRestartANoOpByAgentId.has(agentId);
  }

  /** Whether the restart this workspace is running skips the stop step, or null
   * when there is no restart in flight to describe. Only `false` -- a full
   * stop+start bounce, which only the user's own click dispatches -- licenses
   * calling it a restart. */
  isRestartStartOnlyFor(agentId: string): boolean | null {
    return this.isRestartStartOnlyByAgentId.get(agentId) ?? null;
  }

  isContentAssumedReady(agentId: string): boolean {
    return this.statusFor(agentId) === "healthy";
  }
}
