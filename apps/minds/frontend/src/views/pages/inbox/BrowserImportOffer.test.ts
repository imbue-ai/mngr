import { describe, expect, it, vi } from "vitest";
import { InboxModel } from "../../../models/inbox";
import {
  allText,
  attrsOf,
  byId,
  collectVnodes,
  renderDeep,
  renderRoot,
  unreadableChromeProfileOutcome,
  withMindsNative,
} from "../../../testing";
import type { AnyVnode } from "../../../testing";
import { BrowserImportOffer } from "./BrowserImportOffer";

function render(model: InboxModel): AnyVnode[] {
  return collectVnodes(renderRoot(BrowserImportOffer, { model }));
}

/** As a reader would see it: the notice components rendered through. */
function renderThrough(model: InboxModel): AnyVnode[] {
  return collectVnodes(renderDeep(renderRoot(BrowserImportOffer, { model })));
}

function ids(nodes: AnyVnode[]): string[] {
  return nodes
    .map((node) => attrsOf(node).id)
    .filter((id): id is string => typeof id === "string");
}

describe("BrowserImportOffer", () => {
  it("keeps the modal closed until the model opens the offer", () => {
    const isOpen = (model: InboxModel): unknown =>
      render(model).find((node) => "isOpen" in attrsOf(node))?.attrs?.isOpen;
    const model = new InboxModel();
    expect(isOpen(model)).toBe(false);
    model.isBrowserImportOfferOpen = true;
    expect(isOpen(model)).toBe(true);
  });

  it("asks the question with going on without the import as the primary answer", () => {
    const model = new InboxModel();
    model.isBrowserImportOfferOpen = true;

    const nodes = render(model);

    expect(allText(nodes)).toContain("Import your Chrome cookies?");
    expect(allText(nodes)).toContain("This is optional");
    expect(attrsOf(byId(nodes, "browser-import-decline")!).variant).toBe(
      "primary",
    );
    expect(attrsOf(byId(nodes, "browser-import-accept")!).variant).toBe(
      "secondary",
    );
  });

  it("shows progress instead of the buttons while the import runs", () => {
    const model = new InboxModel();
    model.isBrowserImportOfferOpen = true;
    model.browserImport.isBusy = true;

    const nodes = render(model);

    expect(ids(nodes)).toContain("browser-import-progress");
    expect(ids(nodes)).not.toContain("browser-import-accept");
  });

  it("after a failed import, shows the reason and offers to go on to the sign-in", () => {
    const model = new InboxModel();
    model.isBrowserImportOfferOpen = true;
    model.browserImport.outcome = {
      is_success: false,
      detail: "Google Chrome is not installed.",
      error_code: null,
      is_full_disk_access_needed: false,
    };

    const nodes = render(model);

    expect(allText(nodes)).toContain("Google Chrome is not installed.");
    // The question is answered: the outcome replaces the offer's pitch.
    expect(allText(nodes)).toContain("Couldn't import your Chrome cookies");
    expect(allText(nodes)).not.toContain("about to open a browser window");
    expect(allText(nodes)).not.toContain("You can import later");
    expect(ids(nodes)).toEqual(
      expect.arrayContaining([
        "browser-import-continue",
        "browser-import-retry",
      ]),
    );
    expect(ids(nodes)).not.toContain("browser-import-accept");
  });

  it("after an import macOS refused, explains Full Disk Access instead of quoting the error, with no retry", async () => {
    const openFullDiskAccessSettings = vi.fn(async () => true);
    await withMindsNative(
      { platform: "darwin", openFullDiskAccessSettings },
      async () => {
        const model = new InboxModel();
        model.isBrowserImportOfferOpen = true;
        model.browserImport.outcome = unreadableChromeProfileOutcome();

        const nodes = renderThrough(model);

        expect(allText(nodes)).toContain("Full Disk Access");
        expect(allText(nodes)).not.toContain("EPERM");
        expect(ids(nodes)).toContain("browser-import-unreadable-profile");
        expect(ids(nodes)).toContain("browser-import-continue");
        expect(ids(nodes)).not.toContain("browser-import-retry");

        const openButton = byId(nodes, "browser-import-open-full-disk-access");
        (attrsOf(openButton!).onclick as () => void)();
        expect(openFullDiskAccessSettings).toHaveBeenCalledTimes(1);
      },
    );
  });

  it("leaves the Open System Settings button out where there is no list to open", async () => {
    const model = new InboxModel();
    model.isBrowserImportOfferOpen = true;
    model.browserImport.outcome = unreadableChromeProfileOutcome();
    const expectNoticeWithoutButton = (): void => {
      const nodes = renderThrough(model);
      expect(ids(nodes)).toContain("browser-import-unreadable-profile");
      expect(ids(nodes)).not.toContain("browser-import-open-full-disk-access");
    };

    // The browser build: no desktop shell at all.
    expectNoticeWithoutButton();

    // The desktop app off macOS: the shell knows the call, but there is no
    // Full Disk Access list for it to open.
    const openFullDiskAccessSettings = vi.fn(async () => true);
    await withMindsNative(
      { platform: "linux", openFullDiskAccessSettings },
      async () => {
        expectNoticeWithoutButton();
      },
    );
    expect(openFullDiskAccessSettings).not.toHaveBeenCalled();
  });

  it("tells the user to fix the profile's permissions, with no retry, when it was unreadable off macOS", () => {
    const model = new InboxModel();
    model.isBrowserImportOfferOpen = true;
    model.browserImport.outcome = unreadableChromeProfileOutcome(false);

    const nodes = renderThrough(model);

    expect(ids(nodes)).toContain("browser-import-unreadable-profile");
    expect(allText(nodes)).toContain("readable by the account running");
    expect(allText(nodes)).not.toContain("Full Disk Access");
    // Latchkey's sentence names the path, so it stays.
    expect(allText(nodes)).toContain(
      "The Google Chrome profile could not be read: EPERM.",
    );
    expect(ids(nodes)).not.toContain("browser-import-open-full-disk-access");
    expect(ids(nodes)).not.toContain("browser-import-retry");
    expect(ids(nodes)).toContain("browser-import-continue");
  });
});
