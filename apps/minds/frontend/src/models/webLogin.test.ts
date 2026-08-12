import { describe, expect, it } from "vitest";
import { jsonResponse } from "../testing";
import { WebLoginModel, consumeWebLoginParams } from "./webLogin";

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
