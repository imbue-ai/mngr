// Machine recovery (/agents/<id>/recovery): the recovery card on a surface of
// its own, for a window with nothing of the machine on screen -- a cold entry,
// or a click into a machine from a list. Nothing is being preserved here, so
// navigating costs nothing, and the titlebar stays above it throughout (home,
// switcher and inbox remain reachable).
//
// When the machine IS on screen the same card renders as a modal instead
// (RecoveryModal). The card is shared wholesale; this page supplies no corner
// control where the modal supplies its X, and nothing else differs. There is
// nothing for one to do here: the page is reached only because the machine
// would not load, so a button back to it would name the destination that is
// known not to work, and the way onward once the machine does answer is the
// click-through's own ?return_to, honored below.
//
// NEVER auto-navigated to, and never linked to by the shell's band, which
// raises the modal instead. Unattended recovery is dispatched by the server's
// health tracker on the STUCK edge, so this page reports a restart already
// under way rather than racing to start one. The way in, and the only thing
// that dispatches from here, is a machines-list click-through:
// ?intent=start ("open this stopped machine" -- a plain, idempotent start) and
// ?intent=restart ("restart this machine" -- the full stop+start bounce).

import m from "mithril";
import { getAppContext } from "../../app-context";
import { PageContainer } from "../components/Layout";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";
import { RecoveryPanel } from "../recovery/RecoveryCard";
import { browserLifecycleDeps, RecoveryModel } from "../../models/backups";

interface RecoveryState {
  model: RecoveryModel;
  /** Validated same-app ?return_to destination, honored on restart success. */
  returnTo: string | null;
  hasReturned: boolean;
}

/** Follow a validated in-app return_to; /goto/ URLs enter the workspace
 * through the shell (routing a /goto path would dead-end on RouteError). */
function followReturnTo(returnTo: string): void {
  const gotoMatch = returnTo.match(/^\/goto\/((?:agent|host)-[a-f0-9]+)(?:[/?]|$)/i);
  if (gotoMatch) {
    getAppContext().shell.enterWorkspace(gotoMatch[1]);
    return;
  }
  m.route.set(returnTo);
}

export const RecoveryPage: m.Component<Record<string, never>, RecoveryState> = {
  oninit(vnode) {
    const workspaceAnyId = m.route.param("agentId");
    // Two distinct intents, because they cost different things. "Open this
    // stopped machine" wants only the idempotent start -- stopping a host that
    // is already stopped buys nothing and is what let a plain open bounce a
    // container. "Restart this machine" wants the full bounce it asked for.
    const intent = m.route.param("intent");
    const rawReturnTo = m.route.param("return_to") ?? "";
    // Same-app paths only ("/..." but not protocol-relative "//..."), so a
    // crafted deeplink cannot turn the return into an open redirect.
    vnode.state.returnTo = rawReturnTo.startsWith("/") && !rawReturnTo.startsWith("//") ? rawReturnTo : null;
    vnode.state.hasReturned = false;
    const model = new RecoveryModel(workspaceAnyId, browserLifecycleDeps(() => m.redraw()));
    vnode.state.model = model;
    void model.load().then(() => {
      // load() already re-attached if a restart is in flight, and
      // dispatchRestart is a no-op while one is running.
      if ((intent === "start" || intent === "restart") && model.loadError === null) {
        void model.dispatchRestart(intent === "start");
      }
    });
  },
  onremove(vnode) {
    vnode.state.model.stop();
  },
  onupdate(vnode) {
    // The click-through carried where the user was headed; once the restart
    // lands, take them there instead of parking them on the success notice.
    const { model, returnTo } = vnode.state;
    if (model.isRestartSucceeded && returnTo !== null && !vnode.state.hasReturned) {
      vnode.state.hasReturned = true;
      followReturnTo(returnTo);
    }
  },
  view(vnode) {
    const { model } = vnode.state;
    if (model.loadError !== null) {
      return m(
        PageContainer,
        m("div", { class: "flex flex-col gap-4 pt-10" }, [
          m("h1", { class: "type-heading-lg" }, "Machine recovery"),
          m(Notice, { variant: "error" }, model.loadError),
        ]),
      );
    }
    if (model.info === null) {
      return m(
        PageContainer,
        m("div", { class: "flex items-center gap-2 pt-10" }, [m(Spinner, { size: "sm" }), "Loading..."]),
      );
    }
    return m(
      "div",
      { class: "flex justify-center px-4 pt-10 pb-10" },
      m(RecoveryPanel, { panelId: "recovery-page-panel", model }),
    );
  },
};
