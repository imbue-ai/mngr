import { afterEach, describe, expect, it, vi } from "vitest";
import type { PeekedChannel, UpdateChannel, UpdateState, UpdateStatus } from "../electron-bridge";
import { jsonResponse, settingsOverview, settle, withMindsNative, withReceiverGuardedGlobalFetch } from "../testing";
import {
  DEFAULT_NOTIFICATION_PREFS,
  applyNotificationPrefs,
  currentNotificationPrefs,
  resetNotificationPrefsForTests,
  type NotificationPrefs,
} from "./notificationsUi";
import {
  SettingsModel,
  addAccountBlockedReason,
  type ServicePermissionOverview,
  type SettingsOverview,
} from "./settings";

const BASE_OVERVIEW: SettingsOverview = {
  services_overview: [],
  file_sharing_grants: [],
  workspace_delegation_grants: [],
  permissions_unavailable: false,
  is_master_password_set: false,
  report_unexpected_errors: true,
  version: "v-one",
};

const BASE_PREFS: NotificationPrefs = {
  is_enabled: true,
  style: "both",
  is_os_hint_dismissed: false,
  os_permission_confirmed: false,
  version: "np-1",
};

afterEach(() => {
  resetNotificationPrefsForTests();
  vi.unstubAllGlobals();
});

describe("SettingsModel", () => {
  it("invokes the default fetch as a plain call (Illegal-invocation regression guard)", async () => {
    // Browsers reject the global fetch when it is invoked with any other
    // receiver (as `this.fetchImpl(...)` would if the default were the bare
    // global), so the default must wrap it in a plain call.
    await withReceiverGuardedGlobalFetch(settingsOverview(), async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.load();
      expect(model.isLoadFailed).toBe(false);
      expect(model.overview?.version).toBe("v-one");
    });
  });

  it("loads the overview payload and clears the failure flag", async () => {
    const model = new SettingsModel(
      async () => jsonResponse(settingsOverview()),
      () => {},
    );

    await model.load();

    expect(model.overview?.version).toBe("v-one");
    expect(model.isLoadFailed).toBe(false);
  });

  it("marks the load failed on a non-OK response", async () => {
    const model = new SettingsModel(
      async () => new Response("nope", { status: 503 }),
      () => {},
    );

    await model.load();

    expect(model.overview).toBeNull();
    expect(model.isLoadFailed).toBe(true);
  });

  it("applies the returned version after a successful error-reporting write", async () => {
    const requests: { url: string; ifMatch: string | null }[] = [];
    const model = new SettingsModel(
      async (input, init) => {
        const url = String(input);
        const headers = new Headers(init?.headers);
        requests.push({ url, ifMatch: headers.get("If-Match") });
        if (url.endsWith("/error-reporting"))
          return jsonResponse({ version: "v-two" });
        return jsonResponse(settingsOverview());
      },
      () => {},
    );
    await model.load();

    await model.setReportUnexpectedErrors(false);

    expect(model.overview?.report_unexpected_errors).toBe(false);
    expect(model.overview?.version).toBe("v-two");
    const write = requests.find((request) =>
      request.url.endsWith("/error-reporting"),
    );
    expect(write?.ifMatch).toBe("v-one");
  });

  it("surfaces a refused error-reporting write with the server's reason", async () => {
    const model = new SettingsModel(
      async (input) => {
        const url = String(input);
        if (url.endsWith("/error-reporting"))
          return jsonResponse({ error: "consent has not been recorded" }, 428);
        return jsonResponse(settingsOverview());
      },
      () => {},
    );
    await model.load();

    await model.setReportUnexpectedErrors(false);

    expect(model.errorReportingError).toBe("consent has not been recorded");
    // Nothing persisted: the model state stands.
    expect(model.overview?.report_unexpected_errors).toBe(true);
  });

  it("surfaces a network failure of the error-reporting write and clears it on success", async () => {
    let isServerUp = false;
    const model = new SettingsModel(
      async (input) => {
        const url = String(input);
        if (url.endsWith("/error-reporting")) {
          if (!isServerUp) throw new TypeError("Failed to fetch");
          return jsonResponse({ version: "v-two" });
        }
        return jsonResponse(settingsOverview());
      },
      () => {},
    );
    await model.load();

    await model.setReportUnexpectedErrors(false);
    expect(model.errorReportingError).toContain("network error");

    isServerUp = true;
    await model.setReportUnexpectedErrors(false);
    expect(model.errorReportingError).toBe("");
    expect(model.overview?.report_unexpected_errors).toBe(false);
  });

  it("rebases on a 412 conflict by reloading instead of clobbering", async () => {
    let overviewValue = settingsOverview();
    const model = new SettingsModel(
      async (input) => {
        const url = String(input);
        if (url.endsWith("/error-reporting"))
          return jsonResponse({ error: "stale" }, 412);
        return jsonResponse(overviewValue);
      },
      () => {},
    );
    await model.load();
    // Another window flipped the flag; the server now serves the newer state.
    overviewValue = settingsOverview({ report_unexpected_errors: false, version: "v-newer" });

    await model.setReportUnexpectedErrors(false);

    expect(model.overview?.version).toBe("v-newer");
    expect(model.overview?.report_unexpected_errors).toBe(false);
  });

  it("keeps the revoke dialog open with an error message when the revoke fails", async () => {
    const model = new SettingsModel(
      async (input) => {
        const url = String(input);
        if (url === "/settings/permissions/revoke")
          return new Response("boom", { status: 502 });
        return jsonResponse(settingsOverview());
      },
      () => {},
    );
    await model.load();
    model.openRevoke({
      title: "Revoke?",
      body: "b",
      confirmLabel: "Revoke",
      url: "/settings/permissions/revoke",
      payload: {},
    });

    await model.confirmRevoke();

    expect(model.pendingRevoke).not.toBeNull();
    expect(model.revokeError).toContain("502");
  });

  it("closes the dialog and reloads after a successful revoke", async () => {
    let loadCount = 0;
    const model = new SettingsModel(
      async (input) => {
        const url = String(input);
        if (url === "/settings/permissions/revoke")
          return jsonResponse({ status: "ok" });
        loadCount += 1;
        return jsonResponse(settingsOverview());
      },
      () => {},
    );
    await model.load();
    model.openRevoke({
      title: "Revoke?",
      body: "b",
      confirmLabel: "Revoke",
      url: "/settings/permissions/revoke",
      payload: {},
    });

    await model.confirmRevoke();

    expect(model.pendingRevoke).toBeNull();
    expect(loadCount).toBe(2);
  });

  it("reads notification prefs as the defaults while the backend omits the field", async () => {
    const model = new SettingsModel(
      async () => jsonResponse(BASE_OVERVIEW),
      () => {},
    );
    await model.load();
    expect(model.notificationPrefs()).toEqual(DEFAULT_NOTIFICATION_PREFS);
  });

  it("writes notification prefs with the prefs' own If-Match token and applies the new version", async () => {
    const writes: { url: string; ifMatch: string | null; body: unknown }[] = [];
    const model = new SettingsModel(
      async (input, init) => {
        const url = String(input);
        if (url.endsWith("/settings/notifications")) {
          writes.push({
            url,
            ifMatch: new Headers(init?.headers).get("If-Match"),
            body: JSON.parse(String(init?.body)),
          });
          return jsonResponse({ version: "np-2" });
        }
        return jsonResponse({
          ...BASE_OVERVIEW,
          notification_prefs: BASE_PREFS,
        });
      },
      () => {},
    );
    await model.load();

    await model.setNotificationPrefs({
      is_enabled: true,
      style: "os",
      is_os_hint_dismissed: false,
    });

    expect(writes).toEqual([
      {
        url: "/ui/api/settings/notifications",
        ifMatch: "np-1",
        body: { is_enabled: true, style: "os", is_os_hint_dismissed: false },
      },
    ]);
    expect(model.notificationPrefs()).toEqual({
      is_enabled: true,
      style: "os",
      is_os_hint_dismissed: false,
      os_permission_confirmed: false,
      version: "np-2",
    });
    // The app-wide applied prefs (which gate arrivals) follow the write.
    expect(currentNotificationPrefs().style).toBe("os");
  });

  it("rebases notification prefs on a 412 conflict by reloading instead of clobbering", async () => {
    let served: NotificationPrefs = BASE_PREFS;
    const model = new SettingsModel(
      async (input) => {
        const url = String(input);
        if (url.endsWith("/settings/notifications"))
          return jsonResponse({ error: "stale" }, 412);
        return jsonResponse({ ...BASE_OVERVIEW, notification_prefs: served });
      },
      () => {},
    );
    await model.load();
    // Another window changed the prefs; the server now serves the newer state.
    served = {
      is_enabled: false,
      style: "cards",
      is_os_hint_dismissed: true,
      os_permission_confirmed: false,
      version: "np-9",
    };

    await model.setNotificationPrefs({
      is_enabled: true,
      style: "both",
      is_os_hint_dismissed: false,
    });

    expect(model.notificationPrefs()).toEqual(served);
    expect(currentNotificationPrefs()).toEqual(served);
  });

  it("surfaces a network failure of the notification-prefs write and clears it on success", async () => {
    let isServerUp = false;
    const model = new SettingsModel(
      async (input) => {
        const url = String(input);
        if (url.endsWith("/settings/notifications")) {
          if (!isServerUp) throw new TypeError("Failed to fetch");
          return jsonResponse({ version: "np-2" });
        }
        return jsonResponse({
          ...BASE_OVERVIEW,
          notification_prefs: BASE_PREFS,
        });
      },
      () => {},
    );
    await model.load();

    await model.setNotificationPrefs({
      is_enabled: false,
      style: "both",
      is_os_hint_dismissed: false,
    });
    expect(model.notificationPrefsError).toContain("network error");

    isServerUp = true;
    await model.setNotificationPrefs({
      is_enabled: false,
      style: "both",
      is_os_hint_dismissed: false,
    });
    expect(model.notificationPrefsError).toBe("");
    expect(model.notificationPrefs().is_enabled).toBe(false);
  });

  it("surfaces a refused notification-prefs write and leaves the model standing", async () => {
    const model = new SettingsModel(
      async (input) => {
        const url = String(input);
        if (url.endsWith("/settings/notifications"))
          return jsonResponse(
            { error: "notifications are not available yet" },
            404,
          );
        return jsonResponse({
          ...BASE_OVERVIEW,
          notification_prefs: BASE_PREFS,
        });
      },
      () => {},
    );
    await model.load();

    await model.setNotificationPrefs({
      is_enabled: false,
      style: "both",
      is_os_hint_dismissed: false,
    });

    expect(model.notificationPrefsError).toBe(
      "notifications are not available yet",
    );
    expect(model.notificationPrefs().is_enabled).toBe(true);
  });

  it("syncs the notification-prefs copy from the applied-prefs cell with no network request", async () => {
    // Regression guard: this used to be a full model.load(), which could
    // flip isLoadFailed (blanking the whole Settings modal) over a transient
    // failure in what is only a best-effort background refresh.
    const model = new SettingsModel(
      async (input) => {
        if (String(input).endsWith("/settings/notifications")) {
          throw new Error("must not write during a local sync");
        }
        return jsonResponse({
          ...BASE_OVERVIEW,
          notification_prefs: BASE_PREFS,
        });
      },
      () => {},
    );
    await model.load();

    applyNotificationPrefs({
      is_enabled: true,
      style: "cards",
      is_os_hint_dismissed: false,
      os_permission_confirmed: false,
      version: "np-9",
    });
    model.syncNotificationPrefsFromApplied();

    expect(model.notificationPrefs()).toEqual({
      is_enabled: true,
      style: "cards",
      is_os_hint_dismissed: false,
      os_permission_confirmed: false,
      version: "np-9",
    });
    expect(model.isLoadFailed).toBe(false);
  });

  it("does nothing before the overview has loaded", () => {
    const model = new SettingsModel(
      async () => jsonResponse(BASE_OVERVIEW),
      () => {},
    );
    model.syncNotificationPrefsFromApplied();
    expect(model.overview).toBeNull();
  });

  it("clears the open-failed flag on a successful OS-settings open", async () => {
    vi.stubGlobal("window", {
      mindsNative: {
        platform: "darwin",
        openNotificationSettings: vi.fn(async () => true),
      },
    });
    const model = new SettingsModel(
      async () => jsonResponse(BASE_OVERVIEW),
      () => {},
    );
    model.notificationOsSettingsOpenFailed = true;

    await model.openNotificationOsSettings();

    expect(model.notificationOsSettingsOpenFailed).toBe(false);
  });

  it("records the failure when the OS settings pane does not open (e.g. an unsupported Linux desktop environment)", async () => {
    vi.stubGlobal("window", {
      mindsNative: {
        platform: "linux",
        openNotificationSettings: vi.fn(async () => false),
      },
    });
    const model = new SettingsModel(
      async () => jsonResponse(BASE_OVERVIEW),
      () => {},
    );

    await model.openNotificationOsSettings();

    expect(model.notificationOsSettingsOpenFailed).toBe(true);
  });

  it("surfaces a mismatch error without posting when master passwords differ", async () => {
    let postCount = 0;
    const model = new SettingsModel(
      async () => {
        postCount += 1;
        return jsonResponse({});
      },
      () => {},
    );

    await model.changeMasterPassword("aaa", "bbb");

    expect(model.masterPasswordError).toContain("do not match");
    expect(postCount).toBe(0);
  });
});

function updateStateAt(channel: UpdateChannel, currentVersion: string): UpdateState {
  return {
    channel,
    currentVersion,
    available: ["stable", "beta", "alpha"],
    status: { type: "up-to-date" },
  };
}

/** Stands in for main's `webContents.send('update-status', ...)`.
 *
 * The bridge registers its callback once at module scope, so every stub records
 * it here and any case can push a status without depending on which earlier one
 * happened to trigger that single registration. */
let pushUpdateStatus: ((status: UpdateStatus) => void) | null = null;

/** A stub main process on `channel` at `currentVersion`, serving `peeked`. */
function nativeStub(
  state: UpdateState,
  peeked: Record<string, PeekedChannel>,
): { surface: Record<string, unknown>; switches: UpdateChannel[] } {
  const switches: UpdateChannel[] = [];
  return {
    switches,
    surface: {
      getUpdateState: async () => state,
      peekUpdateChannels: async () => peeked,
      setUpdateChannel: async (channel: UpdateChannel) => {
        switches.push(channel);
        return { ...state, channel };
      },
      checkForUpdates: async () => state,
      onUpdateStatus: (callback: (status: UpdateStatus) => void) => {
        pushUpdateStatus = callback;
      },
    },
  };
}

describe("SettingsModel release channels", () => {
  // Running 0.4.30 on alpha: stable is far behind (switching parks), beta has
  // caught up (switching does not), and nobody has promoted anything to a
  // channel the app offers but no manifest exists for.
  const RUNNING = updateStateAt("alpha", "0.4.30");
  const PEEKED: Record<string, PeekedChannel> = {
    stable: { version: "0.4.12", wouldPark: true },
    beta: { version: "0.4.30", wouldPark: false },
    alpha: { version: "0.4.30", wouldPark: false },
  };

  it("confirms before a switch that would park, and writes nothing until then", async () => {
    const { surface, switches } = nativeStub(RUNNING, PEEKED);
    await withMindsNative(surface, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();

      await model.requestChannel("stable");

      expect(model.pendingChannelSwitch).toEqual({ channel: "stable", targetVersion: "0.4.12" });
      expect(switches).toEqual([]);
      expect(model.updateState?.channel).toBe("alpha");
    });
  });

  it("applies the parking switch once it is confirmed", async () => {
    const { surface, switches } = nativeStub(RUNNING, PEEKED);
    await withMindsNative(surface, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();
      await model.requestChannel("stable");

      await model.confirmChannelSwitch();

      expect(switches).toEqual(["stable"]);
      expect(model.updateState?.channel).toBe("stable");
      expect(model.pendingChannelSwitch).toBeNull();
    });
  });

  it("leaves the channel alone when the parking switch is cancelled", async () => {
    const { surface, switches } = nativeStub(RUNNING, PEEKED);
    await withMindsNative(surface, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();
      await model.requestChannel("stable");

      model.cancelChannelSwitch();

      expect(model.pendingChannelSwitch).toBeNull();
      expect(switches).toEqual([]);
      expect(model.updateState?.channel).toBe("alpha");
    });
  });

  it("switches straight away to a channel that has caught up", async () => {
    const { surface, switches } = nativeStub(RUNNING, PEEKED);
    await withMindsNative(surface, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();

      await model.requestChannel("beta");

      expect(model.pendingChannelSwitch).toBeNull();
      expect(switches).toEqual(["beta"]);
      expect(model.updateState?.channel).toBe("beta");
    });
  });

  it("refuses a channel whose manifest could not be read, and says so", async () => {
    // No version means no comparison, so wouldPark is false -- taking that as
    // "safe to switch" would write the preference and leave every later check
    // failing against a feed that serves nothing.
    const unpublished = { ...PEEKED, beta: { version: null, wouldPark: false, error: "404" } };
    const { surface, switches } = nativeStub(RUNNING, unpublished);
    await withMindsNative(surface, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();

      await model.requestChannel("beta");

      expect(switches).toEqual([]);
      expect(model.updateState?.channel).toBe("alpha");
      expect(model.updateError).toContain("beta channel is unavailable");
    });
  });

  it("applies a status pushed by the main process to the panel that loaded last", async () => {
    // The preload callback has no unregister, so it is registered once and
    // forwards to whichever model loaded last -- SettingsPage builds a new one
    // on every visit. Registering per visit would fan one status out to every
    // model the session ever built.
    const { surface } = nativeStub(RUNNING, PEEKED);
    await withMindsNative(surface, async () => {
      const firstVisit = new SettingsModel(undefined, () => {});
      await firstVisit.loadUpdateState();
      const secondVisit = new SettingsModel(undefined, () => {});
      await secondVisit.loadUpdateState();
      expect(pushUpdateStatus).not.toBeNull();

      const parked: UpdateStatus = {
        type: "parked",
        channel: "stable",
        currentVersion: "0.4.30",
        feedVersion: "0.4.12",
      };
      pushUpdateStatus?.(parked);

      expect(secondVisit.updateState?.status).toEqual(parked);
      // The rest of the state survives the merge.
      expect(secondVisit.updateState?.currentVersion).toBe("0.4.30");
      expect(secondVisit.updateState?.available).toEqual(["stable", "beta", "alpha"]);
      expect(firstVisit.updateState?.status.type).toBe("up-to-date");
    });
  });

  it("takes the channel a pushed status names, so a second window is not left behind", async () => {
    // Main checks whatever the stored channel says, so a pushed status names the
    // app's channel. Only the window that called setUpdateChannel learns of a
    // switch from its return value; another window with this panel open would
    // keep its old radio checked -- and clicking an already-checked radio fires
    // no onchange, so it could not be corrected from there either.
    const { surface } = nativeStub(RUNNING, PEEKED);
    await withMindsNative(surface, async () => {
      const otherWindow = new SettingsModel(undefined, () => {});
      await otherWindow.loadUpdateState();
      expect(otherWindow.updateState?.channel).toBe("alpha");

      pushUpdateStatus?.({ type: "up-to-date", channel: "stable", currentVersion: "0.4.30" });

      expect(otherWindow.updateState?.channel).toBe("stable");
    });
  });

  it("keeps the channel when a status carries none", async () => {
    // `disabled`, and a check that rejected before it resolved one, have none.
    const { surface } = nativeStub(RUNNING, PEEKED);
    await withMindsNative(surface, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();

      pushUpdateStatus?.({ type: "error", message: "MINDS_ROOT_NAME is unreadable" });

      expect(model.updateState?.channel).toBe("alpha");
      expect(model.updateState?.status.type).toBe("error");
    });
  });

  it("keeps the last check time through a status that is not a check", async () => {
    // `checking` and `disabled` carry no time. Letting them overwrite it would
    // blank the line for the duration of every check, which is exactly when the
    // user is looking at it.
    const { surface } = nativeStub({ ...RUNNING, lastCheckedAt: "2026-08-13T23:53:00.000Z" }, PEEKED);
    await withMindsNative(surface, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();

      model.receiveUpdateStatus({ type: "checking", channel: "alpha" });

      expect(model.updateState?.lastCheckedAt).toBe("2026-08-13T23:53:00.000Z");
    });
  });

  it("reports a failed update-state read rather than reading as a check still running", async () => {
    // describe() reads the data dir and the bundled client.toml, so it can
    // reject. Left unhandled, the panel shows its `updateState === null` copy --
    // "Reading the update state..." -- for good, with no version, no channel,
    // and no reason.
    const { surface } = nativeStub(RUNNING, PEEKED);
    const broken = {
      ...surface,
      getUpdateState: async () => {
        throw new Error("MINDS_ROOT_NAME is unreadable");
      },
    };
    await withMindsNative(broken, async () => {
      const model = new SettingsModel(undefined, () => {});

      await model.loadUpdateState();

      expect(model.updateState).toBeNull();
      expect(model.updateError).toContain("MINDS_ROOT_NAME is unreadable");
    });
  });

  it("re-peeks on an on-demand check, so a channel published since the load stops reading unavailable", async () => {
    let peeked: Record<string, PeekedChannel> = { ...PEEKED, beta: { version: null, wouldPark: false, error: "404" } };
    const afterCheck: UpdateState = {
      ...RUNNING,
      status: { type: "update-available", channel: "alpha", feedVersion: "0.4.31" },
    };
    const surface = {
      getUpdateState: async () => RUNNING,
      peekUpdateChannels: async () => peeked,
      setUpdateChannel: async () => RUNNING,
      checkForUpdates: async () => {
        peeked = PEEKED;
        return afterCheck;
      },
      onUpdateStatus: (callback: (status: UpdateStatus) => void) => {
        pushUpdateStatus = callback;
      },
    };
    await withMindsNative(surface, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();
      expect(model.peekedChannels["beta"].version).toBeNull();

      await model.checkForUpdatesNow();
      await settle();

      expect(model.updateState?.status.type).toBe("update-available");
      expect(model.peekedChannels["beta"].version).toBe("0.4.30");
      expect(model.isUpdateBusy).toBe(false);
      expect(model.updateError).toBe("");
    });
  });

  it("frees the button as soon as the check answers, not when the peek does", async () => {
    // The main process runs one updater task at a time, so a peek issued after
    // a check that found an update waits out the download that check began --
    // minutes, for a build this size. Held under the busy flag, that turns a
    // finished check into a panel reading "Checking..." for the whole transfer.
    let releasePeek = (): void => {};
    const pending = new Promise<Record<string, PeekedChannel>>((resolve) => {
      releasePeek = () => resolve(PEEKED);
    });
    const surface = {
      getUpdateState: async () => RUNNING,
      peekUpdateChannels: () => pending,
      setUpdateChannel: async () => RUNNING,
      checkForUpdates: async () => ({
        ...RUNNING,
        status: { type: "update-available" as const, channel: "alpha" as const, feedVersion: "0.4.31" },
      }),
      onUpdateStatus: (callback: (status: UpdateStatus) => void) => {
        pushUpdateStatus = callback;
      },
    };
    await withMindsNative(surface, async () => {
      const model = new SettingsModel(undefined, () => {});
      model.updateState = RUNNING;

      await model.checkForUpdatesNow();
      await settle();

      // The peek has not resolved and will not until released.
      expect(model.isUpdateBusy).toBe(false);
      expect(model.updateState?.status.type).toBe("update-available");

      releasePeek();
      await settle();
      expect(model.peekedChannels["alpha"].version).toBe("0.4.30");
    });
  });

  it("surfaces a failed on-demand check and leaves the button live", async () => {
    const { surface } = nativeStub(RUNNING, PEEKED);
    const failing = {
      ...surface,
      checkForUpdates: async () => {
        throw new Error("Cannot find channel alpha-mac.yml");
      },
    };
    await withMindsNative(failing, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();

      await model.checkForUpdatesNow();

      expect(model.updateError).toContain("Cannot find channel alpha-mac.yml");
      // Left busy, the panel would read as a check that never returns.
      expect(model.isUpdateBusy).toBe(false);
      expect(model.updateState?.channel).toBe("alpha");
    });
  });

  it("hides the Updates section in the browser build and shows it on desktop", async () => {
    await withMindsNative(null, async () => {
      const names = new SettingsModel(undefined, () => {}).visibleSections.map((section) => section.name);
      expect(names).not.toContain("updates");
      expect(names).toContain("error-reporting");
    });
    await withMindsNative(nativeStub(RUNNING, PEEKED).surface, async () => {
      expect(new SettingsModel(undefined, () => {}).visibleSections.map((s) => s.name)).toContain("updates");
    });
  });
});

describe("addAccountBlockedReason", () => {
  const SERVICE: ServicePermissionOverview = {
    service_name: "aws",
    display_name: "AWS",
    accounts: [],
    is_browser_sign_in_supported: false,
  };

  it("blocks the action, with a reason, for a service that has no browser sign-in", () => {
    expect(addAccountBlockedReason(SERVICE)).toBe(
      "AWS does not support signing in through a browser.",
    );
  });

  it("allows the action for a service that signs in through a browser", () => {
    expect(
      addAccountBlockedReason({
        ...SERVICE,
        is_browser_sign_in_supported: true,
      }),
    ).toBeNull();
  });
});
