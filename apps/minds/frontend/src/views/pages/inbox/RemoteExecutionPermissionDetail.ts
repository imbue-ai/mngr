// Remote-execution permission dialog: letting every agent of the asking
// workspace run commands on this computer as the user, either any command or
// one program. Any command, or a program that can run anything (a shell, a
// runtime), gets the same warning Settings > This computer carries; one
// ordinary program gets a milder notice naming the file it resolves to. No
// Approve at all while that switch is off -- the dialog points at the switch
// instead.

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

/** Whether the request deserves the any-command warning: it is for any command, or for a program that runs anything. */
function isUnbounded(detail: Detail): boolean {
  return detail.program === null || detail.is_program_interpreter;
}

function warningBanner(detail: Detail): m.Children {
  const text =
    detail.program === null
      ? `Approving lets every agent in ${detail.ws_name} run any command on ${detail.device_hostname} as you, ` +
        "without asking first."
      : `${detail.program} can run anything. Approving lets every agent in ${detail.ws_name} run any command on ` +
        `${detail.device_hostname} as you, without asking first.`;
  return remoteExecutionWarningBanner(text, "remote-execution-dialog-banner");
}

/** The program row: what was asked for, and the file it is on this computer right now. */
function programRow(detail: Detail): m.Children {
  if (detail.program === null) return null;
  const resolution =
    detail.resolved_program_path === null
      ? "Not on this computer yet. Approving now lets it run once it exists."
      : detail.resolved_program_path === detail.program
        ? "Found on this computer."
        : `On this computer: ${detail.resolved_program_path}`;
  return m(
    "div",
    { id: "remote-execution-dialog-program", class: "flex flex-col gap-1" },
    [
      m("p", { class: "type-helper text-tertiary" }, "Program"),
      m(
        "p",
        { class: "type-body text-primary font-mono wrap-anywhere" },
        detail.program,
      ),
      m("p", { class: "type-helper text-secondary" }, resolution),
    ],
  );
}

export function RemoteExecutionPermissionDetailView(): m.Component<RemoteExecutionPermissionDetailAttrs> {
  return {
    view(vnode) {
      const { model, detail } = vnode.attrs;
      const unbounded = isUnbounded(detail);
      return m(PermissionsShell, {
        model,
        headerLabel: "This computer",
        mark: m(Icon16, {
          name: "triangle-alert",
          extra: unbounded ? "text-important" : "text-secondary",
        }),
        rationale: detail.rationale,
        approveLabel: unbounded
          ? "Allow commands on this computer"
          : "Allow this program on this computer",
        progressLabel: "Granting permission...",
        body: m("div", { class: "flex flex-col gap-3" }, [
          unbounded
            ? warningBanner(detail)
            : m(
                Notice,
                {
                  variant: "warn",
                  id: "remote-execution-dialog-program-notice",
                },
                `Approving lets every agent in ${detail.ws_name} run this one program on ${detail.device_hostname} ` +
                  "as you, with any arguments, without asking first. Anything the program can do, they can do.",
              ),
          programRow(detail),
          unbounded
            ? remoteExecutionConsequenceList(
                "remote-execution-dialog-consequences",
              )
            : null,
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
