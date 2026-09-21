// The downloaded-update state, held where any surface can read it.
//
// Dismissal is per version and per renderer, and is never persisted: the next
// check pushes the status again, so a dismissal that outlived the session would
// hide an update the user never installed.

import {
  electronBridge,
  type UpdateInstallPolicy,
  type UpdateState,
  type UpdateStatus,
} from "../../electron-bridge";

/** How the offer gets applied, which decides what the card promises. */
export interface UpdateInstallTerms {
  policy: UpdateInstallPolicy;
  needsPassword: boolean;
}

// The macOS policy, and what a state that names none means: the browser
// build and partial test stubs report no terms at all.
const INSTALL_ON_QUIT: UpdateInstallTerms = { policy: "on-quit", needsPassword: false };

/** The terms a main-process state reports, with silence meaning on-quit. */
export function installTermsOf(state: UpdateState): UpdateInstallTerms {
  return {
    policy: state.installPolicy ?? INSTALL_ON_QUIT.policy,
    needsPassword: state.needsPasswordToInstall ?? INSTALL_ON_QUIT.needsPassword,
  };
}

let readyVersion: string | null = null;
let dismissedVersion: string | null = null;
let installTerms: UpdateInstallTerms = INSTALL_ON_QUIT;
let installError: string | null = null;
let isInstalling = false;
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
  // mentioned again. The install terms ride the same read: they are a fact
  // about the running binary, so once is enough.
  void electronBridge
    .getUpdateState()
    .then((state) => {
      if (state === null) return;
      installTerms = installTermsOf(state);
      if (state.status.type === "update-downloaded" && state.status.version !== undefined) {
        readyVersion = state.status.version;
      }
      onChange();
    })
    .catch((error: unknown) => {
      // Logged, not surfaced: the Settings panel reports this to the user. The
      // console line is what separates "nothing was downloaded" from "the
      // bridge call threw", which an absent card looks identical for.
      console.debug(`[update] Could not read the update state: ${String(error)}`);
    });
  electronBridge.onUpdateStatus((status: UpdateStatus) => {
    // Only a check that reached the feed may withdraw an offer. Neither
    // `checking` nor `error` is news about the downloaded artifact, which
    // stays staged either way: handed to the installer for the next restart
    // under on-quit, waiting for the install control under on-request.
    if (status.type === "checking" || status.type === "error") return;
    // `version` is optional on the shared status shape; without one, offer
    // nothing rather than a card reading "Mind undefined is ready".
    const offeredVersion =
      status.type === "update-downloaded" && status.version !== undefined ? status.version : null;
    // A failed install described the version that was offered before; a
    // different offer starts with no history.
    if (offeredVersion !== readyVersion) installError = null;
    readyVersion = offeredVersion;
    onChange();
  });
}

/** The version to offer, or null when there is none or it was dismissed. */
export function updateReadyVersion(): string | null {
  if (readyVersion === null || readyVersion === dismissedVersion) return null;
  return readyVersion;
}

/** How the offered update gets installed, as the main process reported it. */
export function updateInstallTerms(): UpdateInstallTerms {
  return installTerms;
}

/**
 * Install the offered update from the card.
 *
 * The installing state is set before the main process is asked: on a .deb the
 * install runs the package tool synchronously and blocks the main process for
 * its whole duration, so nothing pushed from there could reach the window in
 * time, and without this the card keeps offering a live button for as long as
 * the install takes. The state ends one of three ways: the app quits into the
 * update (nothing left to render); the install fails -- a cancelled password
 * prompt, dpkg refusing -- and the error line takes over with the button live
 * again; or the quit after the install is cancelled at the running-workspaces
 * prompt, which the main process reports by resolving the call, and the card
 * goes back to offering the (now installed) update, whose next click quits
 * into it.
 */
export async function installUpdateReady(onChange: () => void): Promise<void> {
  installError = null;
  isInstalling = true;
  onChange();
  try {
    await electronBridge.installUpdate();
  } catch (error: unknown) {
    installError = error instanceof Error ? error.message : String(error);
  }
  isInstalling = false;
  onChange();
}

/** Whether an install from the card is under way (the app has not quit yet). */
export function isUpdateInstalling(): boolean {
  return isInstalling;
}

/** Why the last install from the card did not go through, or null. */
export function updateInstallError(): string | null {
  return installError;
}

export function dismissUpdateReady(): void {
  dismissedVersion = readyVersion;
}

/** Test seam: the module holds renderer-lifetime state that tests must reset. */
export function resetUpdateReadyForTest(): void {
  readyVersion = null;
  dismissedVersion = null;
  installTerms = INSTALL_ON_QUIT;
  installError = null;
  isInstalling = false;
  isRegistered = false;
}
