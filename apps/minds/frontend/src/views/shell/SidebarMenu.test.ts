import { describe, expect, it } from "vitest";
import { statusIcon } from "./SidebarMenu";
import { attrsOf, collectVnodes } from "../../testing";

function pathData(liveness: string): string[] {
  return collectVnodes(statusIcon(liveness))
    .filter((vnode) => vnode.tag === "path")
    .map((vnode) => String(attrsOf(vnode).d));
}

describe("statusIcon", () => {
  it("gives every segment of every status icon its own path", () => {
    for (const liveness of ["STOPPED", "STOPPING", "STARTING", "UNKNOWN"]) {
      const paths = pathData(liveness);
      expect(paths.length).toBeGreaterThan(0);
      for (const d of paths) {
        expect(d.match(/[Mm]/g)).toEqual([d[0]]);
      }
    }
  });

  it("renders nothing for a running workspace", () => {
    expect(statusIcon("RUNNING")).toBeNull();
  });
});
