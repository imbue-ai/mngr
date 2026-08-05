// The workspace options overlay (/workspace/<id>/options?tab=&group=&target=):
// Share machine + Machine settings tabs over one options-data load. This owns
// the URL-backed tab/group state and the options-data model; the docked panel
// chrome (backdrop, tab strip, card) lives in WorkspaceOptionsOverlay, which
// the Shell floats over the still-mounted workspace surface. The titlebar's
// ws-tab buttons land here; ?tab preselects the pane, ?group the settings
// group, ?target the share target.
//
// The URL is the single source of truth for tab/group: they are re-read from
// the route on every render, so titlebar-driven navigation (which changes
// only the query string, preserving this component instance) switches panes.

import m from "mithril";
import type { OptionsTab, SettingsGroup } from "../../models/workspaceOptions";
import { WorkspaceOptionsModel } from "../../models/workspaceOptions";
import { WorkspaceOptionsOverlay } from "./workspace/WorkspaceOptionsOverlay";

function requestedTab(): OptionsTab {
  return m.route.param("tab") === "settings" ? "settings" : "share";
}

function requestedGroup(): SettingsGroup {
  const group = m.route.param("group");
  return group === "account" || group === "backup" ? group : "general";
}

/** Keep ?tab=/?group= pointing at what is on screen (replace, no history entry). */
function rememberInUrl(param: string, value: string): void {
  const current = m.route.get();
  const [path, query = ""] = current.split("?");
  const params = new URLSearchParams(query);
  if (params.get(param) === value) return;
  params.set(param, value);
  m.route.set(`${path}?${params.toString()}`, undefined, { replace: true });
}

export const WorkspaceOptionsPage: m.ClosureComponent = () => {
  let model: WorkspaceOptionsModel | null = null;

  function ensureModelForRouteAgent(): WorkspaceOptionsModel {
    const agentId = m.route.param("agentId");
    // Route param changes preserve this component instance, so a navigation
    // to another workspace's options must swap the model by hand.
    if (model !== null && model.agentId !== agentId) {
      model.dispose();
      model = null;
    }
    if (model === null) {
      const created = new WorkspaceOptionsModel(agentId);
      model = created;
      void created.load().then(() => {
        const target = m.route.param("target");
        if (target && created.share) created.share.selectTarget(target);
      });
    }
    return model;
  }

  return {
    onremove() {
      model?.dispose();
      model = null;
    },
    view() {
      const currentModel = ensureModelForRouteAgent();
      return m(WorkspaceOptionsOverlay, {
        model: currentModel,
        tab: requestedTab(),
        group: requestedGroup(),
        onSelectTab: (nextTab: OptionsTab) => rememberInUrl("tab", nextTab),
        onSelectGroup: (nextGroup: SettingsGroup) => rememberInUrl("group", nextGroup),
      });
    },
  };
};
