import m from "mithril";
import { Icon16 } from "./Icon";
import type { IconName } from "./icons";
import { joinClasses } from "./attrs";

export interface CardMenuFact {
  label: string;
  value: string;
  /** Rendered in the important tone (a failed backup, say). */
  isImportant?: boolean;
}

export interface CardMenuItem {
  icon: IconName;
  label: string;
  onSelect: () => void;
  /** Shown but inert. */
  isDisabled?: boolean;
  /** Shown on hover: why an inert item is inert. */
  title?: string;
  /** A `data-*` hook for tests and the e2e drivers, besides the label. */
  id?: string;
}

interface CardMenuAttrs extends m.Attributes {
  /** The rows above the divider: the facts the card no longer carries. */
  facts?: readonly CardMenuFact[];
  items: readonly CardMenuItem[];
  /** The "..." button's accessible name. */
  label?: string;
  /** Classes for the "..." button; the card's icon-action look by default. */
  buttonExtra?: string;
}

interface CardMenuState {
  isOpen: boolean;
  onDocumentDown: ((event: MouseEvent) => void) | null;
  onDocumentKey: ((event: KeyboardEvent) => void) | null;
}

/** The icon-action look a card's buttons share: the card's text colour at 20% on hover, strong enough for a photo. */
export const CARD_ICON_BUTTON_CLASS =
  "group relative inline-flex size-7 shrink-0 items-center justify-center rounded-md text-primary border-0 bg-transparent " +
  "cursor-pointer hover:bg-[color-mix(in_srgb,var(--c-text-primary)_20%,transparent)]";

/**
 * A card's "..." menu (specs/home-screen-redesign, 4.9): a popover to the right
 * of the button, closed by a click outside, Escape, or choosing an item. Every
 * click stops at the menu, so the card behind it never opens.
 */
export function CardMenu(): m.Component<CardMenuAttrs, CardMenuState> {
  let state: CardMenuState = { isOpen: false, onDocumentDown: null, onDocumentKey: null };

  function detach(): void {
    if (state.onDocumentDown !== null) document.removeEventListener("mousedown", state.onDocumentDown);
    if (state.onDocumentKey !== null) document.removeEventListener("keydown", state.onDocumentKey);
    state = { ...state, onDocumentDown: null, onDocumentKey: null };
  }

  function close(): void {
    state = { ...state, isOpen: false };
    detach();
  }

  function open(root: Element | null): void {
    state = { ...state, isOpen: true };
    if (typeof document === "undefined") return;
    const onDocumentDown = (event: MouseEvent) => {
      if (root !== null && !root.contains(event.target as Node)) {
        close();
        m.redraw();
      }
    };
    const onDocumentKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        close();
        m.redraw();
      }
    };
    document.addEventListener("mousedown", onDocumentDown);
    document.addEventListener("keydown", onDocumentKey);
    state = { ...state, onDocumentDown, onDocumentKey };
  }

  return {
    onremove() {
      detach();
    },
    view(vnode) {
      const { facts = [], items, label = "More", buttonExtra = "" } = vnode.attrs;
      return m(
        "div",
        {
          class: "relative shrink-0",
          "data-card-menu": state.isOpen ? "open" : "closed",
          onclick: (event: MouseEvent) => event.stopPropagation(),
        },
        [
          m(
            "button",
            {
              type: "button",
              "aria-label": label,
              "aria-expanded": state.isOpen ? "true" : "false",
              "data-tooltip": state.isOpen ? undefined : label,
              class: joinClasses(
                CARD_ICON_BUTTON_CLASS,
                state.isOpen && "bg-[color-mix(in_srgb,var(--c-text-primary)_20%,transparent)]",
                buttonExtra,
              ),
              onclick: (event: MouseEvent) => {
                event.stopPropagation();
                if (state.isOpen) close();
                else open((event.currentTarget as Element | null)?.parentElement ?? null);
              },
            },
            m(Icon16, { name: "more" }),
          ),
          state.isOpen
            ? m(
                "div",
                {
                  role: "menu",
                  "aria-label": label,
                  class:
                    "dark absolute left-full top-0 ml-1 w-[220px] z-10 flex flex-col gap-0.5 p-1 rounded-lg border border-subtle bg-surface-primary text-primary shadow-overlay",
                },
                [
                  facts.length > 0
                    ? m(
                        "dl",
                        {
                          class:
                            "grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 px-2 pt-1.5 pb-2 mb-0.5 border-b border-subtle type-helper",
                        },
                        facts.flatMap((fact) => [
                          m("dt", { class: "text-tertiary" }, fact.label),
                          m(
                            "dd",
                            { class: fact.isImportant ? "font-medium text-important" : "text-secondary" },
                            fact.value,
                          ),
                        ]),
                      )
                    : null,
                  items.map((item) =>
                    m(
                      "button",
                      {
                        type: "button",
                        role: "menuitem",
                        "aria-disabled": item.isDisabled ? "true" : undefined,
                        title: item.title,
                        "data-menu-item": item.id ?? item.label,
                        class: joinClasses(
                          "flex w-full items-center gap-2 h-8 px-2 rounded-md type-body text-primary text-left border-0 bg-transparent",
                          item.isDisabled ? "opacity-40 cursor-default" : "cursor-pointer hover:bg-fill-hover",
                        ),
                        onclick: (event: MouseEvent) => {
                          event.stopPropagation();
                          if (item.isDisabled) return;
                          close();
                          item.onSelect();
                        },
                      },
                      [
                        m(Icon16, { name: item.icon, extra: "shrink-0 text-secondary" }),
                        m("span", { class: "truncate" }, item.label),
                      ],
                    ),
                  ),
                ],
              )
            : null,
        ],
      );
    },
  };
}
