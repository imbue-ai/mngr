import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createEmptyStores } from "../../models/boot";
import { webLogin } from "../../models/webLogin";
import type { AnyVnode } from "../../testing";
import {
  accountEntry,
  accountsMessage,
  allText,
  attrsOf,
  classTokensOf,
  collectVnodes,
  secondAccountEntry,
} from "../../testing";
import type { AccountSwitcherButtonAttrs, AccountSwitcherLabel } from "./AccountSwitcher";
import {
  AccountSwitcherButton,
  AccountSwitcherMenu,
  accountSwitcherLabel,
  accountSwitcherLabelNodes,
  accountSwitcherRows,
} from "./AccountSwitcher";
import { ShellState } from "./shell-state";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const ALICE = accountEntry({ display_name: "Alice Nguyen" });
const BOB = secondAccountEntry();
const CAROL = accountEntry({ user_id: "user-3", email: "carol@example.com", workspace_count: 2 });

describe("accountSwitcherRows", () => {
  it("lists the signed-in accounts, then the signed-out ones with workspaces here, then the device", () => {
    const rows = accountSwitcherRows(BOB.user_id, [ALICE, BOB], [CAROL]);
    expect(rows.map((row) => [row.accountId, row.label, row.detail, row.isCurrent, row.isSignedOut])).toEqual([
      [ALICE.user_id, "Alice Nguyen", ALICE.email, false, false],
      [BOB.user_id, BOB.email, "", true, false],
      [CAROL.user_id, CAROL.email, "Signed out", false, true],
      ["local", "This device", "", false, false],
    ]);
  });

  it("marks the device as the window's own on a device window, and a signed-out account on its own window", () => {
    expect(accountSwitcherRows("local", [ALICE], []).map((row) => row.isCurrent)).toEqual([false, true]);
    expect(accountSwitcherRows(CAROL.user_id, [], [CAROL]).map((row) => row.isCurrent)).toEqual([true, false]);
  });
});

describe("accountSwitcherLabel", () => {
  it("names the window's account by display name, else email, 'This device' for the device, and the id when nothing names it", () => {
    const shell = new ShellState(createEmptyStores());
    shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE, BOB], ALICE.user_id, [CAROL]));
    shell.accountId = ALICE.user_id;
    expect(accountSwitcherLabel(shell)).toEqual({ name: "Alice Nguyen", isSignedOut: false });
    shell.accountId = BOB.user_id;
    expect(accountSwitcherLabel(shell)).toEqual({ name: BOB.email, isSignedOut: false });
    shell.accountId = "local";
    expect(accountSwitcherLabel(shell)).toEqual({ name: "This device", isSignedOut: false });
  });

  it("marks an account with no session here as signed out, without the popover being opened", () => {
    const shell = new ShellState(createEmptyStores());
    shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE, BOB], ALICE.user_id, [CAROL]));
    shell.accountId = CAROL.user_id;
    expect(accountSwitcherLabel(shell)).toEqual({ name: CAROL.email, isSignedOut: true });
    shell.accountId = "user-unknown";
    expect(accountSwitcherLabel(shell)).toEqual({ name: "user-unknown", isSignedOut: true });
    // Signing the account in drops the mark.
    shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE, BOB, CAROL], ALICE.user_id, []));
    shell.accountId = CAROL.user_id;
    expect(accountSwitcherLabel(shell)).toEqual({ name: CAROL.email, isSignedOut: false });
  });
});

describe("accountSwitcherLabelNodes", () => {
  function spans(label: AccountSwitcherLabel, nameId: string | null): AnyVnode[] {
    return collectVnodes(accountSwitcherLabelNodes(label, nameId)).filter((node) => node.tag === "span");
  }

  it("draws the mark as its own non-shrinking node beside the truncating name, so a long name never hides it", () => {
    const nodes = spans({ name: CAROL.email, isSignedOut: true }, "the-name");
    expect(nodes.map((node) => [attrsOf(node).id, allText(node)])).toEqual([
      ["the-name", CAROL.email],
      [undefined, "(signed out)"],
    ]);
    expect(classTokensOf(nodes[0] as AnyVnode)).toContain("truncate");
    expect(classTokensOf(nodes[1] as AnyVnode)).toContain("shrink-0");
    expect(classTokensOf(nodes[1] as AnyVnode)).not.toContain("truncate");
  });

  it("draws only the name for a signed-in account, and no id when the caller is the raised copy", () => {
    const nodes = spans({ name: "Alice Nguyen", isSignedOut: false }, null);
    expect(nodes.map((node) => [attrsOf(node).id, allText(node)])).toEqual([[undefined, "Alice Nguyen"]]);
  });
});

describe("AccountSwitcherButton", () => {
  function renderButton(accountId: string): AnyVnode[] {
    const shell = new ShellState(createEmptyStores());
    shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE, BOB], ALICE.user_id, [CAROL]));
    shell.accountId = accountId;
    const button = AccountSwitcherButton();
    return collectVnodes(
      button.view.call(button, { attrs: { shell, popupHiddenClass: "" } } as m.Vnode<AccountSwitcherButtonAttrs>),
    );
  }

  it("names the window's account in the name span, with the signed-out mark beside it only when it has no session here", () => {
    const signedOut = renderButton(CAROL.user_id);
    expect(allText(signedOut.find((node) => attrsOf(node).id === "account-switcher-name"))).toBe(CAROL.email);
    expect(signedOut.filter((node) => attrsOf(node)["data-account-signed-out"] !== undefined).map(allText)).toEqual([
      "(signed out)",
    ]);

    const signedIn = renderButton(ALICE.user_id);
    expect(allText(signedIn.find((node) => attrsOf(node).id === "account-switcher-name"))).toBe("Alice Nguyen");
    expect(signedIn.some((node) => attrsOf(node)["data-account-signed-out"] !== undefined)).toBe(false);
  });
});

describe("AccountSwitcherMenu", () => {
  /** The open popover on a window for `accountId`, with Alice and Bob signed in and Carol signed out. */
  function openMenu(accountId: string): { shell: ShellState; render: () => AnyVnode[] } {
    const shell = new ShellState(createEmptyStores());
    shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE, BOB], accountId, [CAROL]));
    shell.accountId = accountId;
    shell.isAccountSwitcherOpen = true;
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const menu = AccountSwitcherMenu();
    const render = (): AnyVnode[] =>
      collectVnodes(menu.view.call(menu, { attrs: { shell } } as m.Vnode<{ shell: ShellState }>));
    return { shell, render };
  }

  function rowButtons(nodes: AnyVnode[]): AnyVnode[] {
    return nodes.filter((node) => attrsOf(node).role === "menuitemradio");
  }

  it("marks the window's own account as current and not actionable, and switches on another row's click", () => {
    const { shell, render } = openMenu(BOB.user_id);
    const switchAccount = vi.spyOn(shell, "switchAccount").mockResolvedValue(undefined);

    const rows = rowButtons(render());

    expect(rows.map((row) => [attrsOf(row)["data-account-id"], attrsOf(row)["aria-checked"], attrsOf(row).disabled])).toEqual([
      [ALICE.user_id, "false", false],
      [BOB.user_id, "true", true],
      [CAROL.user_id, "false", false],
      ["local", "false", false],
    ]);
    (attrsOf(rows[0] as AnyVnode).onclick as () => void)();
    expect(switchAccount).toHaveBeenCalledWith(ALICE.user_id);
  });

  it("holds every row while a switch is pending, and renders nothing once closed", () => {
    const { shell, render } = openMenu("local");
    shell.isAccountSwitchPending = true;
    expect(rowButtons(render()).map((row) => attrsOf(row).disabled)).toEqual([true, true, true, true]);

    shell.isAccountSwitcherOpen = false;
    expect(render()).toEqual([]);
  });

  it("offers Sign in beside a signed-out account's row only, which closes the menu and starts a sign-in naming it", () => {
    const { shell, render } = openMenu(CAROL.user_id);
    const start = vi.spyOn(webLogin, "start").mockResolvedValue(undefined);
    const signIns = render().filter((node) => attrsOf(node)["data-account-sign-in"] !== undefined);

    expect(signIns.map((node) => [attrsOf(node)["data-account-sign-in"], allText(node)])).toEqual([
      [CAROL.user_id, "Sign in"],
    ]);
    (attrsOf(signIns[0] as AnyVnode).onclick as () => void)();

    expect(shell.isAccountSwitcherOpen).toBe(false);
    expect(start).toHaveBeenCalledWith(`Sign in as ${CAROL.email} again.`, { isClosedOnSignIn: true });
  });

  it("'Add account' closes the menu and starts a browser sign-in that closes itself on landing", () => {
    const { shell, render } = openMenu("local");
    const start = vi.spyOn(webLogin, "start").mockResolvedValue(undefined);
    const add = render().find((node) => attrsOf(node).id === "account-switcher-add") as AnyVnode;

    (attrsOf(add).onclick as () => void)();

    expect(shell.isAccountSwitcherOpen).toBe(false);
    expect(start).toHaveBeenCalledWith("", { isClosedOnSignIn: true });
  });
});
