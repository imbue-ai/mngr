import m from "mithril";
import { describe, expect, it } from "vitest";
import type { UiWorkspaceEntry } from "../../channel/messages";
import { createEmptyStores } from "../../models/boot";
import type { ShellState } from "./shell-state";
import { SidebarMenu, statusIcon } from "./SidebarMenu";
import {
  attrsOf,
  collectVnodes,
  sharedWorkspaceEntry,
  workspacesMessage,
} from "../../testing";

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

describe("SidebarMenu", () => {
  it("lists the account's own workspaces and never one shared with it", () => {
    const stores = createEmptyStores();
    const message = workspacesMessage();
    stores.workspaces.applyWorkspacesMessage({
      ...message,
      workspaces: [...message.workspaces, sharedWorkspaceEntry()],
    });
    const shell = {
      isSidebarOpen: true,
      sidebarAnchor: { x: 0, y: 0, width: 0, height: 0 },
      displayedWorkspaceAnyId: null,
      stores,
      closeSidebar: () => undefined,
    } as unknown as ShellState;
    const instance = SidebarMenu();

    const root = instance.view.call(
      instance,
      m(instance, { shell }) as m.Vnode<{ shell: ShellState }>,
    );

    const listed = collectVnodes(root)
      .map((vnode) => attrsOf(vnode).workspace as UiWorkspaceEntry | undefined)
      .filter((workspace) => workspace !== undefined)
      .map((workspace) => workspace.id);
    expect(listed).toEqual(["agent-aa11"]);
  });
});
