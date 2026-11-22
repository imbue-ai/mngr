// Settings' About page for a workspace or app shared with the account: whose
// it is, what of it the account reaches, and taking it off the account's lists.

import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../../app-context";
import type { UiSharedWorkspace } from "../../../generated/ui";
import { createEmptyStores } from "../../../models/boot";
import type { SharedEntry } from "../../../models/sharedWithMe";
import type { AnyVnode } from "../../../testing";
import {
  SHARED_WORKSPACE_DOMAIN,
  allText,
  attrsOf,
  byId,
  collectVnodes,
  jsonResponse,
  namedSharedOwner,
  renderDeep,
  required,
  settle,
  sharedAppTarget,
  sharedWorkspaceEntry,
} from "../../../testing";
import { ShellState } from "../../shell/shell-state";
import { Modal } from "../../components/Modal";
import { SharedWorkspaceAbout, sharedAccessLabel, sharedRemovalCopy } from "./SharedWorkspaceAbout";

const SAM = namedSharedOwner();

const NOTES = sharedAppTarget("notes", "Notes");
const BOARD = sharedAppTarget("board", "Board");

function shared(shareOverrides: Partial<UiSharedWorkspace> = {}, name = "orchard"): SharedEntry {
  return sharedWorkspaceEntry({ status: "accepted", owner: SAM, ...shareOverrides }, { name }) as SharedEntry;
}

/** A whole workspace Sam shared (the fixture's shell target, with one app beside it). */
function workspaceShare(): SharedEntry {
  return shared();
}

function appShare(targets: UiSharedWorkspace["targets"]): SharedEntry {
  return shared({ targets });
}

/** A link the account pasted to one app: it names nobody, and the app is known only by the link's label. */
function pastedLink(): SharedEntry {
  return shared({
    is_manual: true,
    status: "accepted",
    owner: { user_id: "", email: null, display_name: null, profile_picture_url: null },
    targets: [{ app: "notes", display_name: null, link: `https://notes-ab12cd34.${SHARED_WORKSPACE_DOMAIN}/` }],
  });
}

describe("sharedRemovalCopy", () => {
  it("calls leaving a shared workspace what it is: hidden here, the owner's grant untouched, undoable", () => {
    const copy = sharedRemovalCopy(workspaceShare());

    expect(copy.action).toBe("Leave workspace");
    expect(copy.question).toBe("Leave orchard?");
    expect(copy.sentence).toBe(
      "Remove orchard from your workspaces. This hides it here; it does not change the access Sam Okafor gave you. " +
        "You can undo it from Home › Invitations.",
    );
    expect(copy.confirm).toBe("Leave");
    expect(copy.busy).toBe("Leaving…");
  });

  it("names the one app of a one-app share", () => {
    const copy = sharedRemovalCopy(appShare([NOTES]));

    expect(copy.action).toBe("Remove app");
    expect(copy.question).toBe("Remove Notes?");
    expect(copy.sentence).toBe(
      "Remove Notes from your apps. This hides it here; it does not change the access Sam Okafor gave you. " +
        "You can undo it from Home › Invitations.",
    );
    expect(copy.confirm).toBe("Remove");
    expect(copy.busy).toBe("Removing…");
  });

  it("names an app share that lists no app yet by its row's name, else by its id", () => {
    expect(sharedRemovalCopy(appShare([])).question).toBe("Remove orchard?");

    const unnamed = shared({ targets: [] }, "");
    expect(sharedRemovalCopy(unnamed).question).toBe(`Remove ${unnamed.id}?`);
  });

  it("says a several-app share goes as a whole, and names every app that goes with it", () => {
    const copy = sharedRemovalCopy(appShare([NOTES, BOARD]));

    expect(copy.sentence).toBe(
      "Remove everything Sam Okafor shared with you from this workspace (Notes, Board). This hides them here; " +
        "it does not change the access Sam Okafor gave you. You can undo it from Home › Invitations.",
    );
    expect(copy.action).toBe("Remove apps");
    expect(copy.question).toBe("Remove everything Sam Okafor shared from this workspace?");
    expect(copy.confirm).toBe("Remove");
  });

  it("promises nothing about access or undoing for a pasted link, which is only forgotten", () => {
    const copy = sharedRemovalCopy(pastedLink());

    expect(copy.action).toBe("Remove link");
    expect(copy.question).toBe("Remove this link?");
    expect(copy.sentence).toBe("Remove this link from your list.");
    expect(copy.confirm).toBe("Remove");
  });

  it("names the owner by email when they have no display name", () => {
    const entry = appShare([NOTES]);
    entry.share.owner = { ...SAM, display_name: null };

    expect(sharedRemovalCopy(entry).sentence).toContain("the access sam@acme.example gave you");
  });
});

describe("sharedAccessLabel", () => {
  it("says the whole workspace for a workspace share, whatever apps it also names", () => {
    expect(sharedAccessLabel(workspaceShare())).toBe("The whole workspace");
  });

  it("names the apps of an app share", () => {
    expect(sharedAccessLabel(appShare([NOTES]))).toBe("Only Notes");
    expect(sharedAccessLabel(appShare([NOTES, BOARD]))).toBe("Only Notes, Board");
  });

  it("names a pasted link's app by its link, the only name it has", () => {
    expect(sharedAccessLabel(pastedLink())).toBe("Only notes");
  });

  it("says so when a share names nothing yet", () => {
    expect(sharedAccessLabel(appShare([]))).toBe("Nothing yet");
  });
});

interface About {
  draw: () => AnyVnode;
  /** Ask the confirmation to close the way Escape or a click on the dimmed window does. */
  dismissDialog: () => void;
  shell: ShellState;
  routeSets: string[];
  requests: { url: string; method: string | undefined }[];
}

/** The page for `entry`, in a window on user-1, with the remove route answering `respond`. */
function openAbout(entry: SharedEntry, respond: () => Promise<Response> = () => Promise.resolve(jsonResponse({}))): About {
  const shell = new ShellState(createEmptyStores());
  shell.accountId = "user-1";
  registerAppContext({ stores: shell.stores, shell });
  const requests: About["requests"] = [];
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    requests.push({ url, method: init?.method });
    return respond();
  });
  const routeSets: string[] = [];
  vi.spyOn(m.route, "set").mockImplementation(((path: string) => {
    routeSets.push(path);
  }) as typeof m.route.set);
  const instance = SharedWorkspaceAbout() as unknown as m.Component;
  const view = (): AnyVnode =>
    (instance.view as unknown as (vnode: m.Vnode) => m.Children).call(
      instance,
      m(instance, { entry } as unknown as m.Attributes) as m.Vnode,
    ) as AnyVnode;
  const draw = (): AnyVnode => renderDeep(view()) as AnyVnode;
  // Escape and a click on the dimmed window both reach the confirmation through the Modal's own onClose.
  const dismissDialog = (): void =>
    (attrsOf(required(collectVnodes(view()).find((vnode) => vnode.tag === Modal))).onClose as () => void)();
  return { draw, dismissDialog, shell, routeSets, requests };
}

function press(root: AnyVnode, id: string): void {
  const button = required(byId(root, id));
  if (attrsOf(button).disabled === true) throw new Error(`#${id} is disabled`);
  (attrsOf(button).onclick as () => void)();
}

/** The confirmation's Cancel button. */
function cancelButton(root: AnyVnode): AnyVnode {
  return required(
    collectVnodes(byId(root, "leave-workspace-dialog")).find(
      (vnode) => vnode.tag === "button" && allText(vnode) === "Cancel",
    ),
  );
}

/** The confirmation's text, or null while it is closed. */
function dialogText(root: AnyVnode): string | null {
  const dialog = byId(root, "leave-workspace-dialog");
  return dialog === undefined ? null : allText(dialog);
}

describe("SharedWorkspaceAbout", () => {
  beforeEach(() => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    clearAppContextForTests();
  });

  it("shows the owner by display name with their email under it, and what the account reaches", () => {
    const root = openAbout(workspaceShare()).draw();
    const text = allText(root);

    expect(text).toContain("Owner");
    expect(text.indexOf("Sam Okafor")).toBeGreaterThan(-1);
    expect(text.indexOf("sam@acme.example")).toBeGreaterThan(text.indexOf("Sam Okafor"));
    expect(text).toContain("Your access");
    expect(allText(required(byId(root, "shared-workspace-access")))).toBe("The whole workspace");
    expect(text).toContain("Sam Okafor decides what you can reach here, and can change it.");
  });

  it("does not repeat the email of an owner who is known only by it", () => {
    const entry = workspaceShare();
    entry.share.owner = { ...SAM, display_name: null };

    const root = openAbout(entry).draw();

    const ownerLines = collectVnodes(root).filter(
      (vnode) => vnode.tag === "p" && allText(vnode) === "sam@acme.example",
    );
    expect(ownerLines).toHaveLength(1);
    expect(allText(root)).toContain("sam@acme.example decides what you can reach here, and can change it.");
  });

  it("says a pasted link names nobody", () => {
    const text = allText(openAbout(pastedLink()).draw());

    expect(text).toContain("Unknown");
    expect(text).toContain("This is a link you pasted, which names nobody.");
    expect(text).toContain("The owner decides what you can reach here, and can change it.");
  });

  it("names the page's one action after what it removes, with its sentence above the button", () => {
    const leave = openAbout(workspaceShare()).draw();
    expect(allText(required(byId(leave, "leave-workspace-btn")))).toBe("Leave workspace");
    expect(allText(required(byId(leave, "shared-workspace-removal")))).toBe(
      sharedRemovalCopy(workspaceShare()).sentence,
    );

    expect(allText(required(byId(openAbout(appShare([NOTES])).draw(), "leave-workspace-btn")))).toBe("Remove app");
    expect(allText(required(byId(openAbout(appShare([NOTES, BOARD])).draw(), "leave-workspace-btn")))).toBe(
      "Remove apps",
    );
    expect(allText(required(byId(openAbout(pastedLink()).draw(), "leave-workspace-btn")))).toBe("Remove link");
  });

  it("asks before removing, and removes nothing when the confirmation is cancelled", () => {
    const about = openAbout(workspaceShare());
    expect(dialogText(about.draw())).toBeNull();

    press(about.draw(), "leave-workspace-btn");

    const asked = dialogText(about.draw());
    expect(asked).toContain("Leave orchard?");
    expect(asked).toContain(sharedRemovalCopy(workspaceShare()).sentence);
    expect(allText(required(byId(about.draw(), "leave-workspace-confirm")))).toBe("Leave");

    (attrsOf(cancelButton(about.draw())).onclick as () => void)();

    expect(dialogText(about.draw())).toBeNull();
    expect(about.requests).toEqual([]);
    expect(about.routeSets).toEqual([]);
  });

  it("removes the share through the remove route and sends the window to its account's home", async () => {
    let answer: (response: Response) => void = () => undefined;
    const about = openAbout(workspaceShare(), () => new Promise<Response>((resolve) => (answer = resolve)));
    press(about.draw(), "leave-workspace-btn");

    press(about.draw(), "leave-workspace-confirm");

    const shareKey = workspaceShare().share.share_key;
    expect(about.requests).toEqual([
      { url: `/ui/api/shared-with-me/${shareKey}/remove?account=user-1`, method: "POST" },
    ]);
    // While the removal runs the dialog holds: both buttons refuse, Escape and the dimmed window do not close it,
    // and the confirming button says what is happening.
    const running = about.draw();
    expect(attrsOf(required(byId(running, "leave-workspace-confirm"))).disabled).toBe(true);
    expect(attrsOf(cancelButton(running)).disabled).toBe(true);
    about.dismissDialog();
    expect(dialogText(about.draw())).not.toBeNull();
    expect(allText(required(byId(running, "leave-workspace-confirm")))).toContain("Leaving…");
    expect(about.routeSets).toEqual([]);

    answer(jsonResponse({}));
    await settle();

    expect(about.routeSets).toEqual([about.shell.hubRoute()]);
    expect(dialogText(about.draw())).toBeNull();
  });

  it("stays in the confirmation with the reason when the removal fails", async () => {
    const about = openAbout(workspaceShare(), () =>
      Promise.resolve(jsonResponse({ error: "The share could not be removed right now." }, 502)),
    );
    press(about.draw(), "leave-workspace-btn");

    press(about.draw(), "leave-workspace-confirm");
    await settle();

    const failed = about.draw();
    expect(dialogText(failed)).toContain("The share could not be removed right now.");
    const alerts = collectVnodes(failed).filter((vnode) => attrsOf(vnode).role === "alert");
    expect(alerts.map((vnode) => allText(vnode))).toEqual(["The share could not be removed right now."]);
    expect(about.routeSets).toEqual([]);
    // The way out and the way to try again are both live again.
    expect(attrsOf(required(byId(failed, "leave-workspace-confirm"))).disabled).toBe(false);
    expect(allText(required(byId(failed, "leave-workspace-confirm")))).toBe("Leave");
    expect(attrsOf(cancelButton(failed)).disabled).toBe(false);
    about.dismissDialog();
    expect(dialogText(about.draw())).toBeNull();
  });
});
