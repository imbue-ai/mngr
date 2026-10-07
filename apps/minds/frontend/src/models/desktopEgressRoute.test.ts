import { describe, expect, it } from "vitest";
import {
  HOP_PHRASE_SEPARATOR,
  addDesktopEgressHop,
  addableDesktopEgressHops,
  areRoutesEqual,
  canMoveDesktopEgressHopDown,
  canMoveDesktopEgressHopUp,
  canRemoveDesktopEgressHop,
  desktopEgressHopLabel,
  desktopEgressHopPhrases,
  desktopEgressMode,
  desktopEgressModeSentence,
  isDesktopEgressHopShownById,
  moveDesktopEgressHopDown,
  moveDesktopEgressHopUp,
  removeDesktopEgressHop,
  switchedDesktopEgressRoute,
  thisComputerDeviceId,
} from "./desktopEgressRoute";
import { DESKTOPS } from "./workspacePermissions.testing";

const OTHER_DESKTOPS = DESKTOPS.filter((desktop) => !desktop.is_this_computer);

describe("thisComputerDeviceId", () => {
  it("is the id of the desktop marked as this computer, or null without one", () => {
    expect(thisComputerDeviceId(DESKTOPS)).toBe("host-here");
    expect(thisComputerDeviceId(OTHER_DESKTOPS)).toBeNull();
    expect(thisComputerDeviceId([])).toBeNull();
  });
});

describe("desktopEgressMode", () => {
  it("reads the workspace's own machine alone as off", () => {
    expect(desktopEgressMode(["self"], "host-here")).toBe("OFF");
    expect(desktopEgressMode(["self"], null)).toBe("OFF");
  });

  it("reads this computer alone as on", () => {
    expect(desktopEgressMode(["host-here"], "host-here")).toBe("ON");
  });

  it("reads every other route as custom", () => {
    expect(desktopEgressMode(["host-office"], "host-here")).toBe("CUSTOM");
    expect(desktopEgressMode(["host-here", "self"], "host-here")).toBe(
      "CUSTOM",
    );
    expect(desktopEgressMode(["host-here", "host-office"], "host-here")).toBe(
      "CUSTOM",
    );
  });

  it("reads the same route relative to the computer it is seen from", () => {
    expect(desktopEgressMode(["host-here"], "host-office")).toBe("CUSTOM");
    expect(desktopEgressMode(["host-here"], null)).toBe("CUSTOM");
  });

  it("gives the switch the route each side stands for", () => {
    expect(switchedDesktopEgressRoute(true, "host-here")).toEqual([
      "host-here",
    ]);
    expect(switchedDesktopEgressRoute(false, "host-here")).toEqual(["self"]);
  });

  it("cannot switch on without this computer's id, but can still switch off", () => {
    expect(switchedDesktopEgressRoute(true, null)).toBeNull();
    expect(switchedDesktopEgressRoute(false, null)).toEqual(["self"]);
  });

  it("compares routes hop by hop, in order", () => {
    expect(areRoutesEqual(["host-here", "self"], ["host-here", "self"])).toBe(
      true,
    );
    expect(
      areRoutesEqual(
        ["host-here", "host-office"],
        ["host-office", "host-here"],
      ),
    ).toBe(false);
    expect(areRoutesEqual(["host-here"], ["host-here", "self"])).toBe(false);
  });
});

describe("desktopEgressHopLabel", () => {
  it("names this computer and the workspace's own machine in words", () => {
    expect(desktopEgressHopLabel("host-here", DESKTOPS)).toBe("This computer");
    expect(desktopEgressHopLabel("self", DESKTOPS)).toBe(
      "The workspace's own machine",
    );
  });

  it("shows any other computer by its device id, listed or not", () => {
    expect(desktopEgressHopLabel("host-office", DESKTOPS)).toBe("host-office");
    expect(desktopEgressHopLabel("host-gone", DESKTOPS)).toBe("host-gone");
    expect(desktopEgressHopLabel("host-here", OTHER_DESKTOPS)).toBe(
      "host-here",
    );
  });

  it("marks only the hops shown by their device id", () => {
    expect(
      ["host-here", "host-office", "host-gone", "self"].map((hop) =>
        isDesktopEgressHopShownById(hop, DESKTOPS),
      ),
    ).toEqual([false, true, true, false]);
  });
});

describe("desktopEgressModeSentence", () => {
  it("says where requests leave from for the two routes the switch sets, and nothing for a custom one", () => {
    expect(desktopEgressModeSentence("OFF")).toBe(
      "Requests leave from the workspace's own machine.",
    );
    expect(desktopEgressModeSentence("ON")).toBe(
      "Requests leave from this computer.",
    );
    expect(desktopEgressModeSentence("CUSTOM")).toBeNull();
  });
});

describe("desktopEgressHopPhrases", () => {
  function line(
    route: string[],
    desktops: typeof DESKTOPS,
    isMidSentence: boolean,
  ): string {
    return desktopEgressHopPhrases(route, desktops, isMidSentence)
      .map((phrase) => phrase.text)
      .join(HOP_PHRASE_SEPARATOR);
  }

  it("lists a route's hops in order", () => {
    expect(line(["host-here", "host-office", "self"], DESKTOPS, false)).toBe(
      "This computer, then host-office, then the workspace's own machine",
    );
    expect(line(["host-office"], DESKTOPS, false)).toBe("host-office");
  });

  it("lists this computer by its id where it is not known to be this computer", () => {
    expect(line(["host-here"], OTHER_DESKTOPS, false)).toBe("host-here");
  });

  it("lowers words after the first hop but never a device id", () => {
    expect(line(["Host-Cap", "host-here", "Host-Gone"], DESKTOPS, false)).toBe(
      "Host-Cap, then this computer, then Host-Gone",
    );
    expect(
      desktopEgressHopPhrases(
        ["host-here", "Host-Cap", "self"],
        DESKTOPS,
        false,
      ),
    ).toEqual([
      { text: "This computer", isDeviceId: false },
      { text: "Host-Cap", isDeviceId: true },
      { text: "the workspace's own machine", isDeviceId: false },
    ]);
  });

  it("lowers the first hop's words too when the hops continue a sentence", () => {
    expect(line(["host-here", "self"], DESKTOPS, true)).toBe(
      "this computer, then the workspace's own machine",
    );
    expect(line(["self"], DESKTOPS, true)).toBe("the workspace's own machine");
    expect(line(["Host-Cap", "host-here"], DESKTOPS, true)).toBe(
      "Host-Cap, then this computer",
    );
  });
});

describe("adding a hop", () => {
  it("offers every desktop the route does not hold, then the workspace's own machine", () => {
    expect(addableDesktopEgressHops(["self"], DESKTOPS)).toEqual([
      "host-here",
      "host-office",
      "host-studio",
    ]);
    expect(addableDesktopEgressHops(["host-office"], DESKTOPS)).toEqual([
      "host-here",
      "host-studio",
      "self",
    ]);
    expect(
      addableDesktopEgressHops(
        ["host-here", "host-office", "host-studio", "self"],
        DESKTOPS,
      ),
    ).toEqual([]);
  });

  it("appends, but ahead of the workspace's own machine", () => {
    expect(addDesktopEgressHop(["host-here"], "host-office")).toEqual([
      "host-here",
      "host-office",
    ]);
    expect(addDesktopEgressHop(["host-here", "self"], "host-office")).toEqual([
      "host-here",
      "host-office",
      "self",
    ]);
    expect(addDesktopEgressHop(["host-here"], "self")).toEqual([
      "host-here",
      "self",
    ]);
  });

  it("never adds a hop twice", () => {
    expect(addDesktopEgressHop(["host-here", "self"], "host-here")).toEqual([
      "host-here",
      "self",
    ]);
    expect(addDesktopEgressHop(["host-office", "self"], "self")).toEqual([
      "host-office",
      "self",
    ]);
  });
});

describe("removing a hop", () => {
  it("drops the hop at the index and keeps the rest in order", () => {
    expect(
      removeDesktopEgressHop(["host-here", "host-office", "self"], 1),
    ).toEqual(["host-here", "self"]);
    expect(removeDesktopEgressHop(["host-here", "self"], 1)).toEqual([
      "host-here",
    ]);
  });

  it("refuses to empty the route", () => {
    expect(canRemoveDesktopEgressHop(["host-here", "self"])).toBe(true);
    expect(canRemoveDesktopEgressHop(["self"])).toBe(false);
    expect(removeDesktopEgressHop(["self"], 0)).toEqual(["self"]);
  });
});

describe("moving a hop", () => {
  const route = ["host-here", "host-office", "self"];

  it("swaps a hop with its neighbour", () => {
    expect(moveDesktopEgressHopUp(route, 1)).toEqual([
      "host-office",
      "host-here",
      "self",
    ]);
    expect(moveDesktopEgressHopDown(route, 0)).toEqual([
      "host-office",
      "host-here",
      "self",
    ]);
    expect(moveDesktopEgressHopDown(["host-here", "host-office"], 0)).toEqual([
      "host-office",
      "host-here",
    ]);
  });

  it("keeps the workspace's own machine last", () => {
    expect(canMoveDesktopEgressHopUp(route, 2)).toBe(false);
    expect(moveDesktopEgressHopUp(route, 2)).toEqual(route);
    expect(canMoveDesktopEgressHopDown(route, 1)).toBe(false);
    expect(moveDesktopEgressHopDown(route, 1)).toEqual(route);
  });

  it("leaves the ends where they are", () => {
    expect(canMoveDesktopEgressHopUp(route, 0)).toBe(false);
    expect(moveDesktopEgressHopUp(route, 0)).toEqual(route);
    expect(canMoveDesktopEgressHopDown(["host-here", "host-office"], 1)).toBe(
      false,
    );
    expect(moveDesktopEgressHopDown(["host-here", "host-office"], 1)).toEqual([
      "host-here",
      "host-office",
    ]);
    expect(canMoveDesktopEgressHopUp(route, 1)).toBe(true);
    expect(canMoveDesktopEgressHopDown(route, 0)).toBe(true);
  });
});
