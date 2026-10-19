import { afterEach, describe, expect, it, vi } from "vitest";
import { settle } from "../testing";
import {
  WorkspaceOptionsModel,
  colorErrorMessageFor,
  defaultFetchJson,
  errorMessageFromBody,
  formatPendingMachineSize,
  machineSizeChangeBody,
  machineSizeDraftError,
  resizableMachineSizeDimensions,
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
    expect(
      machineSizeChangeBody(docker, {
        cpu_count: "4",
        memory_gb: "16",
        disk_gb: "",
      }),
    ).toEqual({
      memory_gb: 16,
    });
    // The disk is typed but a docker machine has none to grow.
    expect(
      machineSizeChangeBody(docker, {
        cpu_count: "4",
        memory_gb: "8",
        disk_gb: "200",
      }),
    ).toBeNull();
    const lima = { ...baseSize, is_resizable: true, is_disk_resizable: true };
    expect(
      machineSizeChangeBody(lima, {
        cpu_count: "6",
        memory_gb: "8",
        disk_gb: "200",
      }),
    ).toEqual({
      cpu_count: 6,
      disk_gb: 200,
    });
  });

  it("refuses a resize that is not whole numbers of at least 1", () => {
    const lima = { ...baseSize, is_resizable: true, is_disk_resizable: true };
    expect(
      machineSizeChangeBody(lima, {
        cpu_count: "0",
        memory_gb: "8",
        disk_gb: "28",
      }),
    ).toBeNull();
    expect(
      machineSizeChangeBody(lima, {
        cpu_count: "4",
        memory_gb: "8.5",
        disk_gb: "28",
      }),
    ).toBeNull();
    expect(
      machineSizeChangeBody(lima, {
        cpu_count: "four",
        memory_gb: "8",
        disk_gb: "28",
      }),
    ).toBeNull();
  });
});

describe("WorkspaceOptionsModel machine size load", () => {
  it("stores an available size, and says why an unavailable one is missing", async () => {
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
    expect(availableModel.machineSizeState).toBe("loading");
    await availableModel.loadMachineSize();
    expect(availableModel.machineSize?.memory_gb).toBe(16);
    expect(availableModel.machineSizeState).toBe("ready");
    expect(availableModel.isResizeOfferLoading).toBe(false);

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
    expect(unavailableModel.machineSizeState).toBe("unavailable");

    // A read that fails outright is told apart from a machine that reports no size.
    const unreadModel = new WorkspaceOptionsModel("agent-3", {
      fetchJson: async () => ({
        ok: false,
        status: 0,
        body: { error: "Could not reach the app server." },
      }),
      redraw: () => undefined,
    });
    await unreadModel.loadMachineSize();
    expect(unreadModel.machineSize).toBe(null);
    expect(unreadModel.machineSizeState).toBe("load_failed");

    // So is a size the server answers it could not read (the connector did
    // not answer for an Imbue Cloud machine, which always has one).
    const unreadByServerModel = new WorkspaceOptionsModel("agent-4", {
      fetchJson: async () => ({
        ok: true,
        status: 200,
        body: { is_available: false, is_read_failed: true },
      }),
      redraw: () => undefined,
    });
    await unreadByServerModel.loadMachineSize();
    expect(unreadByServerModel.machineSize).toBe(null);
    expect(unreadByServerModel.machineSizeState).toBe("load_failed");
  });

  const sizeOnly: WorkspaceMachineSizeData = {
    is_available: true,
    cpu_count: null,
    memory_gb: 8,
    disk_gb: 28,
    is_resizable: false,
    is_disk_resizable: false,
    target_memory_gb: null,
    target_disk_gb: null,
    is_restart_needed_to_apply: false,
    is_running: true,
    is_resize_offer_pending: true,
  };

  it("reads the size first, then the offer the server left pending", async () => {
    const offered: WorkspaceMachineSizeData = {
      ...sizeOnly,
      is_resize_offer_pending: false,
      is_resizable: true,
      is_disk_resizable: true,
      memory_choices_gb: [8, 16],
    };
    const seen: Array<{ url: string; offerLoading: boolean }> = [];
    const model = new WorkspaceOptionsModel("agent-1", {
      fetchJson: async (url) => {
        seen.push({ url, offerLoading: model.isResizeOfferLoading });
        return {
          ok: true,
          status: 200,
          body: url.includes("include_resize_offer=1") ? offered : sizeOnly,
        };
      },
      redraw: () => undefined,
    });

    await model.loadMachineSize();

    expect(seen.map((entry) => entry.url)).toEqual([
      "/ui/api/workspaces/agent-1/machine-size",
      "/ui/api/workspaces/agent-1/machine-size?include_resize_offer=1",
    ]);
    // The size is held, and the controls read as loading, while the offer is read.
    expect(seen[1]?.offerLoading).toBe(true);
    expect(model.isResizeOfferLoading).toBe(false);
    expect(model.machineSize?.is_resizable).toBe(true);
    expect(model.machineSize?.memory_choices_gb).toEqual([8, 16]);
  });

  it("keeps the size read-only when the offer cannot be read", async () => {
    const model = new WorkspaceOptionsModel("agent-1", {
      fetchJson: async (url) =>
        url.includes("include_resize_offer=1")
          ? { ok: false, status: 502, body: { error: "connector down" } }
          : { ok: true, status: 200, body: sizeOnly },
      redraw: () => undefined,
    });

    await model.loadMachineSize();

    expect(model.machineSizeState).toBe("ready");
    expect(model.isResizeOfferLoading).toBe(false);
    expect(model.machineSize?.memory_gb).toBe(8);
    expect(model.machineSize?.is_resizable).toBe(false);
    expect(model.machineSize?.is_resize_offer_pending).toBe(false);
  });

  it("drops a superseded read's offer, and its loading state, when the newer read fails", async () => {
    const offered: WorkspaceMachineSizeData = {
      ...sizeOnly,
      is_resize_offer_pending: false,
      is_resizable: true,
    };
    let answerOffer: () => void = () => undefined;
    let isSizeReadFailing = false;
    const model = new WorkspaceOptionsModel("agent-1", {
      fetchJson: (url) =>
        new Promise((resolve) => {
          if (url.includes("include_resize_offer=1")) {
            answerOffer = () =>
              resolve({ ok: true, status: 200, body: offered });
          } else {
            resolve(
              isSizeReadFailing
                ? { ok: false, status: 502, body: { error: "connector down" } }
                : { ok: true, status: 200, body: sizeOnly },
            );
          }
        }),
      redraw: () => undefined,
    });

    // The first read holds the size and is reading the offer when a second
    // read starts (as a reload of the page's data does) and fails outright.
    const first = model.loadMachineSize();
    await settle();
    expect(model.isResizeOfferLoading).toBe(true);
    isSizeReadFailing = true;
    await model.loadMachineSize();
    expect(model.isResizeOfferLoading).toBe(false);

    answerOffer();
    await first;

    expect(model.isResizeOfferLoading).toBe(false);
    // The size the first read held stays, read-only: its offer fetch was
    // abandoned, so nothing is checking the offer any more.
    expect(model.machineSize?.memory_gb).toBe(8);
    expect(model.machineSize?.is_resizable).toBe(false);
    expect(model.machineSize?.is_resize_offer_pending).toBe(false);
    expect(model.machineSizeState).toBe("ready");
  });
});

type FetchResult = { ok: boolean; status: number; body: unknown };

/** A model holding `size` (none while it is still to be read), whose requests
 * are recorded and answered by `respond`, and whose re-reads of a restarting
 * machine are collected in `scheduled` for the test to fire, rather than timed. */
function machineSizeModel(
  size: WorkspaceMachineSizeData | null,
  respond: (
    url: string,
    init?: RequestInit,
  ) => FetchResult | Promise<FetchResult>,
): {
  model: WorkspaceOptionsModel;
  requests: Array<{ url: string; body: unknown }>;
  scheduled: Array<() => void>;
  cancelledRefreshCount: () => number;
} {
  const requests: Array<{ url: string; body: unknown }> = [];
  const scheduled: Array<() => void> = [];
  let cancelledRefreshCount = 0;
  const model = new WorkspaceOptionsModel("agent-1", {
    fetchJson: async (url, init) => {
      requests.push({
        url,
        body: init?.body === undefined ? null : JSON.parse(String(init.body)),
      });
      return respond(url, init);
    },
    redraw: () => undefined,
    scheduleRefresh: (refresh) => {
      scheduled.push(refresh);
      return () => {
        cancelledRefreshCount += 1;
      };
    },
  });
  if (size !== null) model.machineSize = size;
  return {
    model,
    requests,
    scheduled,
    cancelledRefreshCount: () => cancelledRefreshCount,
  };
}

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

  it("posts the changed dimensions, reports the restart, and re-reads the size", async () => {
    const { model, requests } = machineSizeModel(resizable, (url) =>
      url.endsWith("/machine-size") && url.startsWith("/api/v1/")
        ? {
            ok: true,
            status: 200,
            body: { is_restart_dispatched: true, operation_id: "agent-1" },
          }
        : { ok: true, status: 200, body: { ...resizable, memory_gb: 16 } },
    );

    const isRecorded = await model.resizeMachine({
      cpu_count: "4",
      memory_gb: "16",
      disk_gb: "",
    });

    expect(isRecorded).toBe(true);
    expect(requests[0]).toEqual({
      url: "/api/v1/workspaces/agent-1/machine-size",
      body: { memory_gb: 16 },
    });
    expect(requests[1]?.url).toBe("/ui/api/workspaces/agent-1/machine-size");
    // The restart is the section's one status, not a second notice.
    expect(model.resizeNotice).toBe("");
    expect(model.isRestartDispatched).toBe(true);
    expect(model.isRestartInProgress).toBe(true);
    expect(model.machineSize?.memory_gb).toBe(16);
    expect(model.resizeErrorMessage).toBe("");
  });

  it("says the size waits for the next start when the machine was stopped", async () => {
    const { model } = machineSizeModel(resizable, (url) =>
      url.startsWith("/api/v1/")
        ? {
            ok: true,
            status: 200,
            body: { is_restart_dispatched: false, operation_id: null },
          }
        : { ok: true, status: 200, body: resizable },
    );

    await model.resizeMachine({ cpu_count: "2", memory_gb: "8", disk_gb: "" });

    expect(model.resizeNotice).toContain("next starts");
  });

  it("keeps the provider's refusal as the error and sends nothing for an unchanged size", async () => {
    const { model, requests } = machineSizeModel(resizable, () => ({
      ok: false,
      status: 502,
      body: {
        error:
          "The machine's provider refused the new size: docker has only 4 CPUs",
      },
    }));

    expect(
      await model.resizeMachine({
        cpu_count: "4",
        memory_gb: "8",
        disk_gb: "",
      }),
    ).toBe(false);
    expect(requests).toEqual([]);
    expect(model.resizeErrorMessage).toContain("whole numbers");

    expect(
      await model.resizeMachine({
        cpu_count: "64",
        memory_gb: "8",
        disk_gb: "",
      }),
    ).toBe(false);
    expect(model.resizeErrorMessage).toContain("docker has only 4 CPUs");
  });
});

describe("an Imbue Cloud machine sized from the plan's choices", () => {
  const cloud: WorkspaceMachineSizeData = {
    is_available: true,
    cpu_count: null,
    memory_gb: 8,
    disk_gb: 28,
    is_resizable: true,
    is_disk_resizable: true,
    target_memory_gb: null,
    target_disk_gb: null,
    is_restart_needed_to_apply: false,
    is_running: true,
    is_transitioning: false,
    memory_choices_gb: [8, 16, 24, 32],
    is_memory_capped_by_plan: true,
    plan_memory_limit_gb: 40,
    other_machines_memory_gb: 8,
    max_disk_gb: 200,
    plan_disk_limit_gb: 280,
    other_machines_disk_gb: 28,
  };
  /** The machine with 16 GB of RAM saved, waiting for the restart that applies it. */
  const pendingCloud: WorkspaceMachineSizeData = {
    ...cloud,
    target_memory_gb: 16,
    is_restart_needed_to_apply: true,
  };
  /** The pending size as read while the app restarts the machine to apply it. */
  const restartingCloud: WorkspaceMachineSizeData = {
    ...pendingCloud,
    is_restart_in_progress: true,
  };
  /** The restarting machine's size alone, its offer left to the second read. */
  const restartingSizeOnly: WorkspaceMachineSizeData = {
    ...restartingCloud,
    is_resizable: false,
    is_disk_resizable: false,
    memory_choices_gb: [],
    is_resize_offer_pending: true,
  };

  it("offers RAM and disk but no CPUs, and asks only for what changed", () => {
    expect(resizableMachineSizeDimensions(cloud)).toEqual([
      "memory_gb",
      "disk_gb",
    ]);
    expect(
      machineSizeChangeBody(cloud, {
        cpu_count: "4",
        memory_gb: "16",
        disk_gb: "28",
      }),
    ).toEqual({
      memory_gb: 16,
    });
    expect(
      machineSizeChangeBody(cloud, {
        cpu_count: "",
        memory_gb: "8",
        disk_gb: "56",
      }),
    ).toEqual({
      disk_gb: 56,
    });
    expect(
      machineSizeChangeBody(cloud, {
        cpu_count: "",
        memory_gb: "8",
        disk_gb: "28",
      }),
    ).toBeNull();
  });

  it("refuses a RAM size off the plan's list and a disk outside its grow-only range", () => {
    expect(
      machineSizeDraftError(cloud, {
        cpu_count: "",
        memory_gb: "16",
        disk_gb: "56",
      }),
    ).toBeNull();
    expect(
      machineSizeDraftError(cloud, {
        cpu_count: "",
        memory_gb: "40",
        disk_gb: "28",
      }),
    ).toContain("up to 32 GB");
    expect(
      machineSizeDraftError(cloud, {
        cpu_count: "",
        memory_gb: "8",
        disk_gb: "20",
      }),
    ).toContain("at least 28 GB");
    expect(
      machineSizeDraftError(cloud, {
        cpu_count: "",
        memory_gb: "8",
        disk_gb: "201",
      }),
    ).toContain("up to 200 GB");
    // A machine sized by free numbers leaves the judging to its provider.
    const docker = {
      ...cloud,
      memory_choices_gb: [],
      is_disk_resizable: false,
    };
    expect(
      machineSizeDraftError(docker, {
        cpu_count: "64",
        memory_gb: "1000",
        disk_gb: "",
      }),
    ).toBeNull();
  });

  it("saves the size for the next restart and re-reads what was recorded", async () => {
    const { model, requests } = machineSizeModel(cloud, (url) =>
      url.startsWith("/api/v1/")
        ? {
            ok: true,
            status: 200,
            body: { is_restart_dispatched: false, operation_id: null },
          }
        : { ok: true, status: 200, body: pendingCloud },
    );

    expect(
      await model.resizeMachine({
        cpu_count: "",
        memory_gb: "16",
        disk_gb: "28",
      }),
    ).toBe(true);

    expect(requests[0]).toEqual({
      url: "/api/v1/workspaces/agent-1/machine-size",
      body: { memory_gb: 16 },
    });
    // The pending size the re-read carries is what says a restart applies it.
    expect(model.resizeNotice).toBe("");
    expect(model.isRestartDispatched).toBe(false);
    expect(model.machineSize?.target_memory_gb).toBe(16);
  });

  it("leaves a stopped machine's saved size to the pending notice too", async () => {
    const stopped = { ...cloud, is_running: false };
    const { model } = machineSizeModel(stopped, (url) =>
      url.startsWith("/api/v1/")
        ? {
            ok: true,
            status: 200,
            body: { is_restart_dispatched: false, operation_id: null },
          }
        : { ok: true, status: 200, body: stopped },
    );

    expect(
      await model.resizeMachine({
        cpu_count: "",
        memory_gb: "16",
        disk_gb: "28",
      }),
    ).toBe(true);

    expect(model.resizeNotice).toBe("");
  });

  it("explains a size off the list without sending anything", async () => {
    const { model, requests } = machineSizeModel(cloud, () => ({
      ok: true,
      status: 200,
      body: {},
    }));

    expect(
      await model.resizeMachine({
        cpu_count: "",
        memory_gb: "40",
        disk_gb: "28",
      }),
    ).toBe(false);

    expect(requests).toEqual([]);
    expect(model.resizeErrorMessage).toContain("up to 32 GB");
  });

  it("keeps the quota refusal as the error", async () => {
    const { model } = machineSizeModel(cloud, () => ({
      ok: false,
      status: 409,
      body: {
        error:
          "Your plan cannot hold this size right now: active machine units limit is 16",
      },
    }));

    expect(
      await model.resizeMachine({
        cpu_count: "",
        memory_gb: "32",
        disk_gb: "28",
      }),
    ).toBe(false);
    expect(model.resizeErrorMessage).toContain(
      "active machine units limit is 16",
    );
  });

  it("restarts the machine through the host restart, reports it, and re-reads the size", async () => {
    const stopping = { ...cloud, is_running: false, is_transitioning: true };
    const { model, requests } = machineSizeModel(cloud, (url) =>
      url.endsWith("/restart")
        ? { ok: true, status: 202, body: { operation_id: "agent-1" } }
        : { ok: true, status: 200, body: stopping },
    );

    expect(await model.restartMachineToApplySize()).toBe(true);

    expect(requests.map((request) => request.url)).toEqual([
      "/api/v1/workspaces/agent-1/restart",
      "/ui/api/workspaces/agent-1/machine-size",
    ]);
    expect(requests[0]?.body).toEqual({ scope: "host" });
    expect(model.resizeNotice).toBe("");
    expect(model.isRestartDispatched).toBe(true);
    expect(model.isRestartInProgress).toBe(true);
    expect(model.isRestartBusy).toBe(false);
    expect(model.machineSize?.is_transitioning).toBe(true);

    const refused = machineSizeModel(cloud, () => ({
      ok: false,
      status: 409,
      body: { error: "A backup restore is running." },
    }));
    expect(await refused.model.restartMachineToApplySize()).toBe(false);
    expect(refused.model.resizeErrorMessage).toBe(
      "A backup restore is running.",
    );
  });

  it("keeps the restart in progress, re-reading until the app reports it over", async () => {
    // The connector keeps the machine running with its size pending until the
    // stop lands, which is exactly what would offer the restart a second time;
    // the app's own report of the running recovery is what the page follows.
    let isAppRestarting = true;
    const { model, requests, scheduled } = machineSizeModel(
      pendingCloud,
      (url) =>
        url.endsWith("/restart")
          ? { ok: true, status: 202, body: { operation_id: "agent-1" } }
          : {
              ok: true,
              status: 200,
              body: {
                ...pendingCloud,
                is_restart_in_progress: isAppRestarting,
              },
            },
    );

    expect(await model.restartMachineToApplySize()).toBe(true);
    expect(model.isRestartInProgress).toBe(true);
    expect(scheduled).toHaveLength(1);

    // The scheduled re-read still finds the restart running: stays held, re-armed once.
    scheduled[0]?.();
    await settle();
    expect(model.isRestartInProgress).toBe(true);
    expect(scheduled).toHaveLength(2);

    // Once the app reports the recovery over, the page lets go of its own memory of the dispatch.
    isAppRestarting = false;
    scheduled[1]?.();
    await settle();
    expect(model.isRestartDispatched).toBe(false);
    expect(model.isRestartInProgress).toBe(false);
    expect(scheduled).toHaveLength(2);
    expect(
      requests.filter((request) => request.url.includes("/ui/api/")),
    ).toHaveLength(3);
  });

  it("keeps re-reading through a read that fails while the restart runs", async () => {
    let sizeResponse: FetchResult = {
      ok: false,
      status: 502,
      body: { error: "connector down" },
    };
    const { model, scheduled } = machineSizeModel(pendingCloud, (url) =>
      url.endsWith("/restart")
        ? { ok: true, status: 202, body: { operation_id: "agent-1" } }
        : sizeResponse,
    );

    // The re-read right after the dispatch fails: the page keeps what it knew
    // and the restart stays in progress, with the next re-read armed.
    expect(await model.restartMachineToApplySize()).toBe(true);
    expect(model.isRestartInProgress).toBe(true);
    expect(model.machineSize).toBe(pendingCloud);
    expect(scheduled).toHaveLength(1);

    scheduled[0]?.();
    await settle();
    expect(model.isRestartInProgress).toBe(true);
    expect(scheduled).toHaveLength(2);

    sizeResponse = {
      ok: true,
      status: 200,
      body: { ...pendingCloud, is_restart_in_progress: false },
    };
    scheduled[1]?.();
    await settle();
    expect(model.isRestartInProgress).toBe(false);
    expect(scheduled).toHaveLength(2);
  });

  it("lets go of a restart the app reports over even when the size cannot be shown", async () => {
    // The size was read mid-restart, so the held size reports the restart too.
    let isAppRestarting = true;
    const { model, scheduled } = machineSizeModel(restartingCloud, (url) =>
      url.endsWith("/restart")
        ? { ok: true, status: 202, body: { operation_id: "agent-1" } }
        : {
            ok: true,
            status: 200,
            body: {
              is_available: false,
              is_restart_in_progress: isAppRestarting,
            },
          },
    );

    // The connector has nothing to show for the machine while it restarts.
    expect(await model.restartMachineToApplySize()).toBe(true);
    expect(model.isRestartInProgress).toBe(true);
    expect(model.machineSize).toBe(restartingCloud);
    expect(scheduled).toHaveLength(1);

    // The app reports the restart over before the size can be shown again
    // (the account was signed out, say): the page stops following it rather
    // than re-reading for as long as it stays open.
    isAppRestarting = false;
    scheduled[0]?.();
    await settle();
    expect(model.isRestartDispatched).toBe(false);
    expect(model.machineSize?.is_restart_in_progress).toBe(false);
    expect(model.isRestartInProgress).toBe(false);
    expect(model.machineSize?.target_memory_gb).toBe(16);
    expect(scheduled).toHaveLength(1);
  });

  it("arms the re-read of a restarting machine only once its offer read has answered", async () => {
    let scheduledWhenOfferRead: number | null = null;
    const { model, scheduled } = machineSizeModel(null, (url) => {
      if (url.includes("include_resize_offer=1")) {
        scheduledWhenOfferRead = scheduled.length;
        return { ok: true, status: 200, body: restartingCloud };
      }
      return { ok: true, status: 200, body: restartingSizeOnly };
    });

    await model.loadMachineSize();

    // Had the re-read been armed between the two fetches, it could fire during
    // the slow offer read and supersede it, so the offer would never land.
    expect(scheduledWhenOfferRead).toBe(0);
    expect(scheduled).toHaveLength(1);
    expect(model.isRestartInProgress).toBe(true);
    expect(model.machineSize?.is_resizable).toBe(true);
  });

  it("lets go of a restart the offer read reports over when it cannot show the size", async () => {
    const { model, scheduled } = machineSizeModel(null, (url) =>
      url.includes("include_resize_offer=1")
        ? {
            ok: true,
            status: 200,
            body: { is_available: false, is_restart_in_progress: false },
          }
        : { ok: true, status: 200, body: restartingSizeOnly },
    );

    await model.loadMachineSize();

    // The size read saw the restart running; the offer read, answered once it
    // was over, could not show the size any more but still reports that, so
    // the page is not left re-reading to find it out.
    expect(model.machineSize?.memory_gb).toBe(8);
    expect(model.machineSize?.is_resize_offer_pending).toBe(false);
    expect(model.machineSize?.is_restart_in_progress).toBe(false);
    expect(model.isRestartInProgress).toBe(false);
    expect(scheduled).toHaveLength(0);
  });

  it("stops following the restart once the page is left, even with a read in flight", async () => {
    let answerRead: () => void = () => undefined;
    const { model, scheduled, cancelledRefreshCount } = machineSizeModel(
      restartingCloud,
      (url) =>
        new Promise<FetchResult>((resolve) => {
          answerRead = () =>
            resolve(
              url.endsWith("/restart")
                ? { ok: true, status: 202, body: { operation_id: "agent-1" } }
                : { ok: true, status: 200, body: restartingCloud },
            );
        }),
    );

    // The dispatch is answered, then the re-read that follows it.
    const restarted = model.restartMachineToApplySize();
    answerRead();
    await settle();
    answerRead();
    expect(await restarted).toBe(true);
    expect(scheduled).toHaveLength(1);

    // The armed re-read fires and its fetch is still in flight when the page is left.
    scheduled[0]?.();
    await settle();
    model.dispose();
    answerRead();
    await settle();

    // The timer had already fired, so there was nothing to cancel; the answer
    // that lands on a left page arms no further re-read.
    expect(cancelledRefreshCount()).toBe(0);
    expect(scheduled).toHaveLength(1);
  });
});
