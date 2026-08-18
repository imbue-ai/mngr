// The Permissions tab: everything agents in this machine can reach, as
// toggles. Left nav is one entry per connection (a signed-in or granted
// (service, account) pair), then Add connection, then the two latchkey-self
// families -- Local files (shared paths) and Other machines (cross-workspace
// verbs). The right pane holds the selected entry's toggles.
//
// Port of WorkspacePermissionsSection.jinja + workspace_permissions.js. The
// one behavior change: a flip re-renders from the server's refreshed view
// (PermissionsModel adopts the response) instead of flipping optimistically
// and reverting, so a control never shows a state that was not stored.

import m from "mithril";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { TextInput } from "../../components/FormControls";
import { Icon16 } from "../../components/Icon";
import { SectionHeader } from "../../components/Layout";
import { Modal } from "../../components/Modal";
import { Notice } from "../../components/Notice";
import type { MarkTone } from "../../components/ServiceMark";
import { serviceMark } from "../../components/ServiceMark";
import { Spinner } from "../../components/Spinner";
import type {
  UiAvailableConnection,
  UiPermissionConnection,
  UiSelfPermissionToggle,
  UiWaitingPermissionRequest,
  UiWorkspacePermissions,
} from "../../../generated/ui";
import type { PermissionsModel } from "../../../models/workspacePermissions";
import {
  ADD_CONNECTION_SECTION,
  LOCAL_FILES_SECTION,
  OTHER_MACHINES_SECTION,
  WAITING_SECTION,
  connectActionFor,
  connectServiceRowKey,
  connectionSectionId,
  connectorToggleRowKey,
  isCredentialFormComplete,
  resolvePermissionsSection,
  disconnectRowKey,
  revokeAllRowKey,
  selfToggleRowKey,
} from "../../../models/workspacePermissions";
import { navEntryClass, splitPane } from "../../components/SplitPane";
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
 * it. Built per call -- one vnode cannot appear twice in a tree. */
const navFallbackMark = (): m.Children => m(Icon16, { name: "box", extra: "shrink-0" });
const catalogFallbackMark = (): m.Children => m(Icon16, { name: "box", extra: "shrink-0 text-tertiary" });

const SELF_TOGGLE_BLOCKED_TITLE =
  "This grant can't be re-enabled; ask the agent to request it again.";
const CONNECTOR_TOGGLE_BLOCKED_TITLE = "Connect this account before granting permissions.";

/** Why a service offers no action at all: latchkey cannot sign in to it and
 * told us nothing about the credentials it takes, so there is nothing to ask
 * for. Shown on hover, in place of a button that could only fail. */
function unconnectableTitle(displayName: string): string {
  return `Minds can't work out which credentials ${displayName} needs, so it has to be connected another way.`;
}

export interface PermissionsTabAttrs {
  model: PermissionsModel;
  /** Machine name for the heading; '' when the options load has not landed. */
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
}

export function PermissionsTab(): m.Component<PermissionsTabAttrs> {
  const local: PermissionsTabLocalState = {
    armedRevokeRowKey: null,
    disarmTimer: null,
    disconnectSectionId: null,
  };

  return {
    oninit(vnode) {
      // Lazy: the tab is only mounted while it is the selected one, and the
      // model no-ops a second call, so reopening the tab neither refetches
      // nor blanks the pane.
      vnode.attrs.model.ensureLoaded();
    },
    onremove() {
      disarmRevoke(local);
      local.disconnectSectionId = null;
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
            : "What agents in this machine can access. They can never reach beyond the permissions you grant them.",
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
  return machineName === "" ? "this machine" : machineName;
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
  if (model.status === "load_failed") {
    return m("div", { class: "mt-6 shrink-0 flex flex-col gap-3 items-start" }, [
      m(Notice, { variant: "warn" }, `Permissions can't be loaded right now: ${model.errorMessage}`),
      m(Button, { variant: "secondary", onclick: () => void model.load() }, "Try again"),
    ]);
  }
  const data = model.data;
  if (data === null || data.permissions_unavailable) {
    // The gateway (or the machine's host) is unreachable: say so rather than
    // rendering an empty tree that reads as "nothing is granted".
    return m(
      "div",
      { class: "mt-6 shrink-0" },
      m(Notice, { variant: "warn" }, "Permissions can't be loaded right now. Try again in a moment."),
    );
  }

  // Waiting on you is a section like any other: what is selected is whatever
  // the URL asks for.
  const selected = resolvePermissionsSection(data, requestedSection);
  const selectSection = (section: string): void => {
    disarmRevoke(local);
    local.disconnectSectionId = null;
    model.clearErrorMessage();
    onSelectSection(section);
  };

  return [
    splitPane({
      navLabel: "Permission sections",
      nav: renderNav(data.connections, data.waiting_requests, selected, selectSection),
      content: [
        model.errorMessage
          ? m(
              "p",
              { id: "ws-perm-error", class: "type-body text-important mb-3 shrink-0", role: "alert" },
              model.errorMessage,
            )
          : null,
        renderSelectedPanel(model, local, data, machineName, selected, selectSection, onReviewRequest),
      ],
      extra: "mt-8",
      // pb-8 keeps the panel foot (the Disconnect button) off the pane's
      // bottom edge when scrolled to the end.
      contentExtra: "flex flex-col pb-8",
    }),
    // Fixed-position when open and nothing at all when closed, so it rides
    // beside the pane rather than as a third column inside it.
    renderConnectFailurePopup(model),
  ];
}

// -- Waiting on you -----------------------------------------------------------

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
        ? "An agent in this machine is waiting on an answer."
        : "Agents in this machine are waiting on an answer. The one asked for longest is first.",
    ),
    m(
      "div",
      { class: "flex flex-col gap-0.5" },
      waitingRequests.map((waiting) => renderWaitingRow(waiting, onReviewRequest)),
    ),
  ]);
}

// -- Left nav -----------------------------------------------------------------

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
        m("span", { class: "truncate" }, "Other machines"),
      ),
    ]),
  ];
}

// -- Right pane ---------------------------------------------------------------

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
  if (selected === ADD_CONNECTION_SECTION) return renderAddConnectionPanel(model, selectSection);
  const connection = connections.find((entry) => connectionSectionId(entry) === selected);
  if (connection === undefined) return null;
  return renderConnectionPanel(model, local, connection, machineName, selectSection);
}

function renderConnectionPanel(
  model: PermissionsModel,
  local: PermissionsTabLocalState,
  connection: UiPermissionConnection,
  machineName: string,
  selectSection: (section: string) => void,
): m.Children {
  const machineLabel = machineLabelFor(machineName);
  const isDisconnectBusy = model.isRowBusy(disconnectRowKey(connection.service_name, connection.account));
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
    m("p", { class: "type-body text-secondary mb-4" }, "Choose what agents in this machine can do."),
    connection.is_connected
      ? null
      : m(
          Notice,
          { variant: "warn" },
          "This account isn't connected, so agents can't use these grants right now. Reconnect it " +
            "from Add connection, or turn the leftover grants off here.",
        ),
    connection.scopes.map((scopePanel) => [
      connection.scopes.length > 1
        ? m("h3", { class: "type-heading text-primary mt-6 mb-1" }, scopePanel.heading)
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
    // takes the account away from every machine. Only a connected account has a
    // stored sign-in to forget -- a leftover-grants row is Revoke all's job.
    connection.is_connected
      ? [
          m(SectionHeader, { divider: true }, "Disconnect"),
          m("p", { class: "type-body text-secondary mb-3" }, [
            "Disconnect from ",
            m("span", { class: "font-semibold" }, `${connection.display_name} · ${connection.account_label}`),
            `. Disconnecting is not limited to ${machineLabel} — every machine will lose this access. `,
            "Use Revoke all above to disconnect just this machine.",
          ]),
          m(
            Button,
            {
              variant: "danger",
              size: "md",
              "data-perm-disconnect": connection.service_name,
              disabled: isDisconnectBusy,
              onclick: () => {
                model.clearErrorMessage();
                disarmRevoke(local);
                local.disconnectSectionId = connectionSectionId(connection);
              },
            },
            isDisconnectBusy ? "Disconnecting..." : "Disconnect",
          ),
          renderDisconnectDialog(model, local, connection, machineLabel, selectSection),
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
  selectSection: (section: string) => void,
): m.Children {
  const isOpen = local.disconnectSectionId === connectionSectionId(connection);
  const isBusy = model.isRowBusy(disconnectRowKey(connection.service_name, connection.account));
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
            `Disconnect ${connection.display_name} · ${connection.account_label} from Minds?`,
          ),
          m("p", { class: "type-body text-primary mb-4" }, [
            "This will disconnect ",
            m("strong", `${connection.display_name} · ${connection.account_label}`),
            ` from all of your machines in Minds, not just ${machineLabel}. Agents won't be able `,
            "to use it anywhere until you connect it again from scratch.",
          ]),
          model.errorMessage ? m(Notice, { variant: "error", role: "alert" }, model.errorMessage) : null,
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
                disabled: isBusy,
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
      disabled: isBusy,
      onclick: () => {
        model.clearErrorMessage();
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

/** A failed connection attempt, raised over the pane.
 *
 * What a service says when it refuses a sign-in is usually a errand somewhere
 * else -- "ask an administrator of your Zoom account to grant you the developer
 * privilege, then try again" -- and runs to several lines. It gets a popup so
 * it is read rather than skimmed past: the inline banner sits at the top of a
 * pane the user has often scrolled away from, and the click that triggered this
 * was somewhere further down. Dismissal is the user's alone. */
function renderConnectFailurePopup(model: PermissionsModel): m.Children {
  return m(
    Modal,
    {
      isOpen: model.alertMessage !== "",
      onClose: () => model.dismissAlert(),
      id: "ws-perm-alert",
    },
    [
      m("h2", { class: "type-heading-lg text-primary" }, "Couldn't connect"),
      m(
        "p",
        { class: "mt-2 type-body text-secondary whitespace-pre-line", role: "alert" },
        model.alertMessage,
      ),
      m(
        "div",
        { class: "mt-5 flex justify-end" },
        m(Button, { variant: "secondary", onclick: () => model.dismissAlert() }, "Close"),
      ),
    ],
  );
}

function renderCatalogAction(
  model: PermissionsModel,
  service: UiAvailableConnection,
  actionLabel: string,
  selectSection: (section: string) => void,
): m.Children {
  const isBusy = model.isRowBusy(connectServiceRowKey(service.service_name));
  const action = connectActionFor(service.sign_in);
  const attrs = {
    variant: "secondary" as const,
    size: "md" as const,
    extra: "shrink-0",
    "data-perm-connect": service.service_name,
    disabled: isBusy || action === "unconnectable",
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
          model.clearErrorMessage();
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
  return m(
    Button,
    {
      ...attrs,
      onclick: () => {
        void model.connectService(service.service_name).then((section) => {
          if (section !== null) selectSection(section);
        });
      },
    },
    isBusy ? "Waiting for sign-in..." : actionLabel,
  );
}

/** The credential form for a service with no browser sign-in: one input per
 * value its own command asks for, plus a name for the account when the service
 * already has one. The command itself is never shown -- Minds runs it, so it is
 * not something the user has to know about. */
function renderCredentialForm(
  model: PermissionsModel,
  service: UiAvailableConnection,
  actionLabel: string,
  selectSection: (section: string) => void,
): m.Children {
  const signIn = service.sign_in;
  const isBusy = model.isRowBusy(connectServiceRowKey(service.service_name));
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
        `${service.display_name} can't be signed in to through a browser, so Minds needs its ` +
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
            m("span", { class: "type-helper text-tertiary" }, "How this account is labelled in Minds."),
          ])
        : null,
      model.credentialErrorMessage
        ? m(Notice, { variant: "error", role: "alert" }, model.credentialErrorMessage)
        : null,
      m("div", { class: "flex items-center gap-2" }, [
        m(
          Button,
          {
            variant: "primary",
            size: "md",
            "data-perm-credential-submit": service.service_name,
            disabled: isBusy || !isComplete,
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
  const toggles = model.data?.file_sharing_toggles ?? [];
  return m("section", { "data-perm-panel": LOCAL_FILES_SECTION }, [
    m("h2", { class: "type-heading text-primary mb-1" }, "Local files"),
    m(
      "p",
      { class: "type-body text-secondary mb-4" },
      "Files and folders on this computer that agents in this machine can read or write. To share " +
        "a new location, ask an agent to access it; revoked locations can be turned back on here.",
    ),
    toggles.length === 0
      ? m(Notice, { variant: "info" }, "No files are being shared with agents in this machine yet.")
      : m(
          "div",
          { class: "flex flex-col pr-4" },
          toggles.map((toggle) =>
            m("div", { class: TOGGLE_ROW_CLASS }, [
              m(
                "div",
                { class: "min-w-0" },
                m("p", { class: "type-body text-primary flex items-center gap-2 min-w-0" }, [
                  m("code", { class: "code-pill truncate min-w-0", title: toggle.label }, toggle.label),
                  m("span", { class: "type-helper text-secondary shrink-0" }, toggle.detail),
                ]),
              ),
              renderSelfSwitch(model, toggle, `Share ${toggle.label} (${toggle.detail})`),
            ]),
          ),
        ),
  ]);
}

function renderOtherMachinesPanel(model: PermissionsModel): m.Children {
  const toggles = model.data?.workspace_toggles ?? [];
  return m("section", { "data-perm-panel": OTHER_MACHINES_SECTION }, [
    m("h2", { class: "type-heading text-primary mb-1" }, "Other machines"),
    m(
      "p",
      { class: "type-body text-secondary mb-4" },
      "What agents in this machine are allowed to do to your other machines (listing, creating, " +
        "destroying, SSH, and more). To grant more, ask an agent to perform the operation.",
    ),
    toggles.length === 0
      ? m(Notice, { variant: "info" }, "Agents in this machine can't manage your other machines yet.")
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

function renderSelfSwitch(
  model: PermissionsModel,
  toggle: UiSelfPermissionToggle,
  ariaLabel: string,
): m.Children {
  return renderSwitch({
    isGranted: toggle.is_granted,
    isBusy: model.isRowBusy(selfToggleRowKey(toggle.permission)),
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
  isBlocked: boolean;
  blockedTitle: string;
  label: string;
  permission: string;
  onFlip: (enabled: boolean) => void;
}

function renderSwitch(options: SwitchOptions): m.Children {
  const { isGranted, isBusy, isBlocked, blockedTitle, label, permission, onFlip } = options;
  return m("button", {
    type: "button",
    role: "switch",
    "aria-checked": isGranted ? "true" : "false",
    "aria-label": label,
    "data-perm-permission": permission,
    // Busy stays clickable-looking but is ignored by the model: a second click
    // while the write runs should read as "wait", not "broken".
    class: isBusy ? "perm-switch shrink-0 is-busy" : "perm-switch shrink-0",
    disabled: isBlocked,
    ...(isBlocked ? { title: blockedTitle } : {}),
    onclick: () => onFlip(!isGranted),
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
