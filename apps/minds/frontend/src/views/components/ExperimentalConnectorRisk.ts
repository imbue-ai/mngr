// The warning that goes with an experimental connector (a service a latchkey
// plugin adds) wherever one is offered or asked for: one short sentence, with
// what the risk actually is behind its tooltip.

import m from "mithril";
import { Icon16 } from "./Icon";

export const EXPERIMENTAL_CONNECTOR_RISK_DETAIL =
  "Experimental connectors rely on a service's undocumented API and can stop working without notice. " +
  "In the extreme case, the service might suspend your account.";

export const EXPERIMENTAL_CONNECTOR_RISK_ATTR = "data-experimental-connector-risk";

export function renderExperimentalConnectorRisk(sentence: string, extra = ""): m.Children {
  return m(
    "p",
    {
      class: `flex w-fit items-center gap-1.5 type-helper text-warning cursor-help ${extra}`.trimEnd(),
      tabindex: 0,
      "data-tooltip": EXPERIMENTAL_CONNECTOR_RISK_DETAIL,
      [EXPERIMENTAL_CONNECTOR_RISK_ATTR]: "",
    },
    [m(Icon16, { name: "triangle-alert", size: "sm", extra: "shrink-0" }), sentence],
  );
}
