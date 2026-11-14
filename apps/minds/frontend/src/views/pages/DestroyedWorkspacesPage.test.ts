// @vitest-environment jsdom
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import {
  buttonLabelled,
  jsonResponse,
  mountInBody,
  primaryButtonLabels,
  settle,
} from "../../testing";
import { ShellState } from "../shell/shell-state";
import { DestroyedWorkspacesPage } from "./DestroyedWorkspacesPage";

const DESTROYED_ROW = {
  agent_id: "agent-gone",
  display_name: "Old research box",
  is_orphan: false,
  account_label: "me@example.com",
  destroyed_at_display: "2 days ago",
  days_left_display: "28 days left",
  has_backup: true,
  can_download: true,
  is_locked: false,
  can_delete: true,
  delete_hint: "",
};

beforeEach(() => {
  const stores = createEmptyStores();
  registerAppContext({ stores, shell: new ShellState(stores) });
});

afterEach(() => {
  clearAppContextForTests();
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("DestroyedWorkspacesPage", () => {
  it("says the list could not be read, offers to try again and to report it, and shows the rows a retry reads", async () => {
    const responses = [
      jsonResponse({ error: "boom" }, 500),
      jsonResponse({ retention_days: 30, rows: [DESTROYED_ROW] }),
    ];
    vi.stubGlobal("fetch", () => Promise.resolve(responses.shift()));
    const root = mountInBody(() => m(DestroyedWorkspacesPage));
    await settle();
    m.redraw.sync();
    const notice = root.querySelector<HTMLElement>(
      "#destroyed-workspaces-load-error",
    )!;

    expect(notice.textContent).toContain(
      "Could not load recently destroyed workspaces.",
    );
    expect(root.textContent).not.toContain("No recently destroyed workspaces.");
    expect(buttonLabelled(notice, "Report a bug")).toBeDefined();

    buttonLabelled(notice, "Try again").click();
    await settle();
    m.redraw.sync();

    expect(root.querySelector("#destroyed-workspaces-load-error")).toBeNull();
    expect(root.textContent).toContain("Old research box");
  });

  it("leads a failed delete with Try again, which sends the same delete again", async () => {
    const posts: string[] = [];
    const postResponses = [
      jsonResponse({ error: "Could not delete the backup" }, 502),
      jsonResponse({ is_deleted: true }),
    ];
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posts.push(url);
        return Promise.resolve(postResponses.shift());
      }
      return Promise.resolve(
        jsonResponse({ retention_days: 30, rows: [DESTROYED_ROW] }),
      );
    });
    const root = mountInBody(() => m(DestroyedWorkspacesPage));
    await settle();
    m.redraw.sync();
    buttonLabelled(root, "Remove").click();
    m.redraw.sync();
    buttonLabelled(root, "Delete forever").click();
    await settle();
    m.redraw.sync();
    const notice = root.querySelector<HTMLElement>("#delete-backup-error")!;

    expect(notice.textContent).toContain("Couldn't delete this backup.");
    expect(primaryButtonLabels(root)).toEqual(["Try again"]);

    buttonLabelled(notice, "Try again").click();
    await settle();
    m.redraw.sync();

    const deleteUrl = "/ui/api/destroyed-workspaces/agent-gone/delete-backup";
    expect(posts).toEqual([deleteUrl, deleteUrl]);
    expect(root.querySelector("#delete-backup-error")).toBeNull();
  });
});
