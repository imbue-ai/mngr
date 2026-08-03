// The Machines-list start/stop control visibility rules, extracted from
// LandingPage's row renderer so they are a testable pure decision: Start is
// offered only for a shutdown-capable machine that is STOPPED, Stop only for
// one that is RUNNING, and neither during transitions or when the liveness
// is unknown.

import type { UiWorkspaceEntry } from "../../channel/messages";
import type { MindLiveness } from "../../models/create";

export interface MindControls {
  isStartShown: boolean;
  isStopShown: boolean;
}

export function mindControlsFor(
  entry: Pick<UiWorkspaceEntry, "supports_shutdown">,
  liveness: MindLiveness,
): MindControls {
  const isShutdownSupported = entry.supports_shutdown ?? false;
  return {
    isStartShown: isShutdownSupported && liveness === "STOPPED",
    isStopShown: isShutdownSupported && liveness === "RUNNING",
  };
}
