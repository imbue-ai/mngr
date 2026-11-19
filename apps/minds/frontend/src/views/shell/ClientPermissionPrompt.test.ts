import m from "mithril";
import { describe, expect, it } from "vitest";
import type { ClientPermissionAnswer } from "../../electron-bridge";
import { ClientPermissionPromptModel } from "../../models/clientPermissions";
import {
  clientPermissionAsk,
  clientPermissionsView,
  microphoneRow,
} from "../../models/clientPermissions.testing";
import type { AnyVnode } from "../../testing";
import {
  allText,
  attrsOf,
  byId,
  collectVnodes,
  renderRoot,
  settle,
  withMindsNative,
} from "../../testing";
import { DialogCloseButton, Modal } from "../components/Modal";
import { ClientPermissionPrompt } from "./ClientPermissionPrompt";

const WORKSPACE_ID = "agent-" + "c".repeat(8);
const MICROPHONE_URL = `/ui/api/workspaces/${WORKSPACE_ID}/client-permissions/microphone`;

/** One line per thing that happened, in order: a write, then an answer. */
type EventLog = string[];

function promptModel(log: EventLog): ClientPermissionPromptModel {
  return new ClientPermissionPromptModel({
    fetchJson: (url, init) => {
      log.push(`${init?.method ?? "GET"} ${url} ${String(init?.body)}`);
      return Promise.resolve({
        ok: true,
        status: 200,
        body: clientPermissionsView(),
      });
    },
    redraw: () => undefined,
    answer: (answer: ClientPermissionAnswer) =>
      log.push(`answer ${JSON.stringify(answer)}`),
  });
}

function draw(
  model: ClientPermissionPromptModel,
  accent: string | null = null,
): m.Vnode {
  return renderRoot(ClientPermissionPrompt, { model, accent });
}

function button(root: unknown, id: string): AnyVnode {
  const found = byId(root, id);
  if (found === undefined) throw new Error(`no #${id} in the prompt`);
  return found;
}

function buttonLabels(root: unknown): { deny: string; allow: string } {
  return {
    deny: allText(button(root, "client-permission-prompt-deny").children),
    allow: allText(button(root, "client-permission-prompt-allow").children),
  };
}

function remember(root: unknown, isChecked: boolean): void {
  const checkbox = button(root, "client-permission-prompt-remember");
  (attrsOf(checkbox).onchange as (event: Event) => void)({
    target: { checked: isChecked },
  } as unknown as Event);
}

describe("ClientPermissionPrompt", () => {
  it("draws nothing while no ask is up", () => {
    expect(draw(promptModel([]))).toBeNull();
  });

  it("names the workspace and says the answer stays on this computer", () => {
    const model = promptModel([]);
    model.show(clientPermissionAsk({ workspaceName: "Research notes" }));
    const text = allText(draw(model));
    expect(text).toContain('Allow "Research notes" to use your microphone?');
    expect(text).toContain(
      "This workspace's pages want to listen to an audio input. The answer applies only on this computer.",
    );
    expect(text).toContain("Remember for this workspace on this computer");
  });

  it("paints the workspace's accent beside its name when it is known", () => {
    const model = promptModel([]);
    model.show(clientPermissionAsk());
    const swatch = (root: unknown) =>
      collectVnodes(root).find((vnode) =>
        String(attrsOf(vnode).style ?? "").includes("background-color"),
      );
    expect(attrsOf(swatch(draw(model, "#aa3355")) as AnyVnode).style).toBe(
      "background-color: #aa3355",
    );
    expect(swatch(draw(model, null))).toBeUndefined();
  });

  it("writes on each button what the answer does, following the checkbox", () => {
    const model = promptModel([]);
    model.show(clientPermissionAsk());
    const root = draw(model);
    expect(
      attrsOf(button(root, "client-permission-prompt-remember")).checked,
    ).toBe(true);
    expect(buttonLabels(root)).toEqual({ deny: "Don't allow", allow: "Allow" });
    expect(
      attrsOf(button(root, "client-permission-prompt-allow")).variant,
    ).toBe("primary");

    remember(root, false);
    const unchecked = draw(model);
    expect(
      attrsOf(button(unchecked, "client-permission-prompt-remember")).checked,
    ).toBe(false);
    expect(buttonLabels(unchecked)).toEqual({
      deny: "Don't allow this time",
      allow: "Allow for this session",
    });

    remember(unchecked, true);
    expect(buttonLabels(draw(model))).toEqual({
      deny: "Don't allow",
      allow: "Allow",
    });
  });

  it.each([
    ["the backdrop or Escape", Modal],
    ["the close button", DialogCloseButton],
  ] as const)(
    "answers a one-time Don't allow when closed through %s",
    (_how, closer) => {
      const log: EventLog = [];
      const model = promptModel(log);
      model.show(clientPermissionAsk());
      const closing = collectVnodes(draw(model)).find(
        (vnode) => vnode.tag === closer,
      );

      (attrsOf(closing as AnyVnode).onClose as () => void)();

      expect(log).toEqual([
        `answer ${JSON.stringify({ promptId: "prompt-1", decision: "denied", isRemembered: false })}`,
      ]);
      expect(draw(model)).toBeNull();
    },
  );

  it("stores a remembered Allow, then tells main through the bridge", async () => {
    const log: EventLog = [];
    await withMindsNative(
      {
        answerClientPermission: (answer: ClientPermissionAnswer) =>
          log.push(`answer ${JSON.stringify(answer)}`),
      },
      async () => {
        const model = new ClientPermissionPromptModel({
          fetchJson: (url, init) => {
            log.push(`${init?.method ?? "GET"} ${url} ${String(init?.body)}`);
            return Promise.resolve({
              ok: true,
              status: 200,
              body: clientPermissionsView([
                microphoneRow({
                  decision: "granted",
                  source: "workspace",
                  stored: "granted",
                }),
              ]),
            });
          },
          redraw: () => undefined,
        });
        model.show(clientPermissionAsk());

        (
          attrsOf(button(draw(model), "client-permission-prompt-allow"))
            .onclick as () => void
        )();
        await settle();

        expect(log).toEqual([
          `PUT ${MICROPHONE_URL} {"decision":"granted"}`,
          `answer ${JSON.stringify({ promptId: "prompt-1", decision: "granted", isRemembered: true })}`,
        ]);
        expect(draw(model)).toBeNull();
      },
    );
  });

  it("answers an unchecked Allow for this session without storing it", async () => {
    const log: EventLog = [];
    const model = promptModel(log);
    model.show(clientPermissionAsk());
    remember(draw(model), false);

    (
      attrsOf(button(draw(model), "client-permission-prompt-allow"))
        .onclick as () => void
    )();
    await settle();

    expect(log).toEqual([
      `answer ${JSON.stringify({ promptId: "prompt-1", decision: "granted", isRemembered: false })}`,
    ]);
  });

  it("says when a remembered answer could not be stored, and closes without answering again", async () => {
    const log: EventLog = [];
    const model = new ClientPermissionPromptModel({
      fetchJson: () =>
        Promise.resolve({
          ok: false,
          status: 500,
          body: { error: "disk full" },
        }),
      redraw: () => undefined,
      answer: (answer) => log.push(`answer ${JSON.stringify(answer)}`),
    });
    model.show(clientPermissionAsk());

    (
      attrsOf(button(draw(model), "client-permission-prompt-deny"))
        .onclick as () => void
    )();
    await settle();

    const failed = draw(model);
    expect(allText(failed)).toContain(
      "Your answer applies this time, but couldn't be remembered: disk full",
    );
    expect(byId(failed, "client-permission-prompt-allow")).toBeUndefined();
    (
      attrsOf(button(failed, "client-permission-prompt-close"))
        .onclick as () => void
    )();
    expect(log).toEqual([
      `answer ${JSON.stringify({ promptId: "prompt-1", decision: "denied", isRemembered: false })}`,
    ]);
    expect(draw(model)).toBeNull();
  });

  it("shows the next workspace's ask once the first is answered", () => {
    const model = promptModel([]);
    model.show(
      clientPermissionAsk({ promptId: "prompt-1", workspaceName: "alpha" }),
    );
    model.show(
      clientPermissionAsk({ promptId: "prompt-2", workspaceName: "beta" }),
    );
    expect(allText(draw(model))).toContain(
      'Allow "alpha" to use your microphone?',
    );

    model.dismiss();

    expect(allText(draw(model))).toContain(
      'Allow "beta" to use your microphone?',
    );
  });
});
