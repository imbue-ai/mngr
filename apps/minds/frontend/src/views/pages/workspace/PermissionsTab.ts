// The Permissions tab: everything agents in this workspace can reach, as
// toggles. Left nav is one entry per connection (a signed-in or granted
// (service, account) pair), then Add connection, then the two latchkey-self
// families -- Local files (shared paths) and Other workspaces (cross-workspace
// verbs). The right pane holds the selected entry's toggles.
//
// Port of WorkspacePermissionsSection.jinja + workspace_permissions.js. The
// one behavior change: a flip re-renders from the server's refreshed view
// (PermissionsModel adopts the response) instead of flipping optimistically
// and reverting, so a control never shows a state that was not stored.

import m from "mithril";
import { getAppContext } from "../../../app-context";
import { AskAgentModel } from "../../../models/assistChat";
import { remedyActions } from "../../components/errorRemedies";
import { AskAgentFailure, askAgentActions } from "../../components/AskAgentButton";
import type { ErrorAction, UserFacingError } from "../../../models/errors";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { ErrorNotice } from "../../components/ErrorDisplay";
import { InlineError } from "../../components/errorDesign";
import { TextInput } from "../../components/FormControls";
import { Icon16 } from "../../components/Icon";
import { renderImplicitPermissionsHint } from "../../components/ImplicitPermissionsHint";
import { SectionHeader } from "../../components/Layout";
import { Modal } from "../../components/Modal";
import { Notice } from "../../components/Notice";
import type { MarkTone } from "../../components/ServiceMark";
import { serviceMark } from "../../components/ServiceMark";
import { renderPermissionSwitch } from "../../components/PermissionSwitch";
import { Spinner } from "../../components/Spinner";
import { electronBridge } from "../../../electron-bridge";
import type { StatusBadgeVariant } from "../../components/StatusBadge";
import { StatusBadge } from "../../components/StatusBadge";
import type {
  ErrorRemedy,
  FileSharingAccess,
  FolderSyncState,
  UiAvailableConnection,
  UiSharedPath,
  UiPermissionConnection,
  UiSelfPermissionToggle,
  UiWaitingPermissionRequest,
  UiWorkspacePermissions,
} from "../../../generated/ui";
import type { PermissionsModel } from "../../../models/workspacePermissions";
import {
  ADD_CONNECTION_SECTION,
  FILE_SHARING_ACCESSES,
  LOCAL_FILES_SECTION,
  OTHER_MACHINES_SECTION,
  THIS_COMPUTER_SECTION,
  WAITING_SECTION,
  connectActionFor,
  connectServiceRowKey,
  connectionSectionId,
  connectorToggleRowKey,
  desktopEgressRowKey,
  fileSharingAccessLabel,
  folderSyncStateLabel,
  hasCopyOnMachine,
  hasInactiveCopy,
  isPathSyncOn,
  syncProgressLabel,
  pathAccessRowKey,
  folderSyncDiscardCopyRowKey,
  ADD_SHARED_PATH_ROW_KEY,
  folderSyncRetryRowKey,
  folderSyncToggleRowKey,
  pathRemoveRowKey,
  isCredentialFormComplete,
  resolvePermissionsSection,
  disconnectRowKey,
  revokeAllRowKey,
  selfToggleRowKey,
} from "../../../models/workspacePermissions";
import { navEntryClass, splitPane } from "../../components/SplitPane";
import { SETTING_SELECT_CLASS, renderFolderSyncSetting, renderSettingRow } from "../../components/FolderSyncSetting";
import { renderDesktopEgressRouteSetting } from "../../components/DesktopEgressRouteSetting";
import { areRoutesEqual } from "../../../models/desktopEgressRoute";
import { warmRequestDetail } from "../../../models/requestDetailPrefetch";

/** How long a "Revoke all" stays armed after its first click. */
const REVOKE_CONFIRM_WINDOW_MS = 4000;

/** Add connection reads as the secondary affordance it is until it is the
 * selected section, at which point it is a nav entry like any other. */
const ADD_CONNECTION_CLASS =
  "flex w-full items-center gap-2 rounded-md px-2 py-1 text-left type-body cursor-pointer " +
  "transition-colors text-secondary hover:text-primary hover:bg-fill-hover";

const TOGGLE_ROW_CLASS = "perm-row flex items-center justify-between gap-4 py-2";

const CATALOG_HEADING_CLASS = "type-section text-tertiary mt-6 mb-1";

/** Stand-ins for a service with no mark on disk: the nav and the catalog
 * still need a glyph in the slot, while a connection heading simply drops
 * it. A globe rather than a box for two reasons: what usually lands here is a
 * custom service, which is a connection to a domain and has no vendor artwork
 * to publish; and "Local files" lower down this same nav already draws a box,
 * so a service sharing that glyph read as the same kind of thing.
 * A shipped service whose mark 404s lands here too, and a globe suits it no
 * worse.
 * Built per call -- one vnode cannot appear twice in a tree. */
const navFallbackMark = (): m.Children => m(Icon16, { name: "globe", extra: "shrink-0" });
const catalogFallbackMark = (): m.Children => m(Icon16, { name: "globe", extra: "shrink-0 text-tertiary" });

const SELF_TOGGLE_BLOCKED_TITLE =
  "This grant can't be re-enabled; ask the agent to request it again.";
const CONNECTOR_TOGGLE_BLOCKED_TITLE = "Connect this account before granting permissions.";
/** Why every other control is inert while one change is being applied. */
const PANE_BUSY_TITLE = "Waiting for the last change to reach this workspace.";

/** Whether a control must sit out because a DIFFERENT write is still running.
 *
 * A write is not finished until the workspace's own machine has taken it, and
 * its response replaces the whole pane, so exactly one may be in flight. The
 * acting control shows its own spinner instead (see `renderSwitch`). */
function isLockedByAnotherWrite(model: PermissionsModel, rowKey: string): boolean {
  return model.isWriting() && !model.isRowBusy(rowKey);
}

/** Why a service offers no action at all: latchkey cannot sign in to it and
 * told us nothing about the credentials it takes, so there is nothing to ask
 * for. Shown on hover, in place of a button that could only fail. */
function unconnectableTitle(displayName: string): string {
  return `Imbue Studio can't set up ${displayName} from here yet.`;
}

/** Why Add file and Add folder are greyed out on a desktop with no device id. */
const CANNOT_SHARE_FILES_SENTENCE = "Imbue Studio can't share files from this computer.";

export interface PermissionsTabAttrs {
  model: PermissionsModel;
  /** Workspace name for the heading; '' when the options load has not landed. */
  workspaceName: string;
  /** ?section from the URL, or null for "whatever is first". */
  requestedSection: string | null;
  onSelectSection: (section: string) => void;
  /** Open a waiting request. It opens as its own page over this pane, which
   * stays mounted underneath with its scroll and section intact. */
  onReviewRequest: (requestId: string) => void;
}

interface PermissionsTabLocalState {
  /** Row key of the armed "Revoke all", if any: the second click fires it. */
  armedRevokeRowKey: string | null;
  disarmTimer: ReturnType<typeof setTimeout> | null;
  /** Section id of the connection whose Disconnect dialog is open, or null.
   * Keyed by section rather than by a flag so a dialog cannot outlive the
   * connection it belongs to. */
  disconnectSectionId: string | null;
  /** Ticker that re-reads the pane while any sync is alive. Nothing else tells
   * this pane what the subprocess behind a sync is doing. */
  folderSyncPollTimer: ReturnType<typeof setInterval> | null;
  /** The desktop egress route being edited and the service it belongs to, or
   * null while no editor is open. Nothing is written until it is saved.
   *
   * It names the model it was opened on because this state outlives a
   * navigation to another workspace's pane, where the same service must not
   * pick the draft up. */
  desktopEgressDraft: { model: PermissionsModel; serviceName: string; route: string[] } | null;
  /** The chat a refused change's "Ask the agent to request it again" starts. */
  askAgent: AskAgentModel;
}

/** How often the pane re-reads while a sync is alive.
 *
 * Matched to how often there is anything new: ``mngr pair`` reports a
 * transfer's progress at most once a second, so a slower tick would show every
 * other count at best while a big folder is moving. Affordable at this rate
 * because the endpoint behind it answers from this process alone -- see
 * ``reloadFolderSyncs`` -- and because nothing ticks at all unless a row is
 * live. */
const FOLDER_SYNC_POLL_MS = 1000;

export function PermissionsTab(): m.Component<PermissionsTabAttrs> {
  const local: PermissionsTabLocalState = {
    armedRevokeRowKey: null,
    disarmTimer: null,
    disconnectSectionId: null,
    folderSyncPollTimer: null,
    desktopEgressDraft: null,
    askAgent: new AskAgentModel({
      onStarted: (workspaceAgentId) => getAppContext().shell.enterWorkspace(workspaceAgentId),
    }),
  };

  return {
    oninit(vnode) {
      // Lazy: the tab is only mounted while it is the selected one, and the
      // model no-ops a second call, so reopening the tab neither refetches
      // nor blanks the pane.
      vnode.attrs.model.ensureLoaded();
      const { model } = vnode.attrs;
      local.folderSyncPollTimer = setInterval(() => {
        void model.refreshLiveFolderSyncs();
      }, FOLDER_SYNC_POLL_MS);
    },
    onremove() {
      disarmRevoke(local);
      local.disconnectSectionId = null;
      if (local.folderSyncPollTimer !== null) {
        clearInterval(local.folderSyncPollTimer);
        local.folderSyncPollTimer = null;
      }
    },
    view(vnode) {
      const { model, workspaceName, requestedSection, onSelectSection, onReviewRequest } = vnode.attrs;
      const machineName = workspaceName.trim();

      return m("div", { class: "flex flex-col flex-1 min-h-0" }, [
        m("h1", { class: "type-heading-lg text-primary flex items-center gap-2 min-w-0 shrink-0" }, [
          m(Icon16, { name: "key", size: "lg", extra: "shrink-0" }),
          m("span", { class: "shrink-0" }, machineName ? "Permissions:" : "Permissions"),
          machineName ? m("span", { class: "truncate max-w-[280px]" }, machineName) : null,
        ]),
        m(
          "p",
          { class: "mt-2 max-w-[760px] type-body text-primary shrink-0" },
          machineName
            ? [
                "What agents in ",
                m("span", { class: "font-semibold" }, machineName),
                " can access. They can never reach beyond the permissions you grant them.",
              ]
            : "What agents in this workspace can access. They can never reach beyond the permissions you grant them.",
        ),
        renderBody(model, local, machineName, requestedSection, onSelectSection, onReviewRequest),
      ]);
    },
  };
}

/** What to call this machine in copy that contrasts it with the others. Falls
 * back to a bare description while the options load has not landed, so the
 * sentence still reads. */
function machineLabelFor(machineName: string): string {
  return machineName === "" ? "this workspace" : machineName;
}

function renderBody(
  model: PermissionsModel,
  local: PermissionsTabLocalState,
  machineName: string,
  requestedSection: string | null,
  onSelectSection: (section: string) => void,
  onReviewRequest: (requestId: string) => void,
): m.Children {
  if (model.status === "idle" || model.status === "loading") {
    return m("p", { class: "mt-6 type-body text-secondary flex items-center gap-2 shrink-0" }, [
      m(Spinner, { size: "sm" }),
      "Loading permissions...",
    ]);
  }
  const tryAgain: ErrorAction = { label: "Try again", onclick: () => void model.load() };
  if (model.status === "load_failed" && model.loadError !== null) {
    return m(ErrorNotice, {
      id: "ws-perm-load-error",
      error: model.loadError,
      actions: [tryAgain],
      workspaceAgentId: model.agentId,
      extra: "mt-6 shrink-0",
    });
  }
  const data = model.data;
  if (data === null || data.permissions_unavailable) {
    // The gateway (or the machine's host) is unreachable: say why rather than
    // rendering an empty tree that reads as "nothing is granted".
    const notice = data?.unavailable_notice ?? null;
    const error: UserFacingError = {
      message: notice?.message ?? "Permissions can't be loaded right now. Try again in a moment.",
      details: notice?.detail ?? "",
    };
    return m(ErrorNotice, {
      id: "ws-perm-unavailable",
      error,
      actions: unavailableActions(notice?.remedy ?? null, tryAgain),
      workspaceAgentId: model.agentId,
      extra: "mt-6 shrink-0",
    });
  }

  // Waiting on you is a section like any other: what is selected is whatever
  // the URL asks for.
  const selected = resolvePermissionsSection(data, requestedSection);
  const selectSection = (section: string): void => {
    disarmRevoke(local);
    local.disconnectSectionId = null;
    local.desktopEgressDraft = null;
    model.clearWriteError();
    onSelectSection(section);
  };

  return [
    splitPane({
      navLabel: "Permission sections",
      nav: renderNav(data.connections, data.waiting_requests, selected, selectSection),
      content: [
        renderStaleRefreshNotice(model),
        model.writeError !== null && local.disconnectSectionId === null
          ? m(ErrorNotice, {
              id: "ws-perm-error",
              error: model.writeError,
              actions: writeErrorActions(model, local),
              workspaceAgentId: model.agentId,
              footer: m(AskAgentFailure, {
                id: ASK_AGENT_ID,
                model: local.askAgent,
                workspaceAgentId: model.agentId,
                isBoxed: false,
              }),
              extra: "shrink-0",
            })
          : null,
        renderSelectedPanel(model, local, data, machineName, selected, selectSection, onReviewRequest),
      ],
      extra: "mt-8",
      // pb-8 keeps the panel foot (the Disconnect button) off the pane's
      // bottom edge when scrolled to the end.
      contentExtra: "flex flex-col pb-8",
    }),
  ];
}

/** The buttons over a pane that could not be read. The remedy the server names
 * leads; Try again follows it as secondary, since the permission service can
 * come back by itself but the remedy is the sure fix. A pane a newer Imbue
 * Studio wrote gets no Try again at all: it reads the same until this one
 * updates. */
function unavailableActions(remedy: ErrorRemedy | null, tryAgain: ErrorAction): ErrorAction[] {
  const remedies = remedyActions(remedy);
  if (remedy === "UPDATE_APP") return remedies;
  if (remedies.length === 0) return [tryAgain];
  return [...remedies, tryAgain];
}

/** The id of a refused change's "Ask the agent to request it again". */
const ASK_AGENT_ID = "ws-perm-ask-agent";

/** The buttons a refused change offers: the remedy its refusal names. */
function writeErrorActions(model: PermissionsModel, local: PermissionsTabLocalState): ErrorAction[] {
  const sentence = model.writeError?.message ?? "";
  const reason = model.writeErrorReason;
  return remedyActions(model.writeErrorRemedy, {
    askAgent: () =>
      askAgentActions({
        id: ASK_AGENT_ID,
        model: local.askAgent,
        workspaceAgentId: model.agentId,
        description:
          `Imbue Studio refused a permission change the user made in the Permissions tab: "${sentence}" ` +
          "Request that permission again so the user can approve it." +
          (reason ? ` Imbue Studio's reason: ${reason}` : ""),
        label: "Ask the agent to request it again",
      }),
  });
}

/** Said over a pane whose background re-read failed: what is on screen is the
 * last view that loaded, and may no longer be what the workspace holds. */
function renderStaleRefreshNotice(model: PermissionsModel): m.Children {
  if (model.refreshError === null) return null;
  return m(ErrorNotice, {
    id: "ws-perm-stale",
    error: model.refreshError,
    actions: [{ label: "Try again", onclick: () => void model.retryRefresh() }],
    workspaceAgentId: model.agentId,
    extra: "shrink-0",
  });
}

/** Pending permission requests from this machine's agents, oldest first (the
 * one the agent has been blocked on longest leads). Each row opens the review
 * popup on that request. Hidden entirely when nothing is pending. */
/** The requests this machine's agents are waiting on, oldest first (the one
 * blocked longest leads). Each opens as its own page over this pane.
 *
 * The list lives in the pane -- so it scrolls with everything else, and sits
 * beside the connections its answers create -- while the request itself is read
 * on a page of its own, which is the room a dialog needs.
 */
function renderWaitingPanel(
  waitingRequests: UiWaitingPermissionRequest[],
  onReviewRequest: (requestId: string) => void,
): m.Children {
  return m("section", { "data-perm-panel": WAITING_SECTION }, [
    m("h2", { class: "type-heading text-primary flex items-center gap-2 mb-1" }, [
      "Waiting on you",
      m(Badge, { count: waitingRequests.length }),
    ]),
    m(
      "p",
      { class: "type-body text-secondary mb-4" },
      waitingRequests.length === 1
        ? "An agent in this workspace is waiting on an answer."
        : "Agents in this workspace are waiting on an answer. The one asked for longest is first.",
    ),
    m(
      "div",
      { class: "flex flex-col gap-0.5" },
      waitingRequests.map((waiting) => renderWaitingRow(waiting, onReviewRequest)),
    ),
  ]);
}

function renderNav(
  connections: UiPermissionConnection[],
  waitingRequests: UiWaitingPermissionRequest[],
  selected: string,
  selectSection: (section: string) => void,
): m.Children {
  const navEntry = (section: string, icon: m.Children, label: m.Children): m.Children =>
    m(
      "button",
      {
        type: "button",
        "data-perm-nav": section,
        "aria-pressed": section === selected ? "true" : "false",
        class: navEntryClass(section === selected),
        onclick: () => selectSection(section),
      },
      [icon, label],
    );

  return [
    // Waiting requests lead the nav: they are the one thing here that is
    // asking for an answer, and everything below is what past answers built.
    waitingRequests.length === 0
      ? null
      : [
          m(
            "div",
            { class: "flex flex-col gap-0.5" },
            navEntry(
              WAITING_SECTION,
              m(Icon16, { name: "key", extra: "shrink-0" }),
              m("span", { class: "flex min-w-0 flex-1 items-center gap-1.5" }, [
                m("span", { class: "truncate" }, "Waiting on you"),
                m(Badge, { count: waitingRequests.length }),
              ]),
            ),
          ),
          m("div", { class: "my-1.5 h-px bg-subtle" }),
        ],
    m("div", { class: "flex flex-col gap-0.5" }, [
      ...connections.map((connection) =>
        navEntry(
          connectionSectionId(connection),
          serviceMark(
            connection.service_name,
            "w-4 h-4 shrink-0",
            connection.is_connected ? "brand" : "muted",
            navFallbackMark(),
          ),
          m("span", { class: "truncate min-w-0" }, [
            connection.display_name,
            connection.show_account_label
              ? m("span", { class: "block type-helper text-tertiary truncate font-normal" }, connection.account_label)
              : null,
          ]),
        ),
      ),
      m(
        "button",
        {
          type: "button",
          "data-perm-nav": ADD_CONNECTION_SECTION,
          "aria-pressed": selected === ADD_CONNECTION_SECTION ? "true" : "false",
          class:
            selected === ADD_CONNECTION_SECTION ? navEntryClass(true) : ADD_CONNECTION_CLASS,
          onclick: () => selectSection(ADD_CONNECTION_SECTION),
        },
        [m(Icon16, { name: "plus", extra: "shrink-0" }), m("span", { class: "truncate" }, "Add connection")],
      ),
    ]),
    m("div", { class: "my-1.5 h-px bg-subtle" }),
    m("div", { class: "flex flex-col gap-0.5" }, [
      navEntry(
        LOCAL_FILES_SECTION,
        m(Icon16, { name: "box", extra: "shrink-0" }),
        m("span", { class: "truncate" }, "Local files"),
      ),
      navEntry(
        OTHER_MACHINES_SECTION,
        m(Icon16, { name: "panels-top-left", extra: "shrink-0" }),
        m("span", { class: "truncate" }, "Other workspaces"),
      ),
      navEntry(
        THIS_COMPUTER_SECTION,
        m(Icon16, { name: "triangle-alert", extra: "shrink-0" }),
        m("span", { class: "truncate" }, "This computer"),
      ),
    ]),
  ];
}

function renderSelectedPanel(
  model: PermissionsModel,
  local: PermissionsTabLocalState,
  data: UiWorkspacePermissions,
  machineName: string,
  selected: string,
  selectSection: (section: string) => void,
  onReviewRequest: (requestId: string) => void,
): m.Children {
  const connections: UiPermissionConnection[] = data.connections;
  if (selected === WAITING_SECTION) return renderWaitingPanel(data.waiting_requests, onReviewRequest);
  if (selected === LOCAL_FILES_SECTION) return renderLocalFilesPanel(model);
  if (selected === OTHER_MACHINES_SECTION) return renderOtherMachinesPanel(model);
  if (selected === THIS_COMPUTER_SECTION) return renderThisComputerPanel(model);
  if (selected === ADD_CONNECTION_SECTION) return renderAddConnectionPanel(model, selectSection);
  const connection = connections.find((entry) => connectionSectionId(entry) === selected);
  if (connection === undefined) return null;
  return renderConnectionPanel(model, local, connection, machineName, data.is_credential_store_shared, selectSection);
}

function renderConnectionPanel(
  model: PermissionsModel,
  local: PermissionsTabLocalState,
  connection: UiPermissionConnection,
  machineName: string,
  isCredentialStoreShared: boolean,
  selectSection: (section: string) => void,
): m.Children {
  const machineLabel = machineLabelFor(machineName);
  const disconnectKey = disconnectRowKey(connection.service_name, connection.account);
  const isDisconnectBusy = model.isRowBusy(disconnectKey);
  const isDisconnectLocked = isLockedByAnotherWrite(model, disconnectKey);
  return m("section", { "data-perm-panel": connectionSectionId(connection) }, [
    m("div", { class: "flex items-center justify-between gap-3 mb-1" }, [
      m("h2", { class: "type-heading text-primary flex items-center gap-2 min-w-0" }, [
        serviceMark(
          connection.service_name,
          "w-5 h-5 shrink-0",
          connection.is_connected ? "brand" : "muted",
          null,
        ),
        m("span", { class: "truncate" }, connection.display_name),
        // Same size and tone as the service name: the account is half of the
        // connection's identity, not an aside.
        m("span", { class: "type-heading text-primary truncate" }, `· ${connection.account_label}`),
      ]),
      connection.granted_count > 0 ? renderRevokeAllButton(model, local, connection) : null,
    ]),
    // A service with one scope has no scope heading to hang the implicit
    // permissions hint on, so the intro carries it; several scopes each
    // carry their own on their heading below.
    m("div", { class: "flex items-center gap-1.5 mb-4" }, [
      m("p", { class: "type-body text-secondary" }, "Choose what agents in this workspace can do."),
      connection.scopes.length === 1 ? renderImplicitPermissionsHint(connection.scopes[0].implicit_note) : null,
    ]),
    connection.is_connected
      ? null
      : m(
          Notice,
          { variant: "warn" },
          "This account isn't connected, so agents can't use these grants right now. Reconnect it " +
            "from Add connection, or turn the leftover grants off here.",
        ),
    connection.desktop_egress.is_supported ? renderDesktopEgressRow(model, local, connection) : null,
    connection.scopes.map((scopePanel) => [
      connection.scopes.length > 1
        ? m("h3", { class: "type-heading text-primary mt-6 mb-1 flex items-center gap-1.5" }, [
            m("span", scopePanel.heading),
            renderImplicitPermissionsHint(scopePanel.implicit_note),
          ])
        : null,
      scopePanel.groups.map((group) => [
        m("p", { class: "type-section text-tertiary mt-6 mb-1" }, group.heading),
        m(
          "div",
          { class: "flex flex-col pr-4" },
          group.toggles.map((toggle) => {
            const rowKey = connectorToggleRowKey(scopePanel.scope, connection.account, toggle.permission);
            return m("div", { class: TOGGLE_ROW_CLASS }, [
              m("div", { class: "min-w-0" }, [
                m("p", { class: "type-body text-primary truncate" }, toggle.label),
                toggle.description
                  ? m("p", { class: "type-helper text-tertiary mt-0.5" }, toggle.description)
                  : null,
              ]),
              renderSwitch({
                isGranted: toggle.is_granted,
                isBusy: model.isRowBusy(rowKey),
                isLocked: isLockedByAnotherWrite(model, rowKey),
                // A grant can always be turned OFF, even on a disconnected
                // account -- only turning one ON needs a live connection.
                isBlocked: !toggle.is_granted && !connection.is_connected,
                blockedTitle: CONNECTOR_TOGGLE_BLOCKED_TITLE,
                label: toggle.label,
                permission: toggle.permission,
                onFlip: (enabled) =>
                  void model.toggleConnector(scopePanel.scope, connection.account, toggle.permission, enabled),
              }),
            ]);
          }),
        ),
      ]),
    ]),
    // Disconnect sits at the FOOT of the panel, apart from the toggles and from
    // the heading's Revoke all: that one drops this machine's grants, this one
    // forgets the sign-in behind them. Only a connected account has one to
    // forget -- a leftover-grants row is Revoke all's job.
    connection.is_connected
      ? [
          m(SectionHeader, { divider: true }, "Disconnect"),
          m("p", { class: "type-body text-secondary mb-3" }, [
            "Disconnect from ",
            m("span", { class: "font-semibold" }, `${connection.display_name} · ${connection.account_label}`),
            isCredentialStoreShared
              ? `. ${machineLabel} shares one sign-in with every other workspace on this computer, so all ` +
                `of them lose this access. `
              : `. Only ${machineLabel} loses this access — every other workspace keeps its own sign-in. `,
            "Use Revoke all above to drop just this workspace's grants and keep it connected.",
          ]),
          m(
            Button,
            {
              variant: "danger",
              size: "md",
              "data-perm-disconnect": connection.service_name,
              disabled: isDisconnectBusy || isDisconnectLocked,
              onclick: () => {
                model.clearWriteError();
                disarmRevoke(local);
                local.disconnectSectionId = connectionSectionId(connection);
              },
            },
            isDisconnectBusy ? "Disconnecting..." : "Disconnect",
          ),
          renderDisconnectDialog(model, local, connection, machineLabel, isCredentialStoreShared, selectSection),
        ]
      : null,
  ]);
}

/** The disconnect confirm. An in-DOM Modal, like every other destructive
 * confirm in this card and like the settings page's own Disconnect: a native
 * confirm carries one plain string, and what has to be said here is WHICH
 * machines this reaches. Cancel only closes -- nothing is posted anywhere but
 * from the confirm's own onclick. */
function renderDisconnectDialog(
  model: PermissionsModel,
  local: PermissionsTabLocalState,
  connection: UiPermissionConnection,
  machineLabel: string,
  isCredentialStoreShared: boolean,
  selectSection: (section: string) => void,
): m.Children {
  const isOpen = local.disconnectSectionId === connectionSectionId(connection);
  const rowKey = disconnectRowKey(connection.service_name, connection.account);
  const isBusy = model.isRowBusy(rowKey);
  const isLocked = isLockedByAnotherWrite(model, rowKey);
  const close = (): void => {
    local.disconnectSectionId = null;
  };
  return m(
    Modal,
    { isOpen, onClose: close, id: "ws-perm-disconnect", cardExtra: "text-left" },
    // Guarded rather than left to Modal: a component vnode carries the children
    // it was handed whether or not they are rendered, so a closed dialog has to
    // contribute no buttons and no copy to the tree at all.
    isOpen
      ? [
          m(
            "h2",
            { class: "type-heading-lg text-primary mb-3" },
            isCredentialStoreShared
              ? `Disconnect ${connection.display_name} · ${connection.account_label} from this computer?`
              : `Disconnect ${connection.display_name} · ${connection.account_label} from ${machineLabel}?`,
          ),
          m("p", { class: "type-body text-primary mb-4" }, [
            "This will disconnect ",
            m("strong", `${connection.display_name} · ${connection.account_label}`),
            isCredentialStoreShared
              ? ` from every workspace on this computer, including ${machineLabel}. Their agents won't be able ` +
                "to use it until you connect it again."
              : ` from ${machineLabel}, whose agents won't be able to use it until you connect it again. ` +
                "Every other workspace keeps its own sign-in.",
          ]),
          model.writeError !== null
            ? m(
                "div",
                { class: "mb-4" },
                m(ErrorNotice, {
                  id: "ws-perm-disconnect-error",
                  error: model.writeError,
                  actions: remedyActions(model.writeErrorRemedy),
                  workspaceAgentId: model.agentId,
                }),
              )
            : null,
          m("div", { class: "flex justify-end gap-3" }, [
            m(
              Button,
              {
                variant: "secondary",
                "data-perm-disconnect-cancel": connection.service_name,
                onclick: close,
              },
              "Cancel",
            ),
            m(
              Button,
              {
                variant: "danger",
                "data-perm-disconnect-confirm": connection.service_name,
                disabled: isBusy || isLocked,
                onclick: () => {
                  void model.disconnect(connection).then((section) => {
                    // Refused: the dialog stays up holding the reason.
                    if (section === null) return;
                    close();
                    selectSection(section);
                  });
                },
              },
              isBusy ? "Disconnecting..." : "Yes, disconnect",
            ),
          ]),
        ]
      : null,
  );
}

/** Two-step confirm on the button itself: the first click arms it and relabels
 * it, the second within the window fires the revoke. */
function renderRevokeAllButton(
  model: PermissionsModel,
  local: PermissionsTabLocalState,
  connection: UiPermissionConnection,
): m.Children {
  const rowKey = revokeAllRowKey(connection.service_name, connection.account);
  const isArmed = local.armedRevokeRowKey === rowKey;
  const isBusy = model.isRowBusy(rowKey);
  return m(
    Button,
    {
      variant: "ghost",
      size: "md",
      extra: "shrink-0",
      "data-perm-revoke-all": connection.service_name,
      disabled: isBusy || isLockedByAnotherWrite(model, rowKey),
      onclick: () => {
        model.clearWriteError();
        if (!isArmed) {
          armRevoke(local, rowKey);
          return;
        }
        disarmRevoke(local);
        void model.revokeAll(connection.service_name, connection.account, connection.display_name);
      },
    },
    isBusy ? "Revoking..." : isArmed ? "Really revoke all?" : "Revoke all",
  );
}

function armRevoke(local: PermissionsTabLocalState, rowKey: string): void {
  disarmRevoke(local);
  local.armedRevokeRowKey = rowKey;
  local.disarmTimer = setTimeout(() => {
    local.disarmTimer = null;
    local.armedRevokeRowKey = null;
    m.redraw();
  }, REVOKE_CONFIRM_WINDOW_MS);
}

function disarmRevoke(local: PermissionsTabLocalState): void {
  if (local.disarmTimer !== null) clearTimeout(local.disarmTimer);
  local.disarmTimer = null;
  local.armedRevokeRowKey = null;
}

function renderAddConnectionPanel(
  model: PermissionsModel,
  selectSection: (section: string) => void,
): m.Children {
  const connected = connectedServices(model.data?.connections ?? []);
  const available = model.data?.available_connections ?? [];
  return m("section", { "data-perm-panel": ADD_CONNECTION_SECTION }, [
    m("h2", { class: "type-heading text-primary mb-1" }, "Add connection"),
    m(
      "p",
      { class: "type-body text-secondary mb-4" },
      "Connect a service to grant agents access to it. Connecting signs you in, or asks for the " +
        "service's credentials; nothing is allowed until you turn permissions on.",
    ),
    connected.length === 0
      ? null
      : [
          m("h3", { class: CATALOG_HEADING_CLASS }, "Add another account"),
          renderCatalogList(model, connected, "Add account", selectSection, "brand"),
        ],
    // The second heading exists to set the lists apart, so it appears only
    // when the first list is there to be set apart from.
    connected.length === 0 ? null : m("h3", { class: CATALOG_HEADING_CLASS }, "Connect a new service"),
    available.length === 0
      ? m(Notice, { variant: "info" }, "Every available service already has an account connected.")
      : renderCatalogList(model, available, "Connect", selectSection, "muted"),
  ]);
}

/** The services with at least one account, once each: a service can hold
 * several accounts but offers a single row for adding the next one. Every
 * account of a service carries the same sign-in, so the first one speaks for
 * the service. */
function connectedServices(connections: UiPermissionConnection[]): UiAvailableConnection[] {
  const services = new Map<string, UiAvailableConnection>();
  for (const connection of connections) {
    if (services.has(connection.service_name)) continue;
    services.set(connection.service_name, {
      service_name: connection.service_name,
      display_name: connection.display_name,
      sign_in: connection.sign_in,
    });
  }
  return [...services.values()];
}

/** One catalog row per service. Both lists land on the connection they
 * produced; only the wording differs, since adding a second account to a
 * service is not the same story as connecting it at all.
 *
 * `tone` carries the same meaning it does everywhere else in the pane: a mark
 * is in its brand colors once the service has an account on this machine, and
 * drained when it does not. So the "Add another account" list reads in color
 * and the "Connect a new service" list reads grey, matching the left nav.
 *
 * What the action does depends on the service: a browser sign-in POSTs the
 * settings page's route, while a service latchkey cannot sign in to opens its
 * credential form under the row instead. */
function renderCatalogList(
  model: PermissionsModel,
  services: UiAvailableConnection[],
  actionLabel: string,
  selectSection: (section: string) => void,
  tone: MarkTone,
): m.Children {
  return m(
    "div",
    { class: "flex flex-col" },
    services.map((service) =>
      m("div", { class: "border-b border-subtle" }, [
        m("div", { class: "flex items-center justify-between gap-3 py-2" }, [
          m("div", { class: "flex items-center gap-2 min-w-0" }, [
            serviceMark(service.service_name, "w-4 h-4 shrink-0", tone, catalogFallbackMark()),
            m("span", { class: "type-body text-primary truncate" }, service.display_name),
          ]),
          renderCatalogAction(model, service, actionLabel, selectSection),
        ]),
        model.credentialFormServiceName === service.service_name
          ? renderCredentialForm(model, service, actionLabel, selectSection)
          : null,
      ]),
    ),
  );
}

function renderCatalogAction(
  model: PermissionsModel,
  service: UiAvailableConnection,
  actionLabel: string,
  selectSection: (section: string) => void,
): m.Children {
  const rowKey = connectServiceRowKey(service.service_name);
  const isBusy = model.isRowBusy(rowKey);
  const action = connectActionFor(service.sign_in);
  const attrs = {
    variant: "secondary" as const,
    size: "md" as const,
    extra: "shrink-0",
    "data-perm-connect": service.service_name,
    disabled: isBusy || isLockedByAnotherWrite(model, rowKey) || action === "unconnectable",
    ...(action === "unconnectable" ? { title: unconnectableTitle(service.display_name) } : {}),
  };
  if (action === "credential_form") {
    // Opening is a toggle: a second click closes the form rather than emptying
    // the one the user is in the middle of filling in.
    const isFormOpen = model.credentialFormServiceName === service.service_name;
    return m(
      Button,
      {
        ...attrs,
        onclick: () => {
          model.clearWriteError();
          if (isFormOpen) {
            model.closeCredentialForm();
            return;
          }
          model.openCredentialForm(service.service_name);
        },
      },
      actionLabel,
    );
  }
  const connect = (): void => {
    void model.connectService(service.service_name, connect).then((section) => {
      if (section !== null) selectSection(section);
    });
  };
  return m(
    Button,
    {
      ...attrs,
      onclick: connect,
    },
    isBusy ? "Waiting for sign-in..." : actionLabel,
  );
}

/** The credential form for a service with no browser sign-in: one input per
 * value its own command asks for, plus a name for the account when the service
 * already has one. The command itself is never shown -- Imbue Studio runs it, so it is
 * not something the user has to know about. */
function renderCredentialForm(
  model: PermissionsModel,
  service: UiAvailableConnection,
  actionLabel: string,
  selectSection: (section: string) => void,
): m.Children {
  const signIn = service.sign_in;
  const rowKey = connectServiceRowKey(service.service_name);
  const isBusy = model.isRowBusy(rowKey);
  const isLocked = isLockedByAnotherWrite(model, rowKey);
  const isComplete = isCredentialFormComplete(signIn, model.credentialValues, model.credentialAccountName);
  return m(
    "div",
    {
      "data-perm-credential-form": service.service_name,
      class: "mb-3 rounded-md border border-default bg-fill-subtle p-3 flex flex-col gap-2",
    },
    [
      m(
        "p",
        { class: "type-body text-secondary" },
        `${service.display_name} can't be signed in to through a browser, so Imbue Studio needs its ` +
          "credentials. Get them from the provider and fill them in — they are stored on this computer.",
      ),
      ...signIn.credential_parameters.map((parameter) =>
        m("label", { class: "flex flex-col gap-1" }, [
          m("span", { class: "type-label text-secondary" }, parameter.label),
          m(TextInput, {
            name: `credential-${parameter.name}`,
            autocomplete: "off",
            spellcheck: "false",
            value: model.credentialValues[parameter.name] ?? "",
            oninput: (event: Event) => {
              model.credentialValues[parameter.name] = (event.target as HTMLInputElement).value;
            },
          }),
        ]),
      ),
      signIn.is_account_name_required
        ? m("label", { class: "flex flex-col gap-1" }, [
            m("span", { class: "type-label text-secondary" }, "Account name"),
            m(TextInput, {
              name: "account_name",
              autocomplete: "off",
              spellcheck: "false",
              value: model.credentialAccountName,
              oninput: (event: Event) => {
                model.credentialAccountName = (event.target as HTMLInputElement).value;
              },
            }),
            m("span", { class: "type-helper text-tertiary" }, "How this account is labelled in Imbue Studio."),
          ])
        : null,
      model.credentialError !== null
        ? m(ErrorNotice, {
            id: "ws-perm-credential-error",
            error: model.credentialError,
            actions: remedyActions(model.credentialErrorRemedy),
            isBugReportOffered: model.isCredentialErrorReportable,
            workspaceAgentId: model.agentId,
          })
        : null,
      m("div", { class: "flex items-center gap-2" }, [
        m(
          Button,
          {
            variant: "primary",
            size: "md",
            "data-perm-credential-submit": service.service_name,
            disabled: isBusy || isLocked || !isComplete,
            onclick: () => {
              void model.connectWithCredentials(service.service_name).then((section) => {
                if (section !== null) selectSection(section);
              });
            },
          },
          isBusy ? "Connecting..." : actionLabel,
        ),
        m(
          Button,
          { variant: "ghost", size: "md", disabled: isBusy, onclick: () => model.closeCredentialForm() },
          "Cancel",
        ),
      ]),
    ],
  );
}

function renderLocalFilesPanel(model: PermissionsModel): m.Children {
  const rows = model.data?.shared_paths ?? [];
  const isSyncSupported = model.data?.is_sync_supported === true;
  return m("section", { "data-perm-panel": LOCAL_FILES_SECTION }, [
    m("h2", { class: "type-heading text-primary mb-1" }, "Local files"),
    m(
      "p",
      { class: "type-body text-secondary mb-4" },
      "Files and folders on this computer that agents in this workspace can reach.",
    ),
    rows.length === 0
      ? m(Notice, { variant: "info" }, "No files are being shared with agents in this workspace yet.")
      : m(
          "div",
          { class: "flex flex-col gap-3" },
          rows.map((row) => renderSharedPathRow(model, row, isSyncSupported)),
        ),
    renderAddSharedPath(model),
  ]);
}

/** The band a setting sits in. Each is divided from the one above by a
 * full-width hairline, so a row of any height still reads as one list. */
const SETTING_BAND_CLASS = "px-4 py-3 border-t border-subtle";

/** One shared path: what it is, and what agents may do with it.
 *
 * Drawn as bands -- the path, then one per setting -- rather than as a stack
 * inside one box. A setting here can grow a paragraph or a sub-control under
 * it, and a hairline that reaches both edges is what keeps that from reading
 * as a second, unrelated section of the card.
 */
function renderSharedPathRow(model: PermissionsModel, row: UiSharedPath, isSyncSupported: boolean): m.Children {
  const isRemoving = model.isRowBusy(pathRemoveRowKey(row.path));
  const isSynced = isPathSyncOn(row);
  return m("div", { class: "minds-card overflow-hidden", "data-shared-path": row.path }, [
    m("div", { class: "flex items-center justify-between gap-4 px-4 py-3" }, [
      m("code", { class: "code-pill truncate min-w-0", "data-tooltip": row.path }, row.path_label),
      m(
        Button,
        {
          variant: "danger",
          size: "md",
          disabled: isRemoving,
          "data-remove-path": row.path,
          onclick: () => void model.removeSharedPath(row.path),
        },
        removeButtonLabel(row, isRemoving),
      ),
    ]),
    m("div", { class: SETTING_BAND_CLASS }, renderAccessChoice(model, row)),
    isSyncSupported ? m("div", { class: SETTING_BAND_CLASS }, renderSyncChoice(model, row, isSynced)) : null,
  ]);
}

/** What Remove takes away, said on the button rather than in a warning.
 *
 * Removing a shared path deletes the machine's copy of it along with the
 * grant, and the copy is files the user may care about. The button is the only
 * place that can be said in every case: a path whose copy was already removed
 * draws no notice under the checkbox to carry it. The same distinction holds
 * while the removal runs, so the two halves are never one label apart. */
function removeButtonLabel(row: UiSharedPath, isRemoving: boolean): string {
  if (hasCopyOnMachine(row)) return isRemoving ? "Revoking and removing..." : "Revoke access and remove copy";
  return isRemoving ? "Revoking..." : "Revoke access";
}

/** The access setting, with the spinner every write in this pane wears.
 *
 * Changing it is a round trip to the workspace's own machine -- the grant is
 * not made until that machine has taken it -- so the dropdown showing the new
 * value before the write lands would be showing something that is not true
 * yet. Disabled and spinning says the same thing honestly. */
function renderAccessChoice(model: PermissionsModel, row: UiSharedPath): m.Children {
  const rowKey = pathAccessRowKey(row.path);
  const isBusy = model.isRowBusy(rowKey);
  const isLocked = isLockedByAnotherWrite(model, rowKey);
  return renderSettingRow(
    "Agents in this workspace may",
    m("span", { class: "flex items-center gap-2" }, [
      isBusy ? m(Spinner, { size: "sm" }) : null,
      m(
        "select",
        {
          class: SETTING_SELECT_CLASS,
          "data-access-path": row.path,
          value: row.access,
          disabled: isBusy || isLocked,
          ...(isLocked ? { title: PANE_BUSY_TITLE } : {}),
          onchange: (event: Event) =>
            void model.setSharedPath(row.path, (event.target as HTMLSelectElement).value as FileSharingAccess),
        },
        FILE_SHARING_ACCESSES.map((access) =>
          m("option", { value: access, selected: access === row.access }, fileSharingAccessLabel(access)),
        ),
      ),
    ]),
  );
}

/** The card's sync band: the shared setting, wired to the model's writes and
 * carrying what only the card has to say -- the live status, the set-aside
 * copy, and a failed sync's reason and retry. */
function renderSyncChoice(model: PermissionsModel, row: UiSharedPath, isSynced: boolean): m.Children {
  const sync = row.sync ?? null;
  const rowKey = folderSyncToggleRowKey(row.path);
  return renderFolderSyncSetting({
    path: row.path,
    access: row.access,
    isOn: isSynced,
    isBusy: model.isRowBusy(rowKey),
    lockedTitle: isLockedByAnotherWrite(model, rowKey) ? PANE_BUSY_TITLE : null,
    unavailableReason: row.sync_unavailable_reason ?? "",
    conflict: model.syncConflictFor(row),
    onToggle: (enabled) => void model.toggleSync(row, enabled),
    onConflictChange: (conflict) => void model.setSyncConflict(row, conflict),
    status: sync === null ? null : renderSyncStatus(sync.state, syncProgressLabel(sync)),
    overlapWarning: row.sync_overlap_warning,
    trailing: [
      isSynced ? null : renderInactiveCopy(model, row),
      sync === null || sync.message === ""
        ? null
        : m("div", { class: "flex items-start justify-between gap-3" }, [
            m(
              "div",
              { class: "min-w-0" },
              m(InlineError, { message: sync.message }),
            ),
            sync.state !== "FAILED"
              ? null
              : m(
                  Button,
                  {
                    variant: "secondary",
                    disabled: model.isRowBusy(folderSyncRetryRowKey(row.path)),
                    "data-sync-retry-path": row.path,
                    onclick: () => void model.retrySync(row),
                  },
                  model.isRowBusy(folderSyncRetryRowKey(row.path)) ? "Trying..." : "Try again",
                ),
          ]),
    ],
  });
}

function syncStateVariant(state: FolderSyncState): StatusBadgeVariant {
  if (state === "SYNCED") return "success";
  // Everything under way reads the same, in either direction: the folder is
  // not where the user asked for it yet.
  if (
    state === "SYNCING" ||
    state === "STARTING" ||
    state === "RESTARTING" ||
    state === "DEACTIVATING" ||
    state === "DISCARDING"
  ) {
    return "info";
  }
  if (state === "FAILED") return "error";
  // Wanted, and not happening. Not an error -- nothing reported one -- but not
  // the neutral grey of a folder the user turned off either.
  if (state === "UNKNOWN") return "warn";
  return "neutral";
}

/** What became of the machine's copy once syncing is off.
 *
 * Turning sync off does not throw the copy away -- it is set aside, so turning
 * sync back on resumes from the files that were already there. That leaves the
 * user with a thing they may not want, which nothing else in the pane would
 * mention, so the row says where it is and offers to remove it. What removing
 * the whole shared path would do to the copy is left to that button to say,
 * since it has to say it in the case where this row is not drawn at all.
 */
function renderInactiveCopy(model: PermissionsModel, row: UiSharedPath): m.Children {
  if (!hasInactiveCopy(row)) return null;
  const isDiscarding = model.isRowBusy(folderSyncDiscardCopyRowKey(row.path));
  return m("div", { class: "flex items-start justify-between gap-4" }, [
    m("div", { class: "min-w-0 flex flex-col gap-0.5" }, [
      m("span", { class: "type-body text-secondary" }, "A copy is still in the workspace"),
      m("span", { class: "type-helper text-tertiary" }, "Turning syncing back on picks up where it left off."),
    ]),
    m(
      Button,
      {
        // Destructive, but ranked under the row's own Remove: that one takes
        // the whole shared path away, this one throws away a copy of something
        // the user still has. Two filled reds in one card read as two equal
        // warnings; a neutral button here read as Cancel, which is wrong for a
        // button whose only effect is a deletion.
        variant: "danger-soft",
        disabled: isDiscarding,
        "data-discard-copy-path": row.path,
        onclick: () => void model.discardCopy(row),
      },
      isDiscarding ? "Removing..." : "Remove copy",
    ),
  ]);
}

/** The legend: a turning pair of arrows while bytes move, a check once they
 * have. Everything else keeps a plain badge.
 *
 * The one row with nothing to say is a sync that is settled off, which is what
 * STOPPED means and the only thing it means: the unticked checkbox already
 * says it, and a badge beside it only asks the reader to reconcile the two. A
 * folder the user still wants synced with nothing running is UNKNOWN, not
 * STOPPED, so it keeps its badge without this having to ask a second question
 * to tell the two apart. */
function renderSyncStatus(state: FolderSyncState, progress = ""): m.Children {
  if (state === "STOPPED") return null;
  const label = folderSyncStateLabel(state);
  if (state === "SYNCING") {
    // Blue against the settled green: something is happening, and it is not
    // yet the state the user is waiting to see. The count beside it is
    // unison's own, so a big folder says how far through it is rather than
    // just that it is busy.
    return m("span", { class: "flex items-center gap-1.5 type-helper text-info" }, [
      m(Icon16, { name: "arrows-sync", size: "sm", extra: "sync-spin" }),
      progress === "" ? label : `${label} · ${progress}`,
    ]);
  }
  if (state === "SYNCED") {
    return m("span", { class: "flex items-center gap-1.5 type-helper text-success" }, [
      m(Icon16, { name: "badge-check", size: "sm" }),
      label,
    ]);
  }
  return m(StatusBadge, { variant: syncStateVariant(state), size: "xs" }, label);
}

async function addSharedPath(model: PermissionsModel, mode: "file" | "directory"): Promise<void> {
  const selected = await electronBridge.showFilePicker({ mode }).catch(() => null);
  if (typeof selected !== "string" || selected.length === 0) return;
  await model.addSharedPath(selected);
}

/** The two ways in, at the foot of the list: pick something, or let an agent
 * ask. Read-only to begin with either way -- the row's dropdown widens it, and
 * starting narrow is the safer default for a path the user just pointed at. */
function renderAddSharedPath(model: PermissionsModel): m.Children {
  if (!electronBridge.isDesktop) {
    return m(
      "p",
      { class: "type-helper text-tertiary mt-4" },
      "Tell an agent to request access to a local file or directory.",
    );
  }
  // Sharing a path is a round trip to the workspace's own machine, like every
  // other write here, so it wears the same spinner -- and locks the same way,
  // because a write answers with the whole view and two in flight would fight
  // over what is on screen.
  const isAdding = model.isRowBusy(ADD_SHARED_PATH_ROW_KEY);
  const isAddLocked = isLockedByAnotherWrite(model, ADD_SHARED_PATH_ROW_KEY);
  // A desktop with no device id cannot name the computer a file is on, so it
  // has nothing it can share.
  const canShareFiles = model.data?.can_share_files ?? true;
  const addTitle = !canShareFiles ? CANNOT_SHARE_FILES_SENTENCE : isAddLocked ? PANE_BUSY_TITLE : null;
  return m("div", { class: "mt-4 flex flex-col items-end gap-2" }, [
    m("div", { class: "flex items-center gap-2" }, [
      isAdding ? m(Spinner, { size: "sm" }) : null,
      m(
        Button,
        {
          variant: "secondary",
          id: "add-shared-file",
          disabled: isAdding || isAddLocked || !canShareFiles,
          ...(addTitle === null ? {} : { title: addTitle }),
          onclick: () => void addSharedPath(model, "file"),
        },
        "Add file",
      ),
      m(
        Button,
        {
          variant: "secondary",
          id: "add-shared-folder",
          disabled: isAdding || isAddLocked || !canShareFiles,
          ...(addTitle === null ? {} : { title: addTitle }),
          onclick: () => void addSharedPath(model, "directory"),
        },
        "Add folder",
      ),
    ]),
    m(
      "p",
      { class: "type-helper text-tertiary" },
      canShareFiles ? "Or tell an agent to request access to a local file or directory." : CANNOT_SHARE_FILES_SENTENCE,
    ),
  ]);
}

function renderOtherMachinesPanel(model: PermissionsModel): m.Children {
  const toggles = model.data?.workspace_toggles ?? [];
  return m("section", { "data-perm-panel": OTHER_MACHINES_SECTION }, [
    m("h2", { class: "type-heading text-primary mb-1" }, "Other workspaces"),
    m(
      "p",
      { class: "type-body text-secondary mb-4" },
      "What agents in this workspace are allowed to do to your other workspaces (listing, creating, " +
        "destroying, SSH, and more). To grant more, ask an agent to perform the operation.",
    ),
    toggles.length === 0
      ? m(Notice, { variant: "info" }, "Agents in this workspace can't manage your other workspaces yet.")
      : m(
          "div",
          { class: "flex flex-col pr-4" },
          toggles.map((toggle) =>
            m("div", { class: TOGGLE_ROW_CLASS }, [
              m("div", { class: "min-w-0" }, [
                m("p", { class: "type-body text-primary flex items-center gap-2 min-w-0" }, [
                  m("span", { class: "truncate" }, toggle.label),
                  m("span", { class: "type-helper text-tertiary shrink-0" }, "on:"),
                  m("span", { class: "type-body text-secondary truncate" }, toggle.detail),
                ]),
                toggle.description
                  ? m("p", { class: "type-helper text-tertiary mt-0.5" }, toggle.description)
                  : null,
              ]),
              renderSelfSwitch(model, toggle, `${toggle.label} on ${toggle.detail}`),
            ]),
          ),
        ),
  ]);
}

/** The This computer panel: which of the user's computers this workspace's
 * agents may run commands on. Revoke-only -- granting goes through the one
 * dialog that carries the warning -- and inert while this device's master
 * switch is off, which the panel says rather than hiding the rows. */
function renderThisComputerPanel(model: PermissionsModel): m.Children {
  const toggles = model.data?.remote_execution_toggles ?? [];
  const isEnabled = model.data?.is_remote_execution_enabled === true;
  return m("section", { "data-perm-panel": THIS_COMPUTER_SECTION }, [
    m("h2", { class: "type-heading text-primary mb-1" }, "This computer"),
    m(
      "p",
      { class: "type-body text-secondary mb-4" },
      "Whether agents in this workspace may run commands on your computers, as you. " +
        "An agent asks for this from inside the workspace, and you approve it there; here you can only take it away.",
    ),
    toggles.length === 0
      ? m(
          Notice,
          { variant: "info" },
          "Agents in this workspace can't run commands on your computers.",
        )
      : [
          isEnabled
            ? null
            : m(
                Notice,
                { variant: "warn", id: "remote-execution-disabled-notice" },
                "Disabled: agents are not allowed to run commands on this computer. " +
                  "Change that under Settings > This computer.",
              ),
          m(
            "div",
            { class: "flex flex-col pr-4" },
            toggles.map((toggle) =>
              m("div", { class: TOGGLE_ROW_CLASS, "data-perm-remote-execution": toggle.permission }, [
                m("div", { class: "min-w-0" }, [
                  m("p", { class: "type-body text-primary flex items-center gap-2 min-w-0" }, [
                    m("span", { class: "truncate" }, toggle.label),
                    toggle.detail
                      ? m("span", { class: "type-helper text-tertiary shrink-0" }, toggle.detail)
                      : null,
                  ]),
                  toggle.description
                    ? m("p", { class: "type-helper text-tertiary mt-0.5" }, toggle.description)
                    : null,
                ]),
                renderSelfSwitch(model, toggle, `Run commands on ${toggle.label}`),
              ]),
            ),
          ),
        ],
  ]);
}

/** The per-service desktop egress row. Drawn in every account panel of the
 * service with the same value, since the server keys it by service alone.
 *
 * The switch writes at once, like every other switch in the pane. The editor
 * works on a draft instead: a route is rearranged over several clicks, and
 * sending each one would push a string of routes nobody meant to the machine. */
function renderDesktopEgressRow(
  model: PermissionsModel,
  local: PermissionsTabLocalState,
  connection: UiPermissionConnection,
): m.Children {
  const serviceName = connection.service_name;
  const rowKey = desktopEgressRowKey(serviceName);
  const savedRoute = connection.desktop_egress.route;
  const isBusy = model.isRowBusy(rowKey);
  const held = local.desktopEgressDraft;
  const draft = held !== null && held.model === model && held.serviceName === serviceName ? held : null;
  return m(
    "div",
    { class: "perm-row pr-4 py-2", "data-perm-desktop-egress": serviceName },
    renderDesktopEgressRouteSetting({
      serviceName,
      route: savedRoute,
      desktops: model.data?.desktops ?? [],
      isBusy,
      lockedTitle: isLockedByAnotherWrite(model, rowKey) ? PANE_BUSY_TITLE : null,
      onSwitch: (route) => {
        void model.setDesktopEgressRoute(serviceName, route).then((isSaved) => {
          // A flip that lands replaces the route the draft started from; a
          // refused one leaves the route, and so the draft, as they were.
          if (isSaved && draft !== null && local.desktopEgressDraft === draft) local.desktopEgressDraft = null;
        });
      },
      onAdjust: () => {
        local.desktopEgressDraft = { model, serviceName, route: [...savedRoute] };
      },
      editor:
        draft === null
          ? null
          : {
              route: draft.route,
              onChange: (route) => {
                local.desktopEgressDraft = { model, serviceName, route };
              },
              onClose: () => {
                local.desktopEgressDraft = null;
              },
              isSaveDisabled: areRoutesEqual(draft.route, savedRoute),
              onSave: () => {
                void model.setDesktopEgressRoute(serviceName, draft.route).then((isSaved) => {
                  // Refused: the editor stays open on the draft, beside the reason.
                  if (isSaved && local.desktopEgressDraft === draft) local.desktopEgressDraft = null;
                });
              },
            },
    }),
  );
}

function renderSelfSwitch(
  model: PermissionsModel,
  toggle: UiSelfPermissionToggle,
  ariaLabel: string,
): m.Children {
  const rowKey = selfToggleRowKey(toggle.permission);
  return renderSwitch({
    isGranted: toggle.is_granted,
    isBusy: model.isRowBusy(rowKey),
    isLocked: isLockedByAnotherWrite(model, rowKey),
    // A grant whose schema is gone can still be turned off; turning it back on
    // has to come from the agent asking again.
    isBlocked: !toggle.is_granted && !toggle.can_enable,
    blockedTitle: SELF_TOGGLE_BLOCKED_TITLE,
    label: ariaLabel,
    permission: toggle.permission,
    onFlip: (enabled) => void model.toggleSelf(toggle.permission, enabled),
  });
}

interface SwitchOptions {
  isGranted: boolean;
  isBusy: boolean;
  isLocked: boolean;
  isBlocked: boolean;
  blockedTitle: string;
  label: string;
  permission: string;
  onFlip: (enabled: boolean) => void;
}

/** A permission switch, inert while any write other than its own runs: the
 * response to a write is the whole view, so two in flight would fight over
 * what is on screen. */
function renderSwitch(options: SwitchOptions): m.Children {
  const { isGranted, isLocked, isBlocked } = options;
  return renderPermissionSwitch({
    isOn: isGranted,
    isBusy: options.isBusy,
    isDisabled: isBlocked || isLocked,
    title: isBlocked ? options.blockedTitle : isLocked ? PANE_BUSY_TITLE : null,
    label: options.label,
    permission: options.permission,
    onFlip: options.onFlip,
  });
}


/** One waiting request: a row that opens it. */
function renderWaitingRow(
  waiting: UiWaitingPermissionRequest,
  onReviewRequest: (requestId: string) => void,
): m.Children {
  return m(
    "button",
    {
      type: "button",
      "data-perm-waiting-id": waiting.id,
      class: "flex w-full items-center gap-2 rounded-md px-1 py-2 text-left cursor-pointer hover:bg-fill-hover",
      // Pointing at the row starts fetching what opening it will show. The
      // detail costs a latchkey probe server-side, so starting it on the way in
      // is the difference between opening onto the request and onto a spinner.
      onpointerenter: () => warmRequestDetail(waiting.id),
      onfocus: () => warmRequestDetail(waiting.id),
      onclick: () => onReviewRequest(waiting.id),
    },
    [
      m(
        "span",
        { class: "flex h-5 w-5 shrink-0 items-center justify-center" },
        serviceMark(waiting.service_name, "w-4 h-4", "brand", m(Icon16, { name: "key", extra: "text-primary" })),
      ),
      m("span", { class: "min-w-0 flex-1" }, [
        m("span", { class: "block truncate type-label text-primary" }, waiting.title),
        waiting.reason
          ? m("span", { class: "mt-0.5 block truncate type-helper text-secondary" }, waiting.reason)
          : null,
      ]),
      m(Icon16, { name: "chevron-right", size: "sm", extra: "shrink-0 text-tertiary" }),
    ],
  );
}
