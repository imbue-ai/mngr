// Shared "does this window currently have OS focus" resolution, for anything
// that gates behavior on it.
//
// In the desktop app the answer comes from Electron main, which relays each
// window's own focus and blur events (see setRelayedWindowFocus). The
// renderer's own signal is not enough there: Chromium fires the top-level
// window's focus/blur only while keyboard focus sits in the chrome document,
// so with focus inside the workspace iframe a switch to another app and back
// goes unseen, and document.hasFocus() keeps answering from before the
// switch. In plain-browser mode there is no main process, so the document's
// own answer stands.

/** One window's focus knowledge. The app holds a single instance (the
 * relay speaks for the whole window); tests build their own. */
export class WindowFocusState {
  private relayedFocus: boolean | null = null;

  /** Record the focus state Electron main relayed for this window. */
  record(isFocused: boolean): void {
    this.relayedFocus = isFocused;
  }

  /** Resolve window focus via an injectable override (tests), else the
   * main-process relay once it has spoken, else document.hasFocus().
   * Node-env tests have no document; a missing focus signal must not silence
   * everything downstream, so absence reads as focused. */
  resolve(override: (() => boolean) | undefined): boolean {
    if (override !== undefined) return override();
    if (this.relayedFocus !== null) return this.relayedFocus;
    if (
      typeof document === "undefined" ||
      typeof document.hasFocus !== "function"
    )
      return true;
    return document.hasFocus();
  }
}

// This window's state, behind module functions rather than a threaded
// dependency: there is exactly one window for its readers to ask about.
const windowFocus = new WindowFocusState();

/** Record this window's relayed focus, as :meth:`WindowFocusState.record`. */
export function setRelayedWindowFocus(isFocused: boolean): void {
  windowFocus.record(isFocused);
}

/** This window's focus, resolved as :meth:`WindowFocusState.resolve` describes. */
export function resolveWindowFocus(
  override: (() => boolean) | undefined,
): boolean {
  return windowFocus.resolve(override);
}
