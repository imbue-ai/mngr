// Boot: read the inlined bootstrap document and seed every store before the
// first render, so first paint shows real state with zero round trips (and
// no neutral->accent pop-in). The channel's connect-time snapshot then keeps
// the same stores current.

import type { UiBootstrap } from "../channel/messages";
import { AccountsStore } from "./accounts";
import { HealthStore } from "./health";
import { NotificationsStore } from "./notifications";
import { ProvidersStore } from "./providers";
import { RequestsStore } from "./requests";
import { UpdatesStore } from "./updates";
import { WorkspacesStore } from "./workspaces";

export interface AppStores {
  workspaces: WorkspacesStore;
  health: HealthStore;
  requests: RequestsStore;
  notifications: NotificationsStore;
  accounts: AccountsStore;
  providers: ProvidersStore;
  updates: UpdatesStore;
}

export interface BootContext {
  stores: AppStores;
  seed: { accent: string; isMac: boolean; mngrForwardOrigin: string };
  schemaVersion: number;
}

declare global {
  interface Window {
    __MINDS_BOOTSTRAP__?: UiBootstrap;
  }
}

export function createEmptyStores(): AppStores {
  return {
    workspaces: new WorkspacesStore(),
    health: new HealthStore(),
    requests: new RequestsStore(),
    notifications: new NotificationsStore(),
    accounts: new AccountsStore(),
    providers: new ProvidersStore(),
    updates: new UpdatesStore(),
  };
}

export function bootFromBootstrap(bootstrap: UiBootstrap): BootContext {
  const stores = createEmptyStores();
  applySnapshotToStores(stores, bootstrap);
  return {
    stores,
    seed: {
      accent: bootstrap.seed.accent,
      isMac: bootstrap.seed.is_mac,
      mngrForwardOrigin: bootstrap.seed.mngr_forward_origin,
    },
    schemaVersion: bootstrap.schema_version,
  };
}

export function applySnapshotToStores(stores: AppStores, bootstrap: Pick<UiBootstrap, "snapshot">): void {
  const snapshot = bootstrap.snapshot;
  stores.workspaces.applyWorkspacesMessage(snapshot.workspaces);
  stores.accounts.applyAccountsMessage(snapshot.accounts);
  stores.providers.applyProvidersMessage(snapshot.providers);
  stores.requests.applyRequestsMessage(snapshot.requests);
  stores.notifications.applyNotificationsMessage(snapshot.notifications);
  stores.health.applyDiscoveryHealthMessage(snapshot.discovery_health);
  stores.health.applyEnvironmentMessage(snapshot.environment);
  for (const health of snapshot.health) stores.health.applyHealthMessage(health);
  stores.updates.applyUpdatesMessage(snapshot.workspace_updates);
}
