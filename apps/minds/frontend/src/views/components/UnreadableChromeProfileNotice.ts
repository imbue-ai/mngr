// Why a Chrome import failed because the OS would not let the profile be
// read, and the way out. On a Mac the user grants Full Disk Access by hand in
// System Settings and relaunches the app, since no API can ask for it;
// elsewhere the profile's own file permissions are what to fix. Shown in
// place of the generic failure notice wherever the import's outcome is shown;
// `browserImportFailureNotice` makes that choice for every such surface.

import m from "mithril";
import { electronBridge } from "../../electron-bridge";
import {
  type BrowserImportModel,
  type BrowserImportResult,
  isChromeProfileUnreadable,
} from "../../models/browserImport";
import type { ErrorAction } from "../../models/errors";
import { ErrorNotice, relaunchAppActions } from "./ErrorDisplay";
import type { ShownError } from "./variantBesideErrors";

export interface UnreadableChromeProfileNoticeAttrs {
  model: BrowserImportModel;
  outcome: BrowserImportResult;
}

/** Why the last import did not happen, or null when it did (or none ran):
 * what to do about it when the OS refused to read the profile, the error
 * display under `errorNoticeId` otherwise. */
export function browserImportFailureNotice(
  browserImport: BrowserImportModel,
  errorNoticeId: string,
): m.Children {
  const outcome = browserImport.outcome;
  if (outcome === null || outcome.is_success) return null;
  if (isChromeProfileUnreadable(outcome)) {
    return m(UnreadableChromeProfileNotice, { model: browserImport, outcome });
  }
  if (browserImport.error === null) return null;
  // A computer with no Chrome to import from is not a bug to report.
  return m(ErrorNotice, {
    id: errorNoticeId,
    error: browserImport.error,
    isBugReportOffered: !outcome.is_chrome_missing,
  });
}

const FULL_DISK_ACCESS_SUMMARY =
  "Imbue Studio needs Full Disk Access to read Chrome's cookies.";
const FILE_PERMISSIONS_SUMMARY =
  "Imbue Studio could not read Google Chrome's profile.";

/** Which of the two refusals the notice's sentence names. */
function unreadableProfileSummary(outcome: BrowserImportResult): string {
  return outcome.is_full_disk_access_needed
    ? FULL_DISK_ACCESS_SUMMARY
    : FILE_PERMISSIONS_SUMMARY;
}

function fullDiskAccessSteps(model: BrowserImportModel): m.Children {
  return [
    m("p", { class: "m-0 font-semibold" }, FULL_DISK_ACCESS_SUMMARY),
    m(
      "p",
      { class: "mt-1" },
      "macOS protects Google Chrome's data, so the import could not read it. To allow it:",
    ),
    m("ol", { class: "list-decimal pl-5 mt-1 flex flex-col gap-1" }, [
      m("li", "Open System Settings > Privacy & Security > Full Disk Access."),
      m(
        "li",
        "Turn on Imbue Studio. If it is not listed, add it with the + button.",
      ),
      m(
        "li",
        "Quit and reopen Imbue Studio, then run the import again from Settings > Browser cookies.",
      ),
    ]),
    model.isFullDiskAccessSettingsOpenFailed
      ? m(
          "p",
          { class: "type-helper mt-1", role: "status" },
          "Couldn't open System Settings automatically — open it from the Apple menu instead.",
        )
      : null,
  ];
}

function filePermissionsSteps(): m.Children {
  return [
    m("p", { class: "m-0 font-semibold" }, FILE_PERMISSIONS_SUMMARY),
    m(
      "p",
      { class: "mt-1" },
      "The operating system refused to read Chrome's profile data, so its permissions " +
        "need changing. To allow the import:",
    ),
    m("ol", { class: "list-decimal pl-5 mt-1 flex flex-col gap-1" }, [
      m(
        "li",
        "Make Google Chrome's profile folder readable by the account running Imbue Studio " +
          "(on Linux it is usually ~/.config/google-chrome).",
      ),
      m("li", "Run the import again from Settings > Browser cookies."),
    ]),
  ];
}

/** The way to the Full Disk Access list, where the shell can open it. Gone once
 * opening it has failed: the steps then say to open it by hand, and pressing
 * again would fail the same way. */
function openSettingsActions(model: BrowserImportModel): ErrorAction[] {
  if (
    !electronBridge.canOpenFullDiskAccessSettings ||
    model.isFullDiskAccessSettingsOpenFailed
  )
    return [];
  return [
    {
      label: "Open System Settings",
      id: "browser-import-open-full-disk-access",
      onclick: () => void model.openFullDiskAccessSettings(),
    },
  ];
}

/** Open System Settings leads until the list is open, or has to be opened by
 * hand; the grant then applies only once the app is reopened, so Reopen leads
 * and the list stays a press away behind it. */
function fullDiskAccessActions(model: BrowserImportModel): ErrorAction[] {
  const openSettings = openSettingsActions(model);
  const isListOpenedOrManual =
    model.isFullDiskAccessSettingsOpened || openSettings.length === 0;
  if (!isListOpenedOrManual) return openSettings;
  return [...relaunchAppActions(), ...openSettings];
}

/** The failure notice for the last import as the rule beside it weighs it --
 * the sentence it shows and the buttons it leads with -- so a panel's own
 * buttons can step down beside it (see variantBesideErrors). The generic
 * notice draws no buttons of its own. */
export function browserImportFailureShownErrors(
  browserImport: BrowserImportModel,
): ShownError[] {
  const outcome = browserImport.outcome;
  if (outcome === null || outcome.is_success) return [];
  if (isChromeProfileUnreadable(outcome)) {
    return [
      {
        message: unreadableProfileSummary(outcome),
        actions: outcome.is_full_disk_access_needed
          ? fullDiskAccessActions(browserImport)
          : [],
      },
    ];
  }
  return browserImport.error === null
    ? []
    : [{ message: browserImport.error.message }];
}

export function UnreadableChromeProfileNotice(): m.Component<UnreadableChromeProfileNoticeAttrs> {
  return {
    view(vnode) {
      const { model, outcome } = vnode.attrs;
      const isFullDiskAccessNeeded = outcome.is_full_disk_access_needed;
      // The steps are the fix, so there is no Report a bug; latchkey's reason
      // (which names the profile's path) stays behind See details.
      return m(ErrorNotice, {
        id: "browser-import-unreadable-profile",
        tone: "warn",
        error: {
          message: unreadableProfileSummary(outcome),
          details: outcome.detail,
        },
        content: isFullDiskAccessNeeded
          ? fullDiskAccessSteps(model)
          : filePermissionsSteps(),
        isBugReportOffered: false,
        actions: isFullDiskAccessNeeded ? fullDiskAccessActions(model) : [],
      });
    },
  };
}
