import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  PeekedChannel,
  UpdateChannel,
  UpdateState,
  UpdateStatus,
} from "../electron-bridge";
import {
  jsonResponse,
  pendingInstallUpdate,
  settingsOverview,
  settle,
  withMindsNative,
  withReceiverGuardedGlobalFetch,
} from "../testing";
import {
  DEFAULT_NOTIFICATION_PREFS,
  currentNotificationPrefs,
  resetNotificationPrefsForTests,
  type NotificationPrefs,
} from "./notificationsUi";
import { SettingsModel, type SettingsOverview } from "./settings";

const BASE_OVERVIEW: SettingsOverview = settingsOverview();

const BASE_PREFS: NotificationPrefs = {
  is_enabled: true,
  style: "both",
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

  it("marks the load failed on a non-OK response, and asks for a restart only when the login lapsed", async () => {
    let status = 503;
    const model = new SettingsModel(
      async () => new Response("nope", { status }),
      () => {},
    );

    await model.load();

    expect(model.overview).toBeNull();
    expect(model.isLoadFailed).toBe(true);
    expect(model.loadFailure?.isReconnectNeeded).toBe(false);
    expect(model.loadFailure?.error.message).toBe(
      "Couldn't load your settings. Try again. If this persists, report a bug.",
    );

    status = 401;
    await model.load();
    expect(model.loadFailure?.isReconnectNeeded).toBe(true);
    expect(model.loadFailure?.error.message).toBe(
      "Imbue Studio needs to restart to reconnect this window.",
    );
  });

  it("keeps an account's reason behind the master password results and names the route's own refusals plainly", async () => {
    const answers = [
      jsonResponse({
        ok: false,
        results: [
          { account: "a@b.com", is_ok: true, error: null },
          { account: "c@d.com", is_ok: false, error: "locked on this device" },
        ],
      }),
      jsonResponse(
        {
          error:
            "Couldn't check which accounts are signed in. Try again in a moment.",
          reason: "ACCOUNTS_UNREADABLE",
        },
        503,
      ),
      jsonResponse({ error: "Sync is unavailable in this configuration" }, 503),
      jsonResponse({ error: "Not authenticated" }, 401),
    ];
    const model = new SettingsModel(
      async () => answers.shift() as Response,
      () => {},
    );

    await model.changeMasterPassword("p", "p");
    expect(model.isMasterPasswordAllOk).toBe(false);
    expect(model.masterPasswordResults?.[1]?.error).toBe(
      "locked on this device",
    );

    await model.changeMasterPassword("p", "p");
    expect(model.masterPasswordRefusal).toBe(
      "Couldn't check which accounts are signed in. Try again in a moment.",
    );
    expect(model.masterPasswordFailure).toBeNull();

    await model.changeMasterPassword("p", "p");
    expect(model.masterPasswordRefusal).toBe("");
    expect(model.masterPasswordFailure?.error.message).toBe(
      "Couldn't change the master password. Try again. If this persists, report a bug.",
    );

    // A lapsed login is restarted, not reported.
    await model.changeMasterPassword("p", "p");
    expect(model.masterPasswordFailure?.isReconnectNeeded).toBe(true);
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

  it("writes the sign-in browser and shows it, and keeps the old one when the write is refused", async () => {
    const bodies: unknown[] = [];
    let isRefusing = false;
    const model = new SettingsModel(
      async (input, init) => {
        const url = String(input);
        if (url.endsWith("/sign-in-browser")) {
          bodies.push(JSON.parse(String(init?.body)));
          return isRefusing
            ? jsonResponse(
                {
                  error: "That browser isn't installed any more. Pick another.",
                  reason: "BROWSER_NOT_INSTALLED",
                },
                400,
              )
            : jsonResponse({});
        }
        return jsonResponse(settingsOverview());
      },
      () => {},
    );
    await model.load();

    await model.setSignInBrowser("/Applications/Firefox.app");
    expect(model.overview?.sign_in_browser_id).toBe(
      "/Applications/Firefox.app",
    );

    isRefusing = true;
    await model.setSignInBrowser("/Applications/Gone.app");
    expect(model.overview?.sign_in_browser_id).toBe(
      "/Applications/Firefox.app",
    );
    // The route's own sentence explains itself: no report asked for.
    expect(model.signInBrowserFailure?.isExplained).toBe(true);
    expect(model.signInBrowserFailure?.error.message).toBe(
      "That browser isn't installed any more. Pick another.",
    );
    expect(bodies).toEqual([
      { browser_id: "/Applications/Firefox.app" },
      { browser_id: "/Applications/Gone.app" },
    ]);
    // Picking it again would be refused again, so the refusal offers no retry.
    expect(model.signInBrowserFailure?.retry).toBeUndefined();
  });

  it("offers a failed sign-in browser write a Try again that sends the same pick", async () => {
    const bodies: unknown[] = [];
    let isFailing = true;
    const model = new SettingsModel(
      async (input, init) => {
        const url = String(input);
        if (url.endsWith("/sign-in-browser")) {
          bodies.push(JSON.parse(String(init?.body)));
          return isFailing ? jsonResponse({ error: "Could not save the sign-in browser: disk full" }, 500) : jsonResponse({});
        }
        return jsonResponse(settingsOverview());
      },
      () => {},
    );
    await model.load();

    await model.setSignInBrowser("/Applications/Firefox.app");
    expect(model.signInBrowserFailure?.error.message).toBe(
      "Couldn't save this change. Try again. If this persists, report a bug.",
    );
    const retry = model.signInBrowserFailure?.retry;
    if (retry === undefined) throw new Error("no Try again on the failure");

    isFailing = false;
    retry();
    await vi.waitFor(() => expect(model.signInBrowserFailure).toBeNull());

    expect(model.overview?.sign_in_browser_id).toBe("/Applications/Firefox.app");
    expect(bodies).toEqual([
      { browser_id: "/Applications/Firefox.app" },
      { browser_id: "/Applications/Firefox.app" },
    ]);
  });

  it("says why a settings record a newer app saved can't be changed, with no retry or report", async () => {
    const sentence =
      "These settings were saved by a newer version of Imbue Studio and can only be changed there.";
    const model = new SettingsModel(
      async (input, init) => {
        if (init?.method === "PUT")
          return jsonResponse({ error: sentence, reason: "SETTINGS_TOO_NEW" }, 409);
        return jsonResponse(settingsOverview());
      },
      () => {},
    );
    await model.load();

    await model.setAutoUpdateEnabled("local", true);

    expect(model.autoUpdateFailure?.isExplained).toBe(true);
    expect(model.autoUpdateFailure?.error.message).toBe(sentence);
    expect(model.autoUpdateFailure?.isBugReportOffered).toBe(false);
    expect(model.autoUpdateFailure?.retry).toBeUndefined();
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

    await model.setNotificationPrefs("user-7", {
      is_enabled: true,
      style: "os",
    });

    expect(writes).toEqual([
      {
        url: "/ui/api/settings/notifications",
        ifMatch: "np-1",
        body: { style: "os" },
      },
    ]);
    expect(model.notificationPrefs()).toEqual({
      is_enabled: true,
      style: "os",
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
      version: "np-9",
    };

    await model.setNotificationPrefs("user-7", {
      is_enabled: true,
      style: "both",
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

    await model.setNotificationPrefs("user-7", {
      is_enabled: false,
      style: "both",
    });
    expect(model.notificationPrefsFailure?.error.message).toBe(
      "Couldn't save this change — Imbue Studio didn't respond. Try again in a moment.",
    );
    expect(model.notificationPrefsFailure?.isBugReportOffered).toBe(false);

    isServerUp = true;
    await model.setNotificationPrefs("user-7", {
      is_enabled: false,
      style: "both",
    });
    expect(model.notificationPrefsFailure).toBeNull();
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

    await model.setNotificationPrefs("user-7", {
      is_enabled: false,
      style: "both",
    });

    expect(model.notificationPrefsFailure?.error.message).toBe(
      "Couldn't save this change. Try again. If this persists, report a bug.",
    );
    expect(model.notificationPrefsFailure?.isBugReportOffered).toBe(true);
    expect(model.notificationPrefsFailure?.error.details).toContain(
      "notifications are not available yet",
    );
    expect(model.notificationPrefs().is_enabled).toBe(true);
  });

  it("reports whether the test notification could reach the OS, and a failure to send one", async () => {
    let status = 200;
    let isElectron = true;
    const model = new SettingsModel(
      async (input, init) => {
        const url = String(input);
        if (url.endsWith("/settings/notifications/test")) {
          expect(init?.method).toBe("POST");
          return jsonResponse({ is_electron: isElectron }, status);
        }
        return jsonResponse({
          ...BASE_OVERVIEW,
          notification_prefs: BASE_PREFS,
        });
      },
      () => {},
    );
    await model.load();

    await model.sendTestNotification();
    expect(model.testNotificationResult).toContain("Sent.");
    expect(model.isTestNotificationBusy).toBe(false);

    isElectron = false;
    await model.sendTestNotification();
    expect(model.testNotificationResult).toContain("need the desktop app");

    status = 503;
    await model.sendTestNotification();
    expect(model.testNotificationResult).toContain("HTTP 503");
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

    expect(model.masterPasswordRefusal).toContain("do not match");
    expect(postCount).toBe(0);
  });
});

function updateStateAt(
  channel: UpdateChannel,
  currentVersion: string,
): UpdateState {
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

      expect(model.pendingChannelSwitch).toEqual({
        channel: "stable",
        targetVersion: "0.4.12",
      });
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
    const unpublished = {
      ...PEEKED,
      beta: { version: null, wouldPark: false, error: "404" },
    };
    const { surface, switches } = nativeStub(RUNNING, unpublished);
    await withMindsNative(surface, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();

      await model.requestChannel("beta");

      expect(switches).toEqual([]);
      expect(model.updateState?.channel).toBe("alpha");
      expect(model.updateError?.message).toBe(
        "The Beta channel is unavailable right now, so Imbue Studio stayed on Alpha.",
      );
      // The sentence says what happened and what it means; nothing to report.
      expect(model.updateError?.isBugReportOffered).toBe(false);
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
      expect(secondVisit.updateState?.available).toEqual([
        "stable",
        "beta",
        "alpha",
      ]);
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

      pushUpdateStatus?.({
        type: "up-to-date",
        channel: "stable",
        currentVersion: "0.4.30",
      });

      expect(otherWindow.updateState?.channel).toBe("stable");
    });
  });

  it("keeps the channel when a status carries none", async () => {
    // `disabled`, and a check that rejected before it resolved one, have none.
    const { surface } = nativeStub(RUNNING, PEEKED);
    await withMindsNative(surface, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();

      pushUpdateStatus?.({
        type: "error",
        message: "MINDS_ROOT_NAME is unreadable",
      });

      expect(model.updateState?.channel).toBe("alpha");
      expect(model.updateState?.status.type).toBe("error");
    });
  });

  it("keeps the last check time through a status that is not a check", async () => {
    // `checking` and `disabled` carry no time. Letting them overwrite it would
    // blank the line for the duration of every check, which is exactly when the
    // user is looking at it.
    const { surface } = nativeStub(
      { ...RUNNING, lastCheckedAt: "2026-08-13T23:53:00.000Z" },
      PEEKED,
    );
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
      expect(model.updateError?.message).toBe(
        "Couldn't load your update settings. Try again. If this persists, report a bug.",
      );
      expect(model.updateError?.details).toContain(
        "MINDS_ROOT_NAME is unreadable",
      );
    });
  });

  it("clears a failed update-state read when Try again reads it", async () => {
    const { surface } = nativeStub(RUNNING, PEEKED);
    let isBroken = true;
    const flaky = {
      ...surface,
      getUpdateState: async () => {
        if (isBroken) throw new Error("MINDS_ROOT_NAME is unreadable");
        return RUNNING;
      },
    };
    await withMindsNative(flaky, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();
      expect(model.updateError).not.toBeNull();

      isBroken = false;
      await model.loadUpdateState();

      expect(model.updateError).toBeNull();
      expect(model.updateState?.channel).toBe("alpha");
    });
  });

  it("re-peeks on an on-demand check, so a channel published since the load stops reading unavailable", async () => {
    let peeked: Record<string, PeekedChannel> = {
      ...PEEKED,
      beta: { version: null, wouldPark: false, error: "404" },
    };
    const afterCheck: UpdateState = {
      ...RUNNING,
      status: {
        type: "update-available",
        channel: "alpha",
        feedVersion: "0.4.31",
      },
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
      expect(model.updateError).toBeNull();
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
        status: {
          type: "update-available" as const,
          channel: "alpha" as const,
          feedVersion: "0.4.31",
        },
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

      expect(model.updateError?.message).toBe(
        "Couldn't check for updates. Try again. If this persists, report a bug.",
      );
      expect(model.updateError?.details).toContain(
        "Cannot find channel alpha-mac.yml",
      );
      // Left busy, the panel would read as a check that never returns.
      expect(model.isUpdateBusy).toBe(false);
      expect(model.updateState?.channel).toBe("alpha");
    });
  });

  it("surfaces an install that did not go through, where the app is still up to read it", async () => {
    // A .deb installs under the system password prompt; cancelling it leaves
    // the app running with the download still staged, and the main process
    // rejects the install rather than quitting. Silence here would read as a
    // button that does nothing.
    const { surface } = nativeStub(RUNNING, PEEKED);
    const failing = {
      ...surface,
      installUpdate: async () => ({
        error: "Installing the update failed: Command failed: pkexec",
        kind: "failed" as const,
      }),
    };
    await withMindsNative(failing, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();

      await model.installUpdateNow("Install and restart");

      expect(model.updateError?.message).toBe(
        "The update didn't install. It's still downloaded. Try again. If this persists, report a bug.",
      );
      expect(model.updateError?.details).toContain("Command failed: pkexec");
      expect(model.updateError?.isBugReportOffered).toBe(true);
      expect(model.isUpdateInstalling).toBe(false);
    });
  });

  it("says an install that never signalled may still be under way, naming the button to press again", async () => {
    // macOS: an install asked for while the system updater is still taking the
    // download raises nothing within the wait, and can still go through.
    const { surface } = nativeStub(RUNNING, PEEKED);
    const silent = {
      ...surface,
      installUpdate: async () => ({
        error:
          "Installing the update failed: the updater never started installing it",
        kind: "no-signal" as const,
      }),
    };
    await withMindsNative(silent, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();

      await model.installUpdateNow("Restart now");

      expect(model.updateError?.message).toBe(
        "Imbue Studio couldn't confirm the update started installing. " +
          "If it hasn't restarted in a minute, press Restart now to try again.",
      );
      // The install may still be under way, so the sentence asks for patience, not a report.
      expect(model.updateError?.isBugReportOffered).toBe(false);
    });
  });

  it("says a refused channel switch left the app on the channel it was on", async () => {
    const { surface } = nativeStub(RUNNING, PEEKED);
    const refusing = {
      ...surface,
      setUpdateChannel: async () => {
        throw new Error(
          "Error invoking remote method 'set-update-channel': Error: Unknown update channel",
        );
      },
    };
    await withMindsNative(refusing, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();

      await model.requestChannel("stable");
      if (model.pendingChannelSwitch !== null)
        await model.confirmChannelSwitch();

      expect(model.updateError?.message).toBe(
        "Imbue Studio couldn't switch which updates you get. You're still on Alpha updates. " +
          "Try again. If this persists, report a bug.",
      );
      expect(model.updateError?.details).toContain(
        "Error invoking remote method",
      );
    });
  });

  it("is installing from the click until the main process settles the call", async () => {
    // Set before the main process is asked, since a .deb install blocks it;
    // cleared only when the call settles, which means the app stayed up.
    const { surface } = nativeStub(RUNNING, PEEKED);
    const { installUpdate, resolveInstall } = pendingInstallUpdate();
    await withMindsNative({ ...surface, installUpdate }, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadUpdateState();

      const done = model.installUpdateNow("Restart now");
      expect(model.isUpdateInstalling).toBe(true);
      resolveInstall();
      await done;
      expect(model.isUpdateInstalling).toBe(false);
      expect(model.updateError).toBeNull();
    });
  });

  it("lists the Updates section in the browser build too, since machine updates are configured there", async () => {
    // The app-updates half of the panel is desktop-only, but the panel itself
    // is not: the machine-update window applies to every build.
    await withMindsNative(null, async () => {
      const names = new SettingsModel(undefined, () => {}).visibleSections.map(
        (section) => section.name,
      );
      expect(names).toContain("updates");
      expect(names).toContain("error-reporting");
    });
    await withMindsNative(nativeStub(RUNNING, PEEKED).surface, async () => {
      expect(
        new SettingsModel(undefined, () => {}).visibleSections.map(
          (s) => s.name,
        ),
      ).toContain("updates");
    });
  });
});

describe("SettingsModel display zoom", () => {
  it("reads the stored percent from the main process", async () => {
    await withMindsNative({ getDisplayZoom: async () => 125 }, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadDisplayZoom();
      expect(model.displayZoomPercent).toBe(125);
      expect(model.displayZoomError).toBe("");
    });
  });

  it("reads null in the browser build, where the shell cannot scale windows", async () => {
    await withMindsNative(null, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadDisplayZoom();
      expect(model.displayZoomPercent).toBeNull();
    });
  });

  it("shows the percent the main process confirmed after a change", async () => {
    const stored: number[] = [];
    const surface = {
      getDisplayZoom: async () => 100,
      setDisplayZoom: async (percent: number) => {
        stored.push(percent);
        return percent;
      },
    };
    await withMindsNative(surface, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadDisplayZoom();
      await model.setDisplayZoom(150);
      expect(stored).toEqual([150]);
      expect(model.displayZoomPercent).toBe(150);
    });
  });

  it("keeps the current percent and states the reason when the main process refuses", async () => {
    const surface = {
      getDisplayZoom: async () => 100,
      setDisplayZoom: async () => {
        throw new Error("Unknown display zoom 137");
      },
    };
    await withMindsNative(surface, async () => {
      const model = new SettingsModel(undefined, () => {});
      await model.loadDisplayZoom();
      await model.setDisplayZoom(137);
      expect(model.displayZoomPercent).toBe(100);
      expect(model.displayZoomError).toContain("Unknown display zoom 137");
    });
  });
});
