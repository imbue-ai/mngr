// The Backup group inside Workspace settings: what the workspace's backups are
// doing, the five most recent (with Download / Restore, and a link to the full
// history at /workspace/<id>/backups), where the backups are stored, the
// verification toggle, and the one idempotent "Update backup software"
// converge every reported problem points at.
//
// The three actions run as tracked operations reported through the shared
// operation strip, which reattaches to one already running -- including one
// started from the backup-history page.

import m from "mithril";
import { getAppContext } from "../../../app-context";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { ErrorNotice } from "../../components/ErrorDisplay";
import { FormLabel, Select, Textarea } from "../../components/FormControls";
import { SectionHeader } from "../../components/Layout";
import { Spinner } from "../../components/Spinner";
import { routeLinkAttrs } from "../../components/route-link";
import {
  type ShownError,
  variantBesideErrors,
} from "../../components/variantBesideErrors";
import type { AskAgentModel } from "../../../models/assistChat";
import type { ErrorAction } from "../../../models/errors";
import { isRecreationRequired } from "../../../models/updates";
import {
  BACKUP_SETTINGS_RECENT_LIMIT,
  BackupOperationController,
  BackupSettingsModel,
  type BackupSnapshot,
  backupHistoryLoadError,
  browserLifecycleDeps,
  formatRelativeAgo,
} from "../../../models/backups";
import {
  OperationStrip,
  operationStripErrors,
  serviceConfigHelpModel,
} from "../lifecycle/OperationStrip";
import { RestoreDialog } from "../lifecycle/RestoreDialog";
import { SnapshotTable } from "../lifecycle/SnapshotTable";

/** The sentence the problems notice leads with. */
const PROBLEMS_FOUND_SENTENCE =
  "The backup check found problems with this workspace's backups.";

/** Why a backup action is held while another backup task runs. */
const BACKUP_TASK_RUNNING =
  "Wait for the backup task on this workspace to finish.";

export interface BackupGroupAttrs {
  agentId: string;
}

interface BackupGroupState {
  model: BackupSettingsModel;
  controller: BackupOperationController;
  serviceConfigHelp: AskAgentModel;
  /** The storage form, collapsed until asked for: moving where backups go is
   * the rare path, and an always-open destination picker invites a misclick. */
  isStorageFormOpen: boolean;
  storageProvider: string;
  storageApiKeyEnv: string;
  pendingRestoreSnapshot: BackupSnapshot | null;
}

/** Whether this machine is too old to be given the current backup service.
 * The server refuses the update for the same reason; offering a button that
 * can only be refused is worse than not offering it. */
function isTooOldForUpdate(agentId: string): boolean {
  return isRecreationRequired(getAppContext().stores.updates.forAgent(agentId));
}

function buildState(agentId: string): BackupGroupState {
  const deps = browserLifecycleDeps(() => m.redraw());
  const model = new BackupSettingsModel(agentId, deps);
  const controller = new BackupOperationController(agentId, deps);
  controller.onSuccess = () => void model.load();
  return {
    model,
    controller,
    serviceConfigHelp: serviceConfigHelpModel(),
    isStorageFormOpen: false,
    storageProvider: "IMBUE_CLOUD",
    storageApiKeyEnv: "",
    pendingRestoreSnapshot: null,
  };
}

export function BackupGroup(): m.Component<BackupGroupAttrs, BackupGroupState> {
  return {
    oninit(vnode) {
      Object.assign(vnode.state, buildState(vnode.attrs.agentId));
      void vnode.state.controller.reattach();
      void vnode.state.model.load();
    },
    onremove(vnode) {
      vnode.state.model.stop();
      vnode.state.controller.stop();
    },
    view(vnode) {
      const { agentId } = vnode.attrs;
      // A route change between machines preserves this component instance, so
      // the models are swapped by hand rather than trusting oninit.
      if (vnode.state.model.agentId !== agentId) {
        vnode.state.model.stop();
        vnode.state.controller.stop();
        Object.assign(vnode.state, buildState(agentId));
        void vnode.state.controller.reattach();
        void vnode.state.model.load();
      }
      const { model, controller, serviceConfigHelp } = vnode.state;
      const isBusy = controller.isRunning;
      const historyActions = historyErrorActions(model);
      const verificationActions = verificationErrorActions(model, isBusy);
      const problemsActions = problemsErrorActions(model, controller, isBusy);
      // Each error the panel shows, with the sentence it shows: the rule below
      // weighs a neighbour's lead button by what that sentence asks for.
      const shownErrors = [
        historyActions === null
          ? null
          : {
              message: backupHistoryLoadError(model.snapshotsErrorDetail)
                .message,
              actions: historyActions,
            },
        ...Object.values(operationStripErrors(controller, serviceConfigHelp)),
        model.verificationError === null || verificationActions === null
          ? null
          : {
              message: model.verificationError.message,
              actions: verificationActions,
            },
        problemsActions === null
          ? null
          : { message: PROBLEMS_FOUND_SENTENCE, actions: problemsActions },
      ].filter((error): error is ShownError => error !== null);
      return m("div", { id: "backup-section", class: "max-w-md" }, [
        m(SectionHeader, "Backups"),
        renderStatus(model, shownErrors),
        m(
          "p",
          { class: "type-helper text-tertiary mb-4" },
          "If this workspace is destroyed, its backups are kept for 30 days and can be downloaded from the " +
            "Recently destroyed workspaces page, then deleted automatically.",
        ),

        renderRecentBackups(vnode, historyActions),
        m(OperationStrip, { controller, serviceConfigHelp }),

        m(SectionHeader, "Where your backups are stored"),
        renderStorageForm(vnode, isBusy),

        m(SectionHeader, "Backup service verification"),
        renderVerification(model, isBusy, verificationActions),

        m(SectionHeader, "Fix backup problems"),
        renderFixProblems(vnode, isBusy, problemsActions),

        m(RestoreDialog, {
          snapshot: vnode.state.pendingRestoreSnapshot,
          onCancel: () => {
            vnode.state.pendingRestoreSnapshot = null;
          },
          onConfirm: (snapshot, choices) => {
            vnode.state.pendingRestoreSnapshot = null;
            controller.startRestore(
              snapshot,
              formatRelativeAgo(snapshot.time, Date.now()),
              {
                updateAfter: choices.updateAfter,
              },
            );
          },
        }),
      ]);
    },
  };
}

/** What the backups are doing and what the service check concluded; a listing
 * or a check that could not be read says so, with one way to read it again.
 * A listing that could not establish the status at all re-reads the check
 * with it; otherwise a missing check verdict has its own Check again, the main
 * button unless another error on the panel leads with one. */
function renderStatus(
  model: BackupSettingsModel,
  shownErrors: readonly ShownError[],
): m.Children {
  const statusError = model.statusError;
  const statusText = [model.statusLine, " ", model.checkLine].join("").trim();
  return [
    statusError !== null
      ? m(ErrorNotice, {
          id: "backup-status-error",
          error: statusError,
          actions: [
            {
              label: "Check again",
              onclick: () => void model.reloadFailedReads(),
            },
          ],
          workspaceAgentId: model.agentId,
        })
      : null,
    statusText
      ? m("p", { class: "type-body text-secondary mb-1" }, statusText)
      : null,
    model.isCheckVerdictMissing && statusError === null
      ? m(
          "div",
          {
            id: "backup-check-missing",
            class: "flex flex-wrap items-center gap-2 mb-1",
          },
          [
            m(
              "span",
              { class: "type-body text-secondary" },
              "Imbue Studio couldn't check this workspace's backups this time.",
            ),
            m(
              Button,
              {
                variant: variantBesideErrors("primary", shownErrors),
                onclick: () => void model.loadCheck(),
              },
              "Check again",
            ),
          ],
        )
      : null,
  ];
}

/** The history error's retry; null when the error isn't on show. */
function historyErrorActions(model: BackupSettingsModel): ErrorAction[] | null {
  if (model.statusError !== null) return null;
  if (model.emptyHistoryMessage === null || !model.isSnapshotsErrored)
    return null;
  return [{ label: "Try again", onclick: () => void model.loadSnapshots() }];
}

function renderRecentBackups(
  vnode: m.Vnode<BackupGroupAttrs, BackupGroupState>,
  historyActions: ErrorAction[] | null,
): m.Children {
  const { model, controller } = vnode.state;
  // The status error above already says the listing failed, and reads it again.
  if (model.statusError !== null) return null;
  const emptyMessage = model.emptyHistoryMessage;
  return m("div", { class: "mb-6" }, [
    m("h3", { class: "type-label text-secondary mb-2" }, "Recent backups"),
    historyActions !== null
      ? m(ErrorNotice, {
          id: "backup-history-error",
          error: backupHistoryLoadError(model.snapshotsErrorDetail),
          actions: historyActions,
          workspaceAgentId: model.agentId,
        })
      : emptyMessage !== null
        ? m("p", { class: "type-body text-secondary" }, emptyMessage)
        : m(Card, { padding: "tight" }, [
            m(SnapshotTable, {
              agentId: model.agentId,
              snapshots: model.snapshots.slice(0, BACKUP_SETTINGS_RECENT_LIMIT),
              controller,
              restoreDisabledReason: model.isRestoreDisabledByCheck
                ? "This workspace is offline; start it to restore a backup."
                : null,
              repositoryId: null,
              onRestoreRequested: (snapshot) => {
                vnode.state.pendingRestoreSnapshot = snapshot;
              },
            }),
          ]),
    model.isViewAllShown
      ? m(
          "p",
          { class: "type-body mt-2" },
          m(
            "a",
            {
              id: "backup-view-all",
              class: "text-accent hover:underline",
              ...routeLinkAttrs(`/workspace/${model.agentId}/backups`),
            },
            `View all ${model.snapshotsTotal} backups`,
          ),
        )
      : null,
  ]);
}

function renderStorageForm(
  vnode: m.Vnode<BackupGroupAttrs, BackupGroupState>,
  isBusy: boolean,
): m.Children {
  const { controller } = vnode.state;
  return m("div", { id: "backup-configure", class: "mb-6" }, [
    m(
      "p",
      { class: "type-body text-secondary mb-2" },
      "Backups are encrypted on your computer, so only you can read them.",
    ),
    m(
      Button,
      {
        variant: "secondary",
        id: "backup-configure-toggle-btn",
        onclick: () => {
          vnode.state.isStorageFormOpen = !vnode.state.isStorageFormOpen;
        },
      },
      "Change storage location",
    ),
    vnode.state.isStorageFormOpen
      ? m(
          "div",
          { id: "backup-configure-form", class: "flex flex-col gap-2 mt-3" },
          [
            m("div", [
              m(
                FormLabel,
                { target: "backup-provider-select" },
                "Keep my backups in",
              ),
              m(
                Select,
                {
                  id: "backup-provider-select",
                  name: "backup_provider",
                  width: "w-64",
                  value: vnode.state.storageProvider,
                  onchange: (event: Event) => {
                    vnode.state.storageProvider = (
                      event.target as HTMLSelectElement
                    ).value;
                  },
                },
                [
                  m(
                    "option",
                    {
                      value: "IMBUE_CLOUD",
                      selected: vnode.state.storageProvider === "IMBUE_CLOUD",
                    },
                    "Imbue Studio",
                  ),
                  m(
                    "option",
                    {
                      value: "API_KEY",
                      selected: vnode.state.storageProvider === "API_KEY",
                    },
                    "My own storage",
                  ),
                  m(
                    "option",
                    {
                      value: "NONE",
                      selected: vnode.state.storageProvider === "NONE",
                    },
                    "Nowhere -- turn backups off",
                  ),
                ],
              ),
            ]),
            vnode.state.storageProvider === "API_KEY"
              ? m("div", { id: "backup-api-key-row" }, [
                  m(
                    FormLabel,
                    { target: "backup-api-key-env-input" },
                    "Connection details for your storage",
                  ),
                  m(Textarea, {
                    id: "backup-api-key-env-input",
                    name: "api_key_env",
                    rows: 4,
                    spellcheck: "false",
                    extra: "font-mono",
                    value: vnode.state.storageApiKeyEnv,
                    oninput: (event: InputEvent) => {
                      vnode.state.storageApiKeyEnv = (
                        event.target as HTMLTextAreaElement
                      ).value;
                    },
                  }),
                ])
              : null,
            m(
              "div",
              m(
                Button,
                {
                  variant: "secondary",
                  id: "backup-configure-submit-btn",
                  disabled: isBusy,
                  onclick: () =>
                    controller.startStorageChange(
                      vnode.state.storageProvider,
                      vnode.state.storageApiKeyEnv,
                    ),
                },
                "Save",
              ),
            ),
          ],
        )
      : null,
  ]);
}

/** The verification error's retry; null when the error isn't on show. */
function verificationErrorActions(
  model: BackupSettingsModel,
  isBusy: boolean,
): ErrorAction[] | null {
  if (model.verificationError === null) return null;
  return [
    {
      label: "Try again",
      isDisabled: isBusy,
      tooltip: isBusy ? BACKUP_TASK_RUNNING : undefined,
      onclick: () => void model.retryVerificationChange(),
    },
  ];
}

function renderVerification(
  model: BackupSettingsModel,
  isBusy: boolean,
  verificationActions: ErrorAction[] | null,
): m.Children {
  return m("div", { class: "mb-6" }, [
    m(
      "p",
      { class: "type-body text-secondary mb-2" },
      "Imbue Studio checks your backups regularly and warns you below if anything looks wrong.",
    ),
    model.verificationError !== null && verificationActions !== null
      ? m(ErrorNotice, {
          id: "backup-verification-error",
          error: model.verificationError,
          actions: verificationActions,
          workspaceAgentId: model.agentId,
        })
      : null,
    m(
      Button,
      {
        variant: "secondary",
        id: "backup-verification-btn",
        // The toggle only writes a local setting, but its re-check execs into
        // the machine; mid-operation that would report a transient result.
        disabled: isBusy || model.isVerificationPending,
        onclick: () => void model.toggleVerification(),
      },
      model.isVerificationPending
        ? m(Spinner, { size: "sm" })
        : model.isVerificationEnabled
          ? "Disable"
          : "Enable",
    ),
  ]);
}

const UPDATE_LABEL = "Update backup software";

/** The problems notice's buttons; null when the check found none. The update
 * is the remedy the problems name, so it is their error's main action;
 * otherwise it stays on offer below as the idempotent reset it is. */
function problemsErrorActions(
  model: BackupSettingsModel,
  controller: BackupOperationController,
  isBusy: boolean,
): ErrorAction[] | null {
  if (model.problemLines.length === 0) return null;
  const isUpdateInProblems =
    model.isUpdateOffered(isTooOldForUpdate(model.agentId)) &&
    model.isProblemFixedByUpdate;
  return isUpdateInProblems
    ? [
        {
          id: "backup-update-btn",
          label: UPDATE_LABEL,
          isDisabled: isBusy,
          tooltip: isBusy ? BACKUP_TASK_RUNNING : undefined,
          onclick: () => controller.startUpdate(),
        },
      ]
    : [];
}

function renderFixProblems(
  vnode: m.Vnode<BackupGroupAttrs, BackupGroupState>,
  isBusy: boolean,
  problemsActions: ErrorAction[] | null,
): m.Children {
  const { model, controller } = vnode.state;
  const isTooOld = isTooOldForUpdate(model.agentId);
  const problems = model.problemLines;
  const versionLine = model.versionLine;
  const isUpdateOffered = model.isUpdateOffered(isTooOld);
  const isUpdateInProblems =
    problemsActions !== null && problemsActions.length > 0;
  return m("div", { class: "mb-2" }, [
    m(
      "p",
      { class: "type-body text-secondary mb-2" },
      "If backups stop working, this reinstalls and restarts the backup software. It's safe to run any time " +
        "and never touches your files or existing backups.",
    ),
    model.isCheckLoading
      ? m(
          "p",
          {
            class:
              "type-body text-secondary mb-2 inline-flex items-center gap-2",
          },
          [
            m(Spinner, { size: "sm" }),
            m("span", "Checking the backup service..."),
          ],
        )
      : null,
    versionLine
      ? m(
          "p",
          { id: "backup-versions", class: "type-helper text-tertiary mb-2" },
          versionLine,
        )
      : null,
    problemsActions !== null
      ? m(ErrorNotice, {
          id: "backup-problems",
          error: {
            message: PROBLEMS_FOUND_SENTENCE,
            details: model.problemDetail,
          },
          content: m(
            "ul",
            { class: "m-0 list-disc pl-4" },
            problems.map((problem) => m("li", { key: problem }, problem)),
          ),
          isBugReportOffered: false,
          actions: problemsActions,
        })
      : null,
    isUpdateInProblems
      ? null
      : isUpdateOffered
        ? m(
            Button,
            {
              variant: "secondary",
              id: "backup-update-btn",
              disabled: isBusy,
              onclick: () => controller.startUpdate(),
            },
            UPDATE_LABEL,
          )
        : m(
            "p",
            { class: "type-helper text-tertiary" },
            isTooOld
              ? "This workspace is too old to run today's backup software. Create a new workspace and ask its agent to " +
                  "migrate your work across."
              : "This workspace is offline; start it to update its backup software.",
          ),
  ]);
}
