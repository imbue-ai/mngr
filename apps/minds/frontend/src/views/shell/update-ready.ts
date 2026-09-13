// The downloaded-update state, held where any surface can read it.
//
// Dismissal is per version and per renderer, and is never persisted: the next
// check pushes the status again, so a dismissal that outlived the session would
// hide an update the user never installed.

import { electronBridge, type UpdateStatus } from "../../electron-bridge";

let readyVersion: string | null = null;
let dismissedVersion: string | null = null;
let isRegistered = false;

/**
 * Start listening, once per renderer.
 *
 * `onUpdateStatus` has no unregister, so a second registration would
 * double-handle every push.
 */
export function watchUpdateStatus(onChange: () => void): void {
  if (isRegistered) return;
  isRegistered = true;
  // Seeded as well as pushed. A status is broadcast once, when it changes, and
  // the window is not always listening then -- a download that finishes behind
  // the splash screen would otherwise be announced to nobody and never
  // mentioned again.
  void electronBridge
    .getUpdateState()
    .then((state) => {
      if (state !== null && state.status.type === "update-downloaded" && state.status.version !== undefined) {
        readyVersion = state.status.version;
        onChange();
      }
    })
    .catch((error: unknown) => {
      // Logged, not surfaced: the Settings panel reports this to the user. The
      // console line is what separates "nothing was downloaded" from "the
      // bridge call threw", which an absent card looks identical for.
      console.debug(`[update] Could not read the update state: ${String(error)}`);
    });
  electronBridge.onUpdateStatus((status: UpdateStatus) => {
    // Only a check that reached the feed may withdraw an offer. Neither
    // `checking` nor `error` is news about the artifact, which went to the
    // installer as it landed and installs on the next restart either way.
    if (status.type === "checking" || status.type === "error") return;
    // `version` is optional on the shared status shape; without one, offer
    // nothing rather than a card reading "Minds undefined is ready".
    readyVersion =
      status.type === "update-downloaded" && status.version !== undefined ? status.version : null;
    onChange();
  });
}

/** The version to offer, or null when there is none or it was dismissed. */
export function updateReadyVersion(): string | null {
  if (readyVersion === null || readyVersion === dismissedVersion) return null;
  return readyVersion;
}

export function dismissUpdateReady(): void {
  dismissedVersion = readyVersion;
}

/** Test seam: the module holds renderer-lifetime state that tests must reset. */
export function resetUpdateReadyForTest(): void {
  readyVersion = null;
  dismissedVersion = null;
  isRegistered = false;
}
