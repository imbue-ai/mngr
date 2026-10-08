// The reporting question that closes the manifesto: what it says, what "See more" opens, and that either answer is
// saved before the questions begin. A file of its own because it drives the live page, whose sign-in module is
// replaced here.
import type m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import {
  REPORTING_ACCEPT_LABEL,
  REPORTING_ASK,
  REPORTING_DECLINE_LABEL,
  REPORTING_MORE_DETAIL,
  REPORTING_MORE_LABEL,
} from "../../models/startFlow";
import { allText, attrsOf, collectVnodes } from "../../testing";
import { ShellState } from "../shell/shell-state";
import { StartPage } from "./StartPage";

vi.mock("../../models/webLogin", () => ({ webLogin: { start: vi.fn(async () => {}), email: "" } }));

const CONSENT_URL = "/ui/api/onboarding/consent";

function attrsWith(tree: unknown, name: string, value: string): Record<string, unknown> {
  const vnode = collectVnodes(tree).find((candidate) => attrsOf(candidate)[name] === value);
  if (vnode === undefined) throw new Error(`nothing with ${name}=${value}`);
  return attrsOf(vnode);
}

function renderPage(): () => unknown {
  const component = (StartPage as unknown as (vnode: unknown) => m.Component)({});
  return (): unknown => (component.view as () => unknown)();
}

/** The live page streams its agent turns one character per span, so its text reads back spaced out. */
function unspaced(value: unknown): string {
  return (typeof value === "string" ? value : allText(value)).replace(/\s+/g, "");
}

describe("the manifesto's reporting question", () => {
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

  it("asks it after the manifesto, with both answers and nothing else to press", () => {
    const view = renderPage();

    expect(unspaced(view())).toContain(unspaced(REPORTING_ASK));
    expect(allText(view())).toContain(REPORTING_MORE_LABEL);
    const answers = collectVnodes(view())
      .map((node) => attrsOf(node)["data-answer"])
      .filter((answer) => answer !== undefined);
    expect(answers).toEqual(["reporting-no", "reporting-yes"]);
  });

  it("keeps what it collects behind See more until it is pressed", () => {
    const view = renderPage();
    expect(allText(view())).not.toContain(REPORTING_MORE_DETAIL);

    (attrsWith(view(), "id", "start-reporting-more").onclick as () => void)();

    expect(allText(view())).toContain(REPORTING_MORE_DETAIL);
  });

  it("saves the yes and opens the where-to-run question", async () => {
    const view = renderPage();

    (attrsWith(view(), "data-answer", "reporting-yes").onclick as () => void)();

    expect(await postedConsentBody()).toEqual({ report_unexpected_errors: true });
    expect(allText(view())).toContain(REPORTING_ACCEPT_LABEL);
    expect(unspaced(view())).toContain(unspaced("How do you want to run it?"));
  });

  it("saves the no, and says the no rather than a press nobody made", async () => {
    const view = renderPage();

    (attrsWith(view(), "data-answer", "reporting-no").onclick as () => void)();

    expect(await postedConsentBody()).toEqual({ report_unexpected_errors: false });
    expect(allText(view())).toContain(REPORTING_DECLINE_LABEL);
    expect(unspaced(view())).toContain(unspaced("How do you want to run it?"));
  });

  it("can be taken back and answered the other way, which saves the new answer", async () => {
    const view = renderPage();
    (attrsWith(view(), "data-answer", "reporting-yes").onclick as () => void)();
    await postedConsentBody();

    (attrsWith(view(), "aria-label", "Change answer").onclick as () => void)();
    expect(unspaced(view())).not.toContain(unspaced("How do you want to run it?"));
    (attrsWith(view(), "data-answer", "reporting-no").onclick as () => void)();

    const consentCalls = (): unknown[][] => fetchMock.mock.calls.filter(([url]) => url === CONSENT_URL);
    await vi.waitFor(() => expect(consentCalls()).toHaveLength(2));
    const init = consentCalls()[1]?.[1] as RequestInit | undefined;
    expect(JSON.parse(String(init?.body))).toEqual({ report_unexpected_errors: false });
    expect(allText(view())).toContain(REPORTING_DECLINE_LABEL);
  });

  it("asks the next question afresh after the reporting answer is taken back", () => {
    const view = renderPage();
    const runButtonsDelay = (): string =>
      String(
        collectVnodes(view())
          .filter((node) => collectVnodes(node).some((inner) => attrsOf(inner)["data-answer"] === "cloud"))
          .map((node) => attrsOf(node).style)
          .find((style) => typeof style === "string"),
      );
    (attrsWith(view(), "data-answer", "reporting-yes").onclick as () => void)();
    (attrsWith(view(), "data-answer", "cloud").onclick as () => void)();
    const undos = collectVnodes(view()).filter((node) => attrsOf(node)["aria-label"] === "Change answer");
    (attrsOf(undos[1]).onclick as () => void)();
    expect(runButtonsDelay()).toContain("--start-chat-delay: 0ms");

    (attrsWith(view(), "aria-label", "Change answer").onclick as () => void)();
    (attrsWith(view(), "data-answer", "reporting-yes").onclick as () => void)();

    expect(runButtonsDelay()).toMatch(/--start-chat-delay: [1-9]\d+ms/);
  });

  it("is answered once: the where-to-run answer saves nothing more", async () => {
    const view = renderPage();
    (attrsWith(view(), "data-answer", "reporting-yes").onclick as () => void)();
    await postedConsentBody();

    (attrsWith(view(), "data-answer", "custom").onclick as () => void)();

    expect(fetchMock.mock.calls.filter(([url]) => url === CONSENT_URL)).toHaveLength(1);
  });
});
