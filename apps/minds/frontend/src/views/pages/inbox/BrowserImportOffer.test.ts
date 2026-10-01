import { describe, expect, it } from "vitest";
import { InboxModel } from "../../../models/inbox";
import { allText, attrsOf, collectVnodes, renderRoot } from "../../../testing";
import type { AnyVnode } from "../../../testing";
import { BrowserImportOffer } from "./BrowserImportOffer";

function render(model: InboxModel): AnyVnode[] {
  return collectVnodes(renderRoot(BrowserImportOffer, { model }));
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

  it("asks the question with a way to say yes and a way to say not now", () => {
    const model = new InboxModel();
    model.isBrowserImportOfferOpen = true;

    const nodes = render(model);

    expect(allText(nodes)).toContain("Want to skip logging in?");
    expect(ids(nodes)).toEqual(
      expect.arrayContaining([
        "browser-import-accept",
        "browser-import-decline",
      ]),
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
    };

    const nodes = render(model);

    expect(allText(nodes)).toContain("Google Chrome is not installed.");
    expect(ids(nodes)).toEqual(
      expect.arrayContaining([
        "browser-import-continue",
        "browser-import-retry",
      ]),
    );
    expect(ids(nodes)).not.toContain("browser-import-accept");
  });
});
