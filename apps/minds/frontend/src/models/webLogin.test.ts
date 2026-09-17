import { describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../testing";
import { POLL_INTERVAL_MS, WebLoginModel, consumeWebLoginParams } from "./webLogin";

/** A model whose start request stays pending until the test settles it, so
 * dismiss() can be interleaved with the in-flight request. */
function makeModelWithDeferredStart(): {
  model: WebLoginModel;
  resolve: (response: Response) => void;
  reject: (reason: Error) => void;
} {
  const settlers = {
    resolve: (_response: Response) => {},
    reject: (_reason: Error) => {},
  };
  const model = new WebLoginModel(
    () =>
      new Promise<Response>((resolve, reject) => {
        settlers.resolve = resolve;
        settlers.reject = reject;
      }),
    () => {},
  );
  return { model, resolve: (r) => settlers.resolve(r), reject: (e) => settlers.reject(e) };
}

/** A model whose status polls answer from a scripted sequence of flow states,
 * so a test can walk one sign-in from waiting through to done and count the
 * raises it asked for along the way. */
function makeModelWithStatusScript(states: readonly string[]): {
  model: WebLoginModel;
  raiseCount: () => number;
} {
  const remaining = [...states];
  let raises = 0;
  const model = new WebLoginModel(
    (input) =>
      Promise.resolve(
        String(input).includes("/web-login/start")
          ? jsonResponse({ flow_id: "flow-1" })
          : jsonResponse({ state: remaining.shift() ?? "waiting" }),
      ),
    () => {},
    () => {
      raises += 1;
    },
  );
  return { model, raiseCount: () => raises };
}

describe("WebLoginModel", () => {
  it("stays dismissed when the start request resolves after the user cancelled", async () => {
    const { model, resolve } = makeModelWithDeferredStart();
    const startPromise = model.start("sign in to continue");
    expect(model.state).toBe("starting");

    model.dismiss();
    expect(model.isOpen).toBe(false);

    resolve(jsonResponse({ flow_id: "flow-1" }));
    await startPromise;

    expect(model.state).toBe("idle");
    expect(model.isOpen).toBe(false);
  });

  it("stays dismissed when the start request fails after the user cancelled", async () => {
    const { model, reject } = makeModelWithDeferredStart();
    const startPromise = model.start();
    expect(model.state).toBe("starting");

    model.dismiss();

    reject(new Error("network down"));
    await startPromise;

    expect(model.state).toBe("idle");
    expect(model.error).toBe("");
  });

  it("moves to waiting when the start request succeeds without a dismiss", async () => {
    const { model, resolve } = makeModelWithDeferredStart();
    const startPromise = model.start();

    resolve(jsonResponse({ flow_id: "flow-1" }));
    await startPromise;

    expect(model.state).toBe("waiting");
    // Silence the poll timer the successful start scheduled.
    model.dismiss();
  });

  it("brings the app to the front once the sign-in lands, and only once", async () => {
    vi.useFakeTimers();
    try {
      const { model, raiseCount } = makeModelWithStatusScript(["running", "finishing", "finishing", "done"]);
      await model.start();
      // Signing in happens in the browser; nothing to come back to yet.
      expect(raiseCount()).toBe(0);

      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
      expect(model.state).toBe("waiting");
      expect(raiseCount()).toBe(0);

      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
      expect(model.state).toBe("finishing");
      expect(raiseCount()).toBe(1);

      // The poller keeps running while the account is mirrored; a raise per
      // tick would take focus back off the user every second.
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
      expect(model.state).toBe("finishing");
      expect(raiseCount()).toBe(1);

      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
      expect(model.state).toBe("done");
      expect(raiseCount()).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("brings the app to the front for a sign-in that never reported finishing", async () => {
    // A mirror fast enough that the poll straight after it reads "done".
    vi.useFakeTimers();
    try {
      const { model, raiseCount } = makeModelWithStatusScript(["done"]);
      await model.start();

      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);

      expect(model.state).toBe("done");
      expect(raiseCount()).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("raises again for a second sign-in", async () => {
    // The guard is per flow: adding an account after the first one still
    // hands focus back.
    vi.useFakeTimers();
    try {
      const { model, raiseCount } = makeModelWithStatusScript(["done", "done"]);
      await model.start();
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
      expect(raiseCount()).toBe(1);

      model.dismiss();
      await model.start();
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);

      expect(raiseCount()).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("brings the app to the front when the account lands before any poll does", async () => {
    // What re-picking a recently used account looks like: the accounts channel
    // carries it, the surface waiting on it dismisses the flow, and the poller
    // dies before it ever reads "finishing".
    vi.useFakeTimers();
    try {
      const { model, raiseCount } = makeModelWithStatusScript(["running"]);
      await model.start();
      expect(raiseCount()).toBe(0);

      model.noteSignedIn();
      model.dismiss();

      expect(raiseCount()).toBe(1);
      // The poll is dead, so nothing raises a second time.
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
      expect(raiseCount()).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not raise twice when the poll already saw the sign-in land", async () => {
    vi.useFakeTimers();
    try {
      const { model, raiseCount } = makeModelWithStatusScript(["finishing"]);
      await model.start();
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
      expect(raiseCount()).toBe(1);

      model.noteSignedIn();

      expect(raiseCount()).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("ignores a signed-in note with no flow in flight", () => {
    // An account arriving on the channel for any other reason -- a second
    // window signing in, a startup frame -- must not pull focus.
    const { model, raiseCount } = makeModelWithStatusScript([]);

    model.noteSignedIn();

    expect(model.state).toBe("idle");
    expect(raiseCount()).toBe(0);
  });

  it("does not bring the app to the front when the sign-in fails", async () => {
    vi.useFakeTimers();
    try {
      const { model, raiseCount } = makeModelWithStatusScript(["error"]);
      await model.start();

      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);

      expect(model.state).toBe("error");
      expect(raiseCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("consumeWebLoginParams", () => {
  it("returns the message and strips both params when the sign-in is requested", () => {
    const params = new URLSearchParams("web-login=1&web-login-message=please%20sign%20in&keep=me");

    const message = consumeWebLoginParams(params);

    expect(message).toBe("please sign in");
    expect(params.get("web-login")).toBeNull();
    expect(params.get("web-login-message")).toBeNull();
    expect(params.get("keep")).toBe("me");
  });

  it("returns an empty message when the request carries none", () => {
    const params = new URLSearchParams("web-login=1");

    expect(consumeWebLoginParams(params)).toBe("");
  });

  it("returns null and leaves params alone when no sign-in is requested", () => {
    const params = new URLSearchParams("foo=bar");

    expect(consumeWebLoginParams(params)).toBeNull();
    expect(params.get("foo")).toBe("bar");
  });

  it("treats other web-login values as no request", () => {
    expect(consumeWebLoginParams(new URLSearchParams("web-login=0"))).toBeNull();
  });
});
