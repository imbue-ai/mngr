import m from "mithril";
import { describe, expect, it } from "vitest";
import type { CloudAccountOption, ConnectionBackendOption } from "../../../models/create";
import type { AnyVnode } from "../../../testing";
import { allText, attrsOf, collectVnodes } from "../../../testing";
import { ALPHA_FEATURE_TITLE, COMMUNITY_DISCORD_URL } from "./ComputeNotices";
import { LOCAL_PROFILE_USER_ID } from "../../../models/providers";
import { CONNECTION_BACKEND_OPTIONS } from "../../../testing";
import type { CloudAccountsModalAttrs } from "./CloudAccountsModal";
import type { LocalProfileOption, LocalProfilesLoader } from "./CloudAccountsModal";
import {
  CloudAccountsModal,
  CloudAccountsModalState,
  connectionFailureMessage,
  localProfileChoiceLabel,
} from "./CloudAccountsModal";

const ALICE = { userId: "user-1", label: "alice@example.com" };
const THIS_DEVICE = { userId: LOCAL_PROFILE_USER_ID, label: "This device only" };
const OWNER_OPTIONS = [ALICE, THIS_DEVICE];
const DEFAULT_REGION_BY_BACKEND = { AWS: "us-west-2", GCP: "us-central1-a" };

function backendOption(name: string): ConnectionBackendOption {
  const option = CONNECTION_BACKEND_OPTIONS.find((candidate) => candidate.name === name);
  if (option === undefined) throw new Error(`no backend option named ${name}`);
  return option;
}

describe("CloudAccountsModalState", () => {
  it("keeps an eligible owner and falls back to this device for one that is not offered", () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(
      backendOption("aws"),
      ALICE,
      DEFAULT_REGION_BY_BACKEND,
      OWNER_OPTIONS,
    );
    expect(state.ownerUserId).toBe("user-1");
    expect(state.region).toBe("us-west-2");
    expect(state.isOpen).toBe(true);
    expect(state.view).toBe("form");

    state.openForBackend(
      backendOption("gcp"),
      { userId: "locked-user", label: "locked@example.com" },
      DEFAULT_REGION_BY_BACKEND,
      OWNER_OPTIONS,
    );
    expect(state.ownerUserId).toBe(LOCAL_PROFILE_USER_ID);
    expect(state.region).toBe("us-central1-a");
  });

  it("explains why an ineligible preselected owner was replaced by this device only", () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(
      backendOption("aws"),
      { userId: "locked-user", label: "locked@example.com" },
      DEFAULT_REGION_BY_BACKEND,
      OWNER_OPTIONS,
    );
    expect(state.ownerUserId).toBe(LOCAL_PROFILE_USER_ID);
    expect(state.ineligibleOwnerNote).toBe(
      "locked@example.com cannot own a synced connection on this device (locked, or no master password), " +
        "so this connection stays on this device only.",
    );

    // An eligible owner (and this device itself) needs no explaining.
    state.openForBackend(backendOption("aws"), ALICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    expect(state.ineligibleOwnerNote).toBe("");
    state.openForBackend(backendOption("aws"), THIS_DEVICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    expect(state.ineligibleOwnerNote).toBe("");
  });

  it("forgets a cancelled paste so the next open starts blank", () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(
      backendOption("modal"),
      ALICE,
      DEFAULT_REGION_BY_BACKEND,
      OWNER_OPTIONS,
    );
    state.alias = "team modal";
    state.modalTokenId = "ak-1";
    state.modalTokenSecret = "as-secret";

    state.close();

    expect(state.isOpen).toBe(false);
    expect(state.modalTokenSecret).toBe("");
    expect(state.modalTokenId).toBe("");
    expect(state.alias).toBe("");
  });

  it("sends the owner and both Modal tokens, and no region for a per-create backend", () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(
      backendOption("modal"),
      ALICE,
      DEFAULT_REGION_BY_BACKEND,
      OWNER_OPTIONS,
    );
    state.alias = " team modal ";
    state.modalTokenId = "ak-1";
    expect(state.buildRequestBody()).toBe(
      "Both the token ID and token secret are required.",
    );

    state.modalTokenSecret = "as-1";
    expect(state.buildRequestBody()).toEqual({
      alias: "team modal",
      backend: "modal",
      owner: "user-1",
      modal_token_id: "ak-1",
      modal_token_secret: "as-1",
    });
  });

  it("pins the region on an aws connection and rejects an unknown backend", () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(
      backendOption("aws"),
      THIS_DEVICE,
      DEFAULT_REGION_BY_BACKEND,
      OWNER_OPTIONS,
    );
    state.alias = "mine";
    state.awsAccessKeyId = "AKIA";
    state.awsSecretAccessKey = "s";
    expect(state.buildRequestBody()).toEqual({
      alias: "mine",
      backend: "aws",
      owner: LOCAL_PROFILE_USER_ID,
      region: "us-west-2",
      aws_access_key_id: "AKIA",
      aws_secret_access_key: "s",
    });

    state.selectBackend(
      { name: "hetzner", label: "Hetzner", is_region_pinned: false, is_local_profile_supported: false },
      "",
    );
    expect(state.buildRequestBody()).toBe("Unknown backend hetzner.");
  });

  it("takes the region rule from the served backend option when the backend changes", () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(
      backendOption("aws"),
      THIS_DEVICE,
      DEFAULT_REGION_BY_BACKEND,
      OWNER_OPTIONS,
    );
    expect(state.isRegionPinned).toBe(true);
    state.selectBackend(backendOption("modal"), "us-east");
    expect(state.isRegionPinned).toBe(false);
    expect(state.region).toBe("us-east");
    state.selectBackend(backendOption("gcp"), "us-central1-a");
    expect(state.isRegionPinned).toBe(true);
    expect(state.region).toBe("us-central1-a");
  });
});

const AWS_INSTANCE = "aws_" + "a".repeat(32);
const AWS_CONNECTION = {
  instance: AWS_INSTANCE,
  backend_label: "AWS",
  alias: "team aws",
  placement: { default_region: "us-east-1" },
};

/** A loader for tests that do not care about this device's logins: none are found. */
const NO_PROFILES: LocalProfilesLoader = () => Promise.resolve([]);

function localProfile(overrides: Partial<LocalProfileOption> & Pick<LocalProfileOption, "name">): LocalProfileOption {
  return {
    label: overrides.name,
    region: null,
    tenant_id: null,
    subscription_id: null,
    unusable_reason: "",
    ...overrides,
  };
}

describe("CloudAccountsModalState local profiles", () => {
  const NEVER_CALLED = () => Promise.reject(new Error("no listing expected"));

  it("sends the profile name in place of keys, and names the login the body lacks", async () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(backendOption("aws"), THIS_DEVICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    state.alias = "work aws";
    state.selectCredentialSource("local_profile", () => Promise.resolve([]));
    await Promise.resolve();
    expect(state.buildRequestBody()).toBe("Name the AWS profile from ~/.aws/config to use.");

    state.awsProfile = " work ";
    expect(state.buildRequestBody()).toEqual({
      alias: "work aws",
      backend: "aws",
      owner: LOCAL_PROFILE_USER_ID,
      region: "us-west-2",
      credential_source: "local_profile",
      aws_profile: "work",
    });
  });

  it("sends an az login's tenant and subscription, and a Modal profile's name", () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(backendOption("azure"), ALICE, { AZURE: "eastus" }, OWNER_OPTIONS);
    state.alias = "work azure";
    state.credentialSource = "local_profile";
    state.azureSubscriptionId = "sub-1";
    expect(state.buildRequestBody()).toBe(
      "The subscription id and tenant id of your az login are both required.",
    );
    state.azureTenantId = "tenant-1";
    expect(state.buildRequestBody()).toEqual({
      alias: "work azure",
      backend: "azure",
      owner: "user-1",
      region: "eastus",
      credential_source: "local_profile",
      azure_subscription_id: "sub-1",
      azure_tenant_id: "tenant-1",
    });

    state.openForBackend(backendOption("modal"), ALICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    state.alias = "work modal";
    state.credentialSource = "local_profile";
    state.modalProfile = "work";
    expect(state.buildRequestBody()).toEqual({
      alias: "work modal",
      backend: "modal",
      owner: "user-1",
      credential_source: "local_profile",
      modal_profile: "work",
    });
  });

  it("refuses the local-profile source for a backend without one, and drops it when switching to such a backend", () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(backendOption("gcp"), THIS_DEVICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    expect(state.isLocalProfileSupported).toBe(false);
    state.selectCredentialSource("local_profile", NEVER_CALLED);
    expect(state.credentialSource).toBe("pasted");

    state.selectBackend(backendOption("aws"), "us-west-2");
    state.credentialSource = "local_profile";
    state.selectBackend(backendOption("gcp"), "us-central1-a");
    expect(state.credentialSource).toBe("pasted");
  });

  it("lists this device's logins and takes the first usable one, with its region, when nothing is chosen", async () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(backendOption("aws"), THIS_DEVICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    state.credentialSource = "local_profile";
    const listed = [
      localProfile({ name: "guarded", unusable_reason: "needs an MFA code on every use, which the app cannot enter" }),
      localProfile({ name: "work", region: "eu-west-1" }),
    ];

    await state.loadLocalProfiles((backend) => {
      expect(backend).toBe("aws");
      return Promise.resolve(listed);
    });

    expect(state.localProfiles).toEqual(listed);
    expect(state.awsProfile).toBe("work");
    expect(state.region).toBe("eu-west-1");
    expect(state.isLocalProfilesLoading).toBe(false);
  });

  it("keeps the free-text field when the listing fails", async () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(backendOption("modal"), THIS_DEVICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    state.credentialSource = "local_profile";
    const consoleError = console.error;
    console.error = () => undefined;
    try {
      await state.loadLocalProfiles(() => Promise.reject(new Error("offline")));
    } finally {
      console.error = consoleError;
    }
    expect(state.localProfiles).toEqual([]);
    expect(state.localProfilesError).toBe("Could not read this device's logins; type the name below.");
    expect(state.modalProfile).toBe("");
  });

  it("drops a listing that lands after the form went back to pasted keys", async () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(backendOption("aws"), THIS_DEVICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    let deliver: (profiles: LocalProfileOption[]) => void = () => undefined;
    const pending = new Promise<LocalProfileOption[]>((resolve) => {
      deliver = resolve;
    });
    state.selectCredentialSource("local_profile", () => pending);
    const listing = state.loadLocalProfiles(() => pending);
    state.selectCredentialSource("pasted", NEVER_CALLED);

    deliver([localProfile({ name: "work", region: "eu-west-1" })]);
    await listing;

    expect(state.localProfiles).toEqual([]);
    expect(state.awsProfile).toBe("");
    expect(state.region).toBe("us-west-2");
  });

  it("takes an az subscription's tenant and id together", () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(backendOption("azure"), THIS_DEVICE, { AZURE: "eastus" }, OWNER_OPTIONS);
    state.credentialSource = "local_profile";
    state.selectLocalProfile(
      localProfile({ name: "sub-1", label: "First (sub-1)", tenant_id: "tenant-1", subscription_id: "sub-1" }),
    );
    expect(state.azureSubscriptionId).toBe("sub-1");
    expect(state.azureTenantId).toBe("tenant-1");
  });

  it("opens an edit on the login a local-profile connection names, listing this device's logins, and on pasted mode otherwise", async () => {
    const state = new CloudAccountsModalState();
    const listedFor: string[] = [];
    const loader: LocalProfilesLoader = (backend) => {
      listedFor.push(backend);
      return Promise.resolve([localProfile({ name: "other" }), localProfile({ name: "work" })]);
    };
    state.openForEdit(
      {
        instance: "modal_" + "d".repeat(32),
        backend_label: "Modal",
        alias: "work modal",
        placement: { modal_profile: "work" },
        credential_source: "local_profile",
      },
      backendOption("modal"),
      THIS_DEVICE,
      loader,
    );
    await Promise.resolve();
    expect(state.credentialSource).toBe("local_profile");
    expect(listedFor).toEqual(["modal"]);
    expect(state.localProfiles.map((profile) => profile.name)).toEqual(["other", "work"]);
    // The connection's own login stays selected; the listing never replaces it.
    expect(state.modalProfile).toBe("work");
    expect(state.buildRequestBody()).toEqual({
      alias: "work modal",
      backend: "modal",
      credential_source: "local_profile",
      modal_profile: "work",
    });

    state.openForEdit({ ...AWS_CONNECTION, credential_source: "pasted" }, backendOption("aws"), ALICE, loader);
    expect(state.credentialSource).toBe("pasted");
    expect(state.awsProfile).toBe("");
    expect(listedFor).toEqual(["modal"]);
  });

  it("lists the new backend's logins when the backend changes while a local login is selected", async () => {
    const state = new CloudAccountsModalState();
    const listedFor: string[] = [];
    const loader: LocalProfilesLoader = (backend) => {
      listedFor.push(backend);
      return Promise.resolve([localProfile({ name: `${backend}-login` })]);
    };
    state.openForBackend(backendOption("aws"), THIS_DEVICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    state.selectCredentialSource("local_profile", loader);
    await Promise.resolve();
    expect(state.awsProfile).toBe("aws-login");

    state.selectBackend(backendOption("modal"), "");
    state.refreshLocalProfiles(loader);
    await Promise.resolve();
    expect(listedFor).toEqual(["aws", "modal"]);
    expect(state.modalProfile).toBe("modal-login");

    // Back on pasted keys, a backend change lists nothing.
    state.selectCredentialSource("pasted", loader);
    state.selectBackend(backendOption("aws"), "us-west-2");
    state.refreshLocalProfiles(loader);
    expect(listedFor).toEqual(["aws", "modal"]);
  });

  it("names the login each backend uses on the source switch", () => {
    expect(localProfileChoiceLabel("aws")).toBe("Use an AWS profile on this device");
    expect(localProfileChoiceLabel("azure")).toBe("Use my az login on this device");
    expect(localProfileChoiceLabel("modal")).toBe("Use a Modal profile on this device");
  });
});

describe("CloudAccountsModalState edit mode", () => {

  it("opens with the owner, backend and region fixed, the alias prefilled, and every credential field empty", () => {
    const state = new CloudAccountsModalState();
    // Leftovers from an add that was abandoned must not pre-fill the edit.
    state.openForBackend(backendOption("aws"), ALICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    state.awsAccessKeyId = "AKIA-old";
    state.awsSecretAccessKey = "old-secret";
    state.alias = "something else";

    state.openForEdit(AWS_CONNECTION, backendOption("aws"), ALICE, NO_PROFILES);

    expect(state.mode).toBe("edit");
    expect(state.isOpen).toBe(true);
    expect(state.view).toBe("form");
    expect(state.editInstance).toBe(AWS_INSTANCE);
    expect(state.editOwnerLabel).toBe("alice@example.com");
    expect(state.editBackendLabel).toBe("AWS");
    expect(state.backend).toBe("aws");
    expect(state.isRegionPinned).toBe(true);
    expect(state.region).toBe("us-east-1");
    expect(state.alias).toBe("team aws");
    expect(state.awsAccessKeyId).toBe("");
    expect(state.awsSecretAccessKey).toBe("");
  });

  it("sends the alias and the full credential set as a PUT to the connection's own route, with no owner and no region", () => {
    const state = new CloudAccountsModalState();
    state.openForEdit(AWS_CONNECTION, backendOption("aws"), ALICE, NO_PROFILES);
    expect(state.requestTarget()).toEqual({
      method: "PUT",
      url: `/api/v1/desktop/cloud-accounts/${AWS_INSTANCE}`,
    });

    // Every credential field is required: a blank one is not "keep the old value".
    state.awsAccessKeyId = "AKIA-new";
    expect(state.buildRequestBody()).toBe("Both the access key ID and secret access key are required.");

    state.awsSecretAccessKey = "new-secret";
    state.alias = " rotated aws ";
    expect(state.buildRequestBody()).toEqual({
      alias: "rotated aws",
      backend: "aws",
      aws_access_key_id: "AKIA-new",
      aws_secret_access_key: "new-secret",
    });
  });

  it("goes back to the add route once an add is opened again", () => {
    const state = new CloudAccountsModalState();
    state.openForEdit(AWS_CONNECTION, backendOption("aws"), ALICE, NO_PROFILES);
    state.openForBackend(backendOption("modal"), THIS_DEVICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    expect(state.mode).toBe("add");
    expect(state.requestTarget()).toEqual({ method: "POST", url: "/api/v1/desktop/cloud-accounts" });
    state.alias = "fresh";
    state.modalTokenId = "ak";
    state.modalTokenSecret = "as";
    expect(state.buildRequestBody()).toMatchObject({ owner: LOCAL_PROFILE_USER_ID });
  });

  it("reads a zone-placed connection's zone as the fixed region, and none for a per-create backend", () => {
    const state = new CloudAccountsModalState();
    state.openForEdit(
      { instance: "gcp_" + "b".repeat(32), backend_label: "GCP", alias: "gcp", placement: { default_zone: "us-central1-a" } },
      backendOption("gcp"),
      THIS_DEVICE,
      NO_PROFILES,
    );
    expect(state.region).toBe("us-central1-a");
    expect(state.editOwnerLabel).toBe("This device only");

    state.openForEdit(
      { instance: "modal_" + "c".repeat(32), backend_label: "Modal", alias: "modal", placement: {} },
      backendOption("modal"),
      ALICE,
      NO_PROFILES,
    );
    expect(state.isRegionPinned).toBe(false);
    expect(state.region).toBe("");
  });
});

describe("connectionFailureMessage", () => {
  const FALLBACK = "Could not set up the connection.";

  it("points a master_password_required refusal at Settings or this device only", () => {
    expect(
      connectionFailureMessage(
        { error: "Account alice@example.com is locked here.", code: "master_password_required" },
        FALLBACK,
      ),
    ).toBe(
      "Syncing this connection needs a master password: set one in Settings and unlock the account here, " +
        "or add it to this device only.",
    );
  });

  it("keeps the provider's message above the fix-the-keys line for credentials_rejected", () => {
    expect(
      connectionFailureMessage({ error: "Account setup failed: AccessDenied", code: "credentials_rejected" }, FALLBACK),
    ).toBe("Account setup failed: AccessDenied\n\nNothing was saved; fix the keys or permissions and try again.");
    expect(connectionFailureMessage({ code: "credentials_rejected" }, FALLBACK)).toBe(
      "Nothing was saved; fix the keys or permissions and try again.",
    );
  });

  it("blames the network, not the keys, for sync_failed", () => {
    expect(connectionFailureMessage({ error: "push failed", code: "sync_failed" }, FALLBACK)).toBe(
      "The credentials work, but the connection could not be saved to your account. Check your network and " +
        "try again, or add it to this device only.",
    );
  });

  it("asks for a retry in a moment for account_listing_failed", () => {
    expect(connectionFailureMessage({ error: "listing failed", code: "account_listing_failed" }, FALLBACK)).toBe(
      "Could not read the signed-in accounts; try again in a moment.",
    );
  });

  it("names the operation for any other code, with the server's message when one came back", () => {
    expect(
      connectionFailureMessage(
        { error: "This connection was removed on another device.", code: "connection_removed_elsewhere" },
        FALLBACK,
      ),
    ).toBe("Could not set up the connection.\n\nThis connection was removed on another device.");
    expect(connectionFailureMessage({ error: "Unknown owner." }, FALLBACK)).toBe(
      "Could not set up the connection.\n\nUnknown owner.",
    );
    expect(connectionFailureMessage({}, FALLBACK)).toBe("Could not set up the connection.");
  });
});

describe("CloudAccountsModal view", () => {
  function rendered(state: CloudAccountsModalState): AnyVnode[] {
    const vnode = m(CloudAccountsModal, {
      state,
      regionOptionsByBackend: {},
      backends: CONNECTION_BACKEND_OPTIONS,
      ownerOptions: OWNER_OPTIONS,
      onAccountAdded: () => undefined,
    }) as m.Vnode<CloudAccountsModalAttrs>;
    const component = CloudAccountsModal();
    return collectVnodes(component.view.call(component, vnode));
  }

  it("offers the credential-source switch for backends with a local login, and the login field once chosen", () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(backendOption("aws"), THIS_DEVICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    const pasted = rendered(state);
    expect(pasted.find((node) => attrsOf(node).id === "ca-credential-source")).toBeDefined();
    expect(pasted.find((node) => attrsOf(node).id === "ca-fields-aws")).toBeDefined();
    expect(pasted.find((node) => attrsOf(node).id === "ca-fields-aws-profile")).toBeUndefined();

    state.credentialSource = "local_profile";
    state.localProfiles = [localProfile({ name: "work" })];
    const local = rendered(state);
    expect(local.find((node) => attrsOf(node).id === "ca-fields-aws")).toBeUndefined();
    expect(local.find((node) => attrsOf(node).id === "ca-fields-aws-profile")).toBeDefined();
    expect(local.find((node) => attrsOf(node).id === "ca-aws-profile-list")).toBeDefined();
    expect(allText(local.find((node) => attrsOf(node).id === "ca-local-profile-note"))).toContain(
      "aws configure --profile",
    );

    state.openForBackend(backendOption("gcp"), THIS_DEVICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    expect(rendered(state).find((node) => attrsOf(node).id === "ca-credential-source")).toBeUndefined();
  });

  it("opens on the alpha callout, in add and edit mode alike, with the Discord invite", () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(backendOption("aws"), ALICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    const added = rendered(state);
    const notice = added.find((node) => attrsOf(node).id === "ca-alpha-notice");
    expect(notice).toBeDefined();
    expect(allText(notice)).toContain(ALPHA_FEATURE_TITLE);
    const link = collectVnodes(notice).find((node) => attrsOf(node).href === COMMUNITY_DISCORD_URL);
    expect(link).toBeDefined();
    expect(attrsOf(link as AnyVnode).target).toBe("_blank");

    state.openForEdit(AWS_CONNECTION, backendOption("aws"), ALICE, NO_PROFILES);
    expect(rendered(state).find((node) => attrsOf(node).id === "ca-alpha-notice")).toBeDefined();
  });
});

describe("CloudAccountsModalState operation outcome", () => {
  const ACCOUNT: CloudAccountOption = {
    name: "modal_" + "c".repeat(32),
    alias: "eval modal",
    backend: "modal",
    region: "",
    owner_user_id: LOCAL_PROFILE_USER_ID,
    is_region_pinned: false,
  };

  it("hands a finished registration to the form and says where the connection lives", () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(backendOption("modal"), THIS_DEVICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    state.modalTokenSecret = "as-secret";
    state.isPrepareInFlight = true;
    state.view = "progress";
    const added: CloudAccountOption[] = [];

    state.finishWithAccount(ACCOUNT, (account) => added.push(account));

    expect(added).toEqual([ACCOUNT]);
    expect(state.view).toBe("result");
    expect(state.isPrepareInFlight).toBe(false);
    expect(state.resultHeading).toBe("Connection ready");
    expect(state.resultMessage).toBe('"eval modal" is set up. It stays on this device.');
    expect(state.modalTokenSecret).toBe("");
    expect(state.isBackShownOnResult).toBe(false);
  });

  it("shows a failed operation's reason with a way back to the form, keeping the paste", () => {
    const state = new CloudAccountsModalState();
    state.openForBackend(backendOption("modal"), THIS_DEVICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    state.modalTokenSecret = "as-secret";
    state.isPrepareInFlight = true;
    state.view = "progress";

    state.finishWithFailure({ error: "Account setup failed: bad token", code: "credentials_rejected" });

    expect(state.view).toBe("result");
    expect(state.isPrepareInFlight).toBe(false);
    expect(state.resultHeading).toBe("Connection setup failed");
    expect(state.resultMessage).toContain("Account setup failed: bad token");
    expect(state.isBackShownOnResult).toBe(true);
    expect(state.modalTokenSecret).toBe("as-secret");
  });
});
