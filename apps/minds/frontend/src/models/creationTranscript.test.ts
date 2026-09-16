import { describe, expect, it } from "vitest";
import type { CreateAttemptRequestSummary } from "./create";
import { failureLine, shortRepository, summaryLines } from "./creationTranscript";

function request(overrides: Partial<CreateAttemptRequestSummary> = {}): CreateAttemptRequestSummary {
  return {
    display_name: "workspace-1",
    launch_mode: "IMBUE_CLOUD",
    cloud_account: "",
    backup_provider: "IMBUE_CLOUD",
    region: "US-EAST-VA",
    instance_type: "",
    repository: "https://github.com/imbue-ai/default-workspace-template.git",
    branch: "minds-v0.6.1",
    ...overrides,
  };
}

describe("summaryLines", () => {
  it("restates the cloud preset, one setting per line, with the repository shortened", () => {
    expect(summaryLines(request())).toEqual([
      "Create a workspace with these settings:",
      "Name — workspace-1",
      "Compute — imbue_cloud",
      "Backup — imbue_cloud",
      "Region — US-EAST-VA",
      "Template repository — imbue-ai/default-workspace-template",
      "Branch — minds-v0.6.1",
    ]);
  });

  it("omits blank settings such as region and machine size, and says latest for a blank branch", () => {
    const lines = summaryLines(
      request({ launch_mode: "LIMA", backup_provider: "CONFIGURE_LATER", region: "", branch: "" }),
    );
    expect(lines).not.toContainEqual(expect.stringMatching(/^Region/));
    expect(lines).not.toContainEqual(expect.stringMatching(/^Machine size/));
    expect(lines).toContain("Compute — lima");
    expect(lines).toContain("Branch — latest");
  });

  it("names the backup provider the way the create form's own option does", () => {
    expect(summaryLines(request({ backup_provider: "API_KEY" }))).toContain("Backup — manual");
  });

  it("names a bring-your-own-key account as the compute, with its machine size", () => {
    const lines = summaryLines(
      request({ launch_mode: "AWS", cloud_account: "byok-aws-team", region: "us-west-2", instance_type: "t3.large" }),
    );
    expect(lines).toContain("Compute — byok-aws-team");
    expect(lines).toContain("Machine size — t3.large");
  });
});

describe("shortRepository", () => {
  it("trims only the github prefix and .git suffix", () => {
    expect(shortRepository("https://github.com/a/b.git")).toBe("a/b");
    expect(shortRepository("/Users/me/template")).toBe("/Users/me/template");
    expect(shortRepository("https://gitlab.com/a/b.git")).toBe("https://gitlab.com/a/b");
  });
});

describe("failureLine", () => {
  it("names the workspace and the error, with fallbacks for both", () => {
    expect(failureLine("alpha", "clone blew up")).toBe("Could not create alpha: clone blew up");
    expect(failureLine("", "")).toBe("Could not create the workspace: unknown error");
  });
});
