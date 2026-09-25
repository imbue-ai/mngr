import { afterEach, describe, expect, it, vi } from "vitest";
import { settle, shareModelOptions } from "../testing";
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
  formatMachineSize,
  formatPendingMachineSize,
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
  const model = new ShareModel(
    shareModelOptions({
      ownerEmail: OWNER,
      fetchJson: stub.fetchJson,
      ...overrides,
    }),
  );
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
    expect(requests[0].url).toBe(
      "/api/v1/workspace-sharing/agent-" + "b".repeat(32),
    );
  });

  it("falls back to the legacy host id when no workspace id is known", async () => {
    const { model, requests } = makeShareModel(() => ({
      ok: true,
      status: 200,
      body: sharingResponse(),
    }));
    await model.load();
    expect(requests[0].url).toBe(
      "/api/v1/workspace-sharing/host-" + "a".repeat(32),
    );
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
    // The stub answers the resolve lookup with a body carrying no user_id,
    // which stages the address as an invite.
    await model.addEntry("friend@example.com");
    await model.addEntry("example.org");

    const doc = model.buildGrantsDocument({ system_interface: true });

    expect(doc.workspace.users).toEqual([]);
    expect(doc.workspace.emails).toEqual([OWNER, "friend@example.com"]);
    expect(doc.workspace.email_domains).toEqual(["example.org"]);
  });

  it("stages a resolved address as a user-id grant and keeps its identity for display", async () => {
    const { model, requests } = makeShareModel((url) => {
      if (url === "/ui/api/users/resolve")
        return {
          ok: true,
          status: 200,
          body: {
            user_id: "user-2",
            email: "bob@example.com",
            display_name: "Bob",
            profile_picture_url: null,
          },
        };
      return { ok: true, status: 200, body: sharingResponse() };
    });
    await model.load();

    await model.addEntry("Bob@Example.com");

    const resolve = requests.find(
      (request) => request.url === "/ui/api/users/resolve",
    );
    expect(resolve?.method).toBe("POST");
    expect(resolve?.body).toEqual({ email: "Bob@Example.com" });
    expect(model.targetState("system_interface").entries).toEqual([
      { kind: "user", userId: "user-2" },
    ]);
    expect(model.identityFor("user-2")?.display_name).toBe("Bob");
    const doc = model.buildGrantsDocument({ system_interface: true });
    expect(doc.workspace.users).toEqual(["user-2"]);
    expect(doc.workspace.emails).toEqual([OWNER]);

    model.removeEntry({ kind: "user", userId: "user-2" });
    expect(model.targetState("system_interface").entries).toEqual([]);
  });

  it("stages an entry under the target it was typed into even if the target changes mid-lookup", async () => {
    const { model } = makeShareModel((url) => {
      if (url === "/ui/api/users/resolve")
        return {
          ok: true,
          status: 200,
          body: { user_id: "user-2", email: "bob@example.com" },
        };
      return { ok: true, status: 200, body: sharingResponse() };
    });
    await model.load();

    const adding = model.addEntry("bob@example.com");
    model.selectTarget("web");
    await adding;

    expect(model.targetState("system_interface").entries).toEqual([
      { kind: "user", userId: "user-2" },
    ]);
    expect(model.targetState("web").entries).toEqual([]);
  });

  it("reads user-id grants and their identities back from the sharing document", async () => {
    const { model } = makeShareModel(() => ({
      ok: true,
      status: 200,
      body: sharingResponse({
        enabled: true,
        url: "https://m.relay.example/",
        grants: {
          workspace: { users: ["user-9"], emails: [OWNER], email_domains: [] },
          services: {},
        },
        identities: {
          "user-9": {
            user_id: "user-9",
            email: "nine@example.com",
            display_name: null,
            profile_picture_url: null,
          },
        },
      }),
    }));
    await model.load();

    expect(model.targetState("system_interface").entries).toEqual([
      { kind: "user", userId: "user-9" },
    ]);
    expect(model.identityFor("user-9")?.email).toBe("nine@example.com");
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
    expect(doc.services["web"]?.users).toEqual([]);
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
    await model.addEntry("guest@example.com");
    // Staging while off must not write anything.
    expect(putBodies).toHaveLength(0);

    await model.enable("");

    expect(putBodies).toHaveLength(1);
    expect(putBodies[0].services["web"]).toEqual({
      users: [],
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
  it("builds label-prefixed origins and no link at all for a target without a label", async () => {
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
    // Only <label>.<domain> origins route on a share: a bare service name
    // would be a link that can never work, so none is offered.
    expect(model.targetUrl("docs")).toBe("");
    expect(model.targetUrl("system_interface")).toBe(
      "https://shell-r4nd.machine.relay.example/",
    );
  });

  it("never falls back to the bare machine domain when the shell label is unknown", async () => {
    // The options snapshot was taken before the workspace's registrations
    // reached the backend (right after app start): the whole-machine target
    // has no label, and the unrouted bare domain must not stand in for it.
    const { model } = makeShareModel(
      () => ({
        ok: true,
        status: 200,
        body: sharingResponse({
          enabled: true,
          url: "https://machine.relay.example/",
          grants: {
            workspace: { emails: [OWNER], email_domains: [] },
            services: {},
          },
        }),
      }),
      { serviceLabels: {} },
    );
    await model.load();

    expect(model.targetUrl("system_interface")).toBe("");
    expect(model.isLabelKnown("system_interface")).toBe(false);
    expect(model.isAwaitingLabel("system_interface")).toBe(true);
  });

  it("adopts the labels carried by the sharing document", async () => {
    const { model } = makeShareModel(
      () => ({
        ok: true,
        status: 200,
        body: sharingResponse({
          enabled: true,
          url: "https://machine.relay.example/",
          grants: {
            workspace: { emails: [OWNER], email_domains: [] },
            services: {},
          },
          service_labels: { system_interface: "shell-l4te", web: "web-l4te" },
        }),
      }),
      { serviceLabels: {} },
    );
    await model.load();

    expect(model.targetUrl("system_interface")).toBe(
      "https://shell-l4te.machine.relay.example/",
    );
    expect(model.targetUrl("web")).toBe(
      "https://web-l4te.machine.relay.example/",
    );
    expect(model.isAwaitingLabel("system_interface")).toBe(false);
  });

  it("keeps polling an already-live share until its label arrives, then shows the link", async () => {
    const scheduled: (() => void)[] = [];
    let probeCount = 0;
    const { model } = makeShareModel(
      (url) => {
        if (url.endsWith("/readiness")) {
          probeCount += 1;
          // The first poll still lacks the label; the second carries it.
          return {
            ok: true,
            status: 200,
            body:
              probeCount === 1
                ? { ready: false, service_labels: {} }
                : {
                    ready: true,
                    service_labels: { system_interface: "shell-l4te" },
                  },
          };
        }
        return {
          ok: true,
          status: 200,
          body: sharingResponse({
            enabled: true,
            url: "https://machine.relay.example/",
            grants: {
              workspace: { emails: [OWNER], email_domains: [] },
              services: {},
            },
          }),
        };
      },
      {
        serviceLabels: {},
        setTimer: (callback: () => void) => {
          scheduled.push(callback);
          return scheduled.length;
        },
      },
    );
    await model.load();

    // Already published, so assumed live -- but with no label there is no link
    // to show yet, and the readiness poll is what will deliver it.
    expect(model.isLive).toBe(true);
    expect(model.targetUrl("system_interface")).toBe("");
    expect(scheduled).toHaveLength(1);

    scheduled.shift()?.();
    await settle();
    expect(model.targetUrl("system_interface")).toBe("");
    expect(scheduled).toHaveLength(1);

    scheduled.shift()?.();
    await settle();
    expect(model.targetUrl("system_interface")).toBe(
      "https://shell-l4te.machine.relay.example/",
    );
    expect(model.isAwaitingLabel("system_interface")).toBe(false);
    // The link is known and live: nothing left to poll for.
    expect(scheduled).toHaveLength(0);
  });

  it("keeps polling after the shell answers until the on-screen app's own label arrives", async () => {
    const scheduled: (() => void)[] = [];
    let probeCount = 0;
    const { model } = makeShareModel(
      (url, init) => {
        if (url.endsWith("/readiness")) {
          probeCount += 1;
          return {
            ok: true,
            status: 200,
            body:
              probeCount === 1
                ? {
                    ready: true,
                    service_labels: { system_interface: "shell-r4nd" },
                  }
                : {
                    ready: true,
                    service_labels: {
                      system_interface: "shell-r4nd",
                      docs: "docs-l4te",
                    },
                  },
          };
        }
        if (init?.method === "PUT") {
          const body = JSON.parse(init.body as string) as SharingGrantsDocument;
          return {
            ok: true,
            status: 200,
            body: sharingResponse({
              enabled: true,
              url: "https://machine.relay.example/",
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
    model.selectTarget("docs");
    await model.enable("");
    expect(model.targetUrl("docs")).toBe("");

    // Probe 1: the shell is live end to end, but docs still has no label.
    scheduled.shift()?.();
    await settle();
    expect(model.isLive).toBe(true);
    expect(model.targetUrl("docs")).toBe("");
    expect(scheduled).toHaveLength(1);

    // Probe 2: the docs label lands; the link appears and polling stops.
    scheduled.shift()?.();
    await settle();
    expect(model.targetUrl("docs")).toBe(
      "https://docs-l4te.machine.relay.example/",
    );
    expect(scheduled).toHaveLength(0);
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
            account_display_name: "Owner Person",
            account_profile_picture_url: "https://pictures.example/owner.png",
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
    expect(model.share?.ownerEmail).toBe(OWNER);
    expect(model.share?.ownerDisplayName).toBe("Owner Person");
    expect(model.share?.ownerProfilePictureUrl).toBe(
      "https://pictures.example/owner.png",
    );
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

  it("reverts a color pick to the saved color when the save is refused", async () => {
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

    const saved = model.pickColor("#123456");
    expect(model.pendingColor).toBe("#123456");
    await saved;

    expect(model.pendingColor).toBeNull();
    expect(model.lastSavedColor).toBe("#0b292b");
    expect(model.colorErrorMessage).toContain("hex value is not valid");
  });

  describe("color picks during a slow save", () => {
    type Reply = { ok: boolean; status: number; body: unknown };

    function makeHeldColorModel(): {
      model: WorkspaceOptionsModel;
      sentColors: string[];
      reply: (response: Reply) => Promise<void>;
    } {
      const sentColors: string[] = [];
      const heldReplies: ((response: Reply) => void)[] = [];
      const model = new WorkspaceOptionsModel("agent-" + "f".repeat(32), {
        fetchJson: (_url, init) => {
          sentColors.push(
            (JSON.parse(String(init?.body)) as { color: string }).color,
          );
          return new Promise((resolve) => heldReplies.push(resolve));
        },
        redraw: () => undefined,
      });
      model.lastSavedColor = "#0b292b";
      const reply = async (response: Reply): Promise<void> => {
        const resolveOldest = heldReplies.shift();
        if (resolveOldest === undefined) throw new Error("no save in flight");
        resolveOldest(response);
        await settle();
      };
      return { model, sentColors, reply };
    }

    it("sends every pick at once and settles on the latest pick's answer", async () => {
      const { model, sentColors, reply } = makeHeldColorModel();

      void model.pickColor("#111111");
      void model.pickColor("#222222");
      void model.pickColor("#333333");

      expect(sentColors).toEqual(["#111111", "#222222", "#333333"]);
      expect(model.pendingColor).toBe("#333333");

      await reply({ ok: true, status: 200, body: {} });
      await reply({ ok: true, status: 200, body: {} });
      expect(model.pendingColor).toBe("#333333");

      await reply({ ok: true, status: 200, body: {} });
      expect(model.lastSavedColor).toBe("#333333");
      expect(model.pendingColor).toBeNull();
      expect(model.colorErrorMessage).toBe("");
    });

    it("does not report a superseded pick's failure", async () => {
      const { model, sentColors, reply } = makeHeldColorModel();

      void model.pickColor("#111111");
      void model.pickColor("#222222");
      await reply({
        ok: false,
        status: 502,
        body: { error: "host_unreachable" },
      });

      expect(sentColors).toEqual(["#111111", "#222222"]);
      expect(model.colorErrorMessage).toBe("");
      expect(model.pendingColor).toBe("#222222");

      await reply({ ok: true, status: 200, body: {} });
      expect(model.lastSavedColor).toBe("#222222");
      expect(model.colorErrorMessage).toBe("");
    });

    it("sends nothing when the pick is the already saved color", async () => {
      const { model, sentColors } = makeHeldColorModel();

      await model.pickColor("#0b292b");

      expect(sentColors).toEqual([]);
      expect(model.pendingColor).toBeNull();
      expect(model.lastSavedColor).toBe("#0b292b");
    });

    it("sends the saved color back when a pick returns to it while another is saving", async () => {
      const { model, sentColors, reply } = makeHeldColorModel();

      void model.pickColor("#111111");
      void model.pickColor("#0b292b");
      await reply({ ok: true, status: 200, body: {} });
      await reply({ ok: true, status: 200, body: {} });

      expect(sentColors).toEqual(["#111111", "#0b292b"]);
      expect(model.lastSavedColor).toBe("#0b292b");
      expect(model.pendingColor).toBeNull();
    });
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

describe("machine size formatting", () => {
  const baseSize = {
    is_available: true,
    memory_units: 8,
    target_memory_units: null,
    disk_gb: 28,
    target_disk_gb: null,
    is_restart_needed_to_apply: false,
  };

  it("renders the current size from units and disk", () => {
    expect(formatMachineSize(baseSize)).toBe("8 GB RAM · 28 GB disk");
  });

  it("omits the factors it does not know", () => {
    expect(formatMachineSize({ ...baseSize, disk_gb: null })).toBe("8 GB RAM");
    expect(
      formatMachineSize({ ...baseSize, memory_units: null, disk_gb: null }),
    ).toBe("");
  });

  it("renders nothing pending when no restart is needed", () => {
    expect(formatPendingMachineSize(baseSize)).toBe("");
  });

  it("falls back to the current value for the factor without a pending target", () => {
    const pending = {
      ...baseSize,
      target_memory_units: 16,
      is_restart_needed_to_apply: true,
    };
    expect(formatPendingMachineSize(pending)).toBe("16 GB RAM · 28 GB disk");
  });
});

describe("WorkspaceOptionsModel machine size load", () => {
  it("stores an available size and leaves an unavailable one hidden", async () => {
    const availableModel = new WorkspaceOptionsModel("agent-1", {
      fetchJson: async () => ({
        ok: true,
        status: 200,
        body: {
          is_available: true,
          memory_units: 16,
          target_memory_units: null,
          disk_gb: 56,
          target_disk_gb: null,
          is_restart_needed_to_apply: false,
        },
      }),
      redraw: () => undefined,
    });
    await availableModel.loadMachineSize();
    expect(availableModel.machineSize?.memory_units).toBe(16);

    const unavailableModel = new WorkspaceOptionsModel("agent-2", {
      fetchJson: async () => ({
        ok: true,
        status: 200,
        body: { is_available: false },
      }),
      redraw: () => undefined,
    });
    await unavailableModel.loadMachineSize();
    expect(unavailableModel.machineSize).toBe(null);
  });
});
