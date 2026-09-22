// The bell's notification feed: a historic list of the recent stream in
// WIRE ORDER (the server sends unresolved first, then resolved, each
// newest-first -- the wire order IS the display order, so nothing is
// re-sorted here). The badge tracks unresolved entries, so it clears when
// they resolve or are cleared, never because you looked. Not a route: the
// Shell floats it as a popover anchored under the bell
// (shell.isNotificationsOpen) over whatever surface is on screen.

import m from "mithril";
import { getAppContext } from "../../app-context";
import type { UiNotificationEntry } from "../../channel/messages";
import type { NotificationsUiController } from "../../models/notificationsUi";
import { Icon16 } from "../components/Icon";
import {
  KIND_ICON_NAME,
  notificationLine,
  timeAgo,
} from "../components/NotificationLine";

/** A resolved row is a spent receipt: strip its saturation so the red dots
 * fall to grey, and dim it, so the live asks stay the only vivid things in
 * the feed. The colored children ride along for free. */
const RESOLVED_FADE =
  "opacity-60 grayscale transition-[opacity,filter] duration-500";

/** The feed's filter tabs: everything, or one kind. */
export type NotificationFilter = "all" | "requests" | "messages" | "system";

const FILTER_TABS: { id: NotificationFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "requests", label: "Requests" },
  { id: "messages", label: "Messages" },
  { id: "system", label: "System" },
];

const KIND_BY_FILTER: Record<
  Exclude<NotificationFilter, "all">,
  UiNotificationEntry["kind"]
> = {
  requests: "permission_request",
  messages: "agent_message",
  system: "system_event",
};

/** The entries a filter tab shows, in wire order. */
export function filterEntries(
  entries: readonly UiNotificationEntry[],
  filter: NotificationFilter,
): UiNotificationEntry[] {
  if (filter === "all") return [...entries];
  const kind = KIND_BY_FILTER[filter];
  return entries.filter((entry) => entry.kind === kind);
}

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

function clearButton(
  entry: UiNotificationEntry,
  controller: NotificationsUiController | null,
): m.Children {
  return m(
    "button",
    {
      type: "button",
      "aria-label": "Clear",
      "data-clear-notification": entry.id,
      class:
        "ml-1 inline-flex h-6 w-6 shrink-0 cursor-pointer items-center justify-center rounded-md " +
        "text-tertiary hover:bg-fill-hover hover:text-primary",
      onclick: (event: MouseEvent) => {
        // A pending row is itself a button; keep the X from also opening it.
        event.stopPropagation();
        controller?.clearEntry(entry.id);
      },
    },
    m(Icon16, { name: "close", size: "sm" }),
  );
}

function feedRow(
  entry: UiNotificationEntry,
  nowMs: number,
  controller: NotificationsUiController | null,
): m.Children {
  const isPending = !entry.is_resolved;
  // Pending -> the "when" reads at full primary weight with a leading red
  // dot, pulling the eye to how fresh the actionable entries are. Resolved
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
      clearButton(entry, controller),
    ],
  );
  const kindMark = m(
    "span",
    {
      class: "mt-1 inline-flex shrink-0 text-tertiary",
      "data-kind-mark": entry.kind,
      "aria-hidden": "true",
    },
    m(Icon16, { name: KIND_ICON_NAME[entry.kind], size: "sm" }),
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
        class: "flex w-full gap-2 px-3 py-2.5 " + RESOLVED_FADE,
        "data-notification-id": entry.id,
      },
      [kindMark, body],
    );
  }
  // The whole pending row is the action: the entry's own open gesture (the
  // review popup, the chat, the backups surface).
  return m(
    "button",
    {
      key: entry.id,
      type: "button",
      class:
        "flex w-full cursor-pointer gap-2 px-3 py-2.5 text-left hover:bg-fill-hover",
      "data-notification-id": entry.id,
      onclick: () => controller?.openEntry(entry),
    },
    [kindMark, body],
  );
}

function NotificationsPageComponent(): m.Component {
  let filter: NotificationFilter = "all";
  return {
    view() {
      const { stores, shell } = getAppContext();
      const controller = shell.notificationsUi ?? null;
      const entries = stores.notifications.entries;
      const shown = filterEntries(entries, filter);
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
                "flex h-[56px] shrink-0 items-center justify-between border-b border-subtle pr-12 pl-3",
            },
            [
              m(
                "span",
                { class: "flex items-center gap-1.5 type-label text-primary" },
                [m(Icon16, { name: "bell", size: "sm" }), "Notifications"],
              ),
              entries.length > 0
                ? m(
                    "button",
                    {
                      type: "button",
                      id: "notifications-clear-all",
                      class:
                        "cursor-pointer type-helper text-secondary hover:text-primary",
                      onclick: () => controller?.clearAll(),
                    },
                    "Clear all",
                  )
                : null,
            ],
          ),
          m(
            "div",
            {
              role: "tablist",
              "aria-label": "Filter notifications",
              class:
                "flex shrink-0 items-center gap-1 border-b border-subtle px-2 py-1.5",
            },
            FILTER_TABS.map((tab) =>
              m(
                "button",
                {
                  type: "button",
                  role: "tab",
                  "data-filter": tab.id,
                  "aria-selected": filter === tab.id ? "true" : "false",
                  class:
                    "cursor-pointer rounded-md px-2 py-0.5 type-helper " +
                    (filter === tab.id
                      ? "bg-fill-hover text-primary"
                      : "text-secondary hover:text-primary"),
                  onclick: () => {
                    filter = tab.id;
                  },
                },
                tab.label,
              ),
            ),
          ),
          shown.length === 0
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
                shown.map((entry) => feedRow(entry, nowMs, controller)),
              ),
        ],
      );
    },
  };
}

export const NotificationsPage: m.ComponentTypes = NotificationsPageComponent;
