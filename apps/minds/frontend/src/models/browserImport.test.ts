import { describe, expect, it } from "vitest";
import { jsonResponse } from "../testing";
import {
  BROWSER_IMPORT_OFFERED_URL,
  BROWSER_IMPORT_URL,
  BrowserImportModel,
  isBrowserImportOfferDue,
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
    resolveResponse(jsonResponse({ is_success: true, detail: "" }));

    await expect(run).resolves.toBe(true);
    expect(model.isBusy).toBe(false);
    expect(model.outcome).toEqual({
      is_success: true,
      detail: "",
    });
  });

  it("turns a refusal and a network failure into a failed outcome with the reason", async () => {
    const refused = new BrowserImportModel(
      async () =>
        jsonResponse(
          { error: "Browser cookies are not configured on this desktop" },
          503,
        ),
      () => {},
    );
    await expect(refused.run()).resolves.toBe(false);
    expect(refused.outcome).toEqual({
      is_success: false,
      detail: "Browser cookies are not configured on this desktop",
    });

    const offline = new BrowserImportModel(
      () => Promise.reject(new Error("offline")),
      () => {},
    );
    await expect(offline.run()).resolves.toBe(false);
    expect(offline.outcome?.is_success).toBe(false);
    expect(offline.outcome?.detail).toContain("network error");
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
});
