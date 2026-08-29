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

/** Whether the app has positively read this machine as behind: what the badge
 * and band claim, and what "Update all" covers. */
export function isUpdateOffered(update: UiWorkspaceUpdate): boolean {
  return update.availability === "OUT_OF_DATE";
}

/** Whether the app has positively read this machine as too old to update in
 * place: the only way forward is a new machine, migrated into. */
export function isRecreationRequired(update: UiWorkspaceUpdate): boolean {
  return update.availability === "NEEDS_RECREATION";
}

/** The standing condition a surface says about a machine's version, if any.
 * Standing because it will still be true tomorrow, unlike a run's phase or
 * outcome. */
export type StandingUpdateNotice = "none" | "out-of-date" | "needs-recreation";

/** What a surface should say about this machine's version. A run in flight
 * silences the out-of-date line (the run is already being reported);
 * `isUpdating` rather than the pushed activity, so it does not reappear between
 * the press and the first frame. Recreation has no run to be silenced by. */
export function standingUpdateNotice(update: UiWorkspaceUpdate, isUpdating: boolean): StandingUpdateNotice {
  if (isRecreationRequired(update)) return "needs-recreation";
  if (isUpdateOffered(update) && !isUpdating) return "out-of-date";
  return "none";
}

/** Whether an update run may be started in this machine at all. Wider than
 * `isUpdateOffered` by the unknown leg: from unknown the offer is the check
 * itself, which the machine's own agent performs. */
export function isUpdateDispatchable(update: UiWorkspaceUpdate): boolean {
  return update.availability === "OUT_OF_DATE" || update.availability === "UNKNOWN";
}

/** Whether the last run ended in a way the user has to be told about. */
export function isFailureVerdict(verdict: UpdateVerdict | null | undefined): boolean {
  return verdict === "STUCK" || verdict === "REFUSED" || verdict === "NEEDS_RECREATION";
}

/** What to say about a version read from the machine's create-time label, or
 * null. Only a stopped machine earns a note: a running one was asked and its
 * label is the truth; a stopped one could not be asked, so its label may be stale. */
export function labelVersionNote(liveness: string | undefined): string | null {
  if (liveness !== "STOPPED") return null;
  return "Read from when this machine was created — start it to see the version it's actually running.";
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

/** What a machine's last run left behind for the user, once it is over. */
export type UpdateRunOutcome = "none" | "failed" | "needs-attention";

/** How the last run ended, for the surfaces that report it. Every failure is
 * one outcome: whatever the agent found, the next step is to check in with it. */
export function updateRunOutcome(update: UiWorkspaceUpdate | null): UpdateRunOutcome {
  if (update === null) return "none";
  if (update.activity === "STALLED" || isFailureVerdict(update.verdict)) return "failed";
  // Not a failure, but detection resets the machine to UP_TO_DATE on its next
  // sweep, so this is the only account of the leftover work.
  if (update.verdict === "UPDATED_WITH_REBUILD_ITEMS") return "needs-attention";
  return "none";
}

/** The modal's line about a run in flight, and whether it is still waiting. */
export interface UpdateActivityNotice {
  message: string;
  /** Draws a spinner: the run is between claimed and actually started. */
  isWaiting: boolean;
}

const STARTING_NOTICE: UpdateActivityNotice = {
  message: "Starting the update agent in this machine…",
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
        message: `Updating this machine${target ? ` to ${target}` : ""}. Its services restart while the update lands.`,
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
  | "needs-attention"
  | "needs-recreation"
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
 * the reader would act on first: a run in flight, a failed one, the standing
 * "needs recreation" or "out of date", then "unknown", then a bare armed
 * schedule. Every one opens the modal (hence buttons); only "out of date" is an
 * offer. A null `update` (nothing published yet) earns no badge, except for a
 * dispatch this window just made. */
export function updateBadgeFor(update: UiWorkspaceUpdate | null, isUpdating: boolean): UpdateBadge | null {
  const phase = updateRunPhase(update, isUpdating);
  // Waiting is the reader's move; a running-looking badge would have them
  // waiting on something that is waiting on them.
  if (phase === "waiting") {
    return {
      state: "waiting",
      tone: "warn",
      label: "Waiting for you",
      tooltip: update?.is_hold_recorded
        ? "The update is waiting for your decision — open this machine to continue"
        : "The update agent has stopped and is waiting in its chat — open this machine to continue",
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
        ? "The update is landing; this machine's services restart while it does"
        : update?.target_override
          ? `An update to ${update.target_override} is being prepared in this machine`
          : "An update is being prepared in this machine; the machine itself is untouched so far",
      isSpinnerShown: true,
    };
  }
  if (update === null) return null;
  const outcome = updateRunOutcome(update);
  if (outcome === "failed") {
    return {
      state: "failed",
      tone: "error",
      label: "Update failed",
      tooltip: "See what happened to this machine's update",
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
      tooltip: "This machine is too old to update in place — see how to move your work to a new one",
      isSpinnerShown: false,
    };
  }
  if (isUpdateOffered(update)) {
    const isScheduled = update.is_scheduled ?? false;
    return {
      state: isScheduled ? "scheduled" : "out-of-date",
      tone: "warn",
      label: isScheduled ? "Update scheduled" : "Update available",
      tooltip: isScheduled ? "See the update scheduled for this machine" : "See the update available for this machine",
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
          ? "This build of Minds has no released version to compare machines against — open to check anyway"
          : "Minds can't tell which version this machine is running — open to check",
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
      tooltip: "See the update scheduled for this machine",
      isSpinnerShown: false,
    };
  }
  return null;
}

export class UpdatesStore {
  /** The configured local window scheduled updates run in, as the modal shows it. */
  updateWindow = "";

  private updateByAgentId = new Map<string, UiWorkspaceUpdate>();
  /** Machines this window has just dispatched for, held until the pushed state
   * shows a run in flight, or at the latest until the request answers. */
  private pendingAgentIds = new Set<string>();

  applyUpdatesMessage(message: UiWorkspaceUpdatesMessage): void {
    this.updateByAgentId = new Map(Object.entries(message.updates ?? {}));
    this.updateWindow = message.update_window ?? "";
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

  /** Every machine confirmed out of date and not already updating -- what the
   * bulk actions cover. */
  updatableAgentIds(): string[] {
    return [...this.updateByAgentId.entries()]
      .filter(([agentId, update]) => isUpdateOffered(update) && !this.isUpdating(agentId))
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

  /** Clear how the last run ended -- its verdict, or a stall. */
  async dismissRunOutcome(agentId: string): Promise<UpdateActionResult> {
    return await postUpdateAction(`/ui/api/updates/${encodeURIComponent(agentId)}/dismiss`, {});
  }

  /** Clear the "Updated to X" note (the row's own dismiss). */
  async dismissNote(agentId: string): Promise<UpdateActionResult> {
    return await postUpdateAction(`/ui/api/updates/${encodeURIComponent(agentId)}/note/dismiss`, {});
  }

  /** Dispatch every listed machine with no optimistic lock: a 200 means only
   * "accepted for attempt", and a machine the schedule's skip gate passes over
   * never reaches an in-flight activity, so a lock here would never release.
   * Each row locks when the backend publishes STARTING for it. */
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
    return { isOk: false, error: "The request failed (network error)." };
  }
  if (response.ok) return { isOk: true, error: "" };
  const payload = (await response.json().catch(() => null)) as { error?: string } | null;
  return {
    isOk: false,
    error: payload?.error ?? `HTTP ${response.status}`,
  };
}
