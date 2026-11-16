// The agent's ask to also send a service's requests through this computer.
// The switch rides on Approve: denying, or closing the dialog, leaves the
// route as it is now.

import m from "mithril";
import type { InboxModel, RequestedProxy } from "../../../models/inbox";
import {
  DESKTOP_EGRESS_LABEL,
  renderDesktopEgressHops,
} from "../../components/DesktopEgressRouteSetting";
import { renderPermissionSwitch } from "../../components/PermissionSwitch";

const PROXY_SUMMARY_ID = "permissions-proxy-summary";

export function renderProxySection(
  model: InboxModel,
  requested: RequestedProxy | null,
  /** What the dialog calls the service whose requests would be rerouted. */
  serviceLabel: string,
): m.Children {
  if (requested === null) return null;
  return m("div", { id: "permissions-proxy", class: "flex flex-col gap-1.5" }, [
    m("h3", { class: "type-section text-tertiary" }, "Where requests leave from"),
    m(
      "p",
      { class: "type-body text-secondary" },
      `The agent asked for this workspace's requests to ${serviceLabel} to leave through this computer.`,
    ),
    m("div", { class: "flex items-center justify-between gap-4" }, [
      m("div", { class: "min-w-0" }, [
        m("p", { class: "type-body text-primary truncate" }, DESKTOP_EGRESS_LABEL),
        m(
          "p",
          { id: PROXY_SUMMARY_ID, class: "type-helper text-tertiary mt-0.5" },
          model.isProxyOn
            ? [
                "Requests will leave from ",
                renderDesktopEgressHops(requested.resulting_route, requested.desktops, true),
                ".",
              ]
            : "Requests will keep leaving from where they do now.",
        ),
      ]),
      renderPermissionSwitch({
        isOn: model.isProxyOn,
        isBusy: false,
        isDisabled: false,
        title: null,
        label: DESKTOP_EGRESS_LABEL,
        describedBy: PROXY_SUMMARY_ID,
        permission: "proxy",
        onFlip: (isOn) => {
          model.isProxyOn = isOn;
        },
      }),
    ]),
  ]);
}
