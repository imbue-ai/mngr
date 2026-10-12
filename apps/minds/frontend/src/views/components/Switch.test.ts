import { describe, expect, it } from "vitest";
import type { AnyVnode } from "../../testing";
import { allText, attrsOf, classTokensOf } from "../../testing";
import {
  renderSwitch,
  renderSwitchPlaceholder,
  renderSwitchWord,
} from "./Switch";

function switchOn(wait: "settling" | "busy" | null): AnyVnode {
  return renderSwitch({
    id: "the-switch",
    isOn: true,
    label: "Lights",
    wait,
    onFlip: () => undefined,
  }) as AnyVnode;
}

describe("renderSwitch", () => {
  it("draws a live switch that reads its value and name", () => {
    const control = switchOn(null);

    expect(control.tag).toBe("button");
    expect(attrsOf(control).type).toBe("button");
    expect(attrsOf(control).role).toBe("switch");
    expect(attrsOf(control).id).toBe("the-switch");
    expect(attrsOf(control)["aria-checked"]).toBe("true");
    expect(attrsOf(control)["aria-label"]).toBe("Lights");
    expect(attrsOf(control)["aria-busy"]).toBeUndefined();
    expect(attrsOf(control).disabled).toBe(false);
    expect(classTokensOf(control)).toEqual(["perm-switch", "shrink-0"]);
  });

  it("asks for the opposite of the value it shows", () => {
    const asked: boolean[] = [];
    const control = renderSwitch({
      isOn: false,
      label: "Lights",
      onFlip: (isOn) => asked.push(isOn),
    }) as AnyVnode;

    (attrsOf(control).onclick as () => void)();

    expect(asked).toEqual([true]);
    expect(attrsOf(control).id).toBeUndefined();
  });

  it("stays live while settling, with the wait marked on the switch itself", () => {
    const control = switchOn("settling");

    expect(attrsOf(control)["aria-busy"]).toBe("true");
    expect(attrsOf(control).disabled).toBe(false);
    expect(classTokensOf(control)).toContain("is-settling");
  });

  it("goes inert while busy, leaving the wait to the caller", () => {
    const control = switchOn("busy");

    expect(attrsOf(control)["aria-busy"]).toBeUndefined();
    expect(attrsOf(control).disabled).toBe(true);
    expect(classTokensOf(control)).toContain("is-busy");
  });

  it("carries the caller's reason, description, and data attributes", () => {
    const control = renderSwitch({
      isOn: false,
      label: "Lights",
      isDisabled: true,
      title: "Locked",
      describedBy: "lights-help",
      data: { "data-perm-permission": "lights" },
      onFlip: () => undefined,
    }) as AnyVnode;

    expect(attrsOf(control).disabled).toBe(true);
    expect(attrsOf(control).title).toBe("Locked");
    expect(attrsOf(control)["aria-describedby"]).toBe("lights-help");
    expect(attrsOf(control)["data-perm-permission"]).toBe("lights");
  });
});

describe("renderSwitchPlaceholder", () => {
  it("spins at the off side while the value is read", () => {
    const placeholder = renderSwitchPlaceholder({
      id: "the-wait",
      isChecking: true,
      label: "Checking the lights",
    }) as AnyVnode;

    expect(placeholder.tag).toBe("span");
    expect(attrsOf(placeholder).role).toBe("status");
    expect(attrsOf(placeholder)["aria-busy"]).toBe("true");
    expect(attrsOf(placeholder)["aria-label"]).toBe("Checking the lights");
    expect(classTokensOf(placeholder)).toContain("is-checking");
  });

  it("greys the bare track out when the read failed", () => {
    const placeholder = renderSwitchPlaceholder({
      isChecking: false,
      label: "Lights unknown",
    }) as AnyVnode;

    expect(attrsOf(placeholder)["aria-busy"]).toBeUndefined();
    expect(classTokensOf(placeholder)).toContain("is-unknown");
    expect(classTokensOf(placeholder)).not.toContain("is-checking");
  });
});

describe("renderSwitchWord", () => {
  it("spells the value out, and says nothing while it is unknown", () => {
    expect(allText(renderSwitchWord({ id: "w", isOn: true }))).toBe("Yes");
    expect(allText(renderSwitchWord({ isOn: false }))).toBe("No");
    expect(allText(renderSwitchWord({ isOn: null }))).toBe("");
    expect(
      attrsOf(renderSwitchWord({ id: "w", isOn: true }) as AnyVnode).id,
    ).toBe("w");
  });
});
