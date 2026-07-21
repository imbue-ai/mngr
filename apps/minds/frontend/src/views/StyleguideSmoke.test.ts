import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";

import { MindsUIError } from "../mount";
import { mountStyleguideSmoke } from "./StyleguideSmoke";

function installBootStateIsland(json: string): void {
  const island = document.createElement("script");
  island.type = "application/json";
  island.id = "minds-boot-state";
  island.textContent = json;
  document.body.appendChild(island);
}

function installMountContainer(): HTMLElement {
  const container = document.createElement("div");
  document.body.appendChild(container);
  return container;
}

afterEach(() => {
  // Run any teardown listener the test's mount registered (a no-op when the
  // test mounted nothing), so mithril roots and once-only window listeners
  // never leak into the next test.
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
});

describe("mountStyleguideSmoke", () => {
  it("mounts synchronously from the boot island and marks the container", () => {
    installBootStateIsland(JSON.stringify({ styleguide_smoke: { message: "island message 48213" } }));
    const container = installMountContainer();

    mountStyleguideSmoke(container);

    expect(container.textContent).toContain("island message 48213");
    expect(container.getAttribute("data-minds-mounted")).toBe("true");
  });

  it("redraws through mithril after a click event", () => {
    installBootStateIsland(JSON.stringify({ styleguide_smoke: { message: "redraw check 71542" } }));
    const container = installMountContainer();
    mountStyleguideSmoke(container);
    const button = container.querySelector("button");
    expect(button).not.toBeNull();
    expect(button?.textContent).toBe("Click to redraw");

    button?.dispatchEvent(new MouseEvent("click"));
    // Mithril batches its auto-redraw; force the flush so the assertion sees
    // the post-click view.
    m.redraw.sync();

    expect(button?.textContent).toBe("Redrawn 1×");
  });

  it("unmounts and clears the marker on minds:page-teardown", () => {
    installBootStateIsland(JSON.stringify({ styleguide_smoke: { message: "teardown check 90311" } }));
    const container = installMountContainer();
    mountStyleguideSmoke(container);
    expect(container.childElementCount).toBeGreaterThan(0);

    window.dispatchEvent(new Event("minds:page-teardown"));

    expect(container.childElementCount).toBe(0);
    expect(container.hasAttribute("data-minds-mounted")).toBe(false);
  });

  it("throws when the boot island is missing", () => {
    const container = installMountContainer();
    expect(() => mountStyleguideSmoke(container)).toThrow(MindsUIError);
  });

  it("throws when the boot island holds invalid JSON", () => {
    installBootStateIsland("{not json");
    const container = installMountContainer();
    expect(() => mountStyleguideSmoke(container)).toThrow(MindsUIError);
  });

  it("throws when the boot island lacks the smoke message", () => {
    installBootStateIsland(JSON.stringify({ styleguide_smoke: {} }));
    const container = installMountContainer();
    expect(() => mountStyleguideSmoke(container)).toThrow(MindsUIError);
  });

  it("throws when the mount target is missing", () => {
    installBootStateIsland(JSON.stringify({ styleguide_smoke: { message: "unused 55120" } }));
    expect(() => mountStyleguideSmoke(document.getElementById("does-not-exist"))).toThrow(MindsUIError);
  });
});
