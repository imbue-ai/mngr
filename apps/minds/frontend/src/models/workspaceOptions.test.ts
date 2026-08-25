import { afterEach, describe, expect, it, vi } from "vitest";
import { settle } from "../testing";
import type {
  MachineSharingResponse,
  SharingGrantsDocument,
} from "./workspaceOptions";
import {
  ShareModel,
  WorkspaceOptionsModel,
  colorErrorMessageFor,
  defaultFetchJson,
  documentGrantsAnyone,
  errorMessageFromBody,
  normalizeWorkspaceColorHex,
} from "./workspaceOptions";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("defaultFetchJson", () => {
  it("resolves with a status-0 error body on network failure instead of rejecting", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    const result = await defaultFetchJson("/api/v1/anything");

    expect(result.ok).toBe(false);
    expect(result.status).toBe(0);
    expect(errorMessageFromBody(result.body, "fallback")).toBe(
      "Could not reach the app server.",
    );
  });
});

const OWNER = "owner@example.com";

interface RecordedRequest {
  url: string;
  method: string;
  body: unknown;
}

function makeFetchStub(
  responder: (
    url: string,
    init?: RequestInit,
  ) => { ok: boolean; status: number; body: unknown },
): {
  requests: RecordedRequest[];
  fetchJson: (
    url: string,
    init?: RequestInit,
  ) => Promise<ReturnType<typeof responder>>;
} {
  const requests: RecordedRequest[] = [];
  return {
    requests,
    fetchJson: (url: string, init?: RequestInit) => {
      requests.push({
        url,
        method: init?.method ?? "GET",
        body:
          typeof init?.body === "string"
            ? (JSON.parse(init.body) as unknown)
            : null,
      });
      return Promise.resolve(responder(url, init));
    },
  };
}

function makeShareModel(
  responder: (
    url: string,
    init?: RequestInit,
  ) => { ok: boolean; status: number; body: unknown },
  overrides: Partial<ConstructorParameters<typeof ShareModel>[0]> = {},
): { model: ShareModel; requests: RecordedRequest[] } {
  const stub = makeFetchStub(responder);
  const model = new ShareModel({
    hostId: "host-" + "a".repeat(32),
    ownerEmail: OWNER,
    wholeService: "system_interface",
    appServices: ["web", "docs"],
    serviceLabels: { web: "web-r4nd", system_interface: "shell-r4nd" },
    fetchJson: stub.fetchJson,
    redraw: () => undefined,
    setTimer: () => 0,
    clearTimer: () => undefined,
    monotonicNowMs: () => 0,
    ...overrides,
  });
  return { model, requests: stub.requests };
}

function sharingResponse(
  overrides: Partial<MachineSharingResponse> = {},
): MachineSharingResponse {
  return {
    enabled: false,
    url: null,
    grants: { workspace: { emails: [], email_domains: [] }, services: {} },
    ...overrides,
  };
}

describe("ShareModel sharing API coordinate", () => {
  it("keys the sharing API by the workspace id when one is known", async () => {
    const { model, requests } = makeShareModel(
      () => ({ ok: true, status: 200, body: sharingResponse() }),
      { agentId: "agent-" + "b".repeat(32) },
    );
    await model.load();
    expect(requests[0].url).toBe("/api/v1/workspace-sharing/agent-" + "b".repeat(32));
  });

  it("falls back to the legacy host id when no workspace id is known", async () => {
    const { model, requests } = makeShareModel(() => ({
      ok: true,
      status: 200,
      body: sharingResponse(),
    }));
    await model.load();
    expect(requests[0].url).toBe("/api/v1/workspace-sharing/host-" + "a".repeat(32));
  });
});

describe("ShareModel grants document building", () => {
  it("always writes the owner into an enabled scope and splits emails from domains", async () => {
    const { model } = makeShareModel(() => ({
      ok: true,
      status: 200,
      body: sharingResponse(),
    }));
    await model.load();
    model.addEntry("friend@example.com");
    model.addEntry("example.org");

    const doc = model.buildGrantsDocument({ system_interface: true });

    expect(doc.workspace.emails).toEqual([OWNER, "friend@example.com"]);
    expect(doc.workspace.email_domains).toEqual(["example.org"]);
  });

  it("preserves scopes granted outside the pane verbatim through every write", async () => {
    const { model } = makeShareModel(() => ({
      ok: true,
      status: 200,
      body: sharingResponse({
        enabled: true,
        url: "https://m-abc.relay.example/",
        grants: {
          workspace: { emails: [], email_domains: [] },
          services: {
            "phantom-service": {
              emails: ["other@example.com"],
              email_domains: [],
            },
          },
        },
      }),
    }));
    await model.load();

    const doc = model.buildGrantsDocument({ web: true });

    expect(doc.services["phantom-service"]).toEqual({
      emails: ["other@example.com"],
      email_domains: [],
    });
    expect(doc.services["web"]?.emails).toEqual([OWNER]);
  });

  it("keeps staged entries for a disabled target and publishes them on enable", async () => {
    const putBodies: SharingGrantsDocument[] = [];
    const { model } = makeShareModel((url, init) => {
      if (init?.method === "PUT") {
        const body = JSON.parse(init.body as string) as SharingGrantsDocument;
        putBodies.push(body);
        return {
          ok: true,
          status: 200,
          body: sharingResponse({
            enabled: true,
            url: "https://m.relay.example/",
            grants: body,
          }),
        };
      }
      return { ok: true, status: 200, body: sharingResponse() };
    });
    await model.load();
    model.selectTarget("web");
    model.addEntry("guest@example.com");
    // Staging while off must not write anything.
    expect(putBodies).toHaveLength(0);

    await model.enable("");

    expect(putBodies).toHaveLength(1);
    expect(putBodies[0].services["web"]).toEqual({
      emails: [OWNER, "guest@example.com"],
      email_domains: [],
    });
    expect(model.targetState("web").isEnabled).toBe(true);
  });

  it("blocks enable when the add box still holds un-added text", async () => {
    const { model, requests } = makeShareModel(() => ({
      ok: true,
      status: 200,
      body: sharingResponse(),
    }));
    await model.load();
    const writesBefore = requests.filter(
      (request) => request.method !== "GET",
    ).length;

    await model.enable("someone@example.com");

    expect(model.errorMessage).toContain("someone@example.com");
    expect(requests.filter((request) => request.method !== "GET")).toHaveLength(
      writesBefore,
    );
  });
});

describe("ShareModel disable", () => {
  it("DELETEs the share when the last enabled target is turned off", async () => {
    const { model, requests } = makeShareModel((url, init) => {
      if (init?.method === "DELETE")
        return { ok: true, status: 200, body: null };
      return {
        ok: true,
        status: 200,
        body: sharingResponse({
          enabled: true,
          url: "https://m.relay.example/",
          grants: {
            workspace: { emails: [OWNER], email_domains: [] },
            services: {},
          },
        }),
      };
    });
    await model.load();
    expect(model.targetState("system_interface").isEnabled).toBe(true);

    await model.disable();

    expect(requests.some((request) => request.method === "DELETE")).toBe(true);
    expect(model.isMachineEnabled).toBe(false);
    expect(model.targetState("system_interface").isEnabled).toBe(false);
  });

  it("PUTs the remaining document when another target stays enabled", async () => {
    const putBodies: SharingGrantsDocument[] = [];
    const { model, requests } = makeShareModel((url, init) => {
      if (init?.method === "PUT") {
        const body = JSON.parse(init.body as string) as SharingGrantsDocument;
        putBodies.push(body);
        return {
          ok: true,
          status: 200,
          body: sharingResponse({
            enabled: true,
            url: "https://m.relay.example/",
            grants: body,
          }),
        };
      }
      return {
        ok: true,
        status: 200,
        body: sharingResponse({
          enabled: true,
          url: "https://m.relay.example/",
          grants: {
            workspace: { emails: [OWNER], email_domains: [] },
            services: {
              web: { emails: [OWNER, "guest@example.com"], email_domains: [] },
            },
          },
        }),
      };
    });
    await model.load();

    model.selectTarget("web");
    await model.disable();

    expect(requests.some((request) => request.method === "DELETE")).toBe(false);
    expect(putBodies).toHaveLength(1);
    expect(putBodies[0].services["web"]).toBeUndefined();
    expect(putBodies[0].workspace.emails).toEqual([OWNER]);
  });
});

describe("ShareModel load failures", () => {
  it("locks the editor when the status read fails", async () => {
    const { model } = makeShareModel(() => ({
      ok: false,
      status: 502,
      body: { error: "relay down" },
    }));
    await model.load();

    expect(model.status).toBe("load_failed");
    expect(model.isEditorEditable).toBe(false);
    expect(model.errorMessage).toContain("relay down");
    expect(model.isRetryOffered).toBe(true);
  });

  it("treats enabled-with-null-grants as a failed read, not an empty policy", async () => {
    const { model } = makeShareModel(() => ({
      ok: true,
      status: 200,
      body: sharingResponse({
        enabled: true,
        url: "https://m.relay.example/",
        grants: null,
      }),
    }));
    await model.load();

    expect(model.status).toBe("load_failed");
    expect(model.errorMessage).toContain("still has it");
  });
});

describe("ShareModel target urls", () => {
  it("builds label-prefixed origins and falls back for label-less services", async () => {
    const { model } = makeShareModel(() => ({
      ok: true,
      status: 200,
      body: sharingResponse({
        enabled: true,
        url: "https://machine.relay.example/",
      }),
    }));
    await model.load();

    expect(model.targetUrl("web")).toBe(
      "https://web-r4nd.machine.relay.example/",
    );
    expect(model.targetUrl("docs")).toBe("https://docs.machine.relay.example/");
    expect(model.targetUrl("system_interface")).toBe(
      "https://shell-r4nd.machine.relay.example/",
    );
  });

  it("selecting an unknown target falls back to the whole machine", async () => {
    const { model } = makeShareModel(() => ({
      ok: true,
      status: 200,
      body: sharingResponse(),
    }));
    await model.load();

    model.selectTarget("no-such-service");

    expect(model.currentTarget).toBe("system_interface");
  });
});

describe("ShareModel write serialization", () => {
  it("serializes overlapping writes so the later body is built after the earlier write landed", async () => {
    const putBodies: SharingGrantsDocument[] = [];
    // Two enables on different targets ride the same write queue.
    const slowThenFast = makeShareModel((url, init) => {
      if (init?.method === "PUT") {
        const body = JSON.parse(init.body as string) as SharingGrantsDocument;
        putBodies.push(body);
        return {
          ok: true,
          status: 200,
          body: sharingResponse({
            enabled: true,
            url: "https://m.relay.example/",
            grants: body,
          }),
        };
      }
      return { ok: true, status: 200, body: sharingResponse() };
    });
    await slowThenFast.model.load();
    slowThenFast.model.selectTarget("web");
    const firstWrite = slowThenFast.model.enable("");
    slowThenFast.model.selectTarget("docs");
    const secondWrite = slowThenFast.model.enable("");
    await Promise.all([firstWrite, secondWrite]);

    expect(putBodies).toHaveLength(2);
    // The second write's document includes the first write's outcome (web on).
    expect(putBodies[1].services["web"]).toBeDefined();
    expect(putBodies[1].services["docs"]).toBeDefined();
  });
});

describe("ShareModel readiness polling", () => {
  it("polls readiness after a fresh enable and marks live when the probe answers ready", async () => {
    const scheduled: (() => void)[] = [];
    let probeCount = 0;
    const { model } = makeShareModel(
      (url, init) => {
        if (url.endsWith("/readiness")) {
          probeCount += 1;
          return { ok: true, status: 200, body: { ready: probeCount >= 2 } };
        }
        if (init?.method === "PUT") {
          const body = JSON.parse(init.body as string) as SharingGrantsDocument;
          return {
            ok: true,
            status: 200,
            body: sharingResponse({
              enabled: true,
              url: "https://m.relay.example/",
              grants: body,
            }),
          };
        }
        return { ok: true, status: 200, body: sharingResponse() };
      },
      {
        setTimer: (callback: () => void) => {
          scheduled.push(callback);
          return scheduled.length;
        },
      },
    );
    await model.load();
    await model.enable("");
    expect(model.isLive).toBe(false);
    expect(model.isAwaitingLink("system_interface")).toBe(true);

    // First probe: not ready -> reschedules. Second: ready -> live.
    while (scheduled.length > 0 && !model.isLive) {
      const next = scheduled.shift();
      if (next) next();
      await settle();
    }

    expect(model.isLive).toBe(true);
    expect(model.isAwaitingLink("system_interface")).toBe(false);
  });

  it("derives provisioning steps from cert issuance and a changed tunnel-login stamp", async () => {
    const scheduled: (() => void)[] = [];
    let probeCount = 0;
    // A re-share: the stale tunnel stamp from the previous share must not
    // count as "tunnel connected" -- only a CHANGED stamp does.
    const readinessBodies = [
      {
        ready: false,
        cert_not_after: null,
        last_tunnel_login_at: "2026-01-01 00:00:00",
      },
      {
        ready: false,
        cert_not_after: "2027-01-01",
        last_tunnel_login_at: "2026-01-01 00:00:00",
      },
      {
        ready: false,
        cert_not_after: "2027-01-01",
        last_tunnel_login_at: "2026-08-13 12:00:00",
      },
      {
        ready: true,
        cert_not_after: "2027-01-01",
        last_tunnel_login_at: "2026-08-13 12:00:00",
      },
    ];
    const { model } = makeShareModel(
      (url, init) => {
        if (url.endsWith("/readiness")) {
          const body =
            readinessBodies[Math.min(probeCount, readinessBodies.length - 1)];
          probeCount += 1;
          return { ok: true, status: 200, body };
        }
        if (init?.method === "PUT") {
          const body = JSON.parse(init.body as string) as SharingGrantsDocument;
          return {
            ok: true,
            status: 200,
            body: sharingResponse({
              enabled: true,
              url: "https://m.relay.example/",
              grants: body,
            }),
          };
        }
        return { ok: true, status: 200, body: sharingResponse() };
      },
      {
        setTimer: (callback: () => void) => {
          scheduled.push(callback);
          return scheduled.length;
        },
      },
    );
    await model.load();
    await model.enable("");
    expect(model.isCertIssued).toBe(false);
    expect(model.isTunnelConnected).toBe(false);

    const runNextProbe = async () => {
      const next = scheduled.shift();
      if (next) next();
      await settle();
    };

    // Probe 1: stale stamp is only snapshotted; nothing is done yet.
    await runNextProbe();
    expect(model.isCertIssued).toBe(false);
    expect(model.isTunnelConnected).toBe(false);
    // Probe 2: the certificate has been issued.
    await runNextProbe();
    expect(model.isCertIssued).toBe(true);
    expect(model.isTunnelConnected).toBe(false);
    // Probe 3: the stamp changed -- the tunnel reconnected under the new token.
    await runNextProbe();
    expect(model.isTunnelConnected).toBe(true);
    expect(model.isLive).toBe(false);
    // Probe 4: end-to-end ready.
    await runNextProbe();
    expect(model.isLive).toBe(true);
  });

  it("keeps the tunnel-stamp snapshot across a mid-wait target switch", async () => {
    const scheduled: (() => void)[] = [];
    let probeCount = 0;
    // The step signals are machine-level: switching the on-screen target away
    // and back mid-provisioning must not re-baseline the tunnel snapshot, or
    // a reconnect spanning the switch would go undetected.
    const readinessBodies = [
      {
        ready: false,
        cert_not_after: null,
        last_tunnel_login_at: "2026-01-01 00:00:00",
      },
      {
        ready: false,
        cert_not_after: "2027-01-01",
        last_tunnel_login_at: "2026-08-14 09:00:00",
      },
    ];
    const { model } = makeShareModel(
      (url, init) => {
        if (url.endsWith("/readiness")) {
          const body =
            readinessBodies[Math.min(probeCount, readinessBodies.length - 1)];
          probeCount += 1;
          return { ok: true, status: 200, body };
        }
        if (init?.method === "PUT") {
          const body = JSON.parse(init.body as string) as SharingGrantsDocument;
          return {
            ok: true,
            status: 200,
            body: sharingResponse({
              enabled: true,
              url: "https://m.relay.example/",
              grants: body,
            }),
          };
        }
        return { ok: true, status: 200, body: sharingResponse() };
      },
      {
        setTimer: (callback: () => void) => {
          scheduled.push(callback);
          return scheduled.length;
        },
      },
    );
    await model.load();
    await model.enable("");

    // Probe 1 snapshots the stale stamp.
    scheduled.shift()?.();
    await settle();
    expect(model.isTunnelConnected).toBe(false);

    // Switch away (polling stops: the other target is not enabled) and back
    // (polling restarts). The snapshot must survive the round trip.
    model.selectTarget("web");
    model.selectTarget("system_interface");

    // The next probe sees the changed stamp: still detected as a reconnect.
    scheduled.shift()?.();
    await settle();
    expect(model.isTunnelConnected).toBe(true);
    expect(model.isCertIssued).toBe(true);
  });

  it("assumes an already-published share is live (no provisioning wait on load)", async () => {
    const { model } = makeShareModel(() => ({
      ok: true,
      status: 200,
      body: sharingResponse({
        enabled: true,
        url: "https://m.relay.example/",
        grants: {
          workspace: { emails: [OWNER], email_domains: [] },
          services: {},
        },
      }),
    }));
    await model.load();

    expect(model.isLive).toBe(true);
    expect(model.isAwaitingLink("system_interface")).toBe(false);
  });
});

describe("WorkspaceOptionsModel", () => {
  it("loads options data and reports load failures", async () => {
    const stub = makeFetchStub((url) => {
      if (url.includes("/ui/api/workspaces/")) {
        return {
          ok: true,
          status: 200,
          body: {
            agent_id: "agent-" + "b".repeat(32),
            host_id: "host-" + "b".repeat(32),
            name: "sunny",
            color: "#0b292b",
            palette: { confusion: "#0b292b" },
            is_stale: false,
            is_leased_imbue_cloud: false,
            has_account: true,
            account_email: OWNER,
            current_account: {
              user_id: "u1",
              email: OWNER,
              display_name: null,
            },
            accounts: [],
            app_services: [],
            service_labels: {},
            whole_service: "system_interface",
          },
        };
      }
      return { ok: true, status: 200, body: sharingResponse() };
    });
    const model = new WorkspaceOptionsModel("agent-" + "b".repeat(32), {
      fetchJson: stub.fetchJson,
      redraw: () => undefined,
      shareOverrides: {
        setTimer: () => 0,
        clearTimer: () => undefined,
        monotonicNowMs: () => 0,
      },
    });

    await model.load();

    expect(model.status).toBe("ready");
    expect(model.data?.name).toBe("sunny");
    expect(model.share).not.toBeNull();
  });

  it("rename requires a non-empty name and surfaces server errors", async () => {
    const stub = makeFetchStub(() => ({
      ok: false,
      status: 409,
      body: { error: "name taken" },
    }));
    const model = new WorkspaceOptionsModel("agent-" + "c".repeat(32), {
      fetchJson: stub.fetchJson,
      redraw: () => undefined,
    });

    expect(await model.rename("   ")).toBe(false);
    expect(model.renameErrorMessage).toContain("required");

    expect(await model.rename("new-name")).toBe(false);
    expect(model.renameErrorMessage).toBe("name taken");
  });

  it("reverts the color preview when the save is refused", async () => {
    const painted: string[] = [];
    const stub = makeFetchStub(() => ({
      ok: false,
      status: 422,
      body: { error: "invalid_hex" },
    }));
    const model = new WorkspaceOptionsModel("agent-" + "d".repeat(32), {
      fetchJson: stub.fetchJson,
      redraw: () => undefined,
    });
    model.lastSavedColor = "#0b292b";

    const isSaved = await model.saveColor("#123456", (hex) =>
      painted.push(hex),
    );

    expect(isSaved).toBe(false);
    expect(painted).toEqual(["#123456", "#0b292b"]);
    expect(model.colorErrorMessage).toContain("hex value is not valid");
  });
});

describe("pure helpers", () => {
  it("normalizes short and long hex forms and rejects garbage", () => {
    expect(normalizeWorkspaceColorHex(" #ABC ")).toBe("#aabbcc");
    expect(normalizeWorkspaceColorHex("abc")).toBe("#aabbcc");
    expect(normalizeWorkspaceColorHex("#a1b2c3")).toBe("#a1b2c3");
    expect(normalizeWorkspaceColorHex("a1b2c3")).toBe("#a1b2c3");
    expect(normalizeWorkspaceColorHex("#a1b2c3ff")).toBeNull();
    expect(normalizeWorkspaceColorHex("nope")).toBeNull();
  });

  it("maps color error codes to their messages", () => {
    expect(colorErrorMessageFor(422, { error: "stale_provider" })).toContain(
      "unreachable",
    );
    expect(colorErrorMessageFor(500, {})).toBe("Save failed (HTTP 500).");
  });

  it("documentGrantsAnyone sees workspace and service scopes", () => {
    expect(
      documentGrantsAnyone({
        workspace: { emails: [], email_domains: [] },
        services: {},
      }),
    ).toBe(false);
    expect(
      documentGrantsAnyone({
        workspace: { emails: [], email_domains: [] },
        services: { web: { emails: ["a@b.c"], email_domains: [] } },
      }),
    ).toBe(true);
  });
});
