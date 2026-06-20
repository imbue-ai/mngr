# Deprecate OVH VPS for Imbue Cloud (default to bare-metal slices)

## Overview

- Deprecate the **classic OVH VPS** pool backend (`ovh_vps`, one VPS per pool host) **as used by Imbue Cloud / Minds**, and make **bare-metal slices** the default for all pool baking.
- Scope is OVH-in-Imbue-Cloud only: the `mngr_ovh` provider library, the `mngr ovh list` operator command, and the vultr/aws launch modes are **out of scope** and untouched.
- Stop the Minds admin CLI, `minds pool create`, the justfile, and the remote connector service from baking or allocating *new* OVH VPS hosts — while keeping the ability to list, destroy, lease, release, and clean up the VPS hosts already in use.
- Reframe the user-facing mental model: Imbue Cloud agents run on bare-metal slices. OVH is only the current internal supplier of the bare-metal boxes those slices are carved on (other suppliers may follow), so OVH should not appear as a user-facing default anywhere — only as a slice-box implementation detail and in legacy-VPS teardown.
- This is a deprecation, not a removal: support for existing OVH VPS hosts stays until everyone is migrated off and the old instances are destroyed; only then will the `ovh_vps` code paths be removed in a later change.

## Expected behavior

- `mngr imbue_cloud admin pool create` and `minds pool create` default to `--backend slice`; a bare invocation bakes a slice (and errors on the missing required `--server-id`, which is expected).
- Passing `--backend ovh_vps` to either CLI fails fast with a hard deprecation error that points the operator at `--backend slice`; there is no override. In `minds pool create`, the rejection happens before any Vault / OVH credential resolution.
- The `bake-pool-host-{dev,prod}` justfile recipes no longer exist; `bake-slice-{dev,prod}`, `list-pool-hosts`, and `destroy-pool-host` remain the supported pool recipes.
- All operations on already-baked OVH VPS pool hosts still work unchanged: `admin pool list/destroy`, `minds pool list/destroy`, the connector's `/hosts/release` + hourly cleanup OVH branch, and `minds env destroy` OVH-tag teardown.
- The remote connector service is unchanged in behavior: it leases whatever `available` rows exist (backend-blind) and never bakes, so once the pool is baked only with slices it naturally hands out slices; existing OVH leases and their teardown keep working.
- The imbue_cloud slow path (rebuild on an already-leased host via the FCT Dockerfile) is unaffected — it never orders a new VPS.
- The desktop client is unaffected: it leases via the backend-blind `imbue_cloud` provider and already displays "Imbue Cloud".
- Documentation describes a single, slice-based pool workflow; OVH appears only as an internal bare-metal box supplier detail and in a clearly-marked legacy-VPS teardown section.

## Changes

### Admin CLI (`libs/mngr_imbue_cloud`)
- Flip `mngr imbue_cloud admin pool create`'s `--backend` default from `ovh_vps` to `slice`.
- Reject `--backend ovh_vps` with a hard deprecation error (migration message pointing at `slice`); keep the option and the `ovh_vps` value so the error is informative, but make the legacy bake path unreachable from the CLI.
- Leave the underlying `ovh_vps` baking helper and all OVH list/destroy/teardown code in place (still needed for existing hosts and not reachable via a new bake).
- Reframe option help text away from presenting OVH as a default; generalize trivial wording toward "bare-metal box" where cheap.
- Update `libs/mngr_imbue_cloud/README.md` so slices are the documented default and OVH-as-VPS is described as legacy.

### Minds CLI (`apps/minds`)
- Flip `minds pool create`'s `--backend` default from `_BACKEND_OVH_VPS` to `_BACKEND_SLICE`.
- Reject `--backend ovh_vps` in `minds pool create` itself, failing fast before Vault / OVH credential resolution (in addition to the admin CLI's own rejection).
- Update `minds pool create` help/usage wording to match the slice-default framing.

### Remote connector service (`apps/remote_service_connector`)
- No behavior change: stays backend-blind for leasing; retains the OVH release + hourly cleanup branch for existing hosts.

### justfile (root → `dev` project)
- Remove the `bake-pool-host-dev` and `bake-pool-host-prod` recipes and their OVH-pool-bake comment block.
- Keep `bake-slice-{dev,prod}`, `list-pool-hosts`, `destroy-pool-host`; update surrounding comments so slices are the primary path and OVH is only referenced for legacy teardown.

### Documentation (`apps/minds/docs`)
- Rewrite `host-pool-setup.md` slice-first: the only documented baking workflow is bare-metal slices (briefly referencing the existing `admin server` box-lifecycle recipes rather than a full box-provisioning prerequisite). Add a short "Legacy OVH VPS teardown" section for destroying existing VPS hosts.
- Reframe the OVH AK/AS/CK credentials (in `host-pool-setup.md`, `vault-setup.md`, `staging-bringup.md`) as "bare-metal box supplier credentials (also used to tear down legacy VPS hosts)."
- Scrub/reframe OVH across the broader minds docs (`design.md`, `overview.md`, `environments.md`): per-tier "OVH account" becomes "bare-metal box supplier account"; generalize trivial "OVH box" wording to "bare-metal box."
- Leave the `ovh` `deploy.toml` Modal secret key name untouched (internal config), and leave desktop `provider_display.py` (`ovh` → "OVH") untouched (direct-provider label, out of scope).

### Forever-claude-template (separate repo, `~/project/forever-claude-template`)
- Work in a `.external_worktrees/forever-claude-template` worktree on the same branch (`mngr/deprecate-ovh-vps`); commit there with its own changelog entry.
- In `.mngr/settings.toml`: keep the `[providers.ovh]` block but mark it deprecated/legacy in comments and drop the "imbue-cloud pool-bake's default" framing; reframe OVH-specific template comments toward slices.

### Tests
- Delete the existing `ovh_vps`-bake tests.
- Add tests asserting that `slice` is the default backend and that `--backend ovh_vps` raises the deprecation error, for both the admin CLI and `minds pool create`.

### Changelog
- Add one per-branch changelog stub per touched project: `libs/mngr_imbue_cloud`, `apps/minds`, and `dev` (for the justfile/root changes), plus the FCT repo's own changelog entry.
