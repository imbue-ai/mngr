// The buttons for a failure whose fix the app can run itself. A route names
// the remedy beside its sentence (see `remedyFromBody`), or a surface picks
// one for a failure it words itself; either way it is drawn here, so every
// surface offers the same button for the same remedy.

import type { ErrorRemedy } from "../../generated/ui";
import type { ErrorAction } from "../../models/errors";
import { restartAppActions, updateAppActions } from "./ErrorDisplay";

export interface RemedyHandlers {
  /** The actions that start a chat asking the workspace's agent to fix it
   * (askAgentActions, worded for the surface). A surface with no workspace to
   * ask leaves this out, and ASK_AGENT then offers no button. */
  askAgent?: () => ErrorAction[];
}

export function remedyActions(
  remedy: ErrorRemedy | null | undefined,
  handlers: RemedyHandlers = {},
): ErrorAction[] {
  switch (remedy) {
    case "RESTART_APP":
      return restartAppActions();
    case "UPDATE_APP":
      return updateAppActions();
    case "ASK_AGENT":
      return handlers.askAgent?.() ?? [];
    case null:
    case undefined:
      return [];
  }
}
