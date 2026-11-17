import m from "mithril";
import { Spinner } from "./Spinner";
import { joinClasses, splitAttrs } from "./attrs";

/** A home badge's tone, one meaning each (specs/home-screen-redesign, 4.13):
 * alarm is broken and unusable now, warn is something for the user to do,
 * neutral is information or a state in progress. */
export type BadgeTone = "neutral" | "warn" | "alarm";

interface HomeBadgeAttrs extends m.Attributes {
  tone: BadgeTone;
  /** In progress: the one badge decoration, a small spinner before the words. */
  isSpinning?: boolean;
  extra?: string;
}

const OWN_KEYS = ["tone", "isSpinning", "extra"] as const;

export function homeBadgeClass(tone: BadgeTone, extra: string): string {
  return joinClasses(
    "inline-flex items-center gap-1 shrink-0 rounded px-1 py-0.5 text-xs font-medium",
    tone === "alarm" && "bg-important text-white",
    tone === "warn" && "bg-[var(--c-warning)] text-white",
    tone === "neutral" && "titlebar-surface status-chip text-primary",
    extra,
  );
}

/** One status on a card: solid red or orange, or the frosted white chip that reads on any wallpaper. */
export function HomeBadge(): m.Component<HomeBadgeAttrs> {
  return {
    view(vnode) {
      const { tone, isSpinning = false, extra = "" } = vnode.attrs;
      return m(
        "span",
        { class: homeBadgeClass(tone, extra), "data-badge-tone": tone, ...splitAttrs(vnode.attrs, OWN_KEYS) },
        [isSpinning ? m(Spinner, { size: "sm" }) : null, vnode.children],
      );
    },
  };
}
