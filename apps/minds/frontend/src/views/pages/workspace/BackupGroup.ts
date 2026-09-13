// The Backup group inside Machine settings: what the machine's backups are
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
import { FormLabel, Select, Textarea } from "../../components/FormControls";
import { Notice } from "../../components/Notice";
import { SectionHeader } from "../../components/Layout";
import { Spinner } from "../../components/Spinner";
import { routeLinkAttrs } from "../../components/route-link";
import { isRecreationRequired } from "../../../models/updates";
import {
  BACKUP_SETTINGS_RECENT_LIMIT,
  BackupOperationController,
  BackupSettingsModel,
  type BackupSnapshot,
  browserLifecycleDeps,
  formatRelativeAgo,
} from "../../../models/backups";
import { OperationStrip } from "../lifecycle/OperationStrip";
import { RestoreDialog } from "../lifecycle/RestoreDialog";
import { SnapshotTable } from "../lifecycle/SnapshotTable";

export interface BackupGroupAttrs {
  agentId: string;
}

interface BackupGroupState {
  model: BackupSettingsModel;
  controller: BackupOperationController;
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
      const { model, controller } = vnode.state;
      const isBusy = controller.isRunning;
      return m("div", { id: "backup-section", class: "max-w-md" }, [
        m(SectionHeader, "Backups"),
        m(
          "p",
          { class: "type-body text-secondary mb-1" },
          [model.statusLine, " ", model.checkLine].join("").trim(),
        ),
        m(
          "p",
          { class: "type-helper text-tertiary mb-4" },
          "If this machine is destroyed, its backups are kept for 30 days and can be downloaded from the " +
            "Recently destroyed machines page, then deleted automatically.",
        ),

        renderRecentBackups(vnode),
        m(OperationStrip, { controller }),

        m(SectionHeader, "Where your backups are stored"),
        renderStorageForm(vnode, isBusy),

        m(SectionHeader, "Backup service verification"),
        renderVerification(model, isBusy),

        m(SectionHeader, "Fix backup problems"),
        renderFixProblems(vnode, isBusy),

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

function renderRecentBackups(
  vnode: m.Vnode<BackupGroupAttrs, BackupGroupState>,
): m.Children {
  const { model, controller } = vnode.state;
  const emptyMessage = model.emptyHistoryMessage;
  return m("div", { class: "mb-6" }, [
    m("h3", { class: "type-label text-secondary mb-2" }, "Recent backups"),
    emptyMessage !== null
      ? m("p", { class: "type-body text-secondary" }, emptyMessage)
      : m(Card, { padding: "tight" }, [
          m(SnapshotTable, {
            agentId: model.agentId,
            snapshots: model.snapshots.slice(0, BACKUP_SETTINGS_RECENT_LIMIT),
            controller,
            restoreDisabledReason: model.isRestoreDisabledByCheck
              ? "This machine is offline; start it to restore a backup."
              : null,
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
                    "Minds",
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

function renderVerification(
  model: BackupSettingsModel,
  isBusy: boolean,
): m.Children {
  return m("div", { class: "mb-6" }, [
    m(
      "p",
      { class: "type-body text-secondary mb-2" },
      "Minds checks your backups regularly and warns you below if anything looks wrong.",
    ),
    model.verificationError !== null
      ? m(Notice, { variant: "error", extra: "mb-2" }, model.verificationError)
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

function renderFixProblems(
  vnode: m.Vnode<BackupGroupAttrs, BackupGroupState>,
  isBusy: boolean,
): m.Children {
  const { model, controller } = vnode.state;
  const isTooOld = isTooOldForUpdate(model.agentId);
  const problems = model.problemLines;
  const versionLine = model.versionLine;
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
    problems.length > 0
      ? m(
          "ul",
          {
            id: "backup-problems",
            class: "type-body text-important mb-2 list-disc pl-4",
          },
          problems.map((problem) => m("li", { key: problem }, problem)),
        )
      : null,
    model.isUpdateOffered(isTooOld)
      ? m(
          Button,
          {
            variant: "secondary",
            id: "backup-update-btn",
            disabled: isBusy,
            onclick: () => controller.startUpdate(),
          },
          "Update backup software",
        )
      : m(
          "p",
          { class: "type-helper text-tertiary" },
          isTooOld
            ? "This machine is too old to run today's backup software. Create a new machine and ask its agent to " +
                "migrate your work across."
            : "This machine is offline; start it to update its backup software.",
        ),
  ]);
}
