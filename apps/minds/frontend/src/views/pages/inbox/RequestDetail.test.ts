import { describe, expect, it } from "vitest";
import { InboxModel } from "../../../models/inbox";
import { allText } from "../../../testing";
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

    const unsupported = requestDetailView(model, { kind: "unsupported", message: "no handler" });
    expect(allText(unsupported)).toContain("no handler");
  });

  it("offers a way out of a scope the catalog does not carry", () => {
    // Nothing can be granted, so the only honest action is to deny it -- and
    // leaving no action at all would strand the agent waiting on an answer.
    const model = new InboxModel();

    const unknown = requestDetailView(model, { kind: "unknown_scope", request_id: "evt-a", scope: "zzz-api" });

    expect(allText(unknown)).toContain("zzz-api");
    expect(allText(unknown)).toContain("Deny this request");
  });
});
