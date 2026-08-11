// The Machines-list row rules, extracted from LandingPage's row renderer so
// they are a testable pure decision: Start is offered only for a
// shutdown-capable machine that is STOPPED, Stop only for one that is
// RUNNING, and neither during transitions or when the liveness is unknown.
//
// A dead discovery consumer makes every row's data stale at once. Nothing is
// arriving to correct it, so the row stops claiming to know a state and stops
// offering actions computed from one -- an action taken on a frozen reading
// is worse than no action offered.

import type { UiWorkspaceEntry } from "../../channel/messages";
import type { DiscoveryHealth } from "../../models/health";
import type { MindLiveness } from "../../models/create";

export interface MindControls {
  isStartShown: boolean;
  isStopShown: boolean;
}

/** Whether the app has any current reading of a machine's state at all. */
export function isMachineStateKnown(discoveryHealth: DiscoveryHealth): boolean {
  return discoveryHealth !== "blocked";
}

export function mindControlsFor(
  entry: Pick<UiWorkspaceEntry, "supports_shutdown">,
  liveness: MindLiveness,
  discoveryHealth: DiscoveryHealth,
): MindControls {
  const isShutdownSupported = (entry.supports_shutdown ?? false) && isMachineStateKnown(discoveryHealth);
  return {
    isStartShown: isShutdownSupported && liveness === "STOPPED",
    isStopShown: isShutdownSupported && liveness === "RUNNING",
  };
}
