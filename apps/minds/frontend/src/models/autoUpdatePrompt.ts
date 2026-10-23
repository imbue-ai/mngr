// The one-time offer to turn on auto-update, raised over the machines list.
//
// Asked once per install: after the first launch into a new app version, or
// the first time a workspace reads as behind, whichever comes first, and only
// while no answer is recorded. "Not now" is an answer -- the prompt does not
// come back -- and "Turn on auto-updates" turns the setting on as well, and
// saves the update window picked in the prompt.
// Closing it without answering only puts it away for this window's session.

import m from "mithril";
import { electronBridge } from "../electron-bridge";
import type { SettingsOverview } from "./settings";

export type AutoUpdatePromptDecision = "accepted" | "declined";

/** Why the prompt is up: the first launch into a new app version, or a
 * workspace reading as behind. */
export type AutoUpdatePromptTrigger = "app-updated" | "out-of-date";

export interface AutoUpdatePromptInputs {
  /** The recorded answer; null when none has been given. */
  decision: AutoUpdatePromptDecision | null;
  isAutoUpdateEnabled: boolean;
  isFirstLaunchAfterAppUpdate: boolean;
  hasOutOfDateWorkspace: boolean;
}

/** Whether the prompt is due, and for which reason. Nothing is offered while
 * auto-update is on, wherever it was turned on; the Settings toggle records no
 * answer, though, so turning it back off can bring the offer back. */
export function autoUpdatePromptTriggerFor(
  inputs: AutoUpdatePromptInputs,
): AutoUpdatePromptTrigger | null {
  if (inputs.decision !== null || inputs.isAutoUpdateEnabled) return null;
  if (inputs.isFirstLaunchAfterAppUpdate) return "app-updated";
  if (inputs.hasOutOfDateWorkspace) return "out-of-date";
  return null;
}

type FetchLike = typeof fetch;

/** What the prompt reads from the settings payload. */
interface PromptSettings {
  decision: AutoUpdatePromptDecision | null;
  startHour: number;
  endHour: number;
}

export class AutoUpdatePromptModel {
  isBusy = false;
  error = "";
  /** The update window picked in the prompt, starting from the saved one;
   * saved only by turning auto-update on. */
  startHour = 0;
  endHour = 0;

  /** Undefined until the settings payload has been read: an unread answer is
   * not "no answer", and asking before the read would ask a user who said no. */
  private decision: AutoUpdatePromptDecision | null | undefined = undefined;
  private isFirstLaunchAfterAppUpdate = false;
  /** The reason it was first raised for, kept while it is up so a sweep that
   * changes a workspace's reading does not pull it out from under the reader. */
  private shownTrigger: AutoUpdatePromptTrigger | null = null;
  private isPutAway = false;
  private loading: Promise<void> | null = null;

  private readonly fetchImpl: FetchLike;
  private readonly readIsFirstLaunchAfterAppUpdate: () => Promise<boolean>;
  private readonly redraw: () => void;

  constructor(
    fetchImpl: FetchLike = (input, init) => fetch(input, init),
    readIsFirstLaunchAfterAppUpdate: () => Promise<boolean> = () =>
      electronBridge.isFirstLaunchAfterAppUpdate(),
    redraw: () => void = () => m.redraw(),
  ) {
    this.fetchImpl = fetchImpl;
    this.readIsFirstLaunchAfterAppUpdate = readIsFirstLaunchAfterAppUpdate;
    this.redraw = redraw;
  }

  /** Read the recorded answer and whether this is a launch into a new app
   * version, once per window. A failed read leaves the prompt down: an offer
   * that may already have been declined is not worth making blind. The next
   * call (the machines list mounting again) reads again, so a backend that was
   * not up yet costs the window its prompt only until then. */
  load(): Promise<void> {
    this.loading ??= this.readInputs();
    return this.loading;
  }

  private async readInputs(): Promise<void> {
    const [settings, isFirstLaunch] = await Promise.all([
      this.readSettings(),
      this.readIsFirstLaunchAfterAppUpdate().catch((error: unknown) => {
        console.warn(
          "Could not read whether this launch follows an app update",
          error,
        );
        return false;
      }),
    ]);
    if (settings === undefined) {
      this.loading = null;
    } else {
      this.decision = settings.decision;
      this.startHour = settings.startHour;
      this.endHour = settings.endHour;
    }
    this.isFirstLaunchAfterAppUpdate = isFirstLaunch;
    this.redraw();
  }

  private async readSettings(): Promise<PromptSettings | undefined> {
    try {
      const response = await this.fetchImpl("/ui/api/settings", {
        credentials: "same-origin",
      });
      if (!response.ok) {
        console.warn(
          `Could not read the auto-update prompt's answer: HTTP ${response.status}`,
        );
        return undefined;
      }
      const payload = (await response.json()) as Pick<
        SettingsOverview,
        | "auto_update_prompt_decision"
        | "update_window_start_hour"
        | "update_window_end_hour"
      >;
      return {
        decision: payload.auto_update_prompt_decision ?? null,
        startHour: payload.update_window_start_hour,
        endHour: payload.update_window_end_hour,
      };
    } catch (error) {
      console.warn("Could not read the auto-update prompt's answer", error);
      return undefined;
    }
  }

  /** The reason to show the prompt now, or null to keep it down.
   * `isAutoUpdateEnabled` is null until the app has said: offering it before
   * then would ask a user who already turned it on. */
  triggerFor(
    isAutoUpdateEnabled: boolean | null,
    hasOutOfDateWorkspace: boolean,
  ): AutoUpdatePromptTrigger | null {
    if (this.decision === undefined || this.isPutAway || isAutoUpdateEnabled === null) return null;
    const due = autoUpdatePromptTriggerFor({
      decision: this.decision,
      isAutoUpdateEnabled,
      isFirstLaunchAfterAppUpdate: this.isFirstLaunchAfterAppUpdate,
      hasOutOfDateWorkspace,
    });
    if (due === null) {
      // Answered, or turned on elsewhere: nothing is pending any more.
      if (this.decision !== null || isAutoUpdateEnabled)
        this.shownTrigger = null;
      return this.shownTrigger;
    }
    this.shownTrigger ??= due;
    return this.shownTrigger;
  }

  /** Pick the update window that turning auto-update on will save. */
  setWindow(startHour: number, endHour: number): void {
    this.startHour = startHour;
    this.endHour = endHour;
  }

  /** Record the answer, with the picked window when it turns auto-update on.
   * A refused write keeps the prompt up with the reason. */
  async choose(decision: AutoUpdatePromptDecision): Promise<void> {
    this.isBusy = true;
    this.error = "";
    this.redraw();
    try {
      const response = await this.fetchImpl(
        "/ui/api/settings/auto-update-prompt",
        {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(
            decision === "accepted"
              ? {
                  decision,
                  window: {
                    start_hour: this.startHour,
                    end_hour: this.endHour,
                  },
                }
              : { decision },
          ),
        },
      );
      if (response.ok) {
        this.decision = decision;
        this.shownTrigger = null;
      } else {
        const data = (await response.json().catch(() => ({}))) as {
          error?: string;
        };
        this.error =
          data.error ?? `Could not save your choice (HTTP ${response.status}).`;
      }
    } catch {
      this.error = "Could not save your choice (network error).";
    }
    this.isBusy = false;
    this.redraw();
  }

  /** Put the prompt away for this session without answering it. */
  putAway(): void {
    this.isPutAway = true;
    this.shownTrigger = null;
  }
}

/** One copy for the window, so the answer and a put-away survive the
 * machines list being left and come back to. */
export const autoUpdatePrompt = new AutoUpdatePromptModel();
