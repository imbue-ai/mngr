import { describe, expect, it } from "vitest";
import { jsonResponse, withReceiverGuardedGlobalFetch } from "../testing";
import { SettingsModel, type SettingsOverview } from "./settings";

const BASE_OVERVIEW: SettingsOverview = {
  services_overview: [],
  file_sharing_grants: [],
  workspace_delegation_grants: [],
  permissions_unavailable: false,
  is_master_password_set: false,
  report_unexpected_errors: true,
  version: "v-one",
};

describe("SettingsModel", () => {
  it("invokes the default fetch as a plain call (Illegal-invocation regression guard)", async () => {
    // Browsers reject the global fetch when it is invoked with any other
    // receiver (as `this.fetchImpl(...)` would if the default were the bare
    // global), so the default must wrap it in a plain call.
    await withReceiverGuardedGlobalFetch(BASE_OVERVIEW, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.load();
      expect(model.isLoadFailed).toBe(false);
      expect(model.overview?.version).toBe("v-one");
    });
  });

  it("loads the overview payload and clears the failure flag", async () => {
    const model = new SettingsModel(
      async () => jsonResponse(BASE_OVERVIEW),
      () => {},
    );

    await model.load();

    expect(model.overview?.version).toBe("v-one");
    expect(model.isLoadFailed).toBe(false);
  });

  it("marks the load failed on a non-OK response", async () => {
    const model = new SettingsModel(
      async () => new Response("nope", { status: 503 }),
      () => {},
    );

    await model.load();

    expect(model.overview).toBeNull();
    expect(model.isLoadFailed).toBe(true);
  });

  it("applies the returned version after a successful error-reporting write", async () => {
    const requests: { url: string; ifMatch: string | null }[] = [];
    const model = new SettingsModel(
      async (input, init) => {
        const url = String(input);
        const headers = new Headers(init?.headers);
        requests.push({ url, ifMatch: headers.get("If-Match") });
        if (url.endsWith("/error-reporting"))
          return jsonResponse({ version: "v-two" });
        return jsonResponse(BASE_OVERVIEW);
      },
      () => {},
    );
    await model.load();

    await model.setReportUnexpectedErrors(false);

    expect(model.overview?.report_unexpected_errors).toBe(false);
    expect(model.overview?.version).toBe("v-two");
    const write = requests.find((request) =>
      request.url.endsWith("/error-reporting"),
    );
    expect(write?.ifMatch).toBe("v-one");
  });

  it("surfaces a refused error-reporting write with the server's reason", async () => {
    const model = new SettingsModel(
      async (input) => {
        const url = String(input);
        if (url.endsWith("/error-reporting"))
          return jsonResponse({ error: "consent has not been recorded" }, 428);
        return jsonResponse(BASE_OVERVIEW);
      },
      () => {},
    );
    await model.load();

    await model.setReportUnexpectedErrors(false);

    expect(model.errorReportingError).toBe("consent has not been recorded");
    // Nothing persisted: the model state stands.
    expect(model.overview?.report_unexpected_errors).toBe(true);
  });

  it("surfaces a network failure of the error-reporting write and clears it on success", async () => {
    let isServerUp = false;
    const model = new SettingsModel(
      async (input) => {
        const url = String(input);
        if (url.endsWith("/error-reporting")) {
          if (!isServerUp) throw new TypeError("Failed to fetch");
          return jsonResponse({ version: "v-two" });
        }
        return jsonResponse(BASE_OVERVIEW);
      },
      () => {},
    );
    await model.load();

    await model.setReportUnexpectedErrors(false);
    expect(model.errorReportingError).toContain("network error");

    isServerUp = true;
    await model.setReportUnexpectedErrors(false);
    expect(model.errorReportingError).toBe("");
    expect(model.overview?.report_unexpected_errors).toBe(false);
  });

  it("rebases on a 412 conflict by reloading instead of clobbering", async () => {
    let overviewValue = { ...BASE_OVERVIEW };
    const model = new SettingsModel(
      async (input) => {
        const url = String(input);
        if (url.endsWith("/error-reporting"))
          return jsonResponse({ error: "stale" }, 412);
        return jsonResponse(overviewValue);
      },
      () => {},
    );
    await model.load();
    // Another window flipped the flag; the server now serves the newer state.
    overviewValue = {
      ...BASE_OVERVIEW,
      report_unexpected_errors: false,
      version: "v-newer",
    };

    await model.setReportUnexpectedErrors(false);

    expect(model.overview?.version).toBe("v-newer");
    expect(model.overview?.report_unexpected_errors).toBe(false);
  });

  it("keeps the revoke dialog open with an error message when the revoke fails", async () => {
    const model = new SettingsModel(
      async (input) => {
        const url = String(input);
        if (url === "/settings/permissions/revoke")
          return new Response("boom", { status: 502 });
        return jsonResponse(BASE_OVERVIEW);
      },
      () => {},
    );
    await model.load();
    model.openRevoke({
      title: "Revoke?",
      body: "b",
      confirmLabel: "Revoke",
      url: "/settings/permissions/revoke",
      payload: {},
    });

    await model.confirmRevoke();

    expect(model.pendingRevoke).not.toBeNull();
    expect(model.revokeError).toContain("502");
  });

  it("closes the dialog and reloads after a successful revoke", async () => {
    let loadCount = 0;
    const model = new SettingsModel(
      async (input) => {
        const url = String(input);
        if (url === "/settings/permissions/revoke")
          return jsonResponse({ status: "ok" });
        loadCount += 1;
        return jsonResponse(BASE_OVERVIEW);
      },
      () => {},
    );
    await model.load();
    model.openRevoke({
      title: "Revoke?",
      body: "b",
      confirmLabel: "Revoke",
      url: "/settings/permissions/revoke",
      payload: {},
    });

    await model.confirmRevoke();

    expect(model.pendingRevoke).toBeNull();
    expect(loadCount).toBe(2);
  });

  it("surfaces a mismatch error without posting when master passwords differ", async () => {
    let postCount = 0;
    const model = new SettingsModel(
      async () => {
        postCount += 1;
        return jsonResponse({});
      },
      () => {},
    );

    await model.changeMasterPassword("aaa", "bbb");

    expect(model.masterPasswordError).toContain("do not match");
    expect(postCount).toBe(0);
  });
});
