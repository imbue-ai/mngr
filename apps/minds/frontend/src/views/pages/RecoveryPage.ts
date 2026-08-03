// Machine recovery (/agents/<id>/recovery): the click-in destination for an
// unhealthy workspace. NEVER auto-navigated to -- the shell's health overlay
// banner links here. Shows the live health state (channel-driven), a manual
// Restart with the restart operation's log stream, SSH diagnostics, and a
// back-to-machine link. The legacy auto-redirects, redirect latches, and
// health-observation auto-restarts are deliberately gone; the one automatic
// dispatch left is ?intent=restart, the explicit stopped-machine
// click-through ("open this machine" implies "start it").

import m from "mithril";
import { getAppContext } from "../../app-context";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { CopyField, PageContainer } from "../components/Layout";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";
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

function _healthHeadline(health: string, isHostOffline: boolean): string {
  if (health === "restarting") {
    return isHostOffline ? "Bringing your machine back online..." : "Restarting your machine...";
  }
  if (health === "restart_failed") return "The last restart did not complete.";
  if (health === "stuck") return "This machine is not responding.";
  return "This machine looks healthy.";
}

export const RecoveryPage: m.Component<Record<string, never>, RecoveryState> = {
  oninit(vnode) {
    const workspaceAnyId = m.route.param("agentId");
    const isRestartIntent = m.route.param("intent") === "restart";
    const rawReturnTo = m.route.param("return_to") ?? "";
    // Same-app paths only ("/..." but not protocol-relative "//..."), so a
    // crafted deeplink cannot turn the return into an open redirect.
    vnode.state.returnTo = rawReturnTo.startsWith("/") && !rawReturnTo.startsWith("//") ? rawReturnTo : null;
    vnode.state.hasReturned = false;
    const model = new RecoveryModel(workspaceAnyId, browserLifecycleDeps(() => m.redraw()));
    vnode.state.model = model;
    void model.load().then(() => {
      // The stopped-machine click-through asks for the start to be
      // dispatched on arrival; load() already re-attached if a restart is
      // in flight, and dispatchRestart is a no-op while one is running.
      if (isRestartIntent && model.loadError === null) {
        void model.dispatchRestart();
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
    const info = model.info;
    return m(
      PageContainer,
      m("div", { class: "flex flex-col gap-4 pt-10 pb-10" }, [
        m("h1", { class: "type-heading-lg" }, info.workspace_name),
        m(Card, {}, [
          m("div", { class: "flex flex-col gap-3" }, [
            m("div", { class: "flex items-center gap-2 type-body text-primary" }, [
              model.isRestartRunning ? m(Spinner, { size: "sm" }) : null,
              m(
                "span",
                model.isRestartRunning
                  ? _healthHeadline("restarting", info.is_host_offline)
                  : _healthHeadline(info.health, info.is_host_offline),
              ),
            ]),
            model.isRestartSucceeded
              ? m(Notice, { variant: "success" }, "The machine restarted. You can head back to it now.")
              : null,
            model.restartError !== null ? m(Notice, { variant: "error" }, model.restartError) : null,
            !model.restartError && info.health_error && !model.isRestartRunning
              ? m(Notice, { variant: "warn" }, info.health_error)
              : null,
            m("div", { class: "flex items-center gap-2" }, [
              m(
                Button,
                {
                  variant: "primary",
                  disabled: model.isRestartRunning,
                  onclick: () => void model.dispatchRestart(),
                },
                model.isRestartRunning ? "Restarting..." : "Restart machine",
              ),
              m(
                Button,
                {
                  variant: "secondary",
                  onclick: () => m.route.set(`/workspace/${encodeURIComponent(info.agent_id)}`),
                },
                "Back to machine",
              ),
            ]),
            model.logLines.length > 0
              ? m(
                  "pre",
                  {
                    class:
                      "type-helper font-mono bg-fill-hover rounded-md p-3 max-h-64 overflow-y-auto whitespace-pre-wrap break-words",
                    onupdate(preVnode) {
                      const el = preVnode.dom;
                      el.scrollTop = el.scrollHeight;
                    },
                  },
                  model.logLines.join("\n"),
                )
              : null,
          ]),
        ]),
        info.ssh_command
          ? m(Card, {}, [
              m("div", { class: "flex flex-col gap-2" }, [
                m("div", { class: "type-heading" }, "Connect over SSH"),
                m(
                  "p",
                  { class: "type-helper text-tertiary" },
                  "For direct debugging, connect to the machine's host from a terminal:",
                ),
                m(CopyField, { value: info.ssh_command }),
              ]),
            ])
          : null,
      ]),
    );
  },
};
