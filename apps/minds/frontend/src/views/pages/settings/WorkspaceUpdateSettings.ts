// The app-wide workspace-update settings: auto-update and the window scheduled
// updates start in. Every surface that draws them shows and writes the same
// app-wide values.

import m from "mithril";
import { getAppContext } from "../../../app-context";
import type { SettingsModel } from "../../../models/settings";
import { renderSwitch } from "../../components/Switch";
import { renderUpdateWindowPicker } from "../../components/UpdateWindowPicker";

/** Where the controls are drawn: the app's own settings, or one workspace's,
 * which says the values are shared. */
export type WorkspaceUpdateSettingsScope = "app" | "workspace";

export function workspaceUpdateSettings(
  model: SettingsModel,
  scope: WorkspaceUpdateSettingsScope,
): m.Children {
  const overview = model.overview;
  if (overview === null) return null;
  const startHour = overview.update_window_start_hour;
  const endHour = overview.update_window_end_hour;
  const isAutoUpdateEnabled = overview.is_auto_update_enabled;
  return m("div", { id: `update-settings-${scope}` }, [
    scope === "workspace"
      ? m(
          "p",
          { class: "type-helper text-tertiary mb-3" },
          "These settings apply to every workspace; changing them here changes them everywhere.",
        )
      : null,
    m(
      "div",
      {
        class:
          "flex items-start justify-between gap-3 py-3 border-b border-subtle",
      },
      [
        m("span", [
          m(
            "span",
            { class: "type-body text-primary font-semibold" },
            "Update workspaces automatically",
          ),
          m(
            "span",
            {
              id: `auto-update-note-${scope}`,
              class: "block type-helper text-tertiary",
            },
            "When a workspace falls behind, Studio schedules its update for the window below. Updating uses " +
              "tokens and counts against your subscription or API usage.",
          ),
        ]),
        renderSwitch({
          id: `auto-update-toggle-${scope}`,
          isOn: isAutoUpdateEnabled,
          label: "Update workspaces automatically",
          describedBy: `auto-update-note-${scope}`,
          isWaiting: model.isAutoUpdateBusy,
          isDisabled: model.isAutoUpdateBusy,
          onFlip: (isOn) =>
            void model.setAutoUpdateEnabled(
              getAppContext().shell.accountId,
              isOn,
            ),
        }),
      ],
    ),
    model.autoUpdateError
      ? m(
          "p",
          { class: "type-helper text-important mt-3", role: "alert" },
          model.autoUpdateError,
        )
      : null,
    renderUpdateWindowPicker({
      idSuffix: scope,
      startHour,
      endHour,
      onChange: (start, end) =>
        void model.setUpdateWindow(getAppContext().shell.accountId, start, end),
      class: "py-3 border-b border-subtle",
    }),
    m(
      "p",
      { class: "type-helper text-tertiary mt-3" },
      "Scheduled updates start inside this window. A workspace must be running during the window for its " +
        "scheduled update to start; one that isn't is tried again in the next window.",
    ),
    model.updateWindowError
      ? m(
          "p",
          { class: "type-helper text-important mt-3", role: "alert" },
          model.updateWindowError,
        )
      : null,
  ]);
}
