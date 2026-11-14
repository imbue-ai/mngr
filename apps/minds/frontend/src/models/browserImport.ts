// Importing the user's Chrome sign-ins into latchkey's browser state, so the
// browser window a permission approval opens for a sign-in is already logged
// in. Two surfaces run it -- the one-time offer the permission dialog makes on
// the first Approve that is about to sign in, and the button in Settings that
// runs it again later -- so the call and its busy/outcome state live here.

import { electronBridge } from "../electron-bridge";
import type { UserFacingError } from "./errors";
import { errorFromResponse, fallbackErrorSentence, readResponseBody } from "./errors";
import type { FetchLike } from "./requestDetailPrefetch";

/** `GET /ui/api/settings/browser-import`. */
export interface BrowserImportStatus {
  /** Whether the permission dialog has already made its one-time offer. */
  is_offered: boolean;
  /** Whether this desktop has a latchkey to import into at all. */
  is_available: boolean;
}

/** `POST /ui/api/settings/browser-import`. */
export interface BrowserImportResult {
  is_success: boolean;
  /** What the user is told about a failed import; empty on success. */
  message: string;
  /** Why the import did not happen, in latchkey's words; empty on success. */
  detail: string;
  /** Latchkey's code for the failure when it gave one; null otherwise, and
   * on success. */
  error_code: string | null;
  /** Whether the fix is granting the app Full Disk Access: the Chrome
   * profile could not be read, and the desktop is a Mac, where that is what
   * denies every application a look at Chrome's data. The backend decides,
   * since it is the one running where latchkey ran. */
  is_full_disk_access_needed: boolean;
  /** Whether this computer has no Chrome profile to import from (Chrome is
   * not installed, or has no default profile), so running the import again
   * cannot help. */
  is_chrome_missing: boolean;
}

/** Latchkey's code for an import the OS refused: the Chrome profile is there,
 * but could not be read. Whether that is a Mac's Full Disk Access or plain
 * file permissions, `is_full_disk_access_needed` says. */
export const CHROME_PROFILE_NOT_READABLE = "CHROME_PROFILE_NOT_READABLE";

/** Whether the outcome is the one the user fixes (a permission) rather than
 * reads about. */
export function isChromeProfileUnreadable(
  outcome: BrowserImportResult | null,
): boolean {
  return (
    outcome !== null &&
    !outcome.is_success &&
    outcome.error_code === CHROME_PROFILE_NOT_READABLE
  );
}

/** Whether running the import again could go differently: not while the OS
 * refuses to read the profile (the permission has to be granted first), nor on
 * a computer with no Chrome profile to import from. */
export function isBrowserImportRetryUseful(outcome: BrowserImportResult): boolean {
  return !isChromeProfileUnreadable(outcome) && !outcome.is_chrome_missing;
}

/** What a failed import says when the server gave no sentence of its own. */
const IMPORT_WHAT_FAILED = "Imbue Studio couldn't import your Chrome cookies";

/** The outcome of a run that never reached latchkey, so carries no code. */
function failedBeforeImport(error: UserFacingError): BrowserImportResult {
  return {
    is_success: false,
    message: error.message,
    detail: error.details,
    error_code: null,
    is_full_disk_access_needed: false,
    is_chrome_missing: false,
  };
}

/** What a successful run says: the backend reports only that it succeeded. */
export const BROWSER_IMPORT_SUCCESS_MESSAGE = "Imported your Chrome cookies.";

export const BROWSER_IMPORT_URL = "/ui/api/settings/browser-import";
export const BROWSER_IMPORT_OFFERED_URL =
  "/ui/api/settings/browser-import/offered";

/** Whether the dialog still owes the user its one-time offer. Any failure to
 * find out reads as "no": the offer is a convenience, and an Approve must
 * never wait on it or be lost to it. */
export async function isBrowserImportOfferDue(
  fetchImpl: FetchLike,
): Promise<boolean> {
  try {
    const response = await fetchImpl(BROWSER_IMPORT_URL, {
      credentials: "same-origin",
    });
    if (!response.ok) return false;
    const status = (await response.json()) as BrowserImportStatus;
    return status.is_available && !status.is_offered;
  } catch {
    return false;
  }
}

/** Record that the offer has been shown, whatever the answer was. Best effort:
 * the worst a lost write does is make the offer once more. */
export async function markBrowserImportOffered(
  fetchImpl: FetchLike,
): Promise<void> {
  try {
    await fetchImpl(BROWSER_IMPORT_OFFERED_URL, {
      method: "POST",
      credentials: "same-origin",
    });
  } catch {
    // Nothing to do: see above.
  }
}

/** One run of the import at a time, with what it came back with. */
export class BrowserImportModel {
  isBusy = false;
  /** The last run's outcome; null before any run, and while one is in flight. */
  outcome: BrowserImportResult | null = null;
  /** Why the last run failed, for the error display; null unless it did. */
  error: UserFacingError | null = null;
  /** Whether the last try to open the Full Disk Access list did not open it,
   * so the surface can say so rather than leave the button looking like it
   * silently did nothing. */
  isFullDiskAccessSettingsOpenFailed = false;
  /** Whether the Full Disk Access list has been opened since the last run, so
   * what is left for the user is to turn the app on there and reopen it. */
  isFullDiskAccessSettingsOpened = false;

  constructor(
    private readonly fetchImpl: FetchLike,
    private readonly redraw: () => void,
  ) {}

  /** Run the import (tens of seconds) and resolve to whether it succeeded.
   * A backend that refuses or cannot be reached lands in `outcome` as a
   * failure with the reason, never as a thrown error. */
  async run(): Promise<boolean> {
    if (this.isBusy) return false;
    this.isBusy = true;
    this.outcome = null;
    this.error = null;
    this.isFullDiskAccessSettingsOpenFailed = false;
    this.isFullDiskAccessSettingsOpened = false;
    this.redraw();
    let outcome: BrowserImportResult;
    try {
      const response = await this.fetchImpl(BROWSER_IMPORT_URL, {
        method: "POST",
        credentials: "same-origin",
      });
      if (response.ok) {
        outcome = (await response.json()) as BrowserImportResult;
        this.error = outcome.is_success
          ? null
          : {
              message:
                outcome.message ||
                fallbackErrorSentence(
                  IMPORT_WHAT_FAILED,
                  { isRetryable: isBrowserImportRetryUseful(outcome) },
                ),
              details: outcome.detail,
            };
      } else {
        // Refused before the import ran: the fallback sentence, with what the
        // refusal said behind See details. Every surface offers the import again.
        const body = await readResponseBody(response);
        this.error = errorFromResponse(IMPORT_WHAT_FAILED, { status: response.status, body });
        outcome = failedBeforeImport(this.error);
      }
    } catch {
      this.error = errorFromResponse(IMPORT_WHAT_FAILED, { status: 0, body: null });
      outcome = failedBeforeImport(this.error);
    }
    this.outcome = outcome;
    this.isBusy = false;
    this.redraw();
    return outcome.is_success;
  }

  /** Open macOS's Full Disk Access list for the user to put the app on. */
  async openFullDiskAccessSettings(): Promise<void> {
    this.isFullDiskAccessSettingsOpenFailed = false;
    const isOpened = await electronBridge.openFullDiskAccessSettings();
    this.isFullDiskAccessSettingsOpenFailed = !isOpened;
    this.isFullDiskAccessSettingsOpened ||= isOpened;
    this.redraw();
  }
}
