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

/** What clicking a machines-list row should do, as a testable pure decision. */
export type RowClickAction = "enter" | "recover" | "recover-start" | "prompt-start";

// ``liveness`` is a plain string (only the "STOPPED" comparison matters) so
// callers without a MindLivenessTracker (e.g. CreateTemplatePage) can pass
// the entry's raw liveness field directly.
export function rowClickActionFor(
  entry: Pick<UiWorkspaceEntry, "supports_shutdown" | "is_slow_start">,
  liveness: string,
  isHealthy: boolean,
): RowClickAction {
  if (!isHealthy) return "recover";
  if ((entry.supports_shutdown ?? false) && liveness === "STOPPED") {
    // A stopped container cannot be entered. Quick-start machines go straight
    // to Recovery, which dispatches the idempotent start. Slow-start machines
    // (a cloud restore taking minutes) instead prompt the user to press Start
    // explicitly -- recovery's auto-dispatch is sized for local bounces and an
    // unexplained minutes-long spinner is worse than a clear message.
    return (entry.is_slow_start ?? false) ? "prompt-start" : "recover-start";
  }
  return "enter";
}

/** The alert shown for a "prompt-start" click: the one place the wording lives.
 * "In the machines list" holds everywhere -- the landing page IS that list, and
 * other surfaces (e.g. the template stepper) point the user back to it. */
export function slowStartPromptMessage(name: string): string {
  return (
    `"${name}" is stopped. Press Start on its card in the machines list -- ` +
    "restoring a cloud workspace can take several minutes."
  );
}
