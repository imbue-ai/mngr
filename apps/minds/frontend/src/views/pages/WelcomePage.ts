// First-run splash: the three-choice account gate. Sign Up is the one
// primary action (most users landing on the first-run splash need an account
// rather than a session); signing in is the "Already have an account? Sign
// in" link below it; the third choice is "Continue without an account",
// which records the skip so the first-run routing stops returning here.
// Sign Up / Sign in both launch the hosted accounts page in the system
// browser (the shared webLogin flow); the page self-advances to home the
// moment an account appears -- every channel `accounts` message triggers a
// redraw, so the onupdate hook re-checks the accounts store.

import m from "mithril";
import { getAppContext } from "../../app-context";
import { skipAccountSetup } from "../../models/onboarding";
import { webLogin } from "../../models/webLogin";
import { Button } from "../components/Button";

function WelcomePageComponent(): m.Component {
  let isBusy = false;
  let hasAdvanced = false;

  async function continueWithoutAccount(): Promise<void> {
    isBusy = true;
    m.redraw();
    await skipAccountSetup();
    m.route.set("/");
  }

  // A sign-in can complete without this page navigating (OAuth finishing in
  // an external browser); advance once the account list reports one. Every
  // channel `accounts` message triggers a redraw, so onupdate re-checks --
  // the navigation lives in lifecycle hooks to keep view() pure.
  function advanceIfSignedIn(): void {
    if (!hasAdvanced && getAppContext().stores.accounts.hasAccounts) {
      hasAdvanced = true;
      m.route.set("/");
    }
  }

  return {
    oninit: advanceIfSignedIn,
    onupdate: advanceIfSignedIn,
    view() {
      return m("div", { class: "min-h-full flex items-center justify-center" }, [
        m("div", { class: "max-w-sm w-full px-6 text-center" }, [
          m("h1", { class: "type-heading-lg text-primary mb-2" }, "Welcome to Minds"),
          m("p", { class: "text-secondary type-body mb-8" }, "Run persistent, autonomous AI agents."),
          m(
            Button,
            {
              variant: "primary",
              size: "lg",
              block: true,
              id: "welcome-signup-btn",
              onclick: () => void webLogin.start(),
            },
            "Sign Up",
          ),
          m("div", { class: "mt-4 mb-8 type-body text-secondary" }, [
            "Already have an account? ",
            // A button (not an href-less anchor) so it is keyboard-focusable;
            // Tailwind preflight strips button chrome, so it renders as a link.
            m(
              "button",
              {
                type: "button",
                id: "welcome-login-btn",
                class: "font-medium text-primary hover:underline cursor-pointer",
                onclick: () => void webLogin.start(),
              },
              "Sign in",
            ),
          ]),
          m(
            Button,
            {
              variant: "ghost",
              id: "skip-account-btn",
              disabled: isBusy,
              extra:
                "!p-0 !bg-transparent !type-helper !text-tertiary hover:!bg-transparent hover:!text-primary hover:underline",
              onclick: () => void continueWithoutAccount(),
            },
            "Continue without an account",
          ),
        ]),
      ]);
    },
  };
}

export const WelcomePage: m.ComponentTypes = WelcomePageComponent;
