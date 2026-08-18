// The request-review popup (/inbox): the only surface a pending permission
// request is reviewed on, and it opens only when the user asks -- the in-chat
// card's "Review & respond" button or a "Waiting on you" row. An app-overlay
// route, so the Shell floats it as a centered card over the surface it was
// opened from -- the live workspace (?workspace=) or the options panel --
// which stays mounted behind it.
//
// Port of the legacy Inbox.jinja popup shell: an eyebrow naming the asking
// machine, and ONE request at a time (never a master/detail list). The request
// it was opened on is the whole of the review: answering it dismisses the page
// (see InboxModel), which lands back on the pane's list or on the machine the
// chat card is in. The card chrome (backdrop, close X, Escape) belongs to the
// Shell.

import m from "mithril";
import { getAppContext } from "../../app-context";
import { InboxModel } from "../../models/inbox";
import { Icon16 } from "../components/Icon";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";
import { requestDetailView } from "./inbox/RequestDetail";

/** The eyebrow: "Permission request for <dot> <machine>". The trailing three
 * parts are dropped while no card matches the selection (a stale id, or the
 * list still in flight), leaving the bare title. */
function eyebrow(model: InboxModel): m.Children {
  const selected = model.cards.find((card) => card.id === model.selectedId) ?? null;
  const { shell } = getAppContext();
  // Only when there IS a Permissions pane to go back to -- opened from the
  // in-chat card there is no menu above this, and a back arrow would promise
  // one. Closing still leaves the window; this is the way back UP.
  const isOpenedFromPermissions = shell.panelRouteBehindOverlay !== null;
  return m(
    "span",
    {
      // Lifted onto the close X's line: the X is pinned 12px from the card's
      // top (DialogCloseButton) while the body starts at its own 20px padding,
      // so without this the line reading the window and the buttons ending it
      // sit 8px apart -- close enough to look like a mistake rather than two
      // rows. The row takes the X's 32px height so the three are one band.
      class: "-mt-2 flex h-8 min-w-0 items-center gap-1.5 pr-8 type-label font-semibold text-primary",
    },
    [
      isOpenedFromPermissions
        ? m(
            "button",
            {
              type: "button",
              id: "request-popup-back",
              "aria-label": "Back to permissions",
              "data-tooltip": "Back to permissions",
              // The same 32px hit area and 20px glyph as the card's close X
              // (DialogCloseButton): they are the two ways out of this window
              // and sit at its two top corners, so one being the smaller
              // control read as the lesser way out.
              class:
                "-ml-2 mr-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md " +
                "text-secondary hover:text-primary hover:bg-fill-hover cursor-pointer",
              onclick: () => shell.returnToPanelBehindOverlay(),
            },
            m(Icon16, { name: "chevron-left", size: "lg" }),
          )
        : null,
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
    // Identified so the Shell can tell the popup has nothing to size to yet:
    // opened over the Permissions panel it holds that window's size rather
    // than shrinking to a spinner and growing again once the request lands.
    //
    // The min-height is for every other way in, where there is no window to
    // hold: a card sized to a spinner alone is barely a strip, so the request
    // landing would blow it open from almost nothing. Roughly the height of a
    // request, so what follows is a small settle rather than an expansion.
    return m(
      "div#request-popup-loading",
      { class: "flex justify-center items-center pt-10 min-h-[220px]" },
      m(Spinner, { size: "md" }),
    );
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
        // The request is answered, so the review is over: back to the pane it
        // was opened from (on its list while other requests are still waiting,
        // on Add connection once none are), or, opened from the chat, back to
        // the machine the card is in.
        onClose: () => {
          if (!shell.returnToPanelAfterRequest()) shell.closeAppOverlay();
        },
        onResolved: (resolved) => shell.notifyRequestResolved(resolved),
        onGone: (requestId) => shell.forgetWaitingRequest(requestId),
        redraw: () => m.redraw(),
      });
      activeModel.markPendingSetSeen(stores.requests.requestIds);
      model = activeModel;
      honoredSelection = requestedSelection();
      // The URL already names the request, so its detail does not wait behind
      // the pending list: the two run together rather than end to end. The
      // list still decides what to show when the URL names nothing.
      if (honoredSelection !== null) void activeModel.select(honoredSelection);
      void activeModel.loadList().then(() => {
        // A second entry point can fire while the list is in flight; whatever
        // it selected wins over this open's request.
        if (activeModel.selectedId !== null) return;
        const initial = honoredSelection ?? activeModel.cards[0]?.id ?? null;
        if (initial !== null) void activeModel.select(initial);
      });
    },
    onremove() {
      model?.dispose();
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
