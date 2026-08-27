// The workspace-options overlay: the Permissions / Share machine / Machine
// settings panel docked under the titlebar's icon-tabs, a faithful port of the
// legacy WorkspaceOptionsShell.jinja docked presentation.
//
// This file is now just the panel's identity: which pane it shows, what its
// card measures, and where its tabs lead. Everything else -- hanging from the
// titlebar's #ws-tab-strip, the raised five-icon strip over it, the backdrop,
// the close X, the centered fallback before the titlebar has been measured --
// is OverlayShell at the "docked" placement, the same one the request popup
// takes.

import m from "mithril";
import type { OptionsTab, SettingsGroup, WorkspaceOptionsModel } from "../../../models/workspaceOptions";
import type { PermissionsModel } from "../../../models/workspacePermissions";
import type { ShellState } from "../../shell/shell-state";
import { OverlayShell } from "../../shell/OverlayShell";
import { OptionsPanel } from "./OptionsPanel";
import { openTitlebarPopup } from "../../shell/RaisedTitlebarIcons";

export interface WorkspaceOptionsOverlayAttrs {
  shell: ShellState;
  /** The machine this panel is open on, so dismissing never has to re-read a
   * route that may belong to a modal floating over the panel. */
  agentId: string;
  model: WorkspaceOptionsModel;
  permissions: PermissionsModel;
  tab: OptionsTab;
  group: SettingsGroup;
  /** ?section for the Permissions left nav, or null for "whatever is first". */
  section: string | null;
  onSelectTab: (tab: OptionsTab) => void;
  onSelectGroup: (group: SettingsGroup) => void;
  onSelectSection: (section: string) => void;
  /** Open a waiting request on its own page over this pane. */
  onReviewRequest: (requestId: string) => void;
}

export function WorkspaceOptionsOverlay(): m.Component<WorkspaceOptionsOverlayAttrs> {
  function dismiss(agentId: string): void {
    // Back to the bare workspace surface (kept mounted behind the overlay),
    // mirroring shell.closeWorkspaceOverlay without needing a shell handle.
    m.route.set(`/workspace/${agentId}`);
  }

  return {
    view(vnode) {
      const {
        shell,
        agentId,
        model,
        permissions,
        tab,
        group,
        section,
        onSelectTab,
        onSelectGroup,
        onSelectSection,
        onReviewRequest,
      } = vnode.attrs;
      return m(
        OverlayShell,
        {
          shell,
          placement: "docked",
          selected: tab,
          panelId: "ws-options-panel",
          backdropId: "ws-options-backdrop",
          closeButtonId: "ws-options-close",
          // A fixed-height card, so a long pane (twenty share entries) scrolls
          // inside it rather than growing the card off-screen; capped to the
          // window, and it stops widening at 880px.
          cardClass: "h-[660px] w-full max-w-[880px]",
          // A column, not a scroller: the pane inside decides what stays
          // pinned (its title + nav) and what scrolls, so a long pane never
          // drags the title off the top with it.
          bodyClass: "flex-1 min-h-0 flex flex-col px-6 py-4",
          onDismiss: () => dismiss(agentId),
          // Within the panel a tab switch is a param change, not a fresh open:
          // `onSelectTab` keeps the group and section the other tabs were left
          // on. The bell and the bug button are different surfaces, so those
          // leave through the strip's own transition.
          onSelectIcon: (id) =>
            id === "notifications" || id === "help"
              ? openTitlebarPopup(shell, id)
              : onSelectTab(id),
        },
        m(OptionsPanel, {
          model,
          permissions,
          tab,
          group,
          section,
          onSelectGroup,
          onSelectSection,
          onReviewRequest,
        }),
      );
    },
  };
}
