import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChromeRequestCard, InboxBootIsland } from "../chrome_state";
import { resetHostForTesting } from "../host";
import { applyChromeEvent, resetStoreForTesting } from "../store";
import type { InboxListHandle } from "./InboxList";
import { mountInboxModal } from "./InboxList";

afterEach(() => {
  window.dispatchEvent(new Event("minds:page-teardown"));
  document.body.innerHTML = "";
  resetStoreForTesting();
  resetHostForTesting();
  delete window.__mindsNavigateContent;
  vi.unstubAllGlobals();
});

function card(id: string, displayName = "slack-api"): ChromeRequestCard {
  return { id, kind_label: "permission", ws_name: "ws-alpha", display_name: displayName, accent: "#112233" };
}

function island(cards: ChromeRequestCard[], selectedId = "", keepOpen = false): InboxBootIsland {
  return {
    chrome: {
      workspaces: {
        type: "workspaces",
        workspaces: [],
        destroying_agent_ids: [],
        destroying_status_by_agent_id: {},
        remote_workspace_states: {},
      },
      providers: { type: "providers_state", providers: [], last_event_at: null, last_full_snapshot_at: null },
      requests: {
        type: "requests",
        count: cards.length,
        request_ids: cards.map((entry) => entry.id),
        cards,
        auto_open: true,
      },
      system_interface_statuses: [],
    },
    inbox: { selected_id: selectedId, keep_open: keepOpen },
  };
}

// Builds the inbox page's island the way Inbox.jinja renders it, then mounts
// the whole modal. The browser host's EventSource and the global fetch are
// stubbed: jsdom implements neither; the detail fetch answers with the
// "unavailable" payload carrying the requested URL as its message so tests
// can assert which detail was fetched and rendered.
function mountFixture(bootIsland: InboxBootIsland): {
  handle: InboxListHandle;
  root: HTMLElement;
  left: HTMLElement;
  detail: HTMLElement;
  fetchCalls: string[];
} {
  const fetchCalls: string[] = [];
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    fetchCalls.push(`${init?.method ?? "GET"} ${url}${init?.body !== undefined ? ` ${String(init.body)}` : ""}`);
    return Promise.resolve(
      new Response(JSON.stringify({ detail: { kind: "unavailable", message: url } }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });
  vi.stubGlobal(
    "EventSource",
    class {
      addEventListener(): void {}
      close(): void {}
    },
  );
  const islandEl = document.createElement("script");
  islandEl.type = "application/json";
  islandEl.id = "minds-boot-state";
  islandEl.textContent = JSON.stringify(bootIsland);
  document.body.appendChild(islandEl);
  const root = document.createElement("div");
  root.id = "inbox-root";
  document.body.appendChild(root);
  const handle = mountInboxModal(root);
  m.redraw.sync();
  const left = root.querySelector("#inbox-left-column") as HTMLElement;
  const detail = root.querySelector("#inbox-detail") as HTMLElement;
  return { handle, root, left, detail, fetchCalls };
}

function flushAsync(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function cardIds(left: HTMLElement): string[] {
  return Array.from(left.querySelectorAll(".inbox-card")).map((el) => el.getAttribute("data-request-id") ?? "");
}

describe("InboxList rendering", () => {
  it("renders the island's cards with the boot selection highlighted", () => {
    const { left } = mountFixture(island([card("evt-1"), card("evt-2", "github-api")], "evt-1"));

    expect(cardIds(left)).toEqual(["evt-1", "evt-2"]);
    expect(left.querySelector('[data-request-id="evt-1"]')?.classList.contains("is-selected")).toBe(true);
    expect(left.querySelector('[data-request-id="evt-2"]')?.classList.contains("is-selected")).toBe(false);
    expect(left.querySelector(".inbox-card-kind")?.textContent).toBe("permission: ws-alpha");
    expect(left.querySelector(".inbox-card-display")?.textContent).toBe("slack-api");
  });

  it("shows the placeholder and collapses the body when empty, expanding on a push", () => {
    const { left } = mountFixture(island([]));

    expect(left.querySelector(".inbox-empty-placeholder")?.textContent).toBe("No pending requests.");
    expect(document.getElementById("inbox-body")?.classList.contains("is-empty")).toBe(true);

    applyChromeEvent({ type: "requests", count: 1, request_ids: ["evt-9"], cards: [card("evt-9")], auto_open: true });
    m.redraw.sync();

    expect(cardIds(left)).toEqual(["evt-9"]);
    expect(document.getElementById("inbox-body")?.classList.contains("is-empty")).toBe(false);
  });

  it("exposes the boot selection through the handle (null when unset)", () => {
    expect(mountFixture(island([card("evt-1")], "evt-1")).handle.getSelectedId()).toBe("evt-1");
    document.body.innerHTML = "";
    resetStoreForTesting();
    expect(mountFixture(island([card("evt-1")])).handle.getSelectedId()).toBeNull();
  });
});

describe("InboxList selection", () => {
  it("clicking a card fetches and renders its typed detail payload", async () => {
    const { root, fetchCalls } = mountFixture(island([card("evt-1"), card("evt-2")], "evt-1"));
    const left = root.querySelector("#inbox-left-column") as HTMLElement;

    (left.querySelector('[data-request-id="evt-2"]') as HTMLElement).dispatchEvent(new MouseEvent("click"));
    await flushAsync();
    m.redraw.sync();

    expect(fetchCalls).toContain("GET /inbox/detail/evt-2");
    // The stubbed payload renders through the typed detail view.
    const detail = root.querySelector("#inbox-detail") as HTMLElement;
    expect(detail.textContent).toContain("/inbox/detail/evt-2");
    expect(left.querySelector('[data-request-id="evt-2"]')?.classList.contains("is-selected")).toBe(true);
    expect(window.location.pathname + window.location.search).toBe("/inbox?selected=evt-2");
  });

  it("swaps in the unavailable payload when a push drops the selection", async () => {
    const { left, fetchCalls } = mountFixture(island([card("evt-1"), card("evt-2")], "evt-1", true));

    applyChromeEvent({ type: "requests", count: 1, request_ids: ["evt-2"], cards: [card("evt-2")], auto_open: true });
    await flushAsync();
    m.redraw.sync();

    // The server owns the "no longer available" copy: refetch the vanished id.
    expect(fetchCalls).toContain("GET /inbox/detail/evt-1");
    expect(left.querySelector(".is-selected")).toBeNull();
    expect(window.location.pathname + window.location.search).toBe("/inbox?keep_open=1");
  });
});

describe("InboxList resolution flow", () => {
  it("hides a granted card and advances to the next selectable one", async () => {
    const { handle, left, fetchCalls } = mountFixture(
      island([card("evt-1"), card("evt-2"), card("evt-3")], "evt-1", true),
    );
    handle.markDenying("evt-2");
    m.redraw.sync();
    expect(left.querySelector('[data-request-id="evt-2"]')?.classList.contains("is-denying")).toBe(true);

    await handle.advanceAfterResolution("evt-1");
    m.redraw.sync();

    // The granted card disappears immediately; the mid-deny card is skipped.
    expect(cardIds(left)).toEqual(["evt-2", "evt-3"]);
    expect(handle.getSelectedId()).toBe("evt-3");
    expect(fetchCalls).toContain("GET /inbox/detail/evt-3");
  });

  it("keeps a denying card visible until the push prunes it", () => {
    const { handle, left } = mountFixture(island([card("evt-1"), card("evt-2")], "evt-1", true));
    handle.markDenying("evt-1");
    m.redraw.sync();
    expect(cardIds(left)).toEqual(["evt-1", "evt-2"]);

    applyChromeEvent({ type: "requests", count: 1, request_ids: ["evt-2"], cards: [card("evt-2")], auto_open: true });
    m.redraw.sync();

    expect(cardIds(left)).toEqual(["evt-2"]);
  });

  it("dismisses instead of advancing when the inbox was opened for one request", async () => {
    const navigations: string[] = [];
    window.__mindsNavigateContent = (url) => navigations.push(url);
    const { handle, fetchCalls } = mountFixture(island([card("evt-1"), card("evt-2")], "evt-1", false));

    await handle.advanceAfterResolution("evt-1");

    expect(navigations).toEqual(["/"]);
    expect(fetchCalls).toEqual([]);
  });

  it("dismisses after resolving the last pending request", async () => {
    const navigations: string[] = [];
    window.__mindsNavigateContent = (url) => navigations.push(url);
    const { handle } = mountFixture(island([card("evt-1")], "evt-1", true));

    await handle.advanceAfterResolution("evt-1");

    expect(navigations).toEqual(["/"]);
  });
});

describe("InboxList teardown", () => {
  it("releases the store subscription on page teardown", async () => {
    const { fetchCalls } = mountFixture(island([card("evt-1")], "evt-1", true));
    window.dispatchEvent(new Event("minds:page-teardown"));
    fetchCalls.length = 0;

    // The selection vanishing after teardown must not trigger the torn-down
    // page's unavailable-fragment refetch.
    applyChromeEvent({ type: "requests", count: 0, request_ids: [], cards: [], auto_open: true });
    await flushAsync();

    expect(fetchCalls).toEqual([]);
  });
});

describe("InboxList auto-open toggle", () => {
  it("POSTs the flipped value and ignores echoing pushes", () => {
    const { left, fetchCalls } = mountFixture(island([card("evt-1")]));
    const checkbox = left.querySelector("#inbox-auto-open") as HTMLInputElement;
    expect(checkbox.checked).toBe(true);

    checkbox.checked = false;
    checkbox.dispatchEvent(new Event("change"));
    m.redraw.sync();

    expect(fetchCalls).toContain('POST /_chrome/requests-auto-open {"enabled":false}');
    // A later push still carrying auto_open=true (the SSE echo hasn't caught
    // up) must not clobber the user's click: the checkbox stays local.
    applyChromeEvent({ type: "requests", count: 1, request_ids: ["evt-1"], cards: [card("evt-1")], auto_open: true });
    m.redraw.sync();
    expect((left.querySelector("#inbox-auto-open") as HTMLInputElement).checked).toBe(false);
  });
});
