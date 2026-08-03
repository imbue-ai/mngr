// The workspace options panel: Share machine / Machine settings tabs over one
// shared options-data load, rendered inside the Shell's workspace overlay by
// WorkspaceOptionsPage (/workspace/<id>/settings is a redirect into the
// settings tab here).

import m from "mithril";
import { Spinner } from "../../components/Spinner";
import { Notice } from "../../components/Notice";
import { Button } from "../../components/Button";
import type { OptionsTab, SettingsGroup, WorkspaceOptionsModel } from "../../../models/workspaceOptions";
import { SettingsGroups } from "./SettingsGroups";
import { ShareTab } from "./ShareTab";

export interface OptionsPanelAttrs {
  model: WorkspaceOptionsModel;
  tab: OptionsTab;
  group: SettingsGroup;
  onSelectTab: (tab: OptionsTab) => void;
  onSelectGroup: (group: SettingsGroup) => void;
}

const TABS: { id: OptionsTab; label: string }[] = [
  { id: "share", label: "Share machine" },
  { id: "settings", label: "Machine settings" },
];

export function OptionsPanel(): m.Component<OptionsPanelAttrs> {
  return {
    view(vnode) {
      const { model, tab, group, onSelectTab, onSelectGroup } = vnode.attrs;

      if (model.status === "loading") {
        return m("p", { class: "type-body text-secondary flex items-center gap-2 pt-10" }, [
          m(Spinner, { size: "sm" }),
          "Loading machine options...",
        ]);
      }
      if (model.status === "load_failed" || model.data === null || model.share === null) {
        return m("div", { class: "pt-10 flex flex-col gap-3 items-start" }, [
          m(Notice, { variant: "warn" }, `Could not load this machine's options: ${model.loadErrorMessage}`),
          m(Button, { variant: "secondary", onclick: () => void model.load() }, "Try again"),
        ]);
      }

      return m("div", { class: "flex flex-col flex-1 min-h-0" }, [
        m(
          "div",
          { role: "tablist", class: "flex items-end gap-1" },
          TABS.map((entry) =>
            m(
              "button",
              {
                type: "button",
                role: "tab",
                "data-wsopt-tab": entry.id,
                "aria-selected": entry.id === tab ? "true" : "false",
                class:
                  "px-4 py-2 rounded-t-lg type-body cursor-pointer transition-colors " +
                  (entry.id === tab
                    ? "bg-surface-primary text-primary font-semibold"
                    : "text-secondary hover:bg-fill-hover hover:text-primary"),
                onclick: () => onSelectTab(entry.id),
              },
              entry.label,
            ),
          ),
        ),
        tab === "share"
          ? m(ShareTab, { share: model.share, workspaceName: model.data.name })
          : m(SettingsGroups, { model, selectedGroup: group, onSelectGroup }),
      ]);
    },
  };
}
