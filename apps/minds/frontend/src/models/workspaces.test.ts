import { describe, expect, it } from "vitest";
import { isWorkspaceOrigin } from "./workspaces";

describe("isWorkspaceOrigin", () => {
  it("accepts the canonical agent-keyed content origins", () => {
    expect(isWorkspaceOrigin("http://agent-0f3c2b71a4de49b1a2c3d4e5f6a7b8c9.localhost:8421")).toBe(true);
    expect(
      isWorkspaceOrigin("http://system_interface-x1y2.agent-0f3c2b71a4de49b1a2c3d4e5f6a7b8c9.localhost:8421"),
    ).toBe(true);
  });

  it("still accepts a legacy host-keyed origin awaiting the redirect heal", () => {
    expect(isWorkspaceOrigin("http://host-0f3c2b71a4de49b1a2c3d4e5f6a7b8c9.localhost:8421")).toBe(true);
  });

  it("rejects origins outside the workspace families", () => {
    expect(isWorkspaceOrigin("http://localhost:8420")).toBe(false);
    expect(isWorkspaceOrigin("https://evil-agent-abc.example.com")).toBe(false);
    expect(isWorkspaceOrigin("https://agent-abc.localhost.example.com")).toBe(false);
    expect(isWorkspaceOrigin("null")).toBe(false);
  });
});
