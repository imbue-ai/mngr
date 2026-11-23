// The first run's welcome (specs/ftue-onboarding-v2): "Welcome, <name>!" and whatever stands under it (the
// build's progress, then Continue). Drawn by the creation page while the workspace is built, and by the shell's
// cover over the workspace while it loads (views/shell/FirstRunCover.ts), so the two read as one screen.

import m from "mithril";
import { firstNameOf, firstRun, welcomeHeading } from "../../../models/firstRun";

export const WORKSPACE_READY_LINE = "Your workspace is ready!";
export const CONTINUE_LABEL = "Continue";

export interface FirstRunWelcomeAttrs {
  /** The element's id: the creation page's is `creating`. */
  id: string;
  attemptId: string;
  /** What stands under the greeting. */
  status: m.Children;
}

/** The welcome, whole. */
export function firstRunWelcome(attrs: FirstRunWelcomeAttrs): m.Children {
  return m(
    "div",
    {
      id: attrs.id,
      "data-agent-id": attrs.attemptId,
      "data-first-run": "true",
      class: "first-run flex min-h-full w-full flex-col items-center justify-center px-6 py-16",
    },
    [
      m(
        "h1",
        { id: "creating-welcome", class: "first-run-headline text-center text-[40px]" },
        welcomeHeading(firstNameOf(firstRun.account)),
      ),
      // One height for whatever stands here (progress, opening, Continue), so the greeting above it never moves
      // as one gives way to the next.
      m("div", { class: "flex min-h-[120px] w-full flex-col items-center" }, attrs.status),
    ],
  );
}

/** The thin bar under its line, as wide as the line, while something is still being readied, with anything more
 * under them. */
export function settingUpStatus(line: string, percent: number, extra: m.Children[] = []): m.Children {
  return m("div", { class: "mt-10 flex flex-col items-center" }, [
    m("div", { class: "flex w-fit flex-col" }, [
      m("p", { id: "creating-setting-up", class: "text-[17px]" }, line),
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

/** "Your workspace is ready!" and Continue. */
export function readyStatus(onContinue: () => void): m.Children {
  return m("div", { class: "mt-10 flex flex-col items-center gap-4" }, [
    m("p", { id: "creating-ready", class: "text-[18px]" }, WORKSPACE_READY_LINE),
    m(
      "button",
      {
        id: "creating-continue",
        type: "button",
        class: "first-run-button type-heading",
        onclick: onContinue,
      },
      CONTINUE_LABEL,
    ),
  ]);
}
