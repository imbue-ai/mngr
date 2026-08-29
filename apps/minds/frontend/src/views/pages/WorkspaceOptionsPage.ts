// The workspace options overlay (/workspace/<id>/options?tab=&group=&section=
// &target=): Permissions + Share machine + Machine settings over one
// options-data load. This owns the URL-backed tab/group/section state and the
// two models; the docked panel chrome (backdrop, tab strip, card) lives in
// WorkspaceOptionsOverlay, which the Shell floats over the still-mounted
// workspace surface. The titlebar's ws-tab buttons land here; ?tab preselects
// the pane, ?group the settings group, ?section the permissions left-nav entry,
// ?target the share target.
//
// The URL is the single source of truth for tab/group/section: they are
// re-read from the route on every render, so titlebar-driven navigation
// (which changes only the query string, preserving this component instance)
// switches panes.

import m from "mithril";
import { getAppContext } from "../../app-context";
import type { OptionsTab, SettingsGroup, ShareModel } from "../../models/workspaceOptions";
import { WorkspaceOptionsModel, toOptionsTab } from "../../models/workspaceOptions";
import { PermissionsModel } from "../../models/workspacePermissions";
import { isWorkspaceOverlayPath, workspaceSurfaceIdFromPath } from "../shell/classify";
import { WorkspaceOptionsOverlay } from "./workspace/WorkspaceOptionsOverlay";

/** Whether the panel is the live route, rather than sitting frozen underneath
 * an app modal that floats over it. */
function isPanelLiveRoute(): boolean {
  return isWorkspaceOverlayPath((m.route.get() ?? "").split("?")[0]);
}

/** The route this panel reads its params from. Normally the live one -- but
 * while an app modal (a request popup) floats over the panel, the live route is
 * that modal's and carries none of the panel's params, so the panel reads the
 * route it was opened on and stays mounted underneath unchanged. */
export function panelRoute(): string {
  const live = m.route.get() ?? "";
  if (isPanelLiveRoute()) return live;
  return getAppContext().shell.panelRouteBehindOverlay ?? live;
}

function panelParam(name: string): string | null {
  return new URLSearchParams(panelRoute().split("?")[1] ?? "").get(name);
}

function requestedAgentId(): string {
  return workspaceSurfaceIdFromPath(panelRoute().split("?")[0]) ?? "";
}

function requestedTab(): OptionsTab {
  return toOptionsTab(panelParam("tab"));
}

function requestedGroup(): SettingsGroup {
  const group = panelParam("group");
  return group === "account" || group === "backup" || group === "updates" ? group : "general";
}

/** The permissions left-nav entry the URL asks for. Any name is accepted here;
 * the pane falls back on its own when the entry no longer exists. */
function requestedSection(): string | null {
  const section = panelParam("section");
  return section !== null && section !== "" ? section : null;
}

/** Apply the share target the URL asks for, returning the value now applied.
 *
 * Runs every render: a share deep link can land while this panel is already
 * open, and the share model only exists once the load completes. Only a
 * CHANGE in the param's value selects -- the user's own target navigation
 * never touches the URL, so reapplying an unchanged param would fight it. */
export function applyRequestedTarget(
  share: Pick<ShareModel, "selectTarget"> | null,
  appliedTarget: string | null,
): string | null {
  const target = panelParam("target");
  if (share === null || target === appliedTarget) return appliedTarget;
  if (target) share.selectTarget(target);
  return target;
}

/** Keep ?tab=/?group=/?section= pointing at what is on screen (replace, no
 * history entry).
 *
 * Written against the same route the params are read from. A pane change can
 * land while a modal floats over the panel -- a connector sign-in resolves
 * long after it was started, and reviewing a Waiting-on-you request floats the
 * popup over the pane -- and writing that to the live route would move the
 * MODAL, leaving the panel to come back on its stale section. */
export function rememberInUrl(changes: Record<string, string | null>): void {
  const [path, query = ""] = panelRoute().split("?");
  const params = new URLSearchParams(query);
  let isChanged = false;
  // Every param in one write. Two writes in a row would each be computed from
  // `panelRoute()`, and m.route.set does not land synchronously -- so the
  // second would be built on the route the first replaced, and put back what
  // it had just removed.
  for (const [param, value] of Object.entries(changes)) {
    if ((params.get(param) ?? null) === value) continue;
    isChanged = true;
    if (value === null) params.delete(param);
    else params.set(param, value);
  }
  if (!isChanged) return;
  const next = `${path}?${params.toString()}`;
  if (!isPanelLiveRoute()) {
    getAppContext().shell.panelRouteBehindOverlay = next;
    return;
  }
  m.route.set(next, undefined, { replace: true });
}


export const WorkspaceOptionsPage: m.ClosureComponent = () => {
  let model: WorkspaceOptionsModel | null = null;
  let permissions: PermissionsModel | null = null;
  // The ?target value applyRequestedTarget last consumed (null until the
  // share model exists to consume one).
  let appliedTarget: string | null = null;
  function ensureModelsForRouteAgent(): { model: WorkspaceOptionsModel; permissions: PermissionsModel } {
    const agentId = requestedAgentId();
    // Route param changes preserve this component instance, so a navigation
    // to another workspace's options must swap the models by hand.
    if (model !== null && model.agentId !== agentId) {
      model.dispose();
      model = null;
      permissions = null;
      appliedTarget = null;
    }
    if (model === null) {
      model = new WorkspaceOptionsModel(agentId);
      void model.load();
    }
    if (permissions === null) {
      // Constructed but not loaded: the Permissions tab reads on its first
      // mount, so opening Share or Settings never touches the gateway.
      const created = new PermissionsModel(agentId);
      permissions = created;
      // The request popup answers a request; this pane is what shows the list
      // it was in, so the popup reaches the list through the shell.
      getAppContext().shell.registerWaitingRequestList(created);
    }
    return { model, permissions };
  }

  return {
    onremove() {
      if (permissions !== null) getAppContext().shell.unregisterWaitingRequestList(permissions);
      model?.dispose();
      model = null;
      permissions = null;
    },
    view() {
      const { model: currentModel, permissions: currentPermissions } = ensureModelsForRouteAgent();
      appliedTarget = applyRequestedTarget(currentModel.share, appliedTarget);
      // Every channel `requests` frame redraws mithril, so reconciling here
      // keeps the Permissions pane live without its own subscription -- and it
      // has to be here rather than in the tab, because the panel stays mounted
      // under the request popup and the tab is never re-created on the way back.
      if (currentPermissions.status === "ready")
        void currentPermissions.refreshIfPendingChanged(getAppContext().stores.requests.requestIds);
      return m(WorkspaceOptionsOverlay, {
        shell: getAppContext().shell,
        agentId: requestedAgentId(),
        model: currentModel,
        permissions: currentPermissions,
        tab: requestedTab(),
        group: requestedGroup(),
        section: requestedSection(),
        onSelectTab: (nextTab: OptionsTab) => rememberInUrl({ tab: nextTab }),
        onSelectGroup: (nextGroup: SettingsGroup) => rememberInUrl({ group: nextGroup }),
        onSelectSection: (nextSection: string) => rememberInUrl({ section: nextSection }),
        // The request opens as its own page over this panel, which stays
        // mounted underneath (openInbox remembers this route).
        onReviewRequest: (requestId: string) => getAppContext().shell.openInbox({ selected: requestId }),
      });
    },
  };
};
