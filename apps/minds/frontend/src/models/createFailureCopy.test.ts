import { describe, expect, it } from "vitest";
import type { CreateAttemptFailure, CreateAttemptRequestSummary } from "./create";
import { failureCopy, interruptedLine, shortRepository } from "./createFailureCopy";

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
    ...overrides,
  };
}


describe("shortRepository", () => {
  it("trims only the github prefix and .git suffix", () => {
    expect(shortRepository("https://github.com/a/b.git")).toBe("a/b");
    expect(shortRepository("/Users/me/template")).toBe("/Users/me/template");
    expect(shortRepository("https://gitlab.com/a/b.git")).toBe(
      "https://gitlab.com/a/b",
    );
  });
});

describe("failureCopy", () => {
  function failure(errorKind: string, overrides: Partial<CreateAttemptFailure> = {}): CreateAttemptFailure {
    return { error: "", errorKind, quotaRefusal: null, providerRemediation: "", ...overrides };
  }

  it("says what failed in our words and never repeats the tool's output", () => {
    const copy = failureCopy("alpha", failure(""), request());
    expect(copy.message).toBe("Couldn't create alpha. Try again. If this persists, report a bug.");
    expect(copy).toMatchObject({ isRetryOffered: true, isRunHereOffered: false, isUpdateOffered: false });
    expect(failureCopy("", failure(""), null).message).toContain("Couldn't create the workspace.");
  });

  it("words an unreachable provider from the fix the provider named, and falls back when it named none", () => {
    const docker = failureCopy(
      "alpha",
      failure("PROVIDER_UNAVAILABLE", { providerRemediation: "start Docker on ssh://me@spare" }),
      request({ launch_mode: "DOCKER" }),
    );
    expect(docker.message).toBe("Couldn't create alpha. To fix it, *start Docker on ssh://me@spare, then select Retry*.");
    expect(docker.isRetryOffered).toBe(true);
    const unnamed = failureCopy("alpha", failure("PROVIDER_UNAVAILABLE"), request({ launch_mode: "DOCKER" }));
    expect(unnamed.message).toBe("Couldn't create alpha. Try again. If this persists, report a bug.");
    const checkout = failureCopy("alpha", failure("BRANCH_CHECKOUT_FAILED"), request({ branch: "feature-x" }));
    expect(checkout.message).toContain("couldn't switch its code to the branch feature-x");
  });

  it("blames a refused clone on the branch only when it named a commit, the one ref the server cannot look up", () => {
    // A missing branch or tag arrives as its own kind, so a refused clone of a named branch is about the repository.
    const namedBranch = failureCopy("alpha", failure("GITHUB_AUTH_REQUIRED"), request({ branch: "v2" })).message;
    const noBranch = failureCopy("alpha", failure("GIT_AUTH_REQUIRED"), request({ branch: "" })).message;
    const commit = failureCopy("alpha", failure("GITHUB_AUTH_REQUIRED"), request({ branch: "a".repeat(40) })).message;
    expect(namedBranch).toContain("the address may be wrong");
    expect(namedBranch).not.toContain("branch");
    expect(noBranch).not.toContain("branch");
    expect(commit).toContain("that commit may not exist");
    expect(failureCopy("alpha", failure("GIT_SSH_AUTH_REQUIRED"), request()).message).toContain("SSH key");
  });

  it("names the repository and the branch it lacks, with Retry", () => {
    const copy = failureCopy(
      "alpha",
      failure("GIT_REF_NOT_FOUND"),
      request({ repository: "https://github.com/acme/widgets.git", branch: "v9" }),
    );
    expect(copy.message).toBe(
      "Couldn't create alpha. acme/widgets has no branch or tag called v9. *Clear the Branch field to use " +
        "its default branch, or enter one that exists, then select Retry*.",
    );
    expect(copy).toMatchObject({ isRetryOffered: true, isRunHereOffered: false });
  });

  it("offers every Imbue Cloud refusal a run on this computer, Retry only where retrying can help", () => {
    const ways = (errorKind: string) => {
      const { message: _message, ...rest } = failureCopy("alpha", failure(errorKind), request());
      return rest;
    };
    // The account can't use Imbue Cloud at all: running here also turns its Imbue Cloud backups and sharing off.
    // Accepting the invite takes the account off the waitlist, so Retry can then go through.
    expect(ways("WAITLISTED")).toEqual({
      isRetryOffered: true,
      isRunHereOffered: true,
      isImbueCloudUnusable: true,
      isUpdateOffered: false,
    });
    expect(ways("ACCOUNT_SUSPENDED")).toMatchObject({ isRetryOffered: false, isImbueCloudUnusable: true });
    expect(ways("EMAIL_NOT_VERIFIED")).toMatchObject({ isRetryOffered: true, isImbueCloudUnusable: true });
    expect(ways("CLIENT_TOO_OLD")).toEqual({
      isRetryOffered: false,
      isRunHereOffered: true,
      isImbueCloudUnusable: true,
      isUpdateOffered: true,
    });
    // A limit or a full cloud leaves the account's backups and sharing usable.
    expect(ways("QUOTA_EXCEEDED")).toMatchObject({ isRetryOffered: true, isRunHereOffered: true, isImbueCloudUnusable: false });
    expect(ways("NO_CLOUD_CAPACITY")).toMatchObject({ isRetryOffered: true, isRunHereOffered: true, isImbueCloudUnusable: false });
  });

  it("gives a held Retry's reason in place of any step that ends in a retry", () => {
    const reason = "The connection this attempt used was removed, so it can't be retried.";
    for (const errorKind of ["", "NAME_TAKEN", "GIT_REF_NOT_FOUND", "PROVIDER_UNAVAILABLE", "TARGET_UNRESOLVED"]) {
      const message = failureCopy("alpha", failure(errorKind, { providerRemediation: "start Docker" }), request(), reason)
        .message;
      expect(message).toBe(`Couldn't create alpha. ${reason}`);
    }
    // A create that may have gone through still says so.
    expect(failureCopy("alpha", failure("OUTCOME_UNKNOWN"), request(), reason).message).toBe(
      `Something went wrong while creating alpha, and Imbue Studio couldn't tell whether the workspace was made. ${reason}`,
    );
    expect(failureCopy("alpha", failure("QUOTA_EXCEEDED"), request(), reason).message).toBe(
      `Couldn't create alpha. ${reason} You can run this workspace on this computer instead.`,
    );
  });

  it("asks for no retry where none is offered", () => {
    for (const errorKind of ["ACCOUNT_SUSPENDED", "CLIENT_TOO_OLD"]) {
      const copy = failureCopy("alpha", failure(errorKind), request());
      expect(copy.isRetryOffered).toBe(false);
      expect(copy.message).not.toMatch(/try again|Retry/i);
    }
  });

  it("names the plan limit that refused the create", () => {
    const copy = failureCopy(
      "alpha",
      failure("QUOTA_EXCEEDED", { quotaRefusal: { entitlement: "max_total_workspaces", limit: 3 } }),
      request(),
    );
    expect(copy.message).toContain("limit of 3 workspaces on Imbue Cloud");
    const unknownLimit = failureCopy(
      "alpha",
      failure("QUOTA_EXCEEDED", { quotaRefusal: { entitlement: "max_widgets", limit: 3 } }),
      request(),
    );
    expect(unknownLimit.message).toContain("You've reached a limit on your plan.");
  });
});


describe("interruptedLine", () => {
  it("offers a retry and a discard, or says why the retry is held", () => {
    expect(interruptedLine("")).toBe(
      "The app closed while this workspace was being created. You can retry with the same settings or discard " +
        "the partial workspace.",
    );
    expect(interruptedLine("Its connection was removed.")).toBe(
      "The app closed while this workspace was being created. Its connection was removed. You can discard the " +
        "partial workspace.",
    );
  });
});
