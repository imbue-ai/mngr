# AWS compute provider for the minds app

## Overview

- Add **AWS** as a new minds compute provider, alongside the existing local (Docker/Lima) and cloud (Vultr, Imbue Cloud) options, so users can launch a workspace on an Amazon EC2 instance.
- AWS is a `mngr_vps_docker`-based provider (the same family as Vultr/OVH): the EC2 instance is the **outer host** (Debian + Docker + runsc), and the agent runs in a **runsc-hardened Docker container** (the workspace). The secure remote latchkey gateway runs on the outer host, outside the agent's container — inherited automatically because `AwsProvider` exposes a vps_docker outer host.
- Rename the existing `CLOUD` launch mode to `VULTR` (clean rename, no backward-compat alias) so each compute option names its provider plainly; `AWS` becomes a sibling launch mode.
- Multi-region is modeled **at the minds level, not in mngr**: EC2's API is per-region (one boto3 client = one region), so minds writes one `[providers.aws-<region>]` block per configured region into the mngr profile settings at startup and addresses each create as `@host.aws-<region>`. The user always picks a region in the create form; the listing collapses every `aws-<region>` back to the single friendly label "AWS".
- Credentials use boto3's default chain (env vars / `AWS_PROFILE` / `~/.aws`) only for this first pass, surfaced by an on-form note; minds auto-runs `mngr aws prepare` (made read-only-first) for the chosen region before the create so it succeeds even with a key that lacks security-group-mutating permissions.

## Expected behavior

- The create form's compute-provider selector lists Docker, Lima, **Vultr** (renamed from Cloud), Imbue Cloud, and **AWS**.
- Selecting **AWS** reveals:
  - A **required region** dropdown listing the configured AWS regions (the 8 with pinned default AMIs), pre-selected to the user's geo-nearest region (same precedence Vultr/Imbue Cloud use: last-used → geo-nearest → hardcoded default).
  - A short credentials note explaining that AWS credentials are read from the environment (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`, or `AWS_PROFILE`, or `~/.aws/credentials`), with no key entry field.
- Submitting an AWS workspace:
  - Provisions an EC2 instance in the chosen region, installs Docker + runsc, and runs the agent inside a runsc-sandboxed container — the same hardened workspace shape as Vultr/OVH workspaces.
  - The host is long-lived: it never idle-shuts-down and has no max-lifetime auto-termination timer.
  - Before launching, minds ensures the region's security group exists; if it already exists with the needed SSH ingress, this is a no-op that succeeds even with a read-only AWS key. If the group is missing and the key cannot create it, the user gets a clear, actionable error naming the region and the required one-time admin step.
  - The secure latchkey gateway runs on the EC2 outer host (outside the agent's container) and is reverse-tunneled into the container, so the agent's third-party access is governed by a gateway it cannot tamper with — identical to the Vultr/Imbue Cloud security posture.
- The workspace listing shows a compute-provider label on **every** workspace row. AWS workspaces (whose underlying provider name is `aws-<region>`) all display as **"AWS"**; Vultr/Docker/Lima/Imbue Cloud display their own friendly labels. Listing, Start/Stop, recovery, destroy, and discovery work for AWS workspaces exactly as they do for other remote workspaces.
- If AWS credentials are absent entirely, the AWS option still appears; the failure surfaces as a clear credentials error at create time (consistent with how other modes behave), not as a hidden/disabled option.
- Existing non-AWS behavior is unchanged except for the user-visible rename of "Cloud" → "Vultr"; any caller still sending the old `CLOUD` launch-mode value is rejected (no silent aliasing).

## Changes

### `apps/minds` — launch mode and create flow

- Rename the `CLOUD` launch mode to `VULTR` and add a new `AWS` launch mode; update every match/branch over the launch mode so the set is handled exhaustively.
- Map the `AWS` launch mode to the create address `@<host>.aws-<region>`, where `<region>` is the form-selected region, and create with the `main` + `aws` templates (mirroring how the other remote modes select their provider via the address while the template supplies the shared knobs).
- Thread the selected AWS region from the create form through the background creation path into the create command.
- Define a single source of truth for the set of supported AWS regions (the regions with pinned default AMIs) used both to write the provider blocks and to populate the form.
- Add an inline AWS credentials help note to the create form, shown when AWS is selected.
- Update the per-launch-mode region wiring so AWS is treated as a region-bearing provider (require a region, offer the configured regions, resolve/persist the default via the existing region-preference precedence). Rename the Vultr-related region keys/labels to match the launch-mode rename where they are user-visible.
- Update the expected-creation-duration mapping for the renamed Vultr mode and add an entry for AWS.

### `apps/minds` — startup provider-block writing

- Extend the startup routine that already writes minds-managed provider blocks into the mngr profile settings (the same place the Imbue Cloud per-account blocks and legacy-block cleanup live) to also write one `[providers.aws-<region>]` block per configured region, each pinned to its region and carrying the container-hardening knobs (gVisor/runsc) that mirror the Vultr/OVH provider blocks.
- Ensure these blocks are idempotent and kept in sync on each startup, consistent with the existing imbue_cloud block management.

### `apps/minds` — security-group preparation

- Before an AWS create, have minds invoke `mngr aws prepare` for the chosen region, surfacing a clear, actionable error (naming the region and the admin step) when the security group is missing and the current key cannot create it.

### `apps/minds` — listing / provider display

- Surface the compute-provider label on every workspace row in the landing/listing UI, sourced from the already-discovered provider name.
- Add a friendly-label mapping that collapses any `aws-<region>` provider name to "AWS" and maps the other known providers (Vultr, Docker, Lima, Imbue Cloud) to their display names, with a sensible fallback for unknown providers.

### `libs/mngr_aws` — read-only-first `prepare`

- Make `mngr aws prepare` perform a read-only check first: if the security group already exists with the required SSH ingress, succeed without issuing any write API call (so it works with a key that only has describe permissions). Only attempt the privileged create/authorize calls when something is actually missing, and surface a clear permission error if those writes are then denied.

### `forever-claude-template` — AWS create template

- Add a `[create_templates.aws]` block mirroring the Vultr/OVH templates: container target path, `idle_mode = "disabled"`, runsc/security `start_arg` hardening, the standard host-env forwarding, and the first-boot seed step. No `provider` line (the create address selects the region-specific provider), no region or instance-type baked in, and **no** `auto_shutdown_seconds` (the host must stay long-lived).
- Provide a changelog entry for the template repo as required by its own contribution rules.

### Packaging

- Ensure the `mngr_aws` plugin is available in the minds app's mngr install (added as a dependency the way the other provider plugins are), so the host-side `mngr` can resolve the `aws` backend.

### Tests

- Unit tests for the pure/wiring pieces: launch-mode → create-address mapping (including the region suffix), the friendly-label provider collapse (`aws-<region>` → "AWS"), the startup writing of per-region provider blocks, the region-form wiring for AWS, and `mngr aws prepare`'s read-only-first behavior (no writes when already prepared; clear error when a write is denied).
- A new **minds release test** (manually run during this work) that drives a real AWS workspace create through the minds create path and asserts the runsc container is up **and** the latchkey gateway is running on the EC2 outer host / reachable from the container.
- Per-project changelog entries for each touched project (`apps/minds`, `libs/mngr_aws`, the FCT template repo, and `dev` if any root-level files change).
