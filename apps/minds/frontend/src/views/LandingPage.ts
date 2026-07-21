// The landing page: workspace rows with health / liveness / backup badges
// and start-stop-restart controls, remote tiles, the sync-unlock banner, the
// providers panel, and the bottom-left app launchers. Port of the deleted
// Landing.jinja markup + ~767 lines of inline JS; the old code's edge-case
// comments moved here with the behavior they constrain. Rows render from the
// store's SSE-fed state, so row-set changes rerender in place -- the old
// refetch-on-drift full-page path is gone.
import m from "mithril";

import type { ChromeWorkspaceEntry, LandingBootExtras } from "../chrome_state";
import type { Host } from "../host";
import { ICONS_16 } from "../icons";
import {
  exportLatestBackup,
  getBackupBadge,
  getExportState,
  loadBackupStatus,
  relativeAgo,
  removeRemoteRecord,
  startMind,
  stopMind,
  submitSyncUnlock,
} from "../landing_service";
import { getHost } from "../host";
import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";
import {
  connect,
  getDestroyingStatus,
  getEffectiveLiveness,
  getRemoteWorkspaceStates,
  getSystemInterfaceStatus,
  getWorkspaces,
  seed,
} from "../store";
import { ProvidersPanel } from "./ProvidersPanel";

export interface LandingPageAttrs {
  host: Host;
  extras: LandingBootExtras;
}

// Card.jinja's class output for the landing rows (layout="row" interactive +
// the accent spine), preserved verbatim so the visual shell is unchanged.
const ROW_CARD_CLASSES =
  "minds-card flex items-center gap-1.5 p-4 cursor-pointer hover:border-strong hover:shadow-raised accent-spine relative overflow-hidden";
const DESTROYING_CARD_CLASSES =
  "minds-card flex items-center gap-1.5 p-4 cursor-pointer hover:border-strong hover:shadow-raised no-underline text-inherit accent-spine relative overflow-hidden";
const REMOTE_CARD_CLASSES = "minds-card flex items-center gap-1.5 p-4 accent-spine relative overflow-hidden opacity-60";

const CHIP_CLASSES = "inline-flex items-center px-2 py-0.5 rounded-md type-label";
// Button.jinja's ghost icon recipe (BTN_BASE + icon size + ghost variant).
const GHOST_ICON_BUTTON_CLASSES =
  "inline-flex items-center justify-center gap-1.5 leading-tight transition-transform duration-100 ease-in-out " +
  "disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer no-underline whitespace-nowrap active:scale-[0.98] " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent p-1.5 rounded-md type-label " +
  "bg-transparent text-primary border border-transparent hover:bg-fill-hover";

function icon16(name: string, extraClass?: string): m.Children {
  return m(
    "svg",
    {
      class: extraClass !== undefined ? `w-4 h-4 ${extraClass}` : "w-4 h-4",
      viewBox: "0 0 16 16",
      fill: "currentColor",
      "aria-hidden": "true",
    },
    m.trust(ICONS_16[name]),
  );
}

function gotoHref(mngrForwardOrigin: string, agentId: string): string {
  return `${mngrForwardOrigin}/goto/${agentId}/`;
}

function recoveryUrl(agentId: string, returnTo: string, isRestartIntent: boolean): string {
  const intent = isRestartIntent ? "&intent=restart" : "";
  return `/agents/${encodeURIComponent(agentId)}/recovery?return_to=${encodeURIComponent(returnTo)}${intent}`;
}

// The health badge, shown only while it carries text. A stopped container has
// no server to be unhealthy: the badge is suppressed entirely so it doesn't
// compete with the "Stopped" state badge.
function healthBadge(agentId: string, effectiveLiveness: string | null): m.Children {
  const status = effectiveLiveness === "STOPPED" ? null : getSystemInterfaceStatus(agentId);
  let text: string | null = null;
  if (status === "stuck") text = "Server not responding";
  else if (status === "restarting") text = "Restarting...";
  else if (status === "restart_failed") text = "Restart failed";
  if (text === null) return null;
  return m("span", { class: `landing-health-badge ${CHIP_CLASSES} bg-warning/15 text-warning` }, text);
}

// The mind container state badge (STOPPED / UNKNOWN / optimistic transients).
// RUNNING renders nothing.
function mindStateBadge(effectiveLiveness: string | null): m.Children {
  let text: string | null = null;
  let tone = "bg-fill-subtle text-primary";
  if (effectiveLiveness === "STOPPED") text = "Stopped";
  else if (effectiveLiveness === "UNKNOWN") {
    text = "Status unknown";
    tone = "bg-fill-subtle text-secondary";
  } else if (effectiveLiveness === "STOPPING") {
    text = "Stopping…";
    tone = "bg-warning/15 text-warning";
  } else if (effectiveLiveness === "STARTING") {
    text = "Starting…";
    tone = "bg-warning/15 text-warning";
  }
  if (text === null) return null;
  return m("span", { class: `landing-mind-state-badge ${CHIP_CLASSES} ${tone}` }, text);
}

function backupBadge(agentId: string): m.Children {
  const badge = getBackupBadge(agentId);
  if (badge === null) return null;
  let text: string | null = null;
  let tone = "bg-fill-subtle text-secondary";
  if (badge.state === "CHECKING") text = "Checking backups…";
  else if (badge.state === "BACKING_UP") {
    text = "Backing up…";
    tone = "bg-accent/15 text-accent";
  } else if (badge.state === "BACKED_UP") {
    text = `Backed up ${relativeAgo(badge.lastSuccessAt)}`;
    tone = "bg-success/15 text-success";
  } else if (badge.state === "CREATED_RECENTLY") {
    text = `Created ${relativeAgo(badge.createdAt)}`;
  } else {
    text = "No backups";
  }
  return m("span", { class: `landing-backup-badge ${CHIP_CLASSES} ${tone}` }, text);
}

// The "download" link next to a BACKED_UP badge (in-flight + failed states
// mirror the old inline exporter).
function backupDownloadLink(agentId: string): m.Children {
  const badge = getBackupBadge(agentId);
  if (badge === null || badge.state !== "BACKED_UP") return null;
  const exportState = getExportState(agentId);
  if (exportState === "exporting") {
    return m("span", { class: "landing-backup-download type-helper text-accent" }, [
      m("span", { class: "spinner spinner-accent inline-block w-3 h-3 align-middle" }),
      " exporting…",
    ]);
  }
  if (exportState === "failed") {
    return m("span", { class: "landing-backup-download type-helper text-important" }, "export failed");
  }
  return m(
    "a",
    {
      href: "#",
      class: "landing-backup-download cursor-pointer type-helper text-accent underline",
      onclick: (event: MouseEvent) => {
        event.preventDefault();
        event.stopPropagation();
        exportLatestBackup(agentId);
      },
    },
    "download",
  );
}

export function LandingPage(): m.Component<LandingPageAttrs> {
  // Remote rows removed optimistically (the record removal propagates via a
  // later workspaces push; hide immediately like the old DOM removal did).
  const hiddenRemoteAgentIds = new Set<string>();
  const removingHostIds = new Set<string>();
  // Sync-unlock banner state.
  let unlockError: string | null = null;
  let isUnlockSubmitting = false;
  let unlockPassword = "";
  let isRemoved = false;
  let discoveringTimer: number | null = null;

  function localRow(host: Host, extras: LandingBootExtras, workspace: ChromeWorkspaceEntry): m.Children {
    const agentId = workspace.id;
    const returnTo = gotoHref(extras.mngr_forward_origin, agentId);
    const effectiveLiveness = getEffectiveLiveness(workspace);
    const healthStatus = getSystemInterfaceStatus(agentId);
    const supportsShutdown = workspace.supports_shutdown === "true";

    const onRowClick = (): void => {
      if (healthStatus === "stuck" || healthStatus === "restarting" || healthStatus === "restart_failed") {
        host.navigate(recoveryUrl(agentId, returnTo, false));
      } else if (effectiveLiveness === "STOPPED") {
        // The mind's container is stopped, so loading the workspace directly
        // would strand the user on the loader for the full HEALTHY->STUCK
        // probe-failure threshold before any restart is dispatched. We
        // already know it's down: route straight to the recovery page, which
        // confirms host_offline via its probe and cold-boots the host
        // immediately. ``intent=restart`` is required -- the health tracker
        // has never probed this stopped mind so it still reports HEALTHY,
        // and without the intent the recovery handler would 302 right back
        // to the loader instead of dispatching the restart.
        host.navigate(recoveryUrl(agentId, returnTo, true));
      } else {
        host.navigate(returnTo);
      }
    };

    return m(
      "div",
      {
        class: ROW_CARD_CLASSES,
        style: { "--workspace-accent": workspace.accent },
        "data-agent-id": agentId,
        "data-default-href": returnTo,
        onclick: onRowClick,
      },
      [
        m("span", { class: "flex-1 font-semibold text-primary pl-1" }, workspace.name),
        workspace.provider !== undefined
          ? m("span", { class: `landing-provider-badge ${CHIP_CLASSES} bg-fill-subtle text-secondary` }, workspace.provider)
          : null,
        backupBadge(agentId),
        backupDownloadLink(agentId),
        mindStateBadge(effectiveLiveness),
        healthBadge(agentId, effectiveLiveness),
        supportsShutdown && effectiveLiveness === "STOPPED"
          ? m(
              "button",
              {
                type: "button",
                class: GHOST_ICON_BUTTON_CLASSES,
                "data-tooltip": "Start workspace",
                "aria-label": "Start workspace",
                onclick: (event: MouseEvent) => {
                  event.stopPropagation();
                  startMind(agentId);
                },
              },
              icon16("play"),
            )
          : null,
        supportsShutdown && effectiveLiveness === "RUNNING"
          ? m(
              "button",
              {
                type: "button",
                class: GHOST_ICON_BUTTON_CLASSES,
                "data-tooltip": "Stop workspace",
                "aria-label": "Stop workspace",
                onclick: (event: MouseEvent) => {
                  event.stopPropagation();
                  // Electron shows a native confirm and issues the stop itself
                  // (the result arrives over the chrome events); the in-page
                  // optimistic flow runs only when the host says so (browser
                  // confirm accepted).
                  void host.confirmStopMind(agentId, workspace.name).then((shouldRunInPageFlow) => {
                    if (shouldRunInPageFlow) stopMind(agentId);
                  });
                },
              },
              icon16("pause"),
            )
          : null,
        !supportsShutdown
          ? m(
              "button",
              {
                type: "button",
                class: GHOST_ICON_BUTTON_CLASSES,
                "data-tooltip": "Restart workspace",
                "aria-label": "Restart workspace",
                onclick: (event: MouseEvent) => {
                  event.stopPropagation();
                  host.navigate(recoveryUrl(agentId, returnTo, true));
                },
              },
              icon16("restart"),
            )
          : null,
        m(
          "button",
          {
            type: "button",
            class: GHOST_ICON_BUTTON_CLASSES,
            "data-tooltip": "Open in new window",
            "aria-label": "Open workspace in new window",
            onclick: (event: MouseEvent) => {
              event.stopPropagation();
              host.openWorkspaceInNewWindow(agentId);
            },
          },
          icon16("arrow-up-right"),
        ),
        m(
          "button",
          {
            type: "button",
            class: GHOST_ICON_BUTTON_CLASSES,
            "data-tooltip": "Settings",
            "aria-label": "Workspace settings",
            onclick: (event: MouseEvent) => {
              event.stopPropagation();
              host.navigate(`/workspace/${agentId}/settings`);
            },
          },
          icon16("settings"),
        ),
      ],
    );
  }

  function destroyingRow(workspace: ChromeWorkspaceEntry, destroyStatus: string): m.Children {
    return m(
      "a",
      {
        class: DESTROYING_CARD_CLASSES,
        style: { "--workspace-accent": workspace.accent },
        href: `/destroying/${workspace.id}`,
      },
      [
        m("span", { class: "flex-1 font-semibold text-secondary pl-1" }, workspace.name),
        destroyStatus === "running"
          ? m("span", { class: `${CHIP_CLASSES} bg-fill-subtle text-secondary gap-2` }, [
              m("span", { class: "spinner inline-block align-middle w-3.5 h-3.5 border" }),
              "Destroying...",
            ])
          : m("span", { class: `${CHIP_CLASSES} bg-important text-white` }, "Destroy failed"),
      ],
    );
  }

  function remoteRow(workspace: ChromeWorkspaceEntry, remoteState: string | null): m.Children {
    const agentId = workspace.id;
    return m(
      "div",
      {
        class: REMOTE_CARD_CLASSES,
        style: { "--workspace-accent": workspace.accent },
        "data-agent-id": agentId,
      },
      [
        m("span", { class: "flex-1 font-semibold text-secondary pl-1" }, workspace.name),
        m("span", { class: `${CHIP_CLASSES} bg-fill-subtle text-secondary` }, `on ${workspace.location ?? "another device"}`),
        // Derived access state for cloud rows: connecting (synced key
        // materialized, discovery hasn't resolved it yet), unreachable (a
        // healthy snapshot lacks the host), or a materialization error
        // (detail in the tooltip). Plain rows render no chip.
        remoteState === "connecting"
          ? m("span", { class: `${CHIP_CLASSES} bg-fill-subtle text-secondary` }, "connecting…")
          : null,
        remoteState === "unreachable"
          ? m("span", { class: `${CHIP_CLASSES} bg-fill-subtle text-important` }, "unreachable")
          : null,
        remoteState === "error"
          ? m(
              "span",
              {
                class: `${CHIP_CLASSES} bg-fill-subtle text-important`,
                "data-tooltip": workspace.state_detail ?? "Could not apply the synced access material.",
              },
              "sync error",
            )
          : null,
        backupBadge(agentId),
        backupDownloadLink(agentId),
        workspace.host_id !== undefined
          ? m(
              "button",
              {
                type: "button",
                class: GHOST_ICON_BUTTON_CLASSES,
                disabled: removingHostIds.has(workspace.host_id),
                "data-remove-host-id": workspace.host_id,
                "data-tooltip": "Remove from this list",
                "aria-label": "Remove from this list",
                onclick: (event: MouseEvent) => {
                  event.preventDefault();
                  event.stopPropagation();
                  const hostId = workspace.host_id;
                  if (hostId === undefined) return;
                  removingHostIds.add(hostId);
                  void removeRemoteRecord(hostId).then((isRemovedOk) => {
                    removingHostIds.delete(hostId);
                    if (isRemovedOk) hiddenRemoteAgentIds.add(agentId);
                    m.redraw();
                  });
                },
              },
              icon16("close"),
            )
          : null,
      ],
    );
  }

  function unlockBanner(extras: LandingBootExtras): m.Children {
    if (extras.locked_account_emails.length === 0) return null;
    // New-device unlock banner: a bundle exists for these accounts but this
    // device has no key yet. Unlocking installs the account's sync key so
    // synced secrets (backup credentials, host keys) become usable here.
    return m("div", { class: "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-info-surface)] text-info mb-4", id: "sync-unlock-banner" }, [
      m("div", { class: "flex flex-col gap-2" }, [
        m(
          "span",
          { class: "type-body text-primary" },
          `Enter your master password to unlock synced workspaces for ${extras.locked_account_emails.join(", ")}.`,
        ),
        m("div", { class: "flex items-center gap-2" }, [
          m("input", {
            type: "password",
            id: "sync-unlock-password",
            autocomplete: "current-password",
            placeholder: "master password",
            class: "h-[34px] px-3 rounded-full type-body bg-surface-secondary text-primary",
            value: unlockPassword,
            oninput: (event: InputEvent) => {
              unlockPassword = (event.target as HTMLInputElement).value;
            },
          }),
          m(
            "button",
            {
              type: "button",
              id: "sync-unlock-btn",
              class:
                "inline-flex items-center justify-center gap-1.5 leading-tight transition-transform duration-100 " +
                "ease-in-out disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer no-underline " +
                "whitespace-nowrap active:scale-[0.98] px-4 py-2 rounded-md type-label bg-transparent text-primary " +
                "border border-default hover:bg-fill-hover",
              disabled: isUnlockSubmitting,
              onclick: () => {
                isUnlockSubmitting = true;
                unlockError = null;
                void submitSyncUnlock(unlockPassword).then((error) => {
                  isUnlockSubmitting = false;
                  if (error === null) {
                    // Guarded: the unlock can resolve after this landing page
                    // was swapped out, and the refresh event targets the
                    // CURRENT page.
                    if (!isRemoved) window.dispatchEvent(new Event("minds:refresh-local-page"));
                    return;
                  }
                  unlockError = error;
                  m.redraw();
                });
              },
            },
            "Unlock",
          ),
        ]),
        unlockError !== null
          ? m("p", { id: "sync-unlock-error", class: "type-helper text-important", role: "alert" }, unlockError)
          : null,
      ]),
    ]);
  }

  return {
    oncreate(vnode) {
      const allRowIds = getWorkspaces().map((workspace) => workspace.id);
      loadBackupStatus(allRowIds);
      if (vnode.attrs.extras.is_discovering) {
        // In-place refresh (no titlebar rebuild) while discovery finishes.
        discoveringTimer = window.setTimeout(() => {
          window.dispatchEvent(new Event("minds:refresh-local-page"));
        }, 2000);
      }
    },
    onremove() {
      isRemoved = true;
      if (discoveringTimer !== null) window.clearTimeout(discoveringTimer);
    },
    view(vnode) {
      const { host, extras } = vnode.attrs;
      const workspaces = getWorkspaces();
      const localRows = workspaces.filter((workspace) => workspace.is_remote !== "true");
      const remoteRows = workspaces.filter(
        (workspace) => workspace.is_remote === "true" && !hiddenRemoteAgentIds.has(workspace.id),
      );
      const hasRows = localRows.length > 0 || remoteRows.length > 0;

      return [
        m("div", { class: "max-w-[720px] mx-auto px-6 py-12" }, [
          hasRows
            ? [
                m("div", { class: "flex items-center justify-between mb-4" }, [
                  m("h1", { class: "type-heading text-primary" }, "Workspaces"),
                  m(
                    "a",
                    {
                      href: "/create",
                      class:
                        "inline-flex items-center justify-center gap-1.5 leading-tight transition-transform " +
                        "duration-100 ease-in-out cursor-pointer no-underline whitespace-nowrap active:scale-[0.98] " +
                        "px-4 py-2 rounded-md type-label bg-surface-inverse text-inverse-primary border " +
                        "border-transparent hover:opacity-80",
                      onclick: (event: MouseEvent) => {
                        event.preventDefault();
                        host.navigate("/create");
                      },
                    },
                    "Create",
                  ),
                ]),
                unlockBanner(extras),
                m("div", { class: "flex flex-col gap-1.5" }, [
                  localRows.map((workspace) => {
                    const destroyStatus = getDestroyingStatus(workspace.id);
                    return destroyStatus !== null
                      ? destroyingRow(workspace, destroyStatus)
                      : localRow(host, extras, workspace);
                  }),
                  // Workspaces known only from synced records (hosted on
                  // another device). Greyed and non-clickable -- their hosts
                  // aren't reachable from here -- but backup status and
                  // download still work via the synced credentials, and the X
                  // removes the record for good.
                  remoteRows.map((workspace) => remoteRow(workspace, remoteStateFor(workspace.id))),
                ]),
              ]
            : extras.is_discovering
              ? m("div", { class: "flex items-center justify-center min-h-[80vh]" }, [
                  m("p", { class: "text-tertiary text-center" }, "Discovering agents..."),
                ])
              : m("div", { class: "text-center py-12" }, [
                  m("p", { class: "text-tertiary mb-6" }, "No workspaces yet"),
                  m(
                    "a",
                    {
                      href: "/create",
                      class:
                        "inline-flex items-center justify-center gap-1.5 leading-tight cursor-pointer no-underline " +
                        "whitespace-nowrap px-4 py-2 rounded-md type-label bg-surface-inverse text-inverse-primary " +
                        "border border-transparent hover:opacity-80",
                      onclick: (event: MouseEvent) => {
                        event.preventDefault();
                        host.navigate("/create");
                      },
                    },
                    "Create",
                  ),
                ]),
          m(ProvidersPanel),
          // Spacer so the last workspace rows can scroll clear of the fixed
          // bottom-left launchers below.
          m("div", { class: "h-20", "aria-hidden": "true" }),
        ]),
        // Bottom-left app-level launchers: Minds Settings + the account entry.
        m("div", { class: "fixed bottom-3 left-3 flex flex-col items-start gap-0.5" }, [
          m(
            "button",
            {
              type: "button",
              id: "landing-minds-settings",
              class:
                "flex items-center gap-2 h-8 px-2 rounded-md cursor-pointer type-body text-secondary " +
                "hover:text-primary hover:bg-fill-hover bg-transparent border-0 text-left",
              onclick: () => host.openModal({ kind: "minds-settings" }),
            },
            [icon16("settings", "shrink-0"), m("span", "Minds Settings")],
          ),
          m(
            "button",
            {
              type: "button",
              id: "landing-account",
              "data-signed-in": extras.account_email !== "" ? "true" : "false",
              class:
                "flex items-center gap-2 h-8 px-2 rounded-md cursor-pointer type-body text-secondary " +
                "hover:text-primary hover:bg-fill-hover bg-transparent border-0 text-left",
              onclick: () => {
                // ``returnTo: '/'`` lands a successful sign-in back on this
                // home screen; ``mode: 'signin'`` leads with the sign-in tab
                // to match the launcher's "Log in" label.
                if (extras.account_email !== "") host.openModal({ kind: "accounts" });
                else host.openModal({ kind: "signin", returnTo: "/", mode: "signin" });
              },
            },
            [
              icon16("user", "shrink-0"),
              m(
                "span",
                extras.account_email !== ""
                  ? extras.extra_account_count > 0
                    ? `${extras.account_email} (+${extras.extra_account_count})`
                    : extras.account_email
                  : "Log in",
              ),
            ],
          ),
        ]),
      ];
    },
  };
}

// The remote tile's derived access state, from the store's drift map.
function remoteStateFor(agentId: string): string | null {
  const state = getRemoteWorkspaceStates()[agentId];
  return state !== undefined && state !== "" ? state : null;
}

interface LandingBootIslandShape {
  chrome?: unknown;
  landing?: LandingBootExtras;
}

// Mount the landing page: seed the store from the island's chrome snapshot,
// connect the document's chrome-event stream, and mount synchronously so the
// first paint is complete on arrival (rows visible, no post-load pop-in).
export function mountLanding(target: Element | null): void {
  const el = requireElement(target, "landing root container");
  const island = readBootState() as LandingBootIslandShape;
  const extras = island.landing;
  if (extras === undefined || island.chrome === undefined) {
    throw new MindsUIError("landing boot island is missing the chrome or landing slice");
  }
  seed(island.chrome as Parameters<typeof seed>[0]);
  const host = getHost();
  connect(host);
  mountWithTeardown(el, { view: () => m(LandingPage, { host, extras }) });
}
