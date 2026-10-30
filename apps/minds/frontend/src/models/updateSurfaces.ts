// What a workspace's update situation says on its two surfaces: the titlebar
// pill and the workspace Settings -> Updates tab. Pure decisions over the
// pushed update state, so every state's label, tone, click target, status
// block and action row can be read and tested in one place.

import type { UiWorkspaceUpdate } from "../channel/messages";
import {
  displayVersion,
  isArmedByAutoUpdate,
  isHandledByAutoUpdate,
  isRecreationRequired,
  isSecurityUpdate,
  isUpdateDispatchable,
  isUpdateOffered,
  isUpdateSkipped,
  plainScheduleTargetRef,
  scheduleSkipLine,
  scheduledLine,
  skippedLine,
  skippedVersionText,
  updateActivityNotice,
  updateChatAgentIdFor,
  updateRunOutcome,
  updateRunPhase,
  updateSituation,
  type UpdateSituation,
} from "./updates";

/** Where a pill's click lands: the run's update chat, or the Updates tab. */
export type UpdatePillTarget = "chat" | "tab";

/** The pill one workspace's update situation earns in the titlebar. */
export interface UpdatePill {
  /** Identity, for tests and the DOM. */
  state:
    | "out-of-date"
    | "security-update"
    | "updating"
    | "finishing"
    | "needs-decision"
    | "not-started"
    | "failed"
    | "needs-sign-in"
    | "needs-recreation"
    | "updated-with-note";
  label: string;
  /** "attention" is the solid orange pill: the update needs the user. "quiet"
   * is the grey pill with a dot: information only. */
  tone: "attention" | "quiet";
  /** The quiet pill's dot colour; the attention pill's dot is always white. */
  dot: "warning" | "success";
  /** A run in flight: the dot breathes. */
  isDotPulsing: boolean;
  /** The hover text: the state in a sentence. */
  tooltip: string;
  target: UpdatePillTarget;
}

/** Whether following the pill settles what it reports: a finished run's chat has the whole
 * story. A pill that opens the Updates tab is settled by the tab, once it is left. */
export function isPillSettledByOpening(pill: UpdatePill): boolean {
  return (pill.state === "failed" || pill.state === "updated-with-note") && pill.target === "chat";
}

/** Whether leaving the Updates tab settles what it showed: how a run ended, once read there.
 * A pending decision is not among them, as dismissing it skips the release. */
export function isSituationSettledByLeaving(situation: UpdateSituation): boolean {
  return (
    situation === "not-started" ||
    situation === "failed" ||
    situation === "updated-with-note" ||
    situation === "updated"
  );
}

const NEEDS_ANSWER_TOOLTIP = "This workspace's update needs your answer in its chat.";
const NEEDS_ANSWER_NO_CHAT_TOOLTIP = "This workspace's update needs your answer.";
const OUT_OF_DATE_TOOLTIP = "This workspace is running an older version of Imbue Studio.";
const SECURITY_TOOLTIP = "An update for this workspace is available, which includes a critical security fix.";

/**
 * The titlebar pill for one workspace's update, or null for none: a run in
 * flight, then how the last run ended, then a standing condition. Nothing that
 * asks nothing of the reader earns a pill, and neither does an apply, which
 * the apply modal reports. A pill whose answer lives in the update chat opens
 * the chat once one is known; the rest open the Updates tab.
 */
export function updatePillFor(
  update: UiWorkspaceUpdate | null,
  isUpdating: boolean,
  isAutoUpdateEnabled: boolean,
  isUpdateChatKnown: boolean,
): UpdatePill | null {
  const chatOrTab: UpdatePillTarget = isUpdateChatKnown ? "chat" : "tab";
  const needsAnswerTooltip = isUpdateChatKnown ? NEEDS_ANSWER_TOOLTIP : NEEDS_ANSWER_NO_CHAT_TOOLTIP;
  const phase = updateRunPhase(update, isUpdating);
  switch (phase) {
    case "applying":
      return null;
    case "preparing": {
      // Starting (the run is claimed but its agent, and so its chat, is not up
      // yet) opens the tab; once preparing, the chat has the live progress.
      const isStarting = update === null || update.activity !== "RUNNING";
      return {
        state: "updating",
        label: "Updating…",
        tone: "quiet",
        dot: "warning",
        isDotPulsing: true,
        tooltip: "Preparing an update for this workspace. Nothing changes until it's ready to land.",
        target: isStarting ? "tab" : chatOrTab,
      };
    }
    case "finishing":
      return {
        state: "finishing",
        label: "Finishing update…",
        tone: "quiet",
        dot: "warning",
        isDotPulsing: true,
        tooltip: "This workspace is updated. Its update agent is finishing its checks.",
        target: chatOrTab,
      };
    case "waiting": {
      const hold = update?.is_hold_recorded ? (update.hold_detail ?? "") : null;
      return {
        state: "needs-decision",
        label: "Update needs a decision",
        tone: "attention",
        dot: "warning",
        isDotPulsing: false,
        tooltip:
          hold !== null
            ? `${hold ? `${hold} ` : ""}This workspace's update is waiting for your go-ahead${isUpdateChatKnown ? " in its chat" : ""}.`
            : needsAnswerTooltip,
        target: chatOrTab,
      };
    }
    case "none":
      break;
  }
  if (update === null) return null;
  switch (updateRunOutcome(update)) {
    case "not-started":
      return attentionPill("not-started", "Update didn't start", "This workspace's update didn't start.", "tab");
    case "needs-decision":
      return attentionPill("needs-decision", "Update needs a decision", needsAnswerTooltip, chatOrTab);
    case "failed":
      return attentionPill("failed", "Update didn't finish", "This workspace's update didn't finish.", chatOrTab);
    case "needs-attention":
      return {
        state: "updated-with-note",
        label: "Updated, with a note",
        tone: "quiet",
        dot: "success",
        isDotPulsing: false,
        tooltip: "This workspace updated, and the update agent left a note for you.",
        target: chatOrTab,
      };
    case "none":
      break;
  }
  if (isRecreationRequired(update))
    return attentionPill(
      "needs-recreation",
      "Workspace too old to update",
      "This workspace is too old to update in place.",
      "tab",
    );
  if (isUpdateSkipped(update)) return null;
  if (update.needs_sign_in ?? false)
    return attentionPill(
      "needs-sign-in",
      "Update needs sign-in",
      "This workspace can't update until an AI account is signed in to it.",
      "tab",
    );
  if (!isUpdateOffered(update) || (update.is_scheduled ?? false)) return null;
  if (isHandledByAutoUpdate(update, isAutoUpdateEnabled)) return null;
  if (isSecurityUpdate(update))
    return attentionPill("security-update", "Security update available", SECURITY_TOOLTIP, "tab");
  return {
    state: "out-of-date",
    label: "Update available",
    tone: "quiet",
    dot: "warning",
    isDotPulsing: false,
    tooltip: OUT_OF_DATE_TOOLTIP,
    target: "tab",
  };
}

function attentionPill(
  state: UpdatePill["state"],
  label: string,
  tooltip: string,
  target: UpdatePillTarget,
): UpdatePill {
  return { state, label, tone: "attention", dot: "warning", isDotPulsing: false, tooltip, target };
}

/** The Updates tab entry's dot, in the pill's colour, or null while there is no pill. */
export function updateNavDotFor(pill: UpdatePill | null): "warning" | "success" | null {
  if (pill === null) return null;
  return pill.tone === "attention" ? "warning" : pill.dot;
}

export type UpdateStatusTone = "neutral" | "info" | "warn" | "error" | "success";

export type UpdateStatusIcon = "check" | "arrow-up" | "clock" | "spinner" | "question" | "alert" | "user" | "slash" | "power";

/** What the Updates tab's status block says for one situation. */
export interface UpdateTabStatus {
  /** The situation it was drawn from; "stopped" comes from the store's stopped filter, never from the update itself. */
  situation: UpdateSituation;
  tone: UpdateStatusTone;
  icon: UpdateStatusIcon;
  title: string;
  /** The one line of explanation. */
  line: string;
  /** A second line, when the situation needs one. */
  secondaryLine: string;
  /** The update agent's own words, quoted under the line; "" for none. */
  quote: string;
  /** The version jump beside the title, or null to show none. */
  versions: { current: string; supported: string } | null;
  /** An update can be started from here, so its costs are said. */
  isCostShown: boolean;
  /** The release on offer carries a critical security fix. */
  isSecurityShown: boolean;
}

/** Where to go when a run ended without landing: its own chat has the details. */
const CHECK_IN_LINE = "Check in with the update agent in the update chat.";

/** What a stopped machine's update surfaces say, its update state being held back until it starts. */
const STOPPED_UPDATES_LINE = "Start this workspace to see its updates.";

/** The oldest version that updates in place; anything older needs a new workspace. */
const OLDEST_IN_PLACE_VERSION = "minds-v0.3.10";

/** What the verdict of a finished run says, in the reader's terms; it points at the run's chat only when
 * the app knows the chat. */
function verdictMessage(update: UiWorkspaceUpdate, isChatKnown: boolean): string {
  // The ref the run reported landing, which the row note names too. Detection
  // is only the fallback: its next sweep is an exec into a machine whose
  // services are still coming back from the apply, so for a while after a
  // verdict `current_version` still reads the version this run moved off.
  const landed = displayVersion(update.success_note_version || update.current_version);
  const checkIn = isChatKnown ? ` ${CHECK_IN_LINE}` : "";
  switch (update.verdict) {
    case "UPDATED":
      return `This workspace was updated${landed ? ` to ${landed}` : ""}.`;
    case "UPDATED_WITH_REBUILD_ITEMS":
      if (update.verdict_detail) return "The update agent left a note for you:";
      return isChatKnown
        ? "The update agent left a note for you in the update chat."
        : "The update agent left a note for you, but its chat can't be found.";
    case "ALREADY_CURRENT":
      return `This workspace is already up to date${update.current_version ? ` on ${displayVersion(update.current_version)}` : ""}. Nothing was changed.`;
    case "NEEDS_RECREATION":
      return `The update agent found that this update can't be applied to this workspace in place.${checkIn}`;
    case "STUCK":
      return `The update ran into a problem it couldn't recover from.${checkIn}`;
    case "NEEDS_DECISION":
      return `The update stopped short of applying: it can't keep something you built in this workspace.${isChatKnown ? " Decide how to go on in its chat." : ""}`;
    case "REFUSED":
      return update.in_place_compatible_ref
        ? `The update wasn't applied. ${displayVersion(update.in_place_compatible_ref)} can still be applied to this workspace.${checkIn}`
        : `The update wasn't applied.${checkIn}`;
    default:
      return "";
  }
}

/** The line for a machine no AI account is signed in to. */
function needsSignInLine(isAutoUpdateEnabled: boolean): string {
  return isAutoUpdateEnabled
    ? "No AI account is signed in to this workspace, so Studio can't update it automatically. Sign in, and Studio schedules its update again."
    : "No AI account is signed in to this workspace, so its update can't start. Sign in, then try again.";
}

/** The line for an update on offer. */
function offeredLine(update: UiWorkspaceUpdate, isAutoUpdateEnabled: boolean): string {
  const version = displayVersion(update.supported_version) || "An update";
  return isHandledByAutoUpdate(update, isAutoUpdateEnabled)
    ? `${version} is ready for this workspace. Studio will schedule it for the next window, or schedule or update it yourself.`
    : `${version} is ready for this workspace. Schedule it for the update window, or update now.`;
}

const UNREADABLE_MACHINE_LINE =
  "Imbue Studio can't detect the version of this workspace. An agent inside the workspace can tell you what " +
  "it's running, and update it if it should be. Running the update starts that agent: it may find there's " +
  "nothing to do.";

const NO_APP_VERSION_LINE =
  "This build of Imbue Studio tracks a branch rather than a released version, so it has nothing to compare " +
  "workspaces against. Every workspace reads as unknown here, whatever version it is on.";

/** The situations an update can be started or scheduled from, whose costs the tab says. */
const COST_SITUATIONS: ReadonlySet<UpdateSituation> = new Set<UpdateSituation>([
  "offered",
  "scheduled",
  "needs-sign-in",
  "skipped",
  "unknown",
]);

/** The situations where the release is still on offer, so its security fix is worth saying. */
const RELEASE_ON_OFFER_SITUATIONS: ReadonlySet<UpdateSituation> = new Set<UpdateSituation>([
  "offered",
  "scheduled",
  "skipped",
  "needs-sign-in",
]);

/**
 * The Updates tab's status block for one machine. `isQuietWhileStopped` is the
 * store's word that nothing of a stopped machine's update state is shown;
 * `workspaceWindow` is the window its updates start in (its own, else
 * Studio's), which its schedule falls back to when the machine's own file
 * names none.
 */
export function updateTabStatusFor(
  update: UiWorkspaceUpdate,
  isUpdating: boolean,
  isQuietWhileStopped: boolean,
  isAutoUpdateEnabled: boolean,
  workspaceWindow: string,
): UpdateTabStatus {
  const situation: UpdateSituation = isQuietWhileStopped ? "stopped" : updateSituation(update, isUpdating);
  const base = {
    situation,
    secondaryLine: "",
    quote: "",
    versions: { current: update.current_version, supported: update.supported_version },
    isCostShown: COST_SITUATIONS.has(situation),
    isSecurityShown: isSecurityUpdate(update) && RELEASE_ON_OFFER_SITUATIONS.has(situation),
  };
  const landed = { current: update.success_note_version || update.supported_version, supported: update.supported_version };
  const isChatKnown = updateChatAgentIdFor(update, isUpdating) !== "";
  switch (situation) {
    case "stopped":
      return {
        ...base,
        tone: "neutral",
        icon: "power",
        title: "Workspace stopped",
        line: STOPPED_UPDATES_LINE,
        versions: null,
      };
    case "in-progress": {
      const activity = updateActivityNotice(update, isUpdating);
      // A press still waiting on its first frame reads as starting, as STARTING does.
      const isRunning = isRunningActivity(update);
      if (update.activity === "WAITING")
        return {
          ...base,
          tone: "warn",
          icon: "question",
          title: update.is_hold_recorded ? "Waiting for your go-ahead" : "Waiting on you",
          line: activity.message,
        };
      const title = !isRunning
        ? "Starting the update"
        : update.activity === "APPLYING"
          ? "Updating"
          : update.activity === "FINISHING"
            ? "Finishing the update"
            : "Preparing the update";
      return { ...base, tone: "info", icon: "spinner", title, line: activity.message };
    }
    case "not-started":
      return {
        ...base,
        tone: "error",
        icon: "alert",
        title: "Update didn't start",
        line: update.dispatch_failure || "Couldn't start the update agent in this workspace.",
      };
    case "needs-decision":
      return {
        ...base,
        tone: "warn",
        icon: "question",
        title: "Needs your decision",
        line: verdictMessage(update, isChatKnown),
        quote: update.verdict_detail ?? "",
      };
    case "failed":
      return {
        ...base,
        tone: "error",
        icon: "alert",
        title: "Update didn't finish",
        line: update.activity === "STALLED" ? updateActivityNotice(update).message : verdictMessage(update, isChatKnown),
        quote: update.verdict_detail ?? "",
      };
    case "updated-with-note":
      return {
        ...base,
        tone: "success",
        icon: "check",
        title: updatedTitle(update),
        line: verdictMessage(update, isChatKnown),
        quote: update.verdict_detail ?? "",
        versions: landed,
      };
    case "needs-recreation":
      return {
        ...base,
        tone: "warn",
        icon: "alert",
        title: "Too old to update in place",
        line:
          `This workspace runs ${displayVersion(update.current_version) || "an old version"}; the oldest that ` +
          `updates in place is ${displayVersion(OLDEST_IN_PLACE_VERSION)}. Create a new workspace and ask its ` +
          "agent to migrate your work across.",
      };
    case "app-behind":
      return {
        ...base,
        tone: "neutral",
        icon: "arrow-up",
        title: "Ahead of Imbue Studio",
        line:
          `This workspace (${displayVersion(update.current_version)}) is newer than this copy of Imbue Studio ` +
          `(${displayVersion(update.supported_version)}). Update the app to catch up — there's nothing to run here.`,
      };
    case "skipped":
      return {
        ...base,
        tone: "neutral",
        icon: "slash",
        title: `Skipped ${skippedVersionText(update)}`,
        line: skippedLine(update, isAutoUpdateEnabled),
      };
    case "needs-sign-in":
      return {
        ...base,
        tone: "warn",
        icon: "user",
        title: "Sign in to update",
        line: needsSignInLine(isAutoUpdateEnabled),
      };
    case "scheduled":
      return {
        ...base,
        tone: "info",
        icon: "clock",
        title: "Update scheduled",
        line: scheduledLine(update, workspaceWindow, isAutoUpdateEnabled),
        secondaryLine: scheduleSkipLine(update),
      };
    case "offered":
      return {
        ...base,
        tone: "warn",
        icon: "arrow-up",
        title: "Update available",
        line: offeredLine(update, isAutoUpdateEnabled),
      };
    case "updated":
      return update.verdict === "ALREADY_CURRENT"
        ? {
            ...base,
            tone: "success",
            icon: "check",
            title: "Already up to date",
            line: verdictMessage(update, isChatKnown),
            versions: { current: update.current_version, supported: update.current_version },
          }
        : {
            ...base,
            tone: "success",
            icon: "check",
            title: updatedTitle(update),
            line: verdictMessage(update, isChatKnown),
            versions: landed,
          };
    case "unknown":
      return update.unknown_reason === "NO_APP_VERSION"
        ? {
            ...base,
            tone: "neutral",
            icon: "alert",
            title: "Version unknown",
            line: NO_APP_VERSION_LINE,
            // Dev-loop instructions in product copy: a released build is pinned
            // to a `minds-v*` tag, so only the dev loop can reach this.
            secondaryLine:
              "just minds-start pins this build to your template worktree's branch. To compare against a " +
              "release instead, launch with just minds-start-cloud or just minds-start <minds-vX.Y.Z>.",
          }
        : { ...base, tone: "neutral", icon: "alert", title: "Version unknown", line: UNREADABLE_MACHINE_LINE };
    case "up-to-date":
      return {
        ...base,
        tone: "success",
        icon: "check",
        title: "Up to date",
        line: "This workspace is up to date.",
      };
  }
}

function updatedTitle(update: UiWorkspaceUpdate): string {
  const landed = displayVersion(update.success_note_version || update.current_version);
  return landed ? `Updated to ${landed}` : "Updated";
}

/** What an Updates tab button does. */
export type UpdateTabActionKind =
  | "schedule"
  | "now"
  | "open-chat"
  | "cancel-schedule"
  | "skip"
  | "sign-in"
  | "create-workspace";

export interface UpdateTabAction {
  kind: UpdateTabActionKind;
  label: string;
  variant: "primary" | "secondary" | "ghost";
  /** For "schedule": the ref the press names ("" for the app's own release). */
  targetRef?: string;
}

/**
 * The Updates tab's action row for one machine, the main action first.
 * `chatAgentId` is the run's chat when it still exists ("" when unknown or
 * deleted, which drops Open chat).
 */
export function updateTabActionsFor(
  update: UiWorkspaceUpdate,
  status: UpdateTabStatus,
  isAutoUpdateEnabled: boolean,
  chatAgentId: string,
): UpdateTabAction[] {
  const openChat = (variant: UpdateTabAction["variant"]): UpdateTabAction[] =>
    chatAgentId ? [{ kind: "open-chat", label: "Open chat", variant }] : [];
  const pressPair = (): UpdateTabAction[] => {
    const schedule: UpdateTabAction = { kind: "schedule", label: "Schedule update", variant: "primary", targetRef: "" };
    const now: UpdateTabAction = { kind: "now", label: "Update now", variant: "secondary" };
    // Scheduling is the default: the apply takes services down. A security fix makes now the default.
    return status.isSecurityShown
      ? [{ ...now, variant: "primary" }, { ...schedule, variant: "secondary" }]
      : [schedule, now];
  };
  switch (status.situation) {
    case "offered":
    case "skipped":
    case "unknown":
      return pressPair();
    case "scheduled": {
      const actions: UpdateTabAction[] = [
        { kind: "now", label: "Update now instead", variant: status.isSecurityShown ? "primary" : "secondary" },
      ];
      // A schedule armed for another version can still be moved to the app's own.
      const scheduleTarget = plainScheduleTargetRef(update);
      if (scheduleTarget !== null)
        actions.push({ kind: "schedule", label: "Schedule update", variant: "secondary", targetRef: scheduleTarget });
      // Under auto-update a cancel would only be re-armed by the next sweep;
      // skipping is the way to say "not this release".
      actions.push(
        isArmedByAutoUpdate(update, isAutoUpdateEnabled)
          ? { kind: "skip", label: `Skip ${skippedVersionText(update)}`, variant: "ghost" }
          : { kind: "cancel-schedule", label: "Cancel schedule", variant: "ghost" },
      );
      return actions;
    }
    case "in-progress":
      // Starting has no chat yet; waiting is the reader's move, so its chat is the main action.
      if (!isRunningActivity(update)) return [];
      return openChat(update.activity === "WAITING" ? "primary" : "secondary");
    // How a run ended needs no Dismiss: leaving the tab settles it.
    case "not-started":
      return isUpdateDispatchable(update) ? [{ kind: "now", label: "Try again", variant: "primary" }] : [];
    case "needs-decision":
      // Dismissing it is a skip: a fresh run would set the kept work aside.
      return [...openChat("primary"), { kind: "skip", label: "Dismiss", variant: "ghost" }];
    case "failed":
    case "updated-with-note":
      return openChat("primary");
    case "updated":
      return update.verdict === "UPDATED" ? openChat("primary") : [];
    case "needs-sign-in":
      return [{ kind: "sign-in", label: "Sign in", variant: "primary" }];
    case "needs-recreation":
      return [{ kind: "create-workspace", label: "Create a new workspace", variant: "primary" }];
    case "app-behind":
    case "up-to-date":
    case "stopped":
      return [];
  }
}

function isRunningActivity(update: UiWorkspaceUpdate): boolean {
  return (
    update.activity === "RUNNING" ||
    update.activity === "WAITING" ||
    update.activity === "APPLYING" ||
    update.activity === "FINISHING"
  );
}

/** Whether the tab's skip question may stand: only where a skip is on offer. */
export function isSkipOffered(actions: UpdateTabAction[]): boolean {
  return actions.some((action) => action.kind === "skip");
}

/** Whether "Update to a specific version" is offered: on any machine that can update in place. */
export function isSpecificVersionOffered(update: UiWorkspaceUpdate, status: UpdateTabStatus): boolean {
  return status.situation !== "stopped" && !isRecreationRequired(update);
}

/** The tab's two cost notes, said wherever an update can be started. */
export const UPDATE_COST_NOTES: readonly string[] = [
  "An agent updates this workspace, which uses tokens. You can follow what it does in its chat.",
  "Your chats keep running while it prepares, then pause for a few minutes while the update is applied.",
];

/** The release's critical security fix, as the tab's card says it. Rewritten
 * whenever a release raises the security floor, since that release's fix is
 * the one every workspace below the floor lacks. */
export const SECURITY_FIX_TITLE = "This update includes a critical security fix.";
export const SECURITY_FIX_DETAILS =
  "Eight certificates issued under eMudhra's emSign authority (an Indian certificate provider trusted by major " +
  "browsers and operating systems) were compromised, which means they could be used to impersonate a real " +
  "website. This update stops your workspace from trusting them, so your data and credentials only reach the " +
  "sites they're meant for.";
