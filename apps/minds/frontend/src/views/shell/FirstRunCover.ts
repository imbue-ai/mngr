// The cover over a new workspace while it loads: the creation page's welcome, carried on over the
// workspace's frame under the title bar. The workspace loads beneath it, and Continue appears once the workspace
// reports its provider ask on screen, so the press lands on that ask with nothing to wait for
// (models/firstRunCover.ts).

import m from "mithril";
import { firstRunCover } from "../../models/firstRunCover";
import { firstRunWelcome, readyStatus, settingUpStatus } from "../pages/create/FirstRunWelcome";

export const OPENING_WORKSPACE_LINE = "Just a sec, we're opening your workspace";

export interface FirstRunCoverAttrs {
  /** The route on screen: the cover belongs to the creation page's route and the workspace's, and drops on any other. */
  routePath: string;
}

function isCoveredRoute(routePath: string): boolean {
  return routePath.startsWith("/workspace/") || routePath.startsWith("/creating/");
}

export function FirstRunCover(): m.Component<FirstRunCoverAttrs> {
  return {
    view(vnode) {
      const state = firstRunCover.state;
      if (state === null) return null;
      if (!isCoveredRoute(vnode.attrs.routePath)) {
        // The user went somewhere else (the home page, settings): the workspace is theirs to come back to uncovered.
        queueMicrotask(() => firstRunCover.lower());
        return null;
      }
      return m(
        "div",
        { id: "first-run-cover", class: "fixed inset-x-0 bottom-0 top-(--titlebar-height) z-[90] overflow-y-auto" },
        firstRunWelcome({
          id: "first-run-cover-welcome",
          attemptId: state.createAttemptId,
          heading: state.heading,
          status: state.isReady
            ? readyStatus(() => firstRunCover.lower())
            : settingUpStatus(OPENING_WORKSPACE_LINE, 100),
        }),
      );
    },
  };
}
