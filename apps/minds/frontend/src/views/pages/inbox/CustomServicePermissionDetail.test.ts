import m from "mithril";
import { describe, expect, it } from "vitest";
import { InboxModel } from "../../../models/inbox";
import type { CustomServicePermissionDetail } from "../../../models/inbox";
import { panelIdFor } from "../../components/Collapsible";
import {
  CAPTURE_LOGIN_RISK_ATTR,
  CAPTURE_LOGIN_RISK_DETAIL,
  EXPERIMENTAL_CONNECTOR_RISK_ATTR,
} from "../../components/ConnectorRisk";
import { ICONS_16 } from "../../components/icons";
import { CustomServicePermissionDetailView } from "./CustomServicePermissionDetail";
import type { AnyVnode } from "../../../testing";
import {
  allText,
  attrsOf,
  byId,
  classesOf,
  collapsibleTrigger,
  collectText,
  collectVnodes,
  pressCollapsible,
  renderDeep,
  required,
  withAttr,
} from "../../../testing";

const DETAIL: CustomServicePermissionDetail = {
  kind: "custom_service",
  request_id: "evt-a",
  agent_id: "agent-1",
  ws_name: "alpha",
  rationale: "look up the part numbers",
  domain: "api.example.com",
  is_already_registered: false,
  domain_warning: null,
  base_api_url: "https://api.example.com/",
  login_url: null,
  is_system_browser_login_supported: false,
  is_capture_login: false,
  credential_header: "X-Api-Key: {token}",
  proxy: null,
};

interface ShellAttrs {
  headerLabel: string;
  headerNotice?: m.Children;
  body: m.Children;
}

// Render without a DOM: instantiate the closure component and call view() directly,
// as the sibling detail tests do. The root is a PermissionsShell vnode. One
// instance draws whichever request it is handed, as Mithril does when the
// request changes.
function mountShell(
  model: InboxModel = new InboxModel(),
): (shown: CustomServicePermissionDetail) => ShellAttrs {
  const instance =
    CustomServicePermissionDetailView() as unknown as m.Component;
  return (shown) => {
    const vnode = m(instance, {
      model,
      detail: shown,
    } as unknown as m.Attributes) as m.Vnode;
    const root = (instance.view as unknown as (v: m.Vnode) => m.Vnode).call(
      instance,
      vnode,
    );
    return root.attrs as unknown as ShellAttrs;
  };
}

function renderBody(
  detail: CustomServicePermissionDetail,
  model: InboxModel = new InboxModel(),
): {
  body: m.Children;
  toggle: () => void;
  /** Re-render the same instance, as Mithril does when the request changes. */
  rerender: (next?: CustomServicePermissionDetail) => m.Children;
} {
  const render = mountShell(model);
  const first = render(detail);
  const disclosure = collectVnodes(first.body).find(
    (vnode) => typeof vnode.attrs?.onToggle === "function",
  );
  return {
    body: first.body,
    toggle: () => (disclosure?.attrs as { onToggle: () => void }).onToggle(),
    rerender: (next = detail) => render(next).body,
  };
}

function findHeaderDisclosure(body: m.Children) {
  return collectVnodes(body).find(
    (vnode) => vnode.attrs?.summary === "How this will be sent",
  );
}

function isDisclosureOpen(body: m.Children): unknown {
  return findHeaderDisclosure(body)?.attrs?.isOpen;
}

describe("CustomServicePermissionDetailView", () => {
  it("keeps the header behind a collapsed disclosure and names it in the user's terms", () => {
    const rendered = renderBody(DETAIL);
    expect(findHeaderDisclosure(rendered.body)).toBeDefined();
    expect(isDisclosureOpen(rendered.body)).toBe(false);
    rendered.toggle();
    const opened = rendered.rerender();
    expect(isDisclosureOpen(opened)).toBe(true);
    const text = collectText(opened).join(" ");
    expect(text).toContain("X-Api-Key: <the token you paste>");
    expect(text).toContain(
      "Every request to https://api.example.com/ will carry the header",
    );
    // The raw placeholder is never shown to the user.
    expect(text).not.toContain("{token}");
  });

  it("scopes the open disclosure to the request it was opened on", () => {
    // One instance serves every request the user moves between (the view is
    // mounted unkeyed), so the state is keyed by request rather than shared.
    const rendered = renderBody(DETAIL);
    rendered.toggle();
    expect(isDisclosureOpen(rendered.rerender())).toBe(true);
    expect(
      isDisclosureOpen(rendered.rerender({ ...DETAIL, request_id: "evt-b" })),
    ).toBe(false);
    expect(isDisclosureOpen(rendered.rerender())).toBe(true);
  });

  it("shows no disclosure when a browser sign-in supplies the credential", () => {
    const rendered = renderBody({
      ...DETAIL,
      credential_header: null,
      login_url: "https://api.example.com/login",
    });
    expect(findHeaderDisclosure(rendered.body)).toBeUndefined();
    expect(collectText(rendered.body).join(" ")).toContain(
      "You will be sent to",
    );
  });

  it("draws no proxy switch for a request that asked for none", () => {
    const rendered = renderBody(DETAIL);
    expect(
      collectVnodes(rendered.body).filter(
        (vnode) => vnode.attrs?.role === "switch",
      ),
    ).toHaveLength(0);
    expect(collectText(rendered.body).join(" ")).not.toContain(
      "Proxy through my desktop",
    );
  });

  it("draws the proxy switch the agent asked for, naming the origin, and flips the model's choice", () => {
    const withProxy: CustomServicePermissionDetail = {
      ...DETAIL,
      proxy: {
        resulting_route: ["host-a", "self"],
        desktops: [{ device_id: "host-a", is_this_computer: true }],
      },
    };
    const model = new InboxModel();
    model.isProxyOn = true;
    const rendered = renderBody(withProxy, model);
    const text = collectText(rendered.body).join(" ");
    expect(text).toContain(
      "The agent asked for this workspace's requests to https://api.example.com/ to leave through this computer.",
    );
    expect(text).toContain("Requests will leave from");
    const switches = collectVnodes(rendered.body).filter(
      (vnode) => vnode.attrs?.role === "switch",
    );
    expect(switches).toHaveLength(1);
    expect(switches[0].attrs?.["aria-label"]).toBe("Proxy through my desktop");
    expect(switches[0].attrs?.["aria-checked"]).toBe("true");

    (switches[0].attrs?.onclick as () => void)();

    expect(model.isProxyOn).toBe(false);
    expect(collectText(rendered.rerender()).join(" ")).toContain(
      "Requests will keep leaving from where they do now.",
    );
  });

  it("spells the placeholder out as the thing the user pastes, wherever it appears", () => {
    const rendered = renderBody({
      ...DETAIL,
      credential_header: "X-Auth: {token}; sig={token}",
    });
    rendered.toggle();
    const text = collectText(rendered.rerender()).join(" ");
    expect(text).toContain(
      "X-Auth: <the token you paste>; sig=<the token you paste>",
    );
    expect(text).not.toContain("{token}");
  });
});

describe("CustomServicePermissionDetailView capture sign-in", () => {
  const CAPTURED: CustomServicePermissionDetail = {
    ...DETAIL,
    login_url: "https://api.example.com/login",
    is_capture_login: true,
    credential_header: null,
  };
  const RISK_ID = "custom-service-capture-login-risk";

  /** One mounted view, drawn for whichever request is shown: what sits under
   * the header as a reader would see it. */
  function mountHeaderNotice(): (shown: CustomServicePermissionDetail) => unknown {
    const shellFor = mountShell();
    return (shown) => renderDeep(shellFor(shown).headerNotice);
  }

  function riskOf(notice: unknown): AnyVnode {
    return withAttr(notice, CAPTURE_LOGIN_RISK_ATTR)[0];
  }

  it("notes under the header that the connection may ride an unofficial API, with why behind See more", () => {
    const noticeFor = mountHeaderNotice();

    const shut = riskOf(noticeFor(CAPTURED));
    expect(collectText(shut)).toEqual(["This connection may rely on an unofficial API.", " ", "See more"]);
    expect(attrsOf(collapsibleTrigger(shut, RISK_ID))["aria-expanded"]).toBe("false");

    pressCollapsible(shut, RISK_ID);
    const opened = riskOf(noticeFor(CAPTURED));
    expect(collectText(required(byId(opened, panelIdFor(RISK_ID))))).toEqual([CAPTURE_LOGIN_RISK_DETAIL]);
    expect(CAPTURE_LOGIN_RISK_DETAIL).toContain("Many connections like this work fine");

    pressCollapsible(opened, RISK_ID);
    expect(allText(riskOf(noticeFor(CAPTURED)))).not.toContain(CAPTURE_LOGIN_RISK_DETAIL);
  });

  it("draws it a tone quieter than an experimental connector's warning", () => {
    const notice = mountHeaderNotice()(CAPTURED);

    expect(withAttr(notice, EXPERIMENTAL_CONNECTOR_RISK_ATTR)).toEqual([]);
    expect(collectVnodes(notice).filter((vnode) => classesOf(vnode).includes("text-warning"))).toEqual([]);
    const glyphs = collectVnodes(notice).filter((vnode) => vnode.tag === "<");
    expect(glyphs.map((glyph) => glyph.children)).toEqual([ICONS_16["info"]]);
    expect(allText(notice)).not.toContain("risk");
  });

  it("starts the next request's note shut after one was opened", () => {
    const noticeFor = mountHeaderNotice();
    pressCollapsible(noticeFor(CAPTURED), RISK_ID);
    expect(allText(noticeFor(CAPTURED))).toContain(CAPTURE_LOGIN_RISK_DETAIL);

    expect(allText(noticeFor({ ...CAPTURED, request_id: "evt-b" }))).not.toContain(CAPTURE_LOGIN_RISK_DETAIL);
  });

  it("says nothing of the kind for a pasted token or a sign-in that captures nothing", () => {
    const noticeFor = mountHeaderNotice();

    expect(noticeFor(DETAIL)).toBeNull();
    expect(noticeFor({ ...CAPTURED, is_capture_login: false })).toBeNull();
  });
});
