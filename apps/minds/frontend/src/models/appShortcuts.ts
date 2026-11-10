// Imbue Studio's own shortcuts (the keyboard-shortcuts spec, section 8): the
// catalog, how a window resolves its account's chords, and what a save
// writes. Main runs them before any page sees the key (electron/main.js, with
// its defaults mirrored in electron/app-shortcuts.js); the workspace's
// keyboard shortcuts page lists and edits them (minds:chrome-shortcuts).
//
// Saved chords live in the account's settings under
// `keybindings["imbue-studio"][id]`, only where they differ from the
// defaults; there is no workspace override.

import { electronBridge } from "../electron-bridge";
import { fetchAccountSettings, putAccountSettings } from "./accountSettings";
import type { AccountKeybindings, AccountSettings } from "./accountSettings";

/** The app name Imbue Studio's shortcuts are stored under in the account's keybindings. */
export const APP_SHORTCUTS_APP_NAME = "imbue-studio";

export type AppShortcutId =
  | "new-window"
  | "close-window"
  | "quit"
  | "zoom-in"
  | "zoom-out"
  | "zoom-reset";

export interface AppShortcutDeclaration {
  readonly id: AppShortcutId;
  readonly label: string;
  readonly defaultChords: readonly string[];
}

/** The catalog, in the order the shortcuts page lists it; every action on every OS. */
export const APP_SHORTCUTS: readonly AppShortcutDeclaration[] = [
  { id: "new-window", label: "New window", defaultChords: ["meta+n"] },
  { id: "close-window", label: "Close window", defaultChords: ["meta+w"] },
  { id: "quit", label: "Quit", defaultChords: ["meta+q"] },
  { id: "zoom-in", label: "Zoom in", defaultChords: ["meta+="] },
  { id: "zoom-out", label: "Zoom out", defaultChords: ["meta+-"] },
  { id: "zoom-reset", label: "Reset zoom", defaultChords: ["meta+0"] },
];

/** One action as the shortcuts page gets it (minds:chrome-shortcuts). */
export interface AppShortcutView {
  readonly id: AppShortcutId;
  readonly label: string;
  readonly defaultChords: readonly string[];
  readonly chords: readonly string[];
}

export function isAppShortcutId(value: unknown): value is AppShortcutId {
  return APP_SHORTCUTS.some((declaration) => declaration.id === value);
}

function isChordList(value: unknown): value is readonly string[] {
  return Array.isArray(value) && value.every((chord) => typeof chord === "string");
}

/** Every action with its chords for the account: the saved assignment, else the defaults. */
export function resolveAppShortcuts(
  keybindings: AccountKeybindings | undefined,
): AppShortcutView[] {
  const saved = keybindings?.[APP_SHORTCUTS_APP_NAME] ?? {};
  return APP_SHORTCUTS.map((declaration) => {
    const assigned: unknown = saved[declaration.id];
    return {
      ...declaration,
      chords: isChordList(assigned) ? [...assigned] : declaration.defaultChords,
    };
  });
}

/** The chords that can arrive on the platform: off a Mac a chord holding Control (`ctrl`) is a Mac's alone. */
export function chordsForPlatform(
  chords: readonly string[],
  isMac: boolean,
): string[] {
  return isMac ? [...chords] : chords.filter((chord) => !chord.split("+").includes("ctrl"));
}

/** What main runs: each action's chords that can arrive on the platform. */
export function appShortcutChordsByAction(
  actions: readonly AppShortcutView[],
  isMac: boolean,
): Record<AppShortcutId, string[]> {
  const chordsByAction = {} as Record<AppShortcutId, string[]>;
  for (const action of actions) {
    chordsByAction[action.id] = chordsForPlatform(action.chords, isMac);
  }
  return chordsByAction;
}

/** The value a save stores for an action: null (no saved assignment) when the chords are its defaults. */
export function appShortcutWriteValue(
  id: AppShortcutId,
  chords: readonly string[],
): string[] | null {
  const declaration = APP_SHORTCUTS.find((candidate) => candidate.id === id);
  const defaults = declaration?.defaultChords ?? [];
  const isDefault =
    chords.length === defaults.length &&
    chords.every((chord, index) => chord === defaults[index]);
  return isDefault ? null : [...chords];
}

const KEY_LABELS: Readonly<Record<string, string>> = {
  arrowup: "Up",
  arrowdown: "Down",
  arrowleft: "Left",
  arrowright: "Right",
  escape: "Esc",
  pageup: "PgUp",
  pagedown: "PgDn",
  plus: "+",
};

/** A chord as Windows and Linux menus write it ("Ctrl+Shift+W"): `meta` is Control there. */
export function chordLabelOffMac(chord: string): string {
  const tokens = chord.split("+");
  const key = tokens[tokens.length - 1] ?? "";
  const modifiers = tokens.slice(0, -1);
  const parts: string[] = [];
  if (modifiers.includes("meta")) parts.push("Ctrl");
  if (modifiers.includes("alt")) parts.push("Alt");
  if (modifiers.includes("shift")) parts.push("Shift");
  const keyLabel =
    KEY_LABELS[key] ?? (key.length === 1 ? key.toUpperCase() : key.charAt(0).toUpperCase() + key.slice(1));
  parts.push(keyLabel);
  return parts.join("+");
}

type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

export interface AppShortcutsModelDeps {
  fetchImpl?: FetchLike;
  /** Whether this chrome's main runs the shortcuts (the desktop app, on a preload that knows them). */
  isRunByMain?: () => boolean;
  isMac?: () => boolean;
  setAppShortcuts?: (accountId: string, chordsByAction: Record<string, readonly string[]>) => void;
}

/**
 * Keeps main's copy of this window's account's chords current: read at boot
 * and whenever the window changes account, and sent again after every save
 * made here. A change saved on another device arrives at the next launch.
 */
export class AppShortcutsModel {
  private readonly fetchImpl: FetchLike;
  private readonly isRunByMain: () => boolean;
  private readonly isMac: () => boolean;
  private readonly sendToMain: (accountId: string, chordsByAction: Record<string, readonly string[]>) => void;
  /** The account whose chords main was last asked to run, or is about to be. */
  private followedAccountId: string | null = null;
  /** Bumped by every read and every send, so a read answered after a newer one, or after a save, is dropped. */
  private generation = 0;
  /** What main was last told, for the menus that show a shortcut beside its action. */
  private sentChordsByAction: Record<AppShortcutId, string[]> | null = null;

  constructor(deps: AppShortcutsModelDeps = {}) {
    this.fetchImpl = deps.fetchImpl ?? ((input, init) => fetch(input, init));
    this.isRunByMain = deps.isRunByMain ?? (() => electronBridge.runsAppShortcuts);
    this.isMac = deps.isMac ?? (() => electronBridge.isMacPlatform);
    this.sendToMain =
      deps.setAppShortcuts ??
      ((accountId, chordsByAction) => electronBridge.setAppShortcuts(accountId, chordsByAction));
  }

  /** Tell main the account's chords, unless they were already read for it. Cheap to call on every redraw. */
  followAccount(accountId: string): void {
    if (!this.isRunByMain() || accountId === this.followedAccountId) return;
    this.followedAccountId = accountId;
    // Until the account's own chords are sent, what the menus show is the defaults, not the last account's.
    this.sentChordsByAction = null;
    const generation = ++this.generation;
    void this.load(accountId).then(
      (actions) => {
        if (generation === this.generation) this.publish(accountId, actions);
      },
      (error: unknown) => {
        // Main keeps what it holds for the account (another window's report), else its defaults, until the next
        // account change or save.
        console.warn("Could not read Imbue Studio's shortcuts", error);
      },
    );
  }

  /** The account's actions with their chords; rejects when the settings route answers an error. */
  async load(accountId: string): Promise<AppShortcutView[]> {
    const settings = await fetchAccountSettings(this.fetchImpl, accountId);
    return resolveAppShortcuts(settings.keybindings);
  }

  /** Save one action's chords to the account and tell main; resolves every action as the account now has it. */
  async save(accountId: string, id: AppShortcutId, chords: readonly string[]): Promise<AppShortcutView[]> {
    const response = await putAccountSettings(this.fetchImpl, accountId, {
      keybindings: { [APP_SHORTCUTS_APP_NAME]: { [id]: appShortcutWriteValue(id, chords) } },
    });
    if (!response.ok) throw new Error(`The settings route answered HTTP ${response.status}`);
    const settings = (await response.json()) as AccountSettings;
    const actions = resolveAppShortcuts(settings.keybindings);
    this.publish(accountId, actions);
    return actions;
  }

  /** The chords main runs the action on: what it was last told, else the defaults. */
  chordsFor(id: AppShortcutId): readonly string[] {
    const sent = this.sentChordsByAction?.[id];
    if (sent !== undefined) return sent;
    const declaration = APP_SHORTCUTS.find((candidate) => candidate.id === id);
    return chordsForPlatform(declaration?.defaultChords ?? [], this.isMac());
  }

  private publish(accountId: string, actions: readonly AppShortcutView[]): void {
    if (!this.isRunByMain()) return;
    this.followedAccountId = accountId;
    this.generation += 1;
    this.sentChordsByAction = appShortcutChordsByAction(actions, this.isMac());
    this.sendToMain(accountId, this.sentChordsByAction);
  }
}
