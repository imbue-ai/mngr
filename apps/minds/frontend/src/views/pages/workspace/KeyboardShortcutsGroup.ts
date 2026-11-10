// The Keyboard shortcuts group inside Workspace settings: the workspace's own
// keyboard shortcuts page (`/keyboard-shortcuts`, served by its shell), framed.
// The shortcuts, their defaults, who is asking, and the saves all live in the
// workspace, so the page does the work and this group only hosts it.
//
// The page reports its state (`minds:keyboard-shortcuts-page`): its height,
// which the frame takes so the page reads as part of the pane and scrolls with
// it, and whether its recorder is listening, while which Escape closes nothing
// in the chrome and main runs no shortcut, handing the recorder every key
// instead (`minds:record-chord`). The frame stays hidden until the first
// report: a workspace too old to serve the page answers the path with its
// whole desktop, which never reports, and the group then says the workspace
// needs an update instead of showing that desktop. The first report
// is answered with what this chrome can do (`minds:embedder-capabilities`), so
// the page reads its shortcuts as the desktop app or a browser.
//
// In the desktop app the first report is also answered with Imbue Studio's own
// shortcuts for the window's account (`minds:chrome-shortcuts`), which the page
// lists in a section of their own. The page saves one with
// `minds:save-chrome-shortcut`; the group writes it to the account, tells main,
// and sends the shortcuts again, saying so when the save failed. Saves run one
// at a time, in the order the page sent them: a Replace can send several.

import m from "mithril";
import { getAppContext } from "../../../app-context";
import { electronBridge } from "../../../electron-bridge";
import { type AppShortcutView, isAppShortcutId } from "../../../models/appShortcuts";
import { Button } from "../../components/Button";
import { SectionHeader } from "../../components/Layout";
import { Notice } from "../../components/Notice";
import { Spinner } from "../../components/Spinner";
import {
  type EmbedContractModule,
  WORKSPACE_FRAME_SANDBOX,
  isWorkspaceOrigin,
  loadEmbedContract,
} from "../../shell/WorkspaceFrame";

export const KEYBOARD_SHORTCUTS_PAGE_PATH = "/keyboard-shortcuts";
// Long enough for a workspace that is waking up to answer; a page that never
// reports by then is taken for a workspace too old to serve it.
export const KEYBOARD_SHORTCUTS_READY_TIMEOUT_MS = 20_000;

export type KeyboardShortcutsPageState = "loading" | "ready" | "unavailable";

/** The part of the embed contract this group uses. */
export type KeyboardShortcutsContract = Pick<
  EmbedContractModule,
  | "KEYBOARD_SHORTCUTS_PAGE"
  | "EMBEDDER_CAPABILITIES"
  | "RECORD_CHORD"
  | "CHROME_SHORTCUTS"
  | "SAVE_CHROME_SHORTCUT"
  | "createEmbedderEndpoint"
>;

export const CHROME_SHORTCUT_SAVE_FAILED_MESSAGE =
  "Couldn't save the shortcut. Check your connection and try again.";

/** The answer to a save of a shortcut this version of Imbue Studio does not have (a page from a newer version). */
export const CHROME_SHORTCUT_UNKNOWN_MESSAGE = "This version of Imbue Studio has no such shortcut to save.";

export interface KeyboardShortcutsGroupAttrs {
  agentId: string;
  onOpenUpdates: () => void;
  /** Where the embed contract comes from: the served module unless a test hands one in. */
  loadContract?: () => Promise<KeyboardShortcutsContract>;
}

// main's record-chord relay has no unregister, so it is registered ONCE at
// module scope and forwards to whichever group is mounted.
let activeRecordChordForwarder: ((chord: string) => void) | null = null;
let isRecordChordRegistered = false;

function ensureRecordChordRegistered(): void {
  if (isRecordChordRegistered || !electronBridge.runsShellShortcuts) return;
  isRecordChordRegistered = true;
  electronBridge.onRecordChord((chord) => activeRecordChordForwarder?.(chord));
}

export function KeyboardShortcutsGroup(): m.Component<KeyboardShortcutsGroupAttrs> {
  let pageState: KeyboardShortcutsPageState = "loading";
  let pageHeight = 0;
  let frameElement: HTMLIFrameElement | null = null;
  let endpoint: {
    send(type: string, payload?: Record<string, unknown>): void;
    dispose(): void;
  } | null = null;
  let contract: KeyboardShortcutsContract | null = null;
  let recordChordForwarder: ((chord: string) => void) | null = null;
  let readyTimer: ReturnType<typeof setTimeout> | null = null;
  let isRemoved = false;
  // Imbue Studio's shortcuts as last sent to the page; null until read.
  let chromeShortcuts: readonly AppShortcutView[] | null = null;
  let pendingSaves: Promise<void> = Promise.resolve();

  function stopWaiting(): void {
    if (readyTimer !== null) clearTimeout(readyTimer);
    readyTimer = null;
  }

  function setRecording(isRecording: boolean): void {
    getAppContext().shell.isWorkspaceShortcutRecording = isRecording;
    electronBridge.setShortcutRecording(isRecording);
  }

  function sendChromeShortcuts(error?: string): void {
    if (contract === null || chromeShortcuts === null) return;
    endpoint?.send(contract.CHROME_SHORTCUTS, {
      actions: chromeShortcuts.map((action) => ({
        id: action.id,
        label: action.label,
        defaultChords: [...action.defaultChords],
        chords: [...action.chords],
      })),
      ...(error === undefined ? {} : { error }),
    });
  }

  async function loadChromeShortcuts(): Promise<void> {
    const { shell } = getAppContext();
    try {
      chromeShortcuts = await shell.appShortcuts.load(shell.accountId);
    } catch (error) {
      console.warn("Could not read Imbue Studio's shortcuts", error);
      return;
    }
    if (!isRemoved) sendChromeShortcuts();
  }

  async function saveChromeShortcut(id: string, chords: string[]): Promise<void> {
    // Before the shortcuts are read, the page has no Imbue Studio section to save from.
    if (chromeShortcuts === null) return;
    // The page settles its saves one per answer, so even a save this chrome cannot make is answered.
    if (!isAppShortcutId(id)) {
      if (!isRemoved) sendChromeShortcuts(CHROME_SHORTCUT_UNKNOWN_MESSAGE);
      return;
    }
    const { shell } = getAppContext();
    try {
      chromeShortcuts = await shell.appShortcuts.save(shell.accountId, id, chords);
    } catch (error) {
      console.warn("Could not save Imbue Studio's shortcut", error);
      if (!isRemoved) sendChromeShortcuts(CHROME_SHORTCUT_SAVE_FAILED_MESSAGE);
      return;
    }
    if (!isRemoved) sendChromeShortcuts();
  }

  function onSaveChromeShortcut(message: Record<string, unknown>): void {
    const id = message.id as string;
    const chords = message.chords as string[];
    pendingSaves = pendingSaves.then(() => saveChromeShortcut(id, chords));
  }

  function onPageReport(message: Record<string, unknown>): void {
    stopWaiting();
    if (pageState !== "ready" && contract !== null) {
      endpoint?.send(contract.EMBEDDER_CAPABILITIES, {
        canPopOut: electronBridge.canPopOut,
      });
      if (electronBridge.runsAppShortcuts) void loadChromeShortcuts();
    }
    pageState = "ready";
    pageHeight = message.height as number;
    setRecording(message.isRecording === true);
    m.redraw();
  }

  function giveUp(): void {
    if (pageState !== "loading") return;
    stopWaiting();
    pageState = "unavailable";
    m.redraw();
  }

  return {
    oncreate(vnode) {
      frameElement = (vnode.dom as HTMLElement).querySelector("iframe");
      readyTimer = setTimeout(giveUp, KEYBOARD_SHORTCUTS_READY_TIMEOUT_MS);
      const loadContract = vnode.attrs.loadContract ?? loadEmbedContract;
      void loadContract().then(
        (loaded) => {
          if (isRemoved) return;
          contract = loaded;
          endpoint = loaded.createEmbedderEndpoint({
            getFrameWindow: () => frameElement?.contentWindow ?? null,
            isExpectedOrigin: isWorkspaceOrigin,
            handlers: {
              [loaded.KEYBOARD_SHORTCUTS_PAGE]: onPageReport,
              ...(electronBridge.runsAppShortcuts
                ? { [loaded.SAVE_CHROME_SHORTCUT]: onSaveChromeShortcut }
                : {}),
            },
          });
          recordChordForwarder = (chord) =>
            endpoint?.send(loaded.RECORD_CHORD, { chord });
          activeRecordChordForwarder = recordChordForwarder;
          ensureRecordChordRegistered();
        },
        // Without the contract nothing can confirm the page loaded.
        giveUp,
      );
    },
    onremove() {
      isRemoved = true;
      if (activeRecordChordForwarder === recordChordForwarder) {
        activeRecordChordForwarder = null;
      }
      endpoint?.dispose();
      stopWaiting();
      setRecording(false);
    },
    view(vnode) {
      const { agentId, onOpenUpdates } = vnode.attrs;
      const frameUrl = getAppContext().shell.stores.workspaces.workspacePageUrl(
        agentId,
        KEYBOARD_SHORTCUTS_PAGE_PATH,
      );
      return m(
        "div",
        { id: "keyboard-shortcuts-group", "data-page-state": pageState },
        [
          m(SectionHeader, {}, "Keyboard shortcuts"),
          m(
            "p",
            { class: "type-body text-secondary mb-4" },
            electronBridge.runsAppShortcuts
              ? "The shortcuts of this workspace's apps, then Imbue Studio's own. Save a workspace app's change for this workspace only, or for your account to use it in every workspace; Imbue Studio's are saved for your account."
              : "The shortcuts of this workspace's apps. Save a change for this workspace only, or for your account to use it in every workspace.",
          ),
          pageState === "loading"
            ? m(
                "div",
                { class: "flex items-center gap-2 type-body text-secondary" },
                [m(Spinner, { size: "sm" }), "Loading the shortcuts..."],
              )
            : null,
          pageState === "unavailable"
            ? m(
                Notice,
                { variant: "warn", id: "keyboard-shortcuts-unavailable" },
                [
                  m(
                    "p",
                    {},
                    "This workspace didn't load its keyboard shortcuts. Make sure it is running; if it is, it may need an update to customize shortcuts here.",
                  ),
                  m(
                    Button,
                    { extra: "mt-2", onclick: onOpenUpdates },
                    "Go to Updates",
                  ),
                ],
              )
            : null,
          m("iframe", {
            id: "keyboard-shortcuts-frame",
            title: "Keyboard shortcuts",
            src: frameUrl,
            sandbox: WORKSPACE_FRAME_SANDBOX,
            class:
              "block w-full border-0 bg-transparent " +
              (pageState === "ready" ? "" : "hidden"),
            style: { height: `${pageHeight}px` },
          }),
        ],
      );
    },
  };
}
