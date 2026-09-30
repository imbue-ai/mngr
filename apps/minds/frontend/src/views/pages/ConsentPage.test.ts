import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { REPORTING_CONSENT_QUESTION } from "../../models/onboarding";
import { attrsOf, collectText, collectVnodes } from "../../testing";
import { ConsentPage } from "./ConsentPage";

function renderPage(): { view: () => unknown } {
  const component = (ConsentPage as () => m.Component)();
  return { view: () => (component.view as () => unknown)() };
}

function byId(tree: unknown, id: string): Record<string, unknown> {
  const vnode = collectVnodes(tree).find((candidate) => attrsOf(candidate).id === id);
  if (vnode === undefined) throw new Error(`no element #${id}`);
  return attrsOf(vnode);
}

async function continueAndReadPostedBody(page: { view: () => unknown }): Promise<unknown> {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(m.route, "set").mockImplementation(() => {});
  vi.spyOn(m, "redraw").mockImplementation(() => {});
  (byId(page.view(), "consent-continue").onclick as () => void)();
  await vi.waitFor(() => expect(m.route.set).toHaveBeenCalledWith("/"));
  const init = fetchMock.mock.calls[0]?.[1];
  return JSON.parse(String(init?.body));
}

describe("ConsentPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("asks its one question with a checkbox that starts checked", () => {
    const tree = renderPage().view();
    expect(collectText(tree)).toContain(REPORTING_CONSENT_QUESTION);
    expect(byId(tree, "consent-reporting-checkbox").checked).toBe(true);
  });

  it("keeps reporting on when the user simply continues", async () => {
    expect(await continueAndReadPostedBody(renderPage())).toEqual({ report_unexpected_errors: true });
  });

  it("turns reporting off when the user unchecks the box before continuing", async () => {
    const page = renderPage();
    const onchange = byId(page.view(), "consent-reporting-checkbox").onchange as (event: Event) => void;
    onchange({ target: { checked: false } } as unknown as Event);
    expect(byId(page.view(), "consent-reporting-checkbox").checked).toBe(false);
    expect(await continueAndReadPostedBody(page)).toEqual({ report_unexpected_errors: false });
  });
});
