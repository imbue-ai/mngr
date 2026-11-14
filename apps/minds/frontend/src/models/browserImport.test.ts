import { describe, expect, it, vi } from "vitest";
import {
  browserImportOutcome,
  chromeMissingOutcome,
  jsonResponse,
  unreadableChromeProfileOutcome,
  withMindsNative,
} from "../testing";
import {
  BROWSER_IMPORT_OFFERED_URL,
  BROWSER_IMPORT_URL,
  BrowserImportModel,
  isBrowserImportOfferDue,
  isChromeProfileUnreadable,
  markBrowserImportOffered,
} from "./browserImport";

describe("isBrowserImportOfferDue", () => {
  it("is due while the offer has not been made and there is a latchkey to import into", async () => {
    await expect(
      isBrowserImportOfferDue(async () =>
        jsonResponse({ is_offered: false, is_available: true }),
      ),
    ).resolves.toBe(true);
  });

  it("is not due once made, nor without a latchkey, nor when the backend cannot say", async () => {
    await expect(
      isBrowserImportOfferDue(async () =>
        jsonResponse({ is_offered: true, is_available: true }),
      ),
    ).resolves.toBe(false);
    await expect(
      isBrowserImportOfferDue(async () =>
        jsonResponse({ is_offered: false, is_available: false }),
      ),
    ).resolves.toBe(false);
    await expect(
      isBrowserImportOfferDue(async () => new Response("", { status: 503 })),
    ).resolves.toBe(false);
    await expect(
      isBrowserImportOfferDue(() => Promise.reject(new Error("offline"))),
    ).resolves.toBe(false);
  });
});

describe("markBrowserImportOffered", () => {
  it("posts the mark and swallows a failure to do so", async () => {
    const urls: string[] = [];
    await markBrowserImportOffered((url, init) => {
      urls.push(`${init?.method ?? "GET"} ${url}`);
      return Promise.reject(new Error("offline"));
    });
    expect(urls).toEqual([`POST ${BROWSER_IMPORT_OFFERED_URL}`]);
  });
});

describe("BrowserImportModel", () => {
  it("is busy for the run and keeps the outcome latchkey reported", async () => {
    let resolveResponse: (response: Response) => void = () => undefined;
    const model = new BrowserImportModel(
      (url, init) => {
        expect(`${init?.method} ${url}`).toBe(`POST ${BROWSER_IMPORT_URL}`);
        return new Promise((resolve) => {
          resolveResponse = resolve;
        });
      },
      () => {},
    );

    const run = model.run();
    expect(model.isBusy).toBe(true);
    expect(model.outcome).toBeNull();
    resolveResponse(
      jsonResponse(browserImportOutcome()),
    );

    await expect(run).resolves.toBe(true);
    expect(model.isBusy).toBe(false);
    expect(model.outcome?.is_success).toBe(true);
    expect(model.error).toBeNull();
  });

  it("shows the server's sentence for a failed import, with latchkey's reason behind See details", async () => {
    const model = new BrowserImportModel(
      async () => jsonResponse(chromeMissingOutcome()),
      () => {},
    );

    await expect(model.run()).resolves.toBe(false);

    expect(model.error).toEqual({
      message: "Imbue Studio couldn't find Google Chrome's cookies on this computer.",
      details: "Google Chrome is not installed in any of the standard locations.",
    });
  });

  it("words a refusal and a network failure as ours, with what came back behind See details", async () => {
    const refused = new BrowserImportModel(
      async () =>
        jsonResponse(
          { error: "Browser cookies are not configured on this desktop" },
          503,
        ),
      () => {},
    );
    await expect(refused.run()).resolves.toBe(false);
    expect(refused.error).toEqual({
      message: "Imbue Studio couldn't import your Chrome cookies. Try again. If this persists, report a bug.",
      details: "HTTP 503\nBrowser cookies are not configured on this desktop",
    });
    expect(refused.outcome).toEqual(
      browserImportOutcome({
        is_success: false,
        message: refused.error?.message,
        detail: refused.error?.details,
      }),
    );

    const offline = new BrowserImportModel(
      () => Promise.reject(new Error("offline")),
      () => {},
    );
    await expect(offline.run()).resolves.toBe(false);
    expect(offline.outcome?.is_success).toBe(false);
    expect(offline.error?.message).toBe("Imbue Studio didn't respond. Try again in a moment.");
  });

  it("keeps a plain-text refusal behind See details, never as the sentence", async () => {
    const model = new BrowserImportModel(
      async () => new Response("Internal Server Error: KeyError('latchkey')", { status: 500 }),
      () => {},
    );

    await expect(model.run()).resolves.toBe(false);

    expect(model.error).toEqual({
      message: "Imbue Studio couldn't import your Chrome cookies. Try again. If this persists, report a bug.",
      details: "HTTP 500\nInternal Server Error: KeyError('latchkey')",
    });
  });

  it("does not start a second run while one is in flight", async () => {
    let callCount = 0;
    const model = new BrowserImportModel(
      () => {
        callCount += 1;
        return new Promise(() => undefined);
      },
      () => {},
    );
    void model.run();
    await expect(model.run()).resolves.toBe(false);
    expect(callCount).toBe(1);
  });

  it("opens the Full Disk Access list through the desktop shell and notes when that did not work", async () => {
    const openFullDiskAccessSettings = vi.fn(async () => false);
    await withMindsNative(
      { platform: "darwin", openFullDiskAccessSettings },
      async () => {
        const model = new BrowserImportModel(
          async () => jsonResponse({}),
          () => {},
        );
        await model.openFullDiskAccessSettings();
        expect(openFullDiskAccessSettings).toHaveBeenCalledTimes(1);
        expect(model.isFullDiskAccessSettingsOpenFailed).toBe(true);

        openFullDiskAccessSettings.mockResolvedValueOnce(true);
        await model.openFullDiskAccessSettings();
        expect(model.isFullDiskAccessSettingsOpenFailed).toBe(false);
      },
    );
  });

  it("forgets a failed settings open when a new run starts", async () => {
    const model = new BrowserImportModel(
      async () =>
        jsonResponse(browserImportOutcome()),
      () => {},
    );
    model.isFullDiskAccessSettingsOpenFailed = true;
    await model.run();
    expect(model.isFullDiskAccessSettingsOpenFailed).toBe(false);
  });
});

describe("isChromeProfileUnreadable", () => {
  it("is exactly the failure latchkey codes as an unreadable Chrome profile, on any platform", () => {
    expect(isChromeProfileUnreadable(unreadableChromeProfileOutcome())).toBe(
      true,
    );
    expect(
      isChromeProfileUnreadable(unreadableChromeProfileOutcome(false)),
    ).toBe(true);
    expect(
      isChromeProfileUnreadable(chromeMissingOutcome()),
    ).toBe(false);
    expect(
      isChromeProfileUnreadable(browserImportOutcome()),
    ).toBe(false);
    expect(isChromeProfileUnreadable(null)).toBe(false);
  });
});
