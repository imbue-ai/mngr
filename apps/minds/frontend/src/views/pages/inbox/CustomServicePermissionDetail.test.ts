import m from "mithril";
import { describe, expect, it } from "vitest";
import { InboxModel } from "../../../models/inbox";
import type { CustomServicePermissionDetail } from "../../../models/inbox";
import { CustomServicePermissionDetailView } from "./CustomServicePermissionDetail";
import { collectText, collectVnodes } from "../../../testing";

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
  credential_header: "X-Api-Key: {token}",
};

interface ShellAttrs {
  headerLabel: string;
  body: m.Children;
}

// Render without a DOM: instantiate the closure component and call view() directly,
// as the sibling detail tests do. The root is a PermissionsShell vnode.
function renderBody(detail: CustomServicePermissionDetail): {
  body: m.Children;
  toggle: () => void;
  /** Re-render the same instance, as Mithril does when the request changes. */
  rerender: (next?: CustomServicePermissionDetail) => m.Children;
} {
  const instance =
    CustomServicePermissionDetailView() as unknown as m.Component;
  const model = new InboxModel();
  const render = (shown: CustomServicePermissionDetail): ShellAttrs => {
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
