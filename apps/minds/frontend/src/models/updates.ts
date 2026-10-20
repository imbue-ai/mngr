// Per-machine template-update state, and the actions that change it. Mirrors
// the channel's `workspace_updates` frame, which every surface reads so they
// cannot disagree about a machine; the one addition is an optimistic pending
// set (as MindLivenessTracker keeps for Start/Stop) so "Update now" locks the
// row before the round trip.

import m from "mithril";
import type { UiWorkspaceUpdate, UiWorkspaceUpdatesMessage, UpdateVerdict } from "../channel/messages";

/** The state for a machine the backend has said nothing about. Never "up to
 * date": silence is not a positive read. */
const UNKNOWN_UPDATE: UiWorkspaceUpdate = {
  availability: "UNKNOWN",
  current_version: "",
  supported_version: "",
  is_version_from_label: false,
  activity: "IDLE",
};

/** Whether a run is live: the row reports it and a second dispatch is refused.
 * WAITING counts: the agent is alive and still owns the workspace. */
export function isRunInFlight(update: UiWorkspaceUpdate): boolean {
  return (
    update.activity === "STARTING" ||
    update.activity === "RUNNING" ||
    update.activity === "WAITING" ||
    update.activity === "APPLYING"
  );
}

/** Whether the app has positively read this machine as a minor release behind:
 * what the badge and band claim, and what "Update all" covers. A machine behind
 * only by a patch is never prompted for. */
export function isUpdateOffered(update: UiWorkspaceUpdate): boolean {
  return update.availability === "OUT_OF_DATE";
}

/** Whether the app has positively read this machine as too old to update in
 * place: the only way forward is a new machine, migrated into. */
export function isRecreationRequired(update: UiWorkspaceUpdate): boolean {
  return update.availability === "NEEDS_RECREATION";
}

/** Whether auto-update will take care of this machine on its own, so the
 * surfaces that offer the update stay quiet. A machine the user skipped, one
 * that cannot be armed until an AI account is signed in, or one whose arm the
 * workspace refused, is not handled: it says it is behind as it would without
 * auto-update. One read behind but not armed yet is handled; the next sweep
 * arms it. */
export function isHandledByAutoUpdate(update: UiWorkspaceUpdate, isAutoUpdateEnabled: boolean): boolean {
  return (
    isAutoUpdateEnabled &&
    isUpdateOffered(update) &&
    !(update.needs_sign_in ?? false) &&
    !(update.is_update_skipped ?? false) &&
    !update.dispatch_failure
  );
}

/** The standing condition a surface says about a machine's version, if any.
 * Standing because it will still be true tomorrow, unlike a run's phase or
 * outcome. */
export type StandingUpdateNotice = "none" | "out-of-date" | "needs-sign-in" | "needs-recreation";

/** What a surface should say about this machine's version. A run in flight
 * silences the out-of-date line (the run is already being reported);
 * `isUpdating` rather than the pushed activity, so it does not reappear between
 * the press and the first frame. Recreation has no run to be silenced by. */
export function standingUpdateNotice(
  update: UiWorkspaceUpdate,
  isUpdating: boolean,
  isAutoUpdateEnabled = false,
): StandingUpdateNotice {
  if (isRecreationRequired(update)) return "needs-recreation";
  if (isUpdating) return "none";
  if (update.needs_sign_in ?? false) return "needs-sign-in";
  if (isUpdateOffered(update) && !isHandledByAutoUpdate(update, isAutoUpdateEnabled)) return "out-of-date";
  return "none";
}

/** Whether an update run may be started or scheduled in this machine at all.
 * Wider than `isUpdateOffered` by the patch leg, which the user may still take
 * by hand, and the unknown leg: from unknown the offer is the check itself,
 * which the machine's own agent performs. */
export function isUpdateDispatchable(update: UiWorkspaceUpdate): boolean {
  return (
    update.availability === "OUT_OF_DATE" ||
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

/** What to say about a version read from the machine's create-time label, or
 * null. Only a stopped machine earns a note: a running one was asked and its
 * label is the truth; a stopped one could not be asked, so its label may be stale. */
export function labelVersionNote(liveness: string | undefined): string | null {
  if (liveness !== "STOPPED") return null;
  return "Read from when this workspace was created — start it to see the version it's actually running.";
}

/** A template ref as the reader sees it: `minds-v0.8.2` -> `v0.8.2`; any other ref as is. */
export function displayVersion(ref: string): string {
  return ref.replace(/^minds-(?=v\d)/, "");
}

/** What the specific-version field is prefilled with on a dev build, or "". A
 * released build (`minds-v*` ceiling) prefills nothing; a dev build reports the
 * branch it is pinned to, addressed the way the update agent's fetch resolves it. */
export function devOverridePrefill(supportedVersion: string): string {
  if (!supportedVersion || supportedVersion.startsWith("minds-v")) return "";
  return supportedVersion === "main" ? "main" : `upstream/${supportedVersion}`;
}

/** Which part of a run a machine is in. Preparing is split from applying
 * because only the apply touches the live machine (its services go down). */
export type UpdateRunPhase = "none" | "preparing" | "applying" | "waiting";

/** One producer for the row badge and the shell's band. */
export function updateRunPhase(update: UiWorkspaceUpdate | null, isUpdating: boolean): UpdateRunPhase {
  if (!isUpdating) return "none";
  if (update?.activity === "WAITING") return "waiting";
  if (update?.activity === "APPLYING") return "applying";
  // STARTING, RUNNING, and a dispatch with no pushed reading yet: none has
  // touched the live machine.
  return "preparing";
}

/** What a machine's last run left behind for the user, once it is over, or
 * that the last one asked for never went out. */
export type UpdateRunOutcome = "none" | "not-started" | "decision-pending" | "failed" | "needs-attention";

/** How the last run ended, for the surfaces that report it. Every failure is
 * one outcome: whatever the agent found, the next step is to check in with it.
 * An update that never went out is newer than any run's outcome: it is the
 * answer to the last press. */
export function updateRunOutcome(update: UiWorkspaceUpdate | null): UpdateRunOutcome {
  if (update === null) return "none";
  if (update.dispatch_failure) return "not-started";
  if (update.is_decision_pending ?? false) return "decision-pending";
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

/** The modal's line about a run in flight, and whether it is still waiting. */
export interface UpdateActivityNotice {
  message: string;
  /** Draws a spinner: the run is between claimed and actually started. */
  isWaiting: boolean;
}

const STARTING_NOTICE: UpdateActivityNotice = {
  message: "Starting the update agent in this workspace…",
  isWaiting: true,
};

/** What a machine's run is doing, for the modal opened on one mid-run.
 * `isWaiting` spins through STARTING, which for a stopped machine is a cold
 * boot and during which the chat tab does not exist yet. `isDispatching` covers
 * the beat before the first frame, when the modal has already dropped its buttons. */
export function updateActivityNotice(update: UiWorkspaceUpdate, isDispatching = false): UpdateActivityNotice {
  if (isDispatching && !isRunInFlight(update)) return STARTING_NOTICE;
  // A run with a named target says so: the phase alone would be a different
  // fact from the one the reader asked for.
  const target = update.target_override ?? "";
  const preparing = target ? `Preparing the update to ${target}` : "Preparing the update";
  switch (update.activity) {
    case "APPLYING":
      return {
        message: `Updating this workspace${target ? ` to ${target}` : ""}. Its services restart while the update lands.`,
        isWaiting: false,
      };
    case "STARTING":
      return STARTING_NOTICE;
    case "RUNNING":
      return {
        message: update.chat_agent_name
          ? `${preparing}. Open the ${update.chat_agent_name} tab inside the workspace to see the progress.`
          : `${preparing}. The update agent's chat tab has the live progress.`,
        isWaiting: false,
      };
    case "WAITING": {
      const where = update.chat_agent_name
        ? `in the ${update.chat_agent_name} tab inside the workspace`
        : "in its chat tab inside the workspace";
      // A recorded hold is about something the reader built; the run's own
      // detail line names it, so it leads.
      const detail = update.hold_detail ? `${update.hold_detail} ` : "";
      return {
        message: update.is_hold_recorded
          ? `${detail}The update agent is waiting for your decision ${where}.`
          : `The update agent is waiting for you ${where}.`,
        isWaiting: false,
      };
    }
    case "STALLED":
      return {
        message: update.chat_agent_name
          ? `The update agent is no longer running and never reported a result. Check in with it in the ${update.chat_agent_name} tab inside the workspace.`
          : "The update agent is no longer running and never reported a result. Check in with it in its chat tab inside the workspace.",
        isWaiting: false,
      };
    default:
      return { message: "", isWaiting: false };
  }
}

export type UpdateBadgeState =
  | "updating"
  | "waiting"
  | "failed"
  | "decision-pending"
  | "needs-attention"
  | "needs-recreation"
  | "needs-sign-in"
  | "scheduled"
  | "out-of-date"
  | "unknown";
export type UpdateBadgeTone = "neutral" | "warn" | "error";

/** The machines-list row badge for one machine's update situation. */
export interface UpdateBadge {
  state: UpdateBadgeState;
  tone: UpdateBadgeTone;
  label: string;
  /** Hover text; the modal behind the badge carries the long version. */
  tooltip: string;
  isSpinnerShown: boolean;
}

/** Which badge a machine's row shows, or null. One at most, ordered by what
 * the reader would act on first: a run in flight, a decision or a failed run,
 * the standing "needs recreation", "needs sign-in", or "out of date", then
 * "unknown", then a bare armed schedule. Every one opens the modal (hence
 * buttons); only "out of date" is an offer, and under auto-update it is
 * withheld for a machine auto-update will arm itself. A null `update` (nothing
 * published yet) earns no badge, except for a dispatch this window just made. */
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
      label: "Waiting for you",
      tooltip: update?.is_hold_recorded
        ? "The update is waiting for your decision — open this workspace to continue"
        : "The update agent has stopped and is waiting in its chat — open this workspace to continue",
      isSpinnerShown: false,
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
  if (outcome === "decision-pending") {
    return {
      state: "decision-pending",
      tone: "warn",
      label: "Needs your decision",
      tooltip: "The update can't keep something you built in this workspace — decide in its chat",
      isSpinnerShown: false,
    };
  }
  if (outcome === "failed") {
    return {
      state: "failed",
      tone: "error",
      label: "Update failed",
      tooltip: "See what happened to this workspace's update",
      isSpinnerShown: false,
    };
  }
  // Not a failure, so not a warning: the update landed. Badged at all only
  // because without one there is no way to reach the modal that carries the note.
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
      tooltip: "This workspace is too old to update in place — see how to move your work to a new one",
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
    if (update.is_scheduled ?? false) {
      // Under auto-update an armed row asks nothing of the reader.
      return {
        state: "scheduled",
        tone: isAutoUpdateEnabled ? "neutral" : "warn",
        label: "Update scheduled",
        tooltip: "See the update scheduled for this workspace",
        isSpinnerShown: false,
      };
    }
    if (isHandledByAutoUpdate(update, isAutoUpdateEnabled)) return null;
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
  // A version the user named is armed against machines this app reads as up to
  // date, and such an intent is kept rather than dropped, so without this the
  // row would say nothing until the window took the machine's services down.
  // Neutral, not the offered path's warn: nothing about this machine is behind.
  if (update.is_scheduled) {
    return {
      state: "scheduled",
      tone: "neutral",
      label: "Update scheduled",
      tooltip: "See the update scheduled for this workspace",
      isSpinnerShown: false,
    };
  }
  return null;
}

/** The row's one shortcut beside its badge, or null: the run's chat for an
 * outcome the user settles there (a decision, a failure), or the sign-in a
 * refused update is waiting on. A run that never went out has no chat. */
export type RowUpdateShortcut = "open-chat" | "sign-in" | null;

export function rowUpdateShortcutFor(update: UiWorkspaceUpdate | null, badge: UpdateBadge | null): RowUpdateShortcut {
  if (update === null || badge === null) return null;
  if (badge.state === "needs-sign-in") return "sign-in";
  const isSettledInChat =
    badge.state === "decision-pending" || (badge.state === "failed" && updateRunOutcome(update) === "failed");
  return isSettledInChat && (update.chat_agent_id ?? "") !== "" ? "open-chat" : null;
}

function localTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/** The window an armed schedule runs in: the one written into the workspace,
 * which is what its own clock acts on, else the app's. Its zone is named only
 * when it is not the reader's own. */
function scheduleWindowText(update: UiWorkspaceUpdate, appWindow: string, viewerZone: string): string {
  const windowText = update.schedule_window || appWindow;
  const zone = update.schedule_zone ?? "";
  return zone !== "" && zone !== viewerZone ? `${windowText}, ${zone} time` : windowText;
}

/** What an armed schedule says, for the modal and the machine's Updates
 * settings group. Window-relative rather than "tonight": the window is
 * configurable to any hours. Under auto-update the schedule is the app's own
 * doing, so it also says nothing is asked of the reader, unless a sign-in is. */
export function scheduledLine(
  update: UiWorkspaceUpdate,
  appWindow: string,
  isAutoUpdateEnabled = false,
  viewerZone: string = localTimeZone(),
): string {
  const override = update.override_ref ?? "";
  const target = override ? ` to ${displayVersion(override)}` : "";
  const windowText = scheduleWindowText(update, appWindow, viewerZone);
  if (!isAutoUpdateEnabled) return `Scheduled to update${target} in the next update window (${windowText}).`;
  const line = `Studio will update this workspace${target} in the next update window (${windowText}).`;
  return (update.needs_sign_in ?? false) ? line : `${line} No action is needed.`;
}

/** Why the last window passed without a start, and that the schedule stands;
 * "" when no window was skipped. */
export function scheduleSkipLine(update: UiWorkspaceUpdate): string {
  const reason = update.last_skip_reason ?? "";
  return reason ? `${reason} Studio will try again in the next window.` : "";
}

export class UpdatesStore {
  /** The configured local window scheduled updates run in, as the modal shows it. */
  updateWindow = "";
  /** Whether the app arms every behind machine's update itself. */
  isAutoUpdateEnabled = false;

  private updateByAgentId = new Map<string, UiWorkspaceUpdate>();
  /** Machines this window has just dispatched for, held until the pushed state
   * shows a run in flight, or at the latest until the request answers. */
  private pendingAgentIds = new Set<string>();
  /** Why the last sign-in ask failed, per machine, so whichever surface the
   * reader goes to next can say so; dropped on the next ask. */
  private signInFailureByAgentId = new Map<string, UpdateActionResult>();

  applyUpdatesMessage(message: UiWorkspaceUpdatesMessage): void {
    this.updateByAgentId = new Map(Object.entries(message.updates ?? {}));
    this.updateWindow = message.update_window ?? "";
    this.isAutoUpdateEnabled = message.is_auto_update_enabled ?? false;
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
    return this.updateByAgentId.get(agentId) ?? UNKNOWN_UPDATE;
  }

  /** This machine's update state as the backend published it, or null before
   * it has. The `forAgent` default is a safe value but not a reading: for the
   * first seconds after launch it would badge every machine "Version unknown". */
  publishedFor(agentId: string): UiWorkspaceUpdate | null {
    return this.updateByAgentId.get(agentId) ?? null;
  }

  /** Whether the row should read as updating, optimistic dispatch included. */
  isUpdating(agentId: string): boolean {
    return this.pendingAgentIds.has(agentId) || isRunInFlight(this.forAgent(agentId));
  }

  /** Whether this machine is mid-apply, so health-driven recovery surfaces defer. */
  isApplying(agentId: string): boolean {
    return this.forAgent(agentId).activity === "APPLYING";
  }

  /** Every machine confirmed out of date, not already updating, and not waiting
   * on the user's decision about its last run -- what the bulk actions cover. */
  updatableAgentIds(): string[] {
    return [...this.updateByAgentId.entries()]
      .filter(
        ([agentId, update]) =>
          isUpdateOffered(update) && !this.isUpdating(agentId) && !(update.is_decision_pending ?? false),
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

  /** Skip the release auto-update armed: it cancels the schedule, and the
   * sweep leaves the machine alone until a newer release. */
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

  /** Clear how the last run ended -- its verdict, or a stall -- or why the last one never went out. */
  async dismissRunOutcome(agentId: string): Promise<UpdateActionResult> {
    return await postUpdateAction(`/ui/api/updates/${encodeURIComponent(agentId)}/dismiss`, {});
  }

  /** Clear the "Updated to X" note (the row's own dismiss). */
  async dismissNote(agentId: string): Promise<UpdateActionResult> {
    return await postUpdateAction(`/ui/api/updates/${encodeURIComponent(agentId)}/note/dismiss`, {});
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
