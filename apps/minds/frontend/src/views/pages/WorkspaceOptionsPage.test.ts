import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import { ShellState } from "../shell/shell-state";
import { panelRoute, rememberInUrl } from "./WorkspaceOptionsPage";

const PANEL_ROUTE = "/workspace/agent-ab12/options?tab=permissions&section=conn:slack:";

/** Register a shell whose live route is `live`, with the options panel either
 * being that route or frozen underneath a modal at `behind`. */
function withRoutes(live: string, behind: string | null): { shell: ShellState; routeSets: string[] } {
  const shell = new ShellState(createEmptyStores());
  shell.panelRouteBehindOverlay = behind;
  registerAppContext({ stores: shell.stores, shell });
  vi.spyOn(m.route, "get").mockReturnValue(live);
  const routeSets: string[] = [];
  vi.spyOn(m.route, "set").mockImplementation(((path: string) => {
    routeSets.push(path);
  }) as typeof m.route.set);
  return { shell, routeSets };
}

afterEach(() => {
  vi.restoreAllMocks();
  clearAppContextForTests();
});

describe("options panel param routing", () => {
  it("reads and writes the live route when the panel is on screen", () => {
    const { shell, routeSets } = withRoutes(PANEL_ROUTE, null);
    expect(panelRoute()).toBe(PANEL_ROUTE);

    rememberInUrl("section", "conn:notion:");

    expect(routeSets).toHaveLength(1);
    expect(routeSets[0]).toContain("/workspace/agent-ab12/options");
    expect(routeSets[0]).toContain("section=conn%3Anotion%3A");
    expect(shell.panelRouteBehindOverlay).toBeNull();
  });

  it("writes to the panel's own route, not the modal's, while a popup covers it", () => {
    // A connector sign-in resolves long after it started, and a request
    // arriving meanwhile auto-opens the popup -- so a pane change genuinely
    // lands while the live route is the modal's. Writing it there would move
    // the modal and leave the panel to come back on its stale section.
    const { shell, routeSets } = withRoutes("/inbox?workspace=agent-ab12&selected=evt-1", PANEL_ROUTE);
    expect(panelRoute()).toBe(PANEL_ROUTE);

    rememberInUrl("section", "conn:notion:");

    expect(routeSets).toEqual([]);
    expect(shell.panelRouteBehindOverlay).toContain("/workspace/agent-ab12/options");
    expect(shell.panelRouteBehindOverlay).toContain("section=conn%3Anotion%3A");
    expect(shell.panelRouteBehindOverlay).not.toContain("/inbox");
  });

  it("does nothing when the value is already what the route carries", () => {
    const { routeSets } = withRoutes(PANEL_ROUTE, null);
    rememberInUrl("tab", "permissions");
    expect(routeSets).toEqual([]);
  });
});
