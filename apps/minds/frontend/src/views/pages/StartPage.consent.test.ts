// The start flow saves the error-reporting checkbox with the run question's answer. A file of its own because the
// existing-login answer starts the browser sign-in, whose module is replaced here.
import type m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import { attrsOf, collectVnodes } from "../../testing";
import { ShellState } from "../shell/shell-state";
import { StartPage } from "./StartPage";

vi.mock("../../models/webLogin", () => ({ webLogin: { start: vi.fn(async () => {}), email: "" } }));

const CONSENT_URL = "/ui/api/onboarding/consent";

function attrsWith(tree: unknown, name: string, value: string): Record<string, unknown> {
  const vnode = collectVnodes(tree).find((candidate) => attrsOf(candidate)[name] === value);
  if (vnode === undefined) throw new Error(`nothing with ${name}=${value}`);
  return attrsOf(vnode);
}

/** The start page with its questions begun, so the run question and its checkbox are showing. */
function renderStartedPage(): () => unknown {
  const component = (StartPage as unknown as (vnode: unknown) => m.Component)({});
  const view = (): unknown => (component.view as () => unknown)();
  (attrsWith(view(), "data-answer", "continue").onclick as () => void)();
  return view;
}

describe("StartPage saving the error-reporting checkbox", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    const shell = new ShellState(createEmptyStores());
    registerAppContext({ stores: shell.stores, shell });
    fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    clearAppContextForTests();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function postedConsentBody(): Promise<unknown> {
    const consentCalls = (): unknown[][] => fetchMock.mock.calls.filter(([url]) => url === CONSENT_URL);
    await vi.waitFor(() => expect(consentCalls()).toHaveLength(1));
    const init = consentCalls()[0]?.[1] as RequestInit | undefined;
    return JSON.parse(String(init?.body));
  }

  it("saves the checkbox as it stands with an answer to the run question", async () => {
    const view = renderStartedPage();

    (attrsWith(view(), "data-answer", "custom").onclick as () => void)();

    expect(await postedConsentBody()).toEqual({ report_unexpected_errors: true });
  });

  it("saves an unchecked box with the existing-login answer", async () => {
    const view = renderStartedPage();
    const onchange = attrsWith(view(), "id", "start-reporting-consent").onchange as (event: Event) => void;
    onchange({ target: { checked: false } } as unknown as Event);

    (attrsWith(view(), "data-aside", "").onclick as () => void)();

    expect(await postedConsentBody()).toEqual({ report_unexpected_errors: false });
  });
});
