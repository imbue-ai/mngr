// Settings: the one place every setting lives, hung from the bar's gear
// (specs/studio-top-bar-and-settings). Three tabs, each with its own rail:
//
// - Studio: the app's own sections (Notifications, Display, Updates...).
// - Accounts: one entry per signed-in account (its plan, usage and log-out).
// - Workspace & apps: a workspace's General, Permissions, Sharing, Backup and
//   Updates, or the About page of a workspace or app shared with the account.
//   A picker at the top of the rail says which; it changes what Settings
//   shows, not what the window shows.
//
// The URL is the single source of truth for where Settings is (see
// settings-route.ts): every choice here is a param written back with a
// history-replacing route set, and re-read on every render. That is what lets
// a deep link, the bar's Share, a workspace's own Share button, and the return
// from reviewing a permission request all land on a place by naming it.

import m from "mithril";
import { getAppContext } from "../../app-context";
import type { UiAccountEntry, UiWorkspaceEntry } from "../../channel/messages";
import { accountEmailLabelFor } from "../../models/accounts";
import { AccountsDetailModel } from "../../models/accountsDetail";
import type { ErrorAction } from "../../models/errors";
import { errorFromException } from "../../models/errors";
import type { SettingsSection } from "../../models/settings";
import { SettingsModel } from "../../models/settings";
import type { SharedApp, SharedEntry } from "../../models/sharedWithMe";
import { isSharedEntry, listedSharedRows, ownedWorkspaceEntries, ownerLabel } from "../../models/sharedWithMe";
import { webLogin } from "../../models/webLogin";
import { WorkspaceOptionsModel } from "../../models/workspaceOptions";
import { PermissionsModel } from "../../models/workspacePermissions";
import { Badge } from "../components/Badge";
import { ErrorNotice, restartAppActions } from "../components/ErrorDisplay";
import { Icon16 } from "../components/Icon";
import type { IconName } from "../components/icons";
import { Spinner } from "../components/Spinner";
import { navEntryClass, splitPane } from "../components/SplitPane";
import { AccountCard } from "./settings/AccountCard";
import type { MasterPasswordState } from "./settings/SettingsSections";
import { studioSettingsPanel } from "./settings/SettingsSections";
import { SharedWorkspaceAbout } from "./settings/SharedWorkspaceAbout";
import type { SettingsPlace, SettingsTab, WorkspaceSettingsGroup as WorkspaceGroupName } from "./settings/settings-route";
import { currentSettingsPlace, rememberSettingsPlace } from "./settings/settings-route";
import { PermissionsTab, permissionsNav } from "./workspace/PermissionsTab";
import { WorkspaceSettingsGroup, updatesNavDot } from "./workspace/SettingsGroups";
import { ShareTab, sharingNav } from "./workspace/ShareTab";

const TABS: readonly { id: SettingsTab; icon: IconName; label: string }[] = [
  { id: "studio", icon: "globe", label: "Studio" },
  { id: "accounts", icon: "user", label: "Accounts" },
  { id: "workspace", icon: "panels-top-left", label: "Workspace & apps" },
];

const WORKSPACE_ENTRIES: readonly { id: WorkspaceGroupName; icon: IconName; label: string }[] = [
  { id: "general", icon: "info", label: "General" },
  { id: "permissions", icon: "key", label: "Permissions" },
  { id: "sharing", icon: "share", label: "Sharing" },
  { id: "backup", icon: "cloud", label: "Backup" },
  { id: "updates", icon: "restart", label: "Updates" },
];

/** Between a row's id and an app's name, in the picker's value for an app shared with the account. */
const APP_VALUE_SEPARATOR = "#";

/** One thing the Workspace & apps tab can speak for. */
export interface PickableRow {
  /** The picker's value for it. */
  value: string;
  label: string;
  entry: UiWorkspaceEntry;
  /** The shared app the pick names; null for a whole workspace. */
  app: string | null;
  group: "owned" | "sharedWorkspace" | "sharedApp";
}

/** What the picker offers, in its three groups' order: the account's own live workspaces (stopped ones included),
 * the workspaces shared with it, and the apps shared with it. A create attempt is not a workspace yet, and a
 * workspace on another device has no settings this one can reach. */
export function pickableRows(entries: readonly UiWorkspaceEntry[]): PickableRow[] {
  const owned = ownedWorkspaceEntries(entries).filter((entry) => !entry.create_attempt_state && !entry.is_remote);
  const listed = listedSharedRows(entries);
  const ownerSuffix = (entry: UiWorkspaceEntry): string =>
    isSharedEntry(entry) && !entry.share.is_manual ? ` (${ownerLabel(entry.share)})` : "";
  return [
    ...owned.map(
      (entry): PickableRow => ({ value: entry.id, label: entry.name || entry.id, entry, app: null, group: "owned" }),
    ),
    ...listed.workspaceShares.map(
      (entry: SharedEntry): PickableRow => ({
        value: entry.id,
        label: entry.name || entry.id,
        entry,
        app: null,
        group: "sharedWorkspace",
      }),
    ),
    ...listed.sharedApps.map(
      (app: SharedApp): PickableRow => ({
        value: `${app.entry.id}${APP_VALUE_SEPARATOR}${app.target.app}`,
        label: app.name + ownerSuffix(app.entry),
        entry: app.entry,
        app: app.target.app,
        group: "sharedApp",
      }),
    ),
  ];
}

/** The row the place names, else the one Settings floats over, else the first on offer; null with nothing to pick.
 * `entryByAnyId` resolves an id as the routes spell it (an agent or host coordinate, or a shared row's `shared-`
 * alias) to its list entry, the way the workspace store does. */
export function pickedRow(
  rows: readonly PickableRow[],
  place: Pick<SettingsPlace, "settingsWorkspace" | "app" | "behindWorkspace">,
  entryByAnyId: (anyId: string) => UiWorkspaceEntry | null,
): PickableRow | null {
  const rowFor = (anyId: string | null, app: string | null): PickableRow | null => {
    if (anyId === null) return null;
    const entry = entryByAnyId(anyId);
    if (entry === null) return null;
    const ofEntry = rows.filter((row) => row.entry.id === entry.id);
    return ofEntry.find((row) => row.app === app) ?? ofEntry[0] ?? null;
  };
  return rowFor(place.settingsWorkspace, place.app) ?? rowFor(place.behindWorkspace, null) ?? rows[0] ?? null;
}

/** The tab Settings shows: the one the URL asks for, else the workspace's when Settings was opened over one and
 * Studio's from Home. The Workspace & apps tab has nothing to show without a workspace to pick. */
function shownTabFor(place: SettingsPlace, picked: PickableRow | null): SettingsTab {
  const asked = place.tab ?? (place.behindWorkspace !== null ? "workspace" : "studio");
  return asked === "workspace" && picked === null ? "studio" : asked;
}

function navEntry(isSelected: boolean, onclick: () => void, children: m.Children, attrs: m.Attributes = {}): m.Children {
  return m(
    "button",
    {
      type: "button",
      class: navEntryClass(isSelected),
      "aria-pressed": isSelected ? "true" : "false",
      onclick,
      ...attrs,
    },
    children,
  );
}

function nestedNav(id: string, children: m.Children): m.Children {
  return m(`div#${id}`, { class: "ml-4 pl-2 my-1 border-l border-subtle flex flex-col gap-0.5" }, children);
}

function loadingLine(text: string): m.Children {
  return m("p", { class: "type-body text-secondary flex items-center gap-2" }, [m(Spinner, { size: "sm" }), text]);
}

export function SettingsPage(): m.Component {
  const studio = new SettingsModel();
  const accountsDetail = new AccountsDetailModel();
  const masterPasswordState: MasterPasswordState = { newPassword: "", confirmPassword: "" };
  // The workspace the Workspace & apps tab speaks for, and its two reads. Permissions has its own, so an
  // unreachable gateway cannot take General, Backup and Updates down (nor they it).
  let workspaceModel: WorkspaceOptionsModel | null = null;
  let permissions: PermissionsModel | null = null;
  // The `share_target` last applied to the share model: only a CHANGE in the param selects, so the page's own
  // moves between targets (its app tiles) are not fought by an unchanged one.
  let appliedShareTarget: string | null = null;

  function dropWorkspaceModels(): void {
    if (permissions !== null) getAppContext().shell.unregisterWaitingRequestList(permissions);
    workspaceModel?.dispose();
    workspaceModel = null;
    permissions = null;
    appliedShareTarget = null;
  }

  /** The models of the owned workspace `agentId`, made on first use and swapped when the pick changes. */
  function modelsFor(agentId: string): { model: WorkspaceOptionsModel; permissions: PermissionsModel } {
    if (workspaceModel !== null && workspaceModel.agentId !== agentId) dropWorkspaceModels();
    if (workspaceModel === null) {
      workspaceModel = new WorkspaceOptionsModel(agentId, {
        shareOverrides: {
          workspaceLiveness: () => getAppContext().stores.workspaces.entryByAnyId(agentId)?.liveness ?? "",
        },
      });
      void workspaceModel.load();
    }
    if (permissions === null) {
      // Constructed but not loaded: the Permissions page reads on its first mount, so opening General never
      // touches the gateway.
      permissions = new PermissionsModel(agentId);
      // The request popup answers a request; this is what shows the list it was in, so the popup reaches the list
      // through the shell.
      getAppContext().shell.registerWaitingRequestList(permissions);
    }
    return { model: workspaceModel, permissions };
  }

  /** Keep the share model's target and the URL's `share_target` saying the same thing: a changed param selects,
   * and a target the page moved to on its own is written back. */
  function syncShareTarget(model: WorkspaceOptionsModel, place: SettingsPlace): void {
    const share = model.share;
    if (share === null) return;
    if (place.shareTarget !== appliedShareTarget) {
      appliedShareTarget = place.shareTarget;
      share.selectTarget(place.shareTarget ?? share.wholeService);
    }
    const current = share.currentTarget === share.wholeService ? null : share.currentTarget;
    if (current !== appliedShareTarget) {
      appliedShareTarget = current;
      rememberSettingsPlace({ share_target: current });
    }
  }

  function studioRail(section: SettingsSection): m.Children {
    return studio.visibleSections.map((entry) =>
      navEntry(entry.name === section, () => rememberSettingsPlace({ section: entry.name }), entry.label, {
        "data-settings-section": entry.name,
      }),
    );
  }

  function studioPanel(): m.Children {
    // Updates, Display and Keyboard shortcuts read none of the /ui/api/settings payload -- they come from the
    // Electron main process -- so they stay reachable while that payload is missing. Which matters because
    // Updates is the only surface reporting an update check, and the menu bar's "Check for Updates..." lands
    // there: gating it on the backend would make a broken backend hide the one remedy the user can apply.
    const isPayloadNeeded =
      studio.activeSection !== "updates" &&
      studio.activeSection !== "display" &&
      studio.activeSection !== "keyboard-shortcuts";
    if (studio.overview !== null || !isPayloadNeeded) return studioSettingsPanel(studio, masterPasswordState);
    if (studio.loadFailure !== null) {
      return m(ErrorNotice, {
        id: "settings-load-error",
        error: studio.loadFailure.error,
        actions: studio.loadFailure.isReconnectNeeded
          ? restartAppActions()
          : [{ label: "Try again", onclick: () => void studio.load() }],
        isBugReportOffered: studio.loadFailure.isBugReportOffered,
      });
    }
    return m("div", { class: "flex items-center gap-2 type-helper text-tertiary" }, [
      m(Spinner, { size: "sm" }),
      "Loading settings…",
    ]);
  }

  function accountsRail(accounts: readonly UiAccountEntry[], selected: UiAccountEntry | null): m.Children {
    const { shell } = getAppContext();
    return [
      accounts.map((account) =>
        navEntry(
          account.user_id === selected?.user_id,
          () => rememberSettingsPlace({ settings_account: account.user_id }),
          [
            m("span", { class: "truncate flex-1" }, accountEmailLabelFor(account)),
            account.user_id === shell.accountId
              ? m("span", { class: "type-helper text-tertiary shrink-0", title: "This window's account" }, "Current")
              : null,
          ],
          { "data-settings-account": account.user_id },
        ),
      ),
      m(
        "button#settings-add-account",
        {
          type: "button",
          class: navEntryClass(false) + " text-secondary",
          disabled: webLogin.isOpen,
          onclick: () => void webLogin.start("", { isClosedOnSignIn: true }),
        },
        [m(Icon16, { name: "plus", extra: "shrink-0" }), webLogin.isOpen ? "Opening browser…" : "Add account"],
      ),
    ];
  }

  function accountsPanel(selected: UiAccountEntry | null): m.Children {
    const retry = accountsDetail.actionErrorRetry;
    const actions: ErrorAction[] = retry === null ? [] : [{ label: "Try again", onclick: retry }];
    return [
      accountsDetail.actionError !== null
        ? m(
            "div",
            { class: "mb-4" },
            m(ErrorNotice, { id: "accounts-action-error", error: accountsDetail.actionError, actions }),
          )
        : null,
      // The card is keyed by its account, so picking another draws a fresh one. A keyed vnode needs every sibling
      // keyed, so it is its wrapper's one child.
      m("div", [
        selected === null
          ? m("p#settings-no-accounts", { class: "text-secondary" }, "No accounts logged in.")
          : m("div", { key: selected.user_id }, m(AccountCard, { model: accountsDetail, account: selected })),
      ]),
    ];
  }

  function workspacePicker(rows: readonly PickableRow[], picked: PickableRow): m.Children {
    const option = (row: PickableRow): m.Children => m("option", { value: row.value }, row.label);
    const group = (label: string, name: PickableRow["group"]): m.Children => {
      const ofGroup = rows.filter((row) => row.group === name);
      return ofGroup.length === 0 ? null : m("optgroup", { label }, ofGroup.map(option));
    };
    const isAnythingShared = rows.some((row) => row.group !== "owned");
    return m("div", { class: "shrink-0 flex flex-col" }, [
      m("label", { for: "settings-workspace-select", class: "pb-2 type-helper text-tertiary" }, "Workspace/App"),
      m(
        "select#settings-workspace-select",
        {
          "aria-label": "Workspace or app",
          class: "select-chevron w-full h-[34px] pl-2 rounded-md type-body bg-fill-subtle text-primary",
          value: picked.value,
          onchange: (event: Event) => {
            const next = rows.find((row) => row.value === (event.target as HTMLSelectElement).value);
            if (next === undefined) return;
            // A pick starts on its own Sharing target and Permissions section, not the last workspace's.
            rememberSettingsPlace({
              settings_workspace: next.entry.id,
              app: next.app,
              share_target: null,
              perm_section: null,
              focus: null,
            });
          },
        },
        isAnythingShared
          ? [
              group("Your workspaces", "owned"),
              group("Workspaces shared with you", "sharedWorkspace"),
              group("Apps shared with you", "sharedApp"),
            ]
          : rows.map(option),
      ),
      m("div", { class: "mt-4 mb-3 h-px bg-subtle", "aria-hidden": "true" }),
    ]);
  }

  function ownedWorkspaceRail(agentId: string, group: WorkspaceGroupName, place: SettingsPlace): m.Children {
    const { stores } = getAppContext();
    const { model, permissions: currentPermissions } = modelsFor(agentId);
    return WORKSPACE_ENTRIES.map((entry) => [
      navEntry(
        entry.id === group,
        () => rememberSettingsPlace({ group: entry.id, focus: null }),
        [
          m(Icon16, { name: entry.icon, extra: "shrink-0" }),
          m("span", { class: "truncate flex-1" }, entry.label),
          entry.id === "updates" ? updatesNavDot(agentId) : null,
          // Its agents are waiting on an answer.
          entry.id === "permissions" && stores.requests.hasPendingForWorkspace(agentId)
            ? m(Badge, { id: "settings-permissions-badge" })
            : null,
        ],
        { "data-settings-group": entry.id },
      ),
      entry.id === "permissions" && group === "permissions"
        ? nestedNav(
            "settings-permission-sections",
            permissionsNav(currentPermissions, place.permSection, (section) =>
              rememberSettingsPlace({ perm_section: section }),
            ),
          )
        : null,
      entry.id === "sharing" && group === "sharing" && model.share !== null
        ? nestedNav("settings-sharing-targets", sharingNav(model.share))
        : null,
    ]);
  }

  function ownedWorkspacePanel(agentId: string, group: WorkspaceGroupName, place: SettingsPlace): m.Children {
    const { shell, stores } = getAppContext();
    const { model, permissions: currentPermissions } = modelsFor(agentId);
    if (group === "permissions") {
      return m(
        "div",
        { key: `permissions:${agentId}`, class: "flex flex-1 flex-col min-h-0" },
        m(PermissionsTab, {
          model: currentPermissions,
          workspaceName: model.data?.name ?? "",
          requestedSection: place.permSection,
          onSelectSection: (section: string) => rememberSettingsPlace({ perm_section: section }),
          // The request opens as its own page over Settings, which stays mounted underneath (openInbox remembers
          // this route).
          onReviewRequest: (requestId: string) => shell.openInbox({ selected: requestId }),
        }),
      );
    }
    if (model.status === "loading") return loadingLine("Loading workspace settings...");
    if (model.status === "load_failed" || model.data === null || model.share === null) {
      return m(ErrorNotice, {
        id: "options-load-error",
        error: model.loadError ?? errorFromException("Couldn't load this workspace's settings", ""),
        actions: model.isLoadReconnectNeeded
          ? restartAppActions()
          : [{ label: "Try again", onclick: () => void model.load() }],
        isBugReportOffered: model.isLoadErrorReportable,
        workspaceAgentId: model.agentId,
      });
    }
    if (group === "sharing") {
      // The connections everyone invited reaches. A reminder, so it is left out while they are not known rather
      // than holding the page for the read, and asked for only once the first sharing read has something to draw
      // or has failed: the read is a round trip into the workspace that would otherwise compete with it.
      if (!model.share.isSharingDocumentLoading) currentPermissions.ensureLoaded();
      const connections =
        currentPermissions.status === "ready" && currentPermissions.data !== null
          ? currentPermissions.data.connections
          : null;
      return m(
        "div",
        { key: `sharing:${agentId}`, class: "flex flex-1 flex-col min-h-0" },
        m(ShareTab, {
          share: model.share,
          workspaceName: model.data.name,
          accent: stores.workspaces.accentEntry(agentId)?.accent ?? null,
          connections,
          onOpenConnection: (sectionId: string) =>
            rememberSettingsPlace({ group: "permissions", perm_section: sectionId }),
        }),
      );
    }
    return m(
      "div",
      { key: `own:${agentId}` },
      m(WorkspaceSettingsGroup, { model, group, isOwnerSectionAsked: place.isOwnerSectionAsked }),
    );
  }

  /** Read each listed account's plan and notification preferences, but only while the Accounts tab is the one
   * shown: each read is a round trip to Imbue Cloud, and the other tabs draw nothing from them. */
  function syncAccountsWhileShown(): void {
    const { stores } = getAppContext();
    const place = currentSettingsPlace();
    const picked = pickedRow(pickableRows(stores.workspaces.workspaces), place, (anyId) =>
      stores.workspaces.entryByAnyId(anyId),
    );
    if (shownTabFor(place, picked) !== "accounts") return;
    accountsDetail.syncAccounts(stores.accounts.accounts);
  }

  return {
    oninit(): void {
      const { shell } = getAppContext();
      syncAccountsWhileShown();
      void studio.load(shell.accountId);
      // Desktop-only, and independent of the /ui/api settings payload: the update state and the zoom live in the
      // Electron main process, not the backend.
      void studio.loadUpdateState();
      void studio.loadDisplayZoom();
    },
    // Before the view rather than after it: the account card reads what the sync puts in place, which includes the
    // plan an earlier visit read.
    onbeforeupdate(): void {
      syncAccountsWhileShown();
    },
    onremove(): void {
      dropWorkspaceModels();
      accountsDetail.dispose();
    },
    view(): m.Children {
      const { shell, stores } = getAppContext();
      const place = currentSettingsPlace();
      const accounts = stores.accounts.accounts;
      const rows = pickableRows(stores.workspaces.workspaces);
      const picked = pickedRow(rows, place, (anyId) => stores.workspaces.entryByAnyId(anyId));
      const tab = shownTabFor(place, picked);
      const shownTabs = TABS.filter((entry) => entry.id !== "workspace" || picked !== null);

      let rail: m.Children = null;
      let content: m.Children = null;
      let isContentColumn = false;
      if (tab === "studio") {
        const section =
          studio.visibleSections.find((entry) => entry.name === place.section)?.name ?? studio.visibleSections[0].name;
        if (studio.activeSection !== section) studio.selectSection(section);
        rail = studioRail(section);
        content = studioPanel();
      } else if (tab === "accounts") {
        // The window's own account first; an account logged out while its entry was selected falls back to it.
        const selected =
          accounts.find((account) => account.user_id === place.settingsAccount) ??
          accounts.find((account) => account.user_id === shell.accountId) ??
          accounts[0] ??
          null;
        rail = accountsRail(accounts, selected);
        content = accountsPanel(selected);
      } else if (picked !== null) {
        if (isSharedEntry(picked.entry)) {
          dropWorkspaceModels();
          rail = [
            workspacePicker(rows, picked),
            navEntry(true, () => undefined, [
              m(Icon16, { name: "info", extra: "shrink-0" }),
              m("span", { class: "truncate flex-1" }, "About"),
            ]),
          ];
          content = m("div", { key: `about:${picked.entry.id}` }, m(SharedWorkspaceAbout, { entry: picked.entry }));
        } else {
          const agentId = picked.entry.id;
          const group = place.group ?? "general";
          const { model, permissions: currentPermissions } = modelsFor(agentId);
          syncShareTarget(model, place);
          // Every channel `requests` frame redraws mithril, so reconciling here keeps the waiting list live without
          // a subscription of its own -- and it has to be here rather than in the page, because Settings stays
          // mounted under the request popup and the page is never re-created on the way back.
          if (currentPermissions.status === "ready")
            void currentPermissions.refreshIfPendingChanged(stores.requests.requestIds);
          rail = [workspacePicker(rows, picked), ownedWorkspaceRail(agentId, group, place)];
          content = ownedWorkspacePanel(agentId, group, place);
          isContentColumn = group === "permissions" || group === "sharing";
        }
      }

      // Rendered inside the Shell's Settings flyout, which supplies the width and padding. The card's body is a
      // bounded column rather than a scroller, so the title and tabs are pinned (shrink-0) and the pane below them
      // scrolls its own columns.
      return [
        m("h1", { class: "type-heading-lg text-primary mb-4 shrink-0" }, "Settings"),
        m(
          "div#settings-tabs",
          {
            role: "tablist",
            "aria-label": "Settings",
            // The rule under the tabs runs the card's full width, past its padding.
            class: "flex shrink-0 items-center gap-1 -mx-6 px-6 pb-4 mb-6 border-b border-subtle",
          },
          shownTabs.map((entry) =>
            m(
              "button",
              {
                type: "button",
                role: "tab",
                id: `settings-tab-${entry.id}`,
                "aria-selected": entry.id === tab ? "true" : "false",
                class:
                  "inline-flex items-center gap-1.5 cursor-pointer rounded-md px-2.5 py-1 type-body border-0 " +
                  (entry.id === tab
                    ? "bg-fill-hover text-primary font-semibold"
                    : "bg-transparent text-secondary hover:text-primary"),
                onclick: () => rememberSettingsPlace({ tab: entry.id }),
              },
              [m(Icon16, { name: entry.icon, extra: "shrink-0" }), entry.label],
            ),
          ),
        ),
        splitPane({
          navLabel: "Settings",
          nav: m("div", { class: "flex flex-col gap-0.5" }, rail),
          // A keyed child needs every sibling keyed, so the page is the column's one child.
          content: [content],
          // The Permissions and Sharing pages are flex columns, with room under the last control when scrolled to
          // the end.
          contentExtra: isContentColumn ? "flex flex-col pb-8" : "",
        }),
      ];
    },
  };
}
