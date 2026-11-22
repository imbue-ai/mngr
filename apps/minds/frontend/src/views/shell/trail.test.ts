import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { UiWorkspaceEntry } from "../../channel/messages";
import { createEmptyStores } from "../../models/boot";
import type { AnyVnode } from "../../testing";
import {
  CREATE_ATTEMPT_ID,
  accountEntry,
  accountsMessage,
  allText,
  attrsOf,
  byId,
  classTokensOf,
  collectText,
  collectVnodes,
  createAttemptEntry,
  namedSharedOwner,
  required,
  secondAccountEntry,
  sharedWorkspace,
  sharedWorkspaceEntry,
  thirdAccountEntry,
  withAttr,
  workspacesMessage,
} from "../../testing";
import { MonogramTile } from "../components/MonogramTile";
import { ShellState } from "./shell-state";
import type { TrailWorkspace } from "./trail";
import {
  accountButtonContent,
  accountButtonLabel,
  barStatusWorkspaceAgentId,
  barWorkspaceAnyId,
  displayedSharedEntry,
  trailWorkspaceFor,
  workspaceButtonContent,
  workspaceSwatch,
} from "./trail";

const ALICE = accountEntry({ display_name: "Alice Nguyen" });
const BOB = secondAccountEntry();
const CAROL = thirdAccountEntry();

const OWNED_HOST_ID = "host-bb22";
const OWNED: UiWorkspaceEntry = { ...workspacesMessage().workspaces[0], host_id: OWNED_HOST_ID };
const SAM = namedSharedOwner();
const SHELL_TARGET = sharedWorkspace().targets[0];
const NOTES_TARGET = sharedWorkspace().targets[1];

function shellWith(workspaces: UiWorkspaceEntry[] = [OWNED]): ShellState {
  const shell = new ShellState(createEmptyStores());
  shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage({ workspaces }));
  shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE, BOB], ALICE.user_id, [CAROL]));
  return shell;
}

/** The names of the icons drawn in `content`. */
function iconNames(content: unknown): unknown[] {
  return withAttr(content, "name")
    .filter((vnode) => vnode.tag !== MonogramTile)
    .map((vnode) => attrsOf(vnode).name);
}

function swatchDot(swatch: unknown): AnyVnode {
  return required(withAttr(swatch, "data-workspace-swatch")[0]);
}

describe("workspaceSwatch", () => {
  it("draws the workspace's colour as a 12px dot in a 16px slot", () => {
    const slot = required(collectVnodes(workspaceSwatch("#aabbcc"))[0]);
    const dot = swatchDot(slot);

    expect(classTokensOf(slot)).toEqual(expect.arrayContaining(["w-4", "h-4", "shrink-0"]));
    expect(attrsOf(slot)["aria-hidden"]).toBe("true");
    expect(classTokensOf(dot)).toEqual(expect.arrayContaining(["w-3", "h-3", "rounded-full", "border"]));
    expect(attrsOf(dot)["data-workspace-swatch"]).toBe("#aabbcc");
    expect(attrsOf(dot).style).toBe("background-color: #aabbcc;");
    expect(classTokensOf(dot)).not.toContain("workspace-swatch-on-bar");
  });

  it("leaves a workspace with no colour as the ring alone", () => {
    const dot = swatchDot(workspaceSwatch(null));

    expect(attrsOf(dot).style).toBeUndefined();
    expect(attrsOf(dot)["data-workspace-swatch"]).toBe("");
    expect(classTokensOf(dot)).toContain("border");

    expect(attrsOf(swatchDot(workspaceSwatch(null, { isOnBar: true }))).style).toBeUndefined();
  });

  it("hands the colour to the bar's tint instead of painting it, on the bar", () => {
    // The bar is painted in this same colour: a dot filled with it would vanish.
    const dot = swatchDot(workspaceSwatch("#aabbcc", { isOnBar: true }));

    expect(classTokensOf(dot)).toContain("workspace-swatch-on-bar");
    expect(attrsOf(dot).style).toBe("--swatch-color: #aabbcc;");
  });

  it("fills a 24px slot for a title", () => {
    const slot = required(collectVnodes(workspaceSwatch("#aabbcc", { size: "md" }))[0]);

    expect(classTokensOf(slot)).toEqual(expect.arrayContaining(["w-6", "h-6"]));
    expect(classTokensOf(swatchDot(slot))).toEqual(expect.arrayContaining(["w-6", "h-6"]));
  });
});

describe("accountButtonLabel", () => {
  it("names the window's account by its email even when it has a display name, the device as 'This device', and an unknown account by its id", () => {
    const shell = shellWith();
    shell.accountId = ALICE.user_id;
    expect(accountButtonLabel(shell)).toEqual({ name: ALICE.email, isSignedOut: false });
    shell.accountId = BOB.user_id;
    expect(accountButtonLabel(shell)).toEqual({ name: BOB.email, isSignedOut: false });
    shell.accountId = "local";
    expect(accountButtonLabel(shell)).toEqual({ name: "This device", isSignedOut: false });
    shell.accountId = "user-unknown";
    expect(accountButtonLabel(shell)).toEqual({ name: "user-unknown", isSignedOut: true });
  });

  it("marks an account with no session here as signed out, until it signs in again", () => {
    const shell = shellWith();
    shell.accountId = CAROL.user_id;
    expect(accountButtonLabel(shell)).toEqual({ name: CAROL.email, isSignedOut: true });

    shell.stores.accounts.applyAccountsMessage(accountsMessage([ALICE, BOB, CAROL], ALICE.user_id, []));
    expect(accountButtonLabel(shell)).toEqual({ name: CAROL.email, isSignedOut: false });
  });

  it("falls back to the display name for an account with no email", () => {
    const shell = shellWith();
    const nameless = accountEntry({ user_id: "user-9", email: "", display_name: "Dana" });
    shell.stores.accounts.applyAccountsMessage(accountsMessage([nameless], nameless.user_id));
    shell.accountId = nameless.user_id;

    expect(accountButtonLabel(shell).name).toBe("Dana");
  });
});

describe("accountButtonContent", () => {
  function spans(content: unknown): AnyVnode[] {
    return collectVnodes(content).filter((node) => node.tag === "span");
  }

  it("draws the mark as its own non-shrinking node beside the truncating name, so a long email never hides it", () => {
    const shell = shellWith();
    shell.accountId = CAROL.user_id;

    const nodes = spans(accountButtonContent(shell, false, "the-name"));

    expect(nodes.map((node) => [attrsOf(node).id, allText(node)])).toEqual([
      ["the-name", CAROL.email],
      [undefined, "(signed out)"],
    ]);
    expect(classTokensOf(nodes[0])).toEqual(expect.arrayContaining(["truncate", "max-w-[220px]"]));
    expect(attrsOf(nodes[1])["data-account-signed-out"]).toBe("true");
    expect(classTokensOf(nodes[1])).toContain("shrink-0");
    expect(classTokensOf(nodes[1])).not.toContain("truncate");
  });

  it("draws only the email for a signed-in account, and no id when the caller is the raised copy", () => {
    const shell = shellWith();
    shell.accountId = ALICE.user_id;

    const nodes = spans(accountButtonContent(shell, false, null));

    expect(nodes.map((node) => [attrsOf(node).id, allText(node)])).toEqual([[undefined, ALICE.email]]);
  });

  it("ends with a chevron that turns over while the menu is open", () => {
    const shell = shellWith();

    expect(iconNames(accountButtonContent(shell, false, null))).toEqual(["chevron-down-small"]);
    expect(iconNames(accountButtonContent(shell, true, null))).toEqual(["chevron-up"]);
  });
});

describe("trailWorkspaceFor", () => {
  it("names nothing where the bar shows no workspace", () => {
    expect(trailWorkspaceFor(shellWith(), null)).toBeNull();
  });

  it("names one of the account's own workspaces with its colour and no owner, by either of its ids", () => {
    const shell = shellWith();
    const expected: TrailWorkspace = { name: "alpha", accent: "#aabbcc", owner: null, isSharedApp: false };

    expect(trailWorkspaceFor(shell, OWNED.id)).toEqual(expected);
    expect(trailWorkspaceFor(shell, OWNED_HOST_ID)).toEqual(expected);
  });

  it("holds the name's place for a workspace the list has not named yet", () => {
    expect(trailWorkspaceFor(shellWith(), "agent-ffff")).toEqual({
      name: "…",
      accent: null,
      owner: null,
      isSharedApp: false,
    });
  });

  it("names a shared workspace with the owner's label: their name, else their email", () => {
    const named = sharedWorkspaceEntry({ status: "accepted", owner: SAM });
    expect(trailWorkspaceFor(shellWith([OWNED, named]), named.id)).toEqual({
      name: "Beta",
      accent: "#112233",
      owner: "Sam Okafor",
      isSharedApp: false,
    });

    const unnamed = sharedWorkspaceEntry({ status: "accepted" });
    expect(trailWorkspaceFor(shellWith([unnamed]), unnamed.id)?.owner).toBe("owner@acme.example");
  });

  it("names nobody for a pasted link, which carries no owner", () => {
    const pasted = sharedWorkspaceEntry({ is_manual: true });

    expect(trailWorkspaceFor(shellWith([pasted]), pasted.id)?.owner).toBeNull();
  });

  it("names the one app of an app share by its display name, as a shared app", () => {
    const appShare = sharedWorkspaceEntry({ status: "accepted", owner: SAM, targets: [NOTES_TARGET] });

    expect(trailWorkspaceFor(shellWith([appShare]), appShare.id)).toEqual({
      name: "Notes",
      accent: "#112233",
      owner: "Sam Okafor",
      isSharedApp: true,
    });
  });

  it("names the app a workspace share's frame was opened on, and the workspace again once it is back on the shell", () => {
    const share = sharedWorkspaceEntry({
      status: "accepted",
      owner: SAM,
      targets: [SHELL_TARGET, { ...NOTES_TARGET, is_granted_as_app: true }],
    });
    const shell = shellWith([share]);
    expect(trailWorkspaceFor(shell, share.id)).toMatchObject({ name: "Beta", isSharedApp: false });

    shell.stores.workspaces.chooseSharedLink(share.id, required(NOTES_TARGET.link ?? undefined), null);
    expect(trailWorkspaceFor(shell, share.id)).toMatchObject({ name: "Notes", owner: "Sam Okafor", isSharedApp: true });

    shell.stores.workspaces.chooseSharedLink(share.id, required(SHELL_TARGET.link ?? undefined), null);
    expect(trailWorkspaceFor(shell, share.id)).toMatchObject({ name: "Beta", isSharedApp: false });
  });
});

describe("the workspace the bar speaks for", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  /** A shell whose route is `route`, showing `displayed` in its frame. */
  function shellOn(route: string, displayed: string | null = null): ShellState {
    const shell = shellWith([OWNED, createAttemptEntry()]);
    vi.spyOn(m.route, "get").mockReturnValue(route);
    shell.displayedWorkspaceAnyId = displayed;
    return shell;
  }

  it("is the workspace on screen, under Settings too, and the one a workspace's own page names", () => {
    const onSurface = shellOn(`/workspace/${OWNED.id}`, OWNED.id);
    expect(barWorkspaceAnyId(onSurface)).toBe(OWNED.id);
    expect(barStatusWorkspaceAgentId(onSurface)).toBe(OWNED.id);

    const underSettings = shellOn(`/settings?workspace=${OWNED.id}&tab=studio`, OWNED.id);
    expect(barWorkspaceAnyId(underSettings)).toBe(OWNED.id);
    expect(barStatusWorkspaceAgentId(underSettings)).toBe(OWNED.id);

    // A page that replaces the surface names the workspace as its route does; its status is asked by agent id.
    const onBackups = shellOn(`/workspace/${OWNED_HOST_ID}/backups`);
    expect(barWorkspaceAnyId(onBackups)).toBe(OWNED_HOST_ID);
    expect(barStatusWorkspaceAgentId(onBackups)).toBe(OWNED.id);
  });

  it("is none on a hub page", () => {
    const home = shellOn("/account/local/");

    expect(barWorkspaceAnyId(home)).toBeNull();
    expect(barStatusWorkspaceAgentId(home)).toBeNull();
  });

  it("is the attempt on a creation page, which has no status or pending request of its own yet", () => {
    const creating = shellOn(`/creating/${CREATE_ATTEMPT_ID}`);

    expect(barWorkspaceAnyId(creating)).toBe(CREATE_ATTEMPT_ID);
    expect(barStatusWorkspaceAgentId(creating)).toBeNull();
  });
});

describe("displayedSharedEntry", () => {
  it("is the shared row on screen, and null on one of the account's own workspaces or none", () => {
    const shared = sharedWorkspaceEntry({ status: "accepted" });
    const shell = shellWith([OWNED, shared]);

    expect(displayedSharedEntry(shell)).toBeNull();
    shell.displayedWorkspaceAnyId = OWNED.id;
    expect(displayedSharedEntry(shell)).toBeNull();
    shell.displayedWorkspaceAnyId = shared.id;
    expect(displayedSharedEntry(shell)).toBe(shared);
  });
});

describe("workspaceButtonContent", () => {
  const OWNED_TRAIL: TrailWorkspace = { name: "alpha", accent: "#aabbcc", owner: null, isSharedApp: false };
  const SHARED_TRAIL: TrailWorkspace = { name: "orchard", accent: "#112233", owner: "Sam Okafor", isSharedApp: false };
  const ON_BAR = { isOpen: false, isOnBar: true, nameId: "the-name" };

  it("invites opening a workspace where none is on screen: muted words, a chevron, and no dot", () => {
    const content = workspaceButtonContent(null, ON_BAR);

    const name = required(byId(content, "the-name"));
    expect(allText(name)).toBe("Open workspace");
    expect(classTokensOf(name)).toContain("text-secondary");
    expect(withAttr(content, "data-workspace-swatch")).toEqual([]);
    expect(iconNames(content)).toEqual(["chevron-down-small"]);
  });

  it("draws an owned workspace as its dot, its name, and a chevron", () => {
    const content = workspaceButtonContent(OWNED_TRAIL, ON_BAR);

    expect(attrsOf(swatchDot(content))["data-workspace-swatch"]).toBe("#aabbcc");
    const name = required(byId(content, "the-name"));
    expect(collectText(name).join("")).toBe("alpha");
    expect(classTokensOf(name)).toEqual(expect.arrayContaining(["truncate", "max-w-[220px]"]));
    expect(iconNames(content)).toEqual(["chevron-down-small"]);
    expect(collectVnodes(content).some((vnode) => vnode.tag === MonogramTile)).toBe(false);
  });

  it("follows a shared workspace's name with its owner in the muted colour", () => {
    const name = required(byId(workspaceButtonContent(SHARED_TRAIL, ON_BAR), "the-name"));

    expect(collectText(name).join("")).toBe("orchard (Sam Okafor)");
    const aside = required(collectVnodes(name.children).find((vnode) => allText(vnode) === " (Sam Okafor)"));
    expect(classTokensOf(aside)).toContain("text-secondary");
  });

  it("draws a shared app with its monogram tile in the dot's place", () => {
    const content = workspaceButtonContent({ ...SHARED_TRAIL, name: "Notes", isSharedApp: true }, ON_BAR);

    const tiles = collectVnodes(content).filter((vnode) => vnode.tag === MonogramTile);
    expect(tiles.map((tile) => attrsOf(tile).name)).toEqual(["Notes"]);
    expect(String(attrsOf(tiles[0]).extra).split(" ")).toEqual(expect.arrayContaining(["w-4", "h-4"]));
    expect(withAttr(content, "data-workspace-swatch")).toEqual([]);
    expect(collectText(required(byId(content, "the-name"))).join("")).toBe("Notes (Sam Okafor)");
  });

  it("tints the dot on the bar and paints it plainly on the open menu's raised copy", () => {
    const onBar = swatchDot(workspaceButtonContent(OWNED_TRAIL, ON_BAR));
    const onCard = swatchDot(workspaceButtonContent(OWNED_TRAIL, { isOpen: true, isOnBar: false, nameId: null }));

    expect(classTokensOf(onBar)).toContain("workspace-swatch-on-bar");
    expect(classTokensOf(onCard)).not.toContain("workspace-swatch-on-bar");
    expect(attrsOf(onCard).style).toBe("background-color: #aabbcc;");
  });

  it("turns the chevron over while the menu is open, and gives the raised copy no name id", () => {
    const open = workspaceButtonContent(OWNED_TRAIL, { isOpen: true, isOnBar: false, nameId: null });

    expect(iconNames(open)).toEqual(["chevron-up"]);
    expect(withAttr(open, "id")).toEqual([]);
    expect(iconNames(workspaceButtonContent(null, { isOpen: true, isOnBar: true, nameId: null }))).toEqual([
      "chevron-up",
    ]);
  });
});
