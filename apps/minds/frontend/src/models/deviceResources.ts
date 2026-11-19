/**
 * Resource warnings for the computer Imbue Studio runs on, as the backend reports them: the user's warning levels,
 * this computer's disk, memory and CPU readings, and the warnings raised. The twin of the workspace shell's resource
 * gauge, with the same words and the same rules.
 *
 * The backend takes the readings and judges the warnings; it pushes them over the channel (`device_resources`, on
 * connect, when the warnings or the levels change, and once a minute regardless), so the titlebar never polls. The
 * settings section, which shows the readings, fetches the view every few seconds while it is open.
 */

import type {
  DeviceReadings,
  GaugeResource,
  ResourceAlert,
  UiDeviceResourcesMessage,
} from "../generated/ui";

export type { GaugeResource, ResourceAlert };

export interface DeviceResourceSettings {
  readonly diskWarningFreeGb: number | null;
  readonly memoryWarningPercent: number | null;
  readonly cpuWarningPercent: number | null;
}

export interface DeviceResourceView {
  readonly settings: DeviceResourceSettings;
  readonly readings: DeviceReadings;
  /** The raised warnings, the most severe first. */
  readonly alerts: readonly ResourceAlert[];
}

export const MIN_WARNING_PERCENT = 50;
export const MAX_WARNING_PERCENT = 99;
export const MIN_DISK_WARNING_FREE_GB = 0.5;
export const MAX_DISK_WARNING_FREE_GB = 500;
export const DEFAULT_DISK_WARNING_FREE_GB = 10;
/** The backend pushes at least once a minute; a view silent for this long is no longer being updated. */
export const STALE_AFTER_MS = 150_000;
/** How often the open settings section fetches the readings. */
export const LIVE_REFRESH_INTERVAL_MS = 5000;

const VIEW_URL = "/ui/api/device-resources";
const SETTINGS_URL = "/ui/api/device-resources/settings";

type WireView = NonNullable<UiDeviceResourcesMessage["view"]>;

/** The view as the backend sends it. */
export function viewFromWire(wire: WireView): DeviceResourceView {
  return {
    settings: {
      diskWarningFreeGb: wire.settings.disk_warning_free_gb ?? null,
      memoryWarningPercent: wire.settings.memory_warning_percent ?? null,
      cpuWarningPercent: wire.settings.cpu_warning_percent ?? null,
    },
    readings: wire.readings,
    alerts: wire.alerts,
  };
}

export function settingsToWire(settings: DeviceResourceSettings): Record<string, unknown> {
  return {
    disk_warning_free_gb: settings.diskWarningFreeGb,
    memory_warning_percent: settings.memoryWarningPercent,
    cpu_warning_percent: settings.cpuWarningPercent,
  };
}

const GIGABYTE = 1024 ** 3;

export function formatGigabytes(bytes: number): string {
  const gigabytes = bytes / GIGABYTE;
  return `${gigabytes >= 10 ? gigabytes.toFixed(0) : gigabytes.toFixed(1)} GB`;
}

/** A resource's current use, in the words its settings row shows above its level: "593 GB of 926 GB disk". */
export function describeUsage(readings: DeviceReadings, resource: GaugeResource): string {
  switch (resource) {
    case "disk":
      return readings.disk === null
        ? "Disk use unknown"
        : `${formatGigabytes(readings.disk.used_bytes)} of ${formatGigabytes(readings.disk.total_bytes)} disk`;
    case "memory":
      return readings.memory === null
        ? "RAM use unknown"
        : `${formatGigabytes(readings.memory.used_bytes)} of ${formatGigabytes(readings.memory.total_bytes)} RAM`;
    case "cpu":
      return readings.cpu === null ? "CPU use unknown" : `${readings.cpu.percent.toFixed(0)}% of CPU`;
  }
}

export function alertOf(view: DeviceResourceView, resource: GaugeResource): ResourceAlert | null {
  return view.alerts.find((alert) => alert.resource === resource) ?? null;
}

type FetchImpl = (input: string, init?: RequestInit) => Promise<Response>;

/** Why a settings save failed, in the backend's words when it gave any. */
async function failureOf(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { error?: unknown };
    if (typeof body.error === "string" && body.error !== "") return body.error;
  } catch {
    // Not JSON: fall through to the generic sentence.
  }
  return "Imbue Studio couldn't save that change. Try again. If this persists, report a bug.";
}

/**
 * The view as last pushed or fetched. Applied from the channel's frames; the open settings section refreshes it
 * every few seconds and saves the levels through it.
 */
export class DeviceResourcesStore {
  private current: DeviceResourceView | null = null;
  private receivedAtMs = 0;
  private staleTimer: ReturnType<typeof setTimeout> | null = null;
  private liveTimer: ReturnType<typeof setInterval> | null = null;

  constructor(
    private readonly fetchImpl: FetchImpl = (input, init) => fetch(input, init),
    private readonly now: () => number = () => Date.now(),
  ) {}

  /** The view; null before the first frame, or when this backend measures nothing. */
  get view(): DeviceResourceView | null {
    return this.current;
  }

  /** Whether the backend has stopped sending the view: its warnings are then not to be trusted. */
  get isStale(): boolean {
    return this.current !== null && this.now() - this.receivedAtMs > STALE_AFTER_MS;
  }

  /** Take the channel's frame. `redraw` runs again once the view has gone silent long enough to read as stale,
   *  which nothing else would repaint for. */
  applyMessage(message: Pick<UiDeviceResourcesMessage, "view">, redraw: () => void = () => {}): void {
    this.apply(message.view === null ? null : viewFromWire(message.view), redraw);
  }

  /** Fetch the view now and every few seconds from here, for the open settings section's readings. */
  startLiveRefresh(redraw: () => void): void {
    if (this.liveTimer !== null) return;
    void this.refresh(redraw);
    this.liveTimer = setInterval(() => void this.refresh(redraw), LIVE_REFRESH_INTERVAL_MS);
  }

  stopLiveRefresh(): void {
    if (this.liveTimer !== null) clearInterval(this.liveTimer);
    this.liveTimer = null;
  }

  /** Save new levels and take the backend's answer as the view; throws with the backend's reason on refusal. */
  async saveSettings(settings: DeviceResourceSettings, redraw: () => void): Promise<void> {
    const response = await this.fetchImpl(SETTINGS_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settingsToWire(settings)),
    });
    if (!response.ok) throw new Error(await failureOf(response));
    this.apply(viewFromWire((await response.json()) as WireView), redraw);
    redraw();
  }

  private apply(view: DeviceResourceView | null, redraw: () => void): void {
    this.current = view;
    this.receivedAtMs = this.now();
    if (this.staleTimer !== null) clearTimeout(this.staleTimer);
    this.staleTimer =
      view === null
        ? null
        : setTimeout(() => {
            this.staleTimer = null;
            redraw();
          }, STALE_AFTER_MS + 1000);
  }

  private async refresh(redraw: () => void): Promise<void> {
    let response: Response;
    try {
      response = await this.fetchImpl(VIEW_URL, { credentials: "same-origin" });
    } catch {
      // A missed fetch keeps the last view; the next one, or the backend's own push, replaces it.
      return;
    }
    if (!response.ok) return;
    this.apply(viewFromWire((await response.json()) as WireView), redraw);
    redraw();
  }
}
