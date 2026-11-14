// @vitest-environment jsdom
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearAppContextForTests,
  registerAppContext,
} from "../../../app-context";
import { createEmptyStores } from "../../../models/boot";
import {
  BackupOperationController,
  type EventSourceLike,
  type LifecycleDeps,
} from "../../../models/backups";
import {
  buttonLabelled,
  buttonLabels,
  jsonResponse,
  mountInBody,
  primaryButtonLabels,
  settle,
} from "../../../testing";
import { ShellState } from "../../shell/shell-state";
import { OperationStrip, serviceConfigHelpModel } from "./OperationStrip";

interface RecordingDeps extends LifecycleDeps {
  posts: string[];
  statuses: unknown[];
  logSources: EventSourceLike[];
}

function recordingDeps(): RecordingDeps {
  const deps: RecordingDeps = {
    posts: [],
    statuses: [],
    logSources: [],
    getJson: async () => deps.statuses.shift() ?? null,
    postJson: async (url: string) => {
      deps.posts.push(url);
      return { status: 202, body: null };
    },
    deleteResource: async () => 200,
    openEventSource: () => {
      const source: EventSourceLike = {
        close: () => {},
        onmessage: null,
        onerror: null,
      };
      deps.logSources.push(source);
      return source;
    },
    schedule: () => {},
    redraw: () => m.redraw(),
  };
  return deps;
}

function mountStrip(controller: BackupOperationController): HTMLElement {
  const serviceConfigHelp = serviceConfigHelpModel();
  return mountInBody(() =>
    m(OperationStrip, { controller, serviceConfigHelp }),
  );
}

beforeEach(() => {
  const stores = createEmptyStores();
  registerAppContext({ stores, shell: new ShellState(stores) });
});

afterEach(() => {
  clearAppContextForTests();
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const UNREADABLE_CONFIG_OUTPUT =
  "ERROR: CANT_REREAD: File contains no section headers.";

function recordRoutes(): string[] {
  const routes: string[] = [];
  vi.spyOn(m.route, "set").mockImplementation(((path: string) => {
    routes.push(path);
  }) as typeof m.route.set);
  return routes;
}

async function finishedRestore(
  status: Record<string, unknown>,
  logLines: string[] = [],
): Promise<BackupOperationController> {
  const deps = recordingDeps();
  const controller = new BackupOperationController("agent-11", deps);
  controller.startRestore(
    { snapshot_id: "snap-1", time: "2026-01-01T00:00:00Z" },
    "",
    {},
  );
  await settle();
  for (const log of logLines) {
    deps.logSources.at(-1)?.onmessage?.({ data: JSON.stringify({ log }) });
  }
  deps.statuses.push({ kind: "backup_restore", ...status });
  await controller.pollOnce();
  return controller;
}

describe("OperationStrip", () => {
  it("shows our sentence for a failed update, keeps its text behind See details, and retries it", async () => {
    const deps = recordingDeps();
    const controller = new BackupOperationController("agent-11", deps);
    controller.startUpdate();
    await settle();
    deps.statuses.push({
      status: "FAILED",
      kind: "backup_update",
      error: "host-backup did not reach RUNNING: FATAL",
    });
    await controller.pollOnce();
    const root = mountStrip(controller);

    expect(root.textContent).toContain(
      "Couldn't update this workspace's backup software.",
    );
    expect(root.textContent).not.toContain("did not reach RUNNING");
    buttonLabelled(root, "See details").click();
    m.redraw.sync();
    expect(root.textContent).toContain(
      "host-backup did not reach RUNNING: FATAL",
    );

    buttonLabelled(root, "Try again").click();
    await settle();
    expect(deps.posts).toEqual([
      "/api/v1/workspaces/agent-11/backup-service/update",
      "/api/v1/workspaces/agent-11/backup-service/update",
    ]);
  });

  it("leads a restore whose safety copy failed with restoring anyway, and Cancel leaves it undone", async () => {
    const controller = await finishedRestore(
      {
        status: "FAILED",
        error: "pre-restore safety snapshot failed: disk full",
        failure_reason: "SAFETY_SNAPSHOT_FAILED",
      },
      ["Saving a safety snapshot..."],
    );
    const root = mountStrip(controller);
    expect(() => buttonLabelled(root, "Show details")).not.toThrow();

    expect(root.textContent).toContain(
      "Imbue Studio couldn't save a copy of this workspace's current state before restoring.",
    );
    expect(primaryButtonLabels(root)).toEqual([
      "Restore without backing up first",
    ]);
    expect(
      buttonLabels(root.querySelector<HTMLElement>("#backup-operation-error")!),
    ).toEqual([
      "See details",
      "Restore without backing up first",
      "Cancel",
      "Report a bug",
    ]);

    buttonLabelled(root, "Cancel").click();
    m.redraw.sync();

    expect(root.textContent).not.toContain("couldn't save a copy");
    expect(() =>
      buttonLabelled(root, "Restore without backing up first"),
    ).toThrow();
    expect(() => buttonLabelled(root, "Show details")).toThrow();
  });

  it("offers a workspace restart when a restore left programs down", async () => {
    const controller = await finishedRestore({
      status: "DONE",
      is_done: true,
      services_down: ["browser"],
    });
    const routes = recordRoutes();
    const root = mountStrip(controller);

    expect(root.textContent).toContain(
      "Your workspace is restored and ready to use.",
    );
    expect(root.textContent).not.toContain("browser");

    buttonLabelled(root, "Restart workspace").click();
    expect(routes).toEqual([
      "/agents/agent-11/recovery?return_to=%252Fworkspace%252Fagent-11&intent=restart",
    ]);
  });

  it("says a restore left the restored program settings unreadable, and asks the agent to fix them", async () => {
    const assistBodies: unknown[] = [];
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
      if (url === "/help/assist")
        assistBodies.push(JSON.parse(String(init?.body)));
      return Promise.resolve(jsonResponse({ ok: true }));
    });
    const controller = await finishedRestore({
      status: "DONE",
      is_done: true,
      service_config_error: UNREADABLE_CONFIG_OUTPUT,
    });
    const routes = recordRoutes();
    const root = mountStrip(controller);
    const notice = root.querySelector<HTMLElement>("#backup-service-config");

    expect(notice?.textContent).toContain(
      "won't start the next time the workspace restarts",
    );
    expect(root.textContent).not.toContain("CANT_REREAD");
    buttonLabelled(notice!, "See details").click();
    m.redraw.sync();
    expect(notice?.textContent).toContain(UNREADABLE_CONFIG_OUTPUT);
    expect(primaryButtonLabels(notice!)).toEqual(["Ask the agent to fix it"]);

    buttonLabelled(notice!, "Ask the agent to fix it").click();
    await vi.waitFor(() => expect(routes).toEqual(["/workspace/agent-11"]));
    expect(assistBodies).toHaveLength(1);
    const assist = assistBodies[0] as {
      workspace_agent_id: string;
      description: string;
    };
    expect(assist.workspace_agent_id).toBe("agent-11");
    expect(assist.description).toContain(UNREADABLE_CONFIG_OUTPUT);
  });

  it("shows a failed agent start under the unreadable-configuration notice's buttons, not among them", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve(jsonResponse({ error: "boom" }, 500)),
    );
    const controller = await finishedRestore({
      status: "DONE",
      is_done: true,
      service_config_error: UNREADABLE_CONFIG_OUTPUT,
    });
    const root = mountStrip(controller);
    const notice = root.querySelector<HTMLElement>("#backup-service-config")!;

    buttonLabelled(notice, "Ask the agent to fix it").click();
    await vi.waitFor(() => {
      m.redraw.sync();
      expect(
        notice.querySelector("#backup-service-config-ask-agent-error"),
      ).not.toBeNull();
    });
    const failure = notice.querySelector<HTMLElement>(
      "#backup-service-config-ask-agent-error",
    )!;
    const row = buttonLabelled(
      notice,
      "Ask the agent to fix it",
    ).parentElement!;

    expect(buttonLabels(row)).toEqual([
      "Ask the agent to fix it",
      "Report a bug",
    ]);
    expect(row.contains(failure)).toBe(false);
    expect(failure.textContent).toContain("Couldn't start an agent");
    expect(
      buttonLabels(notice).filter((label) => label === "Report a bug"),
    ).toHaveLength(1);
  });

  it("draws that failed agent start inside the notice's box rather than in one of its own", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve(jsonResponse({ error: "boom" }, 500)),
    );
    const controller = await finishedRestore({
      status: "DONE",
      is_done: true,
      service_config_error: UNREADABLE_CONFIG_OUTPUT,
    });
    const root = mountStrip(controller);
    const notice = root.querySelector<HTMLElement>("#backup-service-config")!;

    buttonLabelled(notice, "Ask the agent to fix it").click();
    await vi.waitFor(() => {
      m.redraw.sync();
      expect(
        notice.querySelector("#backup-service-config-ask-agent-error"),
      ).not.toBeNull();
    });
    const failure = notice.querySelector<HTMLElement>(
      "#backup-service-config-ask-agent-error",
    )!;

    expect(notice.firstElementChild?.className).toContain(
      "c-important-surface",
    );
    expect(failure.firstElementChild?.className).not.toContain(
      "c-important-surface",
    );
    expect(failure.textContent).toContain("Couldn't start an agent");
  });

  it("withdraws a refused ask from the unreadable-configuration notice, leaving its refusal and Report a bug", async () => {
    const refusal =
      "This workspace doesn't have the agent-assist skill, so an agent can't help here yet.";
    vi.stubGlobal("fetch", () =>
      Promise.resolve(jsonResponse({ error: refusal }, 409)),
    );
    const controller = await finishedRestore({
      status: "DONE",
      is_done: true,
      service_config_error: UNREADABLE_CONFIG_OUTPUT,
    });
    const root = mountStrip(controller);
    const notice = root.querySelector<HTMLElement>("#backup-service-config")!;

    buttonLabelled(notice, "Ask the agent to fix it").click();
    await vi.waitFor(() => {
      m.redraw.sync();
      expect(notice.textContent).toContain(refusal);
    });

    // The notice's own details and Report a bug, then the refusal's details.
    expect(buttonLabels(notice)).toEqual([
      "See details",
      "Report a bug",
      "See details",
    ]);
    expect(primaryButtonLabels(root)).toEqual([]);
  });

  it("leads chats that blocked an update with stopping them, in the error's own row", async () => {
    const deps = recordingDeps();
    const controller = new BackupOperationController("agent-11", deps);
    controller.startUpdate();
    await settle();
    deps.statuses.push({
      status: "FAILED",
      kind: "backup_update",
      blocked_chats: ["chat-1"],
    });
    await controller.pollOnce();
    const root = mountStrip(controller);
    const error = root.querySelector<HTMLElement>("#backup-operation-error")!;

    expect(error.textContent).toContain("Chats are running in this workspace");
    expect(primaryButtonLabels(root)).toEqual(["Stop chats and try again"]);

    buttonLabelled(error, "Stop chats and try again").click();
    await settle();
    expect(deps.posts).toEqual([
      "/api/v1/workspaces/agent-11/backup-service/update",
      "/api/v1/workspaces/agent-11/backup-service/update",
    ]);
  });

  it("offers Update backup software beside a restore's caveat that it finishes, and runs it from there", async () => {
    const warning =
      "The restore succeeded, but Imbue Studio couldn't finish setting up this workspace's backups " +
      'afterwards. Run "Update backup software" to finish.';
    const controller = await finishedRestore({
      status: "DONE",
      is_done: true,
      warning,
      is_update_suggested: true,
    });
    const root = mountStrip(controller);
    const notice = root.querySelector<HTMLElement>(
      "#backup-operation-warning",
    )!;

    expect(notice.textContent).toContain(warning);
    expect(primaryButtonLabels(root)).toEqual(["Update backup software"]);

    buttonLabelled(notice, "Update backup software").click();
    await settle();
    expect(controller.runningLabel).toBe("Updating backup software...");
  });

  it("offers a reload as the main button when a refusal says to reload the page", async () => {
    const deps = recordingDeps();
    deps.postJson = async () => ({
      status: 409,
      body: { error: "not configured", code: "BACKUPS_NOT_CONFIGURED" },
    });
    const controller = new BackupOperationController("agent-11", deps);
    controller.startRestore(
      { snapshot_id: "snap-1", time: "2026-01-01T00:00:00Z" },
      "",
      {},
    );
    await settle();
    const root = mountStrip(controller);

    expect(root.textContent).toContain(
      "Reload the page to see its backup settings.",
    );
    expect(primaryButtonLabels(root)).toEqual(["Reload page"]);
  });

  it("offers no update beside a restore's caveat the update can't finish", async () => {
    const controller = await finishedRestore({
      status: "DONE",
      is_done: true,
      warning:
        "The restore succeeded, but the backup service was left as it is.",
    });
    const root = mountStrip(controller);

    expect(root.textContent).toContain("the backup service was left as it is");
    expect(buttonLabels(root)).not.toContain("Update backup software");
  });

  it("does not offer a restart for programs still down when the restored configuration is unreadable", async () => {
    const controller = await finishedRestore({
      status: "DONE",
      is_done: true,
      services_down: ["browser"],
      service_config_error: UNREADABLE_CONFIG_OUTPUT,
    });
    const root = mountStrip(controller);
    const servicesDown = root.querySelector<HTMLElement>(
      "#backup-services-down",
    );

    expect(servicesDown?.textContent).toContain(
      "A few of its programs haven't restarted yet",
    );
    expect(servicesDown?.textContent).not.toContain("restart the workspace");
    expect(buttonLabels(root)).not.toContain("Restart workspace");
    expect(root.querySelector("#backup-service-config")).not.toBeNull();
  });

  it("shows an unreadable restored configuration beside a failed restore's own sentence", async () => {
    const controller = await finishedRestore({
      status: "FAILED",
      error: "restored, but uv sync failed: resolver error",
      failure_reason: "RESTORE_INCOMPLETE",
      service_config_error: UNREADABLE_CONFIG_OUTPUT,
    });
    const root = mountStrip(controller);

    expect(
      root.querySelector("#backup-operation-error")?.textContent,
    ).toContain("The restore didn't finish.");
    expect(root.querySelector("#backup-service-config")?.textContent).toContain(
      "won't start the next time the workspace restarts",
    );
    expect(root.textContent).not.toContain("CANT_REREAD");
    expect(root.textContent).not.toContain("uv sync failed");
  });

  it("drops the unreadable-configuration notice when the next operation starts", async () => {
    const controller = await finishedRestore({
      status: "DONE",
      is_done: true,
      service_config_error: UNREADABLE_CONFIG_OUTPUT,
    });
    const root = mountStrip(controller);
    expect(root.querySelector("#backup-service-config")).not.toBeNull();

    controller.startUpdate();
    await settle();
    m.redraw.sync();

    expect(root.querySelector("#backup-service-config")).toBeNull();
  });
});
