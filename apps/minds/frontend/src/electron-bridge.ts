// Typed, optional facade over the Electron preload surface (`mindsNative`).
//
// Feature-detected: in plain-browser mode every call is a no-op / null, so
// shell code never branches on "am I in Electron" beyond what this module
// exposes. Only genuinely native affordances live here -- window controls,
// the native file picker, focus, multi-window -- everything else the legacy
// `window.minds` bridge carried is now owned by the SPA itself.

import type { UiNotificationEntry } from "./channel/messages";

export interface FilePickerOptions {
  title?: string;
  defaultPath?: string;
  // The main process maps this to the Electron dialog property
  // (openFile / openDirectory); see ipcMain.handle('show-file-picker').
  mode?: "file" | "directory";
  properties?: string[];
}

/** Slowest to fastest; mirrors CHANNELS in electron/update-channel.js. */
export type UpdateChannel = "stable" | "beta" | "alpha";

export interface UpdateStatus {
  type:
    | "idle"
    | "checking"
    | "up-to-date"
    | "parked"
    | "update-available"
    | "update-downloaded"
    | "error"
    | "disabled";
  channel?: UpdateChannel;
  currentVersion?: string;
  /** What the channel serves. Below currentVersion exactly when parked. */
  feedVersion?: string | null;
  version?: string;
  message?: string;
  /** ISO-8601, carried on every status a settled check publishes. */
  lastCheckedAt?: string | null;
}

export interface PeekedChannel {
  /** What this channel serves right now; null when it is unreachable. */
  version: string | null;
  /** Whether moving here would stop updates until the channel catches up. */
  wouldPark: boolean;
  /**
   * Whether this install sits outside a rollout the channel has already started.
   *
   * Optional: a preload that omits it leaves the row reading "Currently on
   * <version>".
   */
  isOutsideRollout?: boolean;
  error?: string;
}

export interface UpdateState {
  channel: UpdateChannel;
  currentVersion: string;
  /** Just ["stable"] when the tier configures no channel manifest host. */
  available: UpdateChannel[];
  status: UpdateStatus;
  /** ISO-8601 when a check last settled, null before the first one. */
  lastCheckedAt?: string | null;
  /**
   * The version staged for the next restart, null when nothing is.
   *
   * Read this rather than the status to answer "is an update waiting": a
   * completed download is handed to the OS installer and goes in on the next
   * launch, but the status it published is transient and any later check
   * replaces it.
   */
  downloadedVersion?: string | null;
  /**
   * How a staged update gets applied. `on-quit` (macOS) installs when the app
   * quits or from the restart control; `on-request` (Linux) installs only from
   * the install control. Optional because the state shape is shared with the
   * browser build and with tests that stub only part of the surface; silence
   * reads as on-quit, the policy every macOS build has.
   */
  installPolicy?: UpdateInstallPolicy;
  /** Whether installing raises the system password prompt (a Linux .deb). */
  needsPasswordToInstall?: boolean;
}

export type UpdateInstallPolicy = "on-quit" | "on-request";

interface MindsNativeSurface {
  platform: string;
  minimize(): void;
  maximize(): void;
  close(): void;
  /** Bounce the backend and re-prepare every window. Named `retry` on the
   * preload, where it started as the error takeover's button. */
  retry(): void;
  showFilePicker(options: FilePickerOptions): Promise<string | null>;
  /** Open the OS's own notification-settings pane -- no app can force a
   * re-prompt once permission is declined, so this is the escape hatch.
   * Resolves to whether it actually opened. */
  openNotificationSettings(): Promise<boolean>;
  bringAppToFront(): void;
  openWorkspaceInNewWindow(agentId: string): void;
  openNotificationInExistingWindow?(
    route: string,
    entry: UiNotificationEntry,
  ): Promise<boolean>;
  onOpenNotification?(callback: (entry: UiNotificationEntry) => void): void;
  onNavigate(callback: (url: string) => void): void;
  onOpenOverlay(callback: (cmd: unknown) => void): void;
  onCloseActiveTab(callback: () => void): void;
  onEscapePressed(callback: () => void): void;
  // Main relays each window's own focus and blur (the signal the renderer
  // cannot see while keyboard focus sits inside the workspace iframe).
  onWindowFocusChanged?(callback: (isFocused: boolean) => void): void;
  // Main asks this window to flash a plain in-app toast (the "couldn't open
  // link" fallback).
  onToast?(callback: (toast: { title: string; body: string }) => void): void;
  // Renderer -> main shell-event relay (workspace_stopped, focus requests).
  // Added alongside the SPA shell; older preloads lack it, hence optional.
  sendShellEvent?(event: { type: string } & Record<string, unknown>): void;
  // Display zoom (Settings > Display). Optional: a preload from before the
  // setting shipped lacks them, and the browser build scales with the browser.
  getDisplayZoom?(): Promise<number>;
  setDisplayZoom?(percent: number): Promise<number>;
  // Release channels. Optional: a preload from before channels shipped lacks
  // them, and the browser build has no binary to update at all.
  getUpdateState?(): Promise<UpdateState>;
  peekUpdateChannels?(): Promise<Record<string, PeekedChannel>>;
  setUpdateChannel?(channel: UpdateChannel): Promise<UpdateState>;
  checkForUpdates?(): Promise<UpdateState>;
  // Resolves the failure as a payload: a rejected invoke would arrive wrapped
  // in Electron's "Error invoking remote method" text. A stub that resolves
  // nothing (the browser build, a partial test surface) reads as success.
  installUpdate?(): Promise<InstallUpdateOutcome | void>;
  onUpdateStatus?(callback: (status: UpdateStatus) => void): void;
  // Pulled-out windows (the pull-out-window spec). Optional: a preload from
  // before the feature lacks them, and the browser build has no windows.
  openPopoutWindow?(request: PopoutOpenRequest): void;
  // A workspace title-bar drag main watches from here (the pull-out-window
  // spec, section 5.1); it reports each step of the tear-out through onTearOut.
  beginWorkspaceWindowDrag?(request: WorkspaceWindowDragRequest): void;
  endWorkspaceWindowDrag?(workspaceId: string, windowId: string, isDetached: boolean): void;
  onTearOut?(callback: (report: TearOutReport) => void): void;
  // The popout's own bar was pressed; main follows the cursor until the release.
  beginPopoutDrag?(grab: PopoutGrab): void;
  endPopoutBarDrag?(): void;
  // The popout's window is no longer pulled out; main destroys the window.
  closePopout?(): void;
  // The reattach main asked this popout's own page for (an OS close with no
  // desktop window to take the window back) has been sent.
  popoutReattached?(): void;
  setPopoutTitle?(title: string): void;
  onPopoutReattachRequest?(callback: () => void): void;
  // A main window is asked to return a popout's window to the desktop it
  // shows: the popout was dropped onto it, or is closing.
  onReattachPopoutWindow?(callback: (ask: PopoutReattachAsk) => void): void;
  onPopoutDropTarget?(callback: (isOver: boolean) => void): void;
}

/** Why the main process could not install the staged update, or null when it is quitting into it. */
export interface InstallUpdateOutcome {
  error: string | null;
}

/** A workspace window to open in a desktop window of its own beside this one
 * (the pull-out-window spec): what main needs to open it. */
export interface PopoutOpenRequest {
  workspaceId: string;
  windowId: string;
  title: string;
  width: number;
  height: number;
}

/** A workspace title-bar drag for main to watch: the window's rendered size
 * and where inside it the pointer holds it, so the popout main opens once the
 * cursor leaves this window is sized and held the same way. */
export interface WorkspaceWindowDragRequest extends PopoutOpenRequest {
  grabX: number;
  grabY: number;
}

/** One step of a watched drag, as main reports it: the cursor left the window
 * by the tear-out distance and a popout follows it ("out"), came back inside
 * ("in"), or the button was released while out ("released"). */
export interface TearOutReport {
  workspaceId: string;
  windowId: string;
  phase: "out" | "in" | "released";
}

/** Where the pointer holds a popout's bar, in the popout's own CSS pixels. */
export interface PopoutGrab {
  grabX: number;
  grabY: number;
}

/** A frame in fractions of the workspace backdrop, as a drop back onto the
 * desktop names it. */
export interface PopoutFrame {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Main's ask of a main window to return a pulled-out window of the workspace
 * it shows to the desktop: at `frame` when the popout was dropped there, else
 * at the window's kept frame (the popout is closing). */
export interface PopoutReattachAsk {
  workspaceId: string;
  windowId: string;
  frame: PopoutFrame | null;
}

declare global {
  interface Window {
    mindsNative?: MindsNativeSurface;
  }
}

function native(): MindsNativeSurface | null {
  // Guarded for the sake of the view suites, which render components straight
  // to vnodes under node with no DOM at all. Every caller already treats null
  // as "not the desktop app", so there is nothing else to answer there.
  if (typeof window === "undefined") return null;
  return window.mindsNative ?? null;
}

export const electronBridge = {
  get isDesktop(): boolean {
    return native() !== null;
  },
  get isMacPlatform(): boolean {
    return native()?.platform === "darwin";
  },
  minimize(): void {
    native()?.minimize();
  },
  maximize(): void {
    native()?.maximize();
  },
  close(): void {
    native()?.close();
  },
  /** Restart the app's backend -- the one action that fixes a dead discovery
   * consumer. The machines themselves keep running. */
  restartApp(): void {
    native()?.retry();
  },
  async showFilePicker(options: FilePickerOptions): Promise<string | null> {
    const surface = native();
    if (surface === null) return null;
    return surface.showFilePicker(options);
  },
  async openNotificationSettings(): Promise<boolean> {
    const surface = native();
    if (surface === null) return false;
    return surface.openNotificationSettings();
  },
  bringAppToFront(): void {
    native()?.bringAppToFront();
  },
  openWorkspaceInNewWindow(agentId: string): void {
    native()?.openWorkspaceInNewWindow(agentId);
  },
  /** Null when unavailable; false when this window should handle the click. */
  openNotificationInExistingWindow(
    route: string,
    entry: UiNotificationEntry,
  ): Promise<boolean> | null {
    return native()?.openNotificationInExistingWindow?.(route, entry) ?? null;
  },
  onOpenNotification(callback: (entry: UiNotificationEntry) => void): void {
    native()?.onOpenNotification?.(callback);
  },
  onNavigate(callback: (url: string) => void): void {
    native()?.onNavigate(callback);
  },
  onOpenOverlay(callback: (cmd: unknown) => void): void {
    native()?.onOpenOverlay(callback);
  },
  onCloseActiveTab(callback: () => void): void {
    native()?.onCloseActiveTab(callback);
  },
  onEscapePressed(callback: () => void): void {
    native()?.onEscapePressed(callback);
  },
  onWindowFocusChanged(callback: (isFocused: boolean) => void): void {
    native()?.onWindowFocusChanged?.(callback);
  },
  onToast(callback: (toast: { title: string; body: string }) => void): void {
    native()?.onToast?.(callback);
  },
  sendShellEvent(event: { type: string } & Record<string, unknown>): void {
    native()?.sendShellEvent?.(event);
  },

  /** The stored display zoom percent; null in the browser, and on a desktop
   * build older than the setting. */
  async getDisplayZoom(): Promise<number | null> {
    return (await native()?.getDisplayZoom?.()) ?? null;
  },
  /** Store and apply a display zoom percent; resolves the stored value, or
   * null where the shell cannot scale windows. */
  async setDisplayZoom(percent: number): Promise<number | null> {
    return (await native()?.setDisplayZoom?.(percent)) ?? null;
  },

  /** Null in the browser, and on a desktop build older than release channels. */
  async getUpdateState(): Promise<UpdateState | null> {
    return (await native()?.getUpdateState?.()) ?? null;
  },
  async peekUpdateChannels(): Promise<Record<string, PeekedChannel>> {
    return (await native()?.peekUpdateChannels?.()) ?? {};
  },
  async setUpdateChannel(channel: UpdateChannel): Promise<UpdateState | null> {
    return (await native()?.setUpdateChannel?.(channel)) ?? null;
  },
  async checkForUpdates(): Promise<UpdateState | null> {
    return (await native()?.checkForUpdates?.()) ?? null;
  },
  /**
   * Rejects with the main process's own sentence when the install did not go
   * through. Resolves only when the app is staying up after a successful
   * install -- the quit was cancelled at the running-workspaces prompt; when
   * the quit goes ahead the app exits and the call never settles.
   */
  async installUpdate(): Promise<void> {
    const outcome = await native()?.installUpdate?.();
    if (outcome && outcome.error !== null) throw new Error(outcome.error);
  },
  onUpdateStatus(callback: (status: UpdateStatus) => void): void {
    native()?.onUpdateStatus?.(callback);
  },

  /** Whether this chrome can pull a workspace window out into its own desktop
   * window: only the desktop app, on a preload that knows how. */
  get canPopOut(): boolean {
    return native()?.openPopoutWindow !== undefined;
  },
  openPopoutWindow(request: PopoutOpenRequest): void {
    native()?.openPopoutWindow?.(request);
  },
  beginWorkspaceWindowDrag(request: WorkspaceWindowDragRequest): void {
    native()?.beginWorkspaceWindowDrag?.(request);
  },
  endWorkspaceWindowDrag(workspaceId: string, windowId: string, isDetached: boolean): void {
    native()?.endWorkspaceWindowDrag?.(workspaceId, windowId, isDetached);
  },
  onTearOut(callback: (report: TearOutReport) => void): void {
    native()?.onTearOut?.(callback);
  },
  beginPopoutDrag(grab: PopoutGrab): void {
    native()?.beginPopoutDrag?.(grab);
  },
  endPopoutBarDrag(): void {
    native()?.endPopoutBarDrag?.();
  },
  closePopout(): void {
    native()?.closePopout?.();
  },
  popoutReattached(): void {
    native()?.popoutReattached?.();
  },
  setPopoutTitle(title: string): void {
    native()?.setPopoutTitle?.(title);
  },
  onPopoutReattachRequest(callback: () => void): void {
    native()?.onPopoutReattachRequest?.(callback);
  },
  onReattachPopoutWindow(callback: (ask: PopoutReattachAsk) => void): void {
    native()?.onReattachPopoutWindow?.(callback);
  },
  onPopoutDropTarget(callback: (isOver: boolean) => void): void {
    native()?.onPopoutDropTarget?.(callback);
  },
};
