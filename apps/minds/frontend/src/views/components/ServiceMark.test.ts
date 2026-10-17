import m from "mithril";
import { beforeEach, describe, expect, it } from "vitest";
import { DARK_SURFACE_VARIANT_SERVICE_NAMES, forgetFailedServiceMarks, serviceMark } from "./ServiceMark";

// The 404 memo is app-global on purpose, so it has to be cleared between
// tests that would otherwise inherit each other's missing marks.
beforeEach(forgetFailedServiceMarks);
import type { AnyVnode } from "../../testing";
import { attrsOf, collectVnodes } from "../../testing";

function imageSources(mark: m.Children): string[] {
  return collectVnodes(mark)
    .filter((vnode) => vnode.tag === "img")
    .map((img) => String(attrsOf(img).src));
}

/** The services ServiceMark ships a second, dark-surface image for. This
 * literal is the link between the two suites: the test below pins it to the
 * component's own set, and service_icons_test.py pins the same list to the
 * files on disk. So a variant asked for without shipping its artwork -- which
 * would draw a broken image, since the variant carries no fallback probe --
 * fails on one side or the other. */
const DARK_SURFACE_VARIANT_SERVICES = ["aws", "github", "linear", "ngrok", "ramp", "sentry", "umami"];

/** Services drawn with one image in both themes -- one per shape of mark:
 * multicolor, single-color by brand, and the two flat silhouettes. */
const SINGLE_IMAGE_SERVICES = ["slack", "gitlab", "notion-mcp", "dropbox", "stripe", "calendly", "yelp"];

describe("serviceMark", () => {
  it("draws one image per service, from the service's own file", () => {
    for (const serviceName of SINGLE_IMAGE_SERVICES) {
      expect(imageSources(serviceMark(serviceName, "w-4 h-4", "brand", null))).toEqual([
        `/_static/service_icons/${serviceName}.svg`,
      ]);
    }
  });

  it("asks for a second image for exactly the services pinned to shipped artwork", () => {
    expect([...DARK_SURFACE_VARIANT_SERVICE_NAMES].sort()).toEqual([...DARK_SURFACE_VARIANT_SERVICES].sort());
  });

  it("adds the vendor's white variant for the marks that vanish on the dark surface", () => {
    for (const serviceName of DARK_SURFACE_VARIANT_SERVICES) {
      expect(imageSources(serviceMark(serviceName, "w-4 h-4", "brand", null))).toEqual([
        `/_static/service_icons/${serviceName}.svg`,
        `/_static/service_icons/${serviceName}-on-dark.svg`,
      ]);
    }
  });

  it("probes only the base image, so a missing variant cannot retire a logo that loads", () => {
    const images = collectVnodes(serviceMark("github", "w-4 h-4", "brand", null)).filter(
      (vnode) => vnode.tag === "img",
    );
    expect(typeof attrsOf(images[0]).onerror).toBe("function");
    expect(attrsOf(images[1]).onerror).toBeUndefined();
  });

  it("takes the size class as given and marks a disconnected account's own logo", () => {
    const brand = serviceMark("slack", "w-5 h-5 shrink-0", "brand", null) as AnyVnode;
    const muted = serviceMark("slack", "w-5 h-5 shrink-0", "muted", null) as AnyVnode;
    expect(attrsOf(brand).className).toBe("service-mark w-5 h-5 shrink-0");
    expect(attrsOf(muted).className).toBe("service-mark service-mark-muted w-5 h-5 shrink-0");
  });

  it("falls back rather than requesting a mark for a service with no name", () => {
    expect(serviceMark("", "w-4 h-4", "brand", "no mark")).toBe("no mark");
  });
});
