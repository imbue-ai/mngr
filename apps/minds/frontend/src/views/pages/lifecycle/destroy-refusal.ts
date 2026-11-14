// The buttons under a refused Remove workspace, shared by the Workspace
// settings page and the destroy page: the remedy its sentence names, or a
// retry when pressing it again can work.

import type { ErrorAction } from "../../../models/errors";
import type { DestroyRefusal } from "../../../models/workspaceOptions";
import { remedyActions } from "../../components/errorRemedies";

export function destroyRefusalActions(
  refusal: DestroyRefusal,
  surface: { retry: ErrorAction; backToWorkspaces: () => void },
): ErrorAction[] {
  switch (refusal.remedy) {
    case "retry":
      return [surface.retry];
    case "RESTART_APP":
    case "UPDATE_APP":
      return remedyActions(refusal.remedy);
    case "back-to-workspaces":
      return [
        { label: "Back to workspaces", onclick: surface.backToWorkspaces },
      ];
  }
}
