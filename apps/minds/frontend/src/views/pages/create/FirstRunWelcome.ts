// The creation page's one layout (specs/onboarding-tweaks): its heading and whatever stands under it (the build's
// progress or its failure, then that the workspace is ready). Drawn by the creation page while the workspace is
// built, and by the shell's cover over the workspace while it loads (views/shell/FirstRunCover.ts), so the two read
// as one screen.

import m from "mithril";

export const WORKSPACE_READY_LINE = "Your workspace is ready!";

export interface FirstRunWelcomeAttrs {
  /** The element's id: the creation page's is `creating`. */
  id: string;
  attemptId: string;
  heading: string;
  /** What stands under the heading. */
  status: m.Children;
  /** Keyed vnodes drawn over the page (the retry form's modal). */
  overlays?: m.Children[];
}

/** The welcome, whole. */
export function firstRunWelcome(attrs: FirstRunWelcomeAttrs): m.Children {
  return m(
    "div",
    {
      id: attrs.id,
      "data-agent-id": attrs.attemptId,
      "data-first-run": "true",
      class: "first-run light flex min-h-full w-full flex-col items-center justify-center px-6 py-16",
    },
    // Every child is keyed: Mithril rejects a fragment that mixes keyed vnodes with unkeyed ones, and the
    // overlays come and go.
    [
      m(
        "h1",
        { key: "heading", id: "creating-welcome", class: "first-run-headline text-center text-[40px]" },
        attrs.heading,
      ),
      // One height for whatever stands here (progress, opening, ready), so the heading above it never moves
      // as one gives way to the next.
      m("div", { key: "status", class: "flex min-h-[120px] w-full flex-col items-center" }, attrs.status),
      ...(attrs.overlays ?? []),
    ],
  );
}

/** The thin bar under its line, as wide as the line (an empty `line` draws the bar alone, at its least width),
 * while something is still being readied, with anything more under them. */
export function settingUpStatus(line: string, percent: number, extra: m.Children[] = []): m.Children {
  return m("div", { class: "mt-10 flex flex-col items-center" }, [
    m("div", { class: "flex w-fit min-w-[280px] flex-col" }, [
      line === "" ? null : m("p", { id: "creating-setting-up", class: "text-[17px]" }, line),
      m(
        "div",
        { class: "first-run-bar mt-4 h-1 w-full overflow-hidden rounded-full" },
        m("div", {
          id: "creating-welcome-bar",
          class: "h-full rounded-full transition-[width] duration-300 ease-out",
          style: `width: ${percent.toFixed(1)}%`,
        }),
      ),
    ]),
    ...extra,
  ]);
}

/** "Your workspace is ready!", standing for a beat before the welcome gives way to the workspace. */
export function readyStatus(): m.Children {
  return m("div", { class: "mt-10 flex flex-col items-center" }, [
    m("p", { id: "creating-ready", class: "text-lg" }, WORKSPACE_READY_LINE),
  ]);
}
