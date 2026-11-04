// The This computer panel: the switch that lets agents run commands here, and
// the warning that stands between flipping it on and writing it.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppContext } from "../../../app-context";
import {
  clearAppContextForTests,
  registerAppContext,
} from "../../../app-context";
import { SettingsModel } from "../../../models/settings";
import { SettingsSections } from "./SettingsSections";
import { REMOTE_EXECUTION_CONSEQUENCES } from "../../components/RemoteExecutionWarning";
import type { AnyVnode } from "../../../testing";
import {
  allText,
  attrsOf,
  byId,
  collectVnodes,
  jsonResponse,
  renderDeep,
  renderRoot,
  settingsOverview,
  settle,
} from "../../../testing";

beforeEach(() => {
  registerAppContext({
    shell: { accountId: "user-7" },
  } as unknown as AppContext);
});

afterEach(() => {
  clearAppContextForTests();
  vi.unstubAllGlobals();
});

interface Harness {
  model: SettingsModel;
  requests: { url: string; init: RequestInit | undefined }[];
  render: () => AnyVnode;
}

function harness(isAllowed: boolean): Harness {
  const requests: { url: string; init: RequestInit | undefined }[] = [];
  const fetchImpl = (async (url: string, init?: RequestInit) => {
    requests.push({ url, init });
    return jsonResponse({ version: "v-two" });
  }) as unknown as typeof fetch;
  const model = new SettingsModel(fetchImpl, () => {});
  model.overview = settingsOverview({
    allow_agent_command_execution: isAllowed,
    remote_execution_version: "v-one",
  });
  model.activeSection = "this-computer";
  return {
    model,
    requests,
    render: () =>
      renderDeep(renderRoot(SettingsSections, { model })) as AnyVnode,
  };
}

function remoteExecutionSwitch(root: AnyVnode): AnyVnode {
  const control = byId(root, "remote-execution-switch");
  if (control === undefined) throw new Error("the switch is not drawn");
  return control;
}

describe("the This computer panel", () => {
  it("draws the switch off by default and says it is this device's alone", () => {
    const { render } = harness(false);

    const root = render();

    expect(allText(root)).toContain("Applies to this device only");
    expect(attrsOf(remoteExecutionSwitch(root))["aria-checked"]).toBe("false");
    expect(byId(root, "remote-execution-warning-banner")).toBeUndefined();
  });

  it("opens the warning on flipping the switch on, writing nothing yet", async () => {
    const { model, requests, render } = harness(false);

    (attrsOf(remoteExecutionSwitch(render())).onclick as () => void)();
    await settle();

    expect(model.isRemoteExecutionWarningOpen).toBe(true);
    expect(requests).toEqual([]);
    const root = render();
    expect(byId(root, "remote-execution-warning-banner")).toBeDefined();
    const consequences = allText(
      byId(root, "remote-execution-warning-consequences"),
    );
    for (const consequence of REMOTE_EXECUTION_CONSEQUENCES) {
      expect(consequences).toContain(consequence);
    }
    expect(
      attrsOf(byId(root, "remote-execution-confirm") as AnyVnode).disabled,
    ).toBe(true);
  });

  it("writes the switch only once the acknowledgement is ticked and the warning confirmed", async () => {
    const { model, requests, render } = harness(false);
    model.requestAgentCommandExecution(true);

    await model.confirmRemoteExecutionWarning();
    expect(requests).toEqual([]);

    model.isRemoteExecutionAcknowledged = true;
    await model.confirmRemoteExecutionWarning();
    await settle();

    expect(requests).toHaveLength(1);
    expect(requests[0].url).toBe("/ui/api/settings/remote-execution");
    const init = requests[0].init as RequestInit;
    expect((init.headers as Record<string, string>)["If-Match"]).toBe("v-one");
    expect(JSON.parse(String(init.body))).toEqual({
      allow_agent_command_execution: true,
    });
    expect(model.isRemoteExecutionWarningOpen).toBe(false);
    expect(model.overview?.allow_agent_command_execution).toBe(true);
    expect(model.overview?.remote_execution_version).toBe("v-two");
    expect(attrsOf(remoteExecutionSwitch(render()))["aria-checked"]).toBe(
      "true",
    );
  });

  it("cancelling the warning leaves the switch off and writes nothing", () => {
    const { model, requests, render } = harness(false);
    model.requestAgentCommandExecution(true);
    model.isRemoteExecutionAcknowledged = true;

    (
      attrsOf(byId(render(), "remote-execution-cancel") as AnyVnode)
        .onclick as () => void
    )();

    expect(model.isRemoteExecutionWarningOpen).toBe(false);
    expect(requests).toEqual([]);
    expect(attrsOf(remoteExecutionSwitch(render()))["aria-checked"]).toBe(
      "false",
    );
  });

  it("flipping the switch off writes at once, with no warning", async () => {
    const { model, requests, render } = harness(true);

    (attrsOf(remoteExecutionSwitch(render())).onclick as () => void)();
    await settle();

    expect(model.isRemoteExecutionWarningOpen).toBe(false);
    expect(requests).toHaveLength(1);
    expect(JSON.parse(String((requests[0].init as RequestInit).body))).toEqual({
      allow_agent_command_execution: false,
    });
    expect(model.overview?.allow_agent_command_execution).toBe(false);
  });

  it("says what is in force while the switch is on", () => {
    const { render } = harness(true);

    const root = render();

    expect(
      collectVnodes(root).some(
        (vnode) => attrsOf(vnode).id === "remote-execution-on-notice",
      ),
    ).toBe(true);
    expect(allText(root)).toContain(
      "can run any command on this computer as you",
    );
  });
});
