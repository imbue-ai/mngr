import { afterEach, describe, expect, it } from "vitest";
import {
  fakeProfilingEnvironment,
  jsonResponse,
  postedProfileEvents,
} from "./testing";
import type { FakeProfilingEnvironment } from "./testing";
import { installProfiling, instant, span } from "./profiling";
import type { ProfilingHandle, ProfilingInstallOptions } from "./profiling";

const TRACEPARENT_PATTERN = /^00-([0-9a-f]{32})-([0-9a-f]{16})-01$/;
const PROFILE_EVENTS_URL = "/ui/api/profile/events";
const MAX_BATCH_BYTES = 1_000_000;

let installed: ProfilingHandle | null = null;

function install(
  overrides: Partial<ProfilingInstallOptions> = {},
): FakeProfilingEnvironment {
  const environment = fakeProfilingEnvironment(overrides);
  installed = installProfiling(environment.options);
  return environment;
}

afterEach(() => {
  installed?.uninstall();
  installed = null;
});

/** The headers the wrapped fetch was handed on its most recent call. */
function lastSentHeaders(environment: FakeProfilingEnvironment): Headers {
  const call = environment.fetchCalls.at(-1);
  if (call === undefined) throw new Error("Nothing was fetched.");
  return new Headers(call.init?.headers);
}

/** The bodies posted to the profile endpoint by fetch, as sent. */
function postedBodies(environment: FakeProfilingEnvironment): string[] {
  return environment.fetchCalls
    .filter((call) => String(call.input) === PROFILE_EVENTS_URL)
    .map((call) => call.init?.body as string);
}

describe("span", () => {
  it("converts a measure to an event with the documented shape", () => {
    const environment = install();
    environment.clock.nowMs = 10;
    const value = span(
      "share_panel.load",
      () => {
        environment.clock.nowMs = 25;
        return 42;
      },
      { target: "web" },
    );

    expect(value).toBe(42);
    expect(environment.measures).toEqual([
      {
        name: "share_panel.load",
        start: "share_panel.load#1.start",
        end: "share_panel.load#1.end",
        detail: { target: "web" },
      },
    ]);
    // The pair of marks is cleared once measured.
    expect(environment.marks).toEqual([]);

    environment.fireTimer();
    const body = JSON.parse(postedBodies(environment)[0]) as Record<
      string,
      unknown
    >;
    expect(body.session_id).toMatch(/^[0-9a-f]{32}$/);
    expect(body.time_origin_ms).toBe(environment.timeOriginMs);
    expect(body.renderer_pid).toBe(4242);
    expect(body.events).toEqual([
      {
        kind: "span",
        name: "share_panel.load",
        cat: "app",
        start_ms: 10,
        duration_ms: 15,
        args: { target: "web" },
        flow_out_id: null,
        flow_in_id: null,
      },
    ]);
  });

  it("ends an async span when its promise settles", async () => {
    const environment = install();
    environment.clock.nowMs = 100;
    let finish: () => void = () => undefined;
    const pending = span(
      "share_panel.publish",
      () =>
        new Promise<string>((resolve) => {
          finish = () => resolve("done");
        }),
    );
    expect(environment.measures).toEqual([]);

    environment.clock.nowMs = 160;
    finish();
    await expect(pending).resolves.toBe("done");

    environment.fireTimer();
    expect(postedProfileEvents(environment)).toMatchObject([
      { name: "share_panel.publish", start_ms: 100, duration_ms: 60, args: {} },
    ]);
  });

  it("records a thrown fn as failed and rethrows", () => {
    const environment = install();
    const failure = new Error("boom");

    expect(() =>
      span(
        "share_panel.add_grant",
        () => {
          throw failure;
        },
        { target: "web" },
      ),
    ).toThrow(failure);

    environment.fireTimer();
    expect(postedProfileEvents(environment)).toMatchObject([
      {
        name: "share_panel.add_grant",
        args: { target: "web", is_failed: true },
      },
    ]);
  });

  it("records a rejected fn as failed and rethrows", async () => {
    const environment = install();
    const failure = new Error("boom");

    await expect(
      span("share_panel.invite", () => Promise.reject(failure)),
    ).rejects.toBe(failure);

    environment.fireTimer();
    expect(postedProfileEvents(environment)).toMatchObject([
      { name: "share_panel.invite", args: { is_failed: true } },
    ]);
  });

  it("overlapping spans of one name each measure their own pair of marks", async () => {
    const environment = install();
    let finishFirst: () => void = () => undefined;
    const first = span(
      "share_panel.refresh_outcomes",
      () =>
        new Promise<void>((resolve) => {
          finishFirst = resolve;
        }),
    );
    span("share_panel.refresh_outcomes", () => undefined);
    finishFirst();
    await first;

    expect(
      environment.measures.map((measure) => [measure.start, measure.end]),
    ).toEqual([
      [
        "share_panel.refresh_outcomes#2.start",
        "share_panel.refresh_outcomes#2.end",
      ],
      [
        "share_panel.refresh_outcomes#1.start",
        "share_panel.refresh_outcomes#1.end",
      ],
    ]);
  });
});

describe("instant", () => {
  it("records a point marker", () => {
    const environment = install();
    environment.clock.nowMs = 7;

    instant("share_panel.opened", { target: "docs" });

    expect(environment.marks).toEqual([
      { name: "share_panel.opened", detail: { target: "docs" } },
    ]);
    environment.fireTimer();
    expect(postedProfileEvents(environment)).toEqual([
      {
        kind: "instant",
        name: "share_panel.opened",
        cat: "app",
        start_ms: 7,
        duration_ms: 0,
        args: { target: "docs" },
        flow_out_id: null,
        flow_in_id: null,
      },
    ]);
  });
});

describe("batching", () => {
  it("posts on the timer, reschedules it, and posts nothing on an empty tick", () => {
    const environment = install();
    expect(environment.timerDelays).toEqual([1000]);
    instant("one");
    instant("two");

    environment.fireTimer();

    expect(environment.timerDelays).toEqual([1000, 1000]);
    const posts = environment.fetchCalls.filter(
      (call) => String(call.input) === PROFILE_EVENTS_URL,
    );
    expect(posts).toHaveLength(1);
    expect(posts[0].init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
    });
    expect(postedProfileEvents(environment).map((event) => event.name)).toEqual(
      ["one", "two"],
    );

    environment.fireTimer();
    expect(postedBodies(environment)).toHaveLength(1);
  });

  it("posts what is left through the beacon when the page hides", () => {
    const environment = install();
    instant("before");
    environment.fireTimer();
    instant("after");

    environment.firePageHide();

    expect(environment.beacons).toHaveLength(1);
    expect(environment.beacons[0].url).toBe(PROFILE_EVENTS_URL);
    const body = JSON.parse(environment.beacons[0].body) as {
      session_id: string;
      events: { name: string }[];
    };
    expect(body.events.map((event) => event.name)).toEqual(["after"]);
    expect(body.session_id).toBe(
      (JSON.parse(postedBodies(environment)[0]) as { session_id: string })
        .session_id,
    );
  });

  it("splits a batch that would exceed one megabyte", () => {
    const environment = install();
    const eventCount = 3000;
    for (let idx = 0; idx < eventCount; idx++)
      instant(`event ${idx} ${"x".repeat(500)}`);

    environment.fireTimer();

    const bodies = postedBodies(environment);
    expect(bodies.length).toBeGreaterThan(1);
    const encoder = new TextEncoder();
    for (const body of bodies) {
      expect(encoder.encode(body).byteLength).toBeLessThanOrEqual(
        MAX_BATCH_BYTES,
      );
      expect(JSON.parse(body)).toMatchObject({ renderer_pid: 4242 });
    }
    const names = postedProfileEvents(environment).map((event) => event.name);
    expect(names).toHaveLength(eventCount);
    expect(names[0]).toMatch(/^event 0 /);
    expect(names[eventCount - 1]).toMatch(
      new RegExp(`^event ${eventCount - 1} `),
    );
  });

  it("posts a failed batch's events no further, without breaking the page", async () => {
    const environment = install();
    environment.respond = () =>
      Promise.reject(new TypeError("Failed to fetch"));
    instant("lost");

    environment.fireTimer();
    await Promise.resolve();

    expect(postedBodies(environment)).toHaveLength(1);
    environment.fireTimer();
    expect(postedBodies(environment)).toHaveLength(1);
  });
});

describe("the fetch wrapper", () => {
  it("adds a W3C traceparent to a same-origin request and records its span", async () => {
    const environment = install();
    environment.clock.nowMs = 50;
    environment.respond = () => {
      environment.clock.nowMs = 80;
      return Promise.resolve(jsonResponse({ ok: true }, 201));
    };

    const response = await environment.fetchHost.fetch(
      "/api/v1/workspace-sharing/host-1/grants?token=secret",
    );

    expect(response.status).toBe(201);
    const header = lastSentHeaders(environment).get("traceparent");
    expect(header).toMatch(TRACEPARENT_PATTERN);
    const [, traceId, spanId] = TRACEPARENT_PATTERN.exec(header ?? "") ?? [];
    expect(traceId).not.toBe("0".repeat(32));
    expect(spanId).not.toBe("0".repeat(16));

    environment.deliverResourceEntries([]);
    environment.clock.nowMs = 2000;
    environment.fireTimer();
    expect(postedProfileEvents(environment)).toEqual([
      {
        kind: "span",
        name: "fetch GET /api/v1/workspace-sharing/host-1/grants",
        cat: "fetch",
        start_ms: 50,
        duration_ms: 30,
        args: {
          method: "GET",
          path: "/api/v1/workspace-sharing/host-1/grants",
          status: 201,
          trace_id: traceId,
          span_id: spanId,
        },
        flow_out_id: spanId,
        flow_in_id: null,
      },
    ]);
    expect(JSON.stringify(postedProfileEvents(environment))).not.toContain(
      "secret",
    );
  });

  it("mints a fresh pair of ids per request", async () => {
    const environment = install();

    await environment.fetchHost.fetch("/api/v1/a");
    const first = lastSentHeaders(environment).get("traceparent");
    await environment.fetchHost.fetch("/api/v1/a");
    const second = lastSentHeaders(environment).get("traceparent");

    expect(first).not.toBe(second);
  });

  it("never uses an all-zero draw as an id", async () => {
    const environment = install({
      randomBytes: (byteCount) => new Uint8Array(byteCount),
    });

    await environment.fetchHost.fetch("/api/v1/a");

    const header = lastSentHeaders(environment).get("traceparent") ?? "";
    expect(header).toMatch(TRACEPARENT_PATTERN);
    expect(header).toBe(`00-${"0".repeat(31)}1-${"0".repeat(15)}1-01`);
  });

  it("passes a cross-origin request through untouched", async () => {
    const environment = install();
    const init = { method: "POST", body: "{}" };

    await environment.fetchHost.fetch(
      "https://api.other.example/v1/thing",
      init,
    );

    expect(environment.fetchCalls).toEqual([
      { input: "https://api.other.example/v1/thing", init },
    ]);
    environment.fireTimer();
    expect(postedProfileEvents(environment)).toEqual([]);
  });

  it("treats a URL object like its string", async () => {
    const environment = install();

    await environment.fetchHost.fetch(
      new URL("/api/v1/thing?x=1", environment.origin),
    );
    await environment.fetchHost.fetch(
      new URL("https://elsewhere.example/thing"),
    );

    expect(environment.fetchCalls[0].init?.headers).toBeInstanceOf(Headers);
    expect(environment.fetchCalls[1].init).toBeUndefined();
    environment.fireTimer();
    expect(postedProfileEvents(environment).map((event) => event.name)).toEqual(
      [],
    );
    environment.clock.nowMs = 5000;
    environment.fireTimer();
    expect(postedProfileEvents(environment).map((event) => event.name)).toEqual(
      ["fetch GET /api/v1/thing"],
    );
  });

  it("keeps a Request input's method, headers, and body beside the header", async () => {
    const environment = install();
    const request = new Request(`${environment.origin}/api/v1/thing`, {
      method: "put",
      headers: { "X-Panel": "share" },
      body: '{"grants":[]}',
    });

    await environment.fetchHost.fetch(request);

    const call = environment.fetchCalls[0];
    expect(call.input).toBe(request);
    expect(call.init?.method).toBeUndefined();
    expect(call.init?.body).toBeUndefined();
    const headers = new Headers(call.init?.headers);
    expect(headers.get("X-Panel")).toBe("share");
    expect(headers.get("traceparent")).toMatch(TRACEPARENT_PATTERN);
    environment.clock.nowMs = 5000;
    environment.fireTimer();
    expect(postedProfileEvents(environment).map((event) => event.name)).toEqual(
      ["fetch PUT /api/v1/thing"],
    );
  });

  it("lets init headers override a Request's own, as the browser does", async () => {
    const environment = install();
    const request = new Request(`${environment.origin}/api/v1/thing`, {
      headers: { "X-Panel": "from-request" },
    });

    await environment.fetchHost.fetch(request, {
      headers: { "X-Other": "from-init" },
    });

    const headers = new Headers(environment.fetchCalls[0].init?.headers);
    expect(headers.get("X-Panel")).toBeNull();
    expect(headers.get("X-Other")).toBe("from-init");
    expect(headers.get("traceparent")).toMatch(TRACEPARENT_PATTERN);
  });

  it("calls the original fetch as a plain function", async () => {
    const environment = install();
    const host = environment.fetchHost;
    installed?.uninstall();
    host.fetch = function (this: unknown) {
      if (this !== undefined && this !== globalThis)
        throw new TypeError("Illegal invocation");
      return Promise.resolve(jsonResponse({}));
    } as typeof fetch;
    installed = installProfiling({ ...environment.options, fetchHost: host });

    await expect(host.fetch("/api/v1/thing")).resolves.toBeInstanceOf(Response);
  });

  it("does not record the profile endpoint's own posts", async () => {
    const environment = install();

    await environment.fetchHost.fetch("/ui/api/profile/events", {
      method: "POST",
      body: "{}",
    });
    await environment.fetchHost.fetch("/ui/api/profile/chromium", {
      method: "POST",
    });

    for (const call of environment.fetchCalls)
      expect(new Headers(call.init?.headers).get("traceparent")).toBeNull();
    // Nothing was recorded, so the tick has no batch to post.
    const callCount = environment.fetchCalls.length;
    environment.clock.nowMs = 5000;
    environment.fireTimer();
    expect(environment.fetchCalls).toHaveLength(callCount);
  });

  it("records a rejected request by its error's name and rethrows", async () => {
    const environment = install();
    const failure = new TypeError("Failed to fetch");
    environment.respond = () => Promise.reject(failure);

    await expect(environment.fetchHost.fetch("/api/v1/thing")).rejects.toBe(
      failure,
    );

    environment.clock.nowMs = 5000;
    environment.fireTimer();
    expect(postedProfileEvents(environment)).toMatchObject([
      {
        name: "fetch GET /api/v1/thing",
        args: {
          method: "GET",
          path: "/api/v1/thing",
          error: "TypeError",
          is_failed: true,
        },
      },
    ]);
    expect(postedProfileEvents(environment)[0].args).not.toHaveProperty(
      "status",
    );
  });

  it("attaches the browser's timing to the request it belongs to", async () => {
    const environment = install();
    const url = `${environment.origin}/api/v1/readiness`;
    environment.clock.nowMs = 100;
    await environment.fetchHost.fetch("/api/v1/readiness");
    environment.clock.nowMs = 300;
    await environment.fetchHost.fetch("/api/v1/readiness");

    environment.deliverResourceEntries([
      {
        name: url,
        startTime: 301,
        requestStart: 305,
        responseStart: 320,
        responseEnd: 330,
      },
      {
        name: `${environment.origin}/other.js`,
        startTime: 100,
        requestStart: 1,
        responseStart: 2,
        responseEnd: 3,
      },
      {
        name: url,
        startTime: 900,
        requestStart: 1,
        responseStart: 2,
        responseEnd: 3,
      },
      {
        name: url,
        startTime: 102,
        requestStart: 110,
        responseStart: 150,
        responseEnd: 160,
      },
    ]);

    // Both spans post as soon as their timing is attached, without waiting.
    environment.fireTimer();
    expect(postedProfileEvents(environment)).toMatchObject([
      {
        start_ms: 300,
        args: {
          request_start_ms: 305,
          response_start_ms: 320,
          response_end_ms: 330,
        },
      },
      {
        start_ms: 100,
        args: {
          request_start_ms: 110,
          response_start_ms: 150,
          response_end_ms: 160,
        },
      },
    ]);
  });

  it("posts a request whose timing never arrives once the grace period passes", async () => {
    const environment = install();
    environment.clock.nowMs = 100;
    await environment.fetchHost.fetch("/api/v1/thing");

    environment.clock.nowMs = 1000;
    environment.fireTimer();
    expect(postedProfileEvents(environment)).toEqual([]);

    environment.clock.nowMs = 1100;
    environment.fireTimer();
    const events = postedProfileEvents(environment);
    expect(events).toHaveLength(1);
    expect(events[0].args).not.toHaveProperty("request_start_ms");

    // Timing that arrives for a request already posted is ignored.
    environment.deliverResourceEntries([
      {
        name: `${environment.origin}/api/v1/thing`,
        startTime: 101,
        requestStart: 1,
        responseStart: 2,
        responseEnd: 3,
      },
    ]);
    environment.fireTimer();
    expect(postedProfileEvents(environment)).toHaveLength(1);
  });

  it("posts a settled request on pagehide without waiting for its timing", async () => {
    const environment = install();
    await environment.fetchHost.fetch("/api/v1/thing");

    environment.firePageHide();

    expect(environment.beacons).toHaveLength(1);
    expect(postedProfileEvents(environment).map((event) => event.name)).toEqual(
      ["fetch GET /api/v1/thing"],
    );
  });
});

describe("while profiling is not installed", () => {
  it("span just runs fn, instant does nothing, and fetch is untouched", async () => {
    const measuresBefore = performance.getEntriesByType("measure").length;
    const marksBefore = performance.getEntriesByType("mark").length;
    const fetchBefore = globalThis.fetch;

    expect(span("share_panel.load", () => "value", { target: "web" })).toBe(
      "value",
    );
    await expect(
      span("share_panel.load", () => Promise.resolve(3)),
    ).resolves.toBe(3);
    expect(() =>
      span("share_panel.load", () => {
        throw new Error("boom");
      }),
    ).toThrow("boom");
    instant("share_panel.opened");

    expect(performance.getEntriesByType("measure")).toHaveLength(
      measuresBefore,
    );
    expect(performance.getEntriesByType("mark")).toHaveLength(marksBefore);
    expect(globalThis.fetch).toBe(fetchBefore);
  });

  it("uninstalling restores the fetch and stops the timer", () => {
    const environment = install();
    const wrapped = environment.fetchHost.fetch;

    installed?.uninstall();
    installed = null;

    expect(environment.fetchHost.fetch).not.toBe(wrapped);
    expect(() => environment.fireTimer()).toThrow("No flush timer is pending.");
    instant("after");
    expect(environment.marks).toEqual([]);
  });

  it("refuses a second install", () => {
    const environment = install();

    expect(() => installProfiling(environment.options)).toThrow(
      "already installed",
    );
  });
});
