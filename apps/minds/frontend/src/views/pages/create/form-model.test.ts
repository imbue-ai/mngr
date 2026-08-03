import { describe, expect, it } from "vitest";
import type { CreateFormDefaults } from "../../../models/create";
import { CreateFormModel, DEFAULT_RESTIC_ENV, normalizeCreateApiError } from "./form-model";

function buildDefaults(overrides: Partial<CreateFormDefaults> = {}): CreateFormDefaults {
  return {
    accounts: [{ user_id: "user-1", email: "alice@example.com" }],
    default_account_id: "user-1",
    launch_modes: ["IMBUE_CLOUD", "LIMA", "DOCKER", "MODAL", "VULTR"],
    selected_launch_mode: "IMBUE_CLOUD",
    docker_runtimes: ["RUNC", "RUNSC"],
    selected_docker_runtime: "RUNSC",
    backup_providers: ["IMBUE_CLOUD", "API_KEY", "CONFIGURE_LATER"],
    selected_backup_provider: "IMBUE_CLOUD",
    region_options_by_launch_mode: {
      IMBUE_CLOUD: ["us-west", "eu-central"],
      VULTR: ["ewr"],
      AWS: ["us-west-2", "us-east-1"],
    },
    region_selected_by_launch_mode: { IMBUE_CLOUD: "us-west", VULTR: "ewr", AWS: "us-west-2" },
    instance_types_by_backend: {
      AWS: [
        ["t3.large", "t3.large (8 GB)"],
        ["t3.xlarge", "t3.xlarge (16 GB)"],
      ],
    },
    default_instance_type_by_backend: { AWS: "t3.large" },
    cloud_accounts: [{ name: "aws_acct", alias: "team aws", backend: "aws", region: "us-west-2" }],
    byok_clouds_enabled: true,
    git_url: "https://github.com/imbue-ai/default-workspace-template.git",
    branch: "minds-v9.9.9",
    color: "#0b292b",
    prefill: null,
    ...overrides,
  };
}

describe("CreateFormModel", () => {
  it("applies presets by filling the advanced selects (the submit source of truth)", () => {
    const model = new CreateFormModel();
    model.applyDefaults(buildDefaults({ default_account_id: "" }));
    model.accountId = "";
    model.applyPreset("local");
    expect(model.launchValue).toBe("LIMA");
    expect(model.backupProvider).toBe("CONFIGURE_LATER");
    // Picking remote with signed-in accounts auto-picks the first one.
    model.applyPreset("remote");
    expect(model.launchValue).toBe("IMBUE_CLOUD");
    expect(model.accountId).toBe("user-1");
  });

  it("requires an account only for imbue_cloud compute or backups", () => {
    const model = new CreateFormModel();
    model.applyDefaults(buildDefaults({ default_account_id: "" }));
    model.accountId = "";
    model.launchValue = "IMBUE_CLOUD";
    expect(model.imbueCloudNeedsAccount()).toBe(true);
    model.launchValue = "LIMA";
    model.backupProvider = "CONFIGURE_LATER";
    expect(model.imbueCloudNeedsAccount()).toBe(false);
  });

  it("resolves a BYOK selection into the backend mode plus the account name", () => {
    const model = new CreateFormModel();
    model.applyDefaults(buildDefaults());
    model.launchValue = "BYOK:aws_acct";
    expect(model.launchSelection()).toEqual({ mode: "AWS", cloudAccount: "aws_acct" });
    // BYOK entries pin their region: no picker, a note instead.
    expect(model.regionOptions()).toEqual([]);
    expect(model.byokPinnedRegion()).toBe("us-west-2");
    // The machine-size options come from the backend.
    expect(model.instanceTypeOptions().map(([value]) => value)).toEqual(["t3.large", "t3.xlarge"]);
    expect(model.selectedInstanceType()).toBe("t3.large");
  });

  it("keeps an explicit region pick per provider but resets across providers", () => {
    const model = new CreateFormModel();
    model.applyDefaults(buildDefaults());
    model.launchValue = "IMBUE_CLOUD";
    expect(model.selectedRegion()).toBe("us-west");
    model.setRegion("eu-central");
    expect(model.selectedRegion()).toBe("eu-central");
    model.launchValue = "VULTR";
    expect(model.selectedRegion()).toBe("ewr");
    model.launchValue = "IMBUE_CLOUD";
    expect(model.selectedRegion()).toBe("eu-central");
  });

  it("blocks submit on a stale-free taken verdict only for the exact name and scope", () => {
    const model = new CreateFormModel();
    model.applyDefaults(buildDefaults());
    model.hostName = "taken-name";
    const url = model.hostNameAvailabilityUrl();
    model.applyAvailabilityVerdict(url, false);
    expect(model.validateHostNameFormatForSubmit()).toBe(false);
    // Editing the name makes the verdict stale: submit falls through to the
    // server's own conflict check.
    model.hostName = "taken-name-2";
    expect(model.validateHostNameFormatForSubmit()).toBe(true);
  });

  it("builds the submit body from the advanced selects and omits the runtime off docker", () => {
    const model = new CreateFormModel();
    model.applyDefaults(buildDefaults());
    model.gitUrl = " https://example.com/repo.git ";
    model.launchValue = "IMBUE_CLOUD";
    const body = model.submitBody();
    expect(body.git_url).toBe("https://example.com/repo.git");
    expect(body.launch_mode).toBe("IMBUE_CLOUD");
    expect(body.region).toBe("us-west");
    expect(body.runtime).toBeUndefined();
    model.launchValue = "DOCKER";
    expect(model.submitBody().runtime).toBe("RUNSC");
  });

  it("seeds the repository and branch from the server defaults", () => {
    const model = new CreateFormModel();
    model.applyDefaults(buildDefaults());
    expect(model.gitUrl).toBe("https://github.com/imbue-ai/default-workspace-template.git");
    expect(model.branch).toBe("minds-v9.9.9");
  });

  it("keeps a deep-linked repository and its blank latest-version branch over the defaults", () => {
    const model = new CreateFormModel();
    model.gitUrl = "https://example.com/inspiration.git";
    model.applyDefaults(buildDefaults());
    expect(model.gitUrl).toBe("https://example.com/inspiration.git");
    // Blank means "the repo's latest version" for an explicit repository, so
    // the default branch must not be paired with it.
    expect(model.branch).toBe("");
  });

  it("seeds the restic env with the default so the textarea and submit agree", () => {
    const model = new CreateFormModel();
    model.applyDefaults(buildDefaults());
    // The textarea renders model state verbatim, so what the user sees on an
    // untouched form is exactly what an API_KEY submit sends.
    expect(model.backupApiKeyEnv).toBe(DEFAULT_RESTIC_ENV);
    model.backupProvider = "API_KEY";
    expect(model.submitBody().backup_api_key_env).toBe(DEFAULT_RESTIC_ENV);
  });

  it("keeps a retry prefill's saved restic env instead of the seeded default", () => {
    const model = new CreateFormModel();
    const prefill = {
      git_url: "https://example.com/repo.git",
      branch: "",
      host_name: "",
      launch_mode: "LIMA",
      docker_runtime: "RUNSC",
      backup_provider: "API_KEY",
      backup_api_key_env: "RESTIC_REPOSITORY=s3:saved",
      account_id: "",
      region: "",
      cloud_account: "",
      instance_type: "",
      color: "#123456",
    };
    model.applyDefaults(buildDefaults({ prefill }));
    expect(model.backupApiKeyEnv).toBe("RESTIC_REPOSITORY=s3:saved");
  });

  it("applies a retry prefill including the BYOK selection and one-shot machine size", () => {
    const model = new CreateFormModel();
    model.applyDefaults(
      buildDefaults({
        prefill: {
          git_url: "https://example.com/repo.git",
          branch: "main",
          host_name: "old-name",
          launch_mode: "AWS",
          docker_runtime: "RUNC",
          backup_provider: "API_KEY",
          backup_api_key_env: "RESTIC_REPOSITORY=s3:...",
          account_id: "",
          region: "us-east-1",
          cloud_account: "aws_acct",
          instance_type: "t3.xlarge",
          color: "#123456",
        },
      }),
    );
    expect(model.launchValue).toBe("BYOK:aws_acct");
    expect(model.isAdvancedOpen).toBe(true);
    expect(model.selectedInstanceType()).toBe("t3.xlarge");
    expect(model.submitBody().cloud_account).toBe("aws_acct");
  });
});

describe("normalizeCreateApiError", () => {
  it("normalizes the three error shapes into one", () => {
    expect(normalizeCreateApiError({ error: "bad url", field: "git_url" })).toEqual({
      message: "bad url",
      field: "git_url",
      redirectUrl: "",
    });
    expect(normalizeCreateApiError({ error: "no account", redirect_url: "/auth/signup" }).redirectUrl).toBe(
      "/auth/signup",
    );
    expect(normalizeCreateApiError(null).message).toBe("Could not create the workspace.");
  });
});
