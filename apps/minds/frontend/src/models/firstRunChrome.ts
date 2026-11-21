// The chrome around a new workspace while its first run's provider page covers it, as the workspace reports
// (minds:first-run-provider-step). While the page is up the chrome stays in the first run's cream rather than the
// workspace's color, so the window reads as one surface with the page. When the page ends, the workspace's color
// grows from the window's center until it covers the window, the chrome takes that color under it, the workspace
// takes its page down under the color, and the color lifts off the workspace.

import m from "mithril";
import { isReducedMotion, wash } from "./wash";
import type { WashOrigin } from "./wash";

/** The class on <body> that holds the chrome in the first run's cream (style.css). */
export const FIRST_RUN_CHROME_CLASS = "first-run-chrome";

/** How long after the cover is whole the color stays, for the workspace's page to come down under it. */
export const PAGE_DOWN_SETTLE_MS = 250;

export interface FirstRunChromeDeps {
  setChromeClass: (isOn: boolean) => void;
  startWash: (
    accent: string,
    origin: WashOrigin,
    viewport: { width: number; height: number },
    onSwap: () => void,
    coverUntil: Promise<void>,
    isInstant: boolean,
  ) => void;
  viewport: () => { width: number; height: number };
  isInstant: () => boolean;
  delay: (ms: number) => Promise<void>;
  redraw: () => void;
}

export class FirstRunChromeModel {
  private heldBy: string | null = null;
  private readonly deps: FirstRunChromeDeps;

  constructor(deps: FirstRunChromeDeps) {
    this.deps = deps;
  }

  /** The workspace whose provider page holds the chrome, or null. */
  get holder(): string | null {
    return this.heldBy;
  }

  /** `agentId`'s provider page is up: the chrome stays cream until it ends or the workspace goes. */
  hold(agentId: string): void {
    this.heldBy = agentId;
    this.deps.setChromeClass(true);
  }

  /** Let the chrome take its own colors again, if `agentId` is what holds it. */
  release(agentId: string): void {
    if (this.heldBy !== agentId) return;
    this.heldBy = null;
    this.deps.setChromeClass(false);
  }

  /**
   * `agentId`'s provider page has ended: wash its `accent` over the window from the center, release the cream
   * while the cover is whole, and lift once the workspace has had `coverMs` and a beat to take its page down.
   * A workspace that never held the chrome gets nothing; one with no known accent is simply released.
   */
  exit(agentId: string, accent: string | null, coverMs: number): void {
    if (this.heldBy !== agentId) return;
    if (accent === null) {
      this.release(agentId);
      return;
    }
    const viewport = this.deps.viewport();
    const isInstant = this.deps.isInstant();
    this.deps.startWash(
      accent,
      { x: viewport.width / 2, y: viewport.height / 2 },
      viewport,
      () => {
        this.release(agentId);
        this.deps.redraw();
      },
      // The workspace takes its page down after coverMs whether or not the color grew, so it stays that long either way.
      this.deps.delay(coverMs + PAGE_DOWN_SETTLE_MS),
      isInstant,
    );
  }
}

export const firstRunChrome = new FirstRunChromeModel({
  setChromeClass: (isOn) => document.body.classList.toggle(FIRST_RUN_CHROME_CLASS, isOn),
  startWash: (accent, origin, viewport, onSwap, coverUntil, isInstant) =>
    wash.start(accent, origin, viewport, onSwap, coverUntil, isInstant),
  viewport: () => ({ width: window.innerWidth, height: window.innerHeight }),
  isInstant: isReducedMotion,
  delay: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  redraw: () => m.redraw(),
});
