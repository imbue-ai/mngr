import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests } from "../../app-context";
import { onboardingProgress } from "../../models/onboarding";
import { webLogin } from "../../models/webLogin";
import type { UiAccountEntry } from "../../channel/messages";
import {
  accountEntry,
  accountsMessage,
  allText,
  attrsOf,
  byId,
  jsonResponse,
  recordingFetch,
  updatesShell,
  withAttr,
  workspacesMessage,
} from "../../testing";
import {
  LandingPage,
  isUnlockBannerDue,
  otherAccountsWithWorkspaces,
  shouldRedirectToStartFlow,
} from "./LandingPage";

const settled = { is_discovery_complete: true, has_restorable_workspaces: false, destroying_status_by_agent_id: {}, orphaned_failed_destroys: [], locked_account_emails: [] };

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  clearAppContextForTests();
});

/** The page constructed and initialized as the router would, `fetchImpl` answering its fetches. */
function initLandingPage(fetchImpl: (url: string) => Promise<Response>) {
  vi.stubGlobal("fetch", fetchImpl);
  const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
  const redraw = vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  const vnode = m(LandingPage, {}) as m.Vnode;
  const page = (LandingPage as unknown as (v: m.Vnode) => m.Component)(vnode);
  page.oninit?.call(page, vnode as m.VnodeDOM);
  const view = () => (page.view as unknown as (v: m.Vnode) => m.Vnode).call(page, vnode);
  return { page, vnode, routeSet, redraw, view };
}

describe("shouldRedirectToStartFlow", () => {

  it("sends an install that never finished onboarding to the start flow once discovery finds nothing", () => {
    expect(shouldRedirectToStartFlow(false, false, settled)).toBe(true);
  });

  it("never redirects a completed install, an install with a workspace under any account, or an unfinished discovery", () => {
    expect(shouldRedirectToStartFlow(true, false, settled)).toBe(false);
    expect(shouldRedirectToStartFlow(false, true, settled)).toBe(false);
    expect(shouldRedirectToStartFlow(false, false, null)).toBe(false);
    expect(shouldRedirectToStartFlow(false, false, { ...settled, is_discovery_complete: false })).toBe(false);
  });
});

describe("otherAccountsWithWorkspaces", () => {
  const ALICE = accountEntry({ user_id: "user-alice", email: "alice@example.com" });
  const BOB = accountEntry({ user_id: "user-bob", email: "bob@example.com", display_name: "Bob" });
  const GONE = accountEntry({ user_id: "user-gone", email: "gone@example.com" });

  it("lists every other account with workspaces, signed in, signed out, then the device, and skips the window's own", () => {
    const counts = { "user-alice": 4, "user-bob": 0, "user-gone": 1, local: 2 };

    expect(otherAccountsWithWorkspaces("user-bob", counts, [ALICE, BOB], [GONE])).toEqual([
      { accountId: "user-alice", label: "alice@example.com", workspaceCount: 4 },
      { accountId: "user-gone", label: "gone@example.com", workspaceCount: 1 },
      { accountId: "local", label: "This device", workspaceCount: 2 },
    ]);
    expect(otherAccountsWithWorkspaces("local", counts, [ALICE, BOB], [GONE]).map((other) => other.accountId)).toEqual([
      "user-alice",
      "user-gone",
    ]);
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
  /** The page initialized in a window on Alice's hub, with every URL it fetches recorded. */
  function initPageOnAlicesHub() {
    const alice = accountEntry({ user_id: "user-alice", email: "alice@example.com" });
    const shell = updatesShell({}, false);
    shell.stores.accounts.applyAccountsMessage(accountsMessage([alice], alice.user_id));
    shell.accountId = alice.user_id;
    const recorder = recordingFetch();
    const { page, vnode } = initLandingPage(recorder.fetchImpl);
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
    return initLandingPage(recordingFetch().fetchImpl).view();
  }

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

/** The device's home page once discovery settled with none of its own, `countByAccountId` install-wide. */
async function initSettledDeviceHub(options: {
  signedIn: UiAccountEntry[];
  countByAccountId: Record<string, number>;
  restorableIds?: string[];
}) {
  const shell = updatesShell({}, false);
  shell.stores.accounts.applyAccountsMessage(accountsMessage(options.signedIn, "local"));
  shell.stores.workspaces.applyWorkspacesMessage(
    workspacesMessage({
      workspaces: [],
      workspace_count_by_account_id: options.countByAccountId,
      restorable_workspace_ids: options.restorableIds ?? [],
    }),
  );
  shell.accountId = "local";
  const { page, vnode, routeSet, redraw, view } = initLandingPage(
    recordingFetch(() => Promise.resolve(jsonResponse(settled))).fetchImpl,
  );
  await vi.waitFor(() => expect(redraw).toHaveBeenCalled());
  return { shell, page, vnode, routeSet, view };
}

describe("the other-accounts notice", () => {
  const ALICE = accountEntry({ user_id: "user-alice", email: "alice@example.com" });

  async function drawSettledDeviceHub(countByAccountId: Record<string, number>) {
    const { shell, view } = await initSettledDeviceHub({ signedIn: [ALICE], countByAccountId });
    return { shell, root: view() };
  }

  it("offers the account that has workspaces, and choosing it switches the window", async () => {
    const { shell, root } = await drawSettledDeviceHub({ "user-alice": 4 });
    const switchAccount = vi.spyOn(shell, "switchAccount").mockResolvedValue(undefined);

    const notice = byId(root, "landing-other-accounts");
    expect(notice).toBeDefined();
    const offers = withAttr(notice, "data-account-id");
    expect(offers.map((offer) => attrsOf(offer)["data-account-id"])).toEqual(["user-alice"]);
    expect(allText(offers[0])).toBe("alice@example.com · 4 workspaces");
    (attrsOf(offers[0]).onclick as () => void)();
    expect(switchAccount).toHaveBeenCalledWith("user-alice");
  });

  it("is absent when no other account has workspaces", async () => {
    const { root } = await drawSettledDeviceHub({});

    expect(byId(root, "landing-other-accounts")).toBeUndefined();
  });

  it("waits for the new account's extras after a hub switch instead of reading the device's", async () => {
    const { shell, view } = await initSettledDeviceHub({ signedIn: [ALICE], countByAccountId: { local: 1 } });

    shell.accountId = ALICE.user_id;
    const root = view();

    expect(allText(root)).toContain("Discovering workspaces...");
    expect(byId(root, "landing-other-accounts")).toBeUndefined();
  });
});

describe("the start-flow hand-over", () => {
  afterEach(() => {
    onboardingProgress.seed(true);
  });

  /** Where the device's empty home page sends an install that never finished onboarding. */
  async function routeAfterSettledDiscovery(countByAccountId: Record<string, number>, restorableIds: string[]) {
    onboardingProgress.seed(false);
    const { page, vnode, routeSet } = await initSettledDeviceHub({ signedIn: [], countByAccountId, restorableIds });
    page.onupdate?.call(page, vnode as m.VnodeDOM);
    return routeSet.mock.calls.map((call) => call[0]);
  }

  it("hands an install with no workspace anywhere over to the start flow", async () => {
    expect(await routeAfterSettledDiscovery({}, [])).toEqual(["/start"]);
  });

  it("keeps the page when another account has rows", async () => {
    expect(await routeAfterSettledDiscovery({ "user-alice": 2 }, [])).toEqual([]);
  });

  it("keeps the page while a workspace is not re-listed yet", async () => {
    expect(await routeAfterSettledDiscovery({}, ["agent-0a1b"])).toEqual([]);
  });
});
