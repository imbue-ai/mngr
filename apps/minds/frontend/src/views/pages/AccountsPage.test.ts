import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import type { UiAccountEntry } from "../../channel/messages";
import { createEmptyStores } from "../../models/boot";
import { webLogin } from "../../models/webLogin";
import type { AnyVnode } from "../../testing";
import {
  accountEntry,
  accountsMessage,
  allText,
  attrsOf,
  collectVnodes,
  jsonResponse,
  secondAccountEntry,
  settle,
} from "../../testing";
import { Button } from "../components/Button";
import { AccountsPage } from "./AccountsPage";
import { AccountCard } from "./settings/AccountCard";
import { ShellState } from "../shell/shell-state";

const ALICE = accountEntry();

const BOB = secondAccountEntry();

afterEach(() => {
  clearAppContextForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderPage(component: m.Component, vnode: m.Vnode): AnyVnode {
  return (component.view as (v: m.Vnode) => AnyVnode).call(component, vnode);
}

function shownEmails(root: AnyVnode): string[] {
  return collectVnodes(root)
    .filter((node) => node.tag === AccountCard)
    .map((node) => (attrsOf(node).account as UiAccountEntry).email);
}

describe("AccountsPage", () => {
  it("lists an account signed in while it is open, from the launcher's own frame", async () => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const planUrls: string[] = [];
    vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
      planUrls.push(String(input));
      return Promise.resolve(
        jsonResponse({ plan_view: null, trim_status: null }),
      );
    });
    const stores = createEmptyStores();
    stores.accounts.applyAccountsMessage(accountsMessage([ALICE]));
    registerAppContext({ stores, shell: new ShellState(stores) });

    const component = AccountsPage();
    const vnode = m(component as m.ComponentTypes) as m.Vnode;
    component.oninit?.call(component, vnode);
    await settle();
    expect(shownEmails(renderPage(component, vnode))).toEqual([ALICE.email]);

    stores.accounts.applyAccountsMessage(accountsMessage([ALICE, BOB]));
    component.onupdate?.call(component, vnode as m.VnodeDOM);
    await settle();

    expect(shownEmails(renderPage(component, vnode))).toEqual([
      ALICE.email,
      BOB.email,
    ]);
    expect(planUrls).toEqual([
      "/ui/api/accounts/user-1/plan",
      "/ui/api/accounts/user-2/plan",
    ]);
  });

  it("starts an added account's sign-in to close on landing, with no confirmation over the list", () => {
    const start = vi
      .spyOn(webLogin, "start")
      .mockImplementation(() => Promise.resolve());
    const stores = createEmptyStores();
    registerAppContext({ stores, shell: new ShellState(stores) });
    const component = AccountsPage();
    const vnode = m(component as m.ComponentTypes) as m.Vnode;

    const root = renderPage(component, vnode);
    expect(allText(root)).toContain("No accounts logged in.");
    const addButton = collectVnodes(root).find(
      (node) => node.tag === Button && allText(node).includes("Add account"),
    );
    (attrsOf(addButton as AnyVnode).onclick as () => void)();

    expect(start).toHaveBeenCalledWith("", { isClosedOnSignIn: true });
  });
});
