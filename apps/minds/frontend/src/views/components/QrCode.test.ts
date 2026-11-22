import { describe, expect, it } from "vitest";
import { QR_QUIET_ZONE_MODULES, qrModules, qrPathData } from "../../models/qrCode";
import type { AnyVnode } from "../../testing";
import { attrsOf, classTokensOf, collectVnodes, renderRoot } from "../../testing";
import { QrCode } from "./QrCode";

const LINK = "https://a.example/x";

function draw(value: string | null): AnyVnode {
  return renderRoot(QrCode, { value, label: "QR code for alpha" }) as AnyVnode;
}

describe("QrCode", () => {
  it("draws the empty box a code will appear in while there is no link", () => {
    const box = draw(null);

    expect(box.tag).toBe("div");
    expect(attrsOf(box)["data-qr-code"]).toBe("empty");
    expect(attrsOf(box)["aria-hidden"]).toBe("true");
    expect(attrsOf(box).role).toBeUndefined();
    expect(collectVnodes(box).some((vnode) => vnode.tag === "path")).toBe(false);
  });

  it("draws the link's code as a labelled image", () => {
    const code = draw(LINK);

    expect(code.tag).toBe("svg");
    expect(attrsOf(code)["data-qr-code"]).toBe("ready");
    expect(attrsOf(code).role).toBe("img");
    expect(attrsOf(code)["aria-label"]).toBe("QR code for alpha");
    const paths = collectVnodes(code).filter((vnode) => vnode.tag === "path");
    expect(paths).toHaveLength(1);
    expect(attrsOf(paths[0]).d).toBe(qrPathData(qrModules(LINK)));
  });

  it("leaves the quiet zone around the modules on every side", () => {
    const side = qrModules(LINK).length + 2 * QR_QUIET_ZONE_MODULES;

    expect(attrsOf(draw(LINK)).viewBox).toBe(`0 0 ${side} ${side}`);
  });

  it("stays black on white whatever the colour scheme", () => {
    const code = draw(LINK);

    expect(classTokensOf(code)).toContain("bg-white");
    expect(attrsOf(collectVnodes(code).filter((vnode) => vnode.tag === "path")[0]).fill).toBe("#000000");
  });

  it("draws the new link's code when the link changes", () => {
    const instance = QrCode();
    const view = (value: string): AnyVnode =>
      (instance.view as unknown as (vnode: unknown) => AnyVnode).call(instance, {
        attrs: { value, label: "QR code" },
      });
    const pathOf = (code: AnyVnode): unknown =>
      attrsOf(collectVnodes(code).filter((vnode) => vnode.tag === "path")[0]).d;
    const other = "https://a.example/another-link-that-is-longer";

    expect(pathOf(view(LINK))).toBe(qrPathData(qrModules(LINK)));
    expect(pathOf(view(other))).toBe(qrPathData(qrModules(other)));
    expect(attrsOf(view(other)).viewBox).toBe(
      `0 0 ${qrModules(other).length + 2 * QR_QUIET_ZONE_MODULES} ${qrModules(other).length + 2 * QR_QUIET_ZONE_MODULES}`,
    );
  });
});
