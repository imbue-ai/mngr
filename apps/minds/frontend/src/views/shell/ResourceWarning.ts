/**
 * The titlebar's resource warning for this computer: while one stands, a pill says what is wrong in words ("Disk
 * space is low", with "+1" when there are more). A click opens Settings > This computer, where the readings and the
 * warning levels are. Nothing is drawn while no warning stands.
 *
 * The live pill is drawn above the titlebar rather than in it: an app-level dialog (Settings, Accounts) covers the
 * bar with a backdrop that closes the dialog on a click, and the pill has to stay readable and clickable there. The
 * bar keeps an invisible copy in its row to hold the pill's place. Keep every utility name a contiguous literal (the
 * Tailwind scanner reads them).
 */

import m from "mithril";
import type { DeviceResourcesStore, ResourceAlert } from "../../models/deviceResources";

/** The id of the invisible copy inside the titlebar that holds the pill's place. */
const SLOT_ID = "device-resource-warning-slot";

const PILL_CLASS =
  "inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full border-0 px-2.5 type-helper font-medium " +
  "whitespace-nowrap text-white";

function standingAlert(store: DeviceResourcesStore): ResourceAlert | null {
  const view = store.view;
  if (view === null || store.isStale) return null;
  return view.alerts[0] ?? null;
}

function pillContent(alert: ResourceAlert, extraCount: number): m.Children {
  return [
    m("span", { "aria-hidden": "true", class: "font-bold" }, "!"),
    m("span", alert.headline),
    extraCount > 0 ? m("span", { "data-device-warning-more": "" }, `+${extraCount}`) : null,
  ];
}

function severityClass(alert: ResourceAlert): string {
  return alert.severity === "critical" ? "bg-important" : "bg-warning";
}

/** The pill's place in the titlebar's row: an invisible copy, so the bar lays out around it. */
export const DeviceResourceWarningSlot: m.Component<{ store: DeviceResourcesStore }> = {
  view(vnode) {
    const store = vnode.attrs.store;
    const alert = standingAlert(store);
    if (alert === null) return null;
    return m(
      `span#${SLOT_ID}`,
      { "aria-hidden": "true", class: `${PILL_CLASS} ${severityClass(alert)} invisible` },
      pillContent(alert, (store.view?.alerts.length ?? 1) - 1),
    );
  },
};

/** Put the live pill over its slot. Anchored by its right edge: the titlebar's right group keeps its distance from
 *  the window's right edge when the window is resized. */
function placeOverSlot(element: HTMLElement): void {
  const slot = document.getElementById(SLOT_ID);
  if (slot === null) return;
  const rect = slot.getBoundingClientRect();
  element.style.right = `${window.innerWidth - rect.right}px`;
  element.style.top = `${rect.top}px`;
}

export interface DeviceResourceWarningAttrs {
  store: DeviceResourcesStore;
  onOpenSettings: () => void;
  /** Classes beyond the pill's own (the titlebar hides it while a popup raises its icons). */
  extra?: string;
}

/** The live pill, a sibling of the titlebar drawn over the slot the bar keeps for it. */
export const DeviceResourceWarning: m.Component<DeviceResourceWarningAttrs> = {
  oncreate(vnode) {
    placeOverSlot(vnode.dom as HTMLElement);
  },
  onupdate(vnode) {
    placeOverSlot(vnode.dom as HTMLElement);
  },
  view(vnode) {
    const { store, onOpenSettings, extra } = vnode.attrs;
    const alert = standingAlert(store);
    if (alert === null) return null;
    const alerts = store.view?.alerts ?? [];
    return m(
      "button",
      {
        id: "device-resource-warning",
        type: "button",
        "data-device-warning": alert.resource,
        "data-severity": alert.severity,
        "data-tooltip": "Resource warnings",
        "aria-label": `${alerts.map((each) => `${each.headline}: ${each.detail}`).join(". ")}. Open resource warnings.`,
        class:
          `fixed z-[120] cursor-pointer ${PILL_CLASS} ${severityClass(alert)} ` +
          "hover:brightness-95 focus-visible:outline-2 focus-visible:outline-accent " +
          (extra ?? ""),
        onclick: () => onOpenSettings(),
      },
      pillContent(alert, alerts.length - 1),
    );
  },
};
