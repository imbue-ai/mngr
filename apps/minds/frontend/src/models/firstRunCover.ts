// The cover over a new workspace while it loads (views/shell/FirstRunCover.ts): the creation page's
// welcome, carried on by the shell once the page's route gives way to the workspace's. Once the workspace has
// reported its provider ask on screen, the welcome says the workspace is ready for a beat and then lowers on its
// own, so it gives way straight to that ask.

import m from "mithril";

/** How long "Your workspace is ready!" stands before the cover lowers. */
export const READY_HOLD_MS = 1500;

export interface FirstRunCoverState {
  createAttemptId: string;
  /** The creation page's heading, which the cover carries on. */
  heading: string;
  /** The workspace under the cover has its provider ask (or, failing that, its chat) on screen. */
  isReady: boolean;
}

export class FirstRunCoverModel {
  state: FirstRunCoverState | null = null;
  private readonly redraw: () => void;
  private lowerTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(redraw: () => void = () => m.redraw()) {
    this.redraw = redraw;
  }

  raise(createAttemptId: string, heading: string): void {
    this.clearLowerTimer();
    this.state = { createAttemptId, heading, isReady: false };
    this.redraw();
  }

  /** The workspace is ready to be shown: say so, then lower after READY_HOLD_MS. A report for an attempt the cover
   * is no longer up for is dropped. */
  markReady(createAttemptId: string): void {
    if (this.state?.createAttemptId !== createAttemptId || this.state.isReady) return;
    this.state.isReady = true;
    this.lowerTimer = setTimeout(() => this.lower(), READY_HOLD_MS);
    this.redraw();
  }

  lower(): void {
    this.clearLowerTimer();
    if (this.state === null) return;
    this.state = null;
    this.redraw();
  }

  private clearLowerTimer(): void {
    if (this.lowerTimer === null) return;
    clearTimeout(this.lowerTimer);
    this.lowerTimer = null;
  }
}

export const firstRunCover = new FirstRunCoverModel();
