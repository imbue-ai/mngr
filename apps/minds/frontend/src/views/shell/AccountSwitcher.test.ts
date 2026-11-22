// @vitest-environment jsdom
//
// The menu's highlight walks its real rows and the menu takes focus as it
// opens, so those cases mount it; the rest read the tree its view returns.
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
  byId,
  classTokensOf,
  collectVnodes,
  mountInBody,
  required,
  secondAccountEntry,
  thirdAccountEntry,
  withAttr,
} from "../../testing";
import { AccountSwitcherMenu, accountMenuRows, accountSwitcherRows } from "./AccountSwitcher";
import type { RaisedTitlebarIconsAttrs } from "./RaisedTitlebarIcons";
import { RaisedTitlebarIcons } from "./RaisedTitlebarIcons";
import { ShellState } from "./shell-state";

let mountedRoot: HTMLElement | null = null;

afterEach(() => {
  if (mountedRoot !== null) m.mount(mountedRoot, null);
  mountedRoot = null;
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const ALICE = accountEntry({ display_name: "Alice Nguyen" });
const BOB = secondAccountEntry();
const CAROL = thirdAccountEntry();

describe("accountMenuRows", () => {
  it("lists the signed-in accounts by email, then the signed-out ones with workspaces here, then the device", () => {
    const rows = accountMenuRows(BOB.user_id, [ALICE, BOB], [CAROL]);

    // Alice has a display name, and is still listed by her email. Bob's is the window's own, listed with the rest.
    expect(rows.map((row) => [row.accountId, row.label, row.isSignedOut])).toEqual([
      [ALICE.user_id, ALICE.email, false],
      [BOB.user_id, BOB.email, false],
      [CAROL.user_id, CAROL.email, true],
      ["local", "This device", false],
    ]);
  });

  it("lists a window's account that neither list names by its id, as signed out, ahead of the device", () => {
    const rows = accountMenuRows("user-9", [ALICE], [CAROL]);

    expect(rows.map((row) => [row.accountId, row.label, row.isSignedOut])).toEqual([
      [ALICE.user_id, ALICE.email, false],
      [CAROL.user_id, CAROL.email, true],
      ["user-9", "user-9", true],
      ["local", "This device", false],
    ]);
  });
});

describe("accountSwitcherRows", () => {
  it("lists the accounts the window could move to, in the menu's order", () => {
    const rows = accountSwitcherRows(BOB.user_id, [ALICE, BOB], [CAROL]);

    expect(rows.map((row) => [row.accountId, row.label, row.isSignedOut])).toEqual([
      [ALICE.user_id, ALICE.email, false],
      [CAROL.user_id, CAROL.email, true],
      ["local", "This device", false],
    ]);
  });

  it("never lists the window's own account: the device's, a signed-out one's, or one no list names", () => {
    expect(accountSwitcherRows("user-9", [ALICE], []).map((row) => row.accountId)).toEqual([ALICE.user_id, "local"]);
    expect(accountSwitcherRows("local", [ALICE], []).map((row) => row.accountId)).toEqual([ALICE.user_id]);
    expect(accountSwitcherRows(CAROL.user_id, [], [CAROL]).map((row) => row.accountId)).toEqual(["local"]);
    expect(accountSwitcherRows(ALICE.user_id, [ALICE], []).map((row) => row.accountId)).toEqual(["local"]);
  });
});

/** A shell on a window for `accountId` with the account menu open; Alice and Bob signed in and Carol signed out,
 * unless a case says otherwise. */
function shellWithMenuOpen(
  accountId: string,
  signedIn = [ALICE, BOB],
  signedOut = [CAROL],
): ShellState {
  const shell = new ShellState(createEmptyStores());
  shell.stores.accounts.applyAccountsMessage(accountsMessage(signedIn, accountId, signedOut));
  shell.accountId = accountId;
  shell.openAccountSwitcher();
  return shell;
}

/** The menu's view, rendered without mounting; `m.redraw` is stilled since nothing is mounted to draw. */
function menuRenderer(shell: ShellState): () => AnyVnode[] {
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  const menu = AccountSwitcherMenu();
  return () => collectVnodes(menu.view.call(menu, { attrs: { shell } } as m.Vnode<{ shell: ShellState }>));
}

function accountRows(nodes: AnyVnode[]): AnyVnode[] {
  return nodes.filter((node) => attrsOf(node).role === "menuitem" && attrsOf(node)["data-account-id"] !== undefined);
}

function accountRow(nodes: AnyVnode[], accountId: string): AnyVnode {
  return required(accountRows(nodes).find((row) => attrsOf(row)["data-account-id"] === accountId));
}

/** The icons a row draws, left to right. */
function iconNames(row: AnyVnode): unknown[] {
  return withAttr(row.children, "name").map((icon) => attrsOf(icon).name);
}

/** The ids of the rows marked as the window's own account. */
function currentAccountIds(nodes: AnyVnode[]): unknown[] {
  return accountRows(nodes)
    .filter((row) => attrsOf(row)["aria-current"] === "true")
    .map((row) => attrsOf(row)["data-account-id"]);
}

describe("AccountSwitcherMenu", () => {
  it("lists every account by its email, the window's own among them, and moves the window on another's click", () => {
    const shell = shellWithMenuOpen(BOB.user_id);
    const switchAccount = vi.spyOn(shell, "switchAccount").mockResolvedValue(undefined);
    const nodes = menuRenderer(shell)();

    const menu = required(byId(nodes, "account-switcher-menu"));
    expect(attrsOf(menu).role).toBe("menu");
    // At least the menus' common width, growing to fit its longest email.
    expect(String(attrsOf(menu).style)).toContain("width: max-content; min-width: 260px;");
    const rows = accountRows(nodes);
    expect(rows.map((row) => [attrsOf(row)["data-account-id"], allText(row), attrsOf(row).disabled])).toEqual([
      [ALICE.user_id, ALICE.email, false],
      [BOB.user_id, BOB.email, false],
      [CAROL.user_id, `${CAROL.email} Signed out`, false],
      ["local", "This device", false],
    ]);
    // No display name is drawn.
    expect(allText(menu)).not.toContain("Alice Nguyen");

    (attrsOf(rows[0]).onclick as () => void)();
    expect(switchAccount).toHaveBeenCalledWith(ALICE.user_id);
  });

  it("marks the window's own row, and no other, with a fainter fill than the highlight's, a check at its right end, and aria-current", () => {
    const nodes = menuRenderer(shellWithMenuOpen(BOB.user_id))();

    expect(currentAccountIds(nodes)).toEqual([BOB.user_id]);
    const own = accountRow(nodes, BOB.user_id);
    expect(classTokensOf(own)).toContain("!bg-fill-subtle");
    expect(classTokensOf(own)).not.toContain("!bg-fill-hover");
    expect(iconNames(own)).toEqual(["user", "check"]);
    for (const other of [ALICE.user_id, CAROL.user_id, "local"]) {
      const row = accountRow(nodes, other);
      expect(classTokensOf(row), other).not.toContain("!bg-fill-subtle");
      expect(iconNames(row), other).not.toContain("check");
    }
  });

  it("closes on a click on the window's own row, without switching", () => {
    const shell = shellWithMenuOpen(BOB.user_id);
    const switchAccount = vi.spyOn(shell, "switchAccount").mockResolvedValue(undefined);

    (attrsOf(accountRow(menuRenderer(shell)(), BOB.user_id)).onclick as () => void)();

    expect(switchAccount).not.toHaveBeenCalled();
    expect(shell.isAccountSwitcherOpen).toBe(false);
    expect(shell.accountId).toBe(BOB.user_id);
  });

  it("lists the device, marked, on the device's own window", () => {
    const nodes = menuRenderer(shellWithMenuOpen("local"))();

    expect(accountRows(nodes).map((row) => attrsOf(row)["data-account-id"])).toEqual([
      ALICE.user_id,
      BOB.user_id,
      CAROL.user_id,
      "local",
    ]);
    expect(currentAccountIds(nodes)).toEqual(["local"]);
    expect(iconNames(accountRow(nodes, "local"))).toEqual(["monitor", "check"]);
  });

  it("opens every row with an icon: a person for an account, dimmed for a signed-out one, and a monitor for the device", () => {
    const rows = accountRows(menuRenderer(shellWithMenuOpen(BOB.user_id))());

    // The first thing in each row, so the icons share one column.
    const leading = rows.map((row) => attrsOf(required(collectVnodes(row.children)[0])));
    expect(leading.map((icon) => [icon.name, icon.extra])).toEqual([
      ["user", "shrink-0"],
      ["user", "shrink-0"],
      ["user", "shrink-0 text-tertiary"],
      ["monitor", "shrink-0"],
    ]);
  });

  it("holds every row while a switch is pending, and renders nothing once closed", () => {
    const shell = shellWithMenuOpen("local");
    const render = menuRenderer(shell);
    shell.isAccountSwitchPending = true;
    expect(accountRows(render()).map((row) => attrsOf(row).disabled)).toEqual([true, true, true, true]);

    shell.closeAccountSwitcher();
    expect(render()).toEqual([]);
  });

  it("offers Sign in beside a signed-out account's row only, which closes the menu and starts a sign-in naming it", () => {
    const shell = shellWithMenuOpen(ALICE.user_id);
    const start = vi.spyOn(webLogin, "start").mockResolvedValue(undefined);
    const signIns = menuRenderer(shell)().filter((node) => attrsOf(node)["data-account-sign-in"] !== undefined);

    expect(signIns.map((node) => [attrsOf(node)["data-account-sign-in"], allText(node)])).toEqual([
      [CAROL.user_id, "Sign in"],
    ]);
    (attrsOf(signIns[0]).onclick as () => void)();

    expect(shell.isAccountSwitcherOpen).toBe(false);
    expect(start).toHaveBeenCalledWith(`Sign in as ${CAROL.email} again.`, { isClosedOnSignIn: true });
  });

  it("keeps the 'Signed out' line and Sign in on the row of a window's own signed-out account", () => {
    const nodes = menuRenderer(shellWithMenuOpen(CAROL.user_id))();

    expect(currentAccountIds(nodes)).toEqual([CAROL.user_id]);
    const own = accountRow(nodes, CAROL.user_id);
    expect(allText(own)).toBe(`${CAROL.email} Signed out`);
    expect(iconNames(own)).toEqual(["user", "check"]);
    const signIns = nodes.filter((node) => attrsOf(node)["data-account-sign-in"] !== undefined);
    expect(signIns.map((node) => attrsOf(node)["data-account-sign-in"])).toEqual([CAROL.user_id]);
  });

  it("'New account' closes the menu and starts a browser sign-in that closes itself on landing", () => {
    const shell = shellWithMenuOpen("local");
    const start = vi.spyOn(webLogin, "start").mockResolvedValue(undefined);
    const add = required(byId(menuRenderer(shell)(), "account-switcher-add"));

    expect(allText(add)).toBe("New account");
    expect(withAttr(add.children, "name").map((icon) => attrsOf(icon).name)).toEqual(["plus"]);
    (attrsOf(add).onclick as () => void)();

    expect(shell.isAccountSwitcherOpen).toBe(false);
    // The window stays on its account: the sign-in names none to move to.
    expect(start).toHaveBeenCalledWith("", { isClosedOnSignIn: true });
    expect(shell.accountId).toBe("local");
  });

  it("closes on a click on the dimmed window, and not on one inside the menu", () => {
    const shell = shellWithMenuOpen(ALICE.user_id);
    const backdrop = required(byId(menuRenderer(shell)(), "account-switcher-backdrop"));
    const onclick = attrsOf(backdrop).onclick as (event: unknown) => void;

    onclick({ target: "a row", currentTarget: "the backdrop" });
    expect(shell.isAccountSwitcherOpen).toBe(true);

    onclick({ target: "the backdrop", currentTarget: "the backdrop" });
    expect(shell.isAccountSwitcherOpen).toBe(false);
  });

  it("raises the bar's strip with the account button selected, which dismisses it or switches flyouts through the shell", () => {
    const shell = shellWithMenuOpen(ALICE.user_id);
    const openSettingsFromBar = vi.spyOn(shell, "openSettingsFromBar").mockImplementation(() => undefined);
    const switchToWorkspaceSwitcher = vi.spyOn(shell, "switchToWorkspaceSwitcher").mockImplementation(() => undefined);
    const strip = required(menuRenderer(shell)().find((node) => node.tag === RaisedTitlebarIcons));
    const attrs = attrsOf(strip) as unknown as RaisedTitlebarIconsAttrs;

    expect(attrs.selected).toBe("account");
    attrs.onSelect("settings");
    attrs.onSelect("workspace");
    expect(openSettingsFromBar).toHaveBeenCalledTimes(1);
    expect(switchToWorkspaceSwitcher).toHaveBeenCalledTimes(1);

    attrs.onDismiss();
    expect(shell.isAccountSwitcherOpen).toBe(false);
  });
});

function menuItems(nodes: AnyVnode[]): AnyVnode[] {
  return nodes.filter((node) => attrsOf(node).role === "menuitem" || attrsOf(node).role === "separator");
}

describe("the account menu's order", () => {
  it("puts New account straight after the account rows, with the menu's one rule above Manage accounts", () => {
    const items = menuItems(menuRenderer(shellWithMenuOpen(BOB.user_id))());

    expect(
      items.map((node) => {
        const attrs = attrsOf(node);
        return attrs.id ?? attrs["data-account-id"] ?? attrs["data-account-sign-in"] ?? attrs.role;
      }),
    ).toEqual([
      ALICE.user_id,
      BOB.user_id,
      CAROL.user_id,
      // Carol's Sign in.
      CAROL.user_id,
      "local",
      "account-switcher-add",
      "separator",
      "account-switcher-settings",
    ]);
  });
});

describe("the Manage accounts item", () => {
  it("sits last under a rule and opens Settings on the Accounts tab at the window's account, closing the menu", () => {
    const shell = shellWithMenuOpen(BOB.user_id);
    vi.spyOn(m.route, "get").mockReturnValue(`/account/${BOB.user_id}/`);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);

    const items = menuItems(menuRenderer(shell)());
    expect(items.slice(-2).map((node) => attrsOf(node).role)).toEqual(["separator", "menuitem"]);
    const manage = items[items.length - 1];
    expect(attrsOf(manage).id).toBe("account-switcher-settings");
    expect(allText(manage)).toBe("Manage accounts");
    expect(withAttr(manage.children, "name").map((icon) => attrsOf(icon).name)).toEqual(["settings"]);
    (attrsOf(manage).onclick as () => void)();

    expect(shell.isAccountSwitcherOpen).toBe(false);
    expect(routeSet).toHaveBeenCalledWith(
      "/settings",
      { tab: "accounts", settings_account: BOB.user_id, account: BOB.user_id },
      undefined,
    );
  });

  it("opens on the first signed-in account from a window that has none of its own: the device's, or a signed-out one's", () => {
    for (const accountId of ["local", CAROL.user_id]) {
      const shell = shellWithMenuOpen(accountId);
      const openSettings = vi.spyOn(shell, "openSettings").mockImplementation(() => undefined);

      (attrsOf(required(byId(menuRenderer(shell)(), "account-switcher-settings"))).onclick as () => void)();

      expect(openSettings, accountId).toHaveBeenCalledWith({ tab: "accounts", settings_account: ALICE.user_id });
    }
  });

  it("is absent, with its rule, while no account is signed in", () => {
    const items = menuItems(menuRenderer(shellWithMenuOpen("local", [], [CAROL]))());

    // Carol's row and its Sign in, the device's row, and New account last.
    expect(items.map((node) => attrsOf(node).role)).toEqual(["menuitem", "menuitem", "menuitem", "menuitem"]);
    expect(attrsOf(items[items.length - 1]).id).toBe("account-switcher-add");
    expect(byId(items, "account-switcher-settings")).toBeUndefined();
  });
});

describe("the account menu's keyboard", () => {
  function mountMenu(shell: ShellState): HTMLElement {
    mountedRoot = mountInBody(() => m(AccountSwitcherMenu, { shell }));
    return required(mountedRoot.querySelector<HTMLElement>("#account-switcher-menu") ?? undefined);
  }

  function pressKey(menu: HTMLElement, key: string): KeyboardEvent {
    const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
    menu.dispatchEvent(event);
    m.redraw.sync();
    return event;
  }

  function highlighted(menu: HTMLElement): (string | undefined)[] {
    return [...menu.querySelectorAll<HTMLElement>("[data-highlighted]")].map((row) => row.dataset.navKey);
  }

  it("takes focus as it opens, so the arrow keys reach it however it was opened", () => {
    const menu = mountMenu(shellWithMenuOpen(BOB.user_id));

    expect(document.activeElement).toBe(menu);
  });

  it("moves one highlight through the account rows and on into the actions, and back out above the first", () => {
    const menu = mountMenu(shellWithMenuOpen(BOB.user_id));
    expect(highlighted(menu)).toEqual([]);

    const visited: (string | undefined)[][] = [];
    for (let step = 0; step < 7; step++) {
      pressKey(menu, "ArrowDown");
      visited.push(highlighted(menu));
    }
    expect(visited).toEqual([
      [`account:${ALICE.user_id}`],
      [`account:${BOB.user_id}`],
      [`account:${CAROL.user_id}`],
      ["account:local"],
      ["account-new"],
      ["account-manage"],
      ["account-manage"],
    ]);

    for (let step = 0; step < 6; step++) pressKey(menu, "ArrowUp");
    expect(highlighted(menu)).toEqual([]);
  });

  it("lights the window's own row like any other while it is highlighted, and closes on Enter there without switching", () => {
    const shell = shellWithMenuOpen(BOB.user_id);
    const switchAccount = vi.spyOn(shell, "switchAccount").mockResolvedValue(undefined);
    const menu = mountMenu(shell);
    const own = required(menu.querySelector<HTMLElement>(`[data-account-id="${BOB.user_id}"]`) ?? undefined);
    expect(own.classList.contains("!bg-fill-subtle")).toBe(true);

    pressKey(menu, "ArrowDown");
    pressKey(menu, "ArrowDown");
    expect(highlighted(menu)).toEqual([`account:${BOB.user_id}`]);
    // One fill at a time: the highlight's replaces the fainter one.
    expect(own.classList.contains("!bg-fill-hover")).toBe(true);
    expect(own.classList.contains("!bg-fill-subtle")).toBe(false);
    pressKey(menu, "Enter");

    expect(switchAccount).not.toHaveBeenCalled();
    expect(shell.isAccountSwitcherOpen).toBe(false);
  });

  it("shares that highlight with the mouse, and chooses the highlighted row on Enter", () => {
    const shell = shellWithMenuOpen(BOB.user_id);
    const switchAccount = vi.spyOn(shell, "switchAccount").mockResolvedValue(undefined);
    const menu = mountMenu(shell);
    pressKey(menu, "ArrowDown");

    const device = required(menu.querySelector<HTMLElement>('[data-account-id="local"]') ?? undefined);
    device.dispatchEvent(new MouseEvent("mousemove", { bubbles: true }));
    m.redraw.sync();
    // One highlight, not a keyboard one and a hover.
    expect(highlighted(menu)).toEqual(["account:local"]);

    pressKey(menu, "ArrowUp");
    expect(highlighted(menu)).toEqual([`account:${CAROL.user_id}`]);
    const enter = pressKey(menu, "Enter");

    expect(enter.defaultPrevented).toBe(true);
    expect(switchAccount).toHaveBeenCalledWith(CAROL.user_id);
  });

  it("chooses nothing on Enter while no row is highlighted", () => {
    const shell = shellWithMenuOpen(BOB.user_id);
    const switchAccount = vi.spyOn(shell, "switchAccount").mockResolvedValue(undefined);
    const start = vi.spyOn(webLogin, "start").mockResolvedValue(undefined);

    pressKey(mountMenu(shell), "Enter");

    expect(switchAccount).not.toHaveBeenCalled();
    expect(start).not.toHaveBeenCalled();
    expect(shell.isAccountSwitcherOpen).toBe(true);
  });

  it("starts with no highlight each time it opens", () => {
    const shell = shellWithMenuOpen(BOB.user_id);
    pressKey(mountMenu(shell), "ArrowDown");

    shell.closeAccountSwitcher();
    m.redraw.sync();
    shell.openAccountSwitcher();
    m.redraw.sync();

    const reopened = required(document.querySelector<HTMLElement>("#account-switcher-menu") ?? undefined);
    expect(highlighted(reopened)).toEqual([]);
  });
});
