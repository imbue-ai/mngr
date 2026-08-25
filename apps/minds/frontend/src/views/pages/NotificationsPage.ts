// The bell's notification feed: a historic list of the recent stream in
// WIRE ORDER (the server sends unresolved first, then resolved, each
// newest-first -- the wire order IS the display order, so nothing is
// re-sorted here). No clear-all and no per-row dismiss: the badge tracks
// unresolved requests, so it clears only when they resolve, never because
// you looked. Not a route: the Shell floats it as a popover anchored under
// the bell (shell.isNotificationsOpen) over whatever surface is on screen.

import m from "mithril";
import { getAppContext } from "../../app-context";
import type { UiNotificationEntry } from "../../channel/messages";
import { openReviewRoute } from "../../models/notificationsUi";
import { Icon16 } from "../components/Icon";
import { notificationLine, timeAgo } from "../components/NotificationLine";

/** A resolved row is a spent receipt: strip its saturation so the red dots
 * fall to grey, and dim it, so the live asks stay the only vivid things in
 * the feed. The colored children ride along for free. */
const RESOLVED_FADE =
  "opacity-60 grayscale transition-[opacity,filter] duration-500";

/** The resolved-outcome chip a row carries once it is no longer pending.
 * "Closed" is the neutral auto-resolution (the request vanished, e.g. its
 * workspace was destroyed). */
function outcomeChip(outcome: "approved" | "denied" | "closed"): m.Children {
  if (outcome === "approved") {
    return m(
      "span",
      {
        class: "mt-1 inline-flex items-center gap-1 type-badge text-success",
        "data-outcome": "approved",
      },
      [m(Icon16, { name: "check", size: "sm" }), "Approved"],
    );
  }
  if (outcome === "denied") {
    return m(
      "span",
      {
        class: "mt-1 inline-flex items-center gap-1 type-badge text-secondary",
        "data-outcome": "denied",
      },
      [m(Icon16, { name: "close", size: "sm" }), "Denied"],
    );
  }
  return m(
    "span",
    {
      class: "mt-1 inline-flex items-center gap-1 type-badge text-tertiary",
      "data-outcome": "closed",
    },
    "Closed",
  );
}

function feedRow(entry: UiNotificationEntry, nowMs: number): m.Children {
  const isPending = !entry.is_resolved;
  // Pending -> the "when" reads at full primary weight with a leading red
  // dot, pulling the eye to how fresh the actionable asks are. Resolved
  // receipts keep it quiet.
  const meta = m(
    "span",
    {
      class:
        "shrink-0 inline-flex items-center gap-1 type-badge " +
        (isPending ? "text-primary" : "text-secondary"),
    },
    [
      isPending
        ? m("span", {
            class: "h-1.5 w-1.5 shrink-0 rounded-full bg-important",
            "aria-hidden": "true",
          })
        : null,
      timeAgo(entry.created_at, nowMs),
    ],
  );
  const body = notificationLine({
    entry,
    meta,
    footer:
      entry.is_resolved && entry.outcome !== null
        ? outcomeChip(entry.outcome)
        : null,
  });
  if (!isPending) {
    return m(
      "div",
      {
        key: entry.id,
        class: "flex w-full px-3 py-2.5 " + RESOLVED_FADE,
        "data-notification-id": entry.id,
      },
      body,
    );
  }
  // The whole pending row is the action: the uniform review gesture (hop to
  // the asking workspace, open the review popup there).
  return m(
    "button",
    {
      key: entry.id,
      type: "button",
      class:
        "flex w-full cursor-pointer px-3 py-2.5 text-left hover:bg-fill-hover",
      "data-notification-id": entry.id,
      onclick: () =>
        openReviewRoute(entry.workspace_agent_id, entry.request_id),
    },
    body,
  );
}

function NotificationsPageComponent(): m.Component {
  return {
    view() {
      const { stores } = getAppContext();
      const entries = stores.notifications.entries;
      // One timestamp per render pass keeps every row's "when" on one clock.
      const nowMs = Date.now();
      return m(
        "div#notifications-feed",
        { class: "flex min-h-0 flex-1 flex-col" },
        [
          m(
            "div",
            {
              // 56px centers this row on the same line the panel's close X
              // sits on (DialogCloseButton: 12px inset + 32px hit area, so
              // its own center is 28px down) -- half of 56 is 28, so the
              // title lands exactly level with it instead of reading high.
              class:
                "flex h-[56px] shrink-0 items-center border-b border-subtle px-3",
            },
            m(
              "span",
              { class: "flex items-center gap-1.5 type-label text-primary" },
              [m(Icon16, { name: "bell", size: "sm" }), "Notifications"],
            ),
          ),
          entries.length === 0
            ? m(
                "div",
                {
                  class:
                    "flex flex-col items-center justify-center gap-2 px-6 py-10 text-center",
                },
                [
                  m(Icon16, {
                    name: "bell",
                    size: "lg",
                    extra: "text-tertiary",
                  }),
                  m(
                    "p",
                    { class: "type-helper text-tertiary" },
                    "You're all caught up.",
                  ),
                ],
              )
            : m(
                "div",
                {
                  class:
                    "flex min-h-0 flex-col divide-y divide-subtle overflow-y-auto",
                },
                entries.map((entry) => feedRow(entry, nowMs)),
              ),
        ],
      );
    },
  };
}

export const NotificationsPage: m.ComponentTypes = NotificationsPageComponent;
