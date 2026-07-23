// Get-help modal page. Loaded into the shared modal overlay view in Electron
// (and into the browser ModalHost iframe), so the body is transparent and a
// dim backdrop reveals what's behind it -- matching the inbox modal.
//
// Two options are offered: "have an agent help" and "report a bug to Imbue".
// Agent help is available (and the default) only when opened from a loaded
// workspace; submitting swaps the body to a loading state and POSTs the
// description to /help/assist, which blocks until the /assist chat is created
// and its first message sent, then auto-closes the modal. Report mode POSTs
// the description plus the chosen diagnostics to /help/report; the response
// carries the Sentry event_id (or null when Sentry is inactive), shown in the
// confirmation so the user can quote it.
import m from "mithril";

import type { HelpBootIsland } from "../chrome_state";
import { getHost } from "../host";
import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";
import { buttonClasses } from "../ui";
import { Icon } from "./Icon";
import { Spinner } from "./Spinner";

// Sticky checkbox state: persist each box's checked value across reports so
// a user who files several reports does not have to re-tick the same
// diagnostics each time. Keyed by box name; the description text is
// intentionally NOT persisted.
const STICKY_PREFIX = "minds.help.";

type StickyBox = "help-include-logs" | "help-app-diagnostics" | "help-workspace-details" | "help-remote-access";

function restoreSticky(box: StickyBox, fallback: boolean): boolean {
  try {
    const stored = window.localStorage.getItem(STICKY_PREFIX + box);
    if (stored !== null) return stored === "true";
  } catch {
    // Unreadable localStorage just means the value isn't sticky.
  }
  return fallback;
}

function persistSticky(box: StickyBox, checked: boolean): void {
  try {
    window.localStorage.setItem(STICKY_PREFIX + box, checked ? "true" : "false");
  } catch {
    // A full/blocked localStorage just means the value isn't sticky.
  }
}

type HelpPhase = "form" | "loading" | "error" | "sent";

class HelpController {
  readonly extras: HelpBootIsland["help"];
  phase: HelpPhase = "form";
  // Agent help is the default when available -- except when a description was
  // pre-filled (an in-workspace /assist agent escalated its diagnosis): that
  // flow must land on the report form for a human to review and submit.
  mode: "agent" | "report";
  description: string;
  isIncludeLogsChecked: boolean;
  isAppDiagnosticsChecked: boolean;
  isWorkspaceDetailsChecked: boolean;
  isRemoteAccessChecked: boolean;
  statusText: string | null = null;
  isStatusError = false;
  isSubmitDisabled = false;
  agentErrorMessage = "";
  sentEventId: string | null = null;
  copyButtonLabel = "Copy";

  constructor(extras: HelpBootIsland["help"]) {
    this.extras = extras;
    this.mode = extras.assist_available && extras.description === "" && !extras.is_agent_report ? "agent" : "report";
    this.description = extras.description;
    this.isIncludeLogsChecked = restoreSticky("help-include-logs", false);
    this.isAppDiagnosticsChecked = restoreSticky("help-app-diagnostics", false);
    this.isWorkspaceDetailsChecked = restoreSticky("help-workspace-details", false);
    this.isRemoteAccessChecked = restoreSticky("help-remote-access", false);
  }

  close(): void {
    if (window.minds !== undefined) getHost().closeModal();
    else window.history.back();
  }

  submit(): void {
    const description = this.description.trim();
    if (description === "") {
      this.statusText = "Please describe the problem first.";
      this.isStatusError = true;
      m.redraw();
      return;
    }
    if (this.mode === "agent") {
      void this.submitAgentHelp(description);
      return;
    }
    void this.submitReport(description);
  }

  // Agent-help swaps the whole modal body to a loading state while the
  // request blocks (~15s). A failed spawn swaps to an error state with the
  // server's explanation plus a "Back to report" button instead of stranding
  // the user on the spinner.
  private async submitAgentHelp(description: string): Promise<void> {
    this.isSubmitDisabled = true;
    this.phase = "loading";
    m.redraw();
    try {
      const response = await fetch("/help/assist", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ description, workspace_agent_id: this.extras.workspace_agent_id }),
      });
      const data = (await response.json().catch(() => ({}))) as { error?: string };
      if (response.ok) {
        // Creation + first message are done and the new chat tab has already
        // auto-opened in the workspace, so just dismiss the modal.
        this.close();
      } else {
        this.showAgentError(data.error !== undefined && data.error !== "" ? data.error : "Could not start an agent.");
      }
    } catch {
      this.showAgentError("Network error starting the agent.");
    }
  }

  private showAgentError(message: string): void {
    this.phase = "error";
    this.agentErrorMessage = message;
    m.redraw();
  }

  backToReportFromError(): void {
    this.phase = "form";
    this.isSubmitDisabled = false;
    this.mode = "report";
    m.redraw();
  }

  private async submitReport(description: string): Promise<void> {
    this.isSubmitDisabled = true;
    this.statusText = "Sending...";
    this.isStatusError = false;
    m.redraw();
    try {
      const response = await fetch("/help/report", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          description,
          include_logs: this.extras.include_logs_setting || this.isIncludeLogsChecked,
          include_app_diagnostics: this.isAppDiagnosticsChecked,
          include_workspace_details: this.extras.workspace_agent_id !== "" && this.isWorkspaceDetailsChecked,
          remote_access: this.isRemoteAccessChecked,
          workspace_agent_id: this.extras.workspace_agent_id,
        }),
      });
      const data = (await response.json().catch(() => ({}))) as { error?: string; event_id?: string | null };
      if (response.ok) {
        this.phase = "sent";
        this.sentEventId = data.event_id !== undefined && data.event_id !== null && data.event_id !== "" ? data.event_id : null;
        this.statusText = null;
      } else {
        this.isSubmitDisabled = false;
        this.statusText = data.error !== undefined && data.error !== "" ? data.error : "Could not send the report.";
        this.isStatusError = true;
      }
    } catch {
      this.isSubmitDisabled = false;
      this.statusText = "Network error sending the report.";
      this.isStatusError = true;
    }
    m.redraw();
  }

  copyEventId(): void {
    if (this.sentEventId === null) return;
    try {
      void navigator.clipboard?.writeText(this.sentEventId);
      this.copyButtonLabel = "Copied";
      m.redraw();
      setTimeout(() => {
        this.copyButtonLabel = "Copy";
        m.redraw();
      }, 1500);
    } catch {
      // Clipboard unavailable; the id is still visible to copy manually.
    }
  }
}

function stickyCheckbox(
  controller: HelpController,
  id: StickyBox,
  label: string,
  isChecked: boolean,
  setChecked: (value: boolean) => void,
): m.Children {
  return m("label", { class: "flex items-center gap-2 cursor-pointer" }, [
    m("input", {
      type: "checkbox",
      id,
      class: "shrink-0",
      checked: isChecked,
      onchange: (event: Event) => {
        const checked = (event.target as HTMLInputElement).checked;
        setChecked(checked);
        persistSticky(id, checked);
      },
    }),
    m("span", { class: "type-body text-primary" }, label),
  ]);
}

function modeChoice(controller: HelpController): m.Children {
  const { extras } = controller;
  return m("div", { class: "flex flex-col gap-2 mb-4" }, [
    extras.assist_available
      ? m("label", { class: "flex items-start gap-3 cursor-pointer" }, [
          m("input", {
            type: "radio",
            name: "help-mode",
            value: "agent",
            class: "mt-1 shrink-0",
            checked: controller.mode === "agent",
            onchange: () => {
              controller.mode = "agent";
              m.redraw();
            },
          }),
          m("span", [
            m("span", { class: "type-body text-primary font-semibold" }, "Have an agent help fix the problem"),
            m(
              "span",
              { class: "block type-helper text-tertiary" },
              "Opens a new chat in this workspace that diagnoses the issue and fixes what it can.",
            ),
          ]),
        ])
      : m("label", { class: "flex items-start gap-3 cursor-not-allowed opacity-50" }, [
          m("input", { type: "radio", name: "help-mode", value: "agent", class: "mt-1 shrink-0", disabled: true }),
          m("span", [
            m("span", { class: "type-body text-primary font-semibold" }, "Have an agent help fix the problem"),
            m(
              "span",
              { class: "block type-helper text-tertiary" },
              extras.workspace_agent_id !== ""
                ? "Available once this workspace is responding."
                : "Open a workspace to use this.",
            ),
          ]),
        ]),
    m("label", { class: "flex items-start gap-3 cursor-pointer" }, [
      m("input", {
        type: "radio",
        name: "help-mode",
        value: "report",
        class: "mt-1 shrink-0",
        checked: controller.mode === "report",
        onchange: () => {
          controller.mode = "report";
          m.redraw();
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

function formPhase(controller: HelpController): m.Children {
  const { extras } = controller;
  return m("div", { id: "help-form", class: "p-4" }, [
    extras.is_agent_report
      ? m(
          "p",
          { class: "type-body text-primary mb-4" },
          extras.workspace_name !== ""
            ? ["An agent in workspace ", m("span", { class: "font-semibold" }, extras.workspace_name), " wants to submit this report:"]
            : "An agent in this workspace wants to submit this report:",
        )
      : m("p", { class: "type-body text-primary mb-4" }, "Here's how we can help:"),
    // The mode choice is hidden for an agent escalation (we are already
    // reporting); the controller then stays in report mode.
    extras.is_agent_report ? null : modeChoice(controller),
    m("label", { class: "block type-label text-secondary mb-1", for: "help-description" }, "What happened?"),
    m("textarea", {
      id: "help-description",
      rows: 4,
      class: "w-full rounded-md border border-default bg-surface-primary p-2 type-body text-primary",
      placeholder: "Describe the problem you ran into...",
      value: controller.description,
      oninput: (event: Event) => {
        controller.description = (event.target as HTMLTextAreaElement).value;
      },
    }),
    // Report-only options. Hidden in agent-help mode, where the agent
    // gathers its own context.
    controller.mode === "report"
      ? m("div", { id: "help-report-options" }, [
          m("p", { class: "type-label text-secondary mt-4 mb-2" }, "Include with the report"),
          m("div", { class: "flex flex-col gap-2" }, [
            extras.include_logs_setting
              ? null
              : stickyCheckbox(controller, "help-include-logs", "Logs", controller.isIncludeLogsChecked, (value) => {
                  controller.isIncludeLogsChecked = value;
                }),
            stickyCheckbox(
              controller,
              "help-app-diagnostics",
              "App diagnostics (versions, accounts, workspaces, system)",
              controller.isAppDiagnosticsChecked,
              (value) => {
                controller.isAppDiagnosticsChecked = value;
              },
            ),
            extras.workspace_agent_id !== ""
              ? stickyCheckbox(
                  controller,
                  "help-workspace-details",
                  "Details about the current workspace",
                  controller.isWorkspaceDetailsChecked,
                  (value) => {
                    controller.isWorkspaceDetailsChecked = value;
                  },
                )
              : null,
            stickyCheckbox(
              controller,
              "help-remote-access",
              "Allow Imbue to request remote access to help debug",
              controller.isRemoteAccessChecked,
              (value) => {
                controller.isRemoteAccessChecked = value;
              },
            ),
          ]),
        ])
      : null,
    m(
      "button",
      {
        type: "button",
        id: "help-submit",
        class: `${buttonClasses("primary")} w-full mt-4`,
        disabled: controller.isSubmitDisabled,
        onclick: () => controller.submit(),
      },
      controller.mode === "agent" ? "Start agent" : "Send report",
    ),
    controller.statusText !== null
      ? m(
          "p",
          { id: "help-status", class: `type-helper ${controller.isStatusError ? "text-danger" : "text-tertiary"} mt-2` },
          controller.statusText,
        )
      : null,
  ]);
}

function loadingPhase(): m.Children {
  return m("div", { id: "help-loading", class: "p-8 text-center" }, [
    m("div", { class: "flex justify-center mb-4" }, m(Spinner, { size: "lg" })),
    m("p", { class: "type-body text-primary" }, "Starting an agent to help…"),
    m(
      "p",
      { class: "type-helper text-tertiary mt-1" },
      "Setting up a new chat in this workspace. This can take a few seconds.",
    ),
  ]);
}

function errorPhase(controller: HelpController): m.Children {
  return m("div", { id: "help-error", class: "p-4 text-center" }, [
    m("h2", { class: "type-heading text-primary mb-2" }, "Couldn’t start an agent"),
    m("p", { id: "help-error-body", class: "type-body text-secondary mb-4" }, controller.agentErrorMessage),
    m(
      "button",
      {
        type: "button",
        id: "help-error-back-btn",
        class: `${buttonClasses("primary")} w-full`,
        onclick: () => controller.backToReportFromError(),
      },
      "Back to report",
    ),
  ]);
}

function sentPhase(controller: HelpController): m.Children {
  return m("div", { id: "help-sent", class: "p-4 text-center" }, [
    m("h2", { id: "help-sent-title", class: "type-heading text-primary mb-2" }, "Thanks!"),
    m("p", { id: "help-sent-body", class: "type-body text-secondary mb-4" }, "Your report was sent to Imbue."),
    controller.sentEventId !== null
      ? m("div", { id: "help-event-id-row", class: "mb-4 text-left" }, [
          m("p", { class: "type-label text-secondary mb-1" }, "Report ID"),
          m("div", { class: "flex items-center gap-2" }, [
            m(
              "code",
              {
                id: "help-event-id",
                class:
                  "flex-1 min-w-0 truncate rounded-md border border-default bg-fill-subtle px-2 py-1 " +
                  "type-label text-primary font-mono",
              },
              controller.sentEventId,
            ),
            m(
              "button",
              { type: "button", id: "help-copy-id-btn", class: buttonClasses("secondary"), onclick: () => controller.copyEventId() },
              controller.copyButtonLabel,
            ),
          ]),
          m(
            "p",
            { class: "type-helper text-tertiary mt-1" },
            "Quote this ID when you follow up so we can find your report.",
          ),
        ])
      : null,
    m(
      "button",
      { type: "button", id: "help-done-btn", class: `${buttonClasses("primary")} w-full`, onclick: () => controller.close() },
      "Done",
    ),
  ]);
}

interface HelpModalAttrs {
  controller: HelpController;
}

function HelpModal(): m.Component<HelpModalAttrs> {
  return {
    view(vnode) {
      const { controller } = vnode.attrs;
      return m(
        "div",
        {
          id: "help-backdrop",
          class: "fixed inset-0 bg-surface-overlay flex items-center justify-center p-4",
          onclick: (event: Event) => {
            if (event.target === event.currentTarget) controller.close();
          },
        },
        m(
          "div",
          {
            id: "help-dialog",
            class:
              "relative w-full max-w-md max-h-[90vh] overflow-y-auto bg-surface-primary rounded-lg " +
              "border border-default shadow-overlay",
          },
          [
            m("div", { class: "flex items-center justify-between px-4 h-[44px] border-b border-default" }, [
              m("h1", { class: "type-section text-primary" }, "Ran into a bug?"),
              m(
                "button",
                {
                  type: "button",
                  "aria-label": "Close",
                  id: "help-close-btn",
                  "data-tooltip": "Close",
                  class:
                    "inline-flex items-center justify-center w-6 h-6 rounded-md text-tertiary " +
                    "hover:text-primary hover:bg-fill-hover cursor-pointer",
                  onclick: () => controller.close(),
                },
                m(Icon, { name: "close" }),
              ),
            ]),
            controller.phase === "form" ? formPhase(controller) : null,
            controller.phase === "loading" ? loadingPhase() : null,
            controller.phase === "error" ? errorPhase(controller) : null,
            controller.phase === "sent" ? sentPhase(controller) : null,
          ],
        ),
      );
    },
  };
}

export function mountHelpModal(target: Element | null): void {
  const el = requireElement(target, "help modal container");
  const island = readBootState() as HelpBootIsland;
  if (island.help === undefined) {
    throw new MindsUIError("help boot island is missing the help slice");
  }
  const controller = new HelpController(island.help);
  // Escape dismisses (the Electron main process also handles Escape for any
  // modal-view page; this covers the browser iframe / standalone cases).
  const onKeydown = (event: KeyboardEvent): void => {
    if (event.key === "Escape") controller.close();
  };
  document.addEventListener("keydown", onKeydown);
  mountWithTeardown(el, {
    view: () => m(HelpModal, { controller }),
    onremove: () => document.removeEventListener("keydown", onKeydown),
  });
}
