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

import type { DiscoveryHealth, EnvironmentCondition, RecoveryKind, WorkspaceHealth } from "../../models/health";
import type { StandingUpdateNotice, UpdateRunOutcome, UpdateRunPhase } from "../../models/updates";

/** What an action asks the shell to do. The views bind these; the decision
 * itself stays free of routing and IPC. */
export type NoticeActionKind = "open-recovery" | "restart-app" | "update-workspace";

export interface NoticeAction {
  label: string;
  kind: NoticeActionKind;
}

export interface NoticePayload {
  /** Identity for replacement. States that share a key never rewrite the
   * strip as the tracker steps between them; a run's phases carry separate
   * keys because the phase change is what the reader is owed. */
  key:
    | "discovery-blocked"
    | "environment-blocked"
    | "workspace-recovering"
    | "workspace-restart-failed"
    | "workspace-update-preparing"
    | "workspace-update-applying"
    | "workspace-update-waiting"
    | "workspace-update-outcome"
    | "workspace-out-of-date"
    | "workspace-needs-recreation";
  variant: "info" | "warn" | "error";
  message: string;
  action: NoticeAction | null;
}

const DISCOVERY_BLOCKED_MESSAGE =
  "Minds lost contact with your machines and can't reconnect on its own. Your work is safe.";

/** The conditions with a line to say: a measured, confirmed block. */
type EnvironmentBlock = Exclude<EnvironmentCondition, "NONE" | "UNKNOWN">;

function isEnvironmentBlock(condition: EnvironmentCondition): condition is EnvironmentBlock {
  return condition !== "NONE" && condition !== "UNKNOWN";
}

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
const ENVIRONMENT_BLOCKED_MESSAGE: Record<EnvironmentBlock, string> = {
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
  /** True of this device as a whole, before any machine has been convicted.
   * "UNKNOWN" while nothing has been measured, which withholds every blame
   * below it rather than clearing the device. */
  deviceEnvironment?: EnvironmentCondition;
  /** Which recovery is in flight, from the health frame: "restart" is the
   * user's own stop+start bounce, "start" the app's unattended dispatch, null
   * no recovery to describe. */
  recoveryKind?: RecoveryKind | null;
  /** False for a machine on this device, which the network cannot explain. */
  isWorkspaceNetworkDependent?: boolean;
  /** This one connection failed on this device, on a network that works. */
  isDeviceCannotConnect?: boolean;
  /** Which part of an update run owns this machine right now. */
  updateRunPhase?: UpdateRunPhase;
  /** The run's own line naming what it is waiting on, when it recorded a
   * hold ("" for a hold with no line); null when it merely reads idle. */
  updateHoldDetail?: string | null;
  /** What this machine's last run left behind, once it is over. */
  updateRunOutcome?: UpdateRunOutcome;
  /** A standing condition rather than an event, so it is ranked below them all. */
  standingUpdateNotice?: StandingUpdateNotice;
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
 *
 * An update's apply step ranks above the machine's own health (it explains it)
 * but below this device's own condition (it does not); the rest of a run ranks
 * below all of them.
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
    deviceEnvironment = "NONE",
    recoveryKind = null,
    isWorkspaceNetworkDependent = true,
    isDeviceCannotConnect = false,
    updateRunPhase = "none",
    updateHoldDetail = null,
    updateRunOutcome = "none",
    standingUpdateNotice = "none",
  } = context;
  if (!isWorkspaceDisplayed) return null;
  if (discoveryHealth === "blocked") return discoveryBlockedNotice(isRestartAppAvailable);
  // This device's own condition outranks the backend's for the same reason
  // discovery death outranks both: it explains them. A laptop with no network
  // cannot reach the provider either, so its poll errors too -- naming the
  // provider there would blame a backend that is fine for a condition the user
  // can fix. It keeps the recovering key, since the condition is still "we have
  // lost contact", only correctly attributed. It speaks over a healthy machine
  // too, offering nothing: there is no recovery card to open. The one exception
  // is a restart the user asked for -- their own stop+start bounce narrates
  // itself, since there is a recovery to report and the waiting state would be
  // false. The app's own unattended start is not that: it is entered unasked,
  // within seconds of any network flap, and lasts as long as the network is
  // down, which is precisely when the device's condition is the explanation
  // the user needs.
  //
  // And it is silent over a machine that runs on this device: a docker
  // container answers over loopback with the wifi off, so a dead network
  // explains nothing about its outage. Displacing its recovery notice would
  // blame the network for a machine the network cannot touch, and send the
  // user to a card for a recovery that would have worked.
  const isUserBounceRunning = workspaceHealth === "recovering" && recoveryKind === "restart";
  const condition: EnvironmentCondition =
    isUserBounceRunning || !isWorkspaceNetworkDependent ? "NONE" : deviceEnvironment;
  // The apply outranks the machine's own health because it explains it: the
  // app took those services down on purpose (the recovery card is withheld for
  // the same reason). It explains nothing about this device, so a CONFIRMED
  // device block speaks first; an unmeasured device names nobody, so the apply
  // still speaks over it. A machine that dies mid-prepare is an ordinary outage.
  if (updateRunPhase === "applying" && !isEnvironmentBlock(condition)) return updateRunNotice("applying", null);
  // The three explanations in rank order, each one line. They differ only in
  // what they say, so the ranking is the whole of the logic and is kept where
  // it can be read as a list -- the payload they share is built once below.
  //
  // The provider's name is the cause alone: one line over the machine's own
  // screen has room for the condition, not for what it means for this machine
  // or what minds is doing about it. The device-side line likewise says whose
  // fault it is and nothing else -- its remedy is an app restart, a real
  // interruption, offered from the card next to the error that justifies it.
  //
  // An unmeasured device names nobody. Both lines below it blame something on
  // the far side of this device's network, and until a probe has looked at
  // that network there is no ground to say the provider is what failed --
  // after a wake, the provider's own poll errored because the laptop was
  // asleep, and naming it would be the wrong headline. The generic recovering
  // line below still speaks, so the user is not left with nothing.
  const explanation = isEnvironmentBlock(condition)
    ? ENVIRONMENT_BLOCKED_MESSAGE[condition]
    : workspaceHealth === "healthy" || condition === "UNKNOWN"
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
  if (workspaceHealth === "recovery_failed") {
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
  if (workspaceHealth === "stuck" || workspaceHealth === "recovering") {
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
  // Below the health conditions (the machine being unusable) and above the
  // standing "out of date"; what a machine is doing outranks how its last
  // attempt ended.
  const runNotice = updateRunNotice(updateRunPhase, updateHoldDetail);
  if (runNotice !== null) return runNotice;
  const outcomeNotice = updateOutcomeNotice(updateRunOutcome);
  if (outcomeNotice !== null) return outcomeNotice;
  // Last: a standing condition, not an event; it will still be true tomorrow.
  return standingNotice(standingUpdateNotice);
}

const SEE_UPDATE: NoticeAction = { label: "See update", kind: "update-workspace" };

/** The line about a machine's version that will still be true tomorrow, or null. */
function standingNotice(notice: StandingUpdateNotice): NoticePayload | null {
  switch (notice) {
    case "out-of-date":
      return {
        key: "workspace-out-of-date",
        variant: "warn",
        message: "This machine is running an older version of Minds.",
        action: SEE_UPDATE,
      };
    case "needs-recreation":
      // The same action as out-of-date: the modal behind it carries the steps.
      return {
        key: "workspace-needs-recreation",
        variant: "warn",
        message: "This machine is too old to update in place.",
        action: { label: "See how to update", kind: "update-workspace" },
      };
    case "none":
      return null;
  }
}

/**
 * What a run in flight says over the machine it is running in, or null.
 * Preparing touches nothing; applying takes the services away, so it says so
 * before the reader starts wondering; waiting names what for, when the run said.
 */
function updateRunNotice(phase: UpdateRunPhase, holdDetail: string | null): NoticePayload | null {
  switch (phase) {
    case "applying":
      return {
        key: "workspace-update-applying",
        variant: "info",
        message: "Updating this machine. Its services restart while the update lands.",
        action: SEE_UPDATE,
      };
    case "preparing":
      return {
        key: "workspace-update-preparing",
        variant: "info",
        // The reader is using the machine meanwhile; the question is whether to stop.
        message: "Preparing an update for this machine. Nothing changes until it's ready to land.",
        action: SEE_UPDATE,
      };
    case "waiting": {
      // A recorded hold is about something the reader built; the run's own line says which.
      return {
        key: "workspace-update-waiting",
        variant: "warn",
        message:
          holdDetail !== null
            ? `${holdDetail ? `${holdDetail} ` : ""}This machine's update is waiting for your decision in its chat.`
            : "This machine's update has stopped to ask you something in its chat.",
        action: SEE_UPDATE,
      };
    }
    default:
      return null;
  }
}

/**
 * What a finished run still owes the reader, or null: the machine-side
 * counterpart to the row badge, which cannot be seen from inside the machine.
 */
function updateOutcomeNotice(outcome: UpdateRunOutcome): NoticePayload | null {
  const message =
    outcome === "failed"
      ? "This machine's update didn't finish."
      : outcome === "needs-attention"
        ? "This machine updated, and the update agent left a note for you."
        : null;
  if (message === null) return null;
  return {
    key: "workspace-update-outcome",
    variant: outcome === "needs-attention" ? "info" : "error",
    message,
    action: SEE_UPDATE,
  };
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
  environment: EnvironmentCondition = "NONE",
): NoticePayload | null {
  if (discoveryHealth === "blocked") return discoveryBlockedNotice(isRestartAppAvailable);
  if (!isEnvironmentBlock(environment)) return null;
  return {
    key: "environment-blocked",
    variant: "warn",
    message: ENVIRONMENT_BLOCKED_MESSAGE[environment],
    action: null,
  };
}

/**
 * The notice the workspace's own hub page renders, or null. Reached through the
 * band's standing leg so the two cannot drift; carries no health conditions,
 * which the band over the machine already reports.
 */
export function workspacePageNoticeFor(notice: StandingUpdateNotice): NoticePayload | null {
  return standingNotice(notice);
}
