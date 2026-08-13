// The Requests inbox: master/detail over the /ui/api inbox routes (the SPA
// twin of the legacy overlay drawer). Plain navigation to /inbox is the
// whole-inbox intent (resolving advances to the next request); arriving with
// ?selected=<id> and no keep_open (auto-open / notification) means resolving
// dismisses back to where the user was.

import m from "mithril";
import type { InboxCard, InboxDetail } from "../../models/inbox";
import { InboxModel, getAttachedRequestsStore } from "../../models/inbox";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";
import { AccountsPermissionDetailView } from "./inbox/AccountsPermissionDetail";
import { FileSharingPermissionDetailView } from "./inbox/FileSharingPermissionDetail";
import { PredefinedPermissionDetailView } from "./inbox/PredefinedPermissionDetail";
import { WorkspacePermissionDetailView } from "./inbox/WorkspacePermissionDetail";

function closeInboxSurface(): void {
  if (window.history.length > 1) window.history.back();
  else m.route.set("/");
}

function detailView(model: InboxModel, detail: InboxDetail): m.Children {
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
      return m("div", { class: "flex flex-col items-center justify-center h-full gap-2 text-center" }, [
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

function cardRow(model: InboxModel, card: InboxCard): m.Children {
  const isSelected = model.selectedId === card.id;
  const isDenying = model.denyingIds.has(card.id);
  return m(
    "div",
    {
      key: card.id,
      class:
        "px-3 py-2.5 cursor-pointer border-l-[3px] hover:bg-fill-hover " +
        (isSelected ? "bg-fill-active " : "border-transparent ") +
        (isDenying ? "opacity-60 pointer-events-none " : ""),
      style: isSelected ? `border-left-color: ${card.accent}` : "",
      "data-request-id": card.id,
      onclick: () => void model.select(card.id),
    },
    [
      m("div", { class: "type-helper uppercase tracking-wide text-tertiary" }, card.kind_label),
      m("div", { class: "type-body font-medium text-primary mt-0.5" }, [
        card.display_name || card.ws_name,
        isDenying ? m("span", { class: "type-helper text-tertiary italic" }, " (denying…)") : null,
      ]),
      card.display_name ? m("div", { class: "type-helper text-tertiary" }, card.ws_name) : null,
    ],
  );
}

function InboxPageComponent(): m.Component {
  let model: InboxModel | null = null;

  return {
    oninit() {
      const selectedParam = m.route.param("selected");
      const keepOpenParam = m.route.param("keep_open");
      const activeModel = new InboxModel({
        onClose: closeInboxSurface,
        redraw: () => m.redraw(),
      });
      // Plain /inbox = the user opened the whole inbox; a targeted arrival
      // (?selected=... from auto-open/notification) dismisses on resolution
      // unless keep_open=1 was carried along.
      activeModel.isKeepOpen = !selectedParam || keepOpenParam === "1";
      model = activeModel;
      void activeModel.loadList().then(() => {
        const initial =
          selectedParam && activeModel.cards.some((card) => card.id === selectedParam)
            ? selectedParam
            : (activeModel.cards[0]?.id ?? null);
        if (initial !== null) void activeModel.select(initial);
      });
    },
    onremove() {
      model = null;
    },
    view() {
      const activeModel = model;
      if (activeModel === null) return null;
      const store = getAttachedRequestsStore();
      if (store !== null && activeModel.isListLoaded) {
        // Every channel `requests` message redraws mithril; reconciling here
        // keeps the open inbox live without its own subscription plumbing.
        void activeModel.refreshIfPendingChanged(store.requestIds);
      }
      const isEmpty =
        activeModel.isListLoaded && activeModel.listErrorMessage === null && activeModel.cards.length === 0;
      return m("div", { class: "flex h-full min-h-0" }, [
        m("div", { class: "flex flex-col w-72 shrink-0 border-r border-default bg-fill-subtle" }, [
          activeModel.listErrorMessage !== null
            ? m("div", { class: "px-3 pt-3" }, m(Notice, { variant: "error" }, activeModel.listErrorMessage))
            : null,
          m(
            "div",
            { class: "flex-1 overflow-y-auto" },
            isEmpty
              ? m("div", { class: "px-4 py-8 text-center type-body text-tertiary" }, "No pending requests.")
              : activeModel.cards.map((card) => cardRow(activeModel, card)),
          ),
          m("div", { class: "border-t border-default px-3 py-2" }, [
            m("label", { class: "type-helper text-secondary cursor-pointer flex items-center gap-1.5" }, [
              m("input", {
                type: "checkbox",
                checked: activeModel.autoOpen,
                onchange: (event: Event) => activeModel.setAutoOpen((event.target as HTMLInputElement).checked),
              }),
              "Auto-open on new request",
            ]),
          ]),
        ]),
        m(
          "div",
          { class: "flex-1 overflow-y-auto p-6" },
          activeModel.isDetailLoading
            ? m("div", { class: "flex justify-center pt-10" }, m(Spinner, { size: "md" }))
            : activeModel.detail !== null
              ? detailView(activeModel, activeModel.detail)
              : isEmpty
                ? null
                : m("p", { class: "type-body text-tertiary" }, "Select a request to review it."),
        ),
      ]);
    },
  };
}

export const InboxPage: m.ComponentTypes = InboxPageComponent;
