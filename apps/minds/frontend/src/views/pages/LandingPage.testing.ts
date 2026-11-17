// Test helpers for the landing page's suites: the closure component
// bootstrapped as the router would, and the extras answer that says
// discovery settled with nothing locked.

import m from "mithril";
import { vi } from "vitest";
import type { UiWorkspaceEntry } from "../../channel/messages";
import type { AnyVnode } from "../../testing";
import {
  accountEntry,
  accountsMessage,
  attrsOf,
  collectVnodes,
  jsonResponse,
  recordingFetch,
  required,
  updatesShell,
  withAttr,
  workspacesMessage,
} from "../../testing";
import { CardMenu, type CardMenuItem } from "../components/CardMenu";
import { HomeAppIcon } from "../components/HomeAppIcon";
import { TabStrip } from "../components/TabStrip";
import { LandingPage } from "./LandingPage";

type FetchImpl = (url: string, init?: RequestInit) => Promise<Response>;

/** The one account signed in on the hubs these suites draw. */
export const ALICE = accountEntry({ user_id: "user-alice", email: "alice@example.com" });

/** The landing extras every page init fetches: discovery done, nothing locked. */
export const SETTLED_LANDING_EXTRAS = {
  is_discovery_complete: true,
  has_restorable_workspaces: false,
  destroying_status_by_agent_id: {},
  orphaned_failed_destroys: [],
  locked_account_emails: [],
};

/** The page constructed and initialized as the router would, `fetchImpl` answering its fetches. */
export function initLandingPage(fetchImpl: FetchImpl) {
  vi.stubGlobal("fetch", fetchImpl);
  const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
  const redraw = vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  const vnode = m(LandingPage, {}) as m.Vnode;
  const page = (LandingPage as unknown as (v: m.Vnode) => m.Component)(vnode);
  page.oninit?.call(page, vnode as m.VnodeDOM);
  const view = () =>
    (page.view as unknown as (v: m.Vnode) => m.Vnode).call(page, vnode);
  return { page, vnode, routeSet, redraw, view };
}

/**
 * The page in a window on Alice's hub, Alice the one account signed in:
 * `workspaces` are the account's rows (the fixture's one row when omitted)
 * and `fetchImpl` answers the page's fetches (the settled extras by default).
 */
export function initPageOnAlicesHub(
  options: { workspaces?: readonly UiWorkspaceEntry[]; fetchImpl?: FetchImpl } = {},
) {
  const shell = updatesShell({}, false);
  shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE], ALICE.user_id));
  shell.accountId = ALICE.user_id;
  const message = workspacesMessage();
  shell.stores.workspaces.applyWorkspacesMessage({
    ...message,
    workspaces: [...(options.workspaces ?? message.workspaces)],
  });
  const fetchImpl =
    options.fetchImpl ?? recordingFetch(() => Promise.resolve(jsonResponse(SETTLED_LANDING_EXTRAS))).fetchImpl;
  return { shell, ...initLandingPage(fetchImpl) };
}

/** The page's tab strip in the rendered tree. */
export function stripOf(rendered: unknown): AnyVnode {
  return required(collectVnodes(rendered).find((v) => v.tag === TabStrip));
}

/** Switch the page to `tab` through the strip, as a click would. */
export function selectTab(rendered: unknown, tab: string): void {
  (attrsOf(stripOf(rendered)).onSelect as (id: string) => void)(tab);
}

/** The strip's tabs as their ids and counts. */
export function tabsOf(rendered: unknown): { id: string; count?: number }[] {
  return (attrsOf(stripOf(rendered)).tabs as { id: string; count?: number }[]).map(({ id, count }) => ({ id, count }));
}

/** The app icons (own and shared) in the rendered tree, in page order. */
export function appIcons(rendered: unknown): AnyVnode[] {
  return collectVnodes(rendered).filter((v) => v.tag === HomeAppIcon);
}

/** The items of every card menu in the tree, flattened. */
export function menuItems(node: unknown): CardMenuItem[] {
  return collectVnodes(node)
    .filter((v) => v.tag === CardMenu)
    .flatMap((v) => attrsOf(v).items as CardMenuItem[]);
}

/** The one card-menu item in the tree labelled `label`. */
export function menuItem(node: unknown, label: string): CardMenuItem {
  return required(menuItems(node).find((item) => item.label === label));
}

/** Which tab's panel the page rendered. */
export function panelOf(rendered: unknown): string {
  return String(attrsOf(required(withAttr(rendered, "data-home-tab-panel")[0]))["data-home-tab-panel"]);
}
