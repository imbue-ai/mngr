// The shared notification sentence: accent dot + workspace name + the ask,
// with an optional service brand mark and a clamped secondary line. One
// component so the feed overlay's rows and the toast cards read as the same
// notification wherever it flashes (the prototype shares PermissionNotice the
// same way).

import m from "mithril";
import type { UiNotificationEntry } from "../../channel/messages";
import { serviceMark } from "./ServiceMark";

/** Coarse "when" label -- the feed is a glance surface, not a precise log.
 * Empty for an unparseable timestamp rather than "NaNd ago". */
export function timeAgo(createdAtIso: string, nowMs: number): string {
  const atMs = Date.parse(createdAtIso);
  if (Number.isNaN(atMs)) return "";
  const seconds = Math.max(0, Math.round((nowMs - atMs) / 1000));
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export interface NotificationLineParts {
  entry: UiNotificationEntry;
  /** Right-aligned metadata on the sentence line (the feed row's timestamp). */
  meta?: m.Children;
  /** Rendered under the body (the feed row's resolved-outcome chip). */
  footer?: m.Children;
}

/** A plain render function rather than a component (the serviceMark
 * precedent), so callers' vnode trees carry the line's markup directly --
 * which is also what lets view tests walk it without expanding components. */
export function notificationLine({
  entry,
  meta,
  footer,
}: NotificationLineParts): m.Children {
  return m("div", { class: "min-w-0 flex-1" }, [
    m("div", { class: "flex items-start gap-2" }, [
      m("span", { class: "min-w-0 flex-1 type-body text-primary" }, [
        m("span", {
          class:
            "mr-1.5 inline-block h-2 w-2 shrink-0 rounded-full align-middle",
          "aria-hidden": "true",
          style: `background-color: ${entry.workspace_accent}`,
        }),
        m("span", { class: "font-semibold" }, entry.workspace_name),
        " asks — ",
        entry.service_name !== ""
          ? m(
              "span",
              { class: "mr-1 inline-block align-middle" },
              serviceMark(entry.service_name, "w-3.5 h-3.5", "brand", null),
            )
          : null,
        m("span", { class: "font-semibold" }, entry.title),
      ]),
      meta ?? null,
    ]),
    entry.body !== ""
      ? m(
          "p",
          { class: "mt-0.5 type-helper text-secondary line-clamp-2" },
          entry.body,
        )
      : null,
    footer ?? null,
  ]);
}
