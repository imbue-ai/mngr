// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BrowserImportModel } from "../../models/browserImport";
import {
  buttonLabelled,
  chromeMissingOutcome,
  failedImportOutcome,
  mountInBody,
  primaryButtonLabels,
  setFailedImport,
  unreadableChromeProfileOutcome,
  withMindsNative,
} from "../../testing";
import { browserImportFailureNotice } from "./UnreadableChromeProfileNotice";

function mountFailureNotice(model: BrowserImportModel): HTMLElement {
  return mountInBody(() =>
    browserImportFailureNotice(model, "browser-import-error"),
  );
}

function importModel(): BrowserImportModel {
  return new BrowserImportModel(
    () => Promise.reject(new Error("not called")),
    () => {},
  );
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("browserImportFailureNotice", () => {
  it.each([
    ["off macOS", false, "readable by the account running"],
    ["on macOS", true, "Full Disk Access"],
  ])(
    "keeps latchkey's reason for an unreadable profile behind See details %s, with no Report a bug",
    (_where, isFullDiskAccessNeeded, guidance) => {
      const model = importModel();
      model.outcome = unreadableChromeProfileOutcome(isFullDiskAccessNeeded);
      const root = mountFailureNotice(model);

      expect(root.textContent).toContain(guidance);
      expect(root.textContent).not.toContain("EPERM");
      expect(root.textContent).not.toContain("Report a bug");

      buttonLabelled(root, "See details").click();
      m.redraw.sync();

      expect(root.textContent).toContain(
        "The Google Chrome profile could not be read: EPERM.",
      );
    },
  );

  it("leads with Open System Settings as the main button, and swaps it for the way to open it by hand once opening it has failed", async () => {
    const openFullDiskAccessSettings = vi.fn(async () => false);
    await withMindsNative(
      { platform: "darwin", openFullDiskAccessSettings },
      async () => {
        const model = new BrowserImportModel(
          () => Promise.reject(new Error("not called")),
          () => m.redraw.sync(),
        );
        model.outcome = unreadableChromeProfileOutcome();
        const root = mountFailureNotice(model);
        expect(primaryButtonLabels(root)).toEqual(["Open System Settings"]);

        buttonLabelled(root, "Open System Settings").click();
        await vi.waitFor(() =>
          expect(root.textContent).toContain(
            "open it from the Apple menu instead",
          ),
        );

        expect(openFullDiskAccessSettings).toHaveBeenCalledTimes(1);
        expect(() => buttonLabelled(root, "Open System Settings")).toThrow();
      },
    );
  });

  it.each([
    [
      "once System Settings is open",
      true,
      ["Reopen Imbue Studio", "Open System Settings"],
    ],
    ["once it has to be opened by hand", false, ["Reopen Imbue Studio"]],
  ])(
    "makes reopening the app the main button %s, and reopens it",
    async (_when, isOpened, labels) => {
      const relaunchApp = vi.fn();
      await withMindsNative(
        {
          platform: "darwin",
          openFullDiskAccessSettings: async () => isOpened,
          relaunchApp,
        },
        async () => {
          const model = new BrowserImportModel(
            () => Promise.reject(new Error("not called")),
            () => m.redraw.sync(),
          );
          model.outcome = unreadableChromeProfileOutcome();
          const root = mountFailureNotice(model);
          expect(() => buttonLabelled(root, "Reopen Imbue Studio")).toThrow();

          buttonLabelled(root, "Open System Settings").click();
          await vi.waitFor(() =>
            expect(primaryButtonLabels(root)).toEqual(["Reopen Imbue Studio"]),
          );
          expect(
            [...root.querySelectorAll("button")].map((button) =>
              button.textContent?.trim(),
            ),
          ).toEqual(expect.arrayContaining(labels));

          buttonLabelled(root, "Reopen Imbue Studio").click();
          expect(relaunchApp).toHaveBeenCalledOnce();
        },
      );
    },
  );

  it("words a computer with no Chrome as ours, with latchkey's reason behind See details and no Report a bug", () => {
    const model = importModel();
    setFailedImport(model, chromeMissingOutcome());
    const root = mountFailureNotice(model);

    expect(root.textContent).toContain(
      "Imbue Studio couldn't find Google Chrome's cookies on this computer.",
    );
    expect(root.textContent).not.toContain("standard locations");
    expect(root.textContent).not.toContain("Report a bug");

    buttonLabelled(root, "See details").click();
    m.redraw.sync();

    expect(root.textContent).toContain(
      "Google Chrome is not installed in any of the standard locations.",
    );
  });

  it("offers Report a bug for a failure that has no explanation of its own", () => {
    const model = importModel();
    setFailedImport(model, failedImportOutcome());
    const root = mountFailureNotice(model);

    expect(buttonLabelled(root, "Report a bug")).toBeDefined();
  });
});
