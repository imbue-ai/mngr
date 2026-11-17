import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests } from "../../app-context";
import type { UiWorkspaceEntry } from "../../channel/messages";
import type { UiSharedWorkspace } from "../../generated/ui";
import type { AnyVnode } from "../../testing";
import {
  allText,
  attrsOf,
  byId,
  collectVnodes,
  jsonResponse,
  required,
  settle,
  sharedWorkspace,
  stubSessionStorage,
  sharedWorkspaceEntry,
  withAttr,
  workspacesMessage,
} from "../../testing";
import {
  appIcons,
  initPageOnAlicesHub,
  menuItem,
  menuItems,
  panelOf,
  selectTab,
  SETTLED_LANDING_EXTRAS,
  tabsOf,
} from "./LandingPage.testing";

/** A fetch answering the extras with the settled shape and everything else with `answer`. */
function fetchAnswering(answer: (url: string, init?: RequestInit) => Response) {
  return (url: string, init?: RequestInit) =>
    Promise.resolve(
      url.includes("landing-extras")
        ? jsonResponse(SETTLED_LANDING_EXTRAS)
        : answer(url, init),
    );
}

const SHARE_KEY = sharedWorkspace().share_key;

/** The fixture's share with its owner named, so a card's chip reads a display name. */
function sharedEntry(
  overrides: Partial<UiSharedWorkspace> = {},
): UiWorkspaceEntry {
  return sharedWorkspaceEntry({
    owner: { ...sharedWorkspace().owner, display_name: "Owen" },
    ...overrides,
  });
}

/** An accepted share: a card under "Workspaces shared with you", not an invitation. */
function acceptedEntry(overrides: Partial<UiSharedWorkspace> = {}): UiWorkspaceEntry {
  return sharedEntry({ status: "accepted", ...overrides });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  clearAppContextForTests();
});

beforeEach(() => {
  stubSessionStorage();
});

/** The page on Alice's hub, with Alice's rows holding one owned workspace and the given shared ones. */
function initPageWithShares(...entries: UiWorkspaceEntry[]) {
  return initPageOnAlicesHub({
    workspaces: [...workspacesMessage().workspaces, ...entries],
    fetchImpl: fetchAnswering(() => jsonResponse({})),
  });
}

function buttonNamed(node: unknown, label: string): AnyVnode {
  return required(
    collectVnodes(node).find((v) => allText(v) === label && typeof attrsOf(v).onclick === "function"),
  );
}

describe("workspaces shared with the account", () => {
  it("lists an accepted workspace share as a guest card under its own section, with the owner's chip and the menu's actions, never among the account's rows", () => {
    const { view } = initPageWithShares(acceptedEntry());

    const rendered = view();
    const section = required(byId(rendered, "landing-shared-with-me"));
    const sectionText = allText(section);
    expect(sectionText).toContain("Workspaces shared with you");
    expect(sectionText).toContain("Beta");
    expect(sectionText).toContain("Owen's workspace");
    expect(menuItems(section).map((item) => item.label)).toEqual([
      "Open",
      "Open Notes",
      "Decline",
      "Report as unwanted",
      "Remove from this list",
    ]);
    // The share's id appears exactly once: in the section, not in the own rows above it.
    expect(
      withAttr(rendered, "data-agent-id").filter((v) => attrsOf(v)["data-agent-id"] === SHARE_KEY),
    ).toHaveLength(1);
    expect(withAttr(rendered, "data-share-key")).toHaveLength(1);
    // No Invitations tab: nothing waits.
    expect(tabsOf(rendered).map((tab) => tab.id)).toEqual(["workspaces", "apps"]);
  });

  it("says the workspace has not registered its address when no target has a link, and opens nothing", () => {
    const share = sharedWorkspace();
    const { view } = initPageWithShares(
      acceptedEntry({ targets: share.targets.map((target) => ({ ...target, link: null })) }),
    );

    const section = required(byId(view(), "landing-shared-with-me"));
    expect(allText(section)).toContain("The workspace has not registered its address yet");
    expect(menuItems(section).map((item) => item.label)).not.toContain("Open");
    const card = required(withAttr(section, "data-share-key")[0]);
    expect(attrsOf(card).role).toBeUndefined();
  });

  it("opens the shell on a card click and an app from the menu, through the shell", async () => {
    const { shell, view } = initPageWithShares(acceptedEntry());
    const open = vi.spyOn(shell, "openSharedWorkspace").mockResolvedValue(undefined);

    const rendered = view();
    const card = required(withAttr(rendered, "data-share-key")[0]);
    (attrsOf(card).onclick as () => void)();
    // A card holds its actions while one is in flight; the second open waits for the first to settle.
    await settle();
    menuItem(view(), "Open Notes").onSelect();
    await settle();

    expect(open).toHaveBeenNthCalledWith(1, SHARE_KEY, null);
    expect(open).toHaveBeenNthCalledWith(2, SHARE_KEY, "notes");
  });

  it("reads Connecting on the card while the open's round trips run, and holds its actions", async () => {
    const { shell, view } = initPageWithShares(acceptedEntry());
    let finishOpen: () => void = () => undefined;
    vi.spyOn(shell, "openSharedWorkspace").mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          finishOpen = resolve;
        }),
    );

    const card = required(withAttr(view(), "data-share-key")[0]);
    (attrsOf(card).onclick as () => void)();
    const busy = view();
    expect(withAttr(busy, "data-badge").map((badge) => attrsOf(badge)["data-badge"])).toContain("connecting");
    expect(attrsOf(required(withAttr(busy, "data-share-key")[0])).onclick).toBeUndefined();

    finishOpen();
    await settle();
    expect(withAttr(view(), "data-badge").map((badge) => attrsOf(badge)["data-badge"])).not.toContain("connecting");
  });

  it("shows the last verdict as a badge with Retry in the menu", () => {
    const { view } = initPageWithShares(
      acceptedEntry({ verdict: "refused", verdict_at: "2026-11-04T10:00:00+00:00" }),
    );

    const section = required(byId(view(), "landing-shared-with-me"));
    const verdict = required(withAttr(section, "data-share-verdict")[0]);
    expect(allText(verdict)).toBe("No longer shared with you");
    expect(attrsOf(verdict).tone).toBe("alarm");
    expect(menuItems(section).map((item) => item.label)).toContain("Retry");
  });

  it("does not open a share whose tunnel reads as down, and offers Retry as the override", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(Date.parse("2026-11-04T12:00:00+00:00"));
      const { view } = initPageWithShares(acceptedEntry({ last_seen_at: "2026-11-04T10:00:00+00:00" }));
      const section = required(byId(view(), "landing-shared-with-me"));

      expect(allText(section)).toContain("Offline");
      expect(attrsOf(required(withAttr(section, "data-share-key")[0])).role).toBeUndefined();
      const items = menuItems(section);
      expect(items.filter((item) => item.label.startsWith("Open")).every((item) => item.isDisabled)).toBe(true);
      expect(items.map((item) => item.label)).toContain("Retry");
    } finally {
      vi.useRealTimers();
    }
  });

  it("retries by forgetting the verdict through the retry route and then opening the shell target", async () => {
    const { shell, view } = initPageWithShares(
      acceptedEntry({ verdict: "offline", verdict_at: "2026-11-04T10:00:00+00:00" }),
    );
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      fetchAnswering((url: string) => {
        requests.push(url);
        return jsonResponse({ share_key: SHARE_KEY, row: sharedEntry().share });
      }),
    );
    const open = vi.spyOn(shell, "openSharedWorkspace").mockResolvedValue(undefined);

    menuItem(view(), "Retry").onSelect();
    await settle();

    expect(requests).toEqual([`/ui/api/shared-with-me/${SHARE_KEY}/retry?account=user-alice`]);
    expect(open).toHaveBeenCalledWith(SHARE_KEY, null);
  });

  it("reports only after the confirmation, through the report route under the window's account", async () => {
    const { view } = initPageWithShares(acceptedEntry());
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      fetchAnswering((url: string) => {
        requests.push(url);
        return jsonResponse({ share_key: SHARE_KEY, response: "reported", row: null });
      }),
    );
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
    vi.stubGlobal("window", { ...globalThis.window, confirm });

    menuItem(view(), "Report as unwanted").onSelect();
    menuItem(view(), "Report as unwanted").onSelect();
    await settle();

    expect(confirm).toHaveBeenCalledTimes(2);
    expect(requests).toEqual([`/ui/api/shared-with-me/${SHARE_KEY}/report?account=user-alice`]);
  });

  it("declines and removes an accepted share through their routes under the window's account", async () => {
    const requests: { url: string; method: string }[] = [];
    const { view } = initPageWithShares(acceptedEntry());
    vi.stubGlobal(
      "fetch",
      fetchAnswering((url: string, init?: RequestInit) => {
        requests.push({ url, method: String(init?.method) });
        return jsonResponse({ share_key: SHARE_KEY, response: null, row: sharedEntry().share });
      }),
    );

    menuItem(view(), "Decline").onSelect();
    await settle();
    menuItem(view(), "Remove from this list").onSelect();
    await settle();

    expect(requests).toEqual([
      { url: `/ui/api/shared-with-me/${SHARE_KEY}/decline?account=user-alice`, method: "POST" },
      { url: `/ui/api/shared-with-me/${SHARE_KEY}/remove?account=user-alice`, method: "POST" },
    ]);
  });

  it("adds a pasted link through the links route and clears the input", async () => {
    const { view } = initPageWithShares(acceptedEntry());
    const requests: { url: string; body: string }[] = [];
    vi.stubGlobal(
      "fetch",
      fetchAnswering((url: string, init?: RequestInit) => {
        requests.push({ url, body: String(init?.body ?? "") });
        return jsonResponse({ row: sharedEntry().share });
      }),
    );

    const input = required(byId(view(), "shared-with-me-link"));
    (attrsOf(input).oninput as (event: unknown) => void)({ target: { value: "https://shell-1.other.example/x" } });
    (attrsOf(required(byId(view(), "shared-with-me-add-link"))).onclick as () => void)();
    await settle();

    expect(requests).toEqual([
      {
        url: "/ui/api/shared-with-me/links?account=user-alice",
        body: JSON.stringify({ link: "https://shell-1.other.example/x", label: "" }),
      },
    ]);
    expect(attrsOf(required(byId(view(), "shared-with-me-link"))).value).toBe("");
  });

  it("draws a pasted shell link as a card reading Pasted link, and a pasted app link as an icon named by its label", () => {
    const shellLink = acceptedEntry({
      share_key: "a".repeat(40),
      host_id: "",
      is_manual: true,
      targets: [{ app: "system_interface", display_name: null, link: "https://system-interface-k3j9x2ab.ws.example/", granted_via: "unknown" }],
    });
    const appLink = sharedWorkspaceEntry(
      {
        share_key: "b".repeat(40),
        host_id: "",
        is_manual: true,
        status: "accepted",
        targets: [{ app: "system_interface", display_name: null, link: "https://reading-list-k3j9x2ab.ws.example/", granted_via: "unknown" }],
      },
      { id: "b".repeat(40), name: "reading-list-k3j9x2ab.ws.example" },
    );
    const { view } = initPageWithShares(shellLink, appLink);

    const workspaces = view();
    const section = required(byId(workspaces, "landing-shared-with-me"));
    expect(withAttr(section, "data-share-key")).toHaveLength(1);
    expect(allText(section)).toContain("Pasted link");
    expect(menuItems(section).map((item) => item.label)).toEqual(["Open", "Remove from this list"]);

    selectTab(workspaces, "apps");
    const icons = appIcons(view());
    expect(icons).toHaveLength(1);
    expect(attrsOf(icons[0]).name).toBe("reading-list");
    expect(attrsOf(icons[0]).owner).toBeUndefined();
  });
});

describe("apps shared with the account", () => {
  const appShare = (overrides: Partial<UiSharedWorkspace> = {}) =>
    acceptedEntry({
      targets: [
        { app: "notes", display_name: "Notes", link: "https://notes-1.k3j9.a1b2.us1.shares.example/", granted_via: "user" },
        { app: "crm", display_name: null, link: null, granted_via: "user" },
      ],
      ...overrides,
    });

  it("lists each target of an app share as an icon on the Apps tab with the sharer's face, and opens it by its app", async () => {
    const { shell, view } = initPageWithShares(appShare());
    const open = vi.spyOn(shell, "openSharedWorkspace").mockResolvedValue(undefined);

    const workspaces = view();
    // An app share is no workspace card.
    expect(withAttr(required(byId(workspaces, "landing-shared-with-me")), "data-share-key")).toHaveLength(0);
    selectTab(workspaces, "apps");
    const icons = appIcons(view());

    expect(icons.map((icon) => [attrsOf(icon).name, attrsOf(icon).isDisabled, attrsOf(icon).title])).toEqual([
      ["Notes", false, undefined],
      ["crm", true, "The workspace has not registered its address yet"],
    ]);
    expect((attrsOf(icons[0]).owner as { label: string }).label).toBe("Owen");
    // Every icon names its share; the row id the deep link scrolls to is on the first alone.
    expect(icons.map((icon) => attrsOf(icon)["data-share-key"])).toEqual([SHARE_KEY, SHARE_KEY]);
    expect(icons.map((icon) => attrsOf(icon).id)).toEqual([`shared-with-me-row-${SHARE_KEY}`, undefined]);
    (attrsOf(icons[0]).onOpen as () => void)();
    await settle();
    expect(open).toHaveBeenCalledWith(SHARE_KEY, "notes");
  });

  it("marks the deep link's icon and lands on the Apps tab for it", () => {
    vi.spyOn(m.route, "param").mockImplementation(((key?: string) =>
      key === "shared" ? SHARE_KEY : undefined) as typeof m.route.param);
    const { view } = initPageWithShares(appShare());

    const rendered = view();
    expect(panelOf(rendered)).toBe("apps");
    // Both of the share's icons are marked; the scroll lands on the one that carries the row id.
    const marked = withAttr(rendered, "data-share-selected");
    expect(marked).toHaveLength(2);
    expect(marked.filter((v) => attrsOf(v).id === `shared-with-me-row-${SHARE_KEY}`)).toHaveLength(1);
  });
});

describe("invitations", () => {
  it("puts an invited share on the Invitations tab, counted on the strip, with Join and Decline, and off the Workspaces tab", () => {
    const { view } = initPageWithShares(sharedEntry());

    const rendered = view();
    expect(tabsOf(rendered)).toEqual([{ id: "workspaces" }, { id: "apps" }, { id: "invitations", count: 1 }]);
    expect(withAttr(required(byId(rendered, "landing-shared-with-me")), "data-share-key")).toHaveLength(0);

    selectTab(rendered, "invitations");
    const invitations = required(byId(view(), "landing-invitations"));
    const text = allText(invitations);
    expect(text).toContain("Beta");
    expect(text).toContain("Owen invited you to a workspace");
    expect(attrsOf(buttonNamed(invitations, "Join")).disabled).toBe(false);
    expect(buttonNamed(invitations, "Decline")).toBeDefined();
    expect(menuItems(invitations).map((item) => item.label)).toEqual([
      "Decline",
      "Report as unwanted",
      "Remove from this list",
    ]);
  });

  it("invites to a workspace, not an app, when the owner also granted one of its apps in its own right, and lists that app as an icon once joined", () => {
    const share = sharedWorkspace();
    const targets = share.targets.map((target) => (target.app === "notes" ? { ...target, is_granted_as_app: true } : target));
    const invited = initPageWithShares(sharedEntry({ targets }));
    selectTab(invited.view(), "invitations");
    const card = required(byId(invited.view(), "landing-invitations"));
    expect(allText(card)).toContain("Beta");
    expect(allText(card)).toContain("Owen invited you to a workspace");
    clearAppContextForTests();

    const accepted = initPageWithShares(acceptedEntry({ targets }));
    const workspaces = accepted.view();
    expect(withAttr(required(byId(workspaces, "landing-shared-with-me")), "data-share-key")).toHaveLength(1);
    selectTab(workspaces, "apps");
    expect(appIcons(accepted.view()).map((icon) => [attrsOf(icon).name, attrsOf(icon)["data-app-name"]])).toEqual([["Notes", "notes"]]);
  });

  it("joins by opening the share's primary target through the shell: the shell for a workspace, the app for an app share", async () => {
    const workspace = initPageWithShares(sharedEntry());
    const openWorkspace = vi.spyOn(workspace.shell, "openSharedWorkspace").mockResolvedValue(undefined);
    selectTab(workspace.view(), "invitations");
    (attrsOf(buttonNamed(workspace.view(), "Join")).onclick as () => void)();
    await settle();
    expect(openWorkspace).toHaveBeenCalledWith(SHARE_KEY, null);
    clearAppContextForTests();

    const app = initPageWithShares(
      sharedEntry({ targets: [{ app: "notes", display_name: "Notes", link: "https://notes-1.k3j9.a1b2.us1.shares.example/", granted_via: "user" }] }),
    );
    const openApp = vi.spyOn(app.shell, "openSharedWorkspace").mockResolvedValue(undefined);
    selectTab(app.view(), "invitations");
    const card = required(byId(app.view(), "landing-invitations"));
    expect(allText(card)).toContain("Owen invited you to an app");
    (attrsOf(buttonNamed(card, "Join")).onclick as () => void)();
    await settle();
    expect(openApp).toHaveBeenCalledWith(SHARE_KEY, "notes");
  });

  it("keeps a declined card in place with Undo until the tab is left, then files it under Declined invitations", async () => {
    const requests: { url: string; method: string }[] = [];
    const { shell, view } = initPageWithShares(sharedEntry());
    vi.stubGlobal(
      "fetch",
      fetchAnswering((url: string, init?: RequestInit) => {
        requests.push({ url, method: String(init?.method) });
        return jsonResponse({ share_key: SHARE_KEY, response: "declined", row: sharedEntry().share });
      }),
    );

    selectTab(view(), "invitations");
    (attrsOf(buttonNamed(view(), "Decline")).onclick as () => void)();
    await settle();
    // The listing now says declined; the card stays where it was, reading Declined with Undo.
    const message = workspacesMessage();
    shell.stores.workspaces.applyWorkspacesMessage({
      ...message,
      workspaces: [...message.workspaces, sharedEntry({ status: "declined" })],
    });
    const stillUp = required(byId(view(), "landing-invitations"));
    expect(withAttr(stillUp, "data-invite-answer")).toHaveLength(1);
    expect(byId(stillUp, "landing-declined-invitations")).toBeUndefined();
    expect(allText(stillUp)).toContain("Declined");
    (attrsOf(buttonNamed(stillUp, "Undo")).onclick as () => void)();
    await settle();

    // Leaving the tab settles it: on return it is under the disclosure, and the tab still shows for it.
    selectTab(view(), "workspaces");
    const workspaces = view();
    expect(tabsOf(workspaces)).toEqual([{ id: "workspaces" }, { id: "apps" }, { id: "invitations", count: 0 }]);
    selectTab(workspaces, "invitations");
    const settled = required(byId(view(), "landing-invitations"));
    expect(byId(settled, "landing-no-invitations")).toBeDefined();
    expect(byId(settled, "landing-declined-invitations")).toBeDefined();
    expect(attrsOf(required(byId(settled, "landing-declined-invitations"))).summary).toBe("Declined invitations (1)");

    expect(requests).toEqual([
      { url: `/ui/api/shared-with-me/${SHARE_KEY}/decline?account=user-alice`, method: "POST" },
      { url: `/ui/api/shared-with-me/${SHARE_KEY}/decline?account=user-alice`, method: "DELETE" },
    ]);
  });

  it("lists invitations newest first and keeps a declined one in its place among them", async () => {
    const older = sharedEntry({ latest_grant_at: "2026-11-01T09:00:00+00:00" });
    const newerKey = "agent-" + "8".repeat(32);
    const newer = (status: string) =>
      sharedWorkspaceEntry(
        { share_key: newerKey, host_id: "host-" + "8".repeat(32), latest_grant_at: "2026-11-05T09:00:00+00:00", status },
        { id: newerKey, name: "Gamma" },
      );
    const { shell, view } = initPageWithShares(older, newer("invited"));
    vi.stubGlobal(
      "fetch",
      fetchAnswering(() => jsonResponse({ share_key: newerKey, response: "declined", row: newer("declined").share })),
    );
    const keysOf = (node: unknown) => withAttr(node, "data-share-key").map((v) => attrsOf(v)["data-share-key"]);

    selectTab(view(), "invitations");
    expect(keysOf(byId(view(), "landing-invitations"))).toEqual([newerKey, SHARE_KEY]);

    const newerCard = required(withAttr(view(), "data-share-key")[0]);
    (attrsOf(buttonNamed(newerCard, "Decline")).onclick as () => void)();
    await settle();
    const message = workspacesMessage();
    shell.stores.workspaces.applyWorkspacesMessage({
      ...message,
      workspaces: [...message.workspaces, older, newer("declined")],
    });

    const cards = withAttr(required(byId(view(), "landing-invitations")), "data-share-key");
    expect(cards.map((v) => [attrsOf(v)["data-share-key"], attrsOf(v)["data-invite-answer"]])).toEqual([
      [newerKey, "declined"],
      [SHARE_KEY, undefined],
    ]);
  });

  it("files a share declined from its guest card under Declined invitations, even with Invitations the remembered tab", async () => {
    // The window remembers Invitations, but nothing is on it, so the page falls back to Workspaces.
    sessionStorage.setItem("home-tab", "invitations");
    const { shell, view } = initPageWithShares(acceptedEntry());
    vi.stubGlobal(
      "fetch",
      fetchAnswering(() => jsonResponse({ share_key: SHARE_KEY, response: "declined", row: sharedEntry().share })),
    );
    const workspaces = view();
    expect(panelOf(workspaces)).toBe("workspaces");

    menuItem(byId(workspaces, "landing-shared-with-me"), "Decline").onSelect();
    await settle();
    const message = workspacesMessage();
    shell.stores.workspaces.applyWorkspacesMessage({
      ...message,
      workspaces: [...message.workspaces, sharedEntry({ status: "declined" })],
    });

    selectTab(view(), "invitations");
    const invitations = required(byId(view(), "landing-invitations"));
    expect(byId(invitations, "landing-no-invitations")).toBeDefined();
    expect(byId(invitations, "landing-declined-invitations")).toBeDefined();
  });

  it("lands the deep link's declined invitation on the Invitations tab with the declined disclosure open", () => {
    vi.spyOn(m.route, "param").mockImplementation(((key?: string) =>
      key === "shared" ? SHARE_KEY : undefined) as typeof m.route.param);
    const { view } = initPageWithShares(sharedEntry({ status: "declined" }));

    const rendered = view();
    expect(panelOf(rendered)).toBe("invitations");
    const disclosure = required(byId(rendered, "landing-declined-invitations"));
    expect(attrsOf(disclosure).isOpen).toBe(true);
    const marked = withAttr(disclosure, "data-share-selected");
    expect(marked).toHaveLength(1);
    expect(attrsOf(marked[0])["data-invite-answer"]).toBe("declined");
  });

  it("lands the deep link's invitation on the Invitations tab, marked and scrolled to", () => {
    vi.spyOn(m.route, "param").mockImplementation(((key?: string) =>
      key === "shared" ? SHARE_KEY : undefined) as typeof m.route.param);
    const { view } = initPageWithShares(sharedEntry());

    const rendered = view();
    expect(panelOf(rendered)).toBe("invitations");
    const marked = withAttr(rendered, "data-share-selected");
    expect(marked).toHaveLength(1);
    expect(attrsOf(marked[0]).id).toBe(`shared-with-me-row-${SHARE_KEY}`);
  });
});
