// Typed, optional facade over the Electron preload surface (`mindsNative`).
//
// Feature-detected: in plain-browser mode every call is a no-op / null, so
// shell code never branches on "am I in Electron" beyond what this module
// exposes. Only genuinely native affordances live here -- window controls,
// the native file picker, focus, multi-window -- everything else the legacy
// `window.minds` bridge carried is now owned by the SPA itself.

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
  type: "idle" | "checking" | "up-to-date" | "parked" | "update-available" | "update-downloaded" | "error" | "disabled";
  channel?: UpdateChannel;
  currentVersion?: string;
  /** What the channel serves. Below currentVersion exactly when parked. */
  feedVersion?: string | null;
  version?: string;
  message?: string;
  reason?: string;
  /** ISO-8601, carried on every status a settled check publishes. */
  lastCheckedAt?: string | null;
}

export interface PeekedChannel {
  /** What this channel serves right now; null when it is unreachable. */
  version: string | null;
  /** Whether moving here would stop updates until the channel catches up. */
  wouldPark: boolean;
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
}

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
  onNavigate(callback: (url: string) => void): void;
  onOpenOverlay(callback: (cmd: unknown) => void): void;
  onCloseActiveTab(callback: () => void): void;
  onEscapePressed(callback: () => void): void;
  // Renderer -> main shell-event relay (workspace_stopped, focus requests).
  // Added alongside the SPA shell; older preloads lack it, hence optional.
  sendShellEvent?(event: { type: string } & Record<string, unknown>): void;
  // Release channels. Optional: a preload from before channels shipped lacks
  // them, and the browser build has no binary to update at all.
  getUpdateState?(): Promise<UpdateState>;
  peekUpdateChannels?(): Promise<Record<string, PeekedChannel>>;
  setUpdateChannel?(channel: UpdateChannel): Promise<UpdateState>;
  checkForUpdates?(): Promise<UpdateState>;
  installUpdate?(): Promise<void>;
  onUpdateStatus?(callback: (status: UpdateStatus) => void): void;
}

declare global {
  interface Window {
    mindsNative?: MindsNativeSurface;
  }
}

function native(): MindsNativeSurface | null {
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
  sendShellEvent(event: { type: string } & Record<string, unknown>): void {
    native()?.sendShellEvent?.(event);
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
  async installUpdate(): Promise<void> {
    await native()?.installUpdate?.();
  },
  onUpdateStatus(callback: (status: UpdateStatus) => void): void {
    native()?.onUpdateStatus?.(callback);
  },
};
