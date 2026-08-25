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

import type { DiscoveryHealth, EnvironmentBlock, WorkspaceHealth } from "../../models/health";

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
  key: "discovery-blocked" | "environment-blocked" | "workspace-recovering" | "workspace-restart-failed";
  variant: "warn" | "error";
  message: string;
  action: NoticeAction | null;
}

const DISCOVERY_BLOCKED_MESSAGE =
  "Minds lost contact with your machines and can't reconnect on its own. Your work is safe.";

/**
 * What this device's own condition means, in one line.
 *
 * The two states are never collapsed into one "connection problem". On a
 * network that blocks SSH the user's browser works, so telling them they are
 * offline is a claim they can see is false -- and they would reasonably
 * discount whatever the app says next.
 *
 * Each line reports only this device, because this device is all that was
 * measured. Reassuring the user that their machines are still running would be
 * a claim about the far side of a connection nothing here can make: minds
 * stopped being able to look at exactly the moment it went offline, so a
 * machine that died a second earlier would be described as fine.
 */
const ENVIRONMENT_BLOCKED_MESSAGE: Record<Exclude<EnvironmentBlock, "NONE">, string> = {
  OFFLINE: "No network connection.",
  SSH_BLOCKED: "This network blocks the connection to your machines.",
};

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
 * Everything beyond the machine's own health that can change what the band says.
 */
export interface NoticeBandContext {
  /** False in a browser, where there is no app to offer a restart of. */
  isRestartAppAvailable?: boolean;
  /** The provider hosting this machine, when discovery cannot reach it. */
  unreachableProviderLabel?: string | null;
  /** True of this device as a whole, before any machine has been convicted. */
  deviceEnvironmentBlock?: EnvironmentBlock;
  /** False for a machine on this device, which the network cannot explain. */
  isWorkspaceNetworkDependent?: boolean;
  /** This one connection failed on this device, on a network that works. */
  isDeviceCannotConnect?: boolean;
}

/**
 * The band over a displayed machine, or null for no band.
 *
 * Discovery death outranks the machine's own health because it explains it:
 * while the consumer is dead every machine reads stuck, and only one of the
 * two conditions has an action that helps. The explanations below it are the
 * same shape at narrowing scales, and are ranked by how much they explain.
 * This device having no usable network is the widest: it takes down the
 * provider poll as well, so naming the provider under it would blame a backend
 * that is fine. An unreachable backend is next -- this machine reads stuck
 * because minds cannot reach the provider that hosts it, so the band names the
 * provider rather than the machine. A connection that failed on this device,
 * on a network that otherwise works, is the narrowest: this one machine reads
 * stuck because the app could not build a connection to it, not because
 * anything is wrong with it. All three keep the recovering notice's key -- the
 * condition is still "we have lost contact and are still trying", only better
 * explained -- so the strip is not rewritten as an explanation lands and
 * clears. Discovery death does not: it is not this machine's condition at all,
 * and its notice carries its own key and its own action.
 */
export function noticeBandFor(
  workspaceHealth: WorkspaceHealth,
  discoveryHealth: DiscoveryHealth,
  isWorkspaceDisplayed: boolean,
  context: NoticeBandContext = {},
): NoticePayload | null {
  const {
    isRestartAppAvailable = true,
    unreachableProviderLabel = null,
    deviceEnvironmentBlock = "NONE",
    isWorkspaceNetworkDependent = true,
    isDeviceCannotConnect = false,
  } = context;
  if (!isWorkspaceDisplayed) return null;
  if (discoveryHealth === "blocked") return discoveryBlockedNotice(isRestartAppAvailable);
  // This device's own condition outranks the backend's for the same reason
  // discovery death outranks both: it explains them. A laptop with no network
  // cannot reach the provider either, so its poll errors too -- naming the
  // provider there would blame a backend that is fine for a condition the user
  // can fix. It keeps the recovering key, since the condition is still "we have
  // lost contact", only correctly attributed. It speaks over a healthy machine
  // too, offering nothing: there is no recovery card to open. A restart that is
  // actually running is the one exception, narrating itself -- there is a
  // restart to report, so the waiting state would be false.
  //
  // And it is silent over a machine that runs on this device: a docker
  // container answers over loopback with the wifi off, so a dead network
  // explains nothing about its outage. Displacing its recovery notice would
  // blame the network for a machine the network cannot touch, and send the
  // user to a card for a restart that would have worked.
  const isRestartRunning = workspaceHealth === "restarting";
  const block: EnvironmentBlock =
    isRestartRunning || !isWorkspaceNetworkDependent ? "NONE" : deviceEnvironmentBlock;
  // The three explanations in rank order, each one line. They differ only in
  // what they say, so the ranking is the whole of the logic and is kept where
  // it can be read as a list -- the payload they share is built once below.
  //
  // The provider's name is the cause alone: one line over the machine's own
  // screen has room for the condition, not for what it means for this machine
  // or what minds is doing about it. The device-side line likewise says whose
  // fault it is and nothing else -- its remedy is an app restart, a real
  // interruption, offered from the card next to the error that justifies it.
  const explanation =
    block !== "NONE"
      ? ENVIRONMENT_BLOCKED_MESSAGE[block]
      : workspaceHealth === "healthy"
        ? null
        : unreachableProviderLabel !== null
          ? `Can't connect to ${unreachableProviderLabel}`
          : isDeviceCannotConnect
            ? "Can't connect to this machine from this device"
            : null;
  if (explanation !== null) {
    return {
      key: "workspace-recovering",
      variant: "warn",
      message: explanation,
      // The device's own condition is the one explanation that also speaks over
      // a healthy machine, and there is no recovery card to open for one.
      action: workspaceHealth !== "healthy" ? { label: "Open recovery", kind: "open-recovery" } : null,
    };
  }
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
 * Only app-wide conditions qualify: a single machine's health is not a hub
 * page's concern, and the machines list already badges that per row. Discovery
 * death is one such condition, and so is this device having no usable network
 * -- it is one fact about the laptop however many machines it takes down, and a
 * user looking at a hub page has no band to read it from. It carries no action,
 * because there is nothing in the app that fixes it.
 */
export function localPageNoticeFor(
  discoveryHealth: DiscoveryHealth,
  isRestartAppAvailable = true,
  environmentBlock: EnvironmentBlock = "NONE",
): NoticePayload | null {
  if (discoveryHealth === "blocked") return discoveryBlockedNotice(isRestartAppAvailable);
  if (environmentBlock === "NONE") return null;
  return {
    key: "environment-blocked",
    variant: "warn",
    message: ENVIRONMENT_BLOCKED_MESSAGE[environmentBlock],
    action: null,
  };
}
