import { describe, expect, it } from "vitest";
import type { ClientPermissionAnswer } from "../electron-bridge";
import type {
  ClientPermissionDecision,
  UiClientPermissions,
} from "../generated/ui";
import { settle } from "../testing";
import type { RequestResult } from "./errors";
import {
  CLIENT_PERMISSION_CHOICES,
  ClientPermissionPromptModel,
  ClientPermissionsModel,
  clientPermissionChoiceLabel,
  selectedChoiceOf,
  storedValueForChoice,
} from "./clientPermissions";
import {
  clientPermissionAsk,
  clientPermissionsView,
  microphoneRow,
} from "./clientPermissions.testing";

const AGENT_ID = "agent-" + "c".repeat(8);
const VIEW_URL = `/ui/api/workspaces/${AGENT_ID}/client-permissions`;
const MICROPHONE_URL = `${VIEW_URL}/microphone`;

/** One line per thing that happened, in order: a request as "<METHOD> <url>
 * <body>", an answer to main as "answer <json>". The prompt's contract is about
 * the order of the two. */
type EventLog = string[];

function recordingFetch(
  log: EventLog,
  respond: (url: string, init?: RequestInit) => RequestResult,
): (url: string, init?: RequestInit) => Promise<RequestResult> {
  return (url, init) => {
    log.push(
      `${init?.method ?? "GET"} ${url}${typeof init?.body === "string" ? ` ${init.body}` : ""}`,
    );
    return Promise.resolve(respond(url, init));
  };
}

/** The view the backend answers a write with: the row now holds what was
 * written, or defers to the default when null was written. */
function viewAfterWrite(
  decision: ClientPermissionDecision | null,
): UiClientPermissions {
  return clientPermissionsView([
    decision === null
      ? microphoneRow()
      : microphoneRow({ decision, source: "workspace", stored: decision }),
  ]);
}

function backend(
  log: EventLog,
): (url: string, init?: RequestInit) => Promise<RequestResult> {
  return recordingFetch(log, (url, init) => {
    if (init?.method === "PUT") {
      const { decision } = JSON.parse(String(init.body)) as {
        decision: ClientPermissionDecision | null;
      };
      return { ok: true, status: 200, body: viewAfterWrite(decision) };
    }
    return { ok: true, status: 200, body: clientPermissionsView() };
  });
}

describe("ClientPermissionsModel", () => {
  it("loads the workspace's view from its own endpoint", async () => {
    const log: EventLog = [];
    const model = new ClientPermissionsModel(AGENT_ID, {
      fetchJson: backend(log),
      redraw: () => undefined,
    });
    expect(model.status).toBe("idle");

    await model.load();

    expect(log).toEqual([`GET ${VIEW_URL}`]);
    expect(model.status).toBe("ready");
    expect(model.data).toEqual(clientPermissionsView());
  });

  it("reads once however often the tab mounts", async () => {
    const log: EventLog = [];
    const model = new ClientPermissionsModel(AGENT_ID, {
      fetchJson: backend(log),
      redraw: () => undefined,
    });
    model.ensureLoaded();
    await settle();
    model.ensureLoaded();
    await settle();
    expect(log).toEqual([`GET ${VIEW_URL}`]);
  });

  it("says why a read failed", async () => {
    const model = new ClientPermissionsModel(AGENT_ID, {
      fetchJson: () =>
        Promise.resolve({
          ok: false,
          status: 500,
          body: { error: "store is unreadable" },
        }),
      redraw: () => undefined,
    });
    await model.load();
    expect(model.status).toBe("load_failed");
    expect(model.errorMessage).toBe("store is unreadable");
  });

  it.each([
    ["default", null],
    ["ask", "ask"],
    ["granted", "granted"],
    ["denied", "denied"],
  ] as const)(
    "stores %s as %j and adopts the answered view",
    async (choice, decision) => {
      const log: EventLog = [];
      const model = new ClientPermissionsModel(AGENT_ID, {
        fetchJson: backend(log),
        redraw: () => undefined,
      });
      await model.load();

      const isStored = await model.setDecision(
        "microphone",
        storedValueForChoice(choice),
      );

      expect(isStored).toBe(true);
      expect(log.at(-1)).toBe(
        `PUT ${MICROPHONE_URL} ${JSON.stringify({ decision })}`,
      );
      expect(model.data).toEqual(viewAfterWrite(decision));
      expect(selectedChoiceOf(model.data!.rows[0])).toBe(choice);
      expect(model.isWriting()).toBe(false);
    },
  );

  it("locks while a write is in flight and drops a second one", async () => {
    const log: EventLog = [];
    let release: (response: RequestResult) => void = () => undefined;
    const model = new ClientPermissionsModel(AGENT_ID, {
      fetchJson: (url, init) => {
        log.push(`${init?.method ?? "GET"} ${url}`);
        return new Promise((resolve) => {
          release = resolve;
        });
      },
      redraw: () => undefined,
    });

    const first = model.setDecision("microphone", "granted");
    expect(model.isWriting()).toBe(true);
    expect(model.isRowBusy("microphone")).toBe(true);
    expect(await model.setDecision("microphone", "denied")).toBe(false);

    release({ ok: true, status: 200, body: viewAfterWrite("granted") });
    expect(await first).toBe(true);
    expect(model.isWriting()).toBe(false);
    expect(log).toEqual([`PUT ${MICROPHONE_URL}`]);
  });

  it("keeps the last good view and says why when a write is refused", async () => {
    const log: EventLog = [];
    let isRefusing = false;
    const model = new ClientPermissionsModel(AGENT_ID, {
      fetchJson: recordingFetch(log, () =>
        isRefusing
          ? {
              ok: false,
              status: 409,
              body: { error: "saved by a newer version" },
            }
          : { ok: true, status: 200, body: clientPermissionsView() },
      ),
      redraw: () => undefined,
    });
    await model.load();
    isRefusing = true;

    expect(await model.setDecision("microphone", "granted")).toBe(false);

    expect(model.data).toEqual(clientPermissionsView());
    expect(model.status).toBe("ready");
    expect(model.errorMessage).toBe(
      "Could not save the change: saved by a newer version",
    );
    expect(model.isWriting()).toBe(false);
  });
});

describe("client permission choices", () => {
  it("offers the four states in order, naming what the default decides", () => {
    const row = microphoneRow();
    expect(
      CLIENT_PERMISSION_CHOICES.map((choice) =>
        clientPermissionChoiceLabel(choice, row),
      ),
    ).toEqual(["Use the default (Ask)", "Ask", "Allow", "Don't allow"]);
  });

  it("selects Use the default only while the workspace stores nothing", () => {
    expect(selectedChoiceOf(microphoneRow())).toBe("default");
    expect(
      selectedChoiceOf(microphoneRow({ stored: "ask", source: "workspace" })),
    ).toBe("ask");
    expect(
      selectedChoiceOf(
        microphoneRow({
          stored: "granted",
          decision: "granted",
          source: "workspace",
        }),
      ),
    ).toBe("granted");
    expect(
      selectedChoiceOf(
        microphoneRow({
          stored: "denied",
          decision: "denied",
          source: "workspace",
        }),
      ),
    ).toBe("denied");
  });
});

/** A prompt model whose requests and answers land in one ordered log. */
function promptModel(
  log: EventLog,
  respond: (url: string, init?: RequestInit) => RequestResult = (
    _url,
    init,
  ) => ({
    ok: true,
    status: 200,
    body: viewAfterWrite(
      (
        JSON.parse(String(init?.body)) as {
          decision: ClientPermissionDecision | null;
        }
      ).decision,
    ),
  }),
): ClientPermissionPromptModel {
  return new ClientPermissionPromptModel({
    fetchJson: recordingFetch(log, respond),
    redraw: () => undefined,
    answer: (answer: ClientPermissionAnswer) =>
      log.push(`answer ${JSON.stringify(answer)}`),
  });
}

describe("ClientPermissionPromptModel", () => {
  it("starts every ask with Remember checked", () => {
    const model = promptModel([]);
    model.show(clientPermissionAsk());
    expect(model.isOpen).toBe(true);
    expect(model.isRemembered).toBe(true);
  });

  it("stores a remembered answer before it answers main", async () => {
    const log: EventLog = [];
    const model = promptModel(log);
    model.show(clientPermissionAsk());

    await model.choose("granted");

    expect(log).toEqual([
      `PUT ${MICROPHONE_URL} {"decision":"granted"}`,
      `answer ${JSON.stringify({ promptId: "prompt-1", decision: "granted", isRemembered: true })}`,
    ]);
    expect(model.isOpen).toBe(false);
  });

  it("stores nothing for an unremembered answer", async () => {
    const log: EventLog = [];
    const model = promptModel(log);
    model.show(clientPermissionAsk());
    model.setRemembered(false);

    await model.choose("denied");

    expect(log).toEqual([
      `answer ${JSON.stringify({ promptId: "prompt-1", decision: "denied", isRemembered: false })}`,
    ]);
    expect(model.isOpen).toBe(false);
  });

  it("still answers, unremembered, when the store refuses the write, and says so", async () => {
    const log: EventLog = [];
    const model = promptModel(log, () => ({
      ok: false,
      status: 500,
      body: { error: "disk full" },
    }));
    model.show(clientPermissionAsk());

    await model.choose("granted");

    expect(log).toEqual([
      `PUT ${MICROPHONE_URL} {"decision":"granted"}`,
      `answer ${JSON.stringify({ promptId: "prompt-1", decision: "granted", isRemembered: false })}`,
    ]);
    expect(model.isOpen).toBe(true);
    expect(model.errorMessage).toBe(
      "Your answer applies this time, but couldn't be remembered: disk full",
    );

    // Putting the failure away answers nothing more: main already has it.
    model.dismiss();
    expect(log).toHaveLength(2);
    expect(model.isOpen).toBe(false);
  });

  it("answers a dismissed prompt with a one-time Don't allow", () => {
    const log: EventLog = [];
    const model = promptModel(log);
    model.show(clientPermissionAsk());

    model.dismiss();

    expect(log).toEqual([
      `answer ${JSON.stringify({ promptId: "prompt-1", decision: "denied", isRemembered: false })}`,
    ]);
    expect(model.isOpen).toBe(false);
  });

  it("shows one ask at a time and the next once the first is answered", async () => {
    const log: EventLog = [];
    const model = promptModel(log);
    model.show(
      clientPermissionAsk({ promptId: "prompt-1", workspaceName: "alpha" }),
    );
    model.show(
      clientPermissionAsk({ promptId: "prompt-2", workspaceName: "beta" }),
    );
    model.setRemembered(false);
    expect(model.current?.promptId).toBe("prompt-1");

    await model.choose("granted");

    expect(model.current?.promptId).toBe("prompt-2");
    expect(model.isRemembered).toBe(true);
    model.dismiss();
    expect(model.isOpen).toBe(false);
    expect(log).toEqual([
      `answer ${JSON.stringify({ promptId: "prompt-1", decision: "granted", isRemembered: false })}`,
      `answer ${JSON.stringify({ promptId: "prompt-2", decision: "denied", isRemembered: false })}`,
    ]);
  });

  it("neither answers nor closes while a remembered answer is being stored", async () => {
    const log: EventLog = [];
    let release: (response: RequestResult) => void = () => undefined;
    const model = new ClientPermissionPromptModel({
      fetchJson: (url, init) => {
        log.push(`${init?.method ?? "GET"} ${url}`);
        return new Promise((resolve) => {
          release = resolve;
        });
      },
      redraw: () => undefined,
      answer: (answer) => log.push(`answer ${answer.decision}`),
    });
    model.show(clientPermissionAsk());

    const choosing = model.choose("denied");
    expect(model.isBusy).toBe(true);
    model.dismiss();
    await model.choose("granted");
    expect(model.isOpen).toBe(true);

    release({ ok: true, status: 200, body: viewAfterWrite("denied") });
    await choosing;
    expect(log).toEqual([`PUT ${MICROPHONE_URL}`, "answer denied"]);
    expect(model.isOpen).toBe(false);
  });
});
