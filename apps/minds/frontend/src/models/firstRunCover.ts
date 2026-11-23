// The cover over a first-run workspace while it loads (views/shell/FirstRunCover.ts): the creation page's
// welcome, carried on by the shell once the page's route gives way to the workspace's. Continue is offered only
// when the workspace has reported its provider ask on screen, so pressing it always lands on that ask at once.

import m from "mithril";

export interface FirstRunCoverState {
  createAttemptId: string;
  /** The workspace under the cover has its provider ask (or, failing that, its chat) on screen. */
  isReady: boolean;
}

export class FirstRunCoverModel {
  state: FirstRunCoverState | null = null;
  private readonly redraw: () => void;

  constructor(redraw: () => void = () => m.redraw()) {
    this.redraw = redraw;
  }

  raise(createAttemptId: string): void {
    this.state = { createAttemptId, isReady: false };
    this.redraw();
  }

  /** The workspace is ready to be shown; a report for an attempt the cover is no longer up for is dropped. */
  markReady(createAttemptId: string): void {
    if (this.state?.createAttemptId !== createAttemptId) return;
    this.state.isReady = true;
    this.redraw();
  }

  lower(): void {
    if (this.state === null) return;
    this.state = null;
    this.redraw();
  }
}

export const firstRunCover = new FirstRunCoverModel();
