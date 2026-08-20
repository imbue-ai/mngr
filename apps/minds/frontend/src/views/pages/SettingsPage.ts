// The app-level ("Minds") settings page: Connectors, Local files, Machines
// (delegation), Error reporting, and Master password. Port of
// templates/pages/Settings.jinja + AppSettingsSections.jinja +
// static/app_settings.js.

import m from "mithril";
import { SettingsModel } from "../../models/settings";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";
import { SettingsSections } from "./settings/SettingsSections";

export function SettingsPage(): m.Component {
  const model = new SettingsModel();
  return {
    oninit(): void {
      void model.load();
    },
    view(): m.Children {
      // Rendered inside the AppOverlay card (Shell), which supplies the width,
      // padding, and close X -- so no PageContainer or back link. The card's
      // body is a bounded column here rather than a scroller, so the title is
      // pinned (shrink-0) and the sections pane below it scrolls its own
      // columns.
      return [
        m(
          "h1",
          { class: "type-heading-lg text-primary mb-8 shrink-0" },
          "Settings",
        ),
        model.isLoadFailed
          ? m(
              Notice,
              { variant: "error" },
              "Settings could not be loaded. Refresh to try again.",
            )
          : model.overview === null
            ? m(
                "div",
                { class: "flex items-center gap-2 type-helper text-tertiary" },
                [m(Spinner, { size: "sm" }), "Loading settings…"],
              )
            : m(SettingsSections, { model }),
      ];
    },
  };
}
