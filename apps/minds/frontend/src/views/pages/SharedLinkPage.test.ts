import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests } from "../../app-context";
import type { ShellState } from "../shell/shell-state";
import {
  accountEntry,
  accountsMessage,
  jsonResponse,
  settle,
  updatesShell,
} from "../../testing";
import { SharedLinkPage, findSharedAccount } from "./SharedLinkPage";

const HOST_ID = "host-" + "c".repeat(32);
const SHARE_KEY = "agent-" + "c".repeat(32);

describe("findSharedAccount", () => {
  it("refreshes every account at once and answers the first whose listing names the host", async () => {
    const refreshed: string[] = [];
    const refresh = async (accountId: string) => {
      refreshed.push(accountId);
      return {
        account_id: accountId,
        shares:
          accountId === "bob"
            ? [{ share_key: SHARE_KEY, host_id: HOST_ID.toUpperCase() }]
            : [],
      };
    };

    const lookup = await findSharedAccount(["alice", "bob", "carol"], HOST_ID, refresh);

    expect(lookup).toEqual({ hit: { accountId: "bob", shareKey: SHARE_KEY }, isAnyRefreshed: true });
    expect(refreshed.sort()).toEqual(["alice", "bob", "carol"]);
  });

  it("skips an account whose refresh fails, and says whether anything was checked at all", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const refresh = async (accountId: string) => {
      if (accountId === "alice") throw new Error("offline");
      return { account_id: accountId, shares: [] };
    };
    const failing = async () => {
      throw new Error("offline");
    };

    expect(await findSharedAccount(["alice", "bob"], HOST_ID, refresh)).toEqual({ hit: null, isAnyRefreshed: true });
    expect(await findSharedAccount(["alice", "bob"], HOST_ID, failing)).toEqual({ hit: null, isAnyRefreshed: false });
    expect(await findSharedAccount([], HOST_ID, refresh)).toEqual({ hit: null, isAnyRefreshed: false });
  });
});

describe("SharedLinkPage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    clearAppContextForTests();
  });

  /** The page initialized on a device with Alice and Bob signed in, `fetchImpl` answering each account's refresh. */
  function initPage(
    hostId: string,
    fetchImpl: (url: string) => Promise<Response>,
    { isAnyoneSignedIn = true }: { isAnyoneSignedIn?: boolean } = {},
  ) {
    const alice = accountEntry({ user_id: "user-alice", email: "alice@example.com" });
    const bob = accountEntry({ user_id: "user-bob", email: "bob@example.com" });
    const shell = updatesShell({}, false);
    if (isAnyoneSignedIn) {
      shell.stores.accounts.applyAccountsMessage(accountsMessage([alice, bob], alice.user_id));
    }
    const toast = vi.fn();
    shell.notificationsUi = { showTransientToast: toast } as unknown as ShellState["notificationsUi"];
    const routeToHub = vi.spyOn(shell, "routeToHub").mockImplementation(() => undefined);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    vi.spyOn(m.route, "param").mockImplementation(((key?: string) =>
      key === "hostId" ? hostId : undefined) as typeof m.route.param);
    const fetchMock = vi.fn(fetchImpl);
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const vnode = m(SharedLinkPage, {}) as m.Vnode<Record<string, never>, { isSettled: boolean; isRemoved: boolean }>;
    vnode.state = { isSettled: false, isRemoved: false };
    SharedLinkPage.oninit?.call(vnode.state, vnode as m.VnodeDOM<Record<string, never>, { isSettled: boolean; isRemoved: boolean }>);
    return { toast, routeToHub, routeSet, fetchMock, vnode };
  }

  it("lands on the share's account's home with the tile named", async () => {
    const { routeSet, routeToHub, toast } = initPage(HOST_ID, async (url) =>
      jsonResponse({
        account_id: url.includes("user-bob") ? "user-bob" : "user-alice",
        shares: url.includes("user-bob") ? [{ share_key: SHARE_KEY, host_id: HOST_ID }] : [],
      }),
    );
    await settle();

    expect(routeSet).toHaveBeenCalledWith("/account/user-bob/", { shared: SHARE_KEY }, { replace: true });
    expect(routeToHub).not.toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalled();
  });

  it("says the workspace is not shared with any account here once the listings were checked", async () => {
    const { routeSet, routeToHub, toast, vnode } = initPage(HOST_ID, async (url) =>
      jsonResponse({ account_id: url.includes("user-bob") ? "user-bob" : "user-alice", shares: [] }),
    );
    await settle();

    expect(toast).toHaveBeenCalledWith("Not shared with you", expect.stringContaining("not shared"));
    expect(routeToHub).toHaveBeenCalledWith("/", { replace: true });
    expect(routeSet).not.toHaveBeenCalled();
    expect(vnode.state.isSettled).toBe(true);
  });

  it("says the listings could not be checked when every refresh failed, rather than that nothing is shared", async () => {
    const { routeToHub, toast } = initPage(HOST_ID, async () => jsonResponse({ error: "offline" }, 502));
    await settle();

    expect(toast).toHaveBeenCalledWith("Could not check your shares", expect.stringContaining("could not be refreshed"));
    expect(routeToHub).toHaveBeenCalledWith("/", { replace: true });
  });

  it("neither routes nor toasts when the page was left before the lookup settled", async () => {
    const { routeSet, routeToHub, toast, vnode } = initPage(HOST_ID, async (url) =>
      jsonResponse({ account_id: url.includes("user-bob") ? "user-bob" : "user-alice", shares: [] }),
    );
    SharedLinkPage.onremove?.call(vnode.state, vnode as m.VnodeDOM<Record<string, never>, typeof vnode.state>);
    await settle();

    expect(routeSet).not.toHaveBeenCalled();
    expect(routeToHub).not.toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalled();
  });

  it("says the workspace is not shared with any account here when none is signed in, without asking", async () => {
    const { routeToHub, fetchMock, toast } = initPage(HOST_ID, async () => jsonResponse({}), {
      isAnyoneSignedIn: false,
    });
    await settle();

    expect(toast).toHaveBeenCalledWith("Not shared with you", expect.stringContaining("not shared"));
    expect(routeToHub).toHaveBeenCalledWith("/", { replace: true });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends a malformed host id home without asking anything", async () => {
    const { routeToHub, fetchMock, toast } = initPage("host-nope", async () => jsonResponse({}));
    await settle();

    expect(routeToHub).toHaveBeenCalledWith("/", { replace: true });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalled();
  });
});
