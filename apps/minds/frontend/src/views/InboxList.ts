// The inbox's left pane: the pending-request card list plus the pinned
// auto-open footer, store-fed from the chrome ``requests`` payload's card
// summaries (the deleted /inbox/list fragment refetch). The right detail
// pane deliberately stays a server-rendered fragment (each request handler
// composes its own form HTML); this module owns fetching and swapping that
// fragment, while the page's inline script keeps the form logic that must
// survive swaps (Approve gating, share-path pickers, grant/deny submission).
import m from "mithril";

import type { ChromeRequestCard, InboxBootIsland } from "../chrome_state";
import { getHost } from "../host";
import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";
import { connect, getRequestCards, isRequestsAutoOpen, seed, subscribe } from "../store";

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
  // off ``is-empty`` on #inbox-body -- OUTSIDE this mount root, because the
  // detail pane it hides is a sibling. Synced after every render.
  const syncEmptyState = (controller: InboxListController): void => {
    const body = document.getElementById("inbox-body");
    if (body === null) return;
    body.classList.toggle("is-empty", controller.getVisibleCards().length === 0);
  };
  return {
    oncreate: (vnode) => syncEmptyState(vnode.attrs.controller),
    onupdate: (vnode) => syncEmptyState(vnode.attrs.controller),
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

export interface MountInboxListOptions {
  // Called after the detail pane's server fragment is swapped in, so the
  // page's inline script can re-wire its form logic (Approve-button gating,
  // share-path picker buttons).
  onDetailSwapped: () => void;
}

// The glue surface the inbox page's inline script drives: grant/deny live in
// the inline script (they own the server fragment's form), but selection and
// advancement are list concerns owned here.
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

// Mount the inbox left pane into #inbox-left-column. Reads the page's boot
// island ({chrome, inbox}), seeds the store, and returns the glue handle for
// the page's inline script.
export function mountInboxList(target: Element | null, options: MountInboxListOptions): InboxListHandle {
  const el = requireElement(target, "inbox left column");
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

  const fetchDetailFragment = async (id: string): Promise<void> => {
    const response = await fetch(`/inbox/detail/${encodeURIComponent(id)}`, { credentials: "same-origin" });
    if (!response.ok) {
      // The server returns 200 with an "unavailable" fragment for stale ids;
      // anything else is a real error, leave the pane untouched.
      return;
    }
    const html = await response.text();
    const detail = document.getElementById("inbox-detail");
    if (detail === null) return;
    detail.innerHTML = html;
    options.onDetailSwapped();
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
    await fetchDetailFragment(id);
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
  // canonical "no longer available" fragment when the selection vanishes
  // out from under the user (resolved elsewhere). Released with the mount
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
      void fetchDetailFragment(vanishedId);
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

  mountWithTeardown(el, {
    view: () => m(InboxList, { controller }),
    onremove: () => unsubscribe(),
  });

  return {
    getSelectedId: () => selectedId,
    markDenying: (id) => {
      denyingIds.add(id);
      m.redraw();
    },
    advanceAfterResolution,
    close,
  };
}
