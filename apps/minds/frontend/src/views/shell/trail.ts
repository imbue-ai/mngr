// What the bar's two trail buttons show: the window's account, and the
// workspace (or the one shared app) open in it. Drawn twice, by the bar and
// by the raised strip that stands in for the bar while a flyout is open, so
// the content lives here and neither draws its own version.

import m from "mithril";
import type { SharedEntry } from "../../models/sharedWithMe";
import { isSharedEntry, ownerLabel, sharedAppsOf } from "../../models/sharedWithMe";
import { Icon16 } from "../components/Icon";
import { MonogramTile } from "../components/MonogramTile";
import { classifyRoute } from "./classify";
import type { ShellState } from "./shell-state";

export const ACCOUNT_BUTTON_ID = "account-switcher-btn";
export const WORKSPACE_BUTTON_ID = "workspace-switcher-btn";

/** The trail's buttons, each ending in its menu's chevron: 10px before the content, 6px after the chevron, whose
 * glyph carries air of its own, and 4px between the parts. */
export const TRAIL_BUTTON_INSET = "pl-2.5 pr-1.5 py-1 gap-1 min-w-0";

/** What the bar says where no workspace is open, on the button that opens the switcher. */
export const OPEN_WORKSPACE_LABEL = "Open workspace";

const SIGNED_OUT_MARK = "(signed out)";

/** A workspace's colour as a 12px dot in a 16px slot, or a 24px dot for a title. A faint ring in the text's colour
 * keeps a pale one in sight, and a workspace with no colour is the ring alone. On the bar, which is painted in that
 * same colour, the dot is tinted toward the bar's text (.workspace-swatch-on-bar). */
export function workspaceSwatch(
  accent: string | null,
  options: { isOnBar?: boolean; size?: "sm" | "md" } = {},
): m.Children {
  const { isOnBar = false, size = "sm" } = options;
  return m(
    "span",
    {
      class: "shrink-0 inline-flex items-center justify-center " + (size === "sm" ? "w-4 h-4" : "w-6 h-6"),
      "aria-hidden": "true",
    },
    m("span", {
      class:
        "rounded-full border border-current/30 " +
        (size === "sm" ? "w-3 h-3" : "w-6 h-6") +
        (isOnBar ? " workspace-swatch-on-bar" : ""),
      "data-workspace-swatch": accent ?? "",
      style: accent ? (isOnBar ? `--swatch-color: ${accent};` : `background-color: ${accent};`) : undefined,
    }),
  );
}

/** What the account button says: the account's email (or "This device"), and whether it has no session here. */
export interface AccountButtonLabel {
  name: string;
  isSignedOut: boolean;
}

export function accountButtonLabel(shell: ShellState): AccountButtonLabel {
  return {
    name: shell.stores.accounts.emailLabelFor(shell.accountId),
    isSignedOut: shell.isAccountSignedOut(),
  };
}

/** The account button's content. `nameId` is the name span's DOM id; the raised copy passes null, since the real
 * button already carries it. The signed-out mark is its own node, so a long email never swallows it. */
export function accountButtonContent(shell: ShellState, isOpen: boolean, nameId: string | null): m.Children {
  const label = accountButtonLabel(shell);
  return [
    m("span", { id: nameId ?? undefined, class: "type-label truncate max-w-[220px]" }, label.name),
    label.isSignedOut
      ? m(
          "span",
          {
            "data-account-signed-out": "true",
            class:
              "type-body font-semibold shrink-0 inline-flex items-center h-6 px-2 rounded-full bg-warning text-white whitespace-nowrap",
          },
          SIGNED_OUT_MARK,
        )
      : null,
    m(Icon16, { name: isOpen ? "chevron-up" : "chevron-down-small", extra: "shrink-0 text-tertiary" }),
  ];
}

/** What the workspace button stands for. */
export interface TrailWorkspace {
  /** The workspace's name, or the shared app's when one is open on its own. */
  name: string;
  accent: string | null;
  /** Who shared it with the account; null for one of the account's own. */
  owner: string | null;
  /** Whether the frame shows one shared app on its own, which the button then names with its tile. */
  isSharedApp: boolean;
}

/** The shared row on screen, or null when the window shows one of the account's own workspaces, or none. */
export function displayedSharedEntry(shell: ShellState): SharedEntry | null {
  const displayed = shell.displayedWorkspaceAnyId;
  const entry = displayed === null ? null : shell.stores.workspaces.entryByAnyId(displayed);
  return entry !== null && isSharedEntry(entry) ? entry : null;
}

/** The workspace the bar names: the one on screen, the one a workspace page (its backups, its recovery) is about,
 * or the one a creation page is making; null on a hub page. The bar's button and its raised copy both read this,
 * so the copy never names something else. */
export function barWorkspaceAnyId(shell: ShellState): string | null {
  const context = classifyRoute(shell.currentRoutePath(), shell.currentRouteSearch());
  const named = context.kind === "workspace" || context.kind === "creating" ? context.workspaceAnyId : null;
  return named ?? shell.displayedWorkspaceAnyId;
}

/** The existing workspace the bar's status pill and pending-request dot speak for: the one the bar names, unless
 * that is a workspace still being made, which has neither yet. */
export function barStatusWorkspaceAgentId(shell: ShellState): string | null {
  const context = classifyRoute(shell.currentRoutePath(), shell.currentRouteSearch());
  if (context.kind === "creating") return null;
  const anyId = barWorkspaceAnyId(shell);
  return anyId === null ? null : shell.stores.workspaces.toAgentScopedId(anyId);
}

/** What the workspace button names for the workspace `anyId`; null where the bar shows none. */
export function trailWorkspaceFor(shell: ShellState, anyId: string | null): TrailWorkspace | null {
  if (anyId === null) return null;
  const workspaces = shell.stores.workspaces;
  const entry = workspaces.entryByAnyId(anyId);
  const accentEntry = workspaces.accentEntry(anyId);
  const name = accentEntry?.name ?? entry?.name ?? "…";
  const accent = accentEntry?.accent ?? entry?.accent ?? null;
  if (entry === null || !isSharedEntry(entry)) return { name, accent, owner: null, isSharedApp: false };
  const owner = entry.share.is_manual ? null : ownerLabel(entry.share);
  const app = shell.sharedFrameTargetApp(entry);
  if (app === null) return { name, accent, owner, isSharedApp: false };
  const sharedApp = sharedAppsOf(entry).find((candidate) => candidate.target.app === app) ?? null;
  return { name: sharedApp?.name ?? app, accent, owner, isSharedApp: true };
}

/** Whose it is, after a name: in the muted text colour. */
function ownerAside(owner: string): m.Children {
  return m("span", { class: "text-secondary font-normal" }, ` (${owner})`);
}

/** The workspace button's content: the colour dot (or a shared app's tile), the name and its owner, and the chevron.
 * With no workspace on screen it is the muted invitation to open one. `isOnBar` is false for the raised copy of the
 * open menu's button, which is drawn on the card's surface and wears the plain dot. */
export function workspaceButtonContent(
  workspace: TrailWorkspace | null,
  options: { isOpen: boolean; isOnBar: boolean; nameId: string | null },
): m.Children {
  const { isOpen, isOnBar, nameId } = options;
  const chevron = m(Icon16, { name: isOpen ? "chevron-up" : "chevron-down-small", extra: "shrink-0 text-tertiary" });
  if (workspace === null) {
    return [
      m("span", { id: nameId ?? undefined, class: "type-label text-secondary whitespace-nowrap" }, OPEN_WORKSPACE_LABEL),
      chevron,
    ];
  }
  return [
    workspace.isSharedApp
      ? m(MonogramTile, { name: workspace.name, extra: "w-4 h-4 shrink-0 rounded-[5px] text-[8px]" })
      : workspaceSwatch(workspace.accent, { isOnBar }),
    m("span", { id: nameId ?? undefined, class: "type-label truncate max-w-[220px]" }, [
      workspace.name,
      workspace.owner === null ? null : ownerAside(workspace.owner),
    ]),
    chevron,
  ];
}
