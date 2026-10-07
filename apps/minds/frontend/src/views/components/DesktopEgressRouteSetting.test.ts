// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import { DESKTOPS } from "../../models/workspacePermissions.testing";
import { renderDesktopEgressRouteSetting } from "./DesktopEgressRouteSetting";

/** Draw the setting into the document with its editor open on `route`, the
 * way a caller holding the route would: every change is drawn again. */
function mountEditor(initialRoute: string[]): HTMLElement {
  const root = document.createElement("div");
  document.body.appendChild(root);
  let route = initialRoute;
  const draw = (): void =>
    m.render(
      root,
      renderDesktopEgressRouteSetting({
        serviceName: "slack",
        route: initialRoute,
        desktops: DESKTOPS,
        onSwitch: () => undefined,
        onAdjust: () => undefined,
        editor: {
          route,
          onChange: (changed) => {
            route = changed;
            draw();
          },
          onClose: () => undefined,
          onSave: () => undefined,
        },
      }),
    );
  draw();
  return root;
}

function moveButton(root: HTMLElement, direction: "up" | "down", hop: string): HTMLButtonElement {
  const button = root.querySelector<HTMLButtonElement>(`[data-desktop-egress-move-${direction}="${hop}"]`);
  if (button === null) throw new Error(`no move-${direction} button for ${hop}`);
  return button;
}

function hops(root: HTMLElement): (string | null)[] {
  return [...root.querySelectorAll("[data-desktop-egress-hop]")].map((row) =>
    row.getAttribute("data-desktop-egress-hop"),
  );
}

describe("desktop egress route editor focus", () => {
  afterEach(() => {
    document.body.replaceChildren();
  });

  it("keeps focus on the button that moved a hop, so a second press moves the same hop again", () => {
    const root = mountEditor(["host-here", "host-office", "host-studio", "self"]);
    const pressed = moveButton(root, "up", "host-studio");
    pressed.focus();
    pressed.click();

    expect(hops(root)).toEqual(["host-here", "host-studio", "host-office", "self"]);
    expect(document.activeElement).toBe(moveButton(root, "up", "host-studio"));

    (document.activeElement as HTMLButtonElement).click();
    expect(hops(root)).toEqual(["host-studio", "host-here", "host-office", "self"]);
  });

  it("hands focus to the moved hop's other button when the pressed one can go no further", () => {
    const root = mountEditor(["host-here", "host-office", "self"]);
    const pressed = moveButton(root, "up", "host-office");
    pressed.focus();
    pressed.click();

    expect(hops(root)).toEqual(["host-office", "host-here", "self"]);
    expect(moveButton(root, "up", "host-office").disabled).toBe(true);
    expect(document.activeElement).toBe(moveButton(root, "down", "host-office"));
  });
});
