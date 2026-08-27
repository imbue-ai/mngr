// Get-help page: "have an agent help" (spawns an in-workspace /assist chat
// via POST /help/assist) vs "report a bug" (POST /help/report with sticky
// diagnostics options). Port of Help.jinja onto the HelpModel. Launch
// context (workspace, assist availability, prefilled description) arrives
// via route params or a staged pending-launch from the open_help flow.

import m from "mithril";
import { HelpModel, setPendingHelpLaunch } from "../../models/help";
import { Button } from "../components/Button";
import { Icon16 } from "../components/Icon";
import { Spinner } from "../components/Spinner";

function closeHelpSurface(): void {
  // Mirror ShellState.closeAppOverlay: return to the opener via history, else
  // fall back to the workspace this help was opened over (else Home).
  if (window.history.length > 1) {
    window.history.back();
    return;
  }
  const workspace = m.route.param("workspace");
  m.route.set(workspace ? `/workspace/${workspace}` : "/");
}

function modeChoice(model: HelpModel): m.Children {
  if (model.launch.isAgentReport) return null;
  const isAssistAvailable = model.launch.isAssistAvailable;
  return m("div", { class: "flex flex-col gap-2 mb-4" }, [
    m(
      "label",
      {
        class: `flex items-start gap-3 ${isAssistAvailable ? "cursor-pointer" : "cursor-not-allowed opacity-50"}`,
      },
      [
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
          m(
            "span",
            { class: "type-body text-primary font-semibold" },
            "Have an agent help fix the problem",
          ),
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
      ],
    ),
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
        m(
          "span",
          { class: "type-body text-primary font-semibold" },
          "Report a bug to Imbue",
        ),
        m(
          "span",
          { class: "block type-helper text-tertiary" },
          "We'll investigate!",
        ),
      ]),
    ]),
  ]);
}

function diagnosticsChoice(
  model: HelpModel,
  options: {
    id: string;
    label: string;
    reason: string;
    isChecked: boolean;
    onChange: (value: boolean) => void;
  },
): m.Children {
  // The reason sits under its own checkbox rather than in one block of prose at
  // the end: what a box is for is only useful next to the box.
  return m("div", { class: "flex flex-col" }, [
    m("label", { class: "flex items-center gap-2 cursor-pointer" }, [
      m("input", {
        type: "checkbox",
        id: options.id,
        class: "shrink-0",
        checked: options.isChecked,
        onchange: (event: Event) =>
          options.onChange((event.target as HTMLInputElement).checked),
      }),
      // An unticked box is the thing the warning below is about, so it carries
      // the same error colour rather than leaving the reader to match them up.
      m(
        "span",
        {
          id: `${options.id}-label`,
          class: `type-body ${options.isChecked ? "text-primary" : "text-important"}`,
        },
        options.label,
      ),
    ]),
    m("span", { class: "type-helper text-tertiary ml-6" }, options.reason),
  ]);
}

function workspaceDiagnosticsChoices(model: HelpModel): m.Children {
  // Both are collected from inside the machine, so they only make sense when
  // the report is scoped to one.
  if (!model.launch.workspaceAgentId) return null;
  return [
    // "workspace logs", not "logs": minds' own app logs (backend, Electron, and
    // their rotations) ride on every Sentry event whatever this is set to, so a
    // bare "Include logs" would promise a scope the checkbox cannot honor.
    diagnosticsChoice(model, {
      id: "help-include-logs",
      label: "Include workspace logs",
      reason: "We'll need these to diagnose the issue.",
      isChecked: model.isLogsIncluded,
      onChange: (value) => model.setLogsIncluded(value),
    }),
    diagnosticsChoice(model, {
      id: "help-include-transcript",
      label: "Include recent chats",
      reason: "We'll need these to diagnose the issue.",
      isChecked: model.isTranscriptIncluded,
      onChange: (value) => model.setTranscriptIncluded(value),
    }),
  ];
}

function withheldDiagnosticsWarning(model: HelpModel): m.Children {
  // Only inside a machine, where the boxes are actually offered: outside one
  // there is nothing to withhold and the warning would be a non-sequitur.
  if (!model.launch.workspaceAgentId) return null;
  const withheld = [
    model.isLogsIncluded ? null : "workspace logs",
    model.isTranscriptIncluded ? null : "recent chats",
  ].filter((name): name is string => name !== null);
  if (withheld.length === 0) return null;
  return m(
    "p",
    { id: "help-withheld-warning", class: "type-helper text-important mt-2" },
    // Naming what is missing rather than warning in the abstract: the sentence
    // has to say which box to tick to make it go away.
    `We may not be able to solve this issue without ${withheld.join(" and ")}.`,
  );
}

export function formPhase(model: HelpModel): m.Children {
  return m("div", [
    model.launch.isAgentReport
      ? m("p", { class: "type-body text-primary mb-4" }, [
          model.launch.workspaceName
            ? m.fragment({}, [
                "An agent in machine ",
                m(
                  "span",
                  { class: "font-semibold" },
                  model.launch.workspaceName,
                ),
                " wants to submit this report:",
              ])
            : "An agent in this machine wants to submit this report:",
        ])
      : m(
          "p",
          { class: "type-body text-primary mb-4" },
          "Here's how we can help:",
        ),
    modeChoice(model),
    m(
      "label",
      {
        class: "block type-label text-secondary mb-1",
        for: "help-description",
      },
      "What happened?",
    ),
    m("textarea", {
      id: "help-description",
      rows: 4,
      class:
        "w-full rounded-md border border-default bg-surface-primary p-2 type-body text-primary",
      placeholder: "Describe the problem you ran into...",
      value: model.description,
      oninput: (event: Event) => {
        model.description = (event.target as HTMLTextAreaElement).value;
      },
    }),
    model.mode === "report" || model.launch.isAgentReport
      ? m("div", [
          m("div", { class: "flex flex-col gap-2 mt-4" }, [
            workspaceDiagnosticsChoices(model),
            m("label", { class: "flex items-center gap-2 cursor-pointer" }, [
              m("input", {
                type: "checkbox",
                id: "help-remote-access",
                class: "shrink-0",
                checked: model.isRemoteAccessAllowed,
                onchange: (event: Event) =>
                  model.setRemoteAccessAllowed(
                    (event.target as HTMLInputElement).checked,
                  ),
              }),
              m(
                "span",
                { class: "type-body text-primary" },
                "Allow Imbue to request remote access to help debug",
              ),
            ]),
          ]),
          withheldDiagnosticsWarning(model),
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
      model.mode === "agent" && !model.launch.isAgentReport
        ? "Start agent"
        : "Send report",
    ),
    model.statusMessage !== null
      ? m(
          "p",
          {
            id: "help-status",
            class: `type-helper mt-2 ${model.isStatusError ? "text-important" : "text-tertiary"}`,
          },
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
    m(
      "h2",
      { class: "type-heading text-primary mb-2" },
      "Couldn't start an agent",
    ),
    m("p", { class: "type-body text-secondary mb-4" }, model.agentErrorMessage),
    m(
      Button,
      {
        variant: "primary",
        block: true,
        onclick: () => model.backToReportFromError(),
      },
      "Back to report",
    ),
  ]);
}

export function sentPhase(model: HelpModel): m.Children {
  return m("div", { class: "p-4 text-center" }, [
    m("h2", { class: "type-heading text-primary mb-2" }, "Thanks!"),
    m(
      "p",
      { class: "type-body text-secondary mb-4" },
      "Your report was sent to Imbue.",
    ),
    model.sentEventId !== null
      ? m("div", { class: "mb-4 text-left" }, [
          m("p", { class: "type-label text-secondary mb-1" }, "Report ID"),
          // The same click-to-copy chip as the share-link: the whole box is the
          // button, and the icon flashes a check while the copy is fresh.
          m(
            "button",
            {
              id: "help-report-id",
              type: "button",
              class:
                "flex w-full items-center gap-2 rounded-md border border-default bg-fill-subtle " +
                "px-2 py-1 type-label text-primary font-mono cursor-pointer hover:bg-fill-hover " +
                "transition-colors",
              style: model.isReportIdCopied
                ? "border-color: var(--c-success); background-color: var(--c-success-surface);"
                : "",
              "aria-label": "Copy the report ID",
              onclick: () => void model.copyReportId(),
            },
            [
              m("span", { class: "truncate" }, model.sentEventId),
              m(Icon16, {
                name: model.isReportIdCopied ? "check" : "copy",
                extra: model.isReportIdCopied
                  ? "shrink-0 text-primary"
                  : "shrink-0 text-tertiary",
              }),
            ],
          ),
          m(
            "p",
            { class: "type-helper text-tertiary mt-1" },
            "Click to copy. Quote this ID when you follow up so we can find your report.",
          ),
        ])
      : null,
    m(
      Button,
      { variant: "primary", block: true, onclick: () => closeHelpSurface() },
      "Done",
    ),
  ]);
}

function HelpPageComponent(): m.Component {
  let model: HelpModel | null = null;

  return {
    oninit() {
      // Route params take precedence (titlebar / deep link); a staged
      // pending-launch (open_help flow) fills anything the params omit.
      const workspaceParam = m.route.param("workspace");
      if (
        workspaceParam ||
        m.route.param("description") ||
        m.route.param("agent_report")
      ) {
        setPendingHelpLaunch({
          workspaceAgentId: workspaceParam ?? "",
          isAssistAvailable: m.route.param("assist") === "1",
          description: m.route.param("description") ?? "",
          isAgentReport: m.route.param("agent_report") === "1",
          workspaceName: m.route.param("workspace_name") ?? "",
        });
      }
      model = new HelpModel({
        onClose: closeHelpSurface,
        redraw: () => m.redraw(),
      });
    },
    onremove() {
      model = null;
    },
    view() {
      const activeModel = model;
      if (activeModel === null) return null;
      let body: m.Children;
      if (activeModel.phase === "agent_loading") body = loadingPhase();
      else if (activeModel.phase === "agent_error")
        body = agentErrorPhase(activeModel);
      else if (activeModel.phase === "sent") body = sentPhase(activeModel);
      else body = formPhase(activeModel);
      // Rendered inside the AppOverlay card (Shell), which supplies the card
      // chrome and close X. The title row and the scroller below it are this
      // page's own.
      return [
        titleRow(),
        m("div", { class: "min-h-0 flex-1 overflow-y-auto px-6 py-5" }, body),
      ];
    },
  };
}

/** The page's title row, mirroring the notification feed's header exactly
 * (same 56px row, icon left, hairline below): the two anchored surfaces are
 * one window shown two ways, so their headers sit on one line. 56px centers
 * the row on the same line the panel's close X sits on (see
 * NotificationsPage's title row for the arithmetic). */
export function titleRow(): m.Children {
  return m(
    "div",
    {
      class: "flex h-[56px] shrink-0 items-center border-b border-subtle px-3",
    },
    m(
      "h1",
      { class: "flex items-center gap-1.5 type-label text-primary" },
      [m(Icon16, { name: "bug", size: "sm" }), "Ran into a bug?"],
    ),
  );
}

export const HelpPage: m.ComponentTypes = HelpPageComponent;
