// The workspace menu: hung from the bar's workspace button, listing what the
// window could show -- the account's workspaces, the workspaces shared with
// it, and the apps shared with it, with what is on screen marked among them --
// under a search field. Rendered by the Shell while open; the button itself is
// the bar's (Titlebar.ts, trail.ts).

import m from "mithril";
import { Icon16 } from "../components/Icon";
import { MonogramTile } from "../components/MonogramTile";
import type { UiWorkspaceEntry } from "../../channel/messages";
import { electronBridge } from "../../electron-bridge";
import { MIND_LIVENESS_LABELS } from "../../models/create";
import { errorDialogs } from "../../models/errorDialog";
import type { SharedApp, SharedEntry } from "../../models/sharedWithMe";
import {
  isSharedEntry,
  listedSharedRows,
  ownedWorkspaceEntries,
  ownerLabel,
  sharedActionErrorMessage,
  sharedRouteId,
} from "../../models/sharedWithMe";
import { RaisedTitlebarIcons, openTitlebarPopup, readAnchorRect, titlebarAnchors } from "./RaisedTitlebarIcons";
import {
  MENU_CLASS,
  MENU_ROW_CLASS,
  MenuHighlight,
  menuActionRow,
  menuCurrentCheck,
  menuDivider,
  menuSectionHeading,
  menuStyle,
} from "./menu-kit";
import type { ShellState } from "./shell-state";
import { WORKSPACE_BUTTON_ID, displayedSharedEntry, workspaceSwatch } from "./trail";

/** What the bar shows in the workspace button's place on a hub page other than Home; the menu hangs from it there. */
const PAGE_CRUMB_ID = "page-crumb";

const CREATE_ATTEMPT_BADGE_LABELS: Record<string, string> = {
  creating: "Creating…",
  interrupted: "Interrupted",
  failed: "Create failed",
};

// Lucide stroke icons for non-running liveness states (sidebar_workspace_row.js).
// Both transitional states share the loader arc. Each entry is one <path>:
// Lucide's segments open with a relative "m", so joined into a single d they
// would be placed relative to the previous segment's end instead of the origin.
const TRANSITION_ICON_PATHS = ["M21 12a9 9 0 1 1-6.219-8.56"];
const STATUS_ICON_PATHS: Record<string, readonly string[]> = {
  STOPPED: [
    "m15 18-.722-3.25",
    "M2 8a10.645 10.645 0 0 0 20 0",
    "m20 15-1.726-2.05",
    "m4 15 1.726-2.05",
    "m9 18 .722-3.25",
  ],
  STOPPING: TRANSITION_ICON_PATHS,
  STARTING: TRANSITION_ICON_PATHS,
  UNKNOWN: [
    "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3",
    "M12 9v4",
    "M12 17h.01",
  ],
};

export function statusIcon(liveness: string): m.Children {
  const paths = STATUS_ICON_PATHS[liveness];
  const title = MIND_LIVENESS_LABELS[liveness];
  if (paths === undefined || title === undefined) return null;
  return m(
    "span",
    {
      class: "sidebar-status-icon shrink-0 inline-flex w-4 justify-center text-secondary",
      title,
    },
    m(
      "svg",
      {
        class: "w-3 h-3",
        viewBox: "0 0 24 24",
        fill: "none",
        stroke: "currentColor",
        "stroke-width": "2",
        "stroke-linecap": "round",
        "stroke-linejoin": "round",
      },
      paths.map((d) => m("path", { d })),
    ),
  );
}

/** Whether `text` contains what is typed in the search (anything does, while nothing is). */
function matchesQuery(query: string, ...texts: (string | null | undefined)[]): boolean {
  const needle = query.trim().toLowerCase();
  return needle === "" || texts.some((text) => (text ?? "").toLowerCase().includes(needle));
}

/** Who a shared row is from, as its row says at the right; "" for a pasted link, which names nobody. */
function ownerOf(entry: SharedEntry): string {
  return entry.share.is_manual ? "" : ownerLabel(entry.share);
}

/** What the menu lists, already narrowed by the search, and which of it is on screen. At most one of the three
 * `current` fields is set, and each stays set while the search hides its row. */
export interface WorkspaceSwitcherRows {
  owned: UiWorkspaceEntry[];
  sharedWorkspaces: SharedEntry[];
  sharedApps: SharedApp[];
  /** The id of the account's own workspace on screen. */
  currentOwnedId: string | null;
  currentSharedWorkspace: SharedEntry | null;
  currentSharedApp: SharedApp | null;
}

export function workspaceSwitcherRows(shell: ShellState, query: string): WorkspaceSwitcherRows {
  const workspaces = shell.stores.workspaces;
  const displayed = shell.displayedWorkspaceAnyId;
  const displayedAgentId = displayed === null ? null : workspaces.toAgentScopedId(displayed);
  const displayedShared = displayedSharedEntry(shell);
  const displayedSharedApp = displayedShared === null ? null : shell.sharedFrameTargetApp(displayedShared);
  const owned = ownedWorkspaceEntries(workspaces.workspaces);
  const listed = listedSharedRows(workspaces.workspaces);
  return {
    owned: owned.filter((entry) => matchesQuery(query, entry.name || entry.id)),
    sharedWorkspaces: listed.workspaceShares.filter((entry) =>
      matchesQuery(query, entry.name || entry.id, ownerOf(entry)),
    ),
    sharedApps: listed.sharedApps.filter((app) =>
      matchesQuery(query, app.name, isSharedEntry(app.entry) ? ownerOf(app.entry) : ""),
    ),
    currentOwnedId: owned.find((entry) => entry.id === displayedAgentId)?.id ?? null,
    // A shared workspace is "on screen" only while its shell is: one of its apps open alone leaves the workspace
    // itself a place to go.
    currentSharedWorkspace:
      listed.workspaceShares.find((entry) => entry === displayedShared && displayedSharedApp === null) ?? null,
    currentSharedApp:
      listed.sharedApps.find((app) => app.entry === displayedShared && app.target.app === displayedSharedApp) ?? null,
  };
}

export interface WorkspaceSwitcherMenuAttrs {
  shell: ShellState;
}

export function WorkspaceSwitcherMenu(): m.Component<WorkspaceSwitcherMenuAttrs> {
  const anchors = titlebarAnchors();
  const highlight = new MenuHighlight();
  let query = "";
  let shortcutLabel: string | null = null;
  let isShortcutLabelRead = false;

  function openShared(shell: ShellState, shareKey: string, app: string | null, name: string): void {
    shell.closeWorkspaceSwitcher();
    shell.openSharedWorkspace(shareKey, app).catch((error: unknown) => {
      errorDialogs.show({
        title: `Couldn't open ${name}`,
        error: { message: sharedActionErrorMessage(error), details: "" },
      });
      m.redraw();
    });
  }

  function ownedRow(shell: ShellState, workspace: UiWorkspaceEntry, isCurrent: boolean): m.Children {
    const createAttemptState = workspace.create_attempt_state || null;
    const isRemote = workspace.is_remote;
    const navKey = `ws:${workspace.id}`;
    return m("div", { class: "group relative flex items-center" }, [
      m(
        "button",
        {
          type: "button",
          role: "menuitem",
          "data-agent-id": workspace.id,
          "aria-current": isCurrent ? "true" : undefined,
          disabled: isRemote,
          ...highlight.rowAttrs(navKey),
          class: highlight.rowClass(
            "sidebar-item " + MENU_ROW_CLASS + (isRemote ? " is-remote opacity-60" : ""),
            navKey,
            { isCurrent },
          ),
          onclick: () => {
            shell.closeWorkspaceSwitcher();
            if (isCurrent) return;
            if (createAttemptState !== null) m.route.set(`/creating/${workspace.id}`);
            else shell.enterWorkspace(workspace.id);
          },
        },
        [
          workspaceSwatch(workspace.accent || null),
          m("span", { class: "flex-1 truncate" }, workspace.name || workspace.id),
          createAttemptState !== null
            ? m(
                "span",
                {
                  class:
                    "shrink-0 type-helper " + (createAttemptState === "failed" ? "text-important" : "text-tertiary"),
                },
                CREATE_ATTEMPT_BADGE_LABELS[createAttemptState] ?? createAttemptState,
              )
            : statusIcon(workspace.liveness ?? ""),
          isCurrent ? menuCurrentCheck() : null,
        ],
      ),
      createAttemptState === null && !isRemote && !isCurrent && electronBridge.isDesktop
        ? m(
            "button",
            {
              type: "button",
              class:
                "sidebar-row-icon absolute right-1 hidden group-hover:flex items-center justify-center w-6 h-6 " +
                "bg-surface-primary border-none cursor-pointer text-secondary rounded-md hover:text-primary hover:bg-fill-hover",
              title: "Open in new window",
              "aria-label": `Open ${workspace.name || workspace.id} in a new window`,
              tabindex: -1,
              onclick: (event: MouseEvent) => {
                event.stopPropagation();
                shell.closeWorkspaceSwitcher();
                electronBridge.openWorkspaceInNewWindow(workspace.id);
              },
            },
            m(Icon16, { name: "arrow-up-right" }),
          )
        : null,
    ]);
  }

  function sharedRow(options: {
    navKey: string;
    mark: m.Children;
    name: string;
    owner: string;
    isCurrent: boolean;
    attrs: m.Attributes;
    open: () => void;
  }): m.Children {
    const { navKey, mark, name, owner, isCurrent, attrs, open } = options;
    return m(
      "button",
      {
        type: "button",
        role: "menuitem",
        "aria-current": isCurrent ? "true" : undefined,
        ...highlight.rowAttrs(navKey),
        class: highlight.rowClass(MENU_ROW_CLASS, navKey, { isCurrent }),
        ...attrs,
        onclick: open,
      },
      [
        mark,
        m("span", { class: "flex-1 truncate" }, name),
        owner === ""
          ? null
          : m("span", { class: "shrink-0 type-helper text-tertiary truncate max-w-[120px]" }, owner),
        isCurrent ? menuCurrentCheck() : null,
      ],
    );
  }

  /** Enter with no row highlighted: go to the first place the search lists other than the one on screen, and
   * close the menu when the one on screen is all it lists. */
  function openFirstMatch(shell: ShellState, rows: WorkspaceSwitcherRows): void {
    const owned = rows.owned.find((entry) => !entry.is_remote && entry.id !== rows.currentOwnedId);
    if (owned !== undefined) {
      shell.closeWorkspaceSwitcher();
      if (owned.create_attempt_state) m.route.set(`/creating/${owned.id}`);
      else shell.enterWorkspace(owned.id);
      return;
    }
    const sharedWorkspace = rows.sharedWorkspaces.find((entry) => entry !== rows.currentSharedWorkspace);
    if (sharedWorkspace !== undefined) {
      openShared(shell, sharedWorkspace.share.share_key, null, sharedWorkspace.name);
      return;
    }
    const sharedApp = rows.sharedApps.find((app) => app !== rows.currentSharedApp);
    if (sharedApp !== undefined) {
      openShared(shell, sharedApp.share.share_key, sharedApp.target.app, sharedApp.name);
      return;
    }
    const isCurrentListed =
      rows.owned.some((entry) => entry.id === rows.currentOwnedId) ||
      rows.sharedWorkspaces.some((entry) => entry === rows.currentSharedWorkspace) ||
      rows.sharedApps.some((app) => app === rows.currentSharedApp);
    if (isCurrentListed) shell.closeWorkspaceSwitcher();
  }

  return {
    onupdate() {
      anchors.remeasure();
    },
    view(vnode) {
      const { shell } = vnode.attrs;
      if (!shell.isWorkspaceSwitcherOpen) {
        query = "";
        highlight.key = null;
        isShortcutLabelRead = false;
        return null;
      }
      // The binding can change between opens (Settings, another window), so its label is read on each one.
      if (!isShortcutLabelRead) {
        isShortcutLabelRead = true;
        void shell.workspaceSwitcherShortcutLabel().then((label) => {
          shortcutLabel = label;
          m.redraw();
        });
      }
      const rows = workspaceSwitcherRows(shell, query);
      const close = () => shell.closeWorkspaceSwitcher();
      const currentOwnedId = rows.currentOwnedId;
      const displayedShared = displayedSharedEntry(shell);
      const hasButton = readAnchorRect(WORKSPACE_BUTTON_ID) !== null;
      const isAnyListed = rows.owned.length + rows.sharedWorkspaces.length + rows.sharedApps.length > 0;

      return m(
        "div#workspace-switcher-backdrop",
        {
          class: "fixed inset-0 z-[200] bg-black/20",
          onclick: (event: MouseEvent) => {
            if (event.target === event.currentTarget) close();
          },
        },
        [
          m(
            "div#workspace-switcher-menu",
            {
              role: "menu",
              "aria-label": "Workspaces",
              style: menuStyle(hasButton ? WORKSPACE_BUTTON_ID : PAGE_CRUMB_ID),
              // With no button to join, every corner is round.
              class: MENU_CLASS + (hasButton ? "" : " !rounded-tl-lg"),
              // The arrow keys' row stays in view in a menu that scrolls.
              onupdate: ({ dom }: m.VnodeDOM) =>
                dom.querySelector("[data-highlighted]")?.scrollIntoView({ block: "nearest" }),
            },
            [
              m("div", { class: "relative flex items-center p-1" }, [
                shortcutLabel === null
                  ? null
                  : m(
                      "kbd",
                      {
                        id: "workspace-switcher-shortcut",
                        "aria-hidden": "true",
                        class:
                          "pointer-events-none absolute right-3 rounded border border-subtle bg-fill-subtle px-1 font-sans type-helper text-tertiary",
                      },
                      shortcutLabel,
                    ),
                m("input#workspace-switcher-search", {
                  type: "text",
                  role: "searchbox",
                  "aria-label": "Search workspaces and apps",
                  placeholder: "Search workspaces and apps",
                  autocomplete: "off",
                  spellcheck: false,
                  class:
                    "h-8 w-full rounded-md border border-subtle bg-surface-primary pl-2 pr-24 type-helper text-primary " +
                    "placeholder:text-tertiary outline-none",
                  value: query,
                  oncreate: ({ dom }: m.VnodeDOM) => (dom as HTMLInputElement).focus(),
                  oninput: (event: Event) => {
                    query = (event.target as HTMLInputElement).value;
                    highlight.key = null;
                  },
                  onkeydown: (event: KeyboardEvent & { redraw?: boolean }) => {
                    const menu = (event.currentTarget as Element).closest("#workspace-switcher-menu");
                    if (menu === null) return;
                    if (highlight.handleKey(event, menu)) return;
                    if (event.key === "Enter") {
                      event.preventDefault();
                      openFirstMatch(shell, rows);
                      return;
                    }
                    event.redraw = false;
                  },
                }),
              ]),
              menuDivider(),
              query.trim() !== "" && !isAnyListed
                ? m(
                    "p#workspace-switcher-no-match",
                    { class: "px-2 py-1.5 type-helper text-tertiary" },
                    "No workspaces or apps match.",
                  )
                : null,
              menuSectionHeading("Your workspaces"),
              m("div#workspace-switcher-owned", { class: "flex flex-col gap-0.5" }, [
                rows.owned.map((workspace) => ownedRow(shell, workspace, workspace.id === currentOwnedId)),
                menuActionRow(highlight, {
                  id: "workspace-switcher-new",
                  navKey: "new",
                  icon: "plus",
                  label: "New workspace",
                  onclick: () => {
                    close();
                    shell.routeToHub("/create");
                  },
                }),
              ]),
              rows.sharedWorkspaces.length > 0
                ? [
                    menuDivider(),
                    menuSectionHeading("Workspaces shared with you"),
                    rows.sharedWorkspaces.map((entry) => {
                      const name = entry.name || entry.id;
                      const isCurrent = entry === rows.currentSharedWorkspace;
                      return sharedRow({
                        navKey: `shared:${entry.share.share_key}`,
                        mark: workspaceSwatch(entry.accent || null),
                        name,
                        owner: ownerOf(entry),
                        isCurrent,
                        attrs: { "data-share-key": entry.share.share_key },
                        open: isCurrent ? close : () => openShared(shell, entry.share.share_key, null, name),
                      });
                    }),
                  ]
                : null,
              rows.sharedApps.length > 0
                ? [
                    menuDivider(),
                    menuSectionHeading("Apps shared with you"),
                    rows.sharedApps.map((app) => {
                      const isCurrent = app === rows.currentSharedApp;
                      return sharedRow({
                        navKey: `app:${app.share.share_key}:${app.target.app}`,
                        mark: m(MonogramTile, { name: app.name, extra: "w-4 h-4 shrink-0 rounded-[5px] text-[8px]" }),
                        name: app.name,
                        owner: isSharedEntry(app.entry) ? ownerOf(app.entry) : "",
                        isCurrent,
                        attrs: { "data-share-key": app.share.share_key, "data-shared-app": app.target.app },
                        open: isCurrent
                          ? close
                          : () => openShared(shell, app.share.share_key, app.target.app, app.name),
                      });
                    }),
                  ]
                : null,
              // Last, under a rule of its own since it opens Settings instead of a place to go: about the workspace
              // on screen -- its settings when it is the account's own, who shared it when it is not.
              displayedShared !== null
                ? [
                    menuDivider(),
                    menuActionRow(highlight, {
                      id: "workspace-switcher-shared-by",
                      navKey: "about",
                      icon: "user",
                      label: displayedShared.share.is_manual
                        ? "About this link"
                        : `Shared by ${ownerLabel(displayedShared.share)}`,
                      onclick: () => {
                        close();
                        shell.openSettings({
                          tab: "workspace",
                          settings_workspace: sharedRouteId(displayedShared.share.share_key),
                        });
                      },
                    }),
                  ]
                : currentOwnedId !== null
                  ? [
                      menuDivider(),
                      menuActionRow(highlight, {
                        id: "workspace-switcher-settings",
                        navKey: "settings",
                        icon: "settings",
                        label: "Workspace & app settings",
                        onclick: () => {
                          close();
                          shell.openSettings({ tab: "workspace", settings_workspace: currentOwnedId });
                        },
                      }),
                    ]
                  : null,
            ],
          ),
          m(RaisedTitlebarIcons, {
            shell,
            anchors,
            selected: "workspace",
            onDismiss: close,
            onSelect: (id) => openTitlebarPopup(shell, id),
          }),
        ],
      );
    },
  };
}
