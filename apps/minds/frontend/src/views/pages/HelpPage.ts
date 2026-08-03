// Get-help page: "have an agent help" (spawns an in-workspace /assist chat
// via POST /help/assist) vs "report a bug" (POST /help/report with sticky
// diagnostics options). Port of Help.jinja onto the HelpModel. Launch
// context (workspace, assist availability, prefilled description) arrives
// via route params or a staged pending-launch from the open_help flow.

import m from "mithril";
import { HelpModel, setPendingHelpLaunch } from "../../models/help";
import { Button } from "../components/Button";
import { Spinner } from "../components/Spinner";

function closeHelpSurface(): void {
  if (window.history.length > 1) window.history.back();
  else m.route.set("/");
}

function modeChoice(model: HelpModel): m.Children {
  if (model.launch.isAgentReport) return null;
  const isAssistAvailable = model.launch.isAssistAvailable;
  return m("div", { class: "flex flex-col gap-2 mb-4" }, [
    m("label", { class: `flex items-start gap-3 ${isAssistAvailable ? "cursor-pointer" : "cursor-not-allowed opacity-50"}` }, [
      m("input", {
        type: "radio",
        name: "help-mode",
        value: "agent",
        class: "mt-1 shrink-0",
        disabled: !isAssistAvailable,
        checked: model.mode === "agent",
        onchange: () => {
          model.mode = "agent";
        },
      }),
      m("span", [
        m("span", { class: "type-body text-primary font-semibold" }, "Have an agent help fix the problem"),
        m(
          "span",
          { class: "block type-helper text-tertiary" },
          isAssistAvailable
            ? "Opens a new chat in this machine that diagnoses the issue and fixes what it can."
            : model.launch.workspaceAgentId
              ? "Available once this machine is responding."
              : "Open a machine to use this.",
        ),
      ]),
    ]),
    m("label", { class: "flex items-start gap-3 cursor-pointer" }, [
      m("input", {
        type: "radio",
        name: "help-mode",
        value: "report",
        class: "mt-1 shrink-0",
        checked: model.mode === "report",
        onchange: () => {
          model.mode = "report";
        },
      }),
      m("span", [
        m("span", { class: "type-body text-primary font-semibold" }, "Report a bug to Imbue"),
        m(
          "span",
          { class: "block type-helper text-tertiary" },
          "Send us a description and diagnostics so we can investigate.",
        ),
      ]),
    ]),
  ]);
}

function formPhase(model: HelpModel): m.Children {
  return m("div", [
    model.launch.isAgentReport
      ? m("p", { class: "type-body text-primary mb-4" }, [
          model.launch.workspaceName
            ? m.fragment({}, [
                "An agent in machine ",
                m("span", { class: "font-semibold" }, model.launch.workspaceName),
                " wants to submit this report:",
              ])
            : "An agent in this machine wants to submit this report:",
        ])
      : m("p", { class: "type-body text-primary mb-4" }, "Here's how we can help:"),
    modeChoice(model),
    m("label", { class: "block type-label text-secondary mb-1", for: "help-description" }, "What happened?"),
    m("textarea", {
      id: "help-description",
      rows: 4,
      class: "w-full rounded-md border border-default bg-surface-primary p-2 type-body text-primary",
      placeholder: "Describe the problem you ran into...",
      value: model.description,
      oninput: (event: Event) => {
        model.description = (event.target as HTMLTextAreaElement).value;
      },
    }),
    model.mode === "report" || model.launch.isAgentReport
      ? m("div", [
          m("div", { class: "flex flex-col gap-2 mt-4" }, [
            m("label", { class: "flex items-center gap-2 cursor-pointer" }, [
              m("input", {
                type: "checkbox",
                id: "help-remote-access",
                class: "shrink-0",
                checked: model.isRemoteAccessAllowed,
                onchange: (event: Event) =>
                  model.setRemoteAccessAllowed((event.target as HTMLInputElement).checked),
              }),
              m("span", { class: "type-body text-primary" }, "Allow Imbue to request remote access to help debug"),
            ]),
          ]),
          m(
            "p",
            { class: "type-helper text-tertiary mt-3" },
            "Recent logs and app diagnostics (app version, signed-in accounts, your list of machines, " +
              "details of the machine you're reporting from, and system info) are always attached to help " +
              "us diagnose the problem. Imbue will never look into your machines without your consent.",
          ),
        ])
      : null,
    m(
      Button,
      {
        variant: "primary",
        block: true,
        id: "help-submit",
        extra: "mt-4",
        disabled: model.isSubmitBusy,
        onclick: () => void model.submit(),
      },
      model.mode === "agent" && !model.launch.isAgentReport ? "Start agent" : "Send report",
    ),
    model.statusMessage !== null
      ? m(
          "p",
          { id: "help-status", class: `type-helper mt-2 ${model.isStatusError ? "text-important" : "text-tertiary"}` },
          model.statusMessage,
        )
      : null,
  ]);
}

function loadingPhase(): m.Children {
  return m("div", { class: "p-8 text-center" }, [
    m("div", { class: "flex justify-center mb-4" }, m(Spinner, { size: "lg" })),
    m("p", { class: "type-body text-primary" }, "Starting an agent to help…"),
    m(
      "p",
      { class: "type-helper text-tertiary mt-1" },
      "Setting up a new chat in this machine. This can take a few seconds.",
    ),
  ]);
}

function agentErrorPhase(model: HelpModel): m.Children {
  return m("div", { class: "p-4 text-center" }, [
    m("h2", { class: "type-heading text-primary mb-2" }, "Couldn't start an agent"),
    m("p", { class: "type-body text-secondary mb-4" }, model.agentErrorMessage),
    m(Button, { variant: "primary", block: true, onclick: () => model.backToReportFromError() }, "Back to report"),
  ]);
}

function sentPhase(model: HelpModel): m.Children {
  return m("div", { class: "p-4 text-center" }, [
    m("h2", { class: "type-heading text-primary mb-2" }, "Thanks!"),
    m("p", { class: "type-body text-secondary mb-4" }, "Your report was sent to Imbue."),
    model.sentEventId !== null
      ? m("div", { class: "mb-4 text-left" }, [
          m("p", { class: "type-label text-secondary mb-1" }, "Report ID"),
          m(
            "code",
            {
              class:
                "block truncate rounded-md border border-default bg-fill-subtle px-2 py-1 type-label text-primary font-mono",
            },
            model.sentEventId,
          ),
          m(
            "p",
            { class: "type-helper text-tertiary mt-1" },
            "Quote this ID when you follow up so we can find your report.",
          ),
        ])
      : null,
    m(Button, { variant: "primary", block: true, onclick: () => closeHelpSurface() }, "Done"),
  ]);
}

function HelpPageComponent(): m.Component {
  let model: HelpModel | null = null;

  return {
    oninit() {
      // Route params take precedence (titlebar / deep link); a staged
      // pending-launch (open_help flow) fills anything the params omit.
      const workspaceParam = m.route.param("workspace");
      if (workspaceParam || m.route.param("description") || m.route.param("agent_report")) {
        setPendingHelpLaunch({
          workspaceAgentId: workspaceParam ?? "",
          isAssistAvailable: m.route.param("assist") === "1",
          description: m.route.param("description") ?? "",
          isAgentReport: m.route.param("agent_report") === "1",
          workspaceName: m.route.param("workspace_name") ?? "",
        });
      }
      model = new HelpModel({ onClose: closeHelpSurface, redraw: () => m.redraw() });
    },
    onremove() {
      model = null;
    },
    view() {
      const activeModel = model;
      if (activeModel === null) return null;
      let body: m.Children;
      if (activeModel.phase === "agent_loading") body = loadingPhase();
      else if (activeModel.phase === "agent_error") body = agentErrorPhase(activeModel);
      else if (activeModel.phase === "sent") body = sentPhase(activeModel);
      else body = formPhase(activeModel);
      return m("div", { class: "flex justify-center p-6" }, [
        m(
          "div",
          {
            class:
              "w-full max-w-md bg-surface-primary rounded-lg border border-default shadow-raised overflow-hidden",
          },
          [
            m("div", { class: "flex items-center justify-between px-4 h-[44px] border-b border-default" }, [
              m("h1", { class: "type-section text-primary" }, "Ran into a bug?"),
            ]),
            m("div", { class: "p-4" }, body),
          ],
        ),
      ]);
    },
  };
}

export const HelpPage: m.ComponentTypes = HelpPageComponent;
