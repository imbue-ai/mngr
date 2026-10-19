import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import type { UiAccountEntry } from "../../channel/messages";
import { createEmptyStores } from "../../models/boot";
import { accountSharesUrl, type SharedWorkspaceRow } from "../../models/sharedWorkspaces";
import type { AnyVnode } from "../../testing";
import {
  accountEntry,
  accountsMessage,
  allText,
  collectVnodes,
  jsonResponse,
  renderDeep,
  secondAccountEntry,
  settle,
  sharedWorkspaceRow,
  withAttr,
} from "../../testing";
import { ShellState } from "../shell/shell-state";
import { SharedWorkspacesPage } from "./SharedWorkspacesPage";

const ALICE = accountEntry();

const BOB = secondAccountEntry();

afterEach(() => {
  clearAppContextForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function row(account: UiAccountEntry, overrides: Partial<SharedWorkspaceRow> = {}): SharedWorkspaceRow {
  return sharedWorkspaceRow({ account_email: account.email, ...overrides });
}

/** Answer each account's shares route with its rows; any other URL is a 404. */
function stubSharesFetch(rowsByAccount: ReadonlyMap<UiAccountEntry, SharedWorkspaceRow[]>): string[] {
  const urls: string[] = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = String(input);
    urls.push(url);
    for (const [account, rows] of rowsByAccount) {
      if (url === accountSharesUrl(account.user_id))
        return Promise.resolve(jsonResponse({ account_id: account.user_id, email: account.email, rows }));
    }
    return Promise.resolve(jsonResponse({ error: "no such account" }, 404));
  });
  return urls;
}

function mountPage(accounts: UiAccountEntry[]): {
  component: m.Component;
  vnode: m.Vnode;
  stores: ReturnType<typeof createEmptyStores>;
  render: () => AnyVnode;
} {
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  const stores = createEmptyStores();
  stores.accounts.applyAccountsMessage(accountsMessage(accounts));
  registerAppContext({ stores, shell: new ShellState(stores) });
  const component = SharedWorkspacesPage as unknown as m.Component;
  const vnode = m(component as m.ComponentTypes) as m.Vnode;
  // What mithril's initComponent does before the first hook: the page keeps
  // its model on vnode.state.
  (vnode as { state: unknown }).state = Object.create(component) as unknown;
  component.oninit?.call(vnode.state, vnode);
  return {
    component,
    vnode,
    stores,
    render: () => renderDeep((component.view as (v: m.Vnode) => AnyVnode).call(vnode.state, vnode)) as AnyVnode,
  };
}

describe("SharedWorkspacesPage", () => {
  it("renders one account's rows, flagging the leaked one", async () => {
    const leaked = row(ALICE, {
      host_id: "host-" + "b".repeat(32),
      display_name: "beta",
      record_state: "destroyed",
      is_likely_leaked: true,
    });
    stubSharesFetch(new Map([[ALICE, [row(ALICE), leaked]]]));
    const { render } = mountPage([ALICE]);
    await settle();

    const root = render();

    const text = allText(root);
    expect(text).toContain("alpha");
    expect(text).toContain("beta");
    expect(text).toContain("1 share looks leaked");
    expect(withAttr(root, "data-leaked").map((node) => node.attrs?.["data-leaked"])).toEqual([leaked.host_id]);
    // A single account gets no per-account heading.
    expect(collectVnodes(root).filter((node) => node.tag === "h2")).toEqual([]);
  });

  it("picks up an account that signs in after it opened", async () => {
    stubSharesFetch(new Map([[ALICE, [row(ALICE)]]]));
    const { component, vnode, stores, render } = mountPage([]);
    await settle();
    expect(allText(render())).toContain("Sign in to an account");

    stores.accounts.applyAccountsMessage(accountsMessage([ALICE]));
    component.onupdate?.call(vnode.state, vnode as m.VnodeDOM);
    await settle();

    expect(allText(render())).toContain("alpha");
  });

  it("renders a heading per account when several are signed in", async () => {
    stubSharesFetch(
      new Map([
        [ALICE, [row(ALICE)]],
        [BOB, [row(BOB, { host_id: "host-" + "c".repeat(32), display_name: "gamma", device_label: "desk" })]],
      ]),
    );
    const { render } = mountPage([ALICE, BOB]);
    await settle();

    const root = render();

    const headings = collectVnodes(root)
      .filter((node) => node.tag === "h2")
      .map((node) => allText(node));
    expect(headings).toEqual([ALICE.email, BOB.email]);
    expect(allText(root)).toContain("gamma");
  });
});
