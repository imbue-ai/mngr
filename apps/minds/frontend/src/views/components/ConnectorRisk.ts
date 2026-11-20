// The cautions that go with a connector that may ride an API its service does
// not document: one short sentence, with what the risk actually is behind the
// "See more" that follows it.
//
// An experimental connector (a service a latchkey plugin adds) is known to
// ride one, so it gets the warning. A connection the agent asked to create
// only might, when its sign-in captures what the service's own website holds,
// so it gets the same line a tone quieter.

import m from "mithril";
import { Collapsible } from "./Collapsible";
import { Icon16 } from "./Icon";
import type { IconName } from "./icons";

export const EXPERIMENTAL_CONNECTOR_RISK_DETAIL =
  "Experimental connectors rely on a service's undocumented API and can stop working without notice. " +
  "In the extreme case, the service might suspend your account.";

const CAPTURE_LOGIN_RISK_SENTENCE = "This connection may rely on an unofficial API.";

export const CAPTURE_LOGIN_RISK_DETAIL =
  "Many connections like this work fine, but it could stop working without notice and, in rare cases, the " +
  "service might restrict your account.";

export const EXPERIMENTAL_CONNECTOR_RISK_ATTR = "data-experimental-connector-risk";

export const CAPTURE_LOGIN_RISK_ATTR = "data-capture-login-risk";

const MORE_LABEL = "See more";

interface ConnectorRiskTone {
  icon: IconName;
  iconClass: string;
  sentenceClass: string;
}

const WARNING_TONE: ConnectorRiskTone = {
  icon: "triangle-alert",
  iconClass: "text-warning",
  sentenceClass: "text-warning",
};

const NOTE_TONE: ConnectorRiskTone = {
  icon: "info",
  iconClass: "text-tertiary",
  sentenceClass: "text-secondary",
};

interface ConnectorRiskToggleAttrs {
  /** Unique on the page; the toggle and the detail it opens are named from it. */
  id: string;
  isOpen: boolean;
  onToggle: () => void;
  extra?: string;
}

interface ExperimentalConnectorRiskAttrs extends ConnectorRiskToggleAttrs {
  sentence: string;
}

function renderConnectorRisk(
  tone: ConnectorRiskTone,
  hookAttr: string,
  sentence: string,
  detail: string,
  attrs: ConnectorRiskToggleAttrs,
): m.Children {
  const { id, isOpen, onToggle, extra = "" } = attrs;
  return m(
    "div",
    {
      class: `flex items-start gap-1.5 type-helper ${extra}`.trimEnd(),
      [hookAttr]: "",
    },
    [
      m(Icon16, { name: tone.icon, size: "sm", extra: `mt-px shrink-0 ${tone.iconClass}` }),
      m("div", { class: "min-w-0" }, [
        m("span", { class: tone.sentenceClass }, sentence),
        " ",
        m(
          Collapsible,
          {
            id,
            isOpen,
            onToggle,
            // The box lays nothing out itself, so the toggle stays on the
            // sentence's line and the detail opens as a block underneath it.
            extra: "contents",
            triggerExtra: "border-0 bg-transparent p-0 text-tertiary underline hover:text-primary",
            panelExtra: "mt-1 text-secondary",
            marker: () => null,
            summary: MORE_LABEL,
          },
          detail,
        ),
      ]),
    ],
  );
}

export function renderExperimentalConnectorRisk(attrs: ExperimentalConnectorRiskAttrs): m.Children {
  return renderConnectorRisk(
    WARNING_TONE,
    EXPERIMENTAL_CONNECTOR_RISK_ATTR,
    attrs.sentence,
    EXPERIMENTAL_CONNECTOR_RISK_DETAIL,
    attrs,
  );
}

export function renderCaptureLoginRisk(attrs: ConnectorRiskToggleAttrs): m.Children {
  return renderConnectorRisk(
    NOTE_TONE,
    CAPTURE_LOGIN_RISK_ATTR,
    CAPTURE_LOGIN_RISK_SENTENCE,
    CAPTURE_LOGIN_RISK_DETAIL,
    attrs,
  );
}
