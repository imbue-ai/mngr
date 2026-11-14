// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import type { ForwardPageActionKind, ForwardPageContext } from "../../models/forwardPages";
import {
  FORWARD_PAGE_SPINNER_ONLY_MS,
  forwardPageNotice,
  isNoticeSpinnerOnly,
  parseForwardPageMessage,
} from "../../models/forwardPages";
import { buttonLabelled, mountInBody, primaryButtonLabels } from "../../testing";
import { ForwardPageOverlay } from "./ForwardPageOverlay";

const DESKTOP: ForwardPageContext = { isRestartAppAvailable: true, isWorkspaceListed: true };
const BROWSER: ForwardPageContext = { isRestartAppAvailable: false, isWorkspaceListed: true };

/** Within a page's spinner-only seconds, and after them. */
const EARLY_MS = FORWARD_PAGE_SPINNER_ONLY_MS / 2;
const LATER_MS = 0;

/** Mount the overlay for the message a proxy page posts, `msUntilExplained`
 * before its spinner-only seconds end, as the frame does. */
function mountForMessage(
  data: Record<string, unknown>,
  context: ForwardPageContext,
  msUntilExplained: number,
): { root: HTMLElement; actions: ForwardPageActionKind[] } {
  const message = parseForwardPageMessage({ type: "mngr-forward:page", details: "", ...data });
  if (message?.kind !== "report") throw new Error("not a page report");
  const notice = forwardPageNotice(message.report, context);
  const actions: ForwardPageActionKind[] = [];
  const root = mountInBody(() =>
    m(ForwardPageOverlay, {
      notice,
      isSpinnerOnly: isNoticeSpinnerOnly(notice, msUntilExplained),
      workspaceAgentId: "agent-ab12",
      onAction: (kind) => actions.push(kind),
    }),
  );
  return { root, actions };
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("ForwardPageOverlay", () => {
  it("shows only the spinner while a wait may still resolve, then why", () => {
    const report = { reason: "NOT_LISTENING", status: 503 };
    const early = mountForMessage(report, DESKTOP, EARLY_MS);
    expect(early.root.querySelector(".spinner")).not.toBeNull();
    expect(early.root.textContent?.trim()).toBe("Loading workspace");

    const later = mountForMessage(report, DESKTOP, LATER_MS);
    expect(later.root.querySelector(".spinner")).not.toBeNull();
    expect(later.root.textContent).toContain("This workspace isn't answering right now.");
  });

  it("shows a connection this device could not build at once, with Restart Imbue Studio, still re-attempting", () => {
    const { root, actions } = mountForMessage({ reason: "LOCAL_CONNECTION_FAILED", status: 503 }, DESKTOP, EARLY_MS);

    expect(root.querySelector(".spinner")).not.toBeNull();
    const notice = root.querySelector("#forward-page-error") as HTMLElement;
    expect(notice.textContent).toContain("Imbue Studio can't connect to this workspace from this device.");
    buttonLabelled(notice, "Restart Imbue Studio").click();
    expect(actions).toEqual(["restart-app"]);
    expect(buttonLabelled(notice, "Report a bug")).toBeDefined();
  });

  it("offers no restart in a browser, and says where the app to restart runs", () => {
    const { root } = mountForMessage({ reason: "LOCAL_CONNECTION_FAILED", status: 503 }, BROWSER, EARLY_MS);

    expect(root.textContent).toContain("Restarting the Imbue Studio app on the computer running it usually fixes this.");
    expect(() => buttonLabelled(root, "Restart Imbue Studio")).toThrow();
  });

  it("offers Restart Imbue Studio first and Try again beside it for a dropped connection, with the proxy's answer behind See details", () => {
    const { root, actions } = mountForMessage(
      { reason: "CONNECTION_LOST", status: 502, details: "Backend connection lost" },
      DESKTOP,
      EARLY_MS,
    );

    expect(root.querySelector(".spinner")).toBeNull();
    expect(root.textContent).toContain("The connection to this workspace dropped.");
    expect(root.textContent).not.toContain("Backend connection lost");
    buttonLabelled(root, "See details").click();
    m.redraw.sync();
    expect(root.textContent).toContain("HTTP 502 CONNECTION_LOST: Backend connection lost");

    expect(primaryButtonLabels(root)).toEqual(["Restart Imbue Studio"]);
    buttonLabelled(root, "Restart Imbue Studio").click();
    buttonLabelled(root, "Try again").click();
    expect(actions).toEqual(["restart-app", "try-again"]);
  });

  it("waits out the spinner-only seconds for an unlisted workspace discovery does not know, then offers the way back", () => {
    const unlisted: ForwardPageContext = { isRestartAppAvailable: true, isWorkspaceListed: false };
    const report = { reason: "UNKNOWN_AGENT", status: 503 };
    // Before the list loads, a real workspace looks unlisted.
    expect(mountForMessage(report, unlisted, EARLY_MS).root.textContent?.trim()).toBe("Loading workspace");

    const { root, actions } = mountForMessage(report, unlisted, LATER_MS);
    expect(root.textContent).toContain("This workspace isn't in your workspaces list. It may have been removed.");
    expect(() => buttonLabelled(root, "Report a bug")).toThrow();
    buttonLabelled(root, "Back to workspaces").click();
    expect(actions).toEqual(["back-to-workspaces"]);
  });

  it("waits out the spinner-only seconds for a listed workspace discovery does not know, then offers the way back", () => {
    const report = { reason: "UNKNOWN_AGENT", status: 503 };
    // A provider that has not reported yet leaves its workspaces unknown for a moment.
    expect(mountForMessage(report, DESKTOP, EARLY_MS).root.textContent?.trim()).toBe("Loading workspace");

    const { root, actions } = mountForMessage(report, DESKTOP, LATER_MS);
    expect(root.querySelector(".spinner")).not.toBeNull();
    const notice = root.querySelector("#forward-page-error") as HTMLElement;
    expect(notice.textContent).toContain("Imbue Studio can't find this workspace right now.");
    buttonLabelled(notice, "Back to workspaces").click();
    expect(actions).toEqual(["back-to-workspaces"]);
    expect(buttonLabelled(notice, "Report a bug")).toBeDefined();
  });

  it("never explains a wait for discovery's first report, however long it lasts", () => {
    const { root } = mountForMessage({ reason: "DISCOVERY_PENDING", status: 503 }, DESKTOP, LATER_MS);

    expect(root.querySelector(".spinner")).not.toBeNull();
    expect(root.textContent?.trim()).toBe("Loading workspace");
  });
});
