// What the bar's two menus (the account menu and the workspace menu) share:
// one highlight that the arrow keys and the mouse both move, the mark of the
// row for what the window already shows, the row and rule classes, and where
// a menu hangs from its button.

import m from "mithril";
import { Icon16 } from "../components/Icon";
import type { IconName } from "../components/icons";
import { readAnchorRect } from "./RaisedTitlebarIcons";

export const MENU_MIN_WIDTH_PX = 260;

// As tall as the window allows, then it scrolls; its rows and rules never shrink to fit (squeezed, a rule vanishes
// and a two-line row overlaps). Its top-left corner is square, where it joins the raised button it hangs from.
export const MENU_CLASS =
  "absolute flex flex-col gap-0.5 p-1 bg-surface-primary text-primary shadow-overlay overflow-y-auto " +
  "max-h-[calc(100vh-var(--titlebar-height)-12px)] rounded-lg rounded-tl-none outline-none [&>*]:shrink-0";

export const MENU_ROW_CLASS =
  "flex w-full items-center gap-2 min-h-8 px-2 py-1 rounded-md type-body text-left text-primary border-0 " +
  "bg-transparent cursor-pointer disabled:cursor-default";

/** The check at the right end of the row for what the window already shows. */
export function menuCurrentCheck(): m.Children {
  return m(Icon16, { name: "check", extra: "shrink-0 text-secondary" });
}

/** Where a menu hangs: straight from its button, left edges lined up, and at least as wide as it. `fallbackLeft`
 * is where it goes with no button to hang from. */
export function menuStyle(
  anchorId: string,
  options: { isFittedToContent?: boolean; fallbackLeft?: number } = {},
): string {
  const rect = readAnchorRect(anchorId);
  const left = rect?.x ?? options.fallbackLeft ?? 80;
  const top = rect === null ? "var(--titlebar-height)" : `${rect.y + rect.height}px`;
  const least = Math.max(MENU_MIN_WIDTH_PX, rect?.width ?? 0);
  return options.isFittedToContent === true
    ? `left: ${left}px; top: ${top}; width: max-content; min-width: ${least}px; max-width: calc(100vw - ${left}px - 8px);`
    : `left: ${left}px; top: ${top}; width: ${least}px;`;
}

/** A menu's rule: across the whole menu, past its padding, with 8px clear above and below. */
export function menuDivider(): m.Children {
  return m("div", { class: "-mx-1 my-1.5 h-px bg-subtle", role: "separator" });
}

export function menuSectionHeading(text: string): m.Children {
  return m("p", { class: "type-helper text-tertiary px-2 pt-1 pb-0.5" }, text);
}

/**
 * One menu's highlight. The mouse and the arrow keys move the same one, so a menu never shows two (a hover and a
 * keyboard highlight); only a real pointer move counts, so a row that slides under a still pointer as the list
 * narrows is not highlighted by that.
 */
export class MenuHighlight {
  key: string | null = null;

  /** The attributes that put a row in the arrow-key order. */
  rowAttrs(navKey: string): Record<string, unknown> {
    return {
      "data-nav-key": navKey,
      ...(navKey === this.key ? { "data-highlighted": "true" } : {}),
      onmousemove: (event: MouseEvent & { redraw?: boolean }) => {
        if (this.key === navKey) event.redraw = false;
        else this.key = navKey;
      },
    };
  }

  /** `base`, lit when the row is the highlighted one. The row for what the window already shows (`isCurrent`)
   * otherwise wears a fainter fill, so it never reads as a second highlight. */
  rowClass(base: string, navKey: string, options: { isCurrent?: boolean } = {}): string {
    if (navKey === this.key) return `${base} !bg-fill-hover`;
    return options.isCurrent === true ? `${base} !bg-fill-subtle` : base;
  }

  /** Move through the menu's enabled rows in the order they are drawn: down from none to the first, up from the
   * first back to none. */
  move(menu: Element, direction: 1 | -1): void {
    const keys = [...menu.querySelectorAll<HTMLElement>("[data-nav-key]:not([disabled])")].map(
      (row) => row.dataset.navKey ?? "",
    );
    if (keys.length === 0) return;
    const at = this.key === null ? -1 : keys.indexOf(this.key);
    const next = Math.min(keys.length - 1, at + direction);
    this.key = next < 0 ? null : keys[next];
  }

  /** Click the highlighted row, reporting whether there was one. */
  activate(menu: Element): boolean {
    if (this.key === null) return false;
    const row = [...menu.querySelectorAll<HTMLElement>("[data-nav-key]")].find(
      (candidate) => candidate.dataset.navKey === this.key,
    );
    if (row === undefined) return false;
    row.click();
    return true;
  }

  /** The arrow keys and Enter, for a menu element's own keydown. Returns whether the key was the menu's. */
  handleKey(event: KeyboardEvent, menu: Element): boolean {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      this.move(menu, event.key === "ArrowDown" ? 1 : -1);
      return true;
    }
    if (event.key === "Enter" && this.activate(menu)) {
      event.preventDefault();
      return true;
    }
    return false;
  }
}

/** An icon-and-label row that does something other than choose from the list. */
export function menuActionRow(
  highlight: MenuHighlight,
  options: {
    id: string;
    navKey: string;
    icon: IconName;
    label: string;
    onclick: () => void;
    disabled?: boolean;
  },
): m.Children {
  return m(
    "button",
    {
      type: "button",
      role: "menuitem",
      id: options.id,
      disabled: options.disabled === true,
      ...highlight.rowAttrs(options.navKey),
      class: highlight.rowClass(MENU_ROW_CLASS, options.navKey),
      onclick: options.onclick,
    },
    [m(Icon16, { name: options.icon, extra: "shrink-0" }), m("span", { class: "flex-1 truncate" }, options.label)],
  );
}
