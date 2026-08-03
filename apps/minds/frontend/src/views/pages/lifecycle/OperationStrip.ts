// The tracked-backup-operation strip: spinner + latest progress line for
// strip-driven operations (update / configure), the terminal notices
// (error / warning / success / cancelled), the failure-specific retry
// buttons, Cancel while the backend still allows it, and the collapsible
// full-output log. A restore reports on its snapshot row instead, so the
// strip shows only its notices and log.

import m from "mithril";
import { Button } from "../../components/Button";
import { Notice } from "../../components/Notice";
import { Spinner } from "../../components/Spinner";
import type { BackupOperationController } from "../../../models/backups";

export interface OperationStripAttrs {
  controller: BackupOperationController;
}

interface StripState {
  isLogShown: boolean;
}

export function OperationStrip(): m.Component<OperationStripAttrs, StripState> {
  return {
    oninit(vnode) {
      vnode.state.isLogShown = false;
    },
    view(vnode) {
      const { controller } = vnode.attrs;
      const isStripSpinnerShown = controller.isRunning && !controller.isRestore;
      const hasLog = controller.logLines.length > 0;
      const isAnythingShown =
        isStripSpinnerShown ||
        controller.errorMessage !== null ||
        controller.warningMessage !== null ||
        controller.successMessage !== null ||
        controller.cancelledMessage !== null ||
        controller.isStopChatsRetryOffered ||
        controller.isSkipSafetyRetryOffered ||
        controller.isForceRetryOffered ||
        hasLog;
      if (!isAnythingShown) return null;
      return m("div", { class: "flex flex-col gap-2 my-2" }, [
        isStripSpinnerShown
          ? m("div", { class: "flex items-center gap-2 type-body text-secondary" }, [
              m(Spinner, { size: "sm" }),
              m("span", controller.runningLabel || "Working..."),
            ])
          : null,
        isStripSpinnerShown && controller.progressLine !== null
          ? m("div", { class: "type-helper text-tertiary truncate" }, controller.progressLine)
          : null,
        controller.errorMessage !== null ? m(Notice, { variant: "error" }, controller.errorMessage) : null,
        controller.successMessage !== null ? m(Notice, { variant: "success" }, controller.successMessage) : null,
        controller.warningMessage !== null ? m(Notice, { variant: "warn" }, controller.warningMessage) : null,
        controller.cancelledMessage !== null ? m(Notice, { variant: "info" }, controller.cancelledMessage) : null,
        m("div", { class: "flex items-center gap-2 flex-wrap" }, [
          controller.isStopChatsRetryOffered
            ? m(
                Button,
                { variant: "secondary", disabled: controller.isRunning, onclick: () => controller.runStopChatsRetry() },
                "Stop chats and try again",
              )
            : null,
          controller.isSkipSafetyRetryOffered
            ? m(
                Button,
                { variant: "secondary", disabled: controller.isRunning, onclick: () => controller.runSkipSafetyRetry() },
                "Restore without backing up first",
              )
            : null,
          controller.isForceRetryOffered
            ? m(
                Button,
                { variant: "danger", disabled: controller.isRunning, onclick: () => controller.runForceRetry() },
                "Force restore",
              )
            : null,
          controller.isRunning && controller.isCancellable && !controller.isRestore
            ? m(Button, { variant: "secondary", onclick: () => void controller.requestCancel() }, "Cancel")
            : null,
          hasLog
            ? m(
                Button,
                {
                  variant: "ghost",
                  onclick: () => {
                    vnode.state.isLogShown = !vnode.state.isLogShown;
                  },
                },
                vnode.state.isLogShown ? "Hide details" : "Show details",
              )
            : null,
        ]),
        hasLog && vnode.state.isLogShown
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
              controller.logLines.join("\n"),
            )
          : null,
      ]);
    },
  };
}
