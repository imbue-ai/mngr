// The waiting modal for the browser sign-in flow: sign-in happens on the
// hosted accounts page in the system browser, so this modal narrates the
// wait, offers a copy-the-link fallback (for browsers that fail to launch),
// and surfaces failures. Rendered by the Shell so any page can trigger it
// through the shared webLogin model.

import m from "mithril";
import { webLogin } from "../../models/webLogin";
import { Button } from "./Button";
import { Icon16 } from "./Icon";
import { Modal } from "./Modal";
import { Spinner } from "./Spinner";

const COPY_FLASH_MS = 1200;

interface WebLoginModalLocalState {
  isCopyConfirmed: boolean;
  copyFlashTimer: number | null;
}

function copyLoginLink(local: WebLoginModalLocalState): void {
  if (!webLogin.loginUrl) return;
  void navigator.clipboard?.writeText(webLogin.loginUrl).then(() => {
    local.isCopyConfirmed = true;
    if (local.copyFlashTimer !== null) window.clearTimeout(local.copyFlashTimer);
    local.copyFlashTimer = window.setTimeout(() => {
      local.isCopyConfirmed = false;
      local.copyFlashTimer = null;
      m.redraw();
    }, COPY_FLASH_MS);
    m.redraw();
  });
}

function waitingBody(local: WebLoginModalLocalState): m.Children {
  return [
    m("div", { class: "flex items-center gap-3 mb-4" }, [
      m(Spinner, { size: "md" }),
      m(
        "p",
        { class: "type-body text-secondary" },
        webLogin.state === "finishing"
          ? "Finishing up…"
          : webLogin.state === "starting"
            ? "Opening the sign-in page in your browser…"
            : "Waiting for you to finish signing in in your browser…",
      ),
    ]),
    webLogin.state === "waiting" && webLogin.loginUrl
      ? m("div", { class: "mb-4" }, [
          m(
            "p",
            { class: "type-helper text-tertiary mb-2" },
            "Browser didn't open? Open this link in a browser on this computer:",
          ),
          // The full link is shown (selectable) and the whole pill copies it
          // on click -- same pattern as the workspace share-link pill.
          m(
            "button",
            {
              id: "web-login-copy-link",
              type: "button",
              class:
                "inline-flex items-start gap-2 max-w-full rounded-md border border-default " +
                "bg-fill-subtle px-3 py-1.5 type-helper font-mono text-primary cursor-pointer " +
                "hover:bg-fill-hover transition-colors text-left",
              style: local.isCopyConfirmed
                ? "border-color: var(--c-success); background-color: var(--c-success-surface);"
                : "",
              "aria-label": "Copy the sign-in link",
              onclick: () => copyLoginLink(local),
            },
            [
              m("span", { id: "web-login-url", class: "break-all select-text" }, webLogin.loginUrl),
              m(Icon16, {
                name: local.isCopyConfirmed ? "check" : "copy",
                extra: "shrink-0 mt-0.5 " + (local.isCopyConfirmed ? "text-primary" : "text-tertiary"),
              }),
            ],
          ),
        ])
      : null,
  ];
}

function errorBody(): m.Children {
  return m(
    "div",
    { class: "rounded-md border border-important/40 bg-important/10 text-important type-body px-3 py-2 mb-4" },
    webLogin.error,
  );
}

export function WebLoginModal(): m.Component {
  const local: WebLoginModalLocalState = { isCopyConfirmed: false, copyFlashTimer: null };
  return {
    onremove() {
      if (local.copyFlashTimer !== null) window.clearTimeout(local.copyFlashTimer);
    },
    view() {
      if (!webLogin.isOpen) return null;
      const isDone = webLogin.state === "done";
      return m(
        Modal,
        { isOpen: true, onClose: () => webLogin.dismiss() },
        m("h2", { class: "type-heading mb-2" }, isDone ? "You're signed in" : "Sign in to Imbue"),
        webLogin.message && !isDone
          ? m("p", { class: "type-body text-secondary mb-4" }, webLogin.message)
          : null,
        isDone
          ? m(
              "p",
              { class: "type-body text-secondary mb-4" },
              `Signed in as ${webLogin.email || "your account"}.`,
            )
          : webLogin.state === "error"
            ? errorBody()
            : waitingBody(local),
        m("div", { class: "flex justify-end gap-2" }, [
          isDone
            ? m(Button, { variant: "primary", id: "web-login-done-btn", onclick: () => webLogin.dismiss() }, "Done")
            : m(
                Button,
                { variant: "secondary", id: "web-login-cancel-btn", onclick: () => webLogin.dismiss() },
                webLogin.state === "error" ? "Close" : "Cancel",
              ),
          webLogin.state === "error"
            ? m(
                Button,
                { variant: "primary", id: "web-login-retry-btn", onclick: () => void webLogin.start(webLogin.message) },
                "Try again",
              )
            : null,
        ]),
      );
    },
  };
}
