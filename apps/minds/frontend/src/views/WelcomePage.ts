// Welcome/splash page for first-time users. Offers Sign Up / Log In and a
// "continue without an account" link. The user must resolve one of the three
// before moving on: the titlebar home button is hidden here, and the home
// route bounces back to this splash until an account exists or the skip was
// chosen.
//
// Sign Up / Log In open the centered sign-in modal through the host adapter
// (Electron overlay view, or the chrome shell's in-document modal layer in
// browser mode). ``mode`` picks which AuthForm tab leads; ``returnTo: '/'``
// lands a successful sign-in on the home screen. In a standalone document
// (no bridge, no modal layer) the links fall back to the full-page /auth/*
// routes on their own -- the anchors' hrefs stay live.
//
// The skip link goes through /welcome/skip (which records the choice, then
// redirects to /) rather than straight to /create: recording the choice
// stops the home route's bounce back to this splash, and the landing handler
// still shows the error-reporting consent screen first (when unanswered), so
// reporting is offered to account-less users too.
import m from "mithril";

import { getHost } from "../host";
import { mountWithTeardown, requireElement } from "../mount";
import { buttonClasses } from "../ui";

// Whether a modal surface exists in this document: the Electron preload
// bridge, or chrome.js's in-document modal layer (registered at shell boot).
// Without either, clicks fall through to the anchors' full-page /auth/*
// routes.
function hasModalSurface(): boolean {
  return window.minds !== undefined || window.__mindsOpenModal !== undefined;
}

function openSigninModal(event: Event, mode: "signin" | "signup"): void {
  if (!hasModalSurface()) return;
  event.preventDefault();
  getHost().openModal({ kind: "signin", returnTo: "/", mode });
}

const SKIP_LINK_CLASSES =
  buttonClasses("ghost") +
  " !p-0 !bg-transparent !type-helper !text-tertiary hover:!bg-transparent hover:!text-primary hover:underline";

function WelcomePage(): m.Component {
  return {
    view() {
      return m("div", { class: "min-h-[calc(100dvh_-_38px)] flex items-center justify-center" }, [
        m("div", { class: "max-w-sm w-full px-6 text-center" }, [
          m("h1", { class: "type-heading-lg text-primary mb-2" }, "Welcome to Minds"),
          m("p", { class: "text-secondary type-body mb-8" }, "Run persistent, autonomous AI agents"),
          m("div", { class: "flex flex-col gap-3 mb-6" }, [
            m(
              "a",
              {
                id: "welcome-signup-btn",
                href: "/auth/signup",
                class: `${buttonClasses("primary")} w-full`,
                onclick: (event: Event) => openSigninModal(event, "signup"),
              },
              "Sign Up",
            ),
            m(
              "a",
              {
                id: "welcome-login-btn",
                href: "/auth/login",
                class: `${buttonClasses("secondary")} w-full`,
                onclick: (event: Event) => openSigninModal(event, "signin"),
              },
              "Log In",
            ),
          ]),
          m("a", { id: "skip-account-btn", href: "/welcome/skip", class: SKIP_LINK_CLASSES }, "Continue without an account"),
        ]),
      ]);
    },
  };
}

export function mountWelcome(target: Element | null): void {
  const el = requireElement(target, "welcome page container");
  // Self-advance the splash once an account exists: a sign-in can complete
  // without this page navigating (e.g. an OAuth flow finished in the external
  // browser after the sign-in modal was dismissed). Watch the shared chrome
  // event stream for has_accounts and land on home; the landing route no
  // longer bounces back once an account is present. The subscription has no
  // teardown: /welcome is not a hub page (it always leaves via a full
  // navigation), and the guard makes a late event a no-op.
  let hasNavigated = false;
  getHost().onChromeEvent((event) => {
    if (hasNavigated) return;
    if (event.type === "workspaces" && event.has_accounts === true) {
      hasNavigated = true;
      window.location.href = "/";
    }
  });
  mountWithTeardown(el, WelcomePage);
}
