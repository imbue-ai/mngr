// @vitest-environment jsdom
//
// Mounted under the real router: what is under test is the route table's own
// redirects, which only m.route resolves. The redirect each one asks for is
// recorded rather than followed, so a case ends at the redirect it is about
// and Settings (with everything that page loads) is never mounted.
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createEmptyStores } from "./models/boot";
import { mountRouter } from "./router";
import { settle } from "./testing";
import { ShellState } from "./views/shell/shell-state";

const AGENT = "agent-ab12";

let root: HTMLElement | null = null;

function unmountRouter(): void {
  if (root === null) return;
  m.mount(root, null);
  root.remove();
  root = null;
}

afterEach(async () => {
  await settle();
  unmountRouter();
  vi.restoreAllMocks();
});

/** Arrive at `url` cold, as a reload or a link from outside the app does, and answer every route the router then
 * asked for. */
async function redirectsFrom(url: string, accountId = "local"): Promise<unknown[][]> {
  unmountRouter();
  const shell = new ShellState(createEmptyStores());
  shell.accountId = accountId;
  const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
  routeSet.mockClear();
  window.history.replaceState(null, "", url);
  root = document.createElement("div");
  document.body.appendChild(root);
  mountRouter(root, shell);
  await settle();
  return [...routeSet.mock.calls];
}

/** The one redirect an arrival at `url` asks for: into Settings, replacing the arrival's history entry. */
async function settingsRedirectFrom(url: string): Promise<Record<string, string>> {
  const calls = await redirectsFrom(url);
  expect(calls, url).toHaveLength(1);
  const [path, query, options] = calls[0];
  expect(path, url).toBe("/settings");
  expect(options, url).toEqual({ replace: true });
  return query as Record<string, string>;
}

describe("the Manage Accounts URL", () => {
  it("lands on Settings' Accounts tab", async () => {
    expect(await settingsRedirectFrom("/accounts")).toEqual({ tab: "accounts" });
  });

  it("keeps the query it arrived with, so the window stays on its account", async () => {
    expect(await settingsRedirectFrom("/accounts?account=user-1")).toEqual({ account: "user-1", tab: "accounts" });
  });

  it("lands on Accounts whatever tab the arrival's own query names", async () => {
    expect(await settingsRedirectFrom("/accounts?tab=studio")).toEqual({ tab: "accounts" });
  });
});

describe("the workspace options URL", () => {
  const ON_WORKSPACE = { workspace: AGENT, tab: "workspace" };

  it("lands the settings tab on its group of the workspace's settings", async () => {
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/options?tab=settings&group=general`)).toEqual({
      ...ON_WORKSPACE,
      group: "general",
    });
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/options?tab=settings&group=backup`)).toEqual({
      ...ON_WORKSPACE,
      group: "backup",
    });
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/options?tab=settings&group=updates`)).toEqual({
      ...ON_WORKSPACE,
      group: "updates",
    });
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/options?tab=settings`)).toEqual({
      ...ON_WORKSPACE,
      group: "general",
    });
  });

  it("lands the settings tab's Account group on General at its Owner section", async () => {
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/options?tab=settings&group=account`)).toEqual({
      ...ON_WORKSPACE,
      group: "general",
      focus: "owner",
    });
  });

  it("lands the permissions tab on Permissions at its section", async () => {
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/options?tab=permissions&section=waiting`)).toEqual({
      ...ON_WORKSPACE,
      group: "permissions",
      perm_section: "waiting",
    });
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/options?tab=permissions`)).toEqual({
      ...ON_WORKSPACE,
      group: "permissions",
    });
  });

  it("lands the share tab on Sharing at its target", async () => {
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/options?tab=share&target=web`)).toEqual({
      ...ON_WORKSPACE,
      group: "sharing",
      share_target: "web",
    });
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/options?tab=share`)).toEqual({
      ...ON_WORKSPACE,
      group: "sharing",
    });
  });

  it("lands on Sharing with no tab, as the options panel opened on Share", async () => {
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/options`)).toEqual({ ...ON_WORKSPACE, group: "sharing" });
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/options?tab=nonsense`)).toEqual({
      ...ON_WORKSPACE,
      group: "sharing",
    });
  });

  it("floats Settings over the workspace by the coordinate the URL carried, a host id included", async () => {
    // A session saved before the agent id was known restores through the host coordinate.
    expect(await settingsRedirectFrom("/workspace/host-99aa/options?tab=settings&group=backup")).toEqual({
      workspace: "host-99aa",
      tab: "workspace",
      group: "backup",
    });
  });
});

describe("the older workspace settings URL", () => {
  const ON_WORKSPACE = { workspace: AGENT, tab: "workspace" };

  it("lands on the workspace's General settings", async () => {
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/settings`)).toEqual({ ...ON_WORKSPACE, group: "general" });
  });

  it("lands on the group it names, the Account group at General's Owner section", async () => {
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/settings?group=updates`)).toEqual({
      ...ON_WORKSPACE,
      group: "updates",
    });
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/settings?group=account`)).toEqual({
      ...ON_WORKSPACE,
      group: "general",
      focus: "owner",
    });
  });

  it("is always the settings tab's redirect, whatever tab its query names", async () => {
    expect(await settingsRedirectFrom(`/workspace/${AGENT}/settings?tab=share&target=web&group=backup`)).toEqual({
      ...ON_WORKSPACE,
      group: "backup",
    });
  });
});

describe("the routes beside the redirects", () => {
  it("lands a bare hub spelling on the window's account's hub, query intact", async () => {
    expect(await redirectsFrom("/create?git_url=https://example.com/r.git", "user-1")).toEqual([
      ["/account/user-1/create?git_url=https://example.com/r.git", undefined, { replace: true }],
    ]);
  });
});
