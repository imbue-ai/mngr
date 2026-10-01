// The one-time "skip logging in" offer, over the request popup: the first
// Approve that is about to open a browser for a sign-in first asks whether to
// import the user's Chrome sign-ins, so that browser (and every later one)
// starts out logged in. Whatever the answer, it is not asked again; Settings
// keeps the import for later.

import m from "mithril";
import type { InboxModel } from "../../../models/inbox";
import { Button } from "../../components/Button";
import { Modal } from "../../components/Modal";
import { Notice } from "../../components/Notice";
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
    return [
      m(
        Notice,
        { variant: "error", role: "alert", id: "browser-import-error" },
        outcome.detail,
      ),
      m("div", { class: "flex items-center justify-end gap-2" }, [
        m(
          Button,
          {
            variant: "secondary",
            id: "browser-import-continue",
            onclick: () => model.declineBrowserImport(),
          },
          "Continue to sign in",
        ),
        m(
          Button,
          {
            variant: "primary",
            id: "browser-import-retry",
            onclick: () => void model.acceptBrowserImport(),
          },
          "Try again",
        ),
      ]),
    ];
  }
  return m("div", { class: "flex items-center justify-end gap-2" }, [
    m(
      Button,
      {
        variant: "secondary",
        id: "browser-import-decline",
        onclick: () => model.declineBrowserImport(),
      },
      "Not now",
    ),
    m(
      Button,
      {
        variant: "primary",
        id: "browser-import-accept",
        onclick: () => void model.acceptBrowserImport(),
      },
      "Import from Chrome",
    ),
  ]);
}

export function BrowserImportOffer(): m.Component<BrowserImportOfferAttrs> {
  return {
    view(vnode) {
      const { model } = vnode.attrs;
      return m(
        Modal,
        {
          isOpen: model.isBrowserImportOfferOpen,
          onClose: () => model.dismissBrowserImportOffer(),
        },
        m("div", { id: "browser-import-offer", class: "flex flex-col gap-4" }, [
          m("h2", { class: "type-heading" }, "Want to skip logging in?"),
          m(
            "p",
            { class: "type-body text-secondary" },
            "Imbue Studio can import your cookies from Google Chrome, so the browser window " +
              "that opens for a sign-in is already logged in.",
          ),
          m(
            "p",
            { class: "type-helper text-tertiary" },
            "You can run this later, or again, from Settings.",
          ),
          actions(model),
        ]),
      );
    },
  };
}
