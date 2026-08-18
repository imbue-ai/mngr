// The workspace options panel body: the Permissions / Share machine / Machine
// settings pane, rendered inside WorkspaceOptionsOverlay's docked card. The tab
// strip lives in that overlay (it hangs from the titlebar icon-tabs); this owns
// the pane title and the active pane's content.
//
// Share and Settings read one shared options-data load; Permissions has its
// own, so an unreachable latchkey gateway cannot take the other two down (nor
// they it). Only the tabs that need the options data wait behind its guard --
// and Permissions renders its own title, so it is not behind this one either.

import m from "mithril";
import { Spinner } from "../../components/Spinner";
import { Notice } from "../../components/Notice";
import { Button } from "../../components/Button";
import { Icon16 } from "../../components/Icon";
import type { OptionsTab, SettingsGroup, WorkspaceOptionsModel } from "../../../models/workspaceOptions";
import type { PermissionsModel } from "../../../models/workspacePermissions";
import { PermissionsTab } from "./PermissionsTab";
import { SettingsGroups } from "./SettingsGroups";
import { ShareTab } from "./ShareTab";

export interface OptionsPanelAttrs {
  model: WorkspaceOptionsModel;
  permissions: PermissionsModel;
  tab: OptionsTab;
  group: SettingsGroup;
  /** ?section for the Permissions left nav, or null for "whatever is first". */
  section: string | null;
  onSelectGroup: (group: SettingsGroup) => void;
  onSelectSection: (section: string) => void;
  /** Open a waiting request on its own page over this pane. */
  onReviewRequest: (requestId: string) => void;
}

/** The pane heading (the legacy WorkspaceShareSection / WorkspaceSettingsSections
 * h1): names the machine because the docked strip is icon-only and, when opened
 * from the workspace list, the titlebar crumb does not name it either. Pinned
 * (shrink-0) above the scrolling pane; the name truncates so a long one never
 * pushes into a second line or crowds the close X. */
function paneTitle(tab: OptionsTab, name: string): m.Child {
  const icon = tab === "share" ? "share" : "settings";
  const label = tab === "share" ? "Share machine:" : "Machine settings:";
  return m("h1", { class: "type-heading-lg text-primary flex items-center gap-2 min-w-0 shrink-0" }, [
    m(Icon16, { name: icon, size: "lg", extra: "shrink-0" }),
    m("span", { class: "shrink-0" }, label),
    m("span", { class: "truncate max-w-[280px]" }, name),
  ]);
}

export function OptionsPanel(): m.Component<OptionsPanelAttrs> {
  return {
    view(vnode) {
      const { model, permissions, tab, group, section, onSelectGroup, onSelectSection, onReviewRequest } =
        vnode.attrs;

      if (tab === "permissions") {
        return m(PermissionsTab, {
          model: permissions,
          workspaceName: model.data?.name ?? "",
          requestedSection: section,
          onSelectSection,
          onReviewRequest,
        });
      }

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
        paneTitle(tab, model.data.name),
        tab === "share"
          ? m(ShareTab, { share: model.share, workspaceName: model.data.name })
          : m(SettingsGroups, { model, selectedGroup: group, onSelectGroup }),
      ]);
    },
  };
}
