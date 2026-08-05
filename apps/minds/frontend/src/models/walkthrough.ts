// Creating-page onboarding walkthrough: state machine port of onboarding.js
// (the legacy apps/minds/imbue/minds/desktop_client/static/onboarding.js).
// Views stay thin; every timer-driven behaviour lives here so it is testable
// with vi.useFakeTimers() rather than only reachable through a mounted DOM.
//
// The four small classes below (WalkthroughStepper, ChatTypewriter,
// TipsRotator, CloudWheel) each own one independent timer loop and expose
// plain state a view reads directly -- no DOM manipulation. Each takes an
// optional `redraw` (defaults to m.redraw, which needs a mounted root, so
// tests inject a spy instead -- same convention as MindLivenessTracker in
// models/create.ts).

import m from "mithril";

export const TOTAL_STEPS = 9;
export const LAST_STEP = TOTAL_STEPS;

// How long each step is held before the walkthrough moves on. The
// connections step runs a sequence (approve, then the link forms), so it
// gets longer than the rest.
const STEP_MS = 7000;
const STEP_MS_BY_STEP: Record<number, number> = { 3: 10000, 6: 16000, 8: 9000 };

export function dwellForStep(step: number): number {
  return STEP_MS_BY_STEP[step] ?? STEP_MS;
}

export type GraphicId =
  | "gfx-minds"
  | "gfx-machine"
  | "gfx-chat"
  | "gfx-browser"
  | "gfx-apps"
  | "gfx-connect"
  | "gfx-devices"
  | "gfx-publish"
  | "gfx-tips";

const GRAPHIC_BY_STEP: Record<number, GraphicId> = {
  1: "gfx-minds",
  2: "gfx-machine",
  3: "gfx-chat",
  4: "gfx-browser",
  5: "gfx-apps",
  6: "gfx-connect",
  7: "gfx-devices",
  8: "gfx-publish",
};

/** Which graphic a step shows; the last step (tips) has no illustration. */
export function graphicForStep(step: number): GraphicId {
  return GRAPHIC_BY_STEP[step] ?? "gfx-tips";
}

function defaultRedraw(): () => void {
  return () => m.redraw();
}

// ---- Step advance: auto-advance timer + dot-click navigation ----

/**
 * Owns which step is current and the auto-advance timer. `visitId` bumps on
 * every navigation (auto-advance or a dot click, including re-clicking the
 * current dot) -- views key their per-visit "replay this scene from
 * scratch" content on `${step}-${visitId}` so a fresh child mounts and its
 * CSS animations (declared to fire from the moment their class is present)
 * play from the start, without the reflow-forcing classList dance the
 * legacy DOM version needed to restart an animation on a *persistent* node.
 */
export class WalkthroughStepper {
  step = 1;
  visitId = 0;
  /** The step navigated away from on the most recent goToStep call. */
  arrivedFrom: number | null = null;
  private autoTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly redraw: () => void;

  constructor(redraw?: () => void) {
    this.redraw = redraw ?? defaultRedraw();
  }

  start(): void {
    this.scheduleAutoAdvance();
  }

  stop(): void {
    if (this.autoTimer !== null) clearTimeout(this.autoTimer);
    this.autoTimer = null;
  }

  goToStep(target: number): void {
    const clamped = Math.min(LAST_STEP, Math.max(1, target));
    this.arrivedFrom = this.step;
    this.step = clamped;
    this.visitId += 1;
    this.scheduleAutoAdvance();
    this.redraw();
  }

  private scheduleAutoAdvance(): void {
    if (this.autoTimer !== null) clearTimeout(this.autoTimer);
    this.autoTimer = null;
    if (this.step === LAST_STEP) return;
    this.autoTimer = setTimeout(() => this.goToStep(this.step + 1), dwellForStep(this.step));
  }
}

// ---- Chat step: the request types itself, then tries other things ----
// A CSS typewriter clips a line to a fixed width, which cannot backspace
// through phrases of differing length, so the text is driven here.

const CHAT_PREFIX = "I want ";
// The last option is the one the walkthrough stays on: the next step's
// window opens exactly this dashboard.
const CHAT_OPTIONS = ['a custom "to do" app.', "you to filter my emails.", "to build a dashboard."];
const CHAT_TYPE_MS = 45;
const CHAT_ERASE_MS = 22;
const CHAT_HOLD_MS = 1300;

export class ChatTypewriter {
  text = "";
  private timer: ReturnType<typeof setTimeout> | null = null;
  private running = false;
  private optionIndex = 0;
  private readonly redraw: () => void;

  constructor(redraw?: () => void) {
    this.redraw = redraw ?? defaultRedraw();
  }

  start(): void {
    if (this.running) return;
    this.stop();
    this.running = true;
    this.optionIndex = 0;
    this.text = "";
    let typed = 0;

    const typePrefix = (): void => {
      typed += 1;
      this.text = CHAT_PREFIX.slice(0, typed);
      this.redraw();
      this.timer = setTimeout(typed < CHAT_PREFIX.length ? typePrefix : typeOption, CHAT_TYPE_MS);
    };
    const typeOption = (): void => {
      const option = CHAT_OPTIONS[this.optionIndex];
      const shown = this.text.length - CHAT_PREFIX.length + 1;
      this.text = CHAT_PREFIX + option.slice(0, shown);
      this.redraw();
      if (shown < option.length) {
        this.timer = setTimeout(typeOption, CHAT_TYPE_MS);
        return;
      }
      // The last one stays: it is what the next step is about to show.
      if (this.optionIndex === CHAT_OPTIONS.length - 1) return;
      this.timer = setTimeout(eraseOption, CHAT_HOLD_MS);
    };
    const eraseOption = (): void => {
      if (this.text.length <= CHAT_PREFIX.length) {
        this.optionIndex += 1;
        this.timer = setTimeout(typeOption, CHAT_TYPE_MS);
        return;
      }
      this.text = this.text.slice(0, -1);
      this.redraw();
      this.timer = setTimeout(eraseOption, CHAT_ERASE_MS);
    };

    typePrefix();
  }

  stop(): void {
    this.running = false;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }
}

// ---- Rotating tips (last step) ----
// Tips say what you can do, not where to click: menus and labels move, and a
// tip that names a path goes stale the moment one does.

export const TIPS: readonly string[] = [
  "Tip: you can run several agents at once, each in its own tab.",
  "Tip: you can have agents run in the background, or on a schedule.",
  "Tip: you can share your machine, or a single app on it, with someone else.",
  "Tip: nothing happens without you — you can view and revoke permission at any time.",
  "Tip: you can set up several machines and switch between them.",
  "Tip: your machine can be backed up so your work is safe in case of a crash.",
  "Tip: you can stop a machine you are not using, and start it again later.",
  "Did you know: you can report a bug from inside minds.",
];
// Seven seconds a tip, and the rotation only starts when the tips step comes
// up: running it from page load would leave the first tip part-way through
// its turn by the time anyone saw it, and swap it moments later.
const TIP_MS = 7000;
const TIP_FADE_MS = 250;

export class TipsRotator {
  text: string = TIPS[0];
  opacity: 0 | 1 = 1;
  private index = 0;
  private tickTimer: ReturnType<typeof setInterval> | null = null;
  private fadeTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly redraw: () => void;

  constructor(redraw?: () => void) {
    this.redraw = redraw ?? defaultRedraw();
  }

  start(): void {
    if (this.tickTimer !== null) return;
    this.index = 0;
    this.text = TIPS[0];
    this.opacity = 1;
    this.tickTimer = setInterval(() => {
      this.index = (this.index + 1) % TIPS.length;
      this.opacity = 0;
      this.redraw();
      // The swap is a fade out, a text change, then a fade in.
      this.fadeTimer = setTimeout(() => {
        this.fadeTimer = null;
        this.text = TIPS[this.index];
        this.opacity = 1;
        this.redraw();
      }, TIP_FADE_MS);
    }, TIP_MS);
  }

  /** Leaving mid-fade must cancel the pending swap, or it lands on the next visit. */
  stop(): void {
    if (this.tickTimer !== null) clearInterval(this.tickTimer);
    this.tickTimer = null;
    if (this.fadeTimer !== null) clearTimeout(this.fadeTimer);
    this.fadeTimer = null;
  }
}

// ---- App wheel (apps step + connections step) ----
// Each cloud shows one big icon in the center with a smaller one either
// side, spinning like a wheel: the left icon grows into the center, the
// center shrinks out to the right, the right one fades away, and a fresh
// app fades in on the left. Every item only ever moves forward through the
// positions; a retiring item is frozen at "is-exit" and kept rendered for
// WHEEL_TRANSITION_MS so its fade-out plays before it is dropped.

export interface WheelApp {
  url: string;
  name: string;
}

export interface WheelItem {
  id: number;
  url: string;
  name: string;
  position: number;
}

export const WHEEL_POSITIONS = ["is-enter", "is-left", "is-center", "is-right", "is-exit"] as const;
const WHEEL_LAST_POSITION = WHEEL_POSITIONS.length - 1;
const WHEEL_STEP_MS = 2200;
const WHEEL_TRANSITION_MS = 900;
// The name pops once the incoming icon is most of the way to the center.
const WHEEL_NAME_DELAY_MS = 450;
const WHEEL_CENTER_POSITION = 2;

export class CloudWheel {
  items: WheelItem[] = [];
  centerName = "";
  private retiring: WheelItem[] = [];
  private readonly apps: WheelApp[];
  private appPtr = 0;
  private nextId = 0;
  private tickTimer: ReturnType<typeof setInterval> | null = null;
  private readonly redraw: () => void;

  constructor(apps: WheelApp[], redraw?: () => void) {
    this.apps = apps;
    this.redraw = redraw ?? defaultRedraw();
  }

  /** Active items plus anything still fading out at is-exit; what the view renders. */
  get renderItems(): WheelItem[] {
    return this.retiring.length === 0 ? this.items : [...this.items, ...this.retiring];
  }

  /**
   * (Re)starts the wheel from a clean seed, discarding any items from a
   * prior run -- unlike ChatTypewriter/TipsRotator this class had no
   * self-reset, so calling start() after stop() used to pile new seed items
   * on top of stale ones. A view holds one long-lived instance per wheel and
   * calls start() fresh every time that step's scene comes up, replaying
   * the wheel from scratch on each revisit (matching the other scenes,
   * which replay by remounting).
   */
  start(): void {
    if (this.apps.length === 0) return;
    this.stop();
    this.items = [];
    this.retiring = [];
    this.appPtr = 0;
    this.nextId = 0;
    this.centerName = "";
    [0, 1, 2, 3].forEach((position) => this.addItem(position));
    const seeded = this.items.find((item) => item.position === WHEEL_CENTER_POSITION);
    if (seeded) this.centerName = seeded.name;
    this.redraw();
    this.tickTimer = setInterval(() => this.advance(), WHEEL_STEP_MS);
  }

  stop(): void {
    if (this.tickTimer !== null) clearInterval(this.tickTimer);
    this.tickTimer = null;
  }

  private addItem(position: number): void {
    const app = this.apps[this.appPtr % this.apps.length];
    this.appPtr += 1;
    this.items.push({ id: this.nextId++, url: app.url, name: app.name, position });
  }

  private advance(): void {
    const stillActive: WheelItem[] = [];
    const justRetired: WheelItem[] = [];
    this.items.forEach((item) => {
      item.position += 1;
      (item.position >= WHEEL_LAST_POSITION ? justRetired : stillActive).push(item);
    });
    justRetired.forEach((item) => {
      item.position = WHEEL_LAST_POSITION;
    });
    this.items = stillActive;
    this.retiring.push(...justRetired);
    justRetired.forEach((item) => {
      setTimeout(() => {
        this.retiring = this.retiring.filter((candidate) => candidate.id !== item.id);
        this.redraw();
      }, WHEEL_TRANSITION_MS);
    });
    // Feed the next app in at the entry position; it starts moving on the
    // following tick, so its entry animates like every other move.
    this.addItem(0);

    const arriving = this.items.find((item) => item.position === WHEEL_CENTER_POSITION);
    this.redraw();
    if (arriving) {
      setTimeout(() => {
        this.centerName = arriving.name;
        this.redraw();
      }, WHEEL_NAME_DELAY_MS);
    }
  }
}
