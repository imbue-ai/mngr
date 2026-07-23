// Error-reporting consent screen. Shown just after login (the user is
// authenticated) while MindsConfig.error_reporting_consent_given is False.
// The two toggles mirror the persistent settings on the dedicated Settings
// page; "Include logs" is only revealed once "Report unexpected errors" is
// enabled. "Continue" records the choices and flips the consent-given flag
// (POST /consent), then reloads the app, which now proceeds past this screen
// to the landing content.
import m from "mithril";

import type { ConsentBootIsland } from "../chrome_state";
import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";
import { buttonClasses } from "../ui";

class ConsentController {
  isReportEnabled: boolean;
  isLogsEnabled: boolean;
  isSubmitInFlight = false;

  constructor(reportUnexpectedErrors: boolean, includeLogs: boolean) {
    this.isReportEnabled = reportUnexpectedErrors;
    this.isLogsEnabled = includeLogs;
  }

  setReportEnabled(enabled: boolean): void {
    this.isReportEnabled = enabled;
    // "Include logs" is only meaningful when reporting is on; clear it when
    // reporting is turned back off so a later re-enable starts unchecked.
    if (!enabled) this.isLogsEnabled = false;
    m.redraw();
  }

  async submit(): Promise<void> {
    this.isSubmitInFlight = true;
    m.redraw();
    try {
      await fetch("/consent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          report_unexpected_errors: this.isReportEnabled,
          include_logs: this.isLogsEnabled,
        }),
      });
    } catch {
      // Even if persisting failed, move on; the consent flag stays unset so
      // the screen will simply reappear on the next launch.
    } finally {
      window.location.href = "/";
    }
  }
}

interface ConsentPageAttrs {
  controller: ConsentController;
}

function ConsentPage(): m.Component<ConsentPageAttrs> {
  return {
    view(vnode) {
      const { controller } = vnode.attrs;
      return m("div", { class: "min-h-[calc(100dvh_-_38px)] flex items-center justify-center" }, [
        m("div", { class: "max-w-md w-full px-6" }, [
          m("h1", { class: "type-heading-lg text-primary mb-2" }, "Help improve Minds"),
          m(
            "p",
            { class: "text-secondary type-body mb-6" },
            "Minds can report unexpected errors to Imbue so we can find and fix problems faster. " +
              "You can change these choices any time in Settings.",
          ),
          m("div", { class: "flex flex-col gap-4 mb-8" }, [
            m("label", { class: "flex items-center gap-3 cursor-pointer" }, [
              m("input", {
                type: "checkbox",
                id: "consent-report",
                class: "shrink-0",
                checked: controller.isReportEnabled,
                onchange: (event: Event) =>
                  controller.setReportEnabled((event.target as HTMLInputElement).checked),
              }),
              m("span", [
                m("span", { class: "type-body text-primary font-semibold" }, "Report unexpected errors"),
                m(
                  "span",
                  { class: "block type-helper text-tertiary" },
                  "Send a report to Imbue when something goes wrong.",
                ),
              ]),
            ]),
            controller.isReportEnabled
              ? m("label", { id: "consent-logs-row", class: "flex items-center gap-3 cursor-pointer" }, [
                  m("input", {
                    type: "checkbox",
                    id: "consent-logs",
                    class: "shrink-0",
                    checked: controller.isLogsEnabled,
                    onchange: (event: Event) => {
                      controller.isLogsEnabled = (event.target as HTMLInputElement).checked;
                    },
                  }),
                  m("span", [
                    m("span", { class: "type-body text-primary font-semibold" }, "Include logs"),
                    m(
                      "span",
                      { class: "block type-helper text-tertiary" },
                      "Attach recent log files to help diagnose the problem.",
                    ),
                  ]),
                ])
              : null,
          ]),
          m(
            "button",
            {
              type: "button",
              id: "consent-continue",
              class: `${buttonClasses("primary")} w-full`,
              disabled: controller.isSubmitInFlight,
              onclick: () => void controller.submit(),
            },
            "Continue",
          ),
        ]),
      ]);
    },
  };
}

export function mountConsent(target: Element | null): void {
  const el = requireElement(target, "consent page container");
  const island = readBootState() as ConsentBootIsland;
  if (island.consent === undefined) {
    throw new MindsUIError("consent boot island is missing the consent slice");
  }
  const controller = new ConsentController(island.consent.report_unexpected_errors, island.consent.include_logs);
  mountWithTeardown(el, { view: () => m(ConsentPage, { controller }) });
}
