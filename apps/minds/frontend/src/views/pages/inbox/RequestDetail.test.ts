import { describe, expect, it, vi } from "vitest";
import type { ErrorAction } from "../../../models/errors";
import { InboxModel } from "../../../models/inbox";
import { allText, attrsOf, collectVnodes } from "../../../testing";
import { ErrorNotice } from "../../components/ErrorDisplay";
import { requestDetailView } from "./RequestDetail";

describe("requestDetailView", () => {
  it("dispatches each detail kind to its own dialog", () => {
    const model = new InboxModel();

    const unavailable = requestDetailView(model, {
      kind: "unavailable",
      message: "It has already been processed.",
    });
    expect(allText(unavailable)).toContain("This permission request is no longer available");
    expect(allText(unavailable)).toContain("It has already been processed.");

  });

  it("shows a request no handler owns as a bug, with the handler's reason behind See details and Close instead of Deny", () => {
    const onClose = vi.fn();
    const model = new InboxModel({ onClose });

    const unsupported = requestDetailView(model, { kind: "unsupported", message: "Unsupported request type" });

    const notice = collectVnodes(unsupported).find((vnode) => vnode.tag === ErrorNotice);
    if (notice === undefined) throw new Error("no error notice");
    // Opening it again fails the same way, so the sentence asks for no retry.
    expect(attrsOf(notice).error).toEqual({
      message: "Imbue Studio couldn't show this request. If this persists, report a bug.",
      details: "Unsupported request type",
    });
    const actions = attrsOf(notice).actions as ErrorAction[];
    expect(actions.map((action) => action.label)).toEqual(["Close"]);
    actions[0].onclick();
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("shows a scope the catalog does not carry as a bug, still offering Deny", () => {
    // The gateway refuses such a scope when it is filed, so reaching here is a
    // bug; Deny stays, since no action at all would strand the agent waiting.
    const model = new InboxModel();

    const unknown = requestDetailView(model, { kind: "unknown_scope", request_id: "evt-a", scope: "zzz-api" });

    const notice = collectVnodes(unknown).find((vnode) => vnode.tag === ErrorNotice);
    if (notice === undefined) throw new Error("no error notice");
    expect(attrsOf(notice).error).toEqual({
      message: "Imbue Studio couldn't show this request. If this persists, report a bug.",
      details: "Scope 'zzz-api' is not in the catalog.",
    });
    // The error's own action, in the danger color as every Deny is.
    const actions = attrsOf(notice).actions as ErrorAction[];
    expect(actions.map(({ label, id, isDestructive }) => ({ label, id, isDestructive }))).toEqual([
      { label: "Deny", id: "permissions-deny-btn", isDestructive: true },
    ]);
  });
});
