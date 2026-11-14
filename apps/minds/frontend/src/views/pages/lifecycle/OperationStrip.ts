// The tracked-backup-operation strip: spinner + latest progress line for
// strip-driven operations (update / configure), the terminal notices
// (error, with its failure-specific retries / warning / success / cancelled),
// Cancel while the backend still allows it, and the collapsible
// full-output log. A restore reports on its snapshot row instead, so the
// strip shows only its notices and log.

import m from "mithril";
import {
  AskAgentFailure,
  askAgentActions,
} from "../../components/AskAgentButton";
import { Button } from "../../components/Button";
import { ErrorNotice, restartAppActions } from "../../components/ErrorDisplay";
import { Notice } from "../../components/Notice";
import { Spinner } from "../../components/Spinner";
import { AskAgentModel } from "../../../models/assistChat";
import type { BackupOperationController } from "../../../models/backups";
import type { ErrorAction } from "../../../models/errors";
import { recoveryRoute } from "../../../models/create";

const SERVICES_DOWN_SENTENCE =
  "Your workspace is restored and ready to use. A few of its programs haven't restarted yet -- they usually " +
  "come back within a few minutes.";
const SERVICES_DOWN_RESTART_SENTENCE =
  "*If something is still missing, restart the workspace.*";

const SERVICE_CONFIG_SENTENCE =
  "The restored settings for this workspace's programs couldn't be read, so its programs are running on the " +
  "settings from before the restore. Until the restored settings are fixed, the programs won't start the next " +
  "time the workspace restarts.";

export interface OperationStripAttrs {
  controller: BackupOperationController;
  /** The "Ask the agent to fix it" under an unreadable service configuration,
   * owned beside the controller (serviceConfigHelpModel) so the page can see
   * the strip's buttons too (operationStripErrors). */
  serviceConfigHelp: AskAgentModel;
}

interface StripState {
  isLogShown: boolean;
}

/** The model for an OperationStrip's serviceConfigHelp: a started agent
 * opens its workspace. */
export function serviceConfigHelpModel(): AskAgentModel {
  return new AskAgentModel({
    onStarted: (workspaceAgentId) =>
      m.route.set(`/workspace/${workspaceAgentId}`),
  });
}

/** One error notice the strip draws: the sentence it shows and the buttons
 * under it. The sentence travels with them because a page weighing its own
 * buttons against the strip's needs both (variantBesideErrors). */
export interface OperationStripError {
  message: string;
  actions: ErrorAction[];
}

/** The error notices the strip draws; null for one it isn't drawing. */
export interface OperationStripErrors {
  failure: OperationStripError | null;
  /** Null too for a warning drawn as a plain notice, with no update to offer. */
  warning: OperationStripError | null;
  servicesDown: OperationStripError | null;
  serviceConfig: OperationStripError | null;
}

/** What the strip draws its error notices from, for the strip itself and for a
 * page that draws its own buttons beside them (variantBesideErrors). */
export function operationStripErrors(
  controller: BackupOperationController,
  serviceConfigHelp: AskAgentModel,
): OperationStripErrors {
  const failure = controller.error;
  const warning = controller.warningMessage;
  const configError = controller.serviceConfigError;
  return {
    failure:
      failure === null
        ? null
        : {
            message: failure.message,
            actions: controller.isRestartAppOffered
              ? restartAppActions()
              : failureActions(controller),
          },
    warning:
      warning !== null && controller.isUpdateSuggested
        ? {
            message: warning,
            actions: [
              {
                label: "Update backup software",
                onclick: () => controller.startUpdate(),
              },
            ],
          }
        : null,
    servicesDown:
      controller.servicesDown.length > 0 ? servicesDownError(controller) : null,
    serviceConfig:
      configError === null
        ? null
        : {
            message: SERVICE_CONFIG_SENTENCE,
            actions: serviceConfigActions(
              controller.agentId,
              configError,
              serviceConfigHelp,
            ),
          },
  };
}

export function OperationStrip(): m.Component<OperationStripAttrs, StripState> {
  return {
    oninit(vnode) {
      vnode.state.isLogShown = false;
    },
    view(vnode) {
      const { controller, serviceConfigHelp } = vnode.attrs;
      const errors = operationStripErrors(controller, serviceConfigHelp);
      const isStripSpinnerShown = controller.isRunning && !controller.isRestore;
      const hasLog = controller.logLines.length > 0;
      const isAnythingShown =
        isStripSpinnerShown ||
        controller.error !== null ||
        controller.warningMessage !== null ||
        controller.servicesDown.length > 0 ||
        controller.serviceConfigError !== null ||
        controller.successMessage !== null ||
        controller.cancelledMessage !== null ||
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
        controller.error !== null && errors.failure !== null
          ? m(ErrorNotice, {
              id: "backup-operation-error",
              error: controller.error,
              isBugReportOffered: controller.isErrorReportOffered,
              workspaceAgentId: controller.agentId,
              actions: errors.failure.actions,
            })
          : null,
        controller.successMessage !== null
          ? m(Notice, { variant: "success" }, controller.successMessage)
          : null,
        errors.servicesDown !== null
          ? servicesDownNotice(controller, errors.servicesDown)
          : null,
        controller.serviceConfigError !== null && errors.serviceConfig !== null
          ? serviceConfigNotice(
              controller.agentId,
              controller.serviceConfigError,
              errors.serviceConfig.actions,
              serviceConfigHelp,
            )
          : null,
        controller.warningMessage !== null
          ? warningNotice(controller.warningMessage, errors.warning)
          : null,
        controller.cancelledMessage !== null
          ? m(Notice, { variant: "info" }, controller.cancelledMessage)
          : null,
        m("div", { class: "flex items-center gap-2 flex-wrap" }, [
          controller.isRunning &&
          controller.isCancellable &&
          !controller.isRestore
            ? m(
                Button,
                {
                  variant: "secondary",
                  onclick: () => void controller.requestCancel(),
                },
                "Cancel",
              )
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

/** The ask, until the route refuses it (a workspace without the assist
 * skill); the notice's Report a bug then stays as its only button. */
function serviceConfigActions(
  agentId: string,
  configError: string,
  help: AskAgentModel,
): ErrorAction[] {
  return askAgentActions({
    id: "backup-service-config-ask-agent",
    model: help,
    workspaceAgentId: agentId,
    description:
      "A backup restore left this workspace's supervisord configuration unreadable: `supervisorctl reread` " +
      "fails, so the services are running on the configuration loaded before the restore and will not start " +
      "on the workspace's next restart. Find the broken program configuration and fix it so that command " +
      `succeeds. supervisorctl said:\n${configError}`,
    label: "Ask the agent to fix it",
  });
}

/** The retries a failure offers, the surest first. */
function failureActions(controller: BackupOperationController): ErrorAction[] {
  const actions: ErrorAction[] = [];
  if (controller.isReloadOffered)
    actions.push({
      label: "Reload page",
      onclick: () => window.location.reload(),
    });
  if (controller.isRetryOffered)
    actions.push({ label: "Try again", onclick: () => controller.runRetry() });
  if (controller.isStopChatsRetryOffered)
    actions.push({
      label: "Stop chats and try again",
      onclick: () => controller.runStopChatsRetry(),
    });
  if (controller.isSkipSafetyRetryOffered)
    actions.push(
      {
        label: "Restore without backing up first",
        onclick: () => controller.runSkipSafetyRetry(),
      },
      {
        label: "Cancel",
        isDismissal: true,
        onclick: () => controller.abandonSkipSafetyRetry(),
      },
    );
  if (controller.isForceRetryOffered)
    actions.push({
      label: "Force restore",
      isDestructive: true,
      onclick: () => controller.runForceRetry(),
    });
  return actions;
}

/** A finished operation's caveat, with the update that finishes what it left
 * undone when there is one. */
function warningNotice(
  warning: string,
  shown: OperationStripError | null,
): m.Children {
  if (shown === null) return m(Notice, { variant: "warn" }, warning);
  return m(ErrorNotice, {
    id: "backup-operation-warning",
    tone: "warn",
    error: { message: warning, details: "" },
    actions: shown.actions,
    isBugReportOffered: false,
  });
}

/** A restore that finished with some programs not yet running again; the ask
 * to restart is made only when the restart is on offer. */
function servicesDownError(
  controller: BackupOperationController,
): OperationStripError {
  const actions = servicesDownActions(controller);
  return {
    message:
      actions.length > 0
        ? `${SERVICES_DOWN_SENTENCE} ${SERVICES_DOWN_RESTART_SENTENCE}`
        : SERVICES_DOWN_SENTENCE,
    actions,
  };
}

function servicesDownNotice(
  controller: BackupOperationController,
  shown: OperationStripError,
): m.Children {
  return m(ErrorNotice, {
    id: "backup-services-down",
    tone: "warn",
    error: {
      message: shown.message,
      details: `Not running yet: ${controller.servicesDown.join(", ")}`,
    },
    actions: shown.actions,
    isBugReportOffered: false,
  });
}

/** A restart, offered only when the restored program settings are readable:
 * otherwise a restart starts none of the programs (see serviceConfigNotice). */
function servicesDownActions(
  controller: BackupOperationController,
): ErrorAction[] {
  const agentId = controller.agentId;
  if (controller.serviceConfigError !== null) return [];
  return [
    {
      label: "Restart workspace",
      onclick: () =>
        m.route.set(recoveryRoute(agentId, `/workspace/${agentId}`, "restart")),
    },
  ];
}

/** A restore, finished or failed, that found the restored program settings
 * (supervisord's configuration) unreadable: the programs run on the settings
 * from before it, and will not start on the next restart until they are fixed. */
function serviceConfigNotice(
  agentId: string,
  configError: string,
  actions: ErrorAction[],
  help: AskAgentModel,
): m.Children {
  return m(ErrorNotice, {
    id: "backup-service-config",
    error: { message: SERVICE_CONFIG_SENTENCE, details: configError },
    workspaceAgentId: agentId,
    actions,
    // This notice already offers Report a bug.
    footer: m(AskAgentFailure, {
      id: "backup-service-config-ask-agent",
      model: help,
      workspaceAgentId: agentId,
      isBugReportOffered: false,
      isBoxed: false,
    }),
  });
}
