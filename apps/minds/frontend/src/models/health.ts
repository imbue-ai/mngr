// Per-workspace system-interface health, plus app-global discovery health.
//
// Deliberately state-only: the legacy chrome auto-redirected STUCK
// workspaces to the Recovery page (with per-connection redirect latches and
// 15s server re-asserts to survive lost one-shot events). That machinery is
// gone -- the snapshot-on-connect channel means a client always knows the
// current state, and the UI surfaces it as a banner + manual actions instead
// of navigating on the user's behalf.

import type { UiDiscoveryHealthMessage, UiEnvironmentMessage, UiHealthMessage } from "../channel/messages";

// Neither recovery state says the machine was stopped -- the unattended path
// only starts it. Which one ran is `RecoveryKind` below; read that, never the
// state name.
export type WorkspaceHealth = "healthy" | "stuck" | "recovering" | "recovery_failed";

/** Which of the two host recoveries is running. Only "restart" stops the
 * machine, and only the user's own click asks for one. */
export type RecoveryKind = "start" | "restart";

export type DiscoveryHealth = "healthy" | "reconnecting" | "blocked";

/**
 * Why a machine cannot be reached, when the answer is about this device rather
 * than about the machine: it has no network at all, or it is on a network that
 * blocks the connection Minds uses (SSH).
 *
 * Carried alongside the health state rather than replacing it. The machine
 * really is unreachable -- what this adds is that the machine is not the thing
 * that is wrong, so the surfaces must not narrate a recovery, and there is no
 * recovery to offer while it holds.
 *
 * "UNKNOWN" is the reading before anything has been measured -- at startup,
 * and after a wake until the next probe lands. It is not "NONE": a surface
 * told the device is fine goes on to blame the next thing in line, which after
 * a wake is the provider, on the strength of no measurement at all. A surface
 * reading "UNKNOWN" blames nothing.
 */
export type EnvironmentCondition = "NONE" | "OFFLINE" | "SSH_BLOCKED" | "UNKNOWN";

export class HealthStore {
  discoveryHealth: DiscoveryHealth = "healthy";

  private statusByAgentId = new Map<string, WorkspaceHealth>();
  private errorByAgentId = new Map<string, string>();
  private deviceEnvironment: EnvironmentCondition = "UNKNOWN";
  private isRecoveryANoOpByAgentId = new Set<string>();
  // A map rather than a set, because the absent case is a third answer: the
  // frame reports which recovery is running only while one is, and "no recovery
  // to describe" is not the same as "a recovery that skips the stop".
  private recoveryKindByAgentId = new Map<string, RecoveryKind>();

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
    this.isRecoveryANoOpByAgentId.clear();
    this.recoveryKindByAgentId.clear();
  }

  applyHealthMessage(message: UiHealthMessage): void {
    if (message.status === "healthy") {
      this.statusByAgentId.delete(message.agent_id);
      this.errorByAgentId.delete(message.agent_id);
      this.isRecoveryANoOpByAgentId.delete(message.agent_id);
      this.recoveryKindByAgentId.delete(message.agent_id);
      return;
    }
    this.statusByAgentId.set(message.agent_id, message.status);
    if (message.error) this.errorByAgentId.set(message.agent_id, message.error);
    else this.errorByAgentId.delete(message.agent_id);
    if (message.is_recovery_a_no_op) this.isRecoveryANoOpByAgentId.add(message.agent_id);
    else this.isRecoveryANoOpByAgentId.delete(message.agent_id);
    if (message.recovery_kind === null || message.recovery_kind === undefined) {
      this.recoveryKindByAgentId.delete(message.agent_id);
    } else {
      this.recoveryKindByAgentId.set(message.agent_id, message.recovery_kind);
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
  appEnvironmentCondition(): EnvironmentCondition {
    return this.deviceEnvironment;
  }

  /** Whether this workspace's dispatched start reported it booted nothing, so
   * there is no failed restart to name -- only a machine that never answered. */
  isRecoveryANoOpFor(agentId: string): boolean {
    return this.isRecoveryANoOpByAgentId.has(agentId);
  }

  /** Which recovery this workspace is running, or null when there is none in
   * flight to describe. Only "restart" -- a full stop+start bounce, which only
   * the user's own click dispatches -- licenses calling it a restart. */
  recoveryKindFor(agentId: string): RecoveryKind | null {
    return this.recoveryKindByAgentId.get(agentId) ?? null;
  }

  isContentAssumedReady(agentId: string): boolean {
    return this.statusFor(agentId) === "healthy";
  }
}
