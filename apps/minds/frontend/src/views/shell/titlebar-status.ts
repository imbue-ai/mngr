// The titlebar's status pill: one short status at a time, after the workspace
// icon group, for notices one level of urgency below the notice band. The band
// keeps what is wrong right now; the pill carries what can wait for the user's
// next look.

import m from "mithril";

export interface TitlebarStatus {
  /** Identity, for the DOM and tests. */
  key: string;
  label: string;
  /** "attention" is the solid orange pill: something needs the user.
   * "quiet" is the grey pill with a dot: information only. */
  tone: "attention" | "quiet";
  /** The quiet pill's dot colour; the attention pill's dot is white. */
  dot: "warning" | "success";
  isDotPulsing: boolean;
  tooltip: string;
  onClick: () => void;
}

const DOT_CLASS: Record<TitlebarStatus["dot"], string> = {
  warning: "bg-warning",
  success: "bg-success",
};

/** The pill's look, shared by the titlebar's own pill and the raised copy an
 * open titlebar popup draws over it. */
export function titlebarStatusPillClass(status: TitlebarStatus): string {
  return (
    "inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full type-helper font-medium whitespace-nowrap " +
    "cursor-pointer focus-visible:outline-2 focus-visible:outline-accent " +
    (status.tone === "attention"
      ? "pl-2 pr-2.5 text-white bg-warning shadow-[0_1px_2px_rgb(0_0_0/0.15)] hover:brightness-95"
      : "px-2.5 text-primary bg-fill-hover hover:bg-fill-active")
  );
}

/** The pill's dot and label. */
export function titlebarStatusPillContent(status: TitlebarStatus): m.Children {
  return [
    m("span", {
      class:
        "size-1.5 rounded-full shrink-0 " +
        (status.tone === "attention" ? "bg-white" : DOT_CLASS[status.dot]) +
        (status.isDotPulsing ? " animate-pulse" : ""),
      "aria-hidden": "true",
    }),
    m("span", status.label),
  ];
}

export interface TitlebarStatusPillAttrs {
  status: TitlebarStatus;
  /** Classes beyond the pill's own (the titlebar hides it while a popup raises its copy). */
  extra?: string;
}

export function TitlebarStatusPill(): m.Component<TitlebarStatusPillAttrs> {
  return {
    view(vnode) {
      const { status, extra } = vnode.attrs;
      return m(
        "button",
        {
          id: "titlebar-status-pill",
          "data-status": status.key,
          "data-tone": status.tone,
          class: `${titlebarStatusPillClass(status)} ml-1 ${extra ?? ""}`.trim(),
          type: "button",
          "data-tooltip": status.tooltip,
          onclick: () => status.onClick(),
        },
        titlebarStatusPillContent(status),
      );
    },
  };
}
