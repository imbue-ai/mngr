// The app-wide workspace-update settings: auto-update and the window scheduled
// updates start in. Every surface that draws them shows and writes the same
// app-wide values.

import m from "mithril";
import type { SettingsModel } from "../../../models/settings";
import { formatClockMinute } from "../../../models/updates";
import { renderSwitch } from "../../components/Switch";

/** Whole hours only: the setting means "while I am asleep". */
const HOUR_OPTIONS: number[] = Array.from(
  { length: 24 },
  (_unused, hour) => hour,
);

function hourSelect(
  id: string,
  value: number,
  onchange: (hour: number) => void,
): m.Children {
  return m(
    "select",
    {
      id,
      class: "h-[34px] px-2 rounded-md type-body bg-fill-subtle text-primary",
      value: String(value),
      onchange: (event: Event) =>
        onchange(Number((event.target as HTMLSelectElement).value)),
    },
    HOUR_OPTIONS.map((hour) =>
      m("option", { value: String(hour) }, formatClockMinute(hour * 60)),
    ),
  );
}

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
    m("div", { class: "flex items-center gap-2 py-3 border-b border-subtle" }, [
      m(
        "label",
        {
          class: "type-body text-primary",
          for: `update-window-start-${scope}`,
        },
        "Between",
      ),
      hourSelect(
        `update-window-start-${scope}`,
        startHour,
        (hour) => void model.setUpdateWindow(hour, endHour),
      ),
      m(
        "label",
        { class: "type-body text-primary", for: `update-window-end-${scope}` },
        "and",
      ),
      hourSelect(
        `update-window-end-${scope}`,
        endHour,
        (hour) => void model.setUpdateWindow(startHour, hour),
      ),
      m("span", { class: "type-helper text-tertiary" }, "local time"),
    ]),
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
