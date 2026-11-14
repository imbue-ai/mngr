// Destroy detail page (/destroying/<id>): status + live log tail from the
// type-segmented destroy operation resource. A finished removal whose
// workspace is not yet seen gone keeps checking; a failed one leads with our
// sentence, offers Retry / Dismiss and keeps the command output behind See
// details. Routes home when the destroy completes.

import m from "mithril";
import { getAppContext } from "../../app-context";
import { Card } from "../components/Card";
import { ErrorNotice } from "../components/ErrorDisplay";
import { PageContainer } from "../components/Layout";
import { Spinner } from "../components/Spinner";
import { StatusBadge } from "../components/StatusBadge";
import { browserLifecycleDeps, DestroyingModel } from "../../models/backups";
import type { ErrorAction } from "../../models/errors";
import type { DestroyRefusal } from "../../models/workspaceOptions";
import { destroyRefusalActions } from "./lifecycle/destroy-refusal";

const DESTROY_FAILED_SENTENCE =
  "Removing this workspace didn't finish. You can try again, or dismiss this and remove it later.";
const DESTROY_CONFIRMING_SENTENCE = "Removal finished. Imbue Studio is checking that the workspace is gone.";
const LOG_UNAVAILABLE_SENTENCE = "The details of this removal aren't available.";

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
      vnode.state.redirectTimer = setTimeout(
        () => getAppContext().shell.routeToHub(),
        REDIRECT_HOME_DELAY_MS,
      );
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
        m("h1", { class: "type-heading-lg" }, "Destroying workspace"),
        model.status === "failed" ? failedNotice(model) : statusLine(model),
        model.status === "failed" ? null : logBox(model),
      ]),
    );
  },
};

function statusLine(model: DestroyingModel): m.Children {
  return m("div", { class: "flex items-center gap-2" }, [
    model.status === "running"
      ? m("div", { class: "flex items-center gap-2" }, [
          m(Spinner, { size: "sm" }),
          m("span", { class: "text-primary type-body" }, "Running..."),
        ])
      : null,
    model.status === "confirming"
      ? m("div", { id: "destroy-confirming", class: "flex items-center gap-2" }, [
          m(Spinner, { size: "sm" }),
          m("span", { class: "text-primary type-body" }, DESTROY_CONFIRMING_SENTENCE),
        ])
      : null,
    model.status === "done" ? m(StatusBadge, { variant: "success" }, "Done. Redirecting...") : null,
  ]);
}

function logBox(model: DestroyingModel): m.Children {
  return m(Card, { padding: "tight" }, [
    m(
      "pre",
      {
        class: "type-helper font-mono p-3 max-h-96 overflow-y-auto whitespace-pre-wrap break-words min-h-24",
        onupdate(preVnode) {
          const el = preVnode.dom;
          el.scrollTop = el.scrollHeight;
        },
      },
      model.isLogUnavailable ? LOG_UNAVAILABLE_SENTENCE : model.logText,
    ),
  ]);
}

function failedNotice(model: DestroyingModel): m.Children {
  const retryFailure = model.retryFailure;
  const retry = { label: "Retry", onclick: () => void model.retry() };
  const dismiss = {
    label: "Dismiss",
    isDismissal: true,
    onclick: () => void model.dismiss(),
  };
  return [
    m(ErrorNotice, {
      id: "destroy-failed",
      error: { message: DESTROY_FAILED_SENTENCE, details: model.logText },
      // A refused retry carries the buttons, led by what its refusal leaves to do,
      // and the page's one Report a bug when it asks for one.
      actions: retryFailure === null ? [retry, dismiss] : [],
      isBugReportOffered: retryFailure === null || !retryFailure.isBugReportOffered,
      workspaceAgentId: model.agentId,
    }),
    retryFailure !== null ? retryFailureNotice(model, retryFailure, retry, dismiss) : null,
  ];
}

function retryFailureNotice(
  model: DestroyingModel,
  retryFailure: DestroyRefusal,
  retry: ErrorAction,
  dismiss: ErrorAction,
): m.Children {
  // Dismissing the failed removal is what takes the page back to the workspaces.
  const remedies = destroyRefusalActions(retryFailure, {
    retry,
    backToWorkspaces: dismiss.onclick,
  });
  return m(ErrorNotice, {
    id: "destroy-retry-error",
    error: retryFailure.error,
    isBugReportOffered: retryFailure.isBugReportOffered,
    workspaceAgentId: model.agentId,
    actions: retryFailure.remedy === "back-to-workspaces" ? remedies : [...remedies, dismiss],
  });
}
