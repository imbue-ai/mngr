import { afterEach, describe, expect, it, vi } from "vitest";
import { settle } from "../testing";
import {
  WorkspaceOptionsModel,
  colorErrorMessageFor,
  defaultFetchJson,
  errorMessageFromBody,
  formatPendingMachineSize,
  machineSizeChangeBody,
  normalizeWorkspaceColorHex,
} from "./workspaceOptions";
import type { WorkspaceMachineSizeData } from "./workspaceOptions";

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
            is_account_owned_provider: false,
            leased_owner_email: "",
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
            public_email_domains: ["gmail.com"],
          },
        };
      }
      return {
        ok: true,
        status: 200,
        body: {
          enabled: false,
          url: null,
          grants: {
            workspace: { emails: [], email_domains: [] },
            services: {},
          },
        },
      };
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
    expect(model.share?.granterEmail).toBe(OWNER);
    expect(model.share?.wholeService).toBe("system_interface");
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
});

describe("machine size pending change and resize body", () => {
  const baseSize: WorkspaceMachineSizeData = {
    is_available: true,
    cpu_count: 4,
    memory_gb: 8,
    disk_gb: 28,
    is_resizable: false,
    is_disk_resizable: false,
    target_memory_gb: null,
    target_disk_gb: null,
    is_restart_needed_to_apply: false,
  };

  it("renders nothing pending when no restart is needed", () => {
    expect(formatPendingMachineSize(baseSize)).toBe("");
  });

  it("falls back to the current value for the factor without a pending target", () => {
    const pending = {
      ...baseSize,
      target_memory_gb: 16,
      is_restart_needed_to_apply: true,
    };
    expect(formatPendingMachineSize(pending)).toBe("16 GB RAM · 28 GB disk");
  });

  it("asks only for the dimensions that changed, and only those the machine can change", () => {
    const docker = { ...baseSize, is_resizable: true, disk_gb: null };
    expect(machineSizeChangeBody(docker, { cpu_count: "4", memory_gb: "16", disk_gb: "" })).toEqual({
      memory_gb: 16,
    });
    // The disk is typed but a docker machine has none to grow.
    expect(machineSizeChangeBody(docker, { cpu_count: "4", memory_gb: "8", disk_gb: "200" })).toBeNull();
    const lima = { ...baseSize, is_resizable: true, is_disk_resizable: true };
    expect(machineSizeChangeBody(lima, { cpu_count: "6", memory_gb: "8", disk_gb: "200" })).toEqual({
      cpu_count: 6,
      disk_gb: 200,
    });
  });

  it("refuses a resize that is not whole numbers of at least 1", () => {
    const lima = { ...baseSize, is_resizable: true, is_disk_resizable: true };
    expect(machineSizeChangeBody(lima, { cpu_count: "0", memory_gb: "8", disk_gb: "28" })).toBeNull();
    expect(machineSizeChangeBody(lima, { cpu_count: "4", memory_gb: "8.5", disk_gb: "28" })).toBeNull();
    expect(machineSizeChangeBody(lima, { cpu_count: "four", memory_gb: "8", disk_gb: "28" })).toBeNull();
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
          cpu_count: 4,
          memory_gb: 16,
          disk_gb: 56,
          is_resizable: true,
          is_disk_resizable: false,
          target_memory_gb: null,
          target_disk_gb: null,
          is_restart_needed_to_apply: false,
        },
      }),
      redraw: () => undefined,
    });
    await availableModel.loadMachineSize();
    expect(availableModel.machineSize?.memory_gb).toBe(16);

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

describe("WorkspaceOptionsModel machine resize", () => {
  const resizable: WorkspaceMachineSizeData = {
    is_available: true,
    cpu_count: 4,
    memory_gb: 8,
    disk_gb: null,
    is_resizable: true,
    is_disk_resizable: false,
    target_memory_gb: null,
    target_disk_gb: null,
    is_restart_needed_to_apply: false,
  };

  function resizingModel(
    respond: (url: string, init?: RequestInit) => { ok: boolean; status: number; body: unknown },
  ): { model: WorkspaceOptionsModel; requests: Array<{ url: string; body: unknown }> } {
    const requests: Array<{ url: string; body: unknown }> = [];
    const model = new WorkspaceOptionsModel("agent-1", {
      fetchJson: async (url, init) => {
        requests.push({ url, body: init?.body === undefined ? null : JSON.parse(String(init.body)) });
        return respond(url, init);
      },
      redraw: () => undefined,
    });
    model.machineSize = resizable;
    return { model, requests };
  }

  it("posts the changed dimensions, reports the restart, and re-reads the size", async () => {
    const { model, requests } = resizingModel((url) =>
      url.endsWith("/machine-size") && url.startsWith("/api/v1/")
        ? { ok: true, status: 200, body: { is_restart_dispatched: true, operation_id: "agent-1" } }
        : { ok: true, status: 200, body: { ...resizable, memory_gb: 16 } },
    );

    const isRecorded = await model.resizeMachine({ cpu_count: "4", memory_gb: "16", disk_gb: "" });

    expect(isRecorded).toBe(true);
    expect(requests[0]).toEqual({ url: "/api/v1/workspaces/agent-1/machine-size", body: { memory_gb: 16 } });
    expect(requests[1]?.url).toBe("/ui/api/workspaces/agent-1/machine-size");
    expect(model.resizeNotice).toContain("restarting");
    expect(model.machineSize?.memory_gb).toBe(16);
    expect(model.resizeErrorMessage).toBe("");
  });

  it("says the size waits for the next start when the machine was stopped", async () => {
    const { model } = resizingModel((url) =>
      url.startsWith("/api/v1/")
        ? { ok: true, status: 200, body: { is_restart_dispatched: false, operation_id: null } }
        : { ok: true, status: 200, body: resizable },
    );

    await model.resizeMachine({ cpu_count: "2", memory_gb: "8", disk_gb: "" });

    expect(model.resizeNotice).toContain("next starts");
  });

  it("keeps the provider's refusal as the error and sends nothing for an unchanged size", async () => {
    const { model, requests } = resizingModel(() => ({
      ok: false,
      status: 502,
      body: { error: "The machine's provider refused the new size: docker has only 4 CPUs" },
    }));

    expect(await model.resizeMachine({ cpu_count: "4", memory_gb: "8", disk_gb: "" })).toBe(false);
    expect(requests).toEqual([]);
    expect(model.resizeErrorMessage).toContain("whole numbers");

    expect(await model.resizeMachine({ cpu_count: "64", memory_gb: "8", disk_gb: "" })).toBe(false);
    expect(model.resizeErrorMessage).toContain("docker has only 4 CPUs");
  });
});
