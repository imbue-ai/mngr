// Studio settings' workspace-update settings: the app-wide auto-update switch,
// the workspaces that use their own setting instead, and the window scheduled
// updates start in. Each workspace's own choice is made on its Updates tab.

import m from "mithril";
import { getAppContext } from "../../../app-context";
import type { SettingsModel } from "../../../models/settings";
import { Button } from "../../components/Button";
import { Spinner } from "../../components/Spinner";
import { renderSwitch } from "../../components/Switch";
import { renderUpdateWindowPicker } from "../../components/UpdateWindowPicker";

export function workspaceUpdateSettings(model: SettingsModel): m.Children {
  const overview = model.overview;
  if (overview === null) return null;
  return m("div", { id: "update-settings-app" }, [
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
              id: "auto-update-note-app",
              class: "block type-helper text-tertiary",
            },
            "When a workspace falls behind, Studio schedules its update for the window below. Updating uses " +
              "tokens and counts against your subscription or API usage.",
          ),
        ]),
        renderSwitch({
          id: "auto-update-toggle-app",
          isOn: overview.is_auto_update_enabled,
          label: "Update workspaces automatically",
          describedBy: "auto-update-note-app",
          isWaiting: model.isAutoUpdateBusy,
          isDisabled: model.isAutoUpdateBusy,
          onFlip: (isOn) => void model.setAutoUpdateEnabled(isOn),
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
    customSettingsRow(model),
    renderUpdateWindowPicker({
      idSuffix: "app",
      startHour: overview.update_window_start_hour,
      endHour: overview.update_window_end_hour,
      onChange: (start, end) => void model.setUpdateWindow(start, end),
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

/** The workspaces with their own auto-update setting, or nothing when none has one.
 * Each name opens that workspace's Updates tab; "Use Studio settings" puts it back on these. */
function customSettingsRow(model: SettingsModel): m.Children {
  const { stores, shell } = getAppContext();
  const updates = stores.updates;
  const custom = stores.workspaces.workspaces.flatMap((workspace) => {
    const autoUpdate = updates.autoUpdateFor(workspace.id);
    return autoUpdate.override === null ? [] : [{ workspace, autoUpdate }];
  });
  if (custom.length === 0) return null;
  return m(
    "div",
    {
      id: "update-settings-custom",
      class: "flex flex-col gap-2 py-3 border-b border-subtle",
    },
    [
      m("span", [
        m(
          "span",
          { class: "type-body text-primary font-semibold" },
          "Custom settings",
        ),
        m(
          "span",
          { class: "block type-helper text-tertiary" },
          "These workspaces use their own settings instead of these.",
        ),
      ]),
      m(
        "ul",
        { class: "flex flex-col gap-1" },
        custom.map(({ workspace, autoUpdate }) =>
          m(
            "li",
            {
              class:
                "flex items-center justify-between gap-3 type-body text-secondary",
              "data-agent-id": workspace.id,
            },
            [
              m("span", { class: "min-w-0" }, [
                m(
                  "button",
                  {
                    type: "button",
                    class:
                      "text-primary underline underline-offset-2 hover:opacity-80 cursor-pointer",
                    onclick: () => shell.openUpdatesTab(workspace.id),
                  },
                  workspace.name,
                ),
                autoUpdate.isEnabled
                  ? ` · auto-update on · ${autoUpdate.window}`
                  : " · auto-update off",
              ]),
              m(
                Button,
                {
                  variant: "secondary",
                  extra: "shrink-0",
                  disabled: model.workspaceResettingId !== null,
                  onclick: () =>
                    void model.resetWorkspaceAutoUpdate(workspace.id, updates),
                },
                model.workspaceResettingId === workspace.id
                  ? m(Spinner, { size: "sm" })
                  : "Use Studio settings",
              ),
            ],
          ),
        ),
      ),
      model.workspaceResetError
        ? m(
            "p",
            { class: "type-helper text-important", role: "alert" },
            model.workspaceResetError,
          )
        : null,
    ],
  );
}
