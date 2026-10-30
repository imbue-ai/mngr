// Per-machine template-update state, and the actions that change it. Mirrors
// the channel's `workspace_updates` frame, which every surface reads so they
// cannot disagree about a machine; the one addition is an optimistic pending
// set (as MindLivenessTracker keeps for Start/Stop) so "Update now" locks the
// row before the round trip.

import m from "mithril";
import type {
  UiWorkspaceAutoUpdateOverride,
  UiWorkspaceUpdate,
  UiWorkspaceUpdatesMessage,
  UpdateVerdict,
} from "../channel/messages";

/** The state for a machine the backend has said nothing about. Never "up to
 * date": silence is not a positive read. */
const UNKNOWN_UPDATE: UiWorkspaceUpdate = {
  availability: "UNKNOWN",
  current_version: "",
  supported_version: "",
  activity: "IDLE",
};

/** Whether a run is live: the row reports it and a second dispatch is refused.
 * WAITING counts: the agent is alive and still owns the workspace. */
export function isRunInFlight(update: UiWorkspaceUpdate): boolean {
  return (
    update.activity === "STARTING" ||
    update.activity === "RUNNING" ||
    update.activity === "WAITING" ||
    update.activity === "APPLYING" ||
    update.activity === "FINISHING"
  );
}

/** Whether the app has positively read this machine as a minor release behind,
 * or below the security floor: what the badge and titlebar status claim, and
 * what "Update all" covers. A machine behind only by a patch is never prompted
 * for, unless the patch carries a security fix it lacks. */
export function isUpdateOffered(update: UiWorkspaceUpdate): boolean {
  return update.availability === "OUT_OF_DATE" || isSecurityUpdate(update);
}

/** Whether the app has positively read this machine as below the security
 * floor, by a build that can take it past it: its update includes a security fix. */
export function isSecurityUpdate(update: UiWorkspaceUpdate): boolean {
  return update.availability === "SECURITY_UPDATE";
}

/** Whether the app has positively read this machine as too old to update in
 * place: the only way forward is a new machine, migrated into. */
export function isRecreationRequired(update: UiWorkspaceUpdate): boolean {
  return update.availability === "NEEDS_RECREATION";
}

/** Whether auto-update will take care of this machine on its own, so the
 * surfaces that offer the update stay quiet. A machine the sweep will not or
 * cannot arm says it is behind as it would without auto-update, among them one
 * whose last run went out for the release on offer. One positively read behind
 * but not armed yet is handled; the next sweep arms it. */
export function isHandledByAutoUpdate(update: UiWorkspaceUpdate, isAutoUpdateEnabled: boolean): boolean {
  return (
    isAutoUpdateEnabled &&
    isUpdateOffered(update) &&
    (update.is_workspace_readable ?? false) &&
    !(update.needs_sign_in ?? false) &&
    !isUpdateSkipped(update) &&
    !update.dispatch_failure &&
    (update.last_dispatch_target_ref ?? "") !== update.supported_version
  );
}

/** Whether this machine's armed schedule is auto-update's own while auto-update
 * is on: it asks nothing of the reader, and the way to drop it is a skip, since
 * the next sweep would re-arm a cancelled one. One the user armed is theirs to cancel. */
export function isArmedByAutoUpdate(update: UiWorkspaceUpdate, isAutoUpdateEnabled: boolean): boolean {
  return isAutoUpdateEnabled && (update.is_scheduled ?? false) && (update.is_schedule_auto_armed ?? false);
}

/** Whether the user skipped the release this machine is behind by: it is
 * neither updated automatically nor reminded about until a newer one ships. A
 * schedule armed by hand since then is the newer intent, and outranks the skip. */
export function isUpdateSkipped(update: UiWorkspaceUpdate): boolean {
  return isUpdateOffered(update) && (update.is_update_skipped ?? false) && !(update.is_scheduled ?? false);
}

/** The release a skip is for, as the reader sees it: the one this app ships. */
export function skippedVersionText(update: UiWorkspaceUpdate): string {
  return displayVersion(update.supported_version) || "this update";
}

/** What skipping this machine's update means, asked before it is done. */
export function skipConfirmationLine(update: UiWorkspaceUpdate, isAutoUpdateEnabled: boolean): string {
  const version = skippedVersionText(update);
  return isAutoUpdateEnabled
    ? `Imbue Studio won't update this workspace to ${version}. When a newer version is available, it'll schedule that one instead.`
    : `Imbue Studio won't update this workspace to ${version}, and won't remind you about it. When a newer version is available, it'll let you know.`;
}

/** What a skipped machine's Updates tab says in place of the offer. */
export function skippedLine(update: UiWorkspaceUpdate, isAutoUpdateEnabled: boolean): string {
  const version = skippedVersionText(update);
  return isAutoUpdateEnabled
    ? `You skipped ${version} for this workspace. Imbue Studio won't update it automatically until a newer version is available.`
    : `You skipped ${version} for this workspace. Imbue Studio won't remind you about it until a newer version is available.`;
}

/** Whether an update run may be started or scheduled in this machine at all.
 * Wider than `isUpdateOffered` by the patch leg, which the user may still take
 * by hand, and the unknown leg: from unknown the offer is the check itself,
 * which the machine's own agent performs. */
export function isUpdateDispatchable(update: UiWorkspaceUpdate): boolean {
  return (
    update.availability === "OUT_OF_DATE" ||
    update.availability === "SECURITY_UPDATE" ||
    update.availability === "PATCH_AVAILABLE" ||
    update.availability === "UNKNOWN"
  );
}

/** Whether the last run ended in a way the user has to be told about as a
 * failure. Not NEEDS_DECISION: that run did its job and handed the user a
 * question, which has a state of its own. */
export function isFailureVerdict(verdict: UpdateVerdict | null | undefined): boolean {
  return verdict === "STUCK" || verdict === "REFUSED" || verdict === "NEEDS_RECREATION";
}

/** A template ref as the reader sees it: `minds-v0.8.2` -> `v0.8.2`; any other ref as is. */
export function displayVersion(ref: string): string {
  return ref.replace(/^minds-(?=v\d)/, "");
}

/** The ref a dev build's supported version is reached by inside a workspace, or
 * "" for a released build (`minds-v*` ceiling): a dev build reports the branch
 * it is pinned to, addressed the way the update agent's fetch resolves it. */
export function devBuildTargetRef(supportedVersion: string): string {
  if (!supportedVersion || supportedVersion.startsWith("minds-v")) return "";
  return supportedVersion === "main" ? "main" : `upstream/${supportedVersion}`;
}

/** What the plain "Schedule update" sends, or null when it is not offered
 * because the machine is already scheduled to the version the app supports.
 * With nothing armed it sends no ref, which means that version. Over a schedule
 * for another version it names that version, since a bare press keeps the armed one. */
export function plainScheduleTargetRef(update: UiWorkspaceUpdate): string | null {
  if (!(update.is_scheduled ?? false)) return "";
  const supported = update.supported_version ?? "";
  const named = devBuildTargetRef(supported) || supported;
  const armed = update.override_ref ?? "";
  if (armed === "" || named === "" || armed === supported || armed === named) return null;
  return named;
}

/** Which part of a run a machine is in. Preparing is split from applying
 * because only the apply touches the live machine (its services go down), and
 * finishing from both because the machine is already on the new version. */
export type UpdateRunPhase = "none" | "preparing" | "applying" | "finishing" | "waiting";

/** One producer for the row badge and the titlebar pill. */
export function updateRunPhase(update: UiWorkspaceUpdate | null, isUpdating: boolean): UpdateRunPhase {
  if (!isUpdating) return "none";
  if (update?.activity === "WAITING") return "waiting";
  if (update?.activity === "APPLYING") return "applying";
  if (update?.activity === "FINISHING") return "finishing";
  // STARTING, RUNNING, and a dispatch with no pushed reading yet: none has
  // touched the live machine.
  return "preparing";
}

/** What a machine's last run left behind for the user, once it is over, or
 * that the last one asked for never went out. */
export type UpdateRunOutcome = "none" | "not-started" | "needs-decision" | "failed" | "needs-attention";

/** How the last run ended, for the surfaces that report it. Every failure is
 * one outcome: whatever the agent found, the next step is to check in with it.
 * An update that never went out is newer than any run's outcome: it is the
 * answer to the last press. */
export function updateRunOutcome(update: UiWorkspaceUpdate | null): UpdateRunOutcome {
  if (update === null) return "none";
  if (update.dispatch_failure) return "not-started";
  if (update.is_decision_pending ?? false) return "needs-decision";
  if (update.activity === "STALLED" || isFailureVerdict(update.verdict)) return "failed";
  // Not a failure, but detection resets the machine to UP_TO_DATE on its next
  // sweep, so this is the only account of the leftover work.
  if (update.verdict === "UPDATED_WITH_REBUILD_ITEMS") return "needs-attention";
  return "none";
}

/** Why the last update asked for never went out, for a surface to show in
 * place of its own answer to a press, which is lost if the surface closed
 * while it waited. Empty while a new press is out: that press is the newer
 * question, and the old answer beside its spinner would read as its reply. */
export function recordedDispatchFailure(
  update: UiWorkspaceUpdate,
  isUpdating: boolean,
): { message: string; detail: string } {
  if (isUpdating) return { message: "", detail: "" };
  return { message: update.dispatch_failure ?? "", detail: update.dispatch_failure_detail ?? "" };
}

/** The Updates tab's line about a run in flight, and whether it is still waiting. */
export interface UpdateActivityNotice {
  message: string;
  /** Draws a spinner: the run is between claimed and actually started. */
  isWaiting: boolean;
}

const STARTING_NOTICE: UpdateActivityNotice = {
  message: "Starting the update agent in this workspace…",
  isWaiting: true,
};

/** What a machine's run is doing, for the Updates tab opened on one mid-run.
 * `isWaiting` spins through STARTING, which for a stopped machine is a cold
 * boot and during which the chat tab does not exist yet. `isDispatching` covers
 * the beat before the first frame, when the tab has already dropped its buttons. */
export function updateActivityNotice(update: UiWorkspaceUpdate, isDispatching = false): UpdateActivityNotice {
  if (isDispatching && !isRunInFlight(update)) return STARTING_NOTICE;
  // A run with a named target says so: the phase alone would be a different
  // fact from the one the reader asked for.
  const target = update.target_override ?? "";
  const preparing = target ? `Preparing the update to ${target}` : "Preparing the update";
  // Only a chat the app can open is pointed at.
  const isChatKnown = updateChatAgentIdFor(update, isDispatching) !== "";
  switch (update.activity) {
    case "APPLYING":
      return {
        message: `Updating this workspace${target ? ` to ${target}` : ""}. Its services restart while the update lands.`,
        isWaiting: false,
      };
    case "FINISHING":
      return {
        message: `The update${target ? ` to ${target}` : ""} is in place. The update agent is finishing its checks${isChatKnown ? " in the update chat" : ""}.`,
        isWaiting: false,
      };
    case "STARTING":
      return STARTING_NOTICE;
    case "RUNNING":
      return {
        message: isChatKnown ? `${preparing}. The update chat has the live progress.` : `${preparing}.`,
        isWaiting: false,
      };
    case "WAITING": {
      // A recorded hold waits for the reader's go-ahead on the version; the
      // run's own detail line says what it needs, so it leads.
      const detail = update.hold_detail ? `${update.hold_detail} ` : "";
      return {
        message: update.is_hold_recorded
          ? `${detail}The update agent is waiting for your go-ahead${isChatKnown ? " in the update chat" : ""}.`
          : `The update agent is waiting for you${isChatKnown ? " in the update chat" : ""}.`,
        isWaiting: false,
      };
    }
    case "STALLED":
      return {
        message: update.is_chat_gone
          ? "The update's chat was deleted before the update recorded a result."
          : `The update agent is no longer running and never reported a result.${isChatKnown ? " Check in with it in the update chat." : ""}`,
        isWaiting: false,
      };
    default:
      return { message: "", isWaiting: false };
  }
}

/** The one situation the Updates tab explains, ordered by what the reader
 * would act on first. A run in flight and a run's outcome come first, as on
 * the row's badge, even over a machine that cannot be updated in place: the
 * outcome is settled (in its chat, or by reading it in the Updates tab) before anything else is
 * said. A run that landed this app's version outranks the offer too, since
 * detection reads the machine as behind for a while after the apply; one that
 * landed an older version ranks below it, so a newer release is not hidden
 * behind its success. A stopped machine with nothing of its update state shown
 * (`UpdatesStore.isQuietWhileStopped`) outranks them all; `updateSituation`
 * reads the update alone, so it never returns "stopped". */
export type UpdateSituation =
  | "stopped"
  | "in-progress"
  | "not-started"
  | "needs-decision"
  | "failed"
  | "updated-with-note"
  | "needs-recreation"
  | "app-behind"
  | "skipped"
  | "needs-sign-in"
  | "scheduled"
  | "offered"
  | "updated"
  | "unknown"
  | "up-to-date";

export function updateSituation(update: UiWorkspaceUpdate, isUpdating: boolean): UpdateSituation {
  if (isUpdating) return "in-progress";
  const outcome = updateRunOutcome(update);
  if (outcome === "not-started") return "not-started";
  if (outcome === "needs-decision") return "needs-decision";
  if (outcome === "failed") return "failed";
  if (outcome === "needs-attention") return "updated-with-note";
  if (isRecreationRequired(update)) return "needs-recreation";
  if (update.availability === "APP_BEHIND") return "app-behind";
  const landed = update.success_note_version ?? "";
  if (update.verdict === "UPDATED" && landed !== "" && landed === update.supported_version) return "updated";
  if (isUpdateSkipped(update)) return "skipped";
  if (update.needs_sign_in ?? false) return "needs-sign-in";
  if (update.is_scheduled ?? false) return "scheduled";
  if (isUpdateOffered(update) || update.availability === "PATCH_AVAILABLE") return "offered";
  // A landed run only while its note is showing: once dismissed, the machine's version decides
  // what is offered. A run that found nothing to do earns no note, so it stands until dismissed.
  if ((update.verdict === "UPDATED" && landed !== "") || update.verdict === "ALREADY_CURRENT") return "updated";
  if (update.availability === "UNKNOWN") return "unknown";
  return "up-to-date";
}

export type UpdateBadgeState =
  | "updating"
  | "waiting"
  | "failed"
  | "needs-decision"
  | "needs-attention"
  | "needs-recreation"
  | "needs-sign-in"
  | "skipped"
  | "security-update"
  | "out-of-date"
  | "unknown";
export type UpdateBadgeTone = "neutral" | "warn" | "error";

/** The run's chat to open from a surface, or "" for none: a press still on its
 * way has no run of its own yet, and the chat the row names is an earlier run's. */
export function updateChatAgentIdFor(update: UiWorkspaceUpdate | null, isUpdating: boolean): string {
  if (update === null || (isUpdating && !isRunInFlight(update))) return "";
  return update.chat_agent_id ?? "";
}

/** Whether a click on a machine's update badge opens the run's chat instead of
 * the Updates tab, which during a run would only repeat the badge and offer
 * that chat. Not while STARTING: the chat tab does not exist yet. */
export function isBadgeOpeningRunChat(update: UiWorkspaceUpdate | null, isUpdating: boolean): boolean {
  return (
    update !== null &&
    updateSituation(update, isUpdating) === "in-progress" &&
    update.activity !== "STARTING" &&
    updateChatAgentIdFor(update, isUpdating) !== ""
  );
}

/** The machines-list row badge for one machine's update situation. */
export interface UpdateBadge {
  state: UpdateBadgeState;
  tone: UpdateBadgeTone;
  label: string;
  /** Hover text; the Updates tab behind the badge carries the long version. */
  tooltip: string;
  isSpinnerShown: boolean;
}

/** Which badge a machine's row shows, or null. One at most, ordered by what
 * the reader would act on first: a run in flight, a decision or a failed run,
 * the standing "needs recreation", "skipped", "needs sign-in", "security update"
 * or "out of date", then "unknown". Every one opens the Updates tab (hence
 * buttons); only "security update" and "out of date" are offers, the security
 * one drawn as the stronger, and both are withheld for a scheduled machine and,
 * under auto-update, for one auto-update will arm itself. A null `update`
 * (nothing published yet) earns no badge, except for a dispatch this window just made. */
export function updateBadgeFor(
  update: UiWorkspaceUpdate | null,
  isUpdating: boolean,
  isAutoUpdateEnabled = false,
): UpdateBadge | null {
  const phase = updateRunPhase(update, isUpdating);
  // Waiting is the reader's move; a running-looking badge would have them
  // waiting on something that is waiting on them.
  if (phase === "waiting") {
    return {
      state: "waiting",
      tone: "warn",
      label: "Update needs a decision",
      tooltip: update?.is_hold_recorded
        ? "The update is waiting for your go-ahead — open this workspace to continue"
        : updateChatAgentIdFor(update, isUpdating) !== ""
          ? "The update agent has stopped and is waiting in its chat — open this workspace to continue"
          : "The update agent has stopped and is waiting for you — open this workspace's updates to continue",
      isSpinnerShown: false,
    };
  }
  if (phase === "finishing") {
    return {
      state: "updating",
      tone: "neutral",
      label: "Finishing update…",
      tooltip: "The update is in place; the update agent is finishing its checks",
      isSpinnerShown: true,
    };
  }
  if (phase !== "none") {
    const isApplying = phase === "applying";
    return {
      state: "updating",
      tone: "neutral",
      label: isApplying ? "Updating…" : "Preparing update…",
      tooltip: isApplying
        ? "The update is landing; this workspace's services restart while it does"
        : update?.target_override
          ? `An update to ${update.target_override} is being prepared in this workspace`
          : "An update is being prepared in this workspace; the workspace itself is untouched so far",
      isSpinnerShown: true,
    };
  }
  if (update === null) return null;
  const outcome = updateRunOutcome(update);
  if (outcome === "not-started") {
    return {
      state: "failed",
      tone: "error",
      label: "Update didn't start",
      tooltip: update.dispatch_failure ?? "",
      isSpinnerShown: false,
    };
  }
  if (outcome === "needs-decision") {
    return {
      state: "needs-decision",
      tone: "warn",
      label: "Update needs a decision",
      tooltip:
        updateChatAgentIdFor(update, isUpdating) !== ""
          ? "The update can't keep something you built in this workspace — decide in its chat"
          : "The update can't keep something you built in this workspace",
      isSpinnerShown: false,
    };
  }
  if (outcome === "failed") {
    return {
      state: "failed",
      tone: "error",
      label: "Update didn't finish",
      tooltip: "See what happened to this workspace's update",
      isSpinnerShown: false,
    };
  }
  // Not a failure, so not a warning: the update landed. Badged at all only
  // because without one there is no way to reach the tab that carries the note.
  if (outcome === "needs-attention") {
    return {
      state: "needs-attention",
      tone: "neutral",
      label: "Updated, with a note",
      tooltip: "The update landed; its agent left a note for you",
      isSpinnerShown: false,
    };
  }
  if (isRecreationRequired(update)) {
    return {
      state: "needs-recreation",
      tone: "warn",
      label: "Recreate to update",
      tooltip:
        "This workspace is too old to update in place — see how to move your work to a new one",
      isSpinnerShown: false,
    };
  }
  // Neutral: the reader chose this, so it asks nothing of them, but it is still
  // the way to the tab that updates the machine by hand.
  if (isUpdateSkipped(update)) {
    return {
      state: "skipped",
      tone: "neutral",
      label: `Skipped ${skippedVersionText(update)}`,
      tooltip: "You skipped this update — open to update by hand",
      isSpinnerShown: false,
    };
  }
  if (update.needs_sign_in ?? false) {
    return {
      state: "needs-sign-in",
      tone: "warn",
      label: isAutoUpdateEnabled ? "Needs sign-in to auto-update" : "Needs sign-in to update",
      tooltip: "No AI account is signed in to this workspace, so its update can't start",
      isSpinnerShown: false,
    };
  }
  if (isUpdateOffered(update)) {
    // A scheduled update asks nothing of the reader until it runs.
    if (update.is_scheduled ?? false) return null;
    if (isHandledByAutoUpdate(update, isAutoUpdateEnabled)) return null;
    if (isSecurityUpdate(update)) {
      return {
        state: "security-update",
        tone: "error",
        label: "Security update",
        tooltip: "See the update available for this workspace, which includes a critical security fix",
        isSpinnerShown: false,
      };
    }
    return {
      state: "out-of-date",
      tone: "warn",
      label: "Update available",
      tooltip: "See the update available for this workspace",
      isSpinnerShown: false,
    };
  }
  if (update.availability === "UNKNOWN") {
    return {
      state: "unknown",
      tone: "neutral",
      label: "Version unknown",
      // A build with no supported version reads unknown for every machine,
      // including ones whose version it did read.
      tooltip:
        update.unknown_reason === "NO_APP_VERSION"
          ? "This build of Imbue Studio has no released version to compare workspaces against — open to check anyway"
          : "Imbue Studio can't tell which version this workspace is running — open to check",
      isSpinnerShown: false,
    };
  }
  return null;
}

const MINUTES_PER_DAY = 24 * 60;

/** A minute of the day as a clock reads it, e.g. "2:00 AM" or "8:30 PM". */
export function formatClockMinute(minuteOfDay: number): string {
  const hour = Math.floor(minuteOfDay / 60);
  const minute = String(minuteOfDay % 60).padStart(2, "0");
  return `${hour % 12 || 12}:${minute} ${hour < 12 ? "AM" : "PM"}`;
}

const MS_PER_MINUTE = 60 * 1000;
const MS_PER_DAY = MINUTES_PER_DAY * MS_PER_MINUTE;

/** The reader's clock: the time now, and their offset from UTC (minutes east of it) at an instant. */
interface ViewerClock {
  readonly now: Date;
  utcOffsetMinutesAt(instant: Date): number;
}

function localViewerClock(): ViewerClock {
  return { now: new Date(), utcOffsetMinutesAt: (instant) => -instant.getTimezoneOffset() };
}

/** The window an armed schedule runs in, in the reader's local time: the one
 * written into the workspace, which is what its own clock acts on, else
 * `workspaceWindow`, the one its updates start in (its own, else Studio's).
 * Read at the offset of the window open now, else the next to open, which is the
 * one the app took the UTC start from. */
function scheduleWindowText(update: UiWorkspaceUpdate, workspaceWindow: string, clock: ViewerClock): string {
  const startUtc = update.schedule_window_start_minute_utc;
  const length = update.schedule_window_length_minutes;
  if (startUtc == null || length == null) return workspaceWindow;
  const nowMs = clock.now.getTime();
  const todaysOpeningMs = nowMs - (nowMs % MS_PER_DAY) + startUtc * MS_PER_MINUTE;
  const openingMs =
    [todaysOpeningMs - MS_PER_DAY, todaysOpeningMs].find((at) => nowMs < at + length * MS_PER_MINUTE) ??
    todaysOpeningMs + MS_PER_DAY;
  const offset = clock.utcOffsetMinutesAt(new Date(openingMs));
  const start = (((startUtc + offset) % MINUTES_PER_DAY) + MINUTES_PER_DAY) % MINUTES_PER_DAY;
  return `${formatClockMinute(start)}-${formatClockMinute((start + length) % MINUTES_PER_DAY)}`;
}

/** What an armed schedule says, for the machine's Updates tab. Window-relative rather than "tonight": the window is
 * configurable to any hours. One auto-update armed is the app's own doing, so
 * it also says nothing is asked of the reader, unless a sign-in is. */
export function scheduledLine(
  update: UiWorkspaceUpdate,
  workspaceWindow: string,
  isAutoUpdateEnabled = false,
  clock: ViewerClock = localViewerClock(),
): string {
  const override = update.override_ref ?? "";
  const target = override ? ` to ${displayVersion(override)}` : "";
  const windowText = scheduleWindowText(update, workspaceWindow, clock);
  if (!isArmedByAutoUpdate(update, isAutoUpdateEnabled))
    return `Scheduled to update${target} in the next update window (${windowText}).`;
  const line = `Studio will update this workspace${target} in the next update window (${windowText}).`;
  return (update.needs_sign_in ?? false) ? line : `${line} No action is needed.`;
}

/** Why the last window passed without a start, and that the schedule stands;
 * "" when no window was skipped. */
export function scheduleSkipLine(update: UiWorkspaceUpdate): string {
  const reason = update.last_skip_reason ?? "";
  return reason ? `${reason} Studio will try again in the next window.` : "";
}

/** Whether a machine's lifecycle reading says it is stopped or stopping, so
 * nothing can be read from it or done in it until it starts. */
function isStoppedLiveness(liveness: string): boolean {
  return liveness === "STOPPED" || liveness === "STOPPING";
}

/** What a stopped machine still shows of its update state, or null for
 * nothing. The rest can be neither read nor acted on until the machine
 * starts, and the first read after that brings back what was not dismissed. A
 * landing apply restarts the machine's services, and a run this app is
 * starting starts the machine itself, so both are shown whole; what needs
 * nothing from the machine stays: an update that never went out, which only
 * this app holds, and a machine too old to update in place. */
function updateShownWhileStopped(update: UiWorkspaceUpdate): UiWorkspaceUpdate | null {
  if (update.activity === "APPLYING" || update.activity === "STARTING") return update;
  const isRecreation = isRecreationRequired(update);
  if (!update.dispatch_failure && !isRecreation) return null;
  return {
    availability: isRecreation ? "NEEDS_RECREATION" : "UNKNOWN",
    current_version: update.current_version,
    supported_version: update.supported_version,
    activity: "IDLE",
    is_backup_configured: update.is_backup_configured,
    dispatch_failure: update.dispatch_failure,
    dispatch_failure_detail: update.dispatch_failure_detail,
  };
}

/** What auto-update does for one machine: its own setting when it has one, else the app's. */
export interface WorkspaceAutoUpdate {
  /** Whether the background check arms this machine's update itself. */
  isEnabled: boolean;
  /** The local window its scheduled updates start in, e.g. "2:00 AM-5:00 AM". */
  window: string;
  /** Its own setting; null while it follows the app's. */
  override: UiWorkspaceAutoUpdateOverride | null;
}

export class UpdatesStore {
  /** The app-wide local window scheduled updates run in. A machine with its
   * own window says so in `autoUpdateFor`. */
  updateWindow = "";
  /** The app-wide window's local hours, which a custom window starts from. */
  updateWindowHours = { start: 2, end: 5 };
  /** The app-wide auto-update switch. A machine with its own setting says so
   * in `autoUpdateFor`. */
  isAutoUpdateEnabled = false;
  /** Whether a frame has arrived, so `isAutoUpdateEnabled` is the app's word rather than the default. */
  hasReceivedUpdates = false;

  private updateByAgentId = new Map<string, UiWorkspaceUpdate>();
  /** Machines this window has just dispatched for, held until the pushed state
   * shows a run in flight, or at the latest until the request answers. */
  private pendingAgentIds = new Set<string>();
  /** Why the last sign-in ask failed, per machine, so whichever surface the
   * reader goes to next can say so; dropped on the next ask. */
  private signInFailureByAgentId = new Map<string, UpdateActionResult>();

  /** `livenessOf` reads a machine's lifecycle state (STOPPED, RUNNING, ...;
   * "" when unknown), which decides how much of its update state is shown. */
  constructor(private readonly livenessOf: (agentId: string) => string = () => "") {}

  applyUpdatesMessage(message: UiWorkspaceUpdatesMessage): void {
    this.updateByAgentId = new Map(Object.entries(message.updates ?? {}));
    this.updateWindow = message.update_window ?? "";
    this.updateWindowHours = {
      start: message.update_window_start_hour ?? this.updateWindowHours.start,
      end: message.update_window_end_hour ?? this.updateWindowHours.end,
    };
    this.isAutoUpdateEnabled = message.is_auto_update_enabled ?? false;
    this.hasReceivedUpdates = true;
    for (const agentId of [...this.pendingAgentIds]) {
      const pushed = this.updateByAgentId.get(agentId);
      if (pushed !== undefined && isRunInFlight(pushed)) this.pendingAgentIds.delete(agentId);
    }
  }

  /** Reconnect is resync. The optimistic set is kept: a dispatch still waiting
   * for its answer is still true across a blip. */
  reset(): void {
    this.updateByAgentId.clear();
  }

  /** This machine's update state; the all-unknown default when there is none. */
  forAgent(agentId: string): UiWorkspaceUpdate {
    return this.publishedFor(agentId) ?? UNKNOWN_UPDATE;
  }

  /** This machine's update state as the backend published it, or null before
   * it has. The `forAgent` default is a safe value but not a reading: for the
   * first seconds after launch it would badge every machine "Version unknown".
   * A stopped machine shows only what `updateShownWhileStopped` keeps, unless
   * this window has just pressed its update, which starts it. */
  publishedFor(agentId: string): UiWorkspaceUpdate | null {
    const published = this.updateByAgentId.get(agentId) ?? null;
    if (published === null || !this.isHiddenWhileStopped(agentId)) return published;
    return updateShownWhileStopped(published);
  }

  /** Whether this machine is stopped or stopping, so a run there starts it first. */
  isStopped(agentId: string): boolean {
    return isStoppedLiveness(this.livenessOf(agentId));
  }

  /** Whether this machine is stopped with nothing of its update state shown. */
  isQuietWhileStopped(agentId: string): boolean {
    return this.publishedFor(agentId) === null && this.isHiddenWhileStopped(agentId);
  }

  private isHiddenWhileStopped(agentId: string): boolean {
    return !this.pendingAgentIds.has(agentId) && isStoppedLiveness(this.livenessOf(agentId));
  }

  /** Whether the row should read as updating, optimistic dispatch included. */
  isUpdating(agentId: string): boolean {
    return this.pendingAgentIds.has(agentId) || isRunInFlight(this.forAgent(agentId));
  }

  /** What auto-update does for this machine. Read past the stopped filter: a
   * stopped machine still has a setting, and its Updates tab still shows it. */
  autoUpdateFor(agentId: string): WorkspaceAutoUpdate {
    const published = this.updateByAgentId.get(agentId);
    return {
      isEnabled: published?.is_auto_update_enabled ?? this.isAutoUpdateEnabled,
      window: published?.update_window || this.updateWindow,
      override: published?.auto_update_override ?? null,
    };
  }

  /** Whether this machine is mid-apply, so health-driven recovery surfaces defer. */
  isApplying(agentId: string): boolean {
    return this.forAgent(agentId).activity === "APPLYING";
  }

  /** The machines the bulk actions cover: confirmed out of date or below the
   * security floor, with nothing standing in the way of a fresh run, and not
   * left to auto-update by their own setting or the app's. */
  updatableAgentIds(): string[] {
    return [...this.updateByAgentId.keys()]
      .map((agentId) => [agentId, this.forAgent(agentId)] as const)
      .filter(
        ([agentId, update]) =>
          isUpdateOffered(update) &&
          !this.isUpdating(agentId) &&
          !(update.is_decision_pending ?? false) &&
          !isUpdateSkipped(update) &&
          !isHandledByAutoUpdate(update, this.autoUpdateFor(agentId).isEnabled),
      )
      .map(([agentId]) => agentId);
  }

  /** Dispatch a run now. `targetRef` names an exact version/branch/ref instead
   * of the newest supported release; the run's own agent validates it. */
  async updateNow(agentId: string, targetRef = ""): Promise<UpdateActionResult> {
    this.pendingAgentIds.add(agentId);
    m.redraw();
    const result = await postUpdateAction(`/ui/api/updates/${encodeURIComponent(agentId)}/now`, {
      target_ref: targetRef,
    });
    // Dropped whatever the answer: the backend publishes the in-flight row
    // before this request returns, and holding the lock past it can leave the
    // row locked on a run that already finished.
    this.pendingAgentIds.delete(agentId);
    m.redraw();
    return result;
  }

  /** Arm a run for the next update window; `targetRef` as for `updateNow`. */
  async scheduleUpdate(agentId: string, targetRef = ""): Promise<UpdateActionResult> {
    return await postUpdateAction(`/ui/api/updates/${encodeURIComponent(agentId)}/schedule`, {
      target_ref: targetRef,
    });
  }

  async cancelSchedule(agentId: string): Promise<UpdateActionResult> {
    return await postUpdateAction(`/ui/api/updates/${encodeURIComponent(agentId)}/schedule/cancel`, {});
  }

  /** Skip the app's current release for this machine: it cancels any schedule
   * and any decision its last run is waiting on, and the machine is neither
   * updated automatically nor reminded about until a newer release. */
  async skipUpdate(agentId: string): Promise<UpdateActionResult> {
    return await postUpdateAction(`/ui/api/updates/${encodeURIComponent(agentId)}/skip`, {});
  }

  /** Open a new chat in the machine with the AI-provider chooser up. The
   * machine has to be the displayed one, its frame connected, so the caller
   * enters it first. */
  async signIn(agentId: string): Promise<UpdateActionResult> {
    this.signInFailureByAgentId.delete(agentId);
    const result = await postUpdateAction(`/ui/api/updates/${encodeURIComponent(agentId)}/sign-in`, {});
    if (!result.isOk) this.signInFailureByAgentId.set(agentId, result);
    return result;
  }

  /** Why this machine's last sign-in ask failed, or null. */
  signInFailureFor(agentId: string): UpdateActionResult | null {
    return this.signInFailureByAgentId.get(agentId) ?? null;
  }

  /** Give this machine its own auto-update setting, or put it back on the
   * app's with null. The frame that follows carries the result. */
  async setAutoUpdateOverride(
    agentId: string,
    override: UiWorkspaceAutoUpdateOverride | null,
  ): Promise<UpdateActionResult> {
    return await postUpdateAction(`/ui/api/updates/${encodeURIComponent(agentId)}/auto-update`, {
      mode: override?.mode ?? null,
      window_start_hour: override?.window_start_hour ?? null,
      window_end_hour: override?.window_end_hour ?? null,
    });
  }

  /** Clear how the last run ended -- its verdict, or a stall -- or why the last one never went out. */
  async dismissRunOutcome(agentId: string): Promise<UpdateActionResult> {
    return await postUpdateAction(`/ui/api/updates/${encodeURIComponent(agentId)}/dismiss`, {});
  }

  /** Clear the "Updated to X" note (the row's own dismiss). */
  async dismissNote(agentId: string): Promise<UpdateActionResult> {
    return await postUpdateAction(`/ui/api/updates/${encodeURIComponent(agentId)}/note/dismiss`, {});
  }

  /** Clear how the last run ended, and the "Updated to X" note that outcome stands in front of. */
  async dismissOutcome(agentId: string): Promise<UpdateActionResult> {
    const update = this.publishedFor(agentId);
    const result = await this.dismissRunOutcome(agentId);
    const isNoteBehind =
      update !== null && (update.success_note_version ?? "") !== "" && update.verdict !== "ALREADY_CURRENT";
    return result.isOk && isNoteBehind ? await this.dismissNote(agentId) : result;
  }

  /** Dispatch every listed machine with no optimistic lock: a 200 means only
   * "accepted for attempt", the dispatches run in the background, and a
   * machine whose dispatch fails never reaches an in-flight activity, so a lock
   * here would never release. Each row reports its own outcome. */
  async updateAllNow(agentIds: string[]): Promise<UpdateActionResult> {
    return await postUpdateAction("/ui/api/updates/bulk/now", { agent_ids: agentIds });
  }

  async scheduleAllUpdates(agentIds: string[]): Promise<UpdateActionResult> {
    return await postUpdateAction("/ui/api/updates/bulk/schedule", { agent_ids: agentIds });
  }

  /** Whether an update on this machine needs the go-ahead-without-backups
   * confirmation. Only a positive "no backups" asks: an unpublished row is not a reading. */
  needsNoBackupConfirmation(agentId: string): boolean {
    const published = this.publishedFor(agentId);
    return published !== null && published.is_backup_configured === false;
  }
}

export interface UpdateActionResult {
  isOk: boolean;
  error: string;
  /** Verbatim output from the machine, mngr, or the host start, when any had
   * something to say (a config the machine's own mngr will not parse, say).
   * Rendered apart from `error`, which stays a sentence: these are their words,
   * not ours. */
  detail: string;
}

async function postUpdateAction(url: string, body: Record<string, unknown>): Promise<UpdateActionResult> {
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    return { isOk: false, error: "The request failed (network error).", detail: "" };
  }
  if (response.ok) return { isOk: true, error: "", detail: "" };
  const payload = (await response.json().catch(() => null)) as { error?: string; detail?: string } | null;
  return {
    isOk: false,
    error: payload?.error ?? `HTTP ${response.status}`,
    detail: payload?.detail ?? "",
  };
}
