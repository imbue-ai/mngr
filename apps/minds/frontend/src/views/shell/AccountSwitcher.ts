// The account menu: hung from the bar's account button, listing every account
// this window could show, its own among them and marked. Every window shows
// exactly one account; choosing another moves THIS window
// (shell.switchAccount), never another one. The button itself is the bar's
// (Titlebar.ts, trail.ts).

import m from "mithril";
import { Icon16 } from "../components/Icon";
import type { UiAccountEntry } from "../../channel/messages";
import { electronBridge } from "../../electron-bridge";
import { DEVICE_ACCOUNT_LABEL, accountEmailLabelFor } from "../../models/accounts";
import { LOCAL_PROFILE_USER_ID } from "../../models/providers";
import { webLogin } from "../../models/webLogin";
import { RaisedTitlebarIcons, openTitlebarPopup, titlebarAnchors } from "./RaisedTitlebarIcons";
import {
  MENU_CLASS,
  MENU_ROW_CLASS,
  MenuHighlight,
  menuActionRow,
  menuCurrentCheck,
  menuDivider,
  menuStyle,
} from "./menu-kit";
import type { ShellState } from "./shell-state";
import { ACCOUNT_BUTTON_ID } from "./trail";

/** One row of the menu: an account (signed in or out) or the device. */
export interface AccountSwitcherRow {
  accountId: string;
  /** The account's email, or "This device". */
  label: string;
  /** Whether the account has no session here, so the row says so and offers to sign it back in. */
  isSignedOut: boolean;
}

/** The menu's rows in order: the signed-in accounts, the signed-out accounts with workspaces here, and the device.
 * The window's own account is always one of them: when neither list names it (signed out, with no workspace left
 * here), it is listed by its id after the signed-out accounts. */
export function accountMenuRows(
  currentAccountId: string,
  signedIn: readonly UiAccountEntry[],
  signedOut: readonly UiAccountEntry[],
): AccountSwitcherRow[] {
  const isCurrentNamed =
    currentAccountId === LOCAL_PROFILE_USER_ID ||
    [...signedIn, ...signedOut].some((account) => account.user_id === currentAccountId);
  return [
    ...signedIn.map((account) => ({
      accountId: account.user_id,
      label: accountEmailLabelFor(account),
      isSignedOut: false,
    })),
    ...signedOut.map((account) => ({
      accountId: account.user_id,
      label: accountEmailLabelFor(account),
      isSignedOut: true,
    })),
    ...(isCurrentNamed ? [] : [{ accountId: currentAccountId, label: currentAccountId, isSignedOut: true }]),
    { accountId: LOCAL_PROFILE_USER_ID, label: DEVICE_ACCOUNT_LABEL, isSignedOut: false },
  ];
}

/** The accounts the window could move to: the menu's rows without the window's own. */
export function accountSwitcherRows(
  currentAccountId: string,
  signedIn: readonly UiAccountEntry[],
  signedOut: readonly UiAccountEntry[],
): AccountSwitcherRow[] {
  return accountMenuRows(currentAccountId, signedIn, signedOut).filter((row) => row.accountId !== currentAccountId);
}

export interface AccountSwitcherMenuAttrs {
  shell: ShellState;
}

/**
 * The "Sign in" at the right of a signed-out account's row: closes the menu
 * and starts the browser sign-in, naming the account the user is picking up
 * again. The window keeps its account; the accounts frame that follows the
 * sign-in is what turns the row (and the bar's mark) back to signed in.
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
        void webLogin.start(`Sign in as ${row.label} again.`, { isClosedOnSignIn: true });
        if (!electronBridge.isDesktop) m.redraw();
      },
    },
    "Sign in",
  );
}

/** The menu: rendered by the Shell while open, dismissed by its backdrop, its button, Escape, or a choice. */
export function AccountSwitcherMenu(): m.Component<AccountSwitcherMenuAttrs> {
  const anchors = titlebarAnchors();
  const highlight = new MenuHighlight();
  return {
    onupdate() {
      anchors.remeasure();
    },
    view(vnode) {
      const { shell } = vnode.attrs;
      if (!shell.isAccountSwitcherOpen) {
        highlight.key = null;
        return null;
      }
      const accounts = shell.stores.accounts;
      const rows = accountMenuRows(shell.accountId, accounts.accounts, accounts.signedOutAccounts);
      const close = () => shell.closeAccountSwitcher();
      // "Manage accounts" opens Settings on the window's account, else on the first signed-in one.
      const managedAccountId = accounts.isSignedIn(shell.accountId)
        ? shell.accountId
        : (accounts.accounts[0]?.user_id ?? null);
      return m(
        "div#account-switcher-backdrop",
        {
          class: "fixed inset-0 z-[200] bg-black/20",
          onclick: (event: MouseEvent) => {
            if (event.target === event.currentTarget) close();
          },
        },
        [
          m(
            "div#account-switcher-menu",
            {
              role: "menu",
              "aria-label": "Accounts",
              tabindex: -1,
              style: menuStyle(ACCOUNT_BUTTON_ID, { isFittedToContent: true }),
              class: MENU_CLASS,
              // The menu has no field to take focus, so it takes it itself: the arrow keys then reach it however it
              // was opened.
              oncreate: ({ dom }: m.VnodeDOM) => (dom as HTMLElement).focus(),
              onkeydown: (event: KeyboardEvent & { redraw?: boolean }) => {
                if (!highlight.handleKey(event, event.currentTarget as Element)) event.redraw = false;
              },
            },
            [
              rows.map((row) => {
                const navKey = `account:${row.accountId}`;
                const isCurrent = row.accountId === shell.accountId;
                return m("div", { class: "flex items-center gap-1" }, [
                  m(
                    "button",
                    {
                      type: "button",
                      role: "menuitem",
                      "data-account-id": row.accountId,
                      "aria-current": isCurrent ? "true" : undefined,
                      disabled: shell.isAccountSwitchPending,
                      ...highlight.rowAttrs(navKey),
                      class: highlight.rowClass(MENU_ROW_CLASS + " flex-1 min-w-0", navKey, { isCurrent }),
                      onclick: () => {
                        if (isCurrent) close();
                        else void shell.switchAccount(row.accountId);
                      },
                    },
                    [
                      m(Icon16, {
                        name: row.accountId === LOCAL_PROFILE_USER_ID ? "monitor" : "user",
                        extra: "shrink-0" + (row.isSignedOut ? " text-tertiary" : ""),
                      }),
                      m("span", { class: "flex-1 min-w-0 flex flex-col" }, [
                        m("span", { class: "truncate" }, row.label),
                        row.isSignedOut
                          ? m("span", { class: "type-helper text-tertiary truncate" }, "Signed out")
                          : null,
                      ]),
                      isCurrent ? menuCurrentCheck() : null,
                    ],
                  ),
                  row.isSignedOut ? signInControl(shell, row) : null,
                ]);
              }),
              menuActionRow(highlight, {
                id: "account-switcher-add",
                navKey: "account-new",
                icon: "plus",
                label: "New account",
                disabled: webLogin.isOpen,
                onclick: () => {
                  close();
                  // The window keeps its account: the start flow and the create form take a landed sign-in up
                  // themselves, and the new account appears in this menu.
                  void webLogin.start("", { isClosedOnSignIn: true });
                  if (!electronBridge.isDesktop) m.redraw();
                },
              }),
              managedAccountId === null
                ? null
                : [
                    menuDivider(),
                    menuActionRow(highlight, {
                      id: "account-switcher-settings",
                      navKey: "account-manage",
                      icon: "settings",
                      label: "Manage accounts",
                      onclick: () => {
                        close();
                        shell.openSettings({ tab: "accounts", settings_account: managedAccountId });
                      },
                    }),
                  ],
            ],
          ),
          m(RaisedTitlebarIcons, {
            shell,
            anchors,
            selected: "account",
            onDismiss: close,
            onSelect: (id) => openTitlebarPopup(shell, id),
          }),
        ],
      );
    },
  };
}
