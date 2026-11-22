// @vitest-environment jsdom
//
// The menu's search, its highlight and its Enter all work on the rows as they
// are drawn, so the menu is mounted and driven through the real DOM.
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { UiWorkspaceEntry } from "../../channel/messages";
import { createEmptyStores } from "../../models/boot";
import { errorDialogs } from "../../models/errorDialog";
import { SharedWithMeError } from "../../models/sharedWithMe";
import { ICONS_16 } from "../components/icons";
import type { RaisedTitlebarIconsAttrs } from "./RaisedTitlebarIcons";
import { RaisedTitlebarIcons } from "./RaisedTitlebarIcons";
import { ShellState } from "./shell-state";
import { WorkspaceSwitcherMenu, statusIcon, workspaceSwitcherRows } from "./WorkspaceSwitcher";
import {
  attrsOf,
  byId,
  collectVnodes,
  createAttemptEntry,
  mountInBody,
  namedSharedOwner,
  placeMeasuredElement,
  required,
  settle,
  sharedWorkspace,
  sharedWorkspaceEntry,
  workspacesMessage,
} from "../../testing";

const ALPHA: UiWorkspaceEntry = workspacesMessage().workspaces[0];
const BRAVO_HOST_ID = "host-cc33";
const BRAVO: UiWorkspaceEntry = {
  ...ALPHA,
  id: "agent-bb22",
  host_id: BRAVO_HOST_ID,
  name: "bravo",
  accent: "",
  liveness: "STOPPED",
};
const ATTEMPT = createAttemptEntry();
const REMOTE: UiWorkspaceEntry = { ...ALPHA, id: "agent-dd44", host_id: "host-dd55", name: "delta", is_remote: true };

const SAM = namedSharedOwner();
const PRIYA = { ...sharedWorkspace().owner, user_id: "priya-id", email: "priya@lab.example", display_name: null };
const SHELL_TARGET = sharedWorkspace().targets[0];
const NOTES_TARGET = { ...sharedWorkspace().targets[1], is_granted_as_app: true };
const NOTES_LINK = required(NOTES_TARGET.link ?? undefined);
const LEDGER_TARGET = {
  app: "ledger",
  display_name: "Ledger",
  link: "https://ledger-1.p0q1.us1.shares.example/",
  granted_via: "user",
};

function shareKey(digit: string): string {
  return "agent-" + digit.repeat(32);
}

/** A whole workspace Sam shared, with Notes also granted as an app in its own right. */
const ORCHARD = sharedWorkspaceEntry(
  {
    share_key: shareKey("1"),
    host_id: "host-" + "1".repeat(32),
    status: "accepted",
    owner: SAM,
    targets: [SHELL_TARGET, NOTES_TARGET],
  },
  { name: "orchard", accent: "#225533" },
);
/** One app Priya shared on its own. */
const LEDGER = sharedWorkspaceEntry(
  {
    share_key: shareKey("2"),
    host_id: "host-" + "2".repeat(32),
    status: "accepted",
    owner: PRIYA,
    targets: [LEDGER_TARGET],
  },
  { name: "books" },
);
const INVITED = sharedWorkspaceEntry(
  { share_key: shareKey("3"), host_id: "host-" + "3".repeat(32), status: "invited" },
  { name: "unanswered" },
);
const DECLINED = sharedWorkspaceEntry(
  { share_key: shareKey("4"), host_id: "host-" + "4".repeat(32), status: "declined" },
  { name: "turned-down" },
);
/** A link to a whole workspace the account pasted in: listed without ever being accepted, and naming no owner. */
const PASTED = sharedWorkspaceEntry(
  {
    share_key: shareKey("5"),
    host_id: "host-" + "5".repeat(32),
    is_manual: true,
    targets: [{ ...SHELL_TARGET, link: "https://system-interface-aaaaaaaa.ws.example/" }],
  },
  { name: "pasted" },
);

const EVERYTHING = [ALPHA, BRAVO, ATTEMPT, REMOTE, ORCHARD, LEDGER, INVITED, DECLINED];

let mountedRoot: HTMLElement | null = null;
let scrolledIntoView: Element[] = [];

beforeEach(() => {
  // jsdom lays nothing out and has no scrollIntoView; the menu calls it to keep the arrow keys' row in view.
  scrolledIntoView = [];
  Element.prototype.scrollIntoView = function (this: Element) {
    scrolledIntoView.push(this);
  };
});

afterEach(() => {
  if (mountedRoot !== null) m.mount(mountedRoot, null);
  mountedRoot = null;
  document.body.replaceChildren();
  delete window.mindsNative;
  delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
  vi.restoreAllMocks();
});

/** A shell whose account lists `workspaces`, showing `displayed` (null for a hub page), with its shortcut reading
 * `shortcutLabel`. */
function makeShell(
  workspaces: UiWorkspaceEntry[],
  displayed: string | null,
  shortcutLabel: string | null = null,
): ShellState {
  const shell = new ShellState(createEmptyStores());
  shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage({ workspaces }));
  shell.displayedWorkspaceAnyId = displayed;
  vi.spyOn(shell, "workspaceSwitcherShortcutLabel").mockResolvedValue(shortcutLabel);
  return shell;
}

function pathData(liveness: string): string[] {
  return collectVnodes(statusIcon(liveness))
    .filter((vnode) => vnode.tag === "path")
    .map((vnode) => String(attrsOf(vnode).d));
}

describe("statusIcon", () => {
  it("gives every segment of every status icon its own path", () => {
    for (const liveness of ["STOPPED", "STOPPING", "STARTING", "UNKNOWN"]) {
      const paths = pathData(liveness);
      expect(paths.length).toBeGreaterThan(0);
      for (const d of paths) {
        expect(d.match(/[Mm]/g)).toEqual([d[0]]);
      }
    }
  });

  it("renders nothing for a running workspace", () => {
    expect(statusIcon("RUNNING")).toBeNull();
  });
});

describe("workspaceSwitcherRows", () => {
  function listed(shell: ShellState, query = ""): { owned: string[]; sharedWorkspaces: string[]; sharedApps: string[] } {
    const rows = workspaceSwitcherRows(shell, query);
    return {
      owned: rows.owned.map((entry) => entry.name),
      sharedWorkspaces: rows.sharedWorkspaces.map((entry) => entry.name),
      sharedApps: rows.sharedApps.map((app) => app.name),
    };
  }

  /** What the rows name as on screen: the own workspace's id, the shared workspace's name, the shared app's name. */
  function current(shell: ShellState, query = ""): (string | null)[] {
    const rows = workspaceSwitcherRows(shell, query);
    return [rows.currentOwnedId, rows.currentSharedWorkspace?.name ?? null, rows.currentSharedApp?.name ?? null];
  }

  it("lists, from Home, every workspace of the account in the list's order, the accepted workspace shares, and the shared apps", () => {
    const shell = makeShell(EVERYTHING, null);

    expect(listed(shell)).toEqual({
      owned: ["alpha", "bravo", "gamma", "delta"],
      sharedWorkspaces: ["orchard"],
      // The app a workspace share grants in its own right, and every target of an app share.
      sharedApps: ["Notes", "Ledger"],
    });
    expect(current(shell)).toEqual([null, null, null]);
  });

  it("leaves out invitations and declined shares, and keeps a pasted link", () => {
    const shell = makeShell([INVITED, DECLINED, PASTED], null);

    expect(listed(shell)).toEqual({ owned: [], sharedWorkspaces: ["pasted"], sharedApps: [] });
  });

  it("keeps the account's own workspace on screen in its list and names it, shown by either of its ids", () => {
    for (const displayed of [BRAVO.id, BRAVO_HOST_ID]) {
      const shell = makeShell(EVERYTHING, displayed);

      expect(listed(shell).owned, displayed).toEqual(["alpha", "bravo", "gamma", "delta"]);
      expect(current(shell), displayed).toEqual([BRAVO.id, null, null]);
    }
  });

  it("keeps the shared workspace on screen in its list and names it, and none of its apps", () => {
    const shell = makeShell(EVERYTHING, ORCHARD.id);

    expect(listed(shell)).toEqual({
      owned: ["alpha", "bravo", "gamma", "delta"],
      sharedWorkspaces: ["orchard"],
      sharedApps: ["Notes", "Ledger"],
    });
    expect(current(shell)).toEqual([null, "orchard", null]);
  });

  it("names the shared app on screen on its own, and not the workspace it belongs to", () => {
    const shell = makeShell(EVERYTHING, ORCHARD.id);
    shell.stores.workspaces.chooseSharedLink(ORCHARD.id, NOTES_LINK, null);
    expect(listed(shell)).toMatchObject({ sharedWorkspaces: ["orchard"], sharedApps: ["Notes", "Ledger"] });
    expect(current(shell)).toEqual([null, null, "Notes"]);

    const onAppShare = makeShell(EVERYTHING, LEDGER.id);
    expect(listed(onAppShare)).toMatchObject({ sharedWorkspaces: ["orchard"], sharedApps: ["Notes", "Ledger"] });
    expect(current(onAppShare)).toEqual([null, null, "Ledger"]);
  });

  it("narrows away the row of what is on screen like any other, and still names it", () => {
    const shell = makeShell(EVERYTHING, BRAVO.id);

    expect(listed(shell, "alp").owned).toEqual(["alpha"]);
    expect(current(shell, "alp")).toEqual([BRAVO.id, null, null]);
  });

  it("narrows every section by a case-insensitive substring of the name", () => {
    const shell = makeShell(EVERYTHING, null);

    expect(listed(shell, "RAV")).toEqual({ owned: ["bravo"], sharedWorkspaces: [], sharedApps: [] });
    expect(listed(shell, "Orch")).toEqual({ owned: [], sharedWorkspaces: ["orchard"], sharedApps: [] });
    expect(listed(shell, "led")).toEqual({ owned: [], sharedWorkspaces: [], sharedApps: ["Ledger"] });
    expect(listed(shell, "L")).toEqual({ owned: ["alpha", "delta"], sharedWorkspaces: [], sharedApps: ["Ledger"] });
  });

  it("matches a shared row on its owner's label as well, and an owned row on its name alone", () => {
    const shell = makeShell(EVERYTHING, null);

    expect(listed(shell, "okafor")).toEqual({ owned: [], sharedWorkspaces: ["orchard"], sharedApps: ["Notes"] });
    expect(listed(shell, "priya@lab")).toEqual({ owned: [], sharedWorkspaces: [], sharedApps: ["Ledger"] });
  });

  it("lists everything for a search that is only spaces, and nothing for one nothing contains", () => {
    const shell = makeShell(EVERYTHING, null);

    expect(listed(shell, "   ")).toEqual(listed(shell, ""));
    expect(listed(shell, "zzz")).toEqual({ owned: [], sharedWorkspaces: [], sharedApps: [] });
  });
});

/** Mount the open menu, and let its read of the shortcut's label land. */
async function openMenu(shell: ShellState): Promise<HTMLElement> {
  shell.openWorkspaceSwitcher();
  mountedRoot = mountInBody(() => m(WorkspaceSwitcherMenu, { shell }));
  await settle();
  m.redraw.sync();
  return theMenu();
}

function theMenu(): HTMLElement {
  return required(document.querySelector<HTMLElement>("#workspace-switcher-menu") ?? undefined);
}

function searchField(): HTMLInputElement {
  return required(document.querySelector<HTMLInputElement>("#workspace-switcher-search") ?? undefined);
}

function typeInSearch(text: string): void {
  const field = searchField();
  field.value = text;
  field.dispatchEvent(new Event("input", { bubbles: true }));
  m.redraw.sync();
}

function pressKey(key: string): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
  searchField().dispatchEvent(event);
  m.redraw.sync();
  return event;
}

function row(navKey: string): HTMLButtonElement {
  const found = [...theMenu().querySelectorAll<HTMLButtonElement>("[data-nav-key]")].find(
    (candidate) => candidate.dataset.navKey === navKey,
  );
  return required(found);
}

function clickRow(navKey: string): void {
  row(navKey).click();
  m.redraw.sync();
}

function navKeys(): (string | undefined)[] {
  return [...theMenu().querySelectorAll<HTMLElement>("[data-nav-key]")].map((candidate) => candidate.dataset.navKey);
}

function highlighted(): (string | undefined)[] {
  return [...theMenu().querySelectorAll<HTMLElement>("[data-highlighted]")].map((candidate) => candidate.dataset.navKey);
}

/** What a row says: its name, then what it carries at its right (a badge, an owner), without its decorative mark. */
function rowText(element: Element): string {
  return [...element.children]
    .filter((child) => child.tagName === "SPAN" && !child.hasAttribute("aria-hidden"))
    .map((child) => child.textContent ?? "")
    .filter((text) => text !== "")
    .join(" | ");
}

/** Whether a row ends with the check that marks what is on screen. */
function endsWithCheck(element: Element): boolean {
  const last = element.lastElementChild;
  const artwork = last?.tagName.toLowerCase() === "svg" ? last.querySelector("path")?.getAttribute("d") : null;
  return typeof artwork === "string" && ICONS_16.check.includes(artwork);
}

/** The menu below its search field, top to bottom: headings in brackets, rules as "---", and each row's text, with
 * "(on screen)" after the row marked as what the window shows. */
function outline(): string[] {
  return [...theMenu().querySelectorAll("p, [role=separator], [data-nav-key]")].map((element) => {
    if (element.tagName === "P") return `[${element.textContent ?? ""}]`;
    if (element.getAttribute("role") === "separator") return "---";
    return rowText(element) + (element.getAttribute("aria-current") === "true" ? " (on screen)" : "");
  });
}

describe("WorkspaceSwitcherMenu", () => {
  it("renders nothing while it is closed", () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    mountedRoot = mountInBody(() => m(WorkspaceSwitcherMenu, { shell }));

    expect(mountedRoot.childElementCount).toBe(0);
    expect(shell.workspaceSwitcherShortcutLabel).not.toHaveBeenCalled();
  });

  it("lists what the window could show in three sections under the search, the workspace on screen marked, then its settings", async () => {
    const menu = await openMenu(makeShell(EVERYTHING, ALPHA.id));

    expect(menu.getAttribute("role")).toBe("menu");
    expect(outline()).toEqual([
      "---",
      "[Your workspaces]",
      "alpha (on screen)",
      "bravo",
      "gamma | Creating…",
      "delta",
      "New workspace",
      "---",
      "[Workspaces shared with you]",
      "orchard | Sam Okafor",
      "---",
      "[Apps shared with you]",
      "Notes | Sam Okafor",
      "Ledger | priya@lab.example",
      "---",
      "Workspace & app settings",
    ]);
  });

  it("marks no row and drops the last one on Home", async () => {
    await openMenu(makeShell([ALPHA, BRAVO, ORCHARD], null));

    expect(outline()).toEqual([
      "---",
      "[Your workspaces]",
      "alpha",
      "bravo",
      "New workspace",
      "---",
      "[Workspaces shared with you]",
      "orchard | Sam Okafor",
      "---",
      "[Apps shared with you]",
      "Notes | Sam Okafor",
    ]);
  });

  it("marks a shared workspace on screen in its own section, and ends with who shared it", async () => {
    await openMenu(makeShell([ALPHA, ORCHARD], ORCHARD.id));

    expect(outline()).toEqual([
      "---",
      "[Your workspaces]",
      "alpha",
      "New workspace",
      "---",
      "[Workspaces shared with you]",
      "orchard | Sam Okafor (on screen)",
      "---",
      "[Apps shared with you]",
      "Notes | Sam Okafor",
      "---",
      "Shared by Sam Okafor",
    ]);
  });

  it("marks a shared app open on its own in its section, and not the workspace it belongs to", async () => {
    const shell = makeShell([ALPHA, ORCHARD, LEDGER], ORCHARD.id);
    shell.stores.workspaces.chooseSharedLink(ORCHARD.id, NOTES_LINK, null);
    await openMenu(shell);

    expect(outline()).toEqual([
      "---",
      "[Your workspaces]",
      "alpha",
      "New workspace",
      "---",
      "[Workspaces shared with you]",
      "orchard | Sam Okafor",
      "---",
      "[Apps shared with you]",
      "Notes | Sam Okafor (on screen)",
      "Ledger | priya@lab.example",
      "---",
      "Shared by Sam Okafor",
    ]);
  });

  it("leaves out a shared section with nothing to list, with its heading and its rule", async () => {
    await openMenu(makeShell([ALPHA, LEDGER], ALPHA.id));

    expect(outline()).toEqual([
      "---",
      "[Your workspaces]",
      "alpha (on screen)",
      "New workspace",
      "---",
      "[Apps shared with you]",
      "Ledger | priya@lab.example",
      "---",
      "Workspace & app settings",
    ]);
  });

  it("is the search, the first heading and New workspace for an account with nothing to list", async () => {
    await openMenu(makeShell([], null));

    expect(outline()).toEqual(["---", "[Your workspaces]", "New workspace"]);
    expect(document.querySelector("#workspace-switcher-no-match")).toBeNull();
  });

  it("heads the account's own list 'Your workspaces' whatever the window shows", async () => {
    for (const displayed of [ALPHA.id, ORCHARD.id, null]) {
      const shell = makeShell(EVERYTHING, displayed);
      await openMenu(shell);

      const headings = outline().filter((line) => line.startsWith("["));
      expect(headings, String(displayed)).toEqual([
        "[Your workspaces]",
        "[Workspaces shared with you]",
        "[Apps shared with you]",
      ]);

      shell.closeWorkspaceSwitcher();
      m.redraw.sync();
      m.mount(required(mountedRoot ?? undefined), null);
    }
  });

  it("puts New workspace at the end of the account's own list, with no rule above it and the shared sections after", async () => {
    await openMenu(makeShell(EVERYTHING, ALPHA.id));

    const lines = outline();
    const at = lines.indexOf("New workspace");
    expect(lines[at - 1]).toBe("delta");
    expect(lines.slice(at + 1, at + 3)).toEqual(["---", "[Workspaces shared with you]"]);
    // In the account's own list, not under it.
    expect(row("new").parentElement?.id).toBe("workspace-switcher-owned");
    expect(row(`ws:${REMOTE.id}`).closest("#workspace-switcher-owned")).not.toBeNull();
  });

  it("keeps a rule above the last row, which opens Settings", async () => {
    for (const [displayed, lastRow] of [
      [ALPHA.id, "Workspace & app settings"],
      [ORCHARD.id, "Shared by Sam Okafor"],
    ]) {
      const shell = makeShell(EVERYTHING, displayed);
      await openMenu(shell);

      expect(outline().slice(-2), lastRow).toEqual(["---", lastRow]);

      shell.closeWorkspaceSwitcher();
      m.redraw.sync();
      m.mount(required(mountedRoot ?? undefined), null);
    }
  });

  it("starts every row at the same left edge, with its mark in one 16px column", async () => {
    await openMenu(makeShell(EVERYTHING, ALPHA.id));

    const rows = [...theMenu().querySelectorAll<HTMLElement>("[data-nav-key]")];
    expect(rows.length).toBe(9);
    for (const each of rows) {
      const navKey = each.dataset.navKey;
      expect(each.classList.contains("px-2"), navKey).toBe(true);
      expect([...each.classList].filter((token) => token.includes("pl-")), navKey).toEqual([]);
      const mark = required(each.firstElementChild ?? undefined);
      expect(mark.classList.contains("w-4") && mark.classList.contains("h-4"), navKey).toBe(true);
    }
  });

  it("puts the cursor in the search field as it opens", async () => {
    await openMenu(makeShell(EVERYTHING, ALPHA.id));

    const field = searchField();
    expect(document.activeElement).toBe(field);
    expect(field.placeholder).toBe("Search workspaces and apps");
    expect(field.getAttribute("aria-label")).toBe("Search workspaces and apps");
    expect(field.value).toBe("");
  });

  it("closes on a click on the dimmed window, and not on one inside the menu", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const menu = await openMenu(shell);

    menu.click();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);

    required(document.querySelector<HTMLElement>("#workspace-switcher-backdrop") ?? undefined).click();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });
});

describe("the workspace menu's shortcut hint", () => {
  it("shows the shortcut's current label at the search field's right", async () => {
    await openMenu(makeShell(EVERYTHING, ALPHA.id, "⌘K"));

    const hint = required(document.querySelector("#workspace-switcher-shortcut") ?? undefined);
    expect(hint.textContent).toBe("⌘K");
    expect(hint.parentElement).toBe(searchField().parentElement);
  });

  it("shows nothing while the shortcut is off", async () => {
    await openMenu(makeShell(EVERYTHING, ALPHA.id, null));

    expect(document.querySelector("#workspace-switcher-shortcut")).toBeNull();
  });

  it("reads the label again on each open, since the binding can change in between", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id, "⌘K");
    await openMenu(shell);

    shell.closeWorkspaceSwitcher();
    m.redraw.sync();
    vi.mocked(shell.workspaceSwitcherShortcutLabel).mockResolvedValue("Ctrl+Shift+J");
    shell.openWorkspaceSwitcher();
    m.redraw.sync();
    await settle();
    m.redraw.sync();

    expect(shell.workspaceSwitcherShortcutLabel).toHaveBeenCalledTimes(2);
    expect(document.querySelector("#workspace-switcher-shortcut")?.textContent).toBe("Ctrl+Shift+J");
  });
});

describe("the workspace menu's search", () => {
  it("narrows every section as the reader types, by name and by a shared row's owner", async () => {
    await openMenu(makeShell(EVERYTHING, ALPHA.id));

    // None of the account's own match, and their heading stays over New workspace.
    typeInSearch("okafor");
    expect(outline()).toEqual([
      "---",
      "[Your workspaces]",
      "New workspace",
      "---",
      "[Workspaces shared with you]",
      "orchard | Sam Okafor",
      "---",
      "[Apps shared with you]",
      "Notes | Sam Okafor",
      "---",
      "Workspace & app settings",
    ]);

    typeInSearch("BRA");
    expect(navKeys()).toEqual([`ws:${BRAVO.id}`, "new", "settings"]);
  });

  it("narrows the row of the workspace on screen like any other", async () => {
    await openMenu(makeShell(EVERYTHING, ALPHA.id));

    typeInSearch("alp");
    expect(navKeys()).toEqual([`ws:${ALPHA.id}`, "new", "settings"]);
    expect(row(`ws:${ALPHA.id}`).getAttribute("aria-current")).toBe("true");

    typeInSearch("bra");
    expect(navKeys()).not.toContain(`ws:${ALPHA.id}`);
  });

  it("says so in one line when nothing matches, keeping the actions", async () => {
    await openMenu(makeShell(EVERYTHING, ALPHA.id));

    typeInSearch("zzz");

    expect(document.querySelector("#workspace-switcher-no-match")?.textContent).toBe("No workspaces or apps match.");
    expect(outline()).toEqual([
      "---",
      "[No workspaces or apps match.]",
      "[Your workspaces]",
      "New workspace",
      "---",
      "Workspace & app settings",
    ]);
  });

  it("is cleared, with the highlight, by closing the menu", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    await openMenu(shell);
    typeInSearch("bravo");
    pressKey("ArrowDown");
    expect(highlighted()).toEqual([`ws:${BRAVO.id}`]);

    shell.closeWorkspaceSwitcher();
    m.redraw.sync();
    shell.openWorkspaceSwitcher();
    m.redraw.sync();

    expect(searchField().value).toBe("");
    expect(highlighted()).toEqual([]);
    expect(navKeys()).toContain(`ws:${ATTEMPT.id}`);
  });
});

describe("the workspace menu's keyboard", () => {
  it("moves the highlight through the rows on screen while the field keeps focus, stepping over a remote row", async () => {
    await openMenu(makeShell([ALPHA, BRAVO, REMOTE, ORCHARD], ALPHA.id));

    const visited: (string | undefined)[][] = [];
    for (let step = 0; step < 7; step++) {
      const event = pressKey("ArrowDown");
      expect(event.defaultPrevented).toBe(true);
      visited.push(highlighted());
    }

    expect(visited).toEqual([
      [`ws:${ALPHA.id}`],
      [`ws:${BRAVO.id}`],
      ["new"],
      [`shared:${ORCHARD.id}`],
      [`app:${ORCHARD.id}:notes`],
      ["settings"],
      ["settings"],
    ]);
    expect(document.activeElement).toBe(searchField());
    // In a menu taller than the window, the highlighted row is scrolled into view.
    expect(scrolledIntoView[scrolledIntoView.length - 1]).toBe(row("settings"));

    pressKey("ArrowUp");
    expect(highlighted()).toEqual([`app:${ORCHARD.id}:notes`]);
  });

  it("lights the row of the workspace on screen like any other while it is highlighted, and closes on Enter there", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const enterWorkspace = vi.spyOn(shell, "enterWorkspace").mockImplementation(() => undefined);
    await openMenu(shell);
    expect(row(`ws:${ALPHA.id}`).classList.contains("!bg-fill-subtle")).toBe(true);

    pressKey("ArrowDown");
    expect(highlighted()).toEqual([`ws:${ALPHA.id}`]);
    // One fill at a time: the highlight's replaces the fainter one.
    expect(row(`ws:${ALPHA.id}`).classList.contains("!bg-fill-hover")).toBe(true);
    expect(row(`ws:${ALPHA.id}`).classList.contains("!bg-fill-subtle")).toBe(false);
    const enter = pressKey("Enter");

    expect(enter.defaultPrevented).toBe(true);
    expect(enterWorkspace).not.toHaveBeenCalled();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("opens the highlighted row on Enter, not the first match", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const enterWorkspace = vi.spyOn(shell, "enterWorkspace").mockImplementation(() => undefined);
    const openSharedWorkspace = vi.spyOn(shell, "openSharedWorkspace").mockResolvedValue(undefined);
    await openMenu(shell);
    typeInSearch("o");
    // "o" keeps bravo, orchard, and both of the shared apps; New workspace sits between bravo and orchard.
    pressKey("ArrowDown");
    pressKey("ArrowDown");
    pressKey("ArrowDown");
    expect(highlighted()).toEqual([`shared:${ORCHARD.id}`]);

    const enter = pressKey("Enter");

    expect(enter.defaultPrevented).toBe(true);
    expect(openSharedWorkspace).toHaveBeenCalledWith(ORCHARD.id, null);
    expect(enterWorkspace).not.toHaveBeenCalled();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("opens the first match on Enter with no highlight: the account's own workspaces first", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const enterWorkspace = vi.spyOn(shell, "enterWorkspace").mockImplementation(() => undefined);
    await openMenu(shell);
    typeInSearch("o");

    pressKey("Enter");

    expect(enterWorkspace).toHaveBeenCalledWith(BRAVO.id);
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("passes over the row on screen, which is no place to go, and a remote row, which cannot be opened here", async () => {
    const shell = makeShell([ALPHA, REMOTE, BRAVO], ALPHA.id);
    const enterWorkspace = vi.spyOn(shell, "enterWorkspace").mockImplementation(() => undefined);
    await openMenu(shell);

    pressKey("Enter");

    expect(enterWorkspace).toHaveBeenCalledWith(BRAVO.id);
  });

  it("passes over a shared row on screen for the first match too", async () => {
    const shell = makeShell([ORCHARD, PASTED], ORCHARD.id);
    const openSharedWorkspace = vi.spyOn(shell, "openSharedWorkspace").mockResolvedValue(undefined);
    await openMenu(shell);

    pressKey("Enter");

    expect(openSharedWorkspace).toHaveBeenCalledWith(PASTED.id, null);
  });

  it("closes the menu on Enter when the search lists only what is on screen", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const enterWorkspace = vi.spyOn(shell, "enterWorkspace").mockImplementation(() => undefined);
    const openSharedWorkspace = vi.spyOn(shell, "openSharedWorkspace").mockResolvedValue(undefined);
    await openMenu(shell);
    typeInSearch("alpha");

    pressKey("Enter");

    expect(enterWorkspace).not.toHaveBeenCalled();
    expect(openSharedWorkspace).not.toHaveBeenCalled();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("goes on to the shared workspaces, then the shared apps, when no workspace of the account's matches", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const openSharedWorkspace = vi.spyOn(shell, "openSharedWorkspace").mockResolvedValue(undefined);
    await openMenu(shell);
    typeInSearch("sam");
    pressKey("Enter");
    expect(openSharedWorkspace).toHaveBeenLastCalledWith(ORCHARD.id, null);

    shell.openWorkspaceSwitcher();
    m.redraw.sync();
    typeInSearch("ledger");
    pressKey("Enter");
    expect(openSharedWorkspace).toHaveBeenLastCalledWith(LEDGER.id, "ledger");
  });

  it("routes to the creation page when the first match is a workspace still being made", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const enterWorkspace = vi.spyOn(shell, "enterWorkspace").mockImplementation(() => undefined);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    await openMenu(shell);
    typeInSearch("gamma");

    pressKey("Enter");

    expect(routeSet).toHaveBeenCalledWith(`/creating/${ATTEMPT.id}`);
    expect(enterWorkspace).not.toHaveBeenCalled();
  });

  it("does nothing on Enter when nothing matches", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const enterWorkspace = vi.spyOn(shell, "enterWorkspace").mockImplementation(() => undefined);
    const openSharedWorkspace = vi.spyOn(shell, "openSharedWorkspace").mockResolvedValue(undefined);
    await openMenu(shell);
    typeInSearch("zzz");

    pressKey("Enter");

    expect(enterWorkspace).not.toHaveBeenCalled();
    expect(openSharedWorkspace).not.toHaveBeenCalled();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });

  it("drops the highlight when the search changes, so Enter is the first match again", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const enterWorkspace = vi.spyOn(shell, "enterWorkspace").mockImplementation(() => undefined);
    await openMenu(shell);
    pressKey("ArrowDown");
    pressKey("ArrowDown");
    pressKey("ArrowDown");
    expect(highlighted()).toEqual([`ws:${ATTEMPT.id}`]);

    typeInSearch("b");
    expect(highlighted()).toEqual([]);
    pressKey("Enter");

    expect(enterWorkspace).toHaveBeenCalledWith(BRAVO.id);
  });

  it("shares its highlight with the mouse", async () => {
    await openMenu(makeShell(EVERYTHING, ALPHA.id));
    pressKey("ArrowDown");

    row("new").dispatchEvent(new MouseEvent("mousemove", { bubbles: true }));
    m.redraw.sync();
    expect(highlighted()).toEqual(["new"]);

    // Up from New workspace, over the remote row above it.
    pressKey("ArrowUp");
    expect(highlighted()).toEqual([`ws:${ATTEMPT.id}`]);
  });
});

describe("the workspace menu's own-workspace rows", () => {
  it("enters the workspace on a click, closing the menu, and keeps the hooks the desktop's context menu keys on", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const enterWorkspace = vi.spyOn(shell, "enterWorkspace").mockImplementation(() => undefined);
    await openMenu(shell);

    const bravo = row(`ws:${BRAVO.id}`);
    expect(bravo.dataset.agentId).toBe(BRAVO.id);
    expect(bravo.classList.contains("sidebar-item")).toBe(true);
    expect(bravo.getAttribute("role")).toBe("menuitem");
    bravo.click();

    expect(enterWorkspace).toHaveBeenCalledWith(BRAVO.id);
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("marks the workspace on screen, and no other row, with a fainter fill than the highlight's, a check at its right end, and aria-current", async () => {
    await openMenu(makeShell(EVERYTHING, ALPHA.id));

    const marked = [...theMenu().querySelectorAll<HTMLElement>("[aria-current]")];
    expect(marked.map((each) => [each.dataset.navKey, each.getAttribute("aria-current")])).toEqual([
      [`ws:${ALPHA.id}`, "true"],
    ]);
    const alpha = row(`ws:${ALPHA.id}`);
    expect(alpha.classList.contains("!bg-fill-subtle")).toBe(true);
    expect(alpha.classList.contains("!bg-fill-hover")).toBe(false);
    expect(endsWithCheck(alpha)).toBe(true);
    for (const navKey of navKeys().filter((each) => each !== `ws:${ALPHA.id}`)) {
      const other = row(required(navKey));
      expect(other.classList.contains("!bg-fill-subtle"), navKey).toBe(false);
      expect(endsWithCheck(other), navKey).toBe(false);
    }
  });

  it("closes on a click on the row of the workspace on screen, without entering it again", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const enterWorkspace = vi.spyOn(shell, "enterWorkspace").mockImplementation(() => undefined);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    await openMenu(shell);

    clickRow(`ws:${ALPHA.id}`);

    expect(enterWorkspace).not.toHaveBeenCalled();
    expect(routeSet).not.toHaveBeenCalled();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("draws the colour dot, an empty ring for a workspace with no colour, and the liveness icon of one not running", async () => {
    await openMenu(makeShell([ALPHA, BRAVO, ORCHARD], ORCHARD.id));

    const swatchOf = (navKey: string): string | undefined =>
      row(navKey).querySelector<HTMLElement>("[data-workspace-swatch]")?.dataset.workspaceSwatch;
    expect(swatchOf(`ws:${ALPHA.id}`)).toBe("#aabbcc");
    expect(swatchOf(`ws:${BRAVO.id}`)).toBe("");
    expect(row(`ws:${BRAVO.id}`).querySelector(".sidebar-status-icon")?.getAttribute("title")).toBe("Stopped");
    expect(row(`ws:${ALPHA.id}`).querySelector(".sidebar-status-icon")).toBeNull();
  });

  it("badges a workspace still being made, and routes to its creation page", async () => {
    const failed: UiWorkspaceEntry = { ...ATTEMPT, id: "create-attempt-ffff", name: "epsilon", create_attempt_state: "failed" };
    const shell = makeShell([ALPHA, ATTEMPT, failed], ALPHA.id);
    const enterWorkspace = vi.spyOn(shell, "enterWorkspace").mockImplementation(() => undefined);
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    await openMenu(shell);

    expect(rowText(row(`ws:${ATTEMPT.id}`))).toBe("gamma | Creating…");
    expect(rowText(row(`ws:${failed.id}`))).toBe("epsilon | Create failed");
    expect(row(`ws:${failed.id}`).querySelector(".text-important")?.textContent).toBe("Create failed");
    clickRow(`ws:${ATTEMPT.id}`);

    expect(routeSet).toHaveBeenCalledWith(`/creating/${ATTEMPT.id}`);
    expect(enterWorkspace).not.toHaveBeenCalled();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("draws a remote row dimmed and inert", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const enterWorkspace = vi.spyOn(shell, "enterWorkspace").mockImplementation(() => undefined);
    await openMenu(shell);

    const remote = row(`ws:${REMOTE.id}`);
    expect(remote.disabled).toBe(true);
    expect(remote.classList.contains("is-remote")).toBe(true);
    expect(remote.classList.contains("opacity-60")).toBe(true);
    remote.click();

    expect(enterWorkspace).not.toHaveBeenCalled();
    expect(shell.isWorkspaceSwitcherOpen).toBe(true);
  });
});

describe("'Open in new window' on a workspace row", () => {
  function newWindowButtons(): HTMLButtonElement[] {
    return [...theMenu().querySelectorAll<HTMLButtonElement>('button[title="Open in new window"]')];
  }

  it("is offered in the desktop app on each live row of the account's own, and opens that workspace's window", async () => {
    const opened: string[] = [];
    window.mindsNative = {
      openWorkspaceInNewWindow: (agentId: string) => opened.push(agentId),
    } as unknown as NonNullable<Window["mindsNative"]>;
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const enterWorkspace = vi.spyOn(shell, "enterWorkspace").mockImplementation(() => undefined);
    await openMenu(shell);

    // Not the row on screen (this window already shows it), the attempt (no workspace yet), the remote row (not
    // this device's), or a shared row.
    const buttons = newWindowButtons();
    expect(buttons.map((button) => button.getAttribute("aria-label"))).toEqual(["Open bravo in a new window"]);
    expect(buttons[0].parentElement).toBe(row(`ws:${BRAVO.id}`).parentElement);
    // Out of the arrow-key order and the tab order: it is the row's aside, reached by the pointer.
    expect(buttons[0].dataset.navKey).toBeUndefined();
    expect(buttons[0].tabIndex).toBe(-1);
    buttons[0].click();

    expect(opened).toEqual([BRAVO.id]);
    // The click is the arrow's alone: the row under it does not also move this window.
    expect(enterWorkspace).not.toHaveBeenCalled();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("is not offered in a browser, which has no second window to open", async () => {
    await openMenu(makeShell(EVERYTHING, ALPHA.id));

    expect(newWindowButtons()).toEqual([]);
  });
});

describe("the workspace menu's shared rows", () => {
  it("opens a shared workspace on its shell, and a shared app on that app, closing the menu", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const openSharedWorkspace = vi.spyOn(shell, "openSharedWorkspace").mockResolvedValue(undefined);
    await openMenu(shell);

    const orchard = row(`shared:${ORCHARD.id}`);
    expect(orchard.dataset.shareKey).toBe(ORCHARD.id);
    expect(orchard.querySelector<HTMLElement>("[data-workspace-swatch]")?.dataset.workspaceSwatch).toBe("#225533");
    orchard.click();
    expect(openSharedWorkspace).toHaveBeenLastCalledWith(ORCHARD.id, null);
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);

    shell.openWorkspaceSwitcher();
    m.redraw.sync();
    const notes = row(`app:${ORCHARD.id}:notes`);
    expect(notes.dataset.shareKey).toBe(ORCHARD.id);
    expect(notes.dataset.sharedApp).toBe("notes");
    // An app wears its monogram tile where a workspace wears its dot.
    expect(notes.querySelector("[data-monogram]")).not.toBeNull();
    expect(notes.querySelector("[data-workspace-swatch]")).toBeNull();
    notes.click();
    expect(openSharedWorkspace).toHaveBeenLastCalledWith(ORCHARD.id, "notes");
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("marks the shared workspace on screen, and closes on a click on its row without opening it again", async () => {
    const shell = makeShell(EVERYTHING, ORCHARD.id);
    const openSharedWorkspace = vi.spyOn(shell, "openSharedWorkspace").mockResolvedValue(undefined);
    await openMenu(shell);

    const orchard = row(`shared:${ORCHARD.id}`);
    expect(orchard.getAttribute("aria-current")).toBe("true");
    expect(orchard.classList.contains("!bg-fill-subtle")).toBe(true);
    expect(endsWithCheck(orchard)).toBe(true);
    // Its app is another place to go.
    expect(row(`app:${ORCHARD.id}:notes`).getAttribute("aria-current")).toBeNull();
    clickRow(`shared:${ORCHARD.id}`);

    expect(openSharedWorkspace).not.toHaveBeenCalled();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("marks the shared app open on its own, and closes on a click on its row without opening it again", async () => {
    const shell = makeShell(EVERYTHING, ORCHARD.id);
    shell.stores.workspaces.chooseSharedLink(ORCHARD.id, NOTES_LINK, null);
    const openSharedWorkspace = vi.spyOn(shell, "openSharedWorkspace").mockResolvedValue(undefined);
    await openMenu(shell);

    const notes = row(`app:${ORCHARD.id}:notes`);
    expect(notes.getAttribute("aria-current")).toBe("true");
    expect(notes.classList.contains("!bg-fill-subtle")).toBe(true);
    expect(endsWithCheck(notes)).toBe(true);
    // The workspace it belongs to is another place to go.
    expect(row(`shared:${ORCHARD.id}`).getAttribute("aria-current")).toBeNull();
    clickRow(`app:${ORCHARD.id}:notes`);

    expect(openSharedWorkspace).not.toHaveBeenCalled();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("names nobody at the right of a pasted link's row", async () => {
    await openMenu(makeShell([ALPHA, PASTED], ALPHA.id));

    expect(rowText(row(`shared:${PASTED.id}`))).toBe("pasted");
  });

  it("reports an open the desktop refuses through the shell's error dialog, and stays closed", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const shell = makeShell(EVERYTHING, ALPHA.id);
    vi.spyOn(shell, "openSharedWorkspace").mockRejectedValue(
      new SharedWithMeError("Sam Okafor no longer shares this with you.", "refused", 403),
    );
    const show = vi.spyOn(errorDialogs, "show").mockImplementation(() => undefined);
    await openMenu(shell);

    clickRow(`shared:${ORCHARD.id}`);
    await settle();

    expect(show).toHaveBeenCalledWith({
      title: "Couldn't open orchard",
      error: { message: "Sam Okafor no longer shares this with you.", details: "" },
    });
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });
});

describe("the workspace menu's action rows", () => {
  it("'New workspace' closes the menu and routes to the hub's create page", async () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    const routeToHub = vi.spyOn(shell, "routeToHub").mockImplementation(() => undefined);
    await openMenu(shell);

    expect(row("new").id).toBe("workspace-switcher-new");
    clickRow("new");

    expect(routeToHub).toHaveBeenCalledWith("/create");
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("'Workspace & app settings' opens Settings on the account's own workspace on screen", async () => {
    // Shown by its host id, as a window restored from a saved session is.
    const shell = makeShell(EVERYTHING, BRAVO_HOST_ID);
    const openSettings = vi.spyOn(shell, "openSettings").mockImplementation(() => undefined);
    await openMenu(shell);

    expect(row("settings").id).toBe("workspace-switcher-settings");
    expect(rowText(row("settings"))).toBe("Workspace & app settings");
    clickRow("settings");

    expect(openSettings).toHaveBeenCalledWith({ tab: "workspace", settings_workspace: BRAVO.id });
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("'Shared by <owner>' opens Settings on the shared workspace on screen", async () => {
    const shell = makeShell(EVERYTHING, ORCHARD.id);
    const openSettings = vi.spyOn(shell, "openSettings").mockImplementation(() => undefined);
    await openMenu(shell);

    expect(navKeys()).not.toContain("settings");
    expect(row("about").id).toBe("workspace-switcher-shared-by");
    expect(rowText(row("about"))).toBe("Shared by Sam Okafor");
    clickRow("about");

    expect(openSettings).toHaveBeenCalledWith({ tab: "workspace", settings_workspace: ORCHARD.id });
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });

  it("says 'About this link' for a pasted link, which names no owner", async () => {
    await openMenu(makeShell([ALPHA, PASTED], PASTED.id));

    expect(rowText(row("about"))).toBe("About this link");
  });

  it("has no row about a workspace on Home, where none is on screen", async () => {
    await openMenu(makeShell(EVERYTHING, null));

    expect(navKeys()).not.toContain("settings");
    expect(navKeys()).not.toContain("about");
    expect(navKeys()[navKeys().length - 1]).toBe(`app:${LEDGER.id}:ledger`);
  });
});

describe("where the workspace menu hangs", () => {
  it("hangs from the workspace button, joined to its raised copy, and is as wide as a wide button", async () => {
    placeMeasuredElement("workspace-switcher-btn", { left: 210, top: 8, width: 300, height: 28 });
    const menu = await openMenu(makeShell(EVERYTHING, ALPHA.id));

    expect(menu.style.left).toBe("210px");
    expect(menu.style.top).toBe("36px");
    expect(menu.style.width).toBe("300px");
    // Square where it meets the raised button.
    expect(menu.classList.contains("rounded-tl-none")).toBe(true);
    expect(menu.classList.contains("!rounded-tl-lg")).toBe(false);
    const raised = required(document.querySelector<HTMLElement>("#workspace-switcher-btn-raised") ?? undefined);
    expect(raised.getAttribute("aria-selected")).toBe("true");
    expect(raised.style.left).toBe("210px");
  });

  it("hangs from the left edge of the page crumb, with no raised tab, where the bar shows no workspace button", async () => {
    placeMeasuredElement("page-crumb", { left: 150, top: 8, width: 120, height: 28 });
    const menu = await openMenu(makeShell(EVERYTHING, null));

    expect(menu.style.left).toBe("150px");
    expect(menu.style.top).toBe("36px");
    expect(menu.style.width).toBe("260px");
    // With no button to join, every corner is round.
    expect(menu.classList.contains("!rounded-tl-lg")).toBe(true);
    expect(document.querySelector("#workspace-switcher-btn-raised")).toBeNull();
  });
});

describe("the workspace menu's raised strip", () => {
  it("raises the bar's strip with the workspace button selected, which dismisses it or switches flyouts through the shell", () => {
    const shell = makeShell(EVERYTHING, ALPHA.id);
    shell.openWorkspaceSwitcher();
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const switchToAccountSwitcher = vi.spyOn(shell, "switchToAccountSwitcher").mockImplementation(() => undefined);
    const openHelp = vi.spyOn(shell, "openHelp").mockImplementation(() => undefined);
    const menu = WorkspaceSwitcherMenu();
    const nodes = collectVnodes(menu.view.call(menu, { attrs: { shell } } as m.Vnode<{ shell: ShellState }>));

    expect(byId(nodes, "workspace-switcher-menu")).toBeDefined();
    const strip = required(nodes.find((node) => node.tag === RaisedTitlebarIcons));
    const attrs = attrsOf(strip) as unknown as RaisedTitlebarIconsAttrs;
    expect(attrs.selected).toBe("workspace");
    attrs.onSelect("account");
    attrs.onSelect("help");
    expect(switchToAccountSwitcher).toHaveBeenCalledTimes(1);
    expect(openHelp).toHaveBeenCalledTimes(1);

    attrs.onDismiss();
    expect(shell.isWorkspaceSwitcherOpen).toBe(false);
  });
});
