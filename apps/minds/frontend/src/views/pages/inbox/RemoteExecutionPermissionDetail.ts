// Remote-execution permission dialog: letting every agent of the asking
// workspace run any command on this computer as the user. All-or-nothing, with
// the same warning Settings > This computer carries, and no Approve at all
// while that switch is off -- the dialog points at the switch instead.

import m from "mithril";
import { getAppContext } from "../../../app-context";
import type {
  InboxModel,
  RemoteExecutionPermissionDetail as Detail,
} from "../../../models/inbox";
import { Icon16 } from "../../components/Icon";
import { Notice } from "../../components/Notice";
import {
  remoteExecutionConsequenceList,
  remoteExecutionWarningBanner,
} from "../../components/RemoteExecutionWarning";
import { PermissionsShell } from "./PermissionsShell";

export interface RemoteExecutionPermissionDetailAttrs {
  model: InboxModel;
  detail: Detail;
}

/** The Settings overlay, opened on the This computer section under the window's account. */
function openThisComputerSettings(): void {
  m.route.set(
    "/settings",
    getAppContext().shell.overlayQuery({ section: "this-computer" }),
  );
}

export function RemoteExecutionPermissionDetailView(): m.Component<RemoteExecutionPermissionDetailAttrs> {
  return {
    view(vnode) {
      const { model, detail } = vnode.attrs;
      return m(PermissionsShell, {
        model,
        headerLabel: "This computer",
        mark: m(Icon16, { name: "triangle-alert", extra: "text-important" }),
        rationale: detail.rationale,
        approveLabel: "Allow commands on this computer",
        progressLabel: "Granting permission...",
        body: m("div", { class: "flex flex-col gap-3" }, [
          remoteExecutionWarningBanner(
            `Approving lets every agent in ${detail.ws_name} run any command on ${detail.device_hostname} as you, ` +
              "without asking first.",
            "remote-execution-dialog-banner",
          ),
          remoteExecutionConsequenceList(
            "remote-execution-dialog-consequences",
          ),
          detail.is_workspace_shared
            ? m(
                Notice,
                { variant: "warn", id: "remote-execution-shared-notice" },
                `${detail.ws_name} is shared. Other people, or visiting agents, may be able to make its agents act, ` +
                  "including on this computer.",
              )
            : null,
          detail.is_setting_enabled
            ? null
            : m(
                Notice,
                {
                  variant: "warn",
                  id: "remote-execution-setting-notice",
                  extra: "flex flex-col gap-2",
                },
                [
                  m(
                    "span",
                    "Agents are not allowed to run commands on this computer yet. Turn on " +
                      "“Allow agents to run commands on this computer” under Settings > This computer, then approve.",
                  ),
                  m("span", { class: "flex items-center gap-3" }, [
                    m(
                      "button",
                      {
                        type: "button",
                        id: "remote-execution-open-settings",
                        class:
                          "type-body text-primary underline cursor-pointer",
                        onclick: openThisComputerSettings,
                      },
                      "Open Settings",
                    ),
                    m(
                      "button",
                      {
                        type: "button",
                        id: "remote-execution-recheck",
                        class:
                          "type-body text-secondary underline cursor-pointer",
                        onclick: () => void model.reloadDetail(),
                      },
                      "I've turned it on",
                    ),
                  ]),
                ],
              ),
        ]),
      });
    },
  };
}
