// Error-reporting notice, shown once per install just after login while
// needs_error_reporting_consent is true (from /ui/api/app-status). Reporting
// defaults on during the pre-release; this screen is informational (opt-out
// lives in Settings). "I agree" records the acknowledgement and lands home.

import m from "mithril";
import { acknowledgeErrorReportingConsent } from "../../models/onboarding";
import { Button } from "../components/Button";
import { Link } from "../components/Link";

function ConsentPageComponent(): m.Component {
  let isBusy = false;

  async function agree(): Promise<void> {
    isBusy = true;
    m.redraw();
    // Even if recording failed, move on: the flag stays unset so the notice
    // simply reappears next launch (legacy parity).
    await acknowledgeErrorReportingConsent();
    m.route.set("/");
  }

  return {
    view() {
      return m("div", { class: "min-h-full flex items-center justify-center" }, [
        m("div", { class: "max-w-md w-full px-6" }, [
          m("h1", { class: "type-heading-lg text-primary mb-2" }, "Help improve Minds"),
          m(
            "p",
            { class: "text-secondary type-body mb-4" },
            "While Minds is in its pre-release phase it defaults to reporting errors and sharing logs with Imbue when things go wrong.",
          ),
          m(
            "p",
            { class: "text-tertiary type-helper mb-4" },
            "Privacy and transparency are core values for Imbue. The reports we collect include diagnostic details about the error and your setup, which can be identifying at times (e.g. an email account). You can turn off error reporting in Settings → Error reporting.",
          ),
          m("p", { class: "text-tertiary type-helper mb-8" }, [
            "Our privacy policy is ",
            m(Link, { href: "https://imbue.com/privacy/", target: "_blank", rel: "noopener" }, "here"),
            ".",
          ]),
          m(
            Button,
            { variant: "primary", block: true, id: "consent-continue", disabled: isBusy, onclick: () => void agree() },
            "I agree",
          ),
        ]),
      ]);
    },
  };
}

export const ConsentPage: m.ComponentTypes = ConsentPageComponent;
