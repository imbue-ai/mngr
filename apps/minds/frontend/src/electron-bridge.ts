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

interface MindsNativeSurface {
  platform: string;
  minimize(): void;
  maximize(): void;
  close(): void;
  showFilePicker(options: FilePickerOptions): Promise<string | null>;
  bringAppToFront(): void;
  openWorkspaceInNewWindow(agentId: string): void;
  onNavigate(callback: (url: string) => void): void;
  onOpenOverlay(callback: (cmd: unknown) => void): void;
  onCloseActiveTab(callback: () => void): void;
  onEscapePressed(callback: () => void): void;
  // Renderer -> main shell-event relay (workspace_stopped, focus requests).
  // Added alongside the SPA shell; older preloads lack it, hence optional.
  sendShellEvent?(event: { type: string } & Record<string, unknown>): void;
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
  async showFilePicker(options: FilePickerOptions): Promise<string | null> {
    const surface = native();
    if (surface === null) return null;
    return surface.showFilePicker(options);
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
};
