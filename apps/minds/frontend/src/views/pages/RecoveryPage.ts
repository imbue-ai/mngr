// Machine recovery (/agents/<id>/recovery): the recovery card on a surface of
// its own, for a window with nothing of the machine on screen -- a cold entry,
// or a click into a machine from a list. Nothing is being preserved here, so
// navigating costs nothing, and the titlebar stays above it throughout (home,
// switcher and inbox remain reachable).
//
// When the machine IS on screen the same card renders as a modal instead
// (RecoveryModal). The card is shared wholesale; this page supplies no corner
// control where the modal supplies its X, and nothing else differs. There is
// nothing for a corner control to do here: the page is reached only because the
// machine would not load, so a dismissal would leave the reader on a machine
// that is not there.
//
// Once the machine IS answering, though, the page is the only thing between the
// reader and it, and it supplies the card's "Open machine" button. That is the
// same moment the ?return_to below fires, and deliberately so: the redirect is
// the path, and the button is what is left when there is no ?return_to to
// follow (a hand-typed or stale URL) or when the redirect is held back -- and
// it costs nothing when the redirect does fire, since the two land in the same
// place and the reader never sees the card again either way.
//
// NEVER auto-navigated to, and never linked to by the shell's band, which
// raises the modal instead. Unattended recovery is dispatched by the server's
// health tracker on the STUCK edge, so this page reports a recovery already
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
import type { RecoveryKind } from "../../models/health";

interface RecoveryState {
  model: RecoveryModel;
  /** Validated same-app ?return_to destination, honored once the machine answers. */
  returnTo: string | null;
  hasReturned: boolean;
  /** Whether the click-through's own recovery has been asked for (immediately
   * true when it carried no ?intent). Until it has, the machine's health is a
   * reading of the state that recovery was meant to change, so returning on it
   * would leave without doing the thing the click-through came to do. */
  isDispatchSettled: boolean;
}

/**
 * Whether the machine is answering again, and this page is therefore done.
 *
 * Deliberately NOT "the recovery succeeded". A recovery can fail while the
 * machine comes back anyway -- the failure and the machine answering have
 * separate causes, and this page's own state poll is what reports the second
 * one. Read the other way, a page whose recovery errored parked the reader on
 * a card saying "nothing further is needed here" with no way into the machine
 * it was saying it about.
 */
function isMachineAnswering(model: RecoveryModel): boolean {
  if (model.isRecoveryRunning) return false;
  if (model.isRecoverySucceeded) return true;
  const info = model.info;
  // A stopped machine reads healthy (there is nothing wrong with it); it is
  // simply not somewhere anyone can be sent.
  return info !== null && info.health === "healthy" && !info.is_host_offline;
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

/** Leave for the machine: where the click-through was headed, or the machine
 * itself when it carried nowhere. One path for the automatic return and the
 * button, so a reader who clicks rather than waiting lands in the same place. */
function leaveForMachine(state: RecoveryState): void {
  state.hasReturned = true;
  if (state.returnTo !== null) {
    followReturnTo(state.returnTo);
    return;
  }
  const agentId = state.model.agentId;
  if (agentId !== null) getAppContext().shell.enterWorkspace(agentId);
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
    const dispatchKind: RecoveryKind | null = intent === "start" || intent === "restart" ? intent : null;
    vnode.state.isDispatchSettled = dispatchKind === null;
    const model = new RecoveryModel(workspaceAnyId, browserLifecycleDeps(() => m.redraw()));
    vnode.state.model = model;
    void model.load().then(async () => {
      // load() already re-attached if a recovery is in flight, and
      // dispatchRecovery is a no-op while one is running.
      if (dispatchKind !== null && model.loadError === null) {
        await model.dispatchRecovery(dispatchKind);
      }
      vnode.state.isDispatchSettled = true;
    });
  },
  onremove(vnode) {
    vnode.state.model.stop();
  },
  onupdate(vnode) {
    // The click-through carried where the user was headed; once the machine
    // answers, take them there instead of parking them on the card.
    const { model, returnTo } = vnode.state;
    if (returnTo === null || vnode.state.hasReturned || !vnode.state.isDispatchSettled) return;
    // Not out from under a bug report opened over this page: the reader is
    // mid-sentence in a form about this machine, and this page is what the
    // form is floating on.
    if (getAppContext().shell.pageRouteBehindOverlay !== null) return;
    if (!isMachineAnswering(model)) return;
    leaveForMachine(vnode.state);
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
      m(RecoveryPanel, {
        panelId: "recovery-page-panel",
        model,
        // Withheld until the machine answers, for the reason the module
        // comment gives: before that, this button would name a destination
        // known not to work.
        onEnterMachine: isMachineAnswering(model) ? () => leaveForMachine(vnode.state) : null,
      }),
    );
  },
};
