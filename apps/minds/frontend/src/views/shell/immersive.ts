// Immersive mode (specs/screen-studio-integration/spec.md, section 5): the
// workspace frame taken Fullscreen-API fullscreen with the keyboard locked, so
// every key reaches the remote desktop shown in it. Only this top-level page
// can do it (`navigator.keyboard.lock()` rejects from any iframe), so the
// workspace asks through the embed contract and the chrome does the three
// things only it can: the fullscreen request, the keyboard lock, and silencing
// its own accelerators and key relays in the desktop app.
//
// Kept free of the DOM behind an injected dependency set so the transitions
// can be asserted over a fake frame and a fake `navigator.keyboard`.

/** What the chrome tells the workspace after every transition. */
export interface ImmersiveChange {
  on: boolean;
  keyboardLocked: boolean;
  error?: string;
}

/** The `navigator.keyboard` surface immersive mode uses; null where the
 * browser has none, which leaves immersive mode on without the lock. */
export interface KeyboardLockSurface {
  lock(): Promise<void>;
  unlock(): void;
}

export interface ImmersiveDeps {
  /** `frame.requestFullscreen()`; must be called synchronously from the
   * message handler so it runs inside the click's transient activation. */
  requestFullscreen: () => Promise<void>;
  /** Whether the document has a fullscreen element right now. */
  isFullscreen: () => boolean;
  /** `document.exitFullscreen()`. */
  exitFullscreen: () => Promise<void>;
  keyboard: KeyboardLockSurface | null;
  /** The desktop app's part (menu shortcuts off, key relays paused); a no-op
   * in a plain browser. */
  setDesktopImmersive: (on: boolean) => void;
  /** Send `minds:immersive-changed` to the frame. */
  send: (change: ImmersiveChange) => void;
}

const MAX_ERROR_LENGTH = 256;

/** A fullscreen refusal as the short string the contract carries. */
export function describeImmersiveError(error: unknown): string {
  const text =
    error instanceof Error
      ? error.message
        ? `${error.name}: ${error.message}`
        : error.name
      : String(error);
  return text.slice(0, MAX_ERROR_LENGTH);
}

/**
 * The immersive-mode state machine for one workspace frame.
 *
 * Entering: `requestFullscreen` and `keyboard.lock()` are both called at once,
 * synchronously, so the fullscreen request keeps the gesture's activation;
 * the fullscreen outcome decides whether immersive mode is on, the lock's
 * whether `keyboardLocked` is true. Leaving happens on `fullscreenchange`
 * with no fullscreen element, whatever ended it (an explicit request, the
 * user holding Escape, the OS, the frame navigating), so there is one exit
 * path and the workspace hears about every one.
 */
export class ImmersiveController {
  private readonly deps: ImmersiveDeps;
  private isOn = false;
  private isEntering = false;
  // Bumped on every transition so a completion of a superseded entry is
  // dropped rather than turning a mode the user already left back on.
  private generation = 0;

  constructor(deps: ImmersiveDeps) {
    this.deps = deps;
  }

  /** Whether immersive mode is on (an entry still in flight does not count). */
  get isImmersive(): boolean {
    return this.isOn;
  }

  /** The workspace's `minds:immersive-request`. */
  request(on: boolean): void {
    if (on) this.enter();
    else this.leave();
  }

  /** The document's `fullscreenchange`: immersive mode ends with fullscreen. */
  fullscreenChanged(): void {
    if (!this.deps.isFullscreen()) this.ended();
  }

  /** The frame is going to another page (another workspace, a reload) or is
   * being unmounted: nothing immersive survives it. */
  frameNavigated(): void {
    if (!this.isOn && !this.isEntering) return;
    if (this.deps.isFullscreen())
      void this.deps.exitFullscreen().catch(() => undefined);
    this.ended();
  }

  private enter(): void {
    if (this.isOn || this.isEntering) return;
    const generation = ++this.generation;
    this.isEntering = true;
    let fullscreen: Promise<void>;
    try {
      fullscreen = this.deps.requestFullscreen();
    } catch (error) {
      this.isEntering = false;
      this.deps.send({
        on: false,
        keyboardLocked: false,
        error: describeImmersiveError(error),
      });
      return;
    }
    // Asked for at once rather than after fullscreen lands: the lock takes
    // effect once fullscreen does, and asking now keeps both inside the
    // gesture. Its refusal (a denied prompt, no API) is not an error: immersive
    // mode stays on without it.
    const keyboard = this.deps.keyboard;
    const locked: Promise<boolean> =
      keyboard === null
        ? Promise.resolve(false)
        : keyboard.lock().then(
            () => true,
            () => false,
          );
    void fullscreen.then(
      async () => {
        const isLocked = await locked;
        if (generation !== this.generation) {
          this.releaseSupersededFullscreen();
          return;
        }
        this.isEntering = false;
        this.isOn = true;
        this.deps.setDesktopImmersive(true);
        this.deps.send({ on: true, keyboardLocked: isLocked });
      },
      (error: unknown) => {
        if (generation !== this.generation) return;
        this.isEntering = false;
        keyboard?.unlock();
        this.deps.send({
          on: false,
          keyboardLocked: false,
          error: describeImmersiveError(error),
        });
      },
    );
  }

  private leave(): void {
    if (!this.isOn && !this.isEntering) {
      // Nothing to leave; the workspace still hears where things stand.
      this.deps.send({ on: false, keyboardLocked: false });
      return;
    }
    if (this.deps.isFullscreen()) {
      // The fullscreenchange this causes is what ends the mode.
      void this.deps.exitFullscreen().catch(() => this.ended());
      return;
    }
    this.ended();
  }

  /** A fullscreen that landed after a leave or a navigation ended its entry
   * belongs to no mode: exit it, unless a newer entry is on or in flight and
   * the document's fullscreen is now that entry's. The exit's own
   * fullscreenchange finds the mode already off and says nothing more. */
  private releaseSupersededFullscreen(): void {
    if (this.isOn || this.isEntering || !this.deps.isFullscreen()) return;
    void this.deps.exitFullscreen().catch(() => undefined);
  }

  private ended(): void {
    if (!this.isOn && !this.isEntering) return;
    this.generation += 1;
    this.isOn = false;
    this.isEntering = false;
    this.deps.keyboard?.unlock();
    this.deps.setDesktopImmersive(false);
    this.deps.send({ on: false, keyboardLocked: false });
  }
}
