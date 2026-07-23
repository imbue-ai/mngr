// The Manage Accounts surface, shared by the full /accounts page and the
// centered accounts modal. Lists each signed-in account with its workspace
// count, default-account marker, and per-account set-default / log-out
// controls; a "Signed out" indicator surfaces when an account's session
// exists but its provider block was disabled via the providers panel.
//
// Account actions POST the existing routes (/accounts/set-default,
// /accounts/<id>/logout) via fetch and reload this document so the list
// reflects the new state -- the routes answer with a redirect to the full
// accounts page, and following it inside the modal iframe would strand the
// full page in the overlay, so the response body is ignored. (The full page
// previously used plain form posts; it now shares the fetch+reload path.)
//
// "Add account" / "Sign in again" open the sign-in modal through the host
// adapter; in a standalone document that falls back to the full-page
// /auth/login route.
import m from "mithril";

import type { AccountEntryPayload, AccountsBootIsland } from "../chrome_state";
import { getHost } from "../host";
import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";
import { buttonClasses } from "../ui";
import { DialogCloseButton } from "./DialogCloseButton";
import { StatusBadge } from "./StatusBadge";

async function postThenReload(url: string, body: string): Promise<void> {
  try {
    await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
  } finally {
    window.location.reload();
  }
}

// Whether a modal surface exists in this document (Electron bridge, adopted
// modal-host bridge, or chrome.js's in-document modal layer). Without one,
// sign-in falls back to the full-page route.
function openSigninModal(): void {
  if (window.minds !== undefined || window.__mindsOpenModal !== undefined) {
    getHost().openModal({ kind: "signin", returnTo: "/", mode: "signin" });
  } else {
    window.location.href = "/auth/login";
  }
}

function accountRow(account: AccountEntryPayload): m.Children {
  return m(
    "div",
    { key: account.user_id, class: "minds-card p-4 flex items-center justify-between gap-1.5" },
    [
      m("div", [
        m("div", { class: "font-semibold" }, [
          account.email,
          !account.is_enabled
            ? m(
                StatusBadge,
                {
                  variant: "warn",
                  size: "xs",
                  extra: "ml-2",
                  title: "Session was rejected by the server. Sign in again to re-enable.",
                },
                "Signed out",
              )
            : null,
        ]),
        m("div", { class: "type-helper text-tertiary" }, [
          `${account.workspace_count} workspace(s)`,
          account.is_default ? " · Default" : null,
        ]),
      ]),
      m("div", { class: "flex flex-wrap justify-end gap-2" }, [
        !account.is_enabled
          ? m("button", { type: "button", class: buttonClasses("primary"), onclick: openSigninModal }, "Sign in again")
          : null,
        account.is_default
          ? m(
              "span",
              {
                class:
                  "inline-flex items-center justify-center px-3 py-2 rounded-md type-label bg-fill-subtle " +
                  "text-primary border border-default opacity-60 cursor-default",
              },
              "Default",
            )
          : m(
              "button",
              {
                type: "button",
                class: buttonClasses("secondary"),
                onclick: () =>
                  void postThenReload("/accounts/set-default", `user_id=${encodeURIComponent(account.user_id)}`),
              },
              "Set default",
            ),
        m(
          "button",
          {
            type: "button",
            class: buttonClasses("danger"),
            onclick: () => void postThenReload(`/accounts/${encodeURIComponent(account.user_id)}/logout`, ""),
          },
          "Log out",
        ),
      ]),
    ],
  );
}

function accountsBody(accounts: AccountEntryPayload[]): m.Children {
  return [
    accounts.length > 0
      ? m("div", { class: "flex flex-col gap-2" }, accounts.map(accountRow))
      : m("p", { class: "text-secondary" }, "No accounts logged in."),
    m("div", { class: "mt-4" }, [
      m("button", { type: "button", class: buttonClasses("primary"), onclick: openSigninModal }, "Add account"),
    ]),
  ];
}

function readAccountsIsland(): AccountsBootIsland["accounts"] {
  const island = readBootState() as AccountsBootIsland;
  if (island.accounts === undefined) {
    throw new MindsUIError("accounts boot island is missing the accounts slice");
  }
  return island.accounts;
}

// Full-page variant (/accounts), inside the shell's PageContainer.
export function mountAccountsPage(target: Element | null): void {
  const el = requireElement(target, "accounts page container");
  const extras = readAccountsIsland();
  mountWithTeardown(el, {
    view: () => [m("h1", { class: "type-heading text-primary mb-4" }, "Manage Accounts"), accountsBody(extras.accounts)],
  });
}

// Centered-modal variant (/accounts/modal, hosted in the shared overlay
// surface): dim backdrop + scrolling card.
export function mountAccountsModal(target: Element | null): void {
  const el = requireElement(target, "accounts modal container");
  const extras = readAccountsIsland();
  const dismiss = (): void => getHost().closeModal();
  mountWithTeardown(el, {
    view: () =>
      m(
        "div",
        {
          id: "accounts-modal-backdrop",
          class: "fixed inset-0 flex items-center justify-center bg-surface-overlay p-4",
          onclick: (event: Event) => {
            if (event.target === event.currentTarget) dismiss();
          },
        },
        m(
          "div",
          {
            class:
              "relative bg-surface-primary rounded-lg shadow-overlay max-w-lg w-full p-8 max-h-[85vh] " +
              "overflow-y-auto text-left",
          },
          [
            m(DialogCloseButton, { onclick: dismiss }),
            m("h1", { class: "type-heading-lg text-primary mb-6" }, "Manage Accounts"),
            accountsBody(extras.accounts),
          ],
        ),
      ),
  });
}
