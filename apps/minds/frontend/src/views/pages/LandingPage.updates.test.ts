// What the machines list says about each workspace's update, and the one
// click each state offers. The decisions are models/updates.ts's; these prove
// the rows draw them and that each click lands where it should.

import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, getAppContext } from "../../app-context";
import type { UiWorkspaceUpdate } from "../../channel/messages";
import type { AnyVnode } from "../../testing";
import {
  allText,
  attrsOf,
  byId,
  classTokensOf,
  collectVnodes,
  OUT_OF_DATE_UPDATE,
  recordingFetch,
  renderRoot,
  updatesShell,
  workspacesMessage,
} from "../../testing";
import { LandingPage } from "./LandingPage";

const ALPHA = "agent-aa11";
const BETA = "agent-cc33";

interface Harness {
  draw: () => m.Children;
  routeSet: ReturnType<typeof vi.fn>;
}

function harness(
  updates: Record<string, UiWorkspaceUpdate>,
  isAutoUpdateEnabled: boolean,
): Harness {
  const shell = updatesShell(updates, isAutoUpdateEnabled);
  const base = workspacesMessage();
  const alpha = base.workspaces[0];
  shell.stores.workspaces.applyWorkspacesMessage({
    ...base,
    workspaces: [
      alpha,
      { ...alpha, id: BETA, name: "beta", host_id: "host-dd44" },
    ],
  });
  const { fetchImpl } = recordingFetch();
  vi.stubGlobal("fetch", fetchImpl);
  const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  // A closure component, called bare as a router mount would with no attrs.
  const page = LandingPage as unknown as () => m.Component;
  return { draw: () => renderRoot(page, {}), routeSet };
}

function updateBadges(root: m.Children): AnyVnode[] {
  return collectVnodes(root).filter((vnode) =>
    classTokensOf(vnode).includes("landing-update-badge"),
  );
}

function pressable(root: m.Children, extraClass: string): AnyVnode | undefined {
  return collectVnodes(root).find((vnode) =>
    String(attrsOf(vnode).extra ?? "").includes(extraClass),
  );
}

function press(vnode: AnyVnode | undefined): void {
  if (vnode === undefined) throw new Error("nothing to press");
  (attrsOf(vnode).onclick as (event: MouseEvent) => void)({
    stopPropagation: () => undefined,
  } as MouseEvent);
}

/** The badge's press landed on the row's Updates tab, flagged to flash. */
function expectUpdatesTabOpened(routeSet: Harness["routeSet"]): void {
  expect(routeSet).toHaveBeenCalledWith(`/workspace/${ALPHA}/options`, { tab: "settings", group: "updates" });
  expect(getAppContext().shell.takeUpdatesTabArrival(ALPHA)).not.toBeNull();
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  clearAppContextForTests();
});

describe("the machines list under auto-update", () => {
  it("says nothing on behind rows auto-update will arm, and offers no Update all", () => {
    const { draw } = harness(
      { [ALPHA]: OUT_OF_DATE_UPDATE, [BETA]: OUT_OF_DATE_UPDATE },
      true,
    );
    const root = draw();

    expect(updateBadges(root)).toEqual([]);
    expect(byId(root, "landing-bulk-updates")).toBeUndefined();
  });

  it("offers Update all for the same rows without auto-update", () => {
    const { draw } = harness(
      { [ALPHA]: OUT_OF_DATE_UPDATE, [BETA]: OUT_OF_DATE_UPDATE },
      false,
    );
    const root = draw();

    expect(
      updateBadges(root).map((badge) => attrsOf(badge)["data-update-state"]),
    ).toEqual(["out-of-date", "out-of-date"]);
    expect(byId(root, "landing-bulk-updates")).toBeDefined();
  });

  const FOLLOWS_STUDIO = {};
  const MANUAL = { is_auto_update_enabled: false, auto_update_override: { mode: "manual" } };
  const OWN_WINDOW = {
    is_auto_update_enabled: true,
    auto_update_override: { mode: "custom_window", window_start_hour: 1, window_end_hour: 4 },
  };
  it.each([
    ["badges and counts rows set to manual, though Studio auto-updates", true, MANUAL, MANUAL, ["out-of-date", "out-of-date"], true],
    ["badges only the manual row when the other follows Studio's auto-update", true, FOLLOWS_STUDIO, MANUAL, ["out-of-date"], false],
    ["leaves out a row with its own window, though Studio does not auto-update", false, FOLLOWS_STUDIO, OWN_WINDOW, ["out-of-date"], false],
  ] as const)("%s, by each row's own effective setting", (_case, isStudioAuto, alphaSetting, betaSetting, badges, isStripShown) => {
    const row = (setting: object): UiWorkspaceUpdate =>
      ({ ...OUT_OF_DATE_UPDATE, is_workspace_readable: true, is_auto_update_enabled: isStudioAuto, ...setting }) as UiWorkspaceUpdate;
    const { draw } = harness({ [ALPHA]: row(alphaSetting), [BETA]: row(betaSetting) }, isStudioAuto);
    const root = draw();

    expect(updateBadges(root).map((badge) => attrsOf(badge)["data-update-state"])).toEqual(badges);
    expect(byId(root, "landing-bulk-updates") !== undefined).toBe(isStripShown);
  });

  it("shows nothing on an armed row, and a skipped one as skipped", () => {
    const { draw } = harness(
      {
        [ALPHA]: { ...OUT_OF_DATE_UPDATE, is_scheduled: true },
        [BETA]: { ...OUT_OF_DATE_UPDATE, is_update_skipped: true },
      },
      true,
    );

    expect(
      updateBadges(draw()).map((badge) => allText(badge.children)),
    ).toEqual(["Skipped v0.4.1"]);
  });
});

describe("a row's update actions", () => {
  it.each([
    ["needs a sign-in", { ...OUT_OF_DATE_UPDATE, needs_sign_in: true }, true],
    [
      "ended in a failure the user never saw a window for",
      { ...OUT_OF_DATE_UPDATE, verdict: "STUCK", chat_agent_id: "agent-lead-9", started_by: "SCHEDULED" },
      true,
    ],
    [
      "waits on the user's decision",
      { ...OUT_OF_DATE_UPDATE, verdict: "NEEDS_DECISION", is_decision_pending: true, chat_agent_id: "agent-lead-8" },
      false,
    ],
  ] as const)("are the badge alone on a row whose update %s, and it opens the Updates tab", (_case, update, isAuto) => {
    const { draw, routeSet } = harness({ [ALPHA]: update as UiWorkspaceUpdate }, isAuto);
    const root = draw();

    expect(updateBadges(root)).toHaveLength(1);
    expect(pressable(root, "landing-update-open-chat")).toBeUndefined();
    press(updateBadges(root)[0]);
    expectUpdatesTabOpened(routeSet);
  });

  it("include the Updated to X note, with its chat, on a row with no update badge", () => {
    const { draw, routeSet } = harness(
      {
        [ALPHA]: {
          ...OUT_OF_DATE_UPDATE,
          availability: "UP_TO_DATE",
          verdict: "UPDATED",
          success_note_version: "minds-v0.4.1",
          chat_agent_id: "agent-lead-10",
        },
      },
      true,
    );
    const root = draw();

    expect(updateBadges(root)).toHaveLength(0);
    expect(allText(root)).toContain("Updated to v0.4.1.");
    press(pressable(root, "landing-update-open-chat"));
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${ALPHA}`, {
      chat: "agent-lead-10",
    });
  });

  it("leave the Updated to X note out while the row's badge says a newer run failed", () => {
    const { draw } = harness(
      {
        [ALPHA]: {
          ...OUT_OF_DATE_UPDATE,
          success_note_version: "minds-v0.4.1",
          verdict: "STUCK",
          chat_agent_id: "agent-lead-11",
        },
      },
      false,
    );
    const root = draw();

    expect(updateBadges(root)).toHaveLength(1);
    expect(allText(root)).not.toContain("Updated to v0.4.1.");
  });

  it("offer no chat for an update that never went out", () => {
    const { draw } = harness(
      {
        [ALPHA]: {
          ...OUT_OF_DATE_UPDATE,
          dispatch_failure: "Couldn't reach this workspace.",
          chat_agent_id: "agent-old",
        },
      },
      false,
    );
    expect(pressable(draw(), "landing-update-open-chat")).toBeUndefined();
  });
});

describe("a row's update badge while its run is under way", () => {
  it.each([
    ["preparing", "RUNNING"],
    ["waiting on the user", "WAITING"],
    ["applying", "APPLYING"],
  ] as const)("goes straight into the run's chat while it is %s", (_case, activity) => {
    const { draw, routeSet } = harness(
      { [ALPHA]: { ...OUT_OF_DATE_UPDATE, activity, chat_agent_id: "agent-lead-12" } },
      true,
    );

    press(updateBadges(draw())[0]);

    expect(routeSet).toHaveBeenCalledWith(`/workspace/${ALPHA}`, {
      chat: "agent-lead-12",
    });
    expect(getAppContext().shell.takeUpdatesTabArrival(ALPHA)).toBeNull();
  });

  it("opens the Updates tab for a run still starting, with no chat yet", () => {
    const { draw, routeSet } = harness({ [ALPHA]: { ...OUT_OF_DATE_UPDATE, activity: "STARTING" } }, false);

    press(updateBadges(draw())[0]);

    expectUpdatesTabOpened(routeSet);
  });
});
