// The inbox modal: the left pending-request card list (store-fed from the
// chrome ``requests`` payload's card summaries) plus the pinned auto-open
// footer, the typed right detail pane (per-kind views over the JSON payloads
// from /inbox/detail/<id> -- see InboxDetail.ts), and the drawer chrome
// (header, backdrop, Escape, drag regions).
import m from "mithril";

import type { ChromeRequestCard, InboxBootIsland, InboxDetailPayload } from "../chrome_state";
import { getHost } from "../host";
import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";
import { connect, getRequestCards, isRequestsAutoOpen, seed, subscribe } from "../store";
import { Icon } from "./Icon";
import { InboxDetail, InboxDetailController } from "./InboxDetail";

// The controller behind the card list: selection, the local deny/resolve
// transients, and the detail-pane fetches. Page-scoped state lives here (one
// per mount) rather than in the chrome store -- it dies with the document.
export interface InboxListController {
  getVisibleCards(): ChromeRequestCard[];
  isDenying(id: string): boolean;
  getSelectedId(): string | null;
  isAutoOpenChecked(): boolean;
  selectCard(id: string): void;
  toggleAutoOpen(isEnabled: boolean): void;
}

export function InboxList(): m.Component<{ controller: InboxListController }> {
  // The empty-state layout (centered message, hidden detail pane) is keyed
  // off ``is-empty`` on #inbox-body, computed by the enclosing modal view
  // from the same visible-card set.
  return {
    view(vnode) {
      const { controller } = vnode.attrs;
      const cards = controller.getVisibleCards();
      const selectedId = controller.getSelectedId();
      return [
        m(
          "div",
          { id: "inbox-list", class: "flex-1 overflow-y-auto" },
          cards.length === 0
            ? m("div", { class: "inbox-empty-placeholder" }, "No pending requests.")
            : // No keys: the empty placeholder and the cards alternate as
              // children of the same list, and cards hold no internal DOM
              // state, so positional diffing is correct here.
              cards.map((card) => {
                const isDenying = controller.isDenying(card.id);
                return m(
                  "div",
                  {
                    class:
                      "inbox-card" +
                      (card.id === selectedId ? " is-selected" : "") +
                      (isDenying ? " is-denying" : ""),
                    "data-request-id": card.id,
                    style: { "--workspace-accent": card.accent },
                    // ``is-denying`` also sets pointer-events: none; the guard
                    // covers non-pointer activation paths.
                    onclick: () => {
                      if (!isDenying) controller.selectCard(card.id);
                    },
                  },
                  [
                    m("div", { class: "inbox-card-kind" }, `${card.kind_label}: ${card.ws_name}`),
                    m("div", { class: "inbox-card-display" }, card.display_name),
                  ],
                );
              }),
        ),
        m(
          "div",
          { id: "inbox-list-footer", class: "border-t border-default px-3 py-2 flex items-center" },
          m("label", { class: "type-helper text-secondary cursor-pointer flex items-center gap-1.5" }, [
            m("input", {
              type: "checkbox",
              id: "inbox-auto-open",
              checked: controller.isAutoOpenChecked(),
              onchange: (event: Event) => {
                controller.toggleAutoOpen((event.target as HTMLInputElement).checked);
              },
            }),
            "Auto-open on new request",
          ]),
        ),
      ];
    },
  };
}

// The glue surface the detail flow drives: grant/deny live in the detail
// controller (InboxDetail.ts), but selection and advancement are list
// concerns owned here.
export interface InboxListHandle {
  getSelectedId(): string | null;
  // Fade the card and block re-selection while its deny POST is in flight;
  // the card leaves the list when the SSE-pushed pending set drops it.
  markDenying(id: string): void;
  // After a request resolves: dismiss the inbox (single-request opens), or
  // advance the selection to the next pending card (whole-inbox opens),
  // dismissing when none remain.
  advanceAfterResolution(resolvedId: string | null): Promise<void>;
  close(): void;
}

// Mount the whole inbox modal (drawer + list + detail) into its container.
// Reads the page's boot island ({chrome, inbox, inbox_detail?}), seeds the
// store, and renders the initially-selected request's payload immediately.
export function mountInboxModal(target: Element | null): InboxListHandle {
  const el = requireElement(target, "inbox modal container");
  const island = readBootState() as InboxBootIsland;
  if (island.chrome === undefined || island.inbox === undefined) {
    throw new MindsUIError("inbox boot island is missing the chrome or inbox slice");
  }
  seed(island.chrome);
  const host = getHost();
  connect(host);

  const keepOpen = island.inbox.keep_open;
  let selectedId: string | null = island.inbox.selected_id === "" ? null : island.inbox.selected_id;
  // Ids whose deny POST is in flight: kept visible but faded, pruned once an
  // SSE pending set no longer contains them.
  const denyingIds = new Set<string>();
  // Ids resolved locally (a grant the server confirmed): hidden immediately,
  // like the old post-approve list refetch, then pruned the same way.
  const resolvedIds = new Set<string>();
  // Uncontrolled-checkbox parity: seeded once from the boot snapshot; this
  // page's own toggle is the only writer, so later SSE ``auto_open`` values
  // (which echo it back) never clobber a click mid-flight.
  let isAutoOpenChecked = isRequestsAutoOpen();

  const close = (): void => host.closeModal();

  const updateUrl = (id: string | null): void => {
    try {
      // Preserve keep_open so a reload keeps the whole-inbox semantics.
      const suffix = keepOpen ? (id !== null ? "&keep_open=1" : "?keep_open=1") : "";
      const target_ = id !== null ? `/inbox?selected=${encodeURIComponent(id)}${suffix}` : `/inbox${suffix}`;
      history.replaceState(null, "", target_);
    } catch {
      // Restricted contexts (e.g. a sandboxed frame) may refuse; non-fatal.
    }
  };

  // The current detail controller (a fresh one per selection/payload swap);
  // null renders an empty right pane.
  let detailController: InboxDetailController | null = null;

  const listGlue = {
    getSelectedId: () => selectedId,
    markDenying: (id: string): void => {
      denyingIds.add(id);
      m.redraw();
    },
    advanceAfterResolution: async (resolvedId: string | null): Promise<void> => {
      await advanceAfterResolution(resolvedId);
    },
  };

  const setDetail = (payload: InboxDetailPayload | null): void => {
    detailController =
      payload === null
        ? null
        : new InboxDetailController(payload, listGlue, () => document.getElementById("inbox-detail"));
    m.redraw();
  };

  const fetchDetail = async (id: string): Promise<void> => {
    const response = await fetch(`/inbox/detail/${encodeURIComponent(id)}`, { credentials: "same-origin" });
    if (!response.ok) {
      // The server returns 200 with an "unavailable" payload for stale ids;
      // anything else is a real error, leave the pane untouched.
      return;
    }
    const data = (await response.json()) as { detail?: InboxDetailPayload };
    if (data.detail === undefined) return;
    setDetail(data.detail);
  };

  const isSelectable = (card: ChromeRequestCard): boolean =>
    !denyingIds.has(card.id) && !resolvedIds.has(card.id);

  // Pick the next selectable card after the resolved one, falling back to the
  // previous, falling back to any selectable card (store order is the
  // server's most-recent-first). Skips mid-deny cards so the user isn't
  // auto-bounced into a dialog that's about to vanish.
  const findNextPendingId = (resolvedId: string | null): string | null => {
    const cards = getRequestCards();
    const candidates = cards.filter((card) => isSelectable(card) && card.id !== resolvedId);
    if (resolvedId === null) return candidates.length > 0 ? candidates[0].id : null;
    const idx = cards.findIndex((card) => card.id === resolvedId);
    if (idx === -1) return candidates.length > 0 ? candidates[0].id : null;
    const after = cards.slice(idx + 1).find(isSelectable);
    if (after !== undefined) return after.id;
    const before = cards
      .slice(0, idx)
      .reverse()
      .find(isSelectable);
    return before !== undefined ? before.id : null;
  };

  const selectItem = async (id: string): Promise<void> => {
    selectedId = id;
    m.redraw();
    updateUrl(id);
    await fetchDetail(id);
  };

  const advanceAfterResolution = async (resolvedId: string | null): Promise<void> => {
    if (!keepOpen) {
      // The inbox was opened for a single request; resolving it dismisses
      // the window instead of surprising the user with an unrelated stale
      // request. An in-flight deny POST carries keepalive, so it still
      // completes after the window closes.
      close();
      return;
    }
    if (resolvedId !== null && !denyingIds.has(resolvedId)) {
      // A grant: the server confirmed resolution before we got here, so drop
      // the card now rather than waiting for the SSE push. (Denies instead
      // stay visible as ``is-denying`` until the push prunes them.)
      resolvedIds.add(resolvedId);
      m.redraw();
    }
    const nextId = findNextPendingId(resolvedId);
    if (nextId !== null) {
      await selectItem(nextId);
    } else {
      // Nothing left to act on; ``is-denying`` cards still in the list
      // resolve in the background via their keepalive POST + the next push.
      close();
    }
  };

  // Reconcile local state against every store change: prune transients the
  // server's pending set has caught up with, and swap the detail pane to the
  // canonical "no longer available" payload when the selection vanishes out
  // from under the user (resolved elsewhere). Released with the mount
  // (onremove below) so a torn-down page stops reconciling.
  const unsubscribe = subscribe(() => {
    const ids = new Set(getRequestCards().map((card) => card.id));
    denyingIds.forEach((id) => {
      if (!ids.has(id)) denyingIds.delete(id);
    });
    resolvedIds.forEach((id) => {
      if (!ids.has(id)) resolvedIds.delete(id);
    });
    if (selectedId !== null && !ids.has(selectedId)) {
      const vanishedId = selectedId;
      selectedId = null;
      updateUrl(null);
      void fetchDetail(vanishedId);
    }
  });

  const controller: InboxListController = {
    getVisibleCards: () => getRequestCards().filter((card) => !resolvedIds.has(card.id)),
    isDenying: (id) => denyingIds.has(id),
    getSelectedId: () => selectedId,
    isAutoOpenChecked: () => isAutoOpenChecked,
    selectCard: (id) => {
      void selectItem(id);
    },
    toggleAutoOpen: (isEnabled) => {
      isAutoOpenChecked = isEnabled;
      // Fire-and-forget: the server reads the value on its next SSE-driven
      // auto-open decision, so the UI doesn't wait for the response.
      fetch("/_chrome/requests-auto-open", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: isEnabled }),
        keepalive: true,
      }).catch(() => undefined);
    },
  };

  // Seed the initial detail from the island (no fetch round-trip).
  if (island.inbox_detail !== undefined) {
    detailController = new InboxDetailController(island.inbox_detail, listGlue, () =>
      document.getElementById("inbox-detail"),
    );
  }

  // Escape dismisses (matching the old shell script; the Electron main
  // process also handles Escape for any modal-view page).
  const onKeydown = (event: KeyboardEvent): void => {
    if (event.key === "Escape") close();
  };
  document.addEventListener("keydown", onKeydown);

  mountWithTeardown(el, {
    view: () =>
      m(
        "div",
        {
          id: "inbox-backdrop",
          class: "fixed inset-0 bg-surface-overlay flex justify-start",
          onclick: (event: Event) => {
            if (event.target === event.currentTarget) close();
          },
        },
        [
          m(
            "div",
            {
              id: "inbox-dialog",
              class:
                "relative w-[90vw] lg:w-[75vw] max-w-[1100px] h-full flex flex-col bg-surface-primary " +
                "border-r border-default shadow-overlay overflow-hidden",
            },
            [
              // Header row: 3-column grid so the title sits centered
              // regardless of the close button's width; opted into
              // app-region: drag so the user can grab it to move the window
              // (the close button opts back out).
              m(
                "div",
                {
                  class: "grid grid-cols-3 items-center px-2 h-[38px] border-b border-default",
                  style: { "-webkit-app-region": "drag" },
                },
                [
                  m("div"),
                  m("h1", { class: "type-section text-primary text-center" }, "Requests"),
                  m(
                    "button",
                    {
                      type: "button",
                      "aria-label": "Close",
                      "data-tooltip": "Close",
                      id: "inbox-close-btn",
                      style: { "-webkit-app-region": "no-drag" },
                      class:
                        "justify-self-end inline-flex items-center justify-center w-6 h-6 rounded-md " +
                        "text-tertiary hover:text-primary hover:bg-fill-hover cursor-pointer",
                      onclick: () => close(),
                    },
                    m(Icon, { name: "close" }),
                  ),
                ],
              ),
              m(
                "div",
                {
                  id: "inbox-body",
                  class:
                    "flex flex-1 min-h-0" + (controller.getVisibleCards().length === 0 ? " is-empty" : ""),
                },
                [
                  m(
                    "div",
                    { id: "inbox-left-column", class: "flex flex-col w-72 border-r border-default bg-fill-subtle" },
                    m(InboxList, { controller }),
                  ),
                  m(
                    "div",
                    { id: "inbox-detail", class: "flex-1 overflow-y-auto p-6 sm:p-6" },
                    detailController !== null ? m(InboxDetail, { controller: detailController }) : null,
                  ),
                ],
              ),
            ],
          ),
          // Drag strip in the backdrop to the right of the drawer, matching
          // the chrome titlebar height. The OS swallows mousedown for
          // dragging here; the rest of the backdrop stays click-to-dismiss.
          m("div", { class: "flex-1 h-[38px] self-start", style: { "-webkit-app-region": "drag" } }),
        ],
      ),
    onremove: () => {
      unsubscribe();
      document.removeEventListener("keydown", onKeydown);
    },
  });

  return {
    getSelectedId: () => selectedId,
    markDenying: listGlue.markDenying,
    advanceAfterResolution,
    close,
  };
}
