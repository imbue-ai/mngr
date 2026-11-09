// The titlebar's account switcher: the button naming the window's account, and
// the popover under it listing every account this window could show instead.
// Every window shows exactly one account; choosing another moves THIS window
// (shell.switchAccount), never another one.

import m from "mithril";
import type { UiAccountEntry } from "../../channel/messages";
import { electronBridge } from "../../electron-bridge";
import { DEVICE_ACCOUNT_LABEL, accountLabelFor } from "../../models/accounts";
import { LOCAL_PROFILE_USER_ID } from "../../models/providers";
import { webLogin } from "../../models/webLogin";
import { Icon16 } from "../components/Icon";
import { TitlebarButton } from "../components/TitlebarButton";
import type { ShellState } from "./shell-state";

// The popover hangs under the button, aligned to its right edge; a few px down
// so it reads as attached to the bar rather than overlapping it.
const MENU_OFFSET_Y = 2;
const MENU_WIDTH_PX = 280;

/** One row of the switcher: an account (signed in or out) or the device. */
export interface AccountSwitcherRow {
  accountId: string;
  label: string;
  /** The line under the label: the email when the label is a display name, or the signed-out note. */
  detail: string;
  isCurrent: boolean;
  /** Whether the account has no session here, so the row offers to sign it back in. */
  isSignedOut: boolean;
}

/** The switcher's rows in order: signed-in accounts, signed-out accounts with workspaces here, the device. */
export function accountSwitcherRows(
  currentAccountId: string,
  signedIn: readonly UiAccountEntry[],
  signedOut: readonly UiAccountEntry[],
): AccountSwitcherRow[] {
  const rows: AccountSwitcherRow[] = [];
  for (const account of signedIn) {
    const label = accountLabelFor(account);
    rows.push({
      accountId: account.user_id,
      label,
      detail: label === account.email ? "" : account.email,
      isCurrent: account.user_id === currentAccountId,
      isSignedOut: false,
    });
  }
  for (const account of signedOut) {
    rows.push({
      accountId: account.user_id,
      label: accountLabelFor(account),
      detail: "Signed out",
      isCurrent: account.user_id === currentAccountId,
      isSignedOut: true,
    });
  }
  rows.push({
    accountId: LOCAL_PROFILE_USER_ID,
    label: DEVICE_ACCOUNT_LABEL,
    detail: "",
    isCurrent: currentAccountId === LOCAL_PROFILE_USER_ID,
    isSignedOut: false,
  });
  return rows;
}

/** The mark the titlebar button carries beside an account that has no session here. */
const SIGNED_OUT_MARK = "(signed out)";

/** What the titlebar button says for the window's account: its name, marked signed out when it has no session here. */
export interface AccountSwitcherLabel {
  name: string;
  isSignedOut: boolean;
}

export function accountSwitcherLabel(shell: ShellState): AccountSwitcherLabel {
  return {
    name: shell.stores.accounts.labelFor(shell.accountId),
    isSignedOut: shell.isAccountSignedOut(),
  };
}

/**
 * The button's copy: the name, which truncates to the button's width, and the
 * signed-out mark as its own node beside it, so a long name never swallows
 * the mark. `nameId` is the name span's DOM id; the raised copy in
 * RaisedTitlebarIcons passes null, since the real button already carries it.
 */
export function accountSwitcherLabelNodes(
  label: AccountSwitcherLabel,
  nameId: string | null,
): m.Children {
  return [
    m(
      "span",
      { id: nameId ?? undefined, class: "type-label truncate max-w-[160px]" },
      label.name,
    ),
    label.isSignedOut
      ? m(
          "span",
          {
            "data-account-signed-out": "true",
            class:
              "type-body font-semibold shrink-0 inline-flex items-center h-6 px-2 rounded-full bg-warning text-white whitespace-nowrap",
          },
          SIGNED_OUT_MARK,
        )
      : null,
  ];
}

export interface AccountSwitcherButtonAttrs {
  shell: ShellState;
  /** The class the titlebar hides its popup icons with while one of their surfaces is open. */
  popupHiddenClass: string;
}

/** The titlebar button: the window's account, and the chevron that opens the popover. */
export function AccountSwitcherButton(): m.Component<AccountSwitcherButtonAttrs> {
  return {
    view(vnode) {
      const { shell, popupHiddenClass } = vnode.attrs;
      return m(
        TitlebarButton,
        {
          id: "account-switcher-btn",
          "aria-label": "Switch account",
          "data-tooltip": "Switch account",
          "aria-expanded": shell.isAccountSwitcherOpen ? "true" : "false",
          "data-account-id": shell.accountId,
          variant: "crumb",
          tone: "muted",
          extra: "gap-1 min-w-0 " + popupHiddenClass,
          onclick: () => {
            if (shell.isAccountSwitcherOpen) shell.closeAccountSwitcher();
            else shell.openAccountSwitcher();
          },
        },
        [
          m(Icon16, { name: "user", extra: "shrink-0" }),
          accountSwitcherLabelNodes(
            accountSwitcherLabel(shell),
            "account-switcher-name",
          ),
          m(Icon16, {
            name: "chevron-down-small",
            extra: "shrink-0 text-tertiary",
          }),
        ],
      );
    },
  };
}

export interface AccountSwitcherMenuAttrs {
  shell: ShellState;
}

/**
 * The "Sign in" at the right of a signed-out account's row: closes the popover
 * and starts the browser sign-in, naming the account the user is picking up
 * again. The window keeps its account; the accounts frame that follows the
 * sign-in is what turns the row (and the titlebar mark) back to signed in.
 */
function signInControl(shell: ShellState, row: AccountSwitcherRow): m.Children {
  return m(
    "button",
    {
      type: "button",
      role: "menuitem",
      "data-account-sign-in": row.accountId,
      disabled: webLogin.isOpen,
      class:
        "shrink-0 h-8 px-2 rounded-md type-label text-accent cursor-pointer hover:bg-fill-hover hover:underline bg-transparent border-0",
      onclick: () => {
        shell.closeAccountSwitcher();
        void webLogin.start(`Sign in as ${row.label} again.`, {
          isClosedOnSignIn: true,
        });
        if (!electronBridge.isDesktop) m.redraw();
      },
    },
    "Sign in",
  );
}

/** The popover: rendered by the Shell while open, dismissed by its backdrop, Escape, or a choice. */
export function AccountSwitcherMenu(): m.Component<AccountSwitcherMenuAttrs> {
  return {
    view(vnode) {
      const { shell } = vnode.attrs;
      if (!shell.isAccountSwitcherOpen) return null;
      const accounts = shell.stores.accounts;
      const rows = accountSwitcherRows(
        shell.accountId,
        accounts.accounts,
        accounts.signedOutAccounts,
      );
      // Measured here rather than stored on the shell: the button's rect only
      // matters while the menu is up, and the titlebar never moves it.
      const button =
        typeof document === "undefined"
          ? null
          : document.getElementById("account-switcher-btn");
      const rect = button?.getBoundingClientRect() ?? null;
      const style =
        rect === null
          ? `right: 8px; top: 40px; width: ${MENU_WIDTH_PX}px;`
          : `left: ${Math.max(8, rect.right - MENU_WIDTH_PX)}px; top: ${rect.bottom + MENU_OFFSET_Y}px; width: ${MENU_WIDTH_PX}px;`;
      return m(
        "div#account-switcher-backdrop",
        {
          class: "fixed inset-0 z-[200]",
          onclick: () => shell.closeAccountSwitcher(),
        },
        m(
          "div#account-switcher-menu",
          {
            role: "menu",
            "aria-label": "Accounts",
            style,
            class:
              "dark absolute flex flex-col gap-0.5 p-1 rounded-lg border border-subtle bg-surface-primary shadow-overlay overflow-hidden",
            onclick: (event: MouseEvent) => event.stopPropagation(),
          },
          [
            ...rows.map((row) =>
              m("div", { class: "flex items-center gap-1" }, [
                m(
                  "button",
                  {
                    type: "button",
                    role: "menuitemradio",
                    "aria-checked": row.isCurrent ? "true" : "false",
                    "data-account-id": row.accountId,
                    disabled: row.isCurrent || shell.isAccountSwitchPending,
                    class:
                      "flex flex-1 min-w-0 items-center gap-2 min-h-8 px-2 py-1 rounded-md type-body text-left border-0 bg-transparent " +
                      (row.isCurrent
                        ? "is-current bg-fill-active text-primary cursor-default"
                        : "text-primary cursor-pointer hover:bg-fill-hover"),
                    onclick: () => void shell.switchAccount(row.accountId),
                  },
                  [
                    m(Icon16, {
                      name: row.isCurrent ? "check" : "user",
                      extra: "shrink-0 " + (row.isCurrent ? "" : "text-tertiary"),
                    }),
                    m("span", { class: "flex-1 min-w-0 flex flex-col" }, [
                      m("span", { class: "truncate" }, row.label),
                      row.detail
                        ? m(
                            "span",
                            { class: "type-helper text-tertiary truncate" },
                            row.detail,
                          )
                        : null,
                    ]),
                  ],
                ),
                row.isSignedOut ? signInControl(shell, row) : null,
              ]),
            ),
            m(
              "button#account-switcher-add",
              {
                type: "button",
                role: "menuitem",
                class:
                  "flex items-center gap-2 h-8 px-2 rounded-md cursor-pointer type-body text-secondary hover:text-primary hover:bg-fill-hover bg-transparent border-0 text-left",
                disabled: webLogin.isOpen,
                onclick: () => {
                  shell.closeAccountSwitcher();
                  // The window keeps its account: the start flow and the create
                  // form take a landed sign-in up themselves, and the new
                  // account appears in this list.
                  void webLogin.start("", { isClosedOnSignIn: true });
                  if (!electronBridge.isDesktop) m.redraw();
                },
              },
              [
                m(Icon16, { name: "plus", extra: "shrink-0" }),
                m("span", "Add account"),
              ],
            ),
          ],
        ),
      );
    },
  };
}
