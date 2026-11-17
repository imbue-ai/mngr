import m from "mithril";
import { Avatar } from "./Avatar";
import { MonogramTile } from "./MonogramTile";
import { joinClasses, splitAttrs } from "./attrs";

export interface HomeAppIconOwner {
  label: string;
  pictureUrl: string | null;
}

interface HomeAppIconAttrs extends m.Attributes {
  name: string;
  /** The line under the name: the workspace an own app belongs to. */
  subtitle?: string;
  /** The served icon, or null for a monogram of the name. */
  iconUrl: string | null;
  /** The sharer, whose face sits on the tile's corner with a "From" tooltip. */
  owner?: HomeAppIconOwner;
  /** Faded and inert, with `title` saying why. */
  isDisabled?: boolean;
  title?: string;
  onOpen: () => void;
  /** Rendered over the tile's top-right corner (a card menu). */
  corner?: m.Children;
}

const OWN_KEYS = ["name", "subtitle", "iconUrl", "owner", "isDisabled", "title", "onOpen", "corner"] as const;

interface HomeAppIconState {
  /** The icon URL that failed to load, so the monogram stands in for it until the icon's version changes. */
  brokenIconUrl: string | null;
}

/**
 * One app as a desktop shortcut (specs/home-screen-redesign, 4.8): the app's
 * icon tile, or a monogram, the name under it, and, for a shared app, the
 * sharer's face on the tile's corner. An icon that fails to load becomes the
 * monogram until the app registers another (a new URL).
 */
export function HomeAppIcon(): m.Component<HomeAppIconAttrs, HomeAppIconState> {
  let state: HomeAppIconState = { brokenIconUrl: null };
  return {
    view(vnode) {
      const { name, subtitle, iconUrl, owner, isDisabled = false, title, onOpen, corner } = vnode.attrs;
      const tileClass =
        "size-16 rounded-[20px] shadow-[0_2px_6px_0_rgb(0_0_0/0.2),0_1px_2px_0_rgb(0_0_0/0.16)] transition-transform group-hover:scale-105";
      const tile =
        iconUrl !== null && iconUrl !== state.brokenIconUrl
          ? m("img", {
              src: iconUrl,
              alt: "",
              draggable: false,
              class: tileClass,
              onerror: () => {
                state = { brokenIconUrl: iconUrl };
              },
            })
          : m(MonogramTile, { name, extra: tileClass + " text-xl" });
      return m(
        "div",
        { class: "relative flex w-24 flex-col items-center", ...splitAttrs(vnode.attrs, OWN_KEYS) },
        [
          m(
            "button",
            {
              type: "button",
              "aria-label": owner === undefined ? name : `${name}, from ${owner.label}`,
              "aria-disabled": isDisabled ? "true" : undefined,
              title,
              onclick: () => {
                if (!isDisabled) onOpen();
              },
              class: joinClasses(
                "group flex w-24 flex-col items-center gap-2 text-center border-0 bg-transparent p-0 outline-none",
                isDisabled ? "opacity-40 cursor-default" : "cursor-pointer",
              ),
            },
            [
              m("span", { class: "relative" }, [
                tile,
                owner === undefined
                  ? null
                  : m(
                      "span",
                      { class: "absolute -bottom-1 -right-1 rounded-full bg-surface-primary p-0.5" },
                      m(Avatar, { src: owner.pictureUrl, label: owner.label, size: 20 }),
                    ),
                owner === undefined
                  ? null
                  : m(
                      "span",
                      {
                        "aria-hidden": "true",
                        class:
                          "pointer-events-none absolute bottom-full left-1/2 z-[110] mb-1.5 inline-flex -translate-x-1/2 items-center gap-1 whitespace-nowrap rounded-sm bg-surface-inverse px-1.5 py-0.5 text-xs text-inverse-primary opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100",
                      },
                      ["From", m(Avatar, { src: owner.pictureUrl, label: owner.label, size: 14 }), owner.label],
                    ),
              ]),
              m("span", { class: "flex flex-col items-center gap-0.5" }, [
                m("span", { class: "type-body font-medium text-primary line-clamp-2 leading-tight" }, name),
                subtitle ? m("span", { class: "type-helper text-tertiary truncate max-w-24" }, subtitle) : null,
              ]),
            ],
          ),
          corner === undefined ? null : m("div", { class: "absolute -top-1 right-1" }, corner),
        ],
      );
    },
  };
}
