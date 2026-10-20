// The one-time offer to turn on auto-update, raised over the machines list.
//
// Asked once per install: after the first launch into a new app version, or
// the first time a workspace reads as behind, whichever comes first, and only
// while no answer is recorded. "Not now" is an answer -- the prompt does not
// come back -- and "Turn on auto-updates" turns the setting on as well.
// Closing it without answering only puts it away for this window's session.

import m from "mithril";
import { electronBridge } from "../electron-bridge";

export type AutoUpdatePromptDecision = "accepted" | "declined";

/** Why the prompt is up, which picks its opening line. */
export type AutoUpdatePromptTrigger = "app-updated" | "out-of-date";

export interface AutoUpdatePromptInputs {
  /** The recorded answer; null when none has been given. */
  decision: AutoUpdatePromptDecision | null;
  isAutoUpdateEnabled: boolean;
  isFirstLaunchAfterAppUpdate: boolean;
  hasOutOfDateWorkspace: boolean;
}

/** Whether the prompt is due, and for which reason. Turning auto-update on
 * from Settings answers it too: there is nothing left to offer. */
export function autoUpdatePromptTriggerFor(
  inputs: AutoUpdatePromptInputs,
): AutoUpdatePromptTrigger | null {
  if (inputs.decision !== null || inputs.isAutoUpdateEnabled) return null;
  if (inputs.isFirstLaunchAfterAppUpdate) return "app-updated";
  if (inputs.hasOutOfDateWorkspace) return "out-of-date";
  return null;
}

type FetchLike = typeof fetch;

export class AutoUpdatePromptModel {
  isBusy = false;
  error = "";

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
   * that may already have been declined is not worth making blind. */
  load(): Promise<void> {
    this.loading ??= this.readInputs();
    return this.loading;
  }

  private async readInputs(): Promise<void> {
    const [decision, isFirstLaunch] = await Promise.all([
      this.readDecision(),
      this.readIsFirstLaunchAfterAppUpdate().catch(() => false),
    ]);
    this.decision = decision;
    this.isFirstLaunchAfterAppUpdate = isFirstLaunch;
    this.redraw();
  }

  private async readDecision(): Promise<
    AutoUpdatePromptDecision | null | undefined
  > {
    try {
      const response = await this.fetchImpl("/ui/api/settings", {
        credentials: "same-origin",
      });
      if (!response.ok) return undefined;
      const payload = (await response.json()) as {
        auto_update_prompt_decision?: AutoUpdatePromptDecision | null;
      };
      return payload.auto_update_prompt_decision ?? null;
    } catch {
      return undefined;
    }
  }

  /** The reason to show the prompt now, or null to keep it down. */
  triggerFor(
    isAutoUpdateEnabled: boolean,
    hasOutOfDateWorkspace: boolean,
  ): AutoUpdatePromptTrigger | null {
    if (this.decision === undefined || this.isPutAway) return null;
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

  /** Record the answer. A refused write keeps the prompt up with the reason. */
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
          body: JSON.stringify({ decision }),
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
