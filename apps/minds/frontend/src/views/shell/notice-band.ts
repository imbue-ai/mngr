// What the shell's failure notice says, as a pure decision.
//
// One producer for the two surfaces the app has: the band that layers over a
// displayed machine, and the in-page notice a hub page renders instead (a
// band over a hub page would reserve height with nothing behind it to
// preserve). Both read this module, so their copy cannot drift apart.
//
// Relevance, not severity, picks the condition. A dead discovery consumer IS
// the loss of every machine, so it names itself rather than the stuck machine
// it produces -- restarting that machine would not help.

import type { DiscoveryHealth, WorkspaceHealth } from "../../models/health";

/** What an action asks the shell to do. The views bind these; the decision
 * itself stays free of routing and IPC. */
export type NoticeActionKind = "open-recovery" | "restart-app";

export interface NoticeAction {
  label: string;
  kind: NoticeActionKind;
}

export interface NoticePayload {
  /** Identity for replacement. States that share a key never rewrite the
   * strip as the tracker steps between them. */
  key: "discovery-blocked" | "workspace-recovering" | "workspace-restart-failed";
  variant: "warn" | "error";
  message: string;
  action: NoticeAction | null;
}

const DISCOVERY_BLOCKED_MESSAGE =
  "Minds lost contact with your machines and can't reconnect on its own. Your work is safe.";

/** Restarting the app is a desktop affordance. In a browser there is no app to
 * restart, so the notice states the condition and offers nothing -- a button
 * that cannot act is worse than no button. */
function discoveryBlockedNotice(isRestartAppAvailable: boolean): NoticePayload {
  return {
    key: "discovery-blocked",
    variant: "error",
    message: DISCOVERY_BLOCKED_MESSAGE,
    action: isRestartAppAvailable ? { label: "Restart Minds", kind: "restart-app" } : null,
  };
}

/**
 * The band over a displayed machine, or null for no band.
 *
 * Discovery death outranks the machine's own health because it explains it:
 * while the consumer is dead every machine reads stuck, and only one of the
 * two conditions has an action that helps.
 */
export function noticeBandFor(
  workspaceHealth: WorkspaceHealth,
  discoveryHealth: DiscoveryHealth,
  isWorkspaceDisplayed: boolean,
  isRestartAppAvailable = true,
): NoticePayload | null {
  if (!isWorkspaceDisplayed) return null;
  if (discoveryHealth === "blocked") return discoveryBlockedNotice(isRestartAppAvailable);
  if (workspaceHealth === "restart_failed") {
    return {
      key: "workspace-restart-failed",
      variant: "error",
      // Just the condition. What to do about it, and what it costs, belongs
      // to the card behind "Open recovery" -- the band is one line over the
      // machine's own screen, not the place to argue a remedy.
      message: "This machine stopped responding.",
      action: { label: "Open recovery", kind: "open-recovery" },
    };
  }
  if (workspaceHealth === "stuck" || workspaceHealth === "restarting") {
    return {
      key: "workspace-recovering",
      variant: "warn",
      // One line for both states: recovery steps between them on its own, and
      // a second near-identical sentence tells the user nothing the first did
      // not -- it only rewrites the strip mid-read.
      message: "Lost connection to this machine. Reconnecting…",
      action: { label: "Open recovery", kind: "open-recovery" },
    };
  }
  return null;
}

/**
 * The notice a hub page renders in its own flow, or null for none.
 *
 * Only discovery death qualifies: a single machine's health is not a hub
 * page's concern, and the machines list already badges that per row.
 */
export function localPageNoticeFor(
  discoveryHealth: DiscoveryHealth,
  isRestartAppAvailable = true,
): NoticePayload | null {
  return discoveryHealth === "blocked" ? discoveryBlockedNotice(isRestartAppAvailable) : null;
}
