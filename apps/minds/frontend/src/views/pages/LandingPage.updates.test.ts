// What the machines list says about each workspace's update, and the one
// click each state offers. The decisions are models/updates.ts's; these prove
// the rows draw them and that each shortcut lands where it should.

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
  settle,
  updatesShell,
  workspacesMessage,
} from "../../testing";
import { LandingPage } from "./LandingPage";

const ALPHA = "agent-aa11";
const BETA = "agent-cc33";

interface Harness {
  draw: () => m.Children;
  requests: string[];
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
  const { fetchImpl, requests } = recordingFetch();
  vi.stubGlobal("fetch", fetchImpl);
  const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => undefined);
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  // A closure component, called bare as a router mount would with no attrs.
  const page = LandingPage as unknown as () => m.Component;
  return { draw: () => renderRoot(page, {}), requests, routeSet };
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

  it("shows an armed row as scheduled, and a skipped one as skipped", () => {
    const { draw } = harness(
      {
        [ALPHA]: { ...OUT_OF_DATE_UPDATE, is_scheduled: true },
        [BETA]: { ...OUT_OF_DATE_UPDATE, is_update_skipped: true },
      },
      true,
    );

    expect(
      updateBadges(draw()).map((badge) => allText(badge.children)),
    ).toEqual(["Update scheduled", "Skipped v0.4.1"]);
  });
});

describe("a row that needs a sign-in", () => {
  it("says so and signs in from the row, entering the workspace before asking it", async () => {
    const { draw, requests, routeSet } = harness(
      { [ALPHA]: { ...OUT_OF_DATE_UPDATE, needs_sign_in: true } },
      true,
    );
    const root = draw();

    expect(allText(updateBadges(root))).toContain(
      "Needs sign-in to auto-update",
    );
    press(pressable(root, "landing-update-sign-in"));
    await settle();

    expect(routeSet).toHaveBeenCalledWith(`/workspace/${ALPHA}`, {});
    expect(requests).toEqual([`/ui/api/updates/${ALPHA}/sign-in`]);
  });
});

describe("a row's way into its run's chat", () => {
  it("sits beside a failed run's badge, for a run the user never saw a window for", () => {
    const { draw, routeSet } = harness(
      {
        [ALPHA]: {
          ...OUT_OF_DATE_UPDATE,
          verdict: "STUCK",
          chat_agent_id: "agent-lead-9",
          started_by: "SCHEDULED",
        },
      },
      true,
    );

    press(pressable(draw(), "landing-update-open-chat"));

    expect(routeSet).toHaveBeenCalledWith(`/workspace/${ALPHA}`, {
      chat: "agent-lead-9",
    });
  });

  it("sits beside the Updated to X note", () => {
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

    expect(allText(root)).toContain("Updated to v0.4.1.");
    press(pressable(root, "landing-update-open-chat"));
    expect(routeSet).toHaveBeenCalledWith(`/workspace/${ALPHA}`, {
      chat: "agent-lead-10",
    });
  });

  it("leaves the Updated to X note's chat to a newer run's own badge", () => {
    const { draw } = harness(
      {
        [ALPHA]: {
          ...OUT_OF_DATE_UPDATE,
          verdict: "STUCK",
          success_note_version: "minds-v0.4.1",
          chat_agent_id: "agent-lead-11",
        },
      },
      false,
    );
    const root = draw();

    expect(allText(root)).toContain("Updated to v0.4.1.");
    const openChats = collectVnodes(root).filter((vnode) =>
      String(attrsOf(vnode).extra ?? "").includes("landing-update-open-chat"),
    );
    expect(openChats).toHaveLength(1);
  });

  it("is not offered for an update that never went out", () => {
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
    expect(getAppContext().shell.openUpdateModalAgentId()).toBeNull();
  });

  it.each([
    ["a run still starting, with no chat yet", { ...OUT_OF_DATE_UPDATE, activity: "STARTING" }],
    ["a scheduled update", { ...OUT_OF_DATE_UPDATE, is_scheduled: true, chat_agent_id: "agent-lead-old" }],
  ] as const)("opens the update window for %s", (_case, update) => {
    const { draw, routeSet } = harness({ [ALPHA]: update as UiWorkspaceUpdate }, false);

    press(updateBadges(draw())[0]);

    expect(getAppContext().shell.openUpdateModalAgentId()).toBe(ALPHA);
    expect(routeSet).not.toHaveBeenCalled();
  });
});
