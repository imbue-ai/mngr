// Studio settings' This computer section: the one switch that lets the agents
// of an approved workspace run commands on this device as the user, and the
// warning that stands between flipping it on and writing it. Each workspace's
// own grant is asked for from inside the workspace and revoked on its
// Permissions tab.

import m from "mithril";
import { electronBridge } from "../../../electron-bridge";
import type { SettingsModel } from "../../../models/settings";
import { Button } from "../../components/Button";
import { Modal } from "../../components/Modal";
import { Notice } from "../../components/Notice";
import {
  remoteExecutionConsequenceList,
  remoteExecutionWarningBanner,
} from "../../components/RemoteExecutionWarning";
import { renderSwitch } from "../../components/Switch";

/** The warning that stands between the switch and its write: the user has to
 * read what they are allowing and tick that they understand it before the
 * confirm button does anything. In-DOM rather than a native dialog, so the
 * browser build behaves exactly as the desktop app. */
function remoteExecutionWarningModal(model: SettingsModel): m.Children {
  return m(
    Modal,
    {
      isOpen: model.isRemoteExecutionWarningOpen,
      onClose: () => model.cancelRemoteExecutionWarning(),
      size: "md",
      id: "remote-execution-warning",
    },
    [
      remoteExecutionWarningBanner(
        "This lets agents run any command on this computer as you.",
        "remote-execution-warning-banner",
      ),
      m(
        "p",
        { class: "type-body text-primary mt-3 mb-2" },
        "An approved workspace's agents will be able to:",
      ),
      remoteExecutionConsequenceList("remote-execution-warning-consequences"),
      m(
        "p",
        { class: "type-helper text-tertiary mt-3" },
        "This applies to this computer only. Every workspace still has to ask for it separately, " +
          "and you can turn it off here at any time.",
      ),
      electronBridge.isMacPlatform
        ? m(
            "p",
            { class: "type-helper text-tertiary mt-2" },
            "macOS may ask you to allow Imbue Studio access to some folders the first time a command touches them.",
          )
        : null,
      m("label", { class: "flex items-start gap-2 mt-4 cursor-pointer" }, [
        m("input", {
          type: "checkbox",
          id: "remote-execution-acknowledge",
          class: "mt-1 shrink-0",
          checked: model.isRemoteExecutionAcknowledged,
          onchange: (event: Event) => {
            model.isRemoteExecutionAcknowledged = (
              event.target as HTMLInputElement
            ).checked;
          },
        }),
        m(
          "span",
          { class: "type-body text-primary" },
          "I understand that an approved workspace's agents will be able to run any command on this computer as me.",
        ),
      ]),
      m("div", { class: "flex items-center justify-end gap-2 mt-5" }, [
        m(
          Button,
          {
            variant: "secondary",
            id: "remote-execution-cancel",
            onclick: () => model.cancelRemoteExecutionWarning(),
          },
          "Cancel",
        ),
        m(
          Button,
          {
            variant: "danger",
            id: "remote-execution-confirm",
            disabled:
              !model.isRemoteExecutionAcknowledged ||
              model.isRemoteExecutionBusy,
            onclick: () => void model.confirmRemoteExecutionWarning(),
          },
          "Allow agents to run commands",
        ),
      ]),
    ],
  );
}

export function thisComputerPanel(model: SettingsModel): m.Children {
  const overview = model.overview;
  if (overview === null) return null;
  const isAllowed = model.isAgentCommandExecutionAllowed();
  return m("section", { id: "this-computer-panel" }, [
    m("h2", { class: "type-heading-lg text-primary mb-2" }, "This computer"),
    m(
      "p",
      { class: "type-body text-secondary mb-3" },
      "Applies to this device only, regardless of which account is signed in.",
    ),
    m(
      "div",
      {
        class:
          "flex items-start justify-between gap-3 py-3 border-b border-subtle",
      },
      [
        m("span", [
          m(
            "span",
            {
              class: "type-body text-primary font-semibold",
              id: "remote-execution-switch-label",
            },
            "Allow agents to run commands on this computer",
          ),
          m(
            "span",
            { class: "block type-helper text-tertiary" },
            "Workspaces still have to ask for this computer one at a time, and you approve each one.",
          ),
        ]),
        renderSwitch({
          id: "remote-execution-switch",
          isOn: isAllowed,
          label: "Allow agents to run commands on this computer",
          isWaiting: model.isRemoteExecutionBusy,
          onFlip: (isOn) => model.requestAgentCommandExecution(isOn),
        }),
      ],
    ),
    isAllowed
      ? m(
          Notice,
          { variant: "warn", id: "remote-execution-on-notice" },
          "Agents in every workspace you approve can run any command on this computer as you. " +
            "Revoke a workspace's access from its Permissions tab, or turn this off to stop them all.",
        )
      : null,
    model.remoteExecutionError !== ""
      ? m(
          "p",
          { class: "type-body text-important mt-3", role: "alert" },
          model.remoteExecutionError,
        )
      : null,
    remoteExecutionWarningModal(model),
  ]);
}
