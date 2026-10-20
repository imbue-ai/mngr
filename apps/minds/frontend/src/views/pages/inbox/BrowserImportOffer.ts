// The one-time Chrome-cookie offer, over the request popup: the first Approve
// that is about to open a browser for a sign-in first asks whether to import
// the user's Chrome sign-ins, so that browser (and every later one) starts
// out logged in. Going on without importing is the default answer: the
// import copies every cookie Chrome holds, which is more than most sign-ins
// need, so it is for a user who wants it rather than one who clicked the
// prominent button. Whatever the answer, it is not asked again; Settings
// keeps the import for later.

import m from "mithril";
import { isChromeProfileUnreadable } from "../../../models/browserImport";
import type { InboxModel } from "../../../models/inbox";
import { Button } from "../../components/Button";
import { browserImportFailureNotice } from "../../components/UnreadableChromeProfileNotice";
import { Modal } from "../../components/Modal";
import { Spinner } from "../../components/Spinner";

export interface BrowserImportOfferAttrs {
  model: InboxModel;
}

function actions(model: InboxModel): m.Children {
  const { browserImport } = model;
  if (browserImport.isBusy) {
    return m(
      "div",
      {
        id: "browser-import-progress",
        class: "flex items-center gap-2 type-body text-secondary",
      },
      [
        m(Spinner, { size: "sm" }),
        "Importing from Chrome… this can take a moment.",
      ],
    );
  }
  const outcome = browserImport.outcome;
  if (outcome !== null && !outcome.is_success) {
    // The import did not happen; the sign-in the click was for still can.
    // When the OS refused to read the profile, trying again is no use until
    // the permission has been granted (on a Mac, with a relaunch), so the
    // retry is left out.
    const isProfileUnreadable = isChromeProfileUnreadable(outcome);
    return [
      browserImportFailureNotice(browserImport, "browser-import-error"),
      m("div", { class: "flex flex-wrap items-center justify-end gap-2" }, [
        isProfileUnreadable
          ? null
          : m(
              Button,
              {
                variant: "secondary",
                extra: "mr-auto",
                id: "browser-import-retry",
                onclick: () => void model.acceptBrowserImport(),
              },
              "Try again",
            ),
        m(
          Button,
          {
            variant: "primary",
            id: "browser-import-continue",
            onclick: () => model.declineBrowserImport(),
          },
          "Continue to sign in",
        ),
      ]),
    ];
  }
  return m("div", { class: "flex flex-wrap items-center justify-end gap-2" }, [
    m(
      Button,
      {
        variant: "secondary",
        extra: "mr-auto",
        id: "browser-import-accept",
        onclick: () => void model.acceptBrowserImport(),
      },
      "Import from Chrome",
    ),
    m(
      Button,
      {
        variant: "primary",
        id: "browser-import-decline",
        onclick: () => model.declineBrowserImport(),
      },
      "Skip import",
    ),
  ]);
}

export function BrowserImportOffer(): m.Component<BrowserImportOfferAttrs> {
  return {
    view(vnode) {
      const { model } = vnode.attrs;
      const outcome = model.browserImport.outcome;
      // Once the import has failed the question is answered, so the dialog
      // reads as the outcome: the offer's pitch would only crowd the notice.
      const isFailed = outcome !== null && !outcome.is_success;
      return m(
        Modal,
        {
          isOpen: model.isBrowserImportOfferOpen,
          onClose: () => model.dismissBrowserImportOffer(),
        },
        m("div", { id: "browser-import-offer", class: "flex flex-col gap-4" }, [
          m(
            "h2",
            { class: "type-heading" },
            isFailed
              ? "Couldn't import your Chrome cookies"
              : "Import your Chrome cookies?",
          ),
          isFailed
            ? null
            : m(
                "p",
                { class: "type-body text-secondary" },
                "Imbue Studio is about to open a browser window for you to sign in. It can first copy " +
                  "your cookies and site logins from Google Chrome's default profile, so that window " +
                  "(and every later one) is already logged in wherever Chrome is. This is optional: " +
                  "signing in by hand works just as well.",
              ),
          isFailed
            ? null
            : m(
                "p",
                { class: "type-helper text-tertiary" },
                "You can import later, or again, from Settings > Browser cookies.",
              ),
          actions(model),
        ]),
      );
    },
  };
}
