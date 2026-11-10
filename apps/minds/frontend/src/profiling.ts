// Renderer-side profiling: spans for the models' operations and for every
// same-origin request, batched and posted to the backend's trace file.
//
// Nothing here runs unless the bootstrap document says profiling is on, in
// which case it is installed once per page load. Each request carries a W3C
// `traceparent` header minted here, and its span's flow id is the header's
// span id, which is how the trace draws the arrow from the renderer's request
// span to the backend span that served it.
//
// Arguments are reviewed under the same rule as log lines: identifiers,
// counts, booleans, and paths without query strings; never a token, a cookie,
// a body, or an email address.

import { electronBridge } from "./electron-bridge";

export type SpanArgs = Record<string, string | number | boolean | null>;

/** One trace event as the events endpoint receives it; `start_ms` and
 * `duration_ms` are `performance.now()` milliseconds, which the backend
 * rebases with `time_origin_ms`. */
export interface ProfileEvent {
  kind: "span" | "instant";
  name: string;
  cat: string;
  start_ms: number;
  duration_ms: number;
  args: SpanArgs;
  flow_out_id: string | null;
  flow_in_id: string | null;
}

export type ResourceTimingEntry = Pick<
  PerformanceResourceTiming,
  "name" | "startTime" | "requestStart" | "responseStart" | "responseEnd"
>;

/** The part of `Performance` the profiler uses: the clock the events are
 * stamped on and the user-timing marks and measures they leave behind. */
export interface ProfilingPerformance {
  readonly timeOrigin: number;
  now(): number;
  mark(name: string, options?: { detail?: unknown }): unknown;
  measure(
    name: string,
    options: { start: string; end: string; detail?: unknown },
  ): unknown;
  clearMarks(name: string): void;
}

export interface ProfilingInstallOptions {
  /** The object whose `fetch` the wrapper replaces; the window by default. */
  fetchHost?: { fetch: typeof fetch };
  performance?: ProfilingPerformance;
  /** Subscribe to the browser's resource-timing entries; returns the unsubscribe. */
  observeResources?: (
    onEntries: (entries: ResourceTimingEntry[]) => void,
  ) => () => void;
  setTimer?: (callback: () => void, delayMs: number) => number;
  clearTimer?: (timerId: number) => void;
  /** Subscribe to the page being hidden; returns the unsubscribe. */
  onPageHide?: (handler: () => void) => () => void;
  sendBeacon?: (url: string, body: string) => boolean;
  randomBytes?: (byteCount: number) => Uint8Array;
  /** The page's origin, which decides which requests get the header and a span. */
  origin?: string;
  rendererPid?: number | null;
}

export interface ProfilingHandle {
  uninstall(): void;
}

const PROFILE_EVENTS_URL = "/ui/api/profile/events";
const PROFILE_URL_PREFIX = "/ui/api/profile/";
const FLUSH_INTERVAL_MS = 1000;
/** How long a settled request waits for the browser's timing entry, which
 * arrives only once the body has been read, before it is posted without it. */
const RESOURCE_TIMING_GRACE_MS = 1000;
/** How far the browser's start of a resource may sit from the wrapper's own
 * for the two to be the same request. */
const RESOURCE_TIMING_START_TOLERANCE_MS = 100;
/** The backend refuses a larger body. */
const MAX_BATCH_BYTES = 1_000_000;
const TRACE_ID_BYTES = 16;
const SPAN_ID_BYTES = 8;
const SESSION_ID_BYTES = 16;
const OPERATION_CATEGORY = "app";
const FETCH_CATEGORY = "fetch";

type ResolvedOptions = Required<ProfilingInstallOptions>;

interface OpenSpan {
  name: string;
  startMark: string;
  endMark: string;
  startMs: number;
}

interface ResourceTimingArgs {
  request_start_ms: number;
  response_start_ms: number;
  response_end_ms: number;
}

/** A same-origin request from the wrapper's call until its event is posted. */
interface RequestRecord {
  /** The resolved URL, query string included, for matching the browser's entry; never recorded. */
  url: string;
  startMs: number;
  event: ProfileEvent | null;
  settledAtMs: number | null;
  timing: ResourceTimingArgs | null;
}

interface BatchEnvelope {
  session_id: string;
  time_origin_ms: number;
  renderer_pid: number | null;
}

let active: ProfilingSession | null = null;

/** Start recording: wrap the page's fetch, watch resource timing, and post
 * the events every second and when the page hides. Call once per page load. */
export function installProfiling(
  options: ProfilingInstallOptions = {},
): ProfilingHandle {
  if (active !== null)
    throw new Error("Profiling is already installed on this page.");
  const session = new ProfilingSession({
    fetchHost: options.fetchHost ?? window,
    performance: options.performance ?? performance,
    observeResources: options.observeResources ?? observeResourceEntries,
    setTimer:
      options.setTimer ??
      ((callback, delayMs) => window.setTimeout(callback, delayMs)),
    clearTimer:
      options.clearTimer ?? ((timerId) => window.clearTimeout(timerId)),
    onPageHide: options.onPageHide ?? onWindowPageHide,
    sendBeacon:
      options.sendBeacon ?? ((url, body) => navigator.sendBeacon(url, body)),
    randomBytes:
      options.randomBytes ??
      ((byteCount) => crypto.getRandomValues(new Uint8Array(byteCount))),
    origin: options.origin ?? location.origin,
    rendererPid:
      options.rendererPid === undefined
        ? electronBridge.rendererPid
        : options.rendererPid,
  });
  active = session;
  return {
    uninstall: () => {
      session.stop();
      if (active === session) active = null;
    },
  };
}

/** Run `fn` as a named span, recording it as a user-timing measure and as an
 * event for the trace; a thrown or rejected `fn` is recorded as failed and
 * rethrown. Just calls `fn` while profiling is not installed. */
export function span<T>(name: string, fn: () => T, args?: SpanArgs): T {
  if (active === null) return fn();
  const session = active;
  const open = session.beginSpan(name);
  let result: T;
  try {
    result = fn();
  } catch (error) {
    session.endSpan(open, failedArgs(args));
    throw error;
  }
  if (isPromiseLike(result)) {
    return result.then(
      (value) => {
        session.endSpan(open, args ?? {});
        return value;
      },
      (error: unknown) => {
        session.endSpan(open, failedArgs(args));
        throw error;
      },
    ) as unknown as T;
  }
  session.endSpan(open, args ?? {});
  return result;
}

/** Record a point-in-time marker. Does nothing while profiling is not installed. */
export function instant(name: string, args?: SpanArgs): void {
  active?.recordInstant(name, args ?? {});
}

class ProfilingSession {
  private readonly events: ProfileEvent[] = [];
  private readonly openRequests: RequestRecord[] = [];
  private readonly envelope: BatchEnvelope;
  private readonly originalFetch: typeof fetch;
  private readonly stopObserving: () => void;
  private readonly stopListeningForPageHide: () => void;
  private flushTimerId: number | null = null;
  private spanSequence = 0;

  constructor(private readonly options: ResolvedOptions) {
    this.envelope = {
      session_id: this.randomHex(SESSION_ID_BYTES),
      time_origin_ms: options.performance.timeOrigin,
      renderer_pid: options.rendererPid,
    };
    this.originalFetch = options.fetchHost.fetch;
    options.fetchHost.fetch = (input, init) => this.profiledFetch(input, init);
    this.stopObserving = options.observeResources((entries) =>
      this.attachResourceEntries(entries),
    );
    this.stopListeningForPageHide = options.onPageHide(() =>
      this.flushOnPageHide(),
    );
    this.scheduleFlush();
  }

  stop(): void {
    this.options.fetchHost.fetch = this.originalFetch;
    if (this.flushTimerId !== null) this.options.clearTimer(this.flushTimerId);
    this.flushTimerId = null;
    this.stopObserving();
    this.stopListeningForPageHide();
  }

  beginSpan(name: string): OpenSpan {
    // The marks are numbered so that overlapping spans of one name measure
    // their own pair; they are cleared once measured.
    const sequence = ++this.spanSequence;
    const open = {
      name,
      startMark: `${name}#${sequence}.start`,
      endMark: `${name}#${sequence}.end`,
      startMs: this.options.performance.now(),
    };
    this.options.performance.mark(open.startMark);
    return open;
  }

  endSpan(open: OpenSpan, args: SpanArgs): void {
    const endMs = this.options.performance.now();
    this.options.performance.mark(open.endMark);
    this.options.performance.measure(open.name, {
      start: open.startMark,
      end: open.endMark,
      detail: args,
    });
    this.options.performance.clearMarks(open.startMark);
    this.options.performance.clearMarks(open.endMark);
    this.events.push({
      kind: "span",
      name: open.name,
      cat: OPERATION_CATEGORY,
      start_ms: open.startMs,
      duration_ms: endMs - open.startMs,
      args,
      flow_out_id: null,
      flow_in_id: null,
    });
  }

  recordInstant(name: string, args: SpanArgs): void {
    this.options.performance.mark(name, { detail: args });
    this.events.push({
      kind: "instant",
      name,
      cat: OPERATION_CATEGORY,
      start_ms: this.options.performance.now(),
      duration_ms: 0,
      args,
      flow_out_id: null,
      flow_in_id: null,
    });
  }

  private profiledFetch(
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> {
    // The browser's fetch checks its receiver, so the original is always
    // called as a plain function, never as a method of this session.
    const originalFetch = this.originalFetch;
    const url = this.profiledUrlOf(input);
    if (url === null) return originalFetch(input, init);

    const isRequest = input instanceof Request;
    const method = (
      init?.method ?? (isRequest ? input.method : "GET")
    ).toUpperCase();
    const traceId = this.randomHex(TRACE_ID_BYTES);
    const spanId = this.randomHex(SPAN_ID_BYTES);
    // init.headers, when given, replaces a Request's own headers entirely, so
    // the copy starts from whichever of the two the browser would have used.
    const headers = new Headers(
      init?.headers ?? (isRequest ? input.headers : undefined),
    );
    headers.set("traceparent", `00-${traceId}-${spanId}-01`);

    const record: RequestRecord = {
      url: url.href,
      startMs: this.options.performance.now(),
      event: null,
      settledAtMs: null,
      timing: null,
    };
    this.openRequests.push(record);
    const name = `fetch ${method} ${url.pathname}`;
    const args = {
      method,
      path: url.pathname,
      trace_id: traceId,
      span_id: spanId,
    };
    return originalFetch(input, { ...init, headers }).then(
      (response) => {
        this.settleRequest(record, name, { ...args, status: response.status });
        return response;
      },
      (error: unknown) => {
        this.settleRequest(record, name, {
          ...args,
          error: errorName(error),
          is_failed: true,
        });
        throw error;
      },
    );
  }

  /** The resolved URL of a request this session records, or null for a
   * cross-origin request, a profile post, or a URL the browser would refuse. */
  private profiledUrlOf(input: RequestInfo | URL): URL | null {
    const raw = input instanceof Request ? input.url : String(input);
    let url: URL;
    try {
      url = new URL(raw, this.options.origin);
    } catch {
      return null;
    }
    if (url.origin !== this.options.origin) return null;
    if (url.pathname.startsWith(PROFILE_URL_PREFIX)) return null;
    return url;
  }

  private settleRequest(
    record: RequestRecord,
    name: string,
    args: SpanArgs & { span_id: string },
  ): void {
    const endMs = this.options.performance.now();
    record.event = {
      kind: "span",
      name,
      cat: FETCH_CATEGORY,
      start_ms: record.startMs,
      duration_ms: endMs - record.startMs,
      args,
      flow_out_id: args.span_id,
      flow_in_id: null,
    };
    record.settledAtMs = endMs;
    if (record.timing !== null) this.releaseRequest(record);
  }

  private attachResourceEntries(entries: ResourceTimingEntry[]): void {
    for (const entry of entries) {
      const record = this.closestOpenRequest(entry);
      if (record === null) continue;
      record.timing = {
        request_start_ms: entry.requestStart,
        response_start_ms: entry.responseStart,
        response_end_ms: entry.responseEnd,
      };
      if (record.event !== null) this.releaseRequest(record);
    }
  }

  private closestOpenRequest(entry: ResourceTimingEntry): RequestRecord | null {
    let closest: RequestRecord | null = null;
    let closestDistanceMs = Infinity;
    for (const record of this.openRequests) {
      if (record.timing !== null || record.url !== entry.name) continue;
      const distanceMs = Math.abs(entry.startTime - record.startMs);
      if (
        distanceMs <= RESOURCE_TIMING_START_TOLERANCE_MS &&
        distanceMs < closestDistanceMs
      ) {
        closest = record;
        closestDistanceMs = distanceMs;
      }
    }
    return closest;
  }

  /** Move a settled request's event, with whatever timing it got, to the batch. */
  private releaseRequest(record: RequestRecord): void {
    const idx = this.openRequests.indexOf(record);
    if (idx === -1 || record.event === null) return;
    this.openRequests.splice(idx, 1);
    if (record.timing !== null)
      record.event.args = { ...record.event.args, ...record.timing };
    this.events.push(record.event);
  }

  private scheduleFlush(): void {
    this.flushTimerId = this.options.setTimer(
      () => this.flushOnTimer(),
      FLUSH_INTERVAL_MS,
    );
  }

  private flushOnTimer(): void {
    this.flushTimerId = null;
    const nowMs = this.options.performance.now();
    for (const record of [...this.openRequests]) {
      if (
        record.settledAtMs !== null &&
        nowMs - record.settledAtMs >= RESOURCE_TIMING_GRACE_MS
      )
        this.releaseRequest(record);
    }
    const originalFetch = this.originalFetch;
    for (const body of this.drainBodies()) {
      void originalFetch(PROFILE_EVENTS_URL, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body,
      }).catch(() => undefined);
    }
    this.scheduleFlush();
  }

  private flushOnPageHide(): void {
    for (const record of [...this.openRequests]) this.releaseRequest(record);
    for (const body of this.drainBodies())
      this.options.sendBeacon(PROFILE_EVENTS_URL, body);
  }

  private drainBodies(): string[] {
    if (this.events.length === 0) return [];
    return encodeBatches(this.envelope, this.events.splice(0), MAX_BATCH_BYTES);
  }

  private randomHex(byteCount: number): string {
    const bytes = this.options.randomBytes(byteCount);
    // The W3C trace context forbids the all-zero id.
    if (bytes.every((byte) => byte === 0)) bytes[bytes.length - 1] = 1;
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
      "",
    );
  }
}

/** The request bodies carrying `events` in order, each at most `maxBytes`
 * (an event too large for a body of its own is sent alone regardless). */
function encodeBatches(
  envelope: BatchEnvelope,
  events: ProfileEvent[],
  maxBytes: number,
): string[] {
  const encoder = new TextEncoder();
  const byteLength = (text: string): number => encoder.encode(text).byteLength;
  const prefix = JSON.stringify({ ...envelope, events: [] }).slice(0, -2);
  const framingBytes = byteLength(prefix) + 2;
  const close = (chunk: string[]): string => `${prefix}${chunk.join(",")}]}`;
  const bodies: string[] = [];
  let chunk: string[] = [];
  let chunkBytes = framingBytes;
  for (const event of events) {
    const encoded = JSON.stringify(event);
    const encodedBytes = byteLength(encoded);
    if (chunk.length > 0 && chunkBytes + 1 + encodedBytes > maxBytes) {
      bodies.push(close(chunk));
      chunk = [];
      chunkBytes = framingBytes;
    }
    chunkBytes += (chunk.length > 0 ? 1 : 0) + encodedBytes;
    chunk.push(encoded);
  }
  if (chunk.length > 0) bodies.push(close(chunk));
  return bodies;
}

function observeResourceEntries(
  onEntries: (entries: ResourceTimingEntry[]) => void,
): () => void {
  const observer = new PerformanceObserver((list) =>
    onEntries(list.getEntries() as PerformanceResourceTiming[]),
  );
  observer.observe({ type: "resource" });
  return () => observer.disconnect();
}

function onWindowPageHide(handler: () => void): () => void {
  window.addEventListener("pagehide", handler);
  return () => window.removeEventListener("pagehide", handler);
}

function failedArgs(args: SpanArgs | undefined): SpanArgs {
  return { ...args, is_failed: true };
}

function errorName(error: unknown): string {
  return error instanceof Error ? error.name : typeof error;
}

function isPromiseLike(value: unknown): value is PromiseLike<unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { then?: unknown }).then === "function"
  );
}
