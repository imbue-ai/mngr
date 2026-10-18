import { beforeEach, describe, expect, it } from "vitest";
import { pendingInstallUpdate, settle, withMindsNative } from "../../testing";
import {
  dismissUpdateReady,
  installUpdateReady,
  isUpdateInstalling,
  resetUpdateReadyForTest,
  updateInstallError,
  updateInstallTerms,
  updateReadyVersion,
  watchUpdateStatus,
} from "./update-ready";

const DOWNLOADED = {
  channel: "alpha",
  currentVersion: "0.3.13",
  available: ["stable", "alpha"],
  status: { type: "update-downloaded", version: "0.3.14" },
};

/** A native surface that answers getUpdateState and records the status listener. */
function nativeStub(state: unknown) {
  let listener: ((status: unknown) => void) | null = null;
  return {
    surface: {
      getUpdateState: () => Promise.resolve(state),
      onUpdateStatus: (callback: (status: unknown) => void) => {
        listener = callback;
      },
    },
    push: (status: unknown) => listener?.(status),
  };
}

describe("the update-ready card's state", () => {
  beforeEach(() => resetUpdateReadyForTest());

  it("seeds from the current state, not only from a push", async () => {
    // A status is broadcast once, when it changes. A download that lands while
    // the splash screen is up, or before a reload, is announced to a window
    // that is not listening yet -- and nothing re-sends it.
    const { surface } = nativeStub(DOWNLOADED);
    await withMindsNative(surface, async () => {
      watchUpdateStatus(() => {});
      await settle();

      expect(updateReadyVersion()).toBe("0.3.14");
    });
  });

  it("reads how the install happens from the same state, defaulting to on-quit", async () => {
    // The terms are a fact about the running binary and ride the seed read; a
    // state that names none (a stub covering only part of the surface) means
    // the macOS policy, install on quit.
    const silent = nativeStub(DOWNLOADED);
    await withMindsNative(silent.surface, async () => {
      watchUpdateStatus(() => {});
      await settle();

      expect(updateInstallTerms()).toEqual({ policy: "on-quit", needsPassword: false });
    });

    resetUpdateReadyForTest();
    const deb = nativeStub({ ...DOWNLOADED, installPolicy: "on-request", needsPasswordToInstall: true });
    await withMindsNative(deb.surface, async () => {
      watchUpdateStatus(() => {});
      await settle();

      expect(updateInstallTerms()).toEqual({ policy: "on-request", needsPassword: true });
    });
  });

  it("offers nothing when the running build is current", async () => {
    const { surface } = nativeStub({ ...DOWNLOADED, status: { type: "up-to-date" } });
    await withMindsNative(surface, async () => {
      watchUpdateStatus(() => {});
      await settle();

      expect(updateReadyVersion()).toBeNull();
    });
  });

  it("takes a later push over what it was seeded with", async () => {
    // The seed is the state as of the moment the window started listening, so
    // anything pushed afterwards is newer by construction and has to win.
    const { surface, push } = nativeStub(DOWNLOADED);
    await withMindsNative(surface, async () => {
      watchUpdateStatus(() => {});
      await settle();
      expect(updateReadyVersion()).toBe("0.3.14");

      push({ type: "update-downloaded", version: "0.3.15" });

      expect(updateReadyVersion()).toBe("0.3.15");
    });
  });

  it("keeps the offer up while the next check runs", async () => {
    // Checks run every ten minutes and push `checking` before they settle, so
    // taking that as "no update" blinks the card out and back on a timer -- for
    // as long as the check takes, which is up to a minute.
    const { surface, push } = nativeStub(DOWNLOADED);
    await withMindsNative(surface, async () => {
      watchUpdateStatus(() => {});
      await settle();

      push({ type: "checking", channel: "alpha" });

      expect(updateReadyVersion()).toBe("0.3.14");
    });
  });

  it("keeps the offer up when a check cannot reach the feed", async () => {
    // The artifact was handed to the installer as it landed, so it installs on
    // the next restart whether or not the feed answers. Taking the error as "no
    // update" hides a restart that is going to update the app anyway, until a
    // check succeeds ten minutes later.
    const { surface, push } = nativeStub(DOWNLOADED);
    await withMindsNative(surface, async () => {
      watchUpdateStatus(() => {});
      await settle();

      push({ type: "error", channel: "alpha", message: "getaddrinfo ENOTFOUND" });

      expect(updateReadyVersion()).toBe("0.3.14");
    });
  });

  it("withdraws the offer when a check reaches the feed and finds nothing staged", async () => {
    // The other half of the rule above: a check that got an answer is the only
    // thing allowed to take the card down.
    const { surface, push } = nativeStub(DOWNLOADED);
    await withMindsNative(surface, async () => {
      watchUpdateStatus(() => {});
      await settle();

      push({ type: "up-to-date", channel: "alpha" });

      expect(updateReadyVersion()).toBeNull();
    });
  });

  it("stays dismissed for the version that was dismissed, and not for the next one", async () => {
    // The check repeats every ten minutes and re-publishes the same downloaded
    // version, so a dismissal that did not stick would reappear on a timer.
    const { surface, push } = nativeStub(DOWNLOADED);
    await withMindsNative(surface, async () => {
      watchUpdateStatus(() => {});
      await settle();
      dismissUpdateReady();

      expect(updateReadyVersion()).toBeNull();
      push({ type: "update-downloaded", version: "0.3.14" });
      expect(updateReadyVersion()).toBeNull();
      push({ type: "update-downloaded", version: "0.3.15" });
      expect(updateReadyVersion()).toBe("0.3.15");
    });
  });

  it("keeps a failed install for the card, and clears it on the next try", async () => {
    // The main process rejects the install when electron-updater reports a
    // failure (a cancelled password prompt on a .deb) rather than quitting;
    // the card is where the click happened, so it is where the answer goes.
    let isInstallable = false;
    const surface = {
      ...nativeStub(DOWNLOADED).surface,
      installUpdate: async () => ({ error: isInstallable ? null : "Installing the update failed: pkexec" }),
    };
    await withMindsNative(surface, async () => {
      let changeCount = 0;
      await installUpdateReady(() => changeCount++);

      expect(updateInstallError()).toBe("Installing the update failed: pkexec");
      expect(isUpdateInstalling()).toBe(false);
      // Once as the install starts, once as it settles.
      expect(changeCount).toBe(2);

      isInstallable = true;
      await installUpdateReady(() => changeCount++);
      expect(updateInstallError()).toBeNull();
    });
  });

  it("is installing from the click until the main process settles the call", async () => {
    // The main process is blocked for the whole of a .deb install, so the
    // state has to be up before it is asked, from the renderer's own hand;
    // and it settles only if the app stays up (here: the quit was cancelled
    // at the running-workspaces prompt), which hands the control back.
    const { installUpdate, resolveInstall } = pendingInstallUpdate();
    await withMindsNative({ ...nativeStub(DOWNLOADED).surface, installUpdate }, async () => {
      const seen: boolean[] = [];
      const done = installUpdateReady(() => seen.push(isUpdateInstalling()));
      expect(isUpdateInstalling()).toBe(true);
      expect(seen).toEqual([true]);

      resolveInstall();
      await done;
      expect(isUpdateInstalling()).toBe(false);
      expect(seen).toEqual([true, false]);
      expect(updateInstallError()).toBeNull();
    });
  });

  it("drops a failed install's account once a different version is offered", async () => {
    // The failure described an attempt on the version offered at the time;
    // shown under the next version's card it would report a click that never
    // happened on that download. The same offer re-published keeps it.
    const { surface, push } = nativeStub(DOWNLOADED);
    const failing = { ...surface, installUpdate: async () => ({ error: "Installing the update failed: pkexec" }) };
    await withMindsNative(failing, async () => {
      watchUpdateStatus(() => {});
      await settle();
      await installUpdateReady(() => {});
      expect(updateInstallError()).toBe("Installing the update failed: pkexec");

      push({ type: "update-downloaded", version: "0.3.14" });
      expect(updateInstallError()).toBe("Installing the update failed: pkexec");

      push({ type: "update-downloaded", version: "0.3.15" });
      expect(updateInstallError()).toBeNull();
    });
  });
});
