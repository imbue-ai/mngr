// The information icon beside a scope's permissions, for what every grant under
// it carries without being a row: the protocol handshake (an MCP session, a
// JMAP session document) that no client can skip. It is not a toggle -- it
// cannot be turned off on its own, and goes with the last chosen permission --
// so it is not drawn as one; the icon's tooltip is how the user finds out it is
// there.

import m from "mithril";
import { Icon16 } from "./Icon";

export const IMPLICIT_PERMISSIONS_HINT_ATTR = "data-implicit-permissions-hint";

export function renderImplicitPermissionsHint(note: string): m.Children {
  if (note === "") return null;
  return m(
    "span",
    {
      class: "inline-flex shrink-0 items-center text-tertiary cursor-help",
      tabindex: 0,
      role: "img",
      "aria-label": note,
      "data-tooltip": note,
      [IMPLICIT_PERMISSIONS_HINT_ATTR]: "",
    },
    m(Icon16, { name: "info", size: "sm" }),
  );
}
