// The app-level ("Minds") settings page: Connectors, Local files, Machines
// (delegation), Error reporting, and Master password. Port of
// templates/pages/Settings.jinja + AppSettingsSections.jinja +
// static/app_settings.js.

import m from "mithril";
import { SettingsModel } from "../../models/settings";
import { PageContainer } from "../components/Layout";
import { Link } from "../components/Link";
import { Notice } from "../components/Notice";
import { routeLinkAttrs } from "../components/route-link";
import { Spinner } from "../components/Spinner";
import { SettingsSections } from "./settings/SettingsSections";

export function SettingsPage(): m.Component {
  const model = new SettingsModel();
  return {
    oninit(): void {
      void model.load();
    },
    view(): m.Children {
      return m(PageContainer, [
        m("h1", { class: "type-heading-lg text-primary mb-8" }, "Settings"),
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
        m(
          "div",
          { class: "mt-8" },
          m(Link, { extra: "type-helper", ...routeLinkAttrs("/") }, "← Back to machines"),
        ),
      ]);
    },
  };
}
