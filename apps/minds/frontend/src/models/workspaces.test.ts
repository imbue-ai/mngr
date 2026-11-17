import { describe, expect, it } from "vitest";
import type { UiWorkspaceEntry } from "../channel/messages";
import {
  SHARED_WORKSPACE_DOMAIN,
  sharedWorkspaceEntry,
  workspacesMessage,
} from "../testing";
import { WorkspacesStore, isWorkspaceOrigin } from "./workspaces";

const DOMAIN = SHARED_WORKSPACE_DOMAIN;

function sharedEntry(shareKey: string): UiWorkspaceEntry {
  return sharedWorkspaceEntry({ share_key: shareKey });
}

describe("WorkspacesStore and shared rows", () => {
  it("loads a shared row's frame from its share URL, the shell's link unless an open chose a target", () => {
    const store = new WorkspacesStore();
    const key = "agent-" + "9".repeat(32);
    const message = workspacesMessage();
    store.applyWorkspacesMessage({
      ...message,
      workspaces: [...message.workspaces, sharedEntry(key)],
    });

    expect(store.workspaceFrameUrl(key)).toBe(`https://shell-1.${DOMAIN}/`);
    store.chooseSharedLink(key, `https://notes-1.${DOMAIN}/`, null);
    expect(store.workspaceFrameUrl(key)).toBe(`https://notes-1.${DOMAIN}/`);
    // An owned row keeps the local forward bridge.
    expect(store.workspaceFrameUrl("agent-aa11")).toBe(
      "/forward-bridge?next=%2Fgoto%2Fagent-aa11%2F",
    );
    expect(store.sharedFrameLink("agent-aa11")).toBeNull();
  });

  it("loads an open's redeem URL in place of the link until it is forgotten, while the link stays what the frame is on", () => {
    const store = new WorkspacesStore();
    const key = "agent-" + "9".repeat(32);
    const message = workspacesMessage();
    store.applyWorkspacesMessage({
      ...message,
      workspaces: [...message.workspaces, sharedEntry(key)],
    });
    const redeem = `https://accounts.example/accounts/session?code=c0de&next=https%3A%2F%2Fnotes-1.${DOMAIN}%2F`;

    store.chooseSharedLink(key, `https://notes-1.${DOMAIN}/`, redeem);
    expect(store.workspaceFrameUrl(key)).toBe(redeem);
    expect(store.sharedFrameLink(key)).toBe(`https://notes-1.${DOMAIN}/`);

    // A reload, or a frame coming back to the share, never replays the spent code.
    store.forgetSharedLoadUrl(key);
    expect(store.workspaceFrameUrl(key)).toBe(`https://notes-1.${DOMAIN}/`);

    // The next open's choice replaces the last one's, with or without a redeem.
    store.chooseSharedLink(key, `https://shell-1.${DOMAIN}/`, redeem);
    store.chooseSharedLink(key, `https://shell-1.${DOMAIN}/`, null);
    expect(store.workspaceFrameUrl(key)).toBe(`https://shell-1.${DOMAIN}/`);
  });

  it("resolves a pasted link's row through the shared route alias", () => {
    const store = new WorkspacesStore();
    const message = workspacesMessage();
    store.applyWorkspacesMessage({
      ...message,
      workspaces: [...message.workspaces, sharedEntry("0f3a9c")],
    });

    expect(store.entryByAnyId("shared-0f3a9c")?.id).toBe("0f3a9c");
    expect(store.sharedEntryByShareKey("0f3a9c")?.name).toBe("Beta");
    expect(store.workspaceFrameUrl("shared-0f3a9c")).toBe(
      `https://shell-1.${DOMAIN}/`,
    );
    expect(store.entryByAnyId("shared-missing")).toBeNull();
  });

  it("loads nothing for a shared row with no address, never the local forward of its id", () => {
    const store = new WorkspacesStore();
    const key = "agent-" + "9".repeat(32);
    const message = workspacesMessage();
    const entry = sharedEntry(key);
    store.applyWorkspacesMessage({
      ...message,
      workspaces: [
        ...message.workspaces,
        {
          ...entry,
          share: {
            ...entry.share!,
            targets: entry.share!.targets.map((target) => ({ ...target, link: null })),
          },
        },
      ],
    });

    expect(store.workspaceFrameUrl(key)).toBe("about:blank");
    // A pasted link's alias whose row is gone from the list.
    expect(store.workspaceFrameUrl("shared-missing")).toBe("about:blank");
  });

  it("keeps loading nothing for a shared row after its row left the list, never the owner's local forward", () => {
    const store = new WorkspacesStore();
    const key = "agent-" + "9".repeat(32);
    const message = workspacesMessage();
    store.applyWorkspacesMessage({ ...message, workspaces: [...message.workspaces, sharedEntry(key)] });
    expect(store.workspaceFrameUrl(key)).toMatch(/^https:/);

    store.applyWorkspacesMessage(message);

    expect(store.entryByAnyId(key)).toBeNull();
    expect(store.isSharedRow(key)).toBe(true);
    expect(store.workspaceFrameUrl(key)).toBe("about:blank");
  });

  it("loads the local forward again once the window's account lists the row as its own", () => {
    // One device, two accounts: the window showed the recipient's rows, then
    // switched to the owner's, whose row for the same workspace id is owned.
    const store = new WorkspacesStore();
    const key = "agent-" + "9".repeat(32);
    const message = workspacesMessage();
    store.applyWorkspacesMessage({ ...message, workspaces: [...message.workspaces, sharedEntry(key)] });
    expect(store.isSharedRow(key)).toBe(true);

    store.applyWorkspacesMessage({
      ...message,
      workspaces: [...message.workspaces, { ...message.workspaces[0], id: key, host_id: "host-" + "9".repeat(32) }],
    });

    expect(store.isSharedRow(key)).toBe(false);
    expect(store.workspaceFrameUrl(key)).toBe(`/forward-bridge?next=${encodeURIComponent(`/goto/${key}/`)}`);
  });
});

describe("isWorkspaceOrigin", () => {
  it("accepts the canonical agent-keyed content origins", () => {
    expect(isWorkspaceOrigin("http://agent-0f3c2b71a4de49b1a2c3d4e5f6a7b8c9.localhost:8421")).toBe(true);
    expect(
      isWorkspaceOrigin("http://system_interface-x1y2.agent-0f3c2b71a4de49b1a2c3d4e5f6a7b8c9.localhost:8421"),
    ).toBe(true);
  });

  it("still accepts a legacy host-keyed origin awaiting the redirect heal", () => {
    expect(isWorkspaceOrigin("http://host-0f3c2b71a4de49b1a2c3d4e5f6a7b8c9.localhost:8421")).toBe(true);
  });

  it("rejects origins outside the workspace families", () => {
    expect(isWorkspaceOrigin("http://localhost:8420")).toBe(false);
    expect(isWorkspaceOrigin("https://evil-agent-abc.example.com")).toBe(false);
    expect(isWorkspaceOrigin("https://agent-abc.localhost.example.com")).toBe(false);
    expect(isWorkspaceOrigin("null")).toBe(false);
  });
});
