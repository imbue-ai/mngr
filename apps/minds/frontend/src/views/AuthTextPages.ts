// The two static auth text pages: the login prompt (points the user at the
// one-time-code URL printed in the server's terminal) and the
// authentication-failure page (a used or malformed one-time code). Both are
// pure text inside the PageNarrowContainer shell; the failure message rides
// the ``auth_error`` island slice.
//
// (The /login JS-redirect stub is deliberately NOT a component: it must stay
// a minimal no-Tailwind, no-bundle document so Chromium's pre-render cannot
// consume the one-time code -- see LoginRedirect.jinja.)
import m from "mithril";

import type { AuthErrorBootIsland } from "../chrome_state";
import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";

function LoginPromptPage(): m.Component {
  return {
    view() {
      return [
        m("h1", { class: "type-heading text-primary" }, "Sign in to Minds"),
        m("p", { class: "type-helper text-tertiary mt-1.5 mb-4" }, "Use the login URL printed in the terminal."),
        m(
          "p",
          { class: "text-primary leading-relaxed" },
          "Each login URL can only be used once. If you've already used yours, restart the server to " +
            "generate a new one.",
        ),
      ];
    },
  };
}

export function mountLoginPrompt(target: Element | null): void {
  const el = requireElement(target, "login prompt container");
  mountWithTeardown(el, LoginPromptPage);
}

interface AuthErrorPageAttrs {
  message: string;
}

function AuthErrorPage(): m.Component<AuthErrorPageAttrs> {
  return {
    view(vnode) {
      return [
        m("h1", { class: "type-heading text-primary" }, "Authentication Failed"),
        m("p", { class: "mt-2 text-primary" }, vnode.attrs.message),
        m(
          "p",
          { class: "mt-2 text-primary" },
          "Each login URL can only be used once. Please use the login URL printed in the terminal " +
            "where the server is running, or restart the server to generate a new one.",
        ),
      ];
    },
  };
}

export function mountAuthError(target: Element | null): void {
  const el = requireElement(target, "auth error container");
  const island = readBootState() as AuthErrorBootIsland;
  if (island.auth_error === undefined) {
    throw new MindsUIError("auth-error boot island is missing the auth_error slice");
  }
  const message = island.auth_error.message;
  mountWithTeardown(el, { view: () => m(AuthErrorPage, { message }) });
}
