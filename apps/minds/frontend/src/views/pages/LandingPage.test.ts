import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests } from "../../app-context";
import { webLogin } from "../../models/webLogin";
import {
  accountEntry,
  accountsMessage,
  attrsOf,
  byId,
  recordingFetch,
  renderRoot,
  updatesShell,
} from "../../testing";
import {
  LandingPage,
  isUnlockBannerDue,
  shouldRedirectToStartFlow,
} from "./LandingPage";

const settled = { is_discovery_complete: true, has_restorable_workspaces: false, destroying_status_by_agent_id: {}, orphaned_failed_destroys: [], locked_account_emails: [] };

describe("shouldRedirectToStartFlow", () => {

  it("sends an install that never finished onboarding to the start flow once discovery finds nothing", () => {
    expect(shouldRedirectToStartFlow(false, false, settled)).toBe(true);
  });

  it("never redirects a completed install, a listed workspace, or an unfinished discovery", () => {
    expect(shouldRedirectToStartFlow(true, false, settled)).toBe(false);
    expect(shouldRedirectToStartFlow(false, true, settled)).toBe(false);
    expect(shouldRedirectToStartFlow(false, false, null)).toBe(false);
    expect(shouldRedirectToStartFlow(false, false, { ...settled, is_discovery_complete: false })).toBe(false);
    expect(shouldRedirectToStartFlow(false, false, { ...settled, has_restorable_workspaces: true })).toBe(false);
  });
});

describe("isUnlockBannerDue", () => {
  const aliceLocked = { ...settled, locked_account_emails: ["alice@example.com"] };

  it("is due exactly when the loaded extras are the window's account's answer and name it locked", () => {
    expect(isUnlockBannerDue(aliceLocked, "user-alice", "user-alice")).toBe(true);
  });

  it("stays down for another window's answer, an unlocked answer, or no answer yet", () => {
    expect(isUnlockBannerDue(aliceLocked, "user-alice", "local")).toBe(false);
    expect(isUnlockBannerDue(settled, "local", "local")).toBe(false);
    expect(isUnlockBannerDue(null, null, "user-alice")).toBe(false);
  });
});

describe("the landing extras fetch", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    clearAppContextForTests();
  });

  /** The page initialized in a window on Alice's hub, with every URL it fetches recorded. */
  function initPageOnAlicesHub() {
    const alice = accountEntry({ user_id: "user-alice", email: "alice@example.com" });
    const shell = updatesShell({}, false);
    shell.stores.accounts.applyAccountsMessage(accountsMessage([alice], alice.user_id));
    shell.accountId = alice.user_id;
    const recorder = recordingFetch();
    vi.stubGlobal("fetch", recorder.fetchImpl);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);

    const vnode = m(LandingPage, {}) as m.Vnode;
    const page = (LandingPage as unknown as (v: m.Vnode) => m.Component)(vnode);
    page.oninit?.call(page, vnode as m.VnodeDOM);
    return { page, vnode, shell, recorder };
  }

  it("asks for the window's account", () => {
    const { recorder } = initPageOnAlicesHub();

    expect(recorder.requests).toContain(
      "/ui/api/create/landing-extras?account=user-alice",
    );
  });

  it("re-asks for the new account when the window's account changes, and only then", () => {
    const { page, vnode, shell, recorder } = initPageOnAlicesHub();

    shell.accountId = "local";
    page.onupdate?.call(page, vnode as m.VnodeDOM);
    // A redraw without an account change must not re-ask.
    page.onupdate?.call(page, vnode as m.VnodeDOM);

    expect(
      recorder.requests.filter((url) => url.includes("landing-extras")),
    ).toEqual([
      "/ui/api/create/landing-extras?account=user-alice",
      "/ui/api/create/landing-extras?account=local",
    ]);
  });
});

describe("the hub's create control", () => {
  const ALICE = accountEntry({ user_id: "user-alice", email: "alice@example.com" });

  /** The page drawn in a window on `accountId`, with Alice the one account signed in. */
  function draw(accountId: string): m.Children {
    const shell = updatesShell({}, false);
    shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE], accountId));
    shell.accountId = accountId;
    vi.stubGlobal("fetch", recordingFetch().fetchImpl);
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const page = LandingPage as unknown as () => m.Component;
    return renderRoot(page, {});
  }

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    clearAppContextForTests();
  });

  it.each([
    ["a signed-in account", ALICE.user_id],
    ["the device", "local"],
  ])("offers Create on %s's hub, linking to that account's create page", (_name, accountId) => {
    const root = draw(accountId);

    expect(byId(root, "landing-sign-in")).toBeUndefined();
    const create = byId(root, "landing-create");
    expect(create).toBeDefined();
    expect(attrsOf(create!).href).toBe(`/account/${accountId}/create`);
  });

  it("offers to sign in again, in place of Create, on a signed-out account's hub", () => {
    const start = vi.spyOn(webLogin, "start").mockResolvedValue(undefined);
    const root = draw("user-gone");

    expect(byId(root, "landing-create")).toBeUndefined();
    const signIn = byId(root, "landing-sign-in");
    expect(signIn).toBeDefined();
    (attrsOf(signIn!).onclick as () => void)();
    expect(start).toHaveBeenCalledTimes(1);
  });
});
