import { describe, expect, it } from "vitest";
import type { CreateAttemptRequestSummary } from "./create";
import {
  IMBUE_CLOUD_SUMMARY_LINE,
  READY_LINE,
  SHOWCASE_APP,
  SHOWCASE_INTRO_LINE,
  SUMMARY_HEADING,
  WELCOME_BODY,
  failureLine,
  readyTurnMarkdown,
  shortRepository,
  showcaseMarkdown,
  summaryLines,
  summarySettingLines,
} from "./creationTranscript";
import { ONBOARDING_MEDIA_BASE_URL } from "./onboardingMedia";

function request(
  overrides: Partial<CreateAttemptRequestSummary> = {},
): CreateAttemptRequestSummary {
  return {
    display_name: "workspace-1",
    launch_mode: "IMBUE_CLOUD",
    cloud_account: "",
    cloud_account_alias: "",
    cloud_account_backend_label: "",
    backup_provider: "IMBUE_CLOUD",
    region: "US-EAST-VA",
    instance_type: "",
    cpu_count: null,
    memory_gb: null,
    disk_gb: null,
    repository: "https://github.com/imbue-ai/default-workspace-template.git",
    branch: "minds-v0.6.1",
    is_onboarding_driven_by_agent: false,
    ...overrides,
  };
}

describe("summaryLines", () => {
  it("restates an Imbue Cloud create as itself, without the settings behind it", () => {
    // The flow picked the region, the backup, the template and the branch --
    // reading them back makes the one choice they did make look like seven.
    expect(summaryLines(request(), true)).toEqual([IMBUE_CLOUD_SUMMARY_LINE]);
  });

  it("still lists the settings when Imbue Cloud was chosen inside the form", () => {
    // Same destination, different provenance: here the reader opened the form
    // and picked, so the values are theirs and hiding them hides their work.
    const lines = summaryLines(request(), false);
    expect(lines).not.toEqual([IMBUE_CLOUD_SUMMARY_LINE]);
    expect(lines).toContain("Compute — imbue_cloud");
    expect(lines).toContain("Region — US-EAST-VA");
  });

  it("restates a custom create one setting per line, with the repository shortened", () => {
    expect(
      summaryLines(
        request({
          launch_mode: "DOCKER",
          backup_provider: "CONFIGURE_LATER",
          region: "",
        }),
        false,
      ),
    ).toEqual([
      "Create a workspace with these settings:",
      "Name — workspace-1",
      "Compute — docker",
      "Backup — configure_later",
      "Template repository — imbue-ai/default-workspace-template",
      "Branch — minds-v0.6.1",
    ]);
  });

  it("omits blank settings such as region and machine size, and says latest for a blank branch", () => {
    const lines = summaryLines(
      request({
        launch_mode: "LIMA",
        backup_provider: "CONFIGURE_LATER",
        region: "",
        branch: "",
      }),
      false,
    );
    expect(lines).not.toContainEqual(expect.stringMatching(/^Region/));
    expect(lines).not.toContainEqual(expect.stringMatching(/^Machine size/));
    expect(lines).toContain("Compute — lima");
    expect(lines).toContain("Branch — latest");
  });

  it("names the backup provider the way the create form's own option does", () => {
    expect(
      summaryLines(
        request({ launch_mode: "LIMA", backup_provider: "API_KEY" }),
        false,
      ),
    ).toContain("Backup — manual");
  });

  it("names a connection as the compute the way the form offered it, with its machine size", () => {
    const connection = `aws_${"a".repeat(32)}`;
    const lines = summaryLines(
      request({
        launch_mode: "AWS",
        cloud_account: connection,
        cloud_account_alias: "team cloud",
        cloud_account_backend_label: "AWS",
        region: "us-west-2",
        instance_type: "t3.large",
      }),
      false,
    );
    expect(lines).toContain("Compute — team cloud (AWS)");
    expect(lines).toContain("Machine size — t3.large");
  });

  it("falls back to the instance name for a connection that is no longer known", () => {
    const connection = `aws_${"a".repeat(32)}`;
    expect(
      summaryLines(
        request({ launch_mode: "AWS", cloud_account: connection }),
        false,
      ),
    ).toContain(`Compute — ${connection}`);
  });
});

describe("summaryLines machine size in numbers", () => {
  it("restates a size chosen in numbers the way the settings page reads it", () => {
    const lines = summaryLines(
      request({ launch_mode: "LIMA", region: "", cpu_count: 2, memory_gb: 4, disk_gb: 100 }),
      false,
    );
    expect(lines).toContain("Machine size — 2 CPUs · 4 GB RAM · 100 GB disk");
  });
});

describe("shortRepository", () => {
  it("trims only the github prefix and .git suffix", () => {
    expect(shortRepository("https://github.com/a/b.git")).toBe("a/b");
    expect(shortRepository("/Users/me/template")).toBe("/Users/me/template");
    expect(shortRepository("https://gitlab.com/a/b.git")).toBe(
      "https://gitlab.com/a/b",
    );
  });
});

describe("failureLine", () => {
  it("names the workspace and the error, with fallbacks for both", () => {
    expect(failureLine("alpha", "clone blew up")).toBe(
      "Could not create alpha: clone blew up",
    );
    expect(failureLine("", "")).toBe(
      "Could not create the workspace: unknown error",
    );
  });
});

describe("readyTurnMarkdown", () => {
  it("puts the ready line as a heading over the welcome", () => {
    expect(readyTurnMarkdown()).toBe(`## ${READY_LINE}\n\n${WELCOME_BODY}`);
    expect(WELCOME_BODY).toContain("**What’s annoying, overwhelming, or tedious");
    expect(WELCOME_BODY.endsWith("tells me what to do next.*")).toBe(true);
    expect(readyTurnMarkdown()).not.toContain(SHOWCASE_APP.gifUrl);
  });
});

describe("summarySettingLines", () => {
  it("is what summaryLines says under its heading, and nothing for the Imbue Cloud answer", () => {
    const lines = summarySettingLines(request(), false);
    expect(lines[0]).toBe("Name — workspace-1");
    expect(summaryLines(request(), false)).toEqual([SUMMARY_HEADING, ...lines]);
    expect(summarySettingLines(request(), true)).toEqual([]);
  });
});

describe("the showcase", () => {
  it("is the to-do list, with a hosted recording under a content-addressed name", () => {
    expect(SHOWCASE_APP.id).toBe("todo-list");
    expect(SHOWCASE_APP.gifUrl.startsWith(`${ONBOARDING_MEDIA_BASE_URL}/`)).toBe(true);
    expect(SHOWCASE_APP.gifUrl).toMatch(/\/[0-9a-f]{64}\.gif$/);
    expect(SHOWCASE_APP.label.length).toBeGreaterThan(0);
  });

  it("carries the recording into the chat under its intro line, with no title or description", () => {
    expect(showcaseMarkdown({ id: "a", label: "Alpha", gifUrl: "https://cdn.example/a.gif" })).toBe(
      `${SHOWCASE_INTRO_LINE}\n\n![Alpha](https://cdn.example/a.gif)`,
    );
  });
});
