// The request-review popup (/inbox): the only surface a pending permission
// request is reviewed on, and it opens only when the user asks -- the in-chat
// card's "Review & respond" button or a "Waiting on you" row. An app-overlay
// route, so the Shell floats it as a centered card over the surface it was
// opened from -- the live workspace (?workspace=) or the options panel --
// which stays mounted behind it.
//
// Port of the legacy Inbox.jinja popup shell: an eyebrow naming the asking
// machine, ONE request at a time (never a master/detail list), resolving
// advances to the next pending one, and the popup dismisses itself when none
// remain. The card chrome (backdrop, close X, Escape) belongs to the Shell.

import m from "mithril";
import { getAppContext } from "../../app-context";
import type { InboxDetail } from "../../models/inbox";
import { InboxModel } from "../../models/inbox";
import { Icon16 } from "../components/Icon";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";
import { AccountsPermissionDetailView } from "./inbox/AccountsPermissionDetail";
import { FileSharingPermissionDetailView } from "./inbox/FileSharingPermissionDetail";
import { PredefinedPermissionDetailView } from "./inbox/PredefinedPermissionDetail";
import { WorkspacePermissionDetailView } from "./inbox/WorkspacePermissionDetail";

/** The grant dialog for one request, by kind. */
export function requestDetailView(model: InboxModel, detail: InboxDetail): m.Children {
  switch (detail.kind) {
    case "predefined":
      return m(PredefinedPermissionDetailView, { model, detail });
    case "file_sharing":
      return m(FileSharingPermissionDetailView, { model, detail });
    case "workspace":
      return m(WorkspacePermissionDetailView, { model, detail });
    case "accounts":
      return m(AccountsPermissionDetailView, { model, detail });
    case "unknown_scope":
      return m("div", { class: "flex flex-col gap-3" }, [
        m(
          Notice,
          { variant: "warn" },
          `The requested scope '${detail.scope}' is not in the catalog, so there are no permissions to offer.`,
        ),
        m(
          "button",
          {
            class: "self-start type-body text-secondary underline cursor-pointer",
            onclick: () => model.deny(),
          },
          "Deny this request",
        ),
      ]);
    case "unsupported":
      return m(Notice, { variant: "error" }, detail.message);
    case "unavailable":
      return m("div", { class: "flex flex-col items-center justify-center gap-2 py-8 text-center" }, [
        m("p", { class: "type-heading text-primary" }, "This permission request is no longer available"),
        detail.message ? m("p", { class: "type-body text-tertiary" }, detail.message) : null,
      ]);
    default: {
      const unreachable: never = detail;
      void unreachable;
      return null;
    }
  }
}

/** The eyebrow: "Permission request for <dot> <machine>". The trailing three
 * parts are dropped while no card matches the selection (a stale id, or the
 * list still in flight), leaving the bare title. */
function eyebrow(model: InboxModel): m.Children {
  const selected = model.cards.find((card) => card.id === model.selectedId) ?? null;
  return m(
    "span",
    { class: "flex min-w-0 items-center gap-1.5 pr-8 type-label font-semibold text-primary" },
    [
      m(Icon16, { name: "key", size: "sm", extra: "shrink-0" }),
      m("span", { class: "shrink-0" }, "Permission request"),
      selected === null ? null : m("span", { class: "shrink-0" }, "for"),
      selected === null
        ? null
        : m("span", {
            class: "h-2.5 w-2.5 shrink-0 rounded-full",
            "aria-hidden": "true",
            style: `background-color: ${selected.accent}`,
          }),
      selected === null ? null : m("span", { class: "truncate" }, selected.ws_name),
    ],
  );
}

function detailPane(model: InboxModel): m.Children {
  if (!model.isListLoaded || model.isDetailLoading) {
    return m("div", { class: "flex justify-center pt-10" }, m(Spinner, { size: "md" }));
  }
  if (model.detail !== null) return requestDetailView(model, model.detail);
  // A failed list load already says so above; claiming the queue is empty on
  // top of that would contradict it.
  if (model.listErrorMessage !== null) return null;
  return m("p", { class: "py-8 text-center type-body text-secondary" }, "You're all caught up — no pending requests.");
}

/** The request the URL names, or null for "whatever is pending". A named id is
 * selected even when it is not in the pending list, so a stale link says the
 * request is gone instead of silently swapping in a different one the user
 * never asked to review. */
function requestedSelection(): string | null {
  const selected = m.route.param("selected");
  return typeof selected === "string" && selected !== "" ? selected : null;
}

function InboxPageComponent(): m.Component {
  let model: InboxModel | null = null;
  // The ?selected the popup has already acted on: a second entry point (a
  // Waiting-on-you row clicked while the popup is up) only changes the query,
  // which preserves this component instance, so it has to be noticed here.
  let honoredSelection: string | null = null;

  return {
    oninit() {
      const { shell, stores } = getAppContext();
      const activeModel = new InboxModel({
        onClose: () => shell.closeAppOverlay(),
        onResolved: (resolved) => shell.notifyRequestResolved(resolved),
        redraw: () => m.redraw(),
      });
      activeModel.markPendingSetSeen(stores.requests.requestIds);
      model = activeModel;
      honoredSelection = requestedSelection();
      void activeModel.loadList().then(() => {
        // A second entry point can fire while the list is in flight; whatever
        // it selected wins over this open's request.
        if (activeModel.selectedId !== null) return;
        const initial = honoredSelection ?? activeModel.cards[0]?.id ?? null;
        if (initial !== null) void activeModel.select(initial);
      });
    },
    onremove() {
      model = null;
    },
    view() {
      const activeModel = model;
      if (activeModel === null) return null;
      const selection = requestedSelection();
      if (selection !== null && selection !== honoredSelection) {
        honoredSelection = selection;
        void activeModel.select(selection);
      }
      // Every channel `requests` message redraws mithril, so reconciling from
      // the store here keeps the popup live without its own subscription.
      if (activeModel.isListLoaded)
        void activeModel.refreshIfPendingChanged(getAppContext().stores.requests.requestIds);

      return m("div#request-popup", { class: "flex flex-col gap-3" }, [
        eyebrow(activeModel),
        m("div#request-popup-detail", { class: "min-h-0" }, [
          activeModel.listErrorMessage !== null
            ? m("div", { class: "mb-3" }, m(Notice, { variant: "error" }, activeModel.listErrorMessage))
            : null,
          detailPane(activeModel),
        ]),
      ]);
    },
  };
}

export const InboxPage: m.ComponentTypes = InboxPageComponent;
