// Why a Chrome import failed because the OS would not let the profile be
// read, and the way out. On a Mac the user grants Full Disk Access by hand in
// System Settings and relaunches the app, since no API can ask for it;
// elsewhere the profile's own file permissions are what to fix. Shown in
// place of latchkey's own sentence wherever the import's outcome is shown;
// `browserImportFailureNotice` makes that choice for every such surface.

import m from "mithril";
import { electronBridge } from "../../electron-bridge";
import {
  type BrowserImportModel,
  type BrowserImportResult,
  isChromeProfileUnreadable,
} from "../../models/browserImport";
import { Button } from "./Button";
import { Notice } from "./Notice";

export interface UnreadableChromeProfileNoticeAttrs {
  model: BrowserImportModel;
  outcome: BrowserImportResult;
}

/** Why the last import did not happen, or null when it did (or none ran):
 * what to do about it when the OS refused to read the profile, latchkey's
 * own sentence under `errorNoticeId` otherwise. */
export function browserImportFailureNotice(
  browserImport: BrowserImportModel,
  errorNoticeId: string,
): m.Children {
  const outcome = browserImport.outcome;
  if (outcome === null || outcome.is_success) return null;
  if (isChromeProfileUnreadable(outcome)) {
    return m(UnreadableChromeProfileNotice, { model: browserImport, outcome });
  }
  return m(
    Notice,
    { variant: "error", role: "alert", id: errorNoticeId },
    outcome.detail,
  );
}

function fullDiskAccessGuidance(model: BrowserImportModel): m.Children {
  return [
    m(
      "p",
      { class: "font-semibold" },
      "Imbue Studio needs Full Disk Access to read Chrome's cookies.",
    ),
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
    electronBridge.canOpenFullDiskAccessSettings
      ? m(
          "div",
          { class: "mt-2" },
          m(
            Button,
            {
              variant: "secondary",
              size: "md",
              id: "browser-import-open-full-disk-access",
              onclick: () => void model.openFullDiskAccessSettings(),
            },
            "Open System Settings",
          ),
        )
      : null,
    model.isFullDiskAccessSettingsOpenFailed
      ? m(
          "p",
          { class: "type-helper mt-1", role: "status" },
          "Couldn't open System Settings automatically — open it from the Apple menu instead.",
        )
      : null,
  ];
}

function filePermissionsGuidance(outcome: BrowserImportResult): m.Children {
  return [
    m(
      "p",
      { class: "font-semibold" },
      "Imbue Studio could not read Google Chrome's profile.",
    ),
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
    m("p", { class: "type-helper mt-2 wrap-anywhere" }, outcome.detail),
  ];
}

export function UnreadableChromeProfileNotice(): m.Component<UnreadableChromeProfileNoticeAttrs> {
  return {
    view(vnode) {
      const { model, outcome } = vnode.attrs;
      return m(
        Notice,
        {
          variant: "warn",
          role: "alert",
          id: "browser-import-unreadable-profile",
        },
        outcome.is_full_disk_access_needed
          ? fullDiskAccessGuidance(model)
          : filePermissionsGuidance(outcome),
      );
    },
  };
}
