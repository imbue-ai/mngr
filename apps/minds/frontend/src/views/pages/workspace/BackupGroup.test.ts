// @vitest-environment jsdom
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearAppContextForTests,
  registerAppContext,
} from "../../../app-context";
import { createEmptyStores } from "../../../models/boot";
import {
  buttonLabelled,
  buttonLabels,
  InertEventSource,
  jsonResponse,
  mountInBody,
  primaryButtonLabels,
  settle,
} from "../../../testing";
import { ShellState } from "../../shell/shell-state";
import { BackupGroup } from "./BackupGroup";

const AGENT_ID = "agent-11";

interface BackupRoutes {
  /** The service check's verdict; null for a check route that fails. */
  check: Record<string, unknown> | null;
  listing?: Record<string, unknown>;
  verificationStatus?: number;
  updateStatus?: number;
}

interface RecordedRequests {
  posts: string[];
  postBodies: unknown[];
  gets: string[];
}

function stubBackupRoutes(routes: BackupRoutes): RecordedRequests {
  const requests: RecordedRequests = { posts: [], postBodies: [], gets: [] };
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      requests.posts.push(url);
      requests.postBodies.push(JSON.parse(String(init.body)));
      if (url.endsWith("/backup-service/verification")) {
        return Promise.resolve(
          jsonResponse({ error: "boom" }, routes.verificationStatus ?? 200),
        );
      }
      if (url.endsWith("/backup-service/update")) {
        const status = routes.updateStatus ?? 202;
        return Promise.resolve(
          jsonResponse(status === 202 ? {} : { error: "boom" }, status),
        );
      }
      return Promise.resolve(jsonResponse({}, 202));
    }
    requests.gets.push(url);
    if (url.includes("/backup-check"))
      return Promise.resolve(
        routes.check === null
          ? jsonResponse({ error: "boom" }, 500)
          : jsonResponse(routes.check),
      );
    if (url.includes(`/workspaces/${AGENT_ID}/backups`)) {
      return Promise.resolve(
        jsonResponse(
          routes.listing ?? {
            is_configured: true,
            snapshots: [
              { snapshot_id: "snap-1", time: "2026-01-01T00:00:00Z" },
            ],
            snapshots_total: 1,
          },
        ),
      );
    }
    return Promise.resolve(jsonResponse({}, 404));
  });
  return requests;
}

async function mountGroup(): Promise<HTMLElement> {
  const root = mountInBody(() => m(BackupGroup, { agentId: AGENT_ID }));
  await settle();
  m.redraw.sync();
  return root;
}

beforeEach(() => {
  const stores = createEmptyStores();
  registerAppContext({ stores, shell: new ShellState(stores) });
  vi.stubGlobal("EventSource", InertEventSource);
});

afterEach(() => {
  clearAppContextForTests();
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("BackupGroup", () => {
  it("offers Update backup software in the problems' row, and starts the update from there", async () => {
    const { posts } = stubBackupRoutes({
      check: {
        check_state: "PROBLEMS",
        problems: ["SERVICE_NOT_RUNNING"],
        check_detail: "host-backup: FATAL",
      },
    });
    const root = await mountGroup();
    const problems = root.querySelector<HTMLElement>("#backup-problems")!;

    expect(problems.textContent).toContain(
      "The backup software in this workspace is not running.",
    );
    expect(root.querySelectorAll("#backup-update-btn")).toHaveLength(1);
    expect(buttonLabels(problems)).toEqual([
      "See details",
      "Update backup software",
    ]);
    expect(primaryButtonLabels(root)).toEqual(["Update backup software"]);

    buttonLabelled(problems, "Update backup software").click();
    await settle();

    expect(posts).toEqual([
      `/api/v1/workspaces/${AGENT_ID}/backup-service/update`,
    ]);
  });

  it("keeps Update backup software out of the problems' row when the only problem is backups being off", async () => {
    stubBackupRoutes({
      check: { check_state: "PROBLEMS", problems: ["NOT_CONFIGURED"] },
    });
    const root = await mountGroup();
    const problems = root.querySelector<HTMLElement>("#backup-problems")!;

    expect(problems.textContent).toContain(
      "Backups are turned off for this workspace.",
    );
    expect(problems.querySelectorAll("button")).toHaveLength(0);
    expect(root.querySelector("#backup-update-btn")).not.toBeNull();
  });

  it("says a failed verification change with its response behind See details and a bug report", async () => {
    const { posts } = stubBackupRoutes({
      check: { check_state: "OK" },
      verificationStatus: 500,
    });
    const root = await mountGroup();

    buttonLabelled(root, "Disable").click();
    await settle();
    m.redraw.sync();
    const notice = root.querySelector<HTMLElement>(
      "#backup-verification-error",
    )!;

    expect(notice.textContent).toContain(
      "Could not change the verification setting. Try again.",
    );
    buttonLabelled(notice, "See details").click();
    m.redraw.sync();
    expect(notice.textContent).toContain("HTTP 500");
    expect(buttonLabelled(notice, "Report a bug")).toBeDefined();
    expect(posts).toEqual([
      `/api/v1/workspaces/${AGENT_ID}/backup-service/verification`,
    ]);
  });

  it("retries a failed verification change as its main button, sending the same change again", async () => {
    const requests = stubBackupRoutes({
      check: { check_state: "OK", is_verification_enabled: true },
      verificationStatus: 500,
    });
    const root = await mountGroup();

    buttonLabelled(root, "Disable").click();
    await settle();
    m.redraw.sync();
    const notice = root.querySelector<HTMLElement>(
      "#backup-verification-error",
    )!;

    expect(primaryButtonLabels(root)).toEqual(["Try again"]);
    expect(buttonLabels(notice).indexOf("Try again")).toBeLessThan(
      buttonLabels(notice).indexOf("Report a bug"),
    );

    buttonLabelled(notice, "Try again").click();
    await settle();

    expect(requests.postBodies).toEqual([
      { enabled: false },
      { enabled: false },
    ]);
  });

  it("gives an unreadable backup listing one notice and one retry, which reads the missing check again too", async () => {
    const requests = stubBackupRoutes({
      check: null,
      listing: {
        is_configured: true,
        snapshots: [],
        snapshots_error: "restic snapshots failed (exit 1)",
      },
    });
    const root = await mountGroup();

    expect(root.textContent).toContain(
      "Imbue Studio couldn't check this workspace's backups.",
    );
    expect(root.querySelector("#backup-history-error")).toBeNull();
    expect(root.querySelector("#backup-check-missing")).toBeNull();
    const retries = buttonLabels(root).filter(
      (label) => label === "Check again" || label === "Try again",
    );
    expect(retries).toEqual(["Check again"]);
    expect(primaryButtonLabels(root)).toEqual(["Check again"]);

    requests.gets.length = 0;
    buttonLabelled(root, "Check again").click();
    await settle();

    expect(requests.gets).toEqual([
      `/api/v1/workspaces/${AGENT_ID}/backups?limit=5`,
      `/api/v1/workspaces/${AGENT_ID}/backup-check`,
    ]);
  });

  it("keeps a missing check verdict's retry secondary while the history's own error leads", async () => {
    stubBackupRoutes({
      check: null,
      listing: {
        is_configured: true,
        is_backing_up: true,
        snapshots: [],
        snapshots_error: "restic snapshots failed (exit 1)",
      },
    });
    const root = await mountGroup();

    expect(root.querySelector("#backup-status-error")).toBeNull();
    expect(root.querySelector("#backup-history-error")).not.toBeNull();
    expect(root.querySelector("#backup-check-missing")).not.toBeNull();
    expect(primaryButtonLabels(root)).toEqual(["Try again"]);
  });

  it("keeps a missing check verdict's retry secondary while a failed backup task leads", async () => {
    stubBackupRoutes({ check: null, updateStatus: 500 });
    const root = await mountGroup();
    expect(primaryButtonLabels(root)).toEqual(["Check again"]);

    buttonLabelled(root, "Update backup software").click();
    await settle();
    m.redraw.sync();

    expect(root.querySelector("#backup-check-missing")).not.toBeNull();
    expect(
      root.querySelector("#backup-operation-error")?.textContent,
    ).toContain("Couldn't update this workspace's backup software.");
    expect(primaryButtonLabels(root)).toEqual(["Try again"]);
  });

  it("keeps a missing check verdict's retry secondary while a failed verification change leads", async () => {
    stubBackupRoutes({ check: null, verificationStatus: 500 });
    const root = await mountGroup();

    buttonLabelled(root, "Disable").click();
    await settle();
    m.redraw.sync();

    expect(root.querySelector("#backup-check-missing")).not.toBeNull();
    expect(root.querySelector("#backup-verification-error")).not.toBeNull();
    expect(primaryButtonLabels(root)).toEqual(["Try again"]);
  });
});
