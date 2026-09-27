// The /popout/<workspace-id>/<window-id> route: a pulled-out workspace
// window in a desktop window of its own. The Shell mounts the workspace
// surface (with the workspace shell in solo mode) and the popout bar for this
// route itself, so the routed page renders nothing.

import m from "mithril";

export const PopoutPage: m.Component = {
  view() {
    return null;
  },
};
