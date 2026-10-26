import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createEmptyStores } from "../../models/boot";
import { webLogin } from "../../models/webLogin";
import type { AnyVnode } from "../../testing";
import { accountEntry, accountsMessage, attrsOf, collectVnodes, secondAccountEntry } from "../../testing";
import { AccountSwitcherMenu, accountSwitcherLabel, accountSwitcherRows } from "./AccountSwitcher";
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
    expect(rows.map((row) => [row.accountId, row.label, row.detail, row.isCurrent])).toEqual([
      [ALICE.user_id, "Alice Nguyen", ALICE.email, false],
      [BOB.user_id, BOB.email, "", true],
      [CAROL.user_id, CAROL.email, "Signed out", false],
      ["local", "This device", "", false],
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
    expect(accountSwitcherLabel(shell)).toBe("Alice Nguyen");
    shell.accountId = BOB.user_id;
    expect(accountSwitcherLabel(shell)).toBe(BOB.email);
    shell.accountId = CAROL.user_id;
    expect(accountSwitcherLabel(shell)).toBe(CAROL.email);
    shell.accountId = "local";
    expect(accountSwitcherLabel(shell)).toBe("This device");
    shell.accountId = "user-unknown";
    expect(accountSwitcherLabel(shell)).toBe("user-unknown");
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

  it("'Add account' closes the menu and starts a browser sign-in that closes itself on landing", () => {
    const { shell, render } = openMenu("local");
    const start = vi.spyOn(webLogin, "start").mockResolvedValue(undefined);
    const add = render().find((node) => attrsOf(node).id === "account-switcher-add") as AnyVnode;

    (attrsOf(add).onclick as () => void)();

    expect(shell.isAccountSwitcherOpen).toBe(false);
    expect(start).toHaveBeenCalledWith("", { isClosedOnSignIn: true });
  });
});
