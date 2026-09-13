// The app-level ("Minds") settings page: Connectors, Local files, Machines
// (delegation), Error reporting, Updates, and Master password. Port of
// templates/pages/Settings.jinja + AppSettingsSections.jinja +
// static/app_settings.js.

import m from "mithril";
import { SettingsModel } from "../../models/settings";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";
import { SettingsSections } from "./settings/SettingsSections";

export function SettingsPage(): m.Component {
  const model = new SettingsModel();
  // The last `?section=` acted on. The page is the same component instance for
  // the whole of `/settings`, so an unchanged param arrives on every redraw --
  // re-applying it would drag the panel back off whatever the nav was just
  // clicked to.
  let appliedSection: string | null = null;

  // `?section=` lets something outside the page open a specific panel -- the
  // menu bar's "Check for Updates..." lands on Updates rather than answering in
  // a dialog of its own. Read on update as well as on init: the router returns
  // the same component for `/settings`, so arriving from `/settings` with a new
  // query re-renders the page rather than re-creating it.
  //
  // Validated rather than cast: an unrecognized name -- or one this build does
  // not offer -- would select a section that renders nothing and has no nav
  // entry to leave it by.
  function selectRequestedSection(): void {
    const requested = m.route.param("section") ?? null;
    if (requested === appliedSection) return;
    appliedSection = requested;
    const section = model.visibleSections.find((s) => s.name === requested);
    if (section !== undefined) {
      model.selectSection(section.name);
    }
  }

  return {
    oninit(): void {
      selectRequestedSection();
      void model.load();
      // Desktop-only, and independent of the /ui/api settings payload: the
      // update state comes from the Electron main process, not the backend.
      void model.loadUpdateState();
    },
    // Before the diff, not after it, so the section it picks is in the render
    // this redraw produces.
    onbeforeupdate(): boolean {
      selectRequestedSection();
      return true;
    },
    view(): m.Children {
      // Rendered inside the AppOverlay card (Shell), which supplies the width,
      // padding, and close X -- so no PageContainer or back link. The card's
      // body is a bounded column here rather than a scroller, so the title is
      // pinned (shrink-0) and the sections pane below it scrolls its own
      // columns.
      // Updates is the one section that reads none of the /ui/api/settings
      // payload -- it comes from the Electron main process -- so it stays
      // reachable while that payload is missing. Which matters because it is
      // now the only surface reporting an update check, and the menu bar's
      // "Check for Updates..." lands here: gating it on the backend would make
      // a broken backend hide the one remedy the user can apply themselves.
      const isPayloadNeeded = model.activeSection !== "updates";
      return [
        m(
          "h1",
          { class: "type-heading-lg text-primary mb-8 shrink-0" },
          "Settings",
        ),
        model.overview !== null || !isPayloadNeeded
          ? m(SettingsSections, { model })
          : model.isLoadFailed
            ? m(
                Notice,
                { variant: "error" },
                "Settings could not be loaded. Refresh to try again.",
              )
            : m(
                "div",
                { class: "flex items-center gap-2 type-helper text-tertiary" },
                [m(Spinner, { size: "sm" }), "Loading settings…"],
              ),
      ];
    },
  };
}
