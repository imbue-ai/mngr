import { describe, expect, it } from "vitest";
import type { ImmersiveChange, KeyboardLockSurface } from "./immersive";
import { ImmersiveController, describeImmersiveError } from "./immersive";

/** A deferred promise, so a test decides when fullscreen or the lock settles. */
function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

/** A fake frame and document: fullscreen is granted or refused as the test
 * says, and `fullscreenchange` is the controller's own to notice. */
function makeFixture(
  options: { hasKeyboard?: boolean; isLockGranted?: boolean } = {},
) {
  const calls: string[] = [];
  const sent: ImmersiveChange[] = [];
  const desktop: boolean[] = [];
  let isFullscreen = false;
  // One deferred per requestFullscreen call, so a test can settle a specific one.
  const fullscreenRequests: ReturnType<typeof deferred<void>>[] = [];
  let lockRequest = deferred<void>();
  const keyboard: KeyboardLockSurface | null =
    options.hasKeyboard === false
      ? null
      : {
          lock: () => {
            calls.push("lock");
            lockRequest = deferred<void>();
            if (options.isLockGranted === false)
              lockRequest.reject(new Error("denied"));
            return lockRequest.promise;
          },
          unlock: () => {
            calls.push("unlock");
          },
        };
  const controller = new ImmersiveController({
    requestFullscreen: () => {
      calls.push("requestFullscreen");
      const request = deferred<void>();
      fullscreenRequests.push(request);
      return request.promise;
    },
    isFullscreen: () => isFullscreen,
    exitFullscreen: () => {
      calls.push("exitFullscreen");
      isFullscreen = false;
      // The browser fires fullscreenchange after the exit settles.
      queueMicrotask(() => controller.fullscreenChanged());
      return Promise.resolve();
    },
    keyboard,
    setDesktopImmersive: (on) => desktop.push(on),
    send: (change) => sent.push(change),
  });
  return {
    controller,
    calls,
    sent,
    desktop,
    /** Settle the latest fullscreen request, or the one at `index`. */
    grantFullscreen: (index = fullscreenRequests.length - 1) => {
      isFullscreen = true;
      fullscreenRequests[index].resolve();
    },
    refuseFullscreen: (error: unknown) =>
      fullscreenRequests[fullscreenRequests.length - 1].reject(error),
    grantLock: () => lockRequest.resolve(),
    /** The browser left fullscreen on its own (the user held Escape, the OS). */
    leaveFullscreenExternally: () => {
      isFullscreen = false;
      controller.fullscreenChanged();
    },
  };
}

describe("ImmersiveController", () => {
  it("asks for fullscreen and the keyboard lock at once, synchronously, inside the request", () => {
    const fixture = makeFixture();
    fixture.controller.request(true);
    // Both calls happen before any promise settles, so the fullscreen request
    // is still inside the gesture's activation.
    expect(fixture.calls).toEqual(["requestFullscreen", "lock"]);
    expect(fixture.sent).toEqual([]);
    expect(fixture.controller.isImmersive).toBe(false);
  });

  it("turns immersive mode on with the lock once fullscreen lands and the lock is granted", async () => {
    const fixture = makeFixture();
    fixture.controller.request(true);
    fixture.grantFullscreen();
    fixture.grantLock();
    await flush();
    expect(fixture.controller.isImmersive).toBe(true);
    expect(fixture.desktop).toEqual([true]);
    expect(fixture.sent).toEqual([{ on: true, keyboardLocked: true }]);
  });

  it("stays on without the lock when the lock is refused", async () => {
    const fixture = makeFixture({ isLockGranted: false });
    fixture.controller.request(true);
    fixture.grantFullscreen();
    await flush();
    expect(fixture.controller.isImmersive).toBe(true);
    expect(fixture.sent).toEqual([{ on: true, keyboardLocked: false }]);
  });

  it("stays on without the lock when the browser has no keyboard API", async () => {
    const fixture = makeFixture({ hasKeyboard: false });
    fixture.controller.request(true);
    expect(fixture.calls).toEqual(["requestFullscreen"]);
    fixture.grantFullscreen();
    await flush();
    expect(fixture.sent).toEqual([{ on: true, keyboardLocked: false }]);
    expect(fixture.desktop).toEqual([true]);
  });

  it("answers a refused fullscreen with an error, off, and the lock released", async () => {
    const fixture = makeFixture();
    fixture.controller.request(true);
    fixture.refuseFullscreen(new TypeError("Permissions check failed"));
    fixture.grantLock();
    await flush();
    expect(fixture.controller.isImmersive).toBe(false);
    expect(fixture.desktop).toEqual([]);
    expect(fixture.calls).toEqual(["requestFullscreen", "lock", "unlock"]);
    expect(fixture.sent).toEqual([
      {
        on: false,
        keyboardLocked: false,
        error: "TypeError: Permissions check failed",
      },
    ]);
  });

  it("answers a fullscreen request that throws synchronously the same way", () => {
    const sent: ImmersiveChange[] = [];
    const controller = new ImmersiveController({
      requestFullscreen: () => {
        throw new Error("no element");
      },
      isFullscreen: () => false,
      exitFullscreen: () => Promise.resolve(),
      keyboard: null,
      setDesktopImmersive: () => undefined,
      send: (change) => sent.push(change),
    });
    controller.request(true);
    expect(sent).toEqual([
      { on: false, keyboardLocked: false, error: "Error: no element" },
    ]);
    // A later request is not blocked by the failed one.
    controller.request(true);
    expect(sent).toHaveLength(2);
  });

  it("leaves on request by exiting fullscreen, unlocking, restoring the desktop and saying so", async () => {
    const fixture = makeFixture();
    fixture.controller.request(true);
    fixture.grantFullscreen();
    fixture.grantLock();
    await flush();
    fixture.controller.request(false);
    await flush();
    expect(fixture.calls).toEqual([
      "requestFullscreen",
      "lock",
      "exitFullscreen",
      "unlock",
    ]);
    expect(fixture.desktop).toEqual([true, false]);
    expect(fixture.sent).toEqual([
      { on: true, keyboardLocked: true },
      { on: false, keyboardLocked: false },
    ]);
    expect(fixture.controller.isImmersive).toBe(false);
  });

  it("ends when fullscreen ends by any other route, with one change sent", async () => {
    const fixture = makeFixture();
    fixture.controller.request(true);
    fixture.grantFullscreen();
    fixture.grantLock();
    await flush();
    fixture.leaveFullscreenExternally();
    // A second fullscreenchange without a fullscreen element (the frame's own
    // inner fullscreen ending later) changes nothing and says nothing more.
    fixture.leaveFullscreenExternally();
    expect(fixture.desktop).toEqual([true, false]);
    expect(fixture.sent).toEqual([
      { on: true, keyboardLocked: true },
      { on: false, keyboardLocked: false },
    ]);
    expect(fixture.calls.filter((call) => call === "unlock")).toHaveLength(1);
  });

  it("drops an entry's completion when fullscreen ended before the lock settled", async () => {
    const fixture = makeFixture();
    fixture.controller.request(true);
    fixture.grantFullscreen();
    await flush();
    // Fullscreen landed, the lock is still pending, and the user leaves.
    fixture.leaveFullscreenExternally();
    fixture.grantLock();
    await flush();
    expect(fixture.controller.isImmersive).toBe(false);
    expect(fixture.desktop).toEqual([false]);
    expect(fixture.sent).toEqual([{ on: false, keyboardLocked: false }]);
  });

  it("exits a fullscreen that lands after a leave request superseded the entry", async () => {
    const fixture = makeFixture();
    fixture.controller.request(true);
    // The workspace changes its mind while the fullscreen request is pending.
    fixture.controller.request(false);
    expect(fixture.sent).toEqual([{ on: false, keyboardLocked: false }]);
    fixture.grantFullscreen();
    fixture.grantLock();
    await flush();
    expect(fixture.calls).toContain("exitFullscreen");
    expect(fixture.controller.isImmersive).toBe(false);
    // The leave restored the desktop at once; nothing turns it back on.
    expect(fixture.desktop).toEqual([false]);
    // The exit's own fullscreenchange adds nothing: the mode was already off.
    expect(fixture.sent).toEqual([{ on: false, keyboardLocked: false }]);
  });

  it("leaves a newer entry's fullscreen alone when a superseded one completes", async () => {
    const fixture = makeFixture();
    fixture.controller.request(true);
    fixture.controller.frameNavigated();
    fixture.controller.request(true);
    // The first request's fullscreen lands while the second entry is in flight.
    fixture.grantFullscreen(0);
    fixture.grantLock();
    await flush();
    expect(fixture.calls.filter((call) => call === "exitFullscreen")).toEqual(
      [],
    );
    fixture.grantFullscreen(1);
    await flush();
    expect(fixture.controller.isImmersive).toBe(true);
    expect(fixture.sent.at(-1)).toEqual({ on: true, keyboardLocked: true });
  });

  it("answers a leave request while off without touching anything", () => {
    const fixture = makeFixture();
    fixture.controller.request(false);
    expect(fixture.calls).toEqual([]);
    expect(fixture.desktop).toEqual([]);
    expect(fixture.sent).toEqual([{ on: false, keyboardLocked: false }]);
  });

  it("ignores a second enter request while one is in flight or immersive is on", async () => {
    const fixture = makeFixture();
    fixture.controller.request(true);
    fixture.controller.request(true);
    fixture.grantFullscreen();
    fixture.grantLock();
    await flush();
    fixture.controller.request(true);
    expect(fixture.calls).toEqual(["requestFullscreen", "lock"]);
    expect(fixture.sent).toEqual([{ on: true, keyboardLocked: true }]);
  });

  it("ends immersive mode when the frame navigates, exiting the fullscreen it holds", async () => {
    const fixture = makeFixture();
    fixture.controller.request(true);
    fixture.grantFullscreen();
    fixture.grantLock();
    await flush();
    fixture.controller.frameNavigated();
    expect(fixture.calls).toContain("exitFullscreen");
    expect(fixture.desktop).toEqual([true, false]);
    expect(fixture.sent).toEqual([
      { on: true, keyboardLocked: true },
      { on: false, keyboardLocked: false },
    ]);
    await flush();
    // The exit's own fullscreenchange says nothing more.
    expect(fixture.sent).toHaveLength(2);
  });

  it("does nothing on a navigation while off", () => {
    const fixture = makeFixture();
    fixture.controller.frameNavigated();
    expect(fixture.calls).toEqual([]);
    expect(fixture.sent).toEqual([]);
  });
});

describe("describeImmersiveError", () => {
  it("names an error by type and message, bounded, and stringifies anything else", () => {
    expect(
      describeImmersiveError(new TypeError("Permissions check failed")),
    ).toBe("TypeError: Permissions check failed");
    expect(describeImmersiveError(new Error(""))).toBe("Error");
    expect(describeImmersiveError("refused")).toBe("refused");
    expect(describeImmersiveError(new Error("x".repeat(400)))).toHaveLength(
      256,
    );
  });
});
