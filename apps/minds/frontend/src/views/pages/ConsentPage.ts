// Error-reporting consent, shown once per install after the start flow while
// needs_error_reporting_consent is true (from /ui/api/app-status). Its one
// question is a checkbox that starts checked, so continuing keeps reporting on;
// unchecking it turns reporting off. Either answer is recorded and lands home,
// and Settings -> Error reporting changes it later.

import m from "mithril";
import { REPORTING_CONSENT_QUESTION, acknowledgeErrorReportingConsent } from "../../models/onboarding";
import { Button } from "../components/Button";
import { Link } from "../components/Link";

function ConsentPageComponent(): m.Component {
  let isBusy = false;
  let isReportingAllowed = true;

  async function agree(): Promise<void> {
    isBusy = true;
    m.redraw();
    // Even if recording failed, move on: the flag stays unset so the screen
    // simply reappears next launch.
    await acknowledgeErrorReportingConsent(isReportingAllowed);
    m.route.set("/");
  }

  return {
    view() {
      return m("div", { class: "min-h-full flex items-center justify-center" }, [
        m("div", { class: "max-w-md w-full px-6" }, [
          m("h1", { class: "type-heading-lg text-primary mb-4" }, "Help improve Imbue Studio"),
          m("label", { class: "flex items-start gap-2 type-body text-secondary cursor-pointer mb-4" }, [
            m("input", {
              id: "consent-reporting-checkbox",
              type: "checkbox",
              checked: isReportingAllowed,
              class: "mt-1 cursor-pointer",
              onchange: (event: Event) => {
                isReportingAllowed = (event.target as HTMLInputElement).checked;
              },
            }),
            m("span", REPORTING_CONSENT_QUESTION),
          ]),
          m(
            "p",
            { class: "text-tertiary type-helper mb-4" },
            "The reports we collect include diagnostic details about the error and your setup, which can be identifying at times (e.g. an email account). You can change this any time in Settings → Error reporting.",
          ),
          m("p", { class: "text-tertiary type-helper mb-8" }, [
            "Our privacy policy is ",
            m(Link, { href: "https://imbue.com/privacy/", target: "_blank", rel: "noopener" }, "here"),
            ".",
          ]),
          m(
            Button,
            { variant: "primary", block: true, id: "consent-continue", disabled: isBusy, onclick: () => void agree() },
            "Continue",
          ),
        ]),
      ]);
    },
  };
}

export const ConsentPage: m.ComponentTypes = ConsentPageComponent;
