import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SharingBootExtras } from "../chrome_state";
import { mountSharingEditor } from "./SharingEditor";

const AGENT = "agent-" + "c".repeat(32);
const STATUS_URL = `/api/v1/workspaces/${AGENT}/sharing/web`;

afterEach(() => {
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function extras(overrides: Partial<SharingBootExtras> = {}): SharingBootExtras {
  return {
    agent_id: AGENT,
    service_name: "web",
    ws_name: "ws-gamma",
    account_email: "alice@example.com",
    initial_emails: [],
    is_modal: false,
    mngr_forward_origin: "https://localhost:8421",
    ...overrides,
  };
}

interface FakeRoutes {
  // "METHOD url" -> a response factory; called once per matching request.
  [key: string]: () => Response;
}

function stubFetch(routes: FakeRoutes): Array<{ key: string; body: string | null }> {
  const calls: Array<{ key: string; body: string | null }> = [];
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${url}`;
    calls.push({ key, body: typeof init?.body === "string" ? init.body : null });
    const route = routes[key];
    if (route === undefined) return Promise.resolve(new Response("not found", { status: 404 }));
    return Promise.resolve(route());
  });
  return calls;
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}

function mountFixture(bootExtras: SharingBootExtras, onDismiss?: () => void): { root: HTMLElement; heading: HTMLElement } {
  const island = document.createElement("script");
  island.type = "application/json";
  island.id = "minds-boot-state";
  island.textContent = JSON.stringify({ sharing: bootExtras });
  document.body.appendChild(island);
  const heading = document.createElement("h1");
  heading.id = "page-heading";
  document.body.appendChild(heading);
  const root = document.createElement("div");
  root.id = "sharing-editor-root";
  document.body.appendChild(root);
  mountSharingEditor(root, onDismiss !== undefined ? { onDismiss } : undefined);
  return { root, heading };
}

// Flush the pending fetch/json promise chains, then force the redraw
// mithril scheduled (jsdom's requestAnimationFrame is too slow to await).
async function flushAsync(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  m.redraw.sync();
}

function aclEmails(root: HTMLElement): string[] {
  return Array.from(root.querySelectorAll("#email-list .type-body")).map((el) => el.textContent ?? "");
}

describe("SharingEditor loading", () => {
  it("shows Loading until the status lands, then the enabled state with existing rows", async () => {
    stubFetch({
      [`GET ${STATUS_URL}`]: () =>
        jsonResponse({ enabled: true, url: "https://ws.example.com", policy: { emails: ["a@x.com", "b@x.com"] } }),
      [`GET ${STATUS_URL}/readiness?url=${encodeURIComponent("https://ws.example.com")}`]: () =>
        jsonResponse({ ready: true }),
    });
    const { root } = mountFixture(extras());
    expect(root.querySelector("#loading-state")?.textContent).toBe("Loading...");

    await flushAsync();
    await flushAsync();

    expect(aclEmails(root)).toEqual(["a@x.com", "b@x.com"]);
    expect(root.querySelector("#action-btn")?.textContent).toBe("Update");
    expect(root.querySelector("#disable-btn")).not.toBeNull();
    // The readiness probe answered ready, so the URL is revealed.
    expect((root.querySelector("#share-url") as HTMLInputElement).value).toBe("https://ws.example.com");
    expect(root.querySelector("#url-fallback-note")).toBeNull();
  });

  it("pre-populates the owner draft and folds initial emails once when disabled", async () => {
    stubFetch({
      [`GET ${STATUS_URL}`]: () => jsonResponse({ enabled: false, policy: { emails: ["alice@example.com"] } }),
    });
    const { root } = mountFixture(extras({ initial_emails: ["bob@x.com"] }));
    await flushAsync();

    // Owner default + URL-proposed email both appear as added drafts.
    expect(aclEmails(root)).toEqual(["alice@example.com", "bob@x.com"]);
    expect(root.querySelector("#action-btn")?.textContent).toBe("Share");
    expect(root.querySelector("#disable-btn")).toBeNull();
    expect(root.querySelector("#url-section")).toBeNull();
  });

  it("keeps the editor usable with drafts when the status fetch fails", async () => {
    stubFetch({ [`GET ${STATUS_URL}`]: () => jsonResponse({ error: "gateway down" }, 502) });
    const { root } = mountFixture(extras({ initial_emails: ["draft@x.com"] }));
    await flushAsync();

    expect(root.querySelector("#loading-state")?.textContent).toBe("Failed to load sharing status: gateway down");
    expect(aclEmails(root)).toEqual(["draft@x.com"]);
  });
});

describe("SharingEditor heading", () => {
  it("links the workspace and account on the full page, flipping wording when enabled", async () => {
    stubFetch({
      [`GET ${STATUS_URL}`]: () => jsonResponse({ enabled: true, policy: { emails: [] } }),
    });
    const { heading } = mountFixture(extras());
    // Pre-load: the question form, mirroring the server-rendered heading.
    expect(heading.textContent).toBe("Share web in ws-gamma (alice@example.com)?");

    await flushAsync();

    expect(heading.textContent).toBe("web shared in ws-gamma (alice@example.com)");
    const links = Array.from(heading.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(links).toEqual([`https://localhost:8421/goto/${AGENT}/`, "/accounts"]);
  });

  it("renders plain text in the modal so nothing can navigate the overlay iframe", async () => {
    stubFetch({ [`GET ${STATUS_URL}`]: () => jsonResponse({ enabled: false, policy: { emails: [] } }) });
    const { heading } = mountFixture(extras({ is_modal: true, mngr_forward_origin: "" }));
    await flushAsync();

    expect(heading.textContent).toBe("Share web in ws-gamma (alice@example.com)?");
    expect(heading.querySelector("a")).toBeNull();
  });
});

describe("SharingEditor access list", () => {
  async function mountEnabled(): Promise<HTMLElement> {
    stubFetch({
      [`GET ${STATUS_URL}`]: () => jsonResponse({ enabled: true, policy: { emails: ["a@x.com"] } }),
    });
    const { root } = mountFixture(extras());
    await flushAsync();
    return root;
  }

  it("adds a draft via the input, then unmarks it via its remove button", async () => {
    const root = await mountEnabled();
    const input = root.querySelector("#new-email") as HTMLInputElement;
    input.value = "new@x.com";
    input.dispatchEvent(new Event("input"));
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    await flushAsync();

    expect(aclEmails(root)).toEqual(["a@x.com", "new@x.com"]);
    expect(input.value).toBe("");

    // The added row's remove button drops the draft entirely.
    const addedRow = Array.from(root.querySelectorAll("#email-list > div"))[1];
    (addedRow.querySelector("button[aria-label='Remove']") as HTMLElement).click();
    await flushAsync();
    expect(aclEmails(root)).toEqual(["a@x.com"]);
  });

  it("marks an existing email removed (strikethrough) and un-marks it", async () => {
    const root = await mountEnabled();
    (root.querySelector("#email-list button[aria-label='Remove']") as HTMLElement).click();
    await flushAsync();

    const removedRow = root.querySelector("#email-list > div") as HTMLElement;
    expect(removedRow.className).toContain("line-through");

    (removedRow.querySelector("button[aria-label='Remove']") as HTMLElement).click();
    await flushAsync();
    expect((root.querySelector("#email-list > div") as HTMLElement).className).not.toContain("line-through");
  });

  it("re-adding a removed email just un-marks it", async () => {
    const root = await mountEnabled();
    (root.querySelector("#email-list button[aria-label='Remove']") as HTMLElement).click();
    const input = root.querySelector("#new-email") as HTMLInputElement;
    input.value = "a@x.com";
    input.dispatchEvent(new Event("input"));
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    await flushAsync();

    expect(aclEmails(root)).toEqual(["a@x.com"]);
    expect((root.querySelector("#email-list > div") as HTMLElement).className).not.toContain("line-through");
  });
});

describe("SharingEditor submission", () => {
  it("PUTs the final email set and refreshes the state in place", async () => {
    let statusCalls = 0;
    const calls = stubFetch({
      [`GET ${STATUS_URL}`]: () => {
        statusCalls += 1;
        return statusCalls === 1
          ? jsonResponse({ enabled: false, policy: { emails: ["alice@example.com"] } })
          : jsonResponse({ enabled: true, url: "https://ws.example.com", policy: { emails: ["alice@example.com"] } });
      },
      [`PUT ${STATUS_URL}`]: () => jsonResponse({ ok: true }),
      [`GET ${STATUS_URL}/readiness?url=${encodeURIComponent("https://ws.example.com")}`]: () =>
        jsonResponse({ ready: true }),
    });
    const { root } = mountFixture(extras());
    await flushAsync();

    (root.querySelector("#action-btn") as HTMLElement).click();
    await flushAsync();
    await flushAsync();

    const put = calls.find((call) => call.key === `PUT ${STATUS_URL}`);
    expect(put?.body).toBe(JSON.stringify({ emails: ["alice@example.com"] }));
    // The in-place refresh flipped the editor to the enabled state.
    expect(root.querySelector("#action-btn")?.textContent).toBe("Update");
    expect(root.querySelector("#disable-btn")).not.toBeNull();
  });

  it("shows the server's inline error and re-enables the editor on failure", async () => {
    stubFetch({
      [`GET ${STATUS_URL}`]: () => jsonResponse({ enabled: false, policy: { emails: [] } }),
      [`PUT ${STATUS_URL}`]: () => jsonResponse({ error: "cloudflare exploded" }, 502),
    });
    const { root } = mountFixture(extras());
    await flushAsync();

    (root.querySelector("#action-btn") as HTMLElement).click();
    await flushAsync();
    await flushAsync();

    expect(root.querySelector("#sharing-error")?.textContent).toBe(
      "Could not save sharing changes: cloudflare exploded",
    );
    expect(root.querySelector("#submit-spinner")).toBeNull();
    expect(root.querySelector("#action-buttons")).not.toBeNull();
  });

  it("DELETEs on Disable and lands back in the disabled state", async () => {
    let statusCalls = 0;
    const calls = stubFetch({
      [`GET ${STATUS_URL}`]: () => {
        statusCalls += 1;
        return statusCalls === 1
          ? jsonResponse({ enabled: true, policy: { emails: ["a@x.com"] } })
          : jsonResponse({ enabled: false, policy: { emails: ["alice@example.com"] } });
      },
      [`DELETE ${STATUS_URL}`]: () => jsonResponse({ ok: true }),
    });
    const { root } = mountFixture(extras());
    await flushAsync();

    (root.querySelector("#disable-btn") as HTMLElement).click();
    await flushAsync();
    await flushAsync();

    expect(calls.some((call) => call.key === `DELETE ${STATUS_URL}`)).toBe(true);
    expect(root.querySelector("#action-btn")?.textContent).toBe("Share");
    expect(root.querySelector("#disable-btn")).toBeNull();
    expect(root.querySelector("#url-section")).toBeNull();
  });
});

describe("SharingEditor modal cancel", () => {
  it("routes Cancel through the dismiss callback instead of a settings link", async () => {
    stubFetch({ [`GET ${STATUS_URL}`]: () => jsonResponse({ enabled: false, policy: { emails: [] } }) });
    const dismissals: number[] = [];
    const { root } = mountFixture(extras({ is_modal: true, mngr_forward_origin: "" }), () => dismissals.push(1));
    await flushAsync();

    expect(root.querySelector(`a[href="/workspace/${AGENT}/settings"]`)).toBeNull();
    const cancel = Array.from(root.querySelectorAll("#action-buttons button")).find(
      (el) => el.textContent === "Cancel",
    ) as HTMLElement;
    cancel.click();

    expect(dismissals).toEqual([1]);
  });
});

describe("SharingEditor readiness polling", () => {
  it("keeps the URL hidden behind the provisioning spinner until the edge is ready", async () => {
    vi.useFakeTimers();
    let readinessCalls = 0;
    stubFetch({
      [`GET ${STATUS_URL}`]: () =>
        jsonResponse({ enabled: true, url: "https://ws.example.com", policy: { emails: [] } }),
      [`GET ${STATUS_URL}/readiness?url=${encodeURIComponent("https://ws.example.com")}`]: () => {
        readinessCalls += 1;
        return jsonResponse({ ready: readinessCalls >= 2 });
      },
    });
    const { root } = mountFixture(extras());
    await vi.advanceTimersByTimeAsync(0);
    m.redraw.sync();

    // First probe answered not-ready: still provisioning.
    expect(root.querySelector("#url-provisioning")).not.toBeNull();
    expect(root.querySelector("#url-ready")).toBeNull();

    // The 2s retry fires and the second probe answers ready.
    await vi.advanceTimersByTimeAsync(2000);
    m.redraw.sync();

    expect(root.querySelector("#url-ready")).not.toBeNull();
    expect(root.querySelector("#url-provisioning")).toBeNull();
  });

  it("a late poll result cannot resurface the URL section after Disable", async () => {
    vi.useFakeTimers();
    let statusCalls = 0;
    stubFetch({
      [`GET ${STATUS_URL}`]: () => {
        statusCalls += 1;
        return statusCalls === 1
          ? jsonResponse({ enabled: true, url: "https://ws.example.com", policy: { emails: [] } })
          : jsonResponse({ enabled: false, policy: { emails: [] } });
      },
      // The edge never answers ready, so the poll loop keeps rescheduling.
      [`GET ${STATUS_URL}/readiness?url=${encodeURIComponent("https://ws.example.com")}`]: () =>
        jsonResponse({ ready: false }),
      [`DELETE ${STATUS_URL}`]: () => jsonResponse({ ok: true }),
    });
    const { root } = mountFixture(extras());
    await vi.advanceTimersByTimeAsync(0);
    m.redraw.sync();
    expect(root.querySelector("#url-provisioning")).not.toBeNull();

    // Disable while the readiness poll's 2s retry is still pending.
    (root.querySelector("#disable-btn") as HTMLElement).click();
    await vi.advanceTimersByTimeAsync(0);
    m.redraw.sync();
    expect(root.querySelector("#url-section")).toBeNull();

    // Run the abandoned loop well past its max-attempts fallback: the stale
    // poll must not flip urlPhase back to "ready" in the disabled editor.
    await vi.advanceTimersByTimeAsync(30000);
    m.redraw.sync();
    expect(root.querySelector("#url-section")).toBeNull();
    expect(root.querySelector("#url-ready")).toBeNull();
  });

  it("stops probing when the page mount is torn down mid-provisioning", async () => {
    vi.useFakeTimers();
    let readinessCalls = 0;
    stubFetch({
      [`GET ${STATUS_URL}`]: () =>
        jsonResponse({ enabled: true, url: "https://ws.example.com", policy: { emails: [] } }),
      // The edge never answers ready, so the poll loop keeps rescheduling.
      [`GET ${STATUS_URL}/readiness?url=${encodeURIComponent("https://ws.example.com")}`]: () => {
        readinessCalls += 1;
        return jsonResponse({ ready: false });
      },
    });
    mountFixture(extras());
    await vi.advanceTimersByTimeAsync(0);
    expect(readinessCalls).toBe(1);

    // A swap-engine page swap tears the mount down while the 2s retry is
    // pending; the released loop must not keep hitting the probe endpoint.
    window.dispatchEvent(new Event("minds:page-teardown"));
    await vi.advanceTimersByTimeAsync(30000);
    expect(readinessCalls).toBe(1);
  });
});
