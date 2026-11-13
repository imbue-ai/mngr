// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import { takePendingHelpLaunch } from "../../models/help";
import {
  buttonLabelled,
  mountInBody,
  primaryButtonLabels,
  withMindsNative,
} from "../../testing";
import { ShellState } from "../shell/shell-state";
import { Button } from "./Button";
import {
  ErrorNotice,
  relaunchAppActions,
  restartAppActions,
  updateAppActions,
} from "./ErrorDisplay";
import type { ErrorNoticeAttrs } from "./ErrorDisplay";
import { BTN_SIZES, BTN_VARIANTS } from "./constants";
import { TONE_SURFACE_CLASS } from "./errorDesign";

const WORKSPACE_ID = "agent-ab12";

function mountNotice(attrs: ErrorNoticeAttrs): HTMLElement {
  return mountInBody(() => m(ErrorNotice, attrs));
}

/** The tinted box the error itself sits in, inside the notice's wrapper. */
function noticeBox(root: ParentNode): HTMLElement {
  const box = root.querySelector<HTMLElement>("[role=alert] > div");
  if (box === null) throw new Error("no notice box");
  return box;
}

afterEach(() => {
  clearAppContextForTests();
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe("ErrorNotice", () => {
  it("keeps the details off the page until See details is opened", () => {
    const root = mountNotice({
      id: "rename-error",
      error: {
        message: "Couldn't rename this workspace.",
        details: "HTTP 500\nFailed to rename workspace host: exit 1",
      },
    });

    expect(root.textContent).toContain("Couldn't rename this workspace.");
    expect(root.textContent).not.toContain("Failed to rename workspace host");
    // The toggle trails the sentence, inside the paragraph it belongs to,
    // rather than standing on a line of its own under it.
    expect(root.querySelector("p")?.textContent).toBe(
      "Couldn't rename this workspace. See details",
    );

    buttonLabelled(root, "See details").click();
    m.redraw.sync();

    expect(root.textContent).toContain(
      "Failed to rename workspace host: exit 1",
    );
  });

  it("takes no tint of its own when it is drawn inside another error's box", () => {
    const boxed = mountNotice({
      id: "boxed-error",
      error: { message: "Couldn't start an agent.", details: "" },
    });
    const nested = mountNotice({
      id: "nested-error",
      error: { message: "Couldn't start an agent.", details: "" },
      isBoxed: false,
    });

    const tint = (root: HTMLElement): string =>
      root.querySelector("[role='alert'] > div")?.className ?? "";
    expect(tint(boxed)).toContain("c-important-surface");
    expect(tint(nested)).not.toContain("c-important-surface");
    // It is still the same error: mark, sentence and buttons are unchanged.
    expect(nested.querySelector("svg")).not.toBeNull();
    expect(nested.textContent).toContain("Couldn't start an agent.");
  });

  it("offers no See details for an error with nothing under it", () => {
    const root = mountNotice({
      id: "plain-error",
      error: { message: "That's already this machine's size.", details: "" },
    });

    expect(root.textContent).not.toContain("See details");
  });

  it("runs the error's own action", () => {
    const retry = vi.fn();
    const root = mountNotice({
      id: "load-error",
      error: { message: "Couldn't load this.", details: "HTTP 500" },
      actions: [{ label: "Try again", onclick: retry }],
    });

    buttonLabelled(root, "Try again").click();

    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("opens the report form with the sentence and details attached when Report a bug is pressed", () => {
    vi.spyOn(m.route, "get").mockReturnValue(`/workspace/${WORKSPACE_ID}`);
    const shell = new ShellState(createEmptyStores());
    registerAppContext({ stores: shell.stores, shell });
    shell.handleRouteChanged(`/workspace/${WORKSPACE_ID}`);
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    const root = mountNotice({
      id: "rename-error",
      error: {
        message: "Couldn't rename this workspace.",
        details: "HTTP 500",
      },
      workspaceAgentId: WORKSPACE_ID,
    });

    buttonLabelled(root, "Report a bug").click();

    expect(routeSet).toHaveBeenCalledWith(
      "/help",
      expect.objectContaining({ workspace: WORKSPACE_ID }),
      undefined,
    );
    expect(takePendingHelpLaunch()?.errorDetails).toBe(
      "Couldn't rename this workspace.\n\nHTTP 500",
    );
  });

  it("runs beforeAction before Report a bug opens the report form", () => {
    const shell = new ShellState(createEmptyStores());
    registerAppContext({ stores: shell.stores, shell });
    const calls: string[] = [];
    vi.spyOn(m.route, "set").mockImplementation(() => {
      calls.push("route");
    });
    const root = mountNotice({
      id: "sign-in-error",
      error: { message: "Couldn't sign in.", details: "HTTP 500" },
      beforeAction: () => calls.push("close modal"),
    });

    buttonLabelled(root, "Report a bug").click();

    expect(calls).toEqual(["close modal", "route"]);
  });

  it("draws content in place of the sentence and still attaches the sentence to a report", () => {
    const shell = new ShellState(createEmptyStores());
    registerAppContext({ stores: shell.stores, shell });
    vi.spyOn(m.route, "set").mockImplementation(() => undefined);
    const root = mountNotice({
      id: "backup-problems",
      error: {
        message: "Backups have two problems.",
        details: "restic exit 3",
      },
      content: m("ul", [
        m("li", "The repository is locked."),
        m("li", "The key is wrong."),
      ]),
    });

    expect(root.textContent).toContain("The repository is locked.");
    expect(root.textContent).not.toContain("Backups have two problems.");

    buttonLabelled(root, "Report a bug").click();

    expect(takePendingHelpLaunch()?.errorDetails).toBe(
      "Backups have two problems.\n\nrestic exit 3",
    );
  });

  it("draws the first fix as the one main button", () => {
    const root = mountNotice({
      id: "rename-error",
      error: { message: "Couldn't rename this workspace.", details: "" },
      actions: [
        { label: "Restart Imbue Studio", onclick: () => undefined },
        { label: "Try again", onclick: () => undefined },
      ],
    });

    expect(primaryButtonLabels(root)).toEqual(["Restart Imbue Studio"]);
  });

  it("steps the lead button down when the copy asks for something the button only confirms", () => {
    const root = mountNotice({
      id: "create-error",
      error: {
        message:
          "Couldn't create my-project. To fix it, *start Docker, then select Retry*.",
        details: "",
      },
      actions: [{ label: "Retry", onclick: () => undefined }],
    });

    // Starting Docker is the next step; Retry waits on it.
    expect(primaryButtonLabels(root)).toEqual([]);
    expect(buttonLabelled(root, "Retry").className).toContain(
      BTN_VARIANTS.secondary,
    );
  });

  it("draws a way out as a ghost after the fixes, never as the main button", () => {
    const root = mountNotice({
      id: "create-error",
      error: { message: "Couldn't create my-project.", details: "" },
      actions: [
        { label: "Dismiss", onclick: () => undefined, isDismissal: true },
        { label: "Retry", onclick: () => undefined },
      ],
      isBugReportOffered: false,
    });

    expect(
      [...root.querySelectorAll("button")].map((button) => button.textContent),
    ).toEqual(["Retry", "Dismiss"]);
    expect(primaryButtonLabels(root)).toEqual(["Retry"]);
    expect(buttonLabelled(root, "Dismiss").className).toContain(
      BTN_VARIANTS.ghost,
    );
  });

  it("draws Report a bug as a ghost beside other buttons, and as a button of its own when alone", () => {
    const beside = mountNotice({
      id: "share-error",
      error: { message: "Couldn't turn sharing on.", details: "" },
      actions: [{ label: "Try again", onclick: () => undefined }],
    });
    const alone = mountNotice({
      id: "mint-error",
      error: { message: "Couldn't create a key.", details: "" },
    });

    expect(buttonLabelled(beside, "Report a bug").className).toContain(
      BTN_VARIANTS.ghost,
    );
    expect(buttonLabelled(alone, "Report a bug").className).toContain(
      BTN_VARIANTS.secondary,
    );
  });

  it("stays a warning with a spinner while the app is still retrying", () => {
    const root = mountNotice({
      id: "requests-error",
      error: { message: "Imbue Studio couldn't load your requests.", details: "" },
      isRetrying: true,
    });

    const box = noticeBox(root);
    expect(box.className).toContain(TONE_SURFACE_CLASS.warn);
    expect(box.className).not.toContain(TONE_SURFACE_CLASS.error);
    expect(root.textContent).toContain("Still trying");
    expect(root.querySelector(".spinner")).not.toBeNull();
  });

  it("keeps the surface's own buttons outside the error's tinted box", () => {
    const root = mountNotice({
      id: "disconnect-error",
      error: { message: "The change wasn't saved.", details: "" },
      actions: [{ label: "Restart Imbue Studio", onclick: () => undefined }],
      isBugReportOffered: false,
      surfaceActions: [m(Button, { variant: "danger" }, "Yes, disconnect")],
    });

    const box = noticeBox(root);
    expect(box.textContent).toContain("Restart Imbue Studio");
    expect(box.textContent).not.toContain("Yes, disconnect");
    expect(root.textContent).toContain("Yes, disconnect");
  });

  it("sizes the error's own buttons smaller than the surface they sit on", () => {
    const root = mountNotice({
      id: "share-error",
      error: { message: "Couldn't turn sharing on.", details: "" },
      actions: [{ label: "Try again", onclick: () => undefined }],
    });

    expect(buttonLabelled(root, "Try again").className).toContain(
      BTN_SIZES.xs,
    );
  });

  it("draws an action that can't be pressed yet as disabled, saying why", () => {
    const retry = vi.fn();
    const root = mountNotice({
      id: "create-error",
      error: { message: "Couldn't create the workspace.", details: "" },
      actions: [
        {
          label: "Retry",
          onclick: retry,
          isDisabled: true,
          tooltip: "The last attempt is still being cleaned up.",
        },
      ],
    });

    const button = buttonLabelled(root, "Retry");
    expect(button.disabled).toBe(true);
    expect(button.dataset.tooltip).toBe(
      "The last attempt is still being cleaned up.",
    );
    button.click();
    expect(retry).not.toHaveBeenCalled();
  });

  it("draws a destructive action in the danger color, and a busy one as a spinner it can't be pressed through", () => {
    const deny = vi.fn();
    const root = mountNotice({
      id: "request-error",
      error: {
        message: "Imbue Studio couldn't show this request.",
        details: "",
      },
      actions: [
        { label: "Deny", onclick: deny, isDestructive: true, id: "deny" },
        { label: "Update now", onclick: () => undefined, isBusy: true },
      ],
    });

    const denyButton = root.querySelector<HTMLButtonElement>("#deny");
    expect(denyButton?.className).toContain(BTN_VARIANTS.danger);
    expect(primaryButtonLabels(root)).toEqual([]);
    const busy = root.querySelectorAll<HTMLButtonElement>("button")[1];
    expect(busy.disabled).toBe(true);
    expect(busy.querySelector(".spinner")).not.toBeNull();
    expect(busy.textContent).not.toContain("Update now");
  });

  it("spins a running way out too, so it cannot be pressed a second time", () => {
    const discard = vi.fn();
    const root = mountNotice({
      id: "draft-error",
      error: { message: "Couldn't save this draft.", details: "" },
      actions: [
        { label: "Try again", onclick: () => undefined },
        {
          label: "Discard",
          onclick: discard,
          isDismissal: true,
          isBusy: true,
          id: "discard",
        },
      ],
      isBugReportOffered: false,
    });

    const button = root.querySelector<HTMLButtonElement>("#discard");
    expect(button?.className).toContain(BTN_VARIANTS.ghost);
    expect(button?.disabled).toBe(true);
    expect(button?.querySelector(".spinner")).not.toBeNull();
    button?.click();
    expect(discard).not.toHaveBeenCalled();
  });

  it("leaves Report a bug off an error that asks for no report", () => {
    const root = mountNotice({
      id: "limit-error",
      error: { message: "You've reached your plan's limit.", details: "" },
      isBugReportOffered: false,
    });

    expect(root.querySelectorAll("button")).toHaveLength(0);
  });
});

describe("restartAppActions", () => {
  it("offers nothing to press outside the desktop app", async () => {
    await withMindsNative(null, async () => {
      expect(restartAppActions()).toEqual([]);
    });
  });

  it("restarts the app from its one button in the desktop app", async () => {
    const retry = vi.fn();
    await withMindsNative({ retry }, async () => {
      const actions = restartAppActions();

      expect(actions.map((action) => action.label)).toEqual([
        "Restart Imbue Studio",
      ]);
      actions[0].onclick();
    });

    expect(retry).toHaveBeenCalledOnce();
  });
});

describe("relaunchAppActions", () => {
  it("offers a reopen only in the desktop app on macOS, and asks main to relaunch", async () => {
    await withMindsNative(
      { platform: "linux", relaunchApp: vi.fn() },
      async () => {
        expect(relaunchAppActions()).toEqual([]);
      },
    );
    const relaunchApp = vi.fn();
    await withMindsNative({ platform: "darwin", relaunchApp }, async () => {
      const actions = relaunchAppActions();
      expect(actions.map((action) => action.label)).toEqual([
        "Reopen Imbue Studio",
      ]);
      actions[0].onclick();
    });
    expect(relaunchApp).toHaveBeenCalledOnce();
  });
});

describe("updateAppActions", () => {
  it("offers nothing to press outside the desktop app", async () => {
    await withMindsNative(null, async () => {
      expect(updateAppActions()).toEqual([]);
    });
  });

  it("opens the app's Updates settings, on the window's account, from its one button in the desktop app", async () => {
    const shell = new ShellState(createEmptyStores());
    shell.accountId = "user-cd34";
    registerAppContext({ stores: shell.stores, shell });
    const routeSet = vi.spyOn(m.route, "set").mockImplementation(() => {});
    await withMindsNative({}, async () => {
      const actions = updateAppActions();

      expect(actions.map((action) => action.label)).toEqual([
        "Update Imbue Studio",
      ]);
      actions[0].onclick();
    });

    expect(routeSet).toHaveBeenCalledWith("/settings", {
      section: "updates",
      account: "user-cd34",
    });
  });
});
