import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import { ShellState } from "../shell/shell-state";
import { applyRequestedTarget, panelRoute, rememberInUrl } from "./WorkspaceOptionsPage";

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

    rememberInUrl({ section: "conn:notion:" });

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

    rememberInUrl({ section: "conn:notion:" });

    expect(routeSets).toEqual([]);
    expect(shell.panelRouteBehindOverlay).toContain("/workspace/agent-ab12/options");
    expect(shell.panelRouteBehindOverlay).toContain("section=conn%3Anotion%3A");
    expect(shell.panelRouteBehindOverlay).not.toContain("/inbox");
  });

  it("writes every change in one go, so one cannot undo another", () => {
    // Choosing a section closes the open request, which is two params. Written
    // one at a time they would each be computed from the route as it is NOW --
    // and m.route.set does not land synchronously, so the second would be
    // built on the route the first replaced and put the request back. That is
    // what left the pane showing requests while the nav said otherwise.
    const { routeSets } = withRoutes(`${PANEL_ROUTE}&request=evt-1`, null);

    rememberInUrl({ section: "conn:notion:", request: null });

    expect(routeSets).toHaveLength(1);
    expect(routeSets[0]).toContain("section=conn%3Anotion%3A");
    expect(routeSets[0]).not.toContain("request=");
  });

  it("does nothing when the value is already what the route carries", () => {
    const { routeSets } = withRoutes(PANEL_ROUTE, null);
    rememberInUrl({ tab: "permissions" });
    expect(routeSets).toEqual([]);
  });
});

const SHARE_ROUTE = "/workspace/agent-ab12/options?tab=share&target=web";

function makeShare(): { share: { selectTarget(target: string): void }; selected: string[] } {
  const selected: string[] = [];
  return { share: { selectTarget: (target: string) => selected.push(target) }, selected };
}

describe("applyRequestedTarget", () => {
  it("holds the param until the share model exists, then selects it", () => {
    // The share model is only created once the options load completes, so the
    // deep link's target must survive the renders before that.
    withRoutes(SHARE_ROUTE, null);
    expect(applyRequestedTarget(null, null)).toBeNull();
    const { share, selected } = makeShare();
    expect(applyRequestedTarget(share, null)).toBe("web");
    expect(selected).toEqual(["web"]);
  });

  it("re-selects only when the param's value changes", () => {
    // A deep link can land while the panel is already open and loaded; the
    // user's own target navigation never touches the URL, so an unchanged
    // param must not fight it by re-selecting every render.
    withRoutes(SHARE_ROUTE, null);
    const { share, selected } = makeShare();
    let applied = applyRequestedTarget(share, null);
    applied = applyRequestedTarget(share, applied);
    expect(selected).toEqual(["web"]);
    vi.spyOn(m.route, "get").mockReturnValue("/workspace/agent-ab12/options?tab=share&target=docs");
    applied = applyRequestedTarget(share, applied);
    expect(applied).toBe("docs");
    expect(selected).toEqual(["web", "docs"]);
  });

  it("consumes a dropped param without selecting anything", () => {
    // The titlebar's openOptionsTab routes with only ?tab: the pane keeps its
    // selection, and a later deep link back to the same target still lands.
    withRoutes("/workspace/agent-ab12/options?tab=share", null);
    const { share, selected } = makeShare();
    expect(applyRequestedTarget(share, "web")).toBeNull();
    expect(selected).toEqual([]);
  });
});
