// Which panel the page opens on, which is decided by `?section=` and by the
// nav -- and the two must not fight.

import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SettingsModel } from "../../models/settings";
import { settingsOverview, settle, withMindsNative, withReceiverGuardedGlobalFetch } from "../../testing";
import { SettingsPage } from "./SettingsPage";
import { SettingsSections } from "./settings/SettingsSections";

/** The page with its hooks callable directly, since there is no DOM to mount into. */
interface Page {
  oninit: () => void;
  onbeforeupdate: () => boolean;
  view: () => m.Children;
}

function openPage(): Page {
  return SettingsPage() as unknown as Page;
}

/** Run `run` with a global fetch that rejects, which is what a backend that is
 * not answering looks like to SettingsModel.load(). Local to this file: it is
 * the only place that needs the failure, and the shared helper serves a payload. */
async function withUnreachableGlobalFetch(run: () => Promise<void>): Promise<void> {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (() => Promise.reject(new Error("backend unreachable"))) as typeof fetch;
  try {
    await run();
  } finally {
    globalThis.fetch = originalFetch;
  }
}

/** The model the view hands to SettingsSections, which is what holds the panel choice. */
function shownModel(page: Page): SettingsModel {
  const rendered = page.view() as m.Vnode[];
  const sections = rendered.find(
    (vnode) => vnode !== null && (vnode.tag as unknown) === (SettingsSections as unknown),
  );
  if (sections === undefined) throw new Error("the page is not showing its sections");
  return (sections.attrs as { model: SettingsModel }).model;
}

describe("SettingsPage", () => {
  afterEach(() => vi.restoreAllMocks());

  it("opens the section the route asks for even when the page is already on screen", async () => {
    // The menu bar's "Check for Updates..." navigates a window that may already
    // be showing Settings. The router returns the same component for
    // `/settings`, so oninit does not run again and the ask would be dropped.
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const section = vi.spyOn(m.route, "param").mockReturnValue(undefined as unknown as string);
    await withReceiverGuardedGlobalFetch(settingsOverview(), async () => {
      await withMindsNative({}, async () => {
        const page = openPage();
        page.oninit();
        await settle();
        expect(shownModel(page).activeSection).toBe("connectors");

        section.mockReturnValue("updates");
        page.onbeforeupdate();

        expect(shownModel(page).activeSection).toBe("updates");
      });
    });
  });

  it("does not drag the panel back to a section the query still names", async () => {
    // The query outlives the ask: it stays on the URL while the user reads down
    // the nav, and every redraw would re-apply it.
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    vi.spyOn(m.route, "param").mockReturnValue("updates");
    await withReceiverGuardedGlobalFetch(settingsOverview(), async () => {
      await withMindsNative({}, async () => {
        const page = openPage();
        page.oninit();
        await settle();
        shownModel(page).selectSection("error-reporting");

        page.onbeforeupdate();

        expect(shownModel(page).activeSection).toBe("error-reporting");
      });
    });
  });

  it("still opens Updates when the settings payload could not be loaded", async () => {
    // The panel is the only surface that reports a check now, and the menu
    // bar's "Check for Updates..." lands on it. Updates reads none of the
    // /ui/api/settings payload, so a backend that is down must not hide it --
    // that is the state where taking a different build is the user's own
    // remedy. Every other section still says the payload is missing.
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const section = vi.spyOn(m.route, "param").mockReturnValue("updates");
    await withUnreachableGlobalFetch(async () => {
      await withMindsNative({}, async () => {
        const page = openPage();
        page.oninit();
        await settle();
        expect(shownModel(page).isLoadFailed).toBe(true);
        expect(shownModel(page).activeSection).toBe("updates");

        section.mockReturnValue("connectors");
        page.onbeforeupdate();

        expect(() => shownModel(page)).toThrow("not showing its sections");
      });
    });
  });

  it("ignores a section name it does not know", async () => {
    // A stale or mistyped deep link would otherwise land on a panel that
    // renders nothing and has no nav entry to leave it by.
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    vi.spyOn(m.route, "param").mockReturnValue("machine-updates");
    await withReceiverGuardedGlobalFetch(settingsOverview(), async () => {
      await withMindsNative(null, async () => {
        const page = openPage();
        page.oninit();
        await settle();

        expect(shownModel(page).activeSection).toBe("connectors");
      });
    });
  });
});
