// Destroy detail page (/destroying/<id>): status badge + live log tail from
// the type-segmented destroy operation resource, with Retry / Dismiss when
// the destroy failed. Routes home when the destroy completes.

import m from "mithril";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { PageContainer } from "../components/Layout";
import { Spinner } from "../components/Spinner";
import { StatusBadge } from "../components/StatusBadge";
import { browserLifecycleDeps, DestroyingModel } from "../../models/backups";

const REDIRECT_HOME_DELAY_MS = 800;

interface DestroyingState {
  model: DestroyingModel;
  redirectTimer: ReturnType<typeof setTimeout> | null;
}

export const DestroyingPage: m.Component<Record<string, never>, DestroyingState> = {
  oninit(vnode) {
    const agentId = m.route.param("agentId");
    const deps = browserLifecycleDeps(() => m.redraw());
    vnode.state.redirectTimer = null;
    vnode.state.model = new DestroyingModel(agentId, deps);
    vnode.state.model.onDone = () => {
      vnode.state.redirectTimer = setTimeout(() => m.route.set("/"), REDIRECT_HOME_DELAY_MS);
    };
    vnode.state.model.start();
  },
  onremove(vnode) {
    // The user may navigate away inside the redirect window; the pending
    // timer must not yank them home afterwards.
    if (vnode.state.redirectTimer !== null) clearTimeout(vnode.state.redirectTimer);
    vnode.state.model.stop();
  },
  view(vnode) {
    const { model } = vnode.state;
    return m(
      PageContainer,
      m("div", { class: "flex flex-col gap-4 pt-10 pb-10" }, [
        m("h1", { class: "type-heading-lg" }, "Destroying machine"),
        m("div", { class: "flex items-center gap-2" }, [
          model.status === "running"
            ? m("div", { class: "flex items-center gap-2" }, [m(Spinner, { size: "sm" }), m("span", { class: "text-primary type-body" }, "Running...")])
            : null,
          model.status === "failed" ? m(StatusBadge, { variant: "error" }, "Failed") : null,
          model.status === "done" ? m(StatusBadge, { variant: "success" }, "Done. Redirecting...") : null,
        ]),
        m(Card, { padding: "tight" }, [
          m(
            "pre",
            {
              class: "type-helper font-mono p-3 max-h-96 overflow-y-auto whitespace-pre-wrap break-words min-h-24",
              onupdate(preVnode) {
                const el = preVnode.dom;
                el.scrollTop = el.scrollHeight;
              },
            },
            model.logText,
          ),
        ]),
        model.status === "failed"
          ? m("div", { class: "flex items-center gap-2" }, [
              m(Button, { variant: "primary", onclick: () => void model.retry() }, "Retry"),
              m(Button, { variant: "secondary", onclick: () => void model.dismiss() }, "Dismiss"),
            ])
          : null,
      ]),
    );
  },
};
