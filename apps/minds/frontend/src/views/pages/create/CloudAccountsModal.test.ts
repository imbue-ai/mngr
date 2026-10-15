import { describe, expect, it } from "vitest";
import type { ConnectionBackendOption } from "../../../models/create";
import { LOCAL_PROFILE_USER_ID } from "../../../models/providers";
import { CONNECTION_BACKEND_OPTIONS } from "../../../testing";
import {
  CloudAccountsModalState,
  connectionFailureMessage,
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

    state.selectBackend({ name: "hetzner", label: "Hetzner", is_region_pinned: false }, "");
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

describe("CloudAccountsModalState edit mode", () => {
  const AWS_INSTANCE = "aws_" + "a".repeat(32);
  const AWS_CONNECTION = {
    instance: AWS_INSTANCE,
    backend_label: "AWS",
    alias: "team aws",
    placement: { default_region: "us-east-1" },
  };

  it("opens with the owner, backend and region fixed, the alias prefilled, and every credential field empty", () => {
    const state = new CloudAccountsModalState();
    // Leftovers from an add that was abandoned must not pre-fill the edit.
    state.openForBackend(backendOption("aws"), ALICE, DEFAULT_REGION_BY_BACKEND, OWNER_OPTIONS);
    state.awsAccessKeyId = "AKIA-old";
    state.awsSecretAccessKey = "old-secret";
    state.alias = "something else";

    state.openForEdit(AWS_CONNECTION, backendOption("aws"), ALICE);

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
    state.openForEdit(AWS_CONNECTION, backendOption("aws"), ALICE);
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
    state.openForEdit(AWS_CONNECTION, backendOption("aws"), ALICE);
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
    );
    expect(state.region).toBe("us-central1-a");
    expect(state.editOwnerLabel).toBe("This device only");

    state.openForEdit(
      { instance: "modal_" + "c".repeat(32), backend_label: "Modal", alias: "modal", placement: {} },
      backendOption("modal"),
      ALICE,
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
