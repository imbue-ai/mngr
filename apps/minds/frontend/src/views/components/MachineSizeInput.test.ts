import { describe, expect, it } from "vitest";
import { MachineSizeInput } from "./MachineSizeInput";
import type { MachineSizeInputAttrs } from "./MachineSizeInput";
import { attrsOf, classTokensOf, collectVnodes, renderDeep, renderRoot } from "../../testing";
import type { AnyVnode } from "../../testing";

function renderedInput(attrs: MachineSizeInputAttrs): { label: AnyVnode; input: AnyVnode } {
  const tree = renderDeep(renderRoot(MachineSizeInput, attrs));
  const vnodes = collectVnodes(tree);
  const label = vnodes.find((vnode) => vnode.tag === "label");
  const input = vnodes.find((vnode) => vnode.tag === "input");
  if (label === undefined || input === undefined) throw new Error("the field did not render a label and an input");
  return { label, input };
}

describe("MachineSizeInput", () => {
  it("renders a whole-number input labelled for its dimension", () => {
    const typed: string[] = [];
    const { label, input } = renderedInput({
      dimension: "memory_gb",
      label: "GB RAM",
      value: "8",
      oninput: (text) => typed.push(text),
    });
    expect(attrsOf(label).for).toBe("machine-size-memory_gb");
    expect(attrsOf(input)).toMatchObject({
      id: "machine-size-memory_gb",
      name: "memory_gb",
      type: "number",
      min: "1",
      step: "1",
      value: "8",
      disabled: false,
    });
    expect(classTokensOf(input)).not.toContain("!border-important");
    (attrsOf(input).oninput as (event: unknown) => void)({ target: { value: "16" } });
    expect(typed).toEqual(["16"]);
  });

  it("marks an invalid value and can be disabled", () => {
    const { input } = renderedInput({
      dimension: "cpu_count",
      label: "CPUs",
      value: "1.5",
      isInvalid: true,
      disabled: true,
      oninput: () => undefined,
    });
    expect(classTokensOf(input)).toContain("!border-important");
    expect(attrsOf(input).disabled).toBe(true);
  });
});
