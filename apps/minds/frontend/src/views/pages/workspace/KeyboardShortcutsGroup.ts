// The Keyboard shortcuts group inside Workspace settings: the workspace's own
// keyboard shortcuts page (`/keyboard-shortcuts`, served by its shell), framed.
// The shortcuts, their defaults, who is asking, and the saves all live in the
// workspace, so the page does the work and this group only hosts it.
//
// The page reports its state (`minds:keyboard-shortcuts-page`): its height,
// which the frame takes so the page reads as part of the pane and scrolls with
// it, and whether its recorder is listening, while which Escape closes nothing
// in the chrome and the close-tab chord closes no window. The frame stays hidden
// until the first
// report: a workspace too old to serve the page answers the path with its whole
// desktop, which never reports, and the group then says the workspace needs an
// update instead of showing that desktop.

import m from "mithril";
import { getAppContext } from "../../../app-context";
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
  "KEYBOARD_SHORTCUTS_PAGE" | "createEmbedderEndpoint"
>;

export interface KeyboardShortcutsGroupAttrs {
  agentId: string;
  onOpenUpdates: () => void;
  /** Where the embed contract comes from: the served module unless a test hands one in. */
  loadContract?: () => Promise<KeyboardShortcutsContract>;
}

export function KeyboardShortcutsGroup(): m.Component<KeyboardShortcutsGroupAttrs> {
  let pageState: KeyboardShortcutsPageState = "loading";
  let pageHeight = 0;
  let frameElement: HTMLIFrameElement | null = null;
  let endpoint: { dispose(): void } | null = null;
  let readyTimer: ReturnType<typeof setTimeout> | null = null;
  let isRemoved = false;

  function stopWaiting(): void {
    if (readyTimer !== null) clearTimeout(readyTimer);
    readyTimer = null;
  }

  function onPageReport(message: Record<string, unknown>): void {
    stopWaiting();
    pageState = "ready";
    pageHeight = message.height as number;
    getAppContext().shell.isWorkspaceShortcutRecording =
      message.isRecording === true;
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
        (contract) => {
          if (isRemoved) return;
          endpoint = contract.createEmbedderEndpoint({
            getFrameWindow: () => frameElement?.contentWindow ?? null,
            isExpectedOrigin: isWorkspaceOrigin,
            handlers: { [contract.KEYBOARD_SHORTCUTS_PAGE]: onPageReport },
          });
        },
        // Without the contract nothing can confirm the page loaded.
        giveUp,
      );
    },
    onremove() {
      isRemoved = true;
      endpoint?.dispose();
      stopWaiting();
      getAppContext().shell.isWorkspaceShortcutRecording = false;
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
            "The shortcuts of this workspace's apps. Save a change for this workspace only, or for your account to use it in every workspace.",
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
