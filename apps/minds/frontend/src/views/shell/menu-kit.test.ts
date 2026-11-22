// @vitest-environment jsdom
//
// The highlight walks the menu's real rows in the order they are drawn, and a
// menu hangs from its button's measured rect, so both need real elements.
import { afterEach, describe, expect, it } from "vitest";
import type { AnyVnode } from "../../testing";
import {
  allText,
  attrsOf,
  classTokensOf,
  collectVnodes,
  placeMeasuredElement,
  required,
  withAttr,
} from "../../testing";
import {
  MENU_MIN_WIDTH_PX,
  MENU_ROW_CLASS,
  MenuHighlight,
  menuActionRow,
  menuCurrentCheck,
  menuDivider,
  menuSectionHeading,
  menuStyle,
} from "./menu-kit";

afterEach(() => {
  document.body.replaceChildren();
});

interface RowSpec {
  key: string;
  isDisabled?: boolean;
}

/** A menu whose rows are buttons in the arrow-key order, with the clicks each one took. */
function buildMenu(rows: RowSpec[]): { menu: HTMLElement; clicks: string[] } {
  const menu = document.createElement("div");
  const clicks: string[] = [];
  for (const row of rows) {
    const button = document.createElement("button");
    button.dataset.navKey = row.key;
    button.disabled = row.isDisabled === true;
    button.addEventListener("click", () => clicks.push(row.key));
    menu.appendChild(button);
  }
  // A row outside the arrow-key order (a "Sign in" beside an account's row).
  menu.appendChild(document.createElement("button"));
  document.body.appendChild(menu);
  return { menu, clicks };
}

function keydown(key: string): KeyboardEvent {
  return new KeyboardEvent("keydown", { key, cancelable: true });
}

describe("MenuHighlight.move", () => {
  it("goes down from none to the first row, then row by row, and stops at the last", () => {
    const { menu } = buildMenu([{ key: "a" }, { key: "b" }, { key: "c" }]);
    const highlight = new MenuHighlight();

    const visited: (string | null)[] = [];
    for (let step = 0; step < 4; step++) {
      highlight.move(menu, 1);
      visited.push(highlight.key);
    }

    expect(visited).toEqual(["a", "b", "c", "c"]);
  });

  it("goes up row by row, and from the first row back to none", () => {
    const { menu } = buildMenu([{ key: "a" }, { key: "b" }, { key: "c" }]);
    const highlight = new MenuHighlight();
    highlight.key = "c";

    const visited: (string | null)[] = [];
    for (let step = 0; step < 4; step++) {
      highlight.move(menu, -1);
      visited.push(highlight.key);
    }

    expect(visited).toEqual(["b", "a", null, null]);
  });

  it("steps over a disabled row", () => {
    const { menu } = buildMenu([{ key: "a" }, { key: "remote", isDisabled: true }, { key: "c" }]);
    const highlight = new MenuHighlight();

    highlight.move(menu, 1);
    highlight.move(menu, 1);
    expect(highlight.key).toBe("c");

    highlight.move(menu, -1);
    expect(highlight.key).toBe("a");
  });

  it("starts again from the top when the highlighted row has left the menu", () => {
    // The search narrowed the list from under the highlight.
    const { menu } = buildMenu([{ key: "a" }, { key: "b" }]);
    const highlight = new MenuHighlight();
    highlight.key = "gone";

    highlight.move(menu, 1);

    expect(highlight.key).toBe("a");
  });

  it("leaves the highlight alone in a menu with no rows to move through", () => {
    const { menu } = buildMenu([{ key: "only", isDisabled: true }]);
    const highlight = new MenuHighlight();

    highlight.move(menu, 1);

    expect(highlight.key).toBeNull();
  });
});

describe("MenuHighlight.activate", () => {
  it("clicks the highlighted row and says so", () => {
    const { menu, clicks } = buildMenu([{ key: "a" }, { key: "b" }]);
    const highlight = new MenuHighlight();
    highlight.key = "b";

    expect(highlight.activate(menu)).toBe(true);
    expect(clicks).toEqual(["b"]);
  });

  it("clicks nothing with no highlight, or with one whose row is not in the menu", () => {
    const { menu, clicks } = buildMenu([{ key: "a" }]);
    const highlight = new MenuHighlight();

    expect(highlight.activate(menu)).toBe(false);
    highlight.key = "gone";
    expect(highlight.activate(menu)).toBe(false);
    expect(clicks).toEqual([]);
  });
});

describe("MenuHighlight.handleKey", () => {
  it("takes the arrow keys as its own, moving the highlight and keeping the page from scrolling", () => {
    const { menu } = buildMenu([{ key: "a" }, { key: "b" }]);
    const highlight = new MenuHighlight();

    const down = keydown("ArrowDown");
    expect(highlight.handleKey(down, menu)).toBe(true);
    expect(down.defaultPrevented).toBe(true);
    expect(highlight.key).toBe("a");

    highlight.handleKey(keydown("ArrowDown"), menu);
    expect(highlight.key).toBe("b");

    const up = keydown("ArrowUp");
    expect(highlight.handleKey(up, menu)).toBe(true);
    expect(up.defaultPrevented).toBe(true);
    expect(highlight.key).toBe("a");
  });

  it("chooses the highlighted row on Enter", () => {
    const { menu, clicks } = buildMenu([{ key: "a" }, { key: "b" }]);
    const highlight = new MenuHighlight();
    highlight.key = "a";

    const enter = keydown("Enter");
    expect(highlight.handleKey(enter, menu)).toBe(true);

    expect(enter.defaultPrevented).toBe(true);
    expect(clicks).toEqual(["a"]);
  });

  it("leaves Enter to the caller while nothing is highlighted, and every other key always", () => {
    const { menu, clicks } = buildMenu([{ key: "a" }]);
    const highlight = new MenuHighlight();

    const enter = keydown("Enter");
    expect(highlight.handleKey(enter, menu)).toBe(false);
    expect(enter.defaultPrevented).toBe(false);

    highlight.key = "a";
    const letter = keydown("x");
    expect(highlight.handleKey(letter, menu)).toBe(false);
    expect(letter.defaultPrevented).toBe(false);
    expect(highlight.key).toBe("a");
    expect(clicks).toEqual([]);
  });
});

describe("a menu row's highlight", () => {
  it("marks only the highlighted row, in its attributes and its class", () => {
    const highlight = new MenuHighlight();
    highlight.key = "b";

    expect(highlight.rowAttrs("a")["data-nav-key"]).toBe("a");
    expect(highlight.rowAttrs("a")["data-highlighted"]).toBeUndefined();
    expect(highlight.rowAttrs("b")["data-highlighted"]).toBe("true");
    expect(highlight.rowClass("row", "a")).toBe("row");
    expect(highlight.rowClass("row", "b").split(" ")).toEqual(["row", "!bg-fill-hover"]);
  });

  it("gives the row for what the window already shows a fainter fill, which the highlight's replaces", () => {
    const highlight = new MenuHighlight();

    expect(highlight.rowClass("row", "a", { isCurrent: true }).split(" ")).toEqual(["row", "!bg-fill-subtle"]);
    expect(highlight.rowClass("row", "b", { isCurrent: false })).toBe("row");

    highlight.key = "a";
    expect(highlight.rowClass("row", "a", { isCurrent: true }).split(" ")).toEqual(["row", "!bg-fill-hover"]);
  });

  it("follows the pointer onto another row, so the mouse and the arrow keys share one highlight", () => {
    const highlight = new MenuHighlight();
    highlight.key = "a";
    const event: { redraw?: boolean } = {};

    (highlight.rowAttrs("b").onmousemove as (event: unknown) => void)(event);

    expect(highlight.key).toBe("b");
    // The move has to be drawn.
    expect(event.redraw).toBeUndefined();
  });

  it("asks for no redraw while the pointer moves within the row it already highlights", () => {
    const highlight = new MenuHighlight();
    highlight.key = "a";
    const event: { redraw?: boolean } = {};

    (highlight.rowAttrs("a").onmousemove as (event: unknown) => void)(event);

    expect(highlight.key).toBe("a");
    expect(event.redraw).toBe(false);
  });
});

describe("menuStyle", () => {
  it("hangs the menu from its button's bottom edge, left edges lined up, at the least width", () => {
    placeMeasuredElement("the-button", { left: 132, top: 8, width: 120, height: 28 });

    expect(menuStyle("the-button")).toBe(`left: 132px; top: 36px; width: ${MENU_MIN_WIDTH_PX}px;`);
    expect(MENU_MIN_WIDTH_PX).toBe(260);
  });

  it("is as wide as a button wider than the least width", () => {
    placeMeasuredElement("the-button", { left: 132, top: 8, width: 310, height: 28 });

    expect(menuStyle("the-button")).toBe("left: 132px; top: 36px; width: 310px;");
  });

  it("grows to fit its content within the window when asked to, never narrower than the least width", () => {
    placeMeasuredElement("the-button", { left: 132, top: 8, width: 120, height: 28 });

    expect(menuStyle("the-button", { isFittedToContent: true })).toBe(
      "left: 132px; top: 36px; width: max-content; min-width: 260px; max-width: calc(100vw - 132px - 8px);",
    );
  });

  it("goes under the bar at the fallback left edge with no button to hang from", () => {
    expect(menuStyle("the-button", { fallbackLeft: 12 })).toBe(
      "left: 12px; top: var(--titlebar-height); width: 260px;",
    );
    expect(menuStyle("the-button")).toBe("left: 80px; top: var(--titlebar-height); width: 260px;");
  });

  it("reads a button the bar is not showing, which measures at nothing, as no button", () => {
    placeMeasuredElement("the-button", { left: 0, top: 0, width: 0, height: 0 });

    expect(menuStyle("the-button")).toBe("left: 80px; top: var(--titlebar-height); width: 260px;");
  });
});

describe("menuActionRow", () => {
  function row(highlight: MenuHighlight, overrides: { disabled?: boolean; onclick?: () => void } = {}): AnyVnode {
    return required(
      collectVnodes(
        menuActionRow(highlight, {
          id: "the-row",
          navKey: "new",
          icon: "plus",
          label: "New workspace",
          onclick: overrides.onclick ?? (() => undefined),
          disabled: overrides.disabled,
        }),
      )[0],
    );
  }

  it("is a menu item with its icon and label, in the arrow-key order, that runs its action on a click", () => {
    let clicks = 0;
    const button = row(new MenuHighlight(), { onclick: () => (clicks += 1) });

    expect(button.tag).toBe("button");
    expect(attrsOf(button).role).toBe("menuitem");
    expect(attrsOf(button).id).toBe("the-row");
    expect(attrsOf(button)["data-nav-key"]).toBe("new");
    expect(attrsOf(button).disabled).toBe(false);
    expect(allText(button)).toBe("New workspace");
    expect(withAttr(button.children, "name").map((icon) => attrsOf(icon).name)).toEqual(["plus"]);
    expect(classTokensOf(button)).toEqual(MENU_ROW_CLASS.split(" "));
    (attrsOf(button).onclick as () => void)();
    expect(clicks).toBe(1);
  });

  it("lights up while it is the highlighted row, and can be disabled", () => {
    const highlight = new MenuHighlight();
    highlight.key = "new";

    const lit = row(highlight, { disabled: true });

    expect(attrsOf(lit)["data-highlighted"]).toBe("true");
    expect(classTokensOf(lit)).toContain("!bg-fill-hover");
    expect(attrsOf(lit).disabled).toBe(true);
  });
});

describe("menuCurrentCheck", () => {
  it("draws the check icon, which keeps its size in a row whose name is cut short", () => {
    const check = required(collectVnodes(menuCurrentCheck())[0]);

    expect(attrsOf(check).name).toBe("check");
    expect(String(attrsOf(check).extra).split(" ")).toContain("shrink-0");
  });
});

describe("the menus' rule and section heading", () => {
  it("draws the rule as a separator across the whole menu, past its padding", () => {
    const rule = required(collectVnodes(menuDivider())[0]);

    expect(attrsOf(rule).role).toBe("separator");
    expect(classTokensOf(rule)).toEqual(expect.arrayContaining(["-mx-1", "h-px"]));
  });

  it("draws a section heading helper-sized and muted", () => {
    const heading = required(collectVnodes(menuSectionHeading("Apps shared with you"))[0]);

    expect(allText(heading)).toBe("Apps shared with you");
    expect(classTokensOf(heading)).toEqual(expect.arrayContaining(["type-helper", "text-tertiary"]));
  });
});
