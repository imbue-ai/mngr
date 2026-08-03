// Providers-panel state (Settings page's provider rows).

import type { UiProvidersMessage } from "../channel/messages";
import type { UiProviderEntry } from "../generated/ui";

export class ProvidersStore {
  providers: readonly UiProviderEntry[] = [];
  lastEventAt: string | null = null;
  lastFullSnapshotAt: string | null = null;

  applyProvidersMessage(message: UiProvidersMessage): void {
    this.providers = message.providers;
    this.lastEventAt = message.last_event_at;
    this.lastFullSnapshotAt = message.last_full_snapshot_at;
  }
}
