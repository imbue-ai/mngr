// The live start page as its suites drive it, without a DOM. Deliberately NOT
// named *.test.ts so vitest does not collect it, the same rule src/testing.ts
// follows. Each suite mocks the sign-in module itself.

import type m from "mithril";
import { startFlow } from "../../models/startFlow";
import { attrsOf, collectVnodes } from "../../testing";
import { StartPage } from "./StartPage";

/** A fresh, initialized start page: `view` renders it, `update` runs its onupdate hook. */
export function renderStartPage(): { view: () => unknown; update: () => void } {
  const component = (StartPage as unknown as (vnode: unknown) => m.Component)({});
  (component.oninit as () => void)();
  return {
    view: (): unknown => (component.view as () => unknown)(),
    update: (): void => (component.onupdate as () => void)(),
  };
}

/** Press the last element whose `name` attribute is `value`: the open question's, when answered ones match too. */
export function pressLast(tree: unknown, name: string, value: string): void {
  const matches = collectVnodes(tree).filter((candidate) => attrsOf(candidate)[name] === value);
  const vnode = matches[matches.length - 1];
  if (vnode === undefined) throw new Error(`nothing with ${name}=${value}`);
  (attrsOf(vnode).onclick as () => void)();
}

/** Press the title bar's log-in, as the start flow offers it for its whole length. */
export function pressTitlebarLogIn(): void {
  if (startFlow.logInExisting === null) throw new Error("no start page offers the title bar's log-in");
  startFlow.logInExisting();
}
