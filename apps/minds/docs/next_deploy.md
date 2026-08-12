# Next deployment: running checklist

A scratchpad collecting everything the next staging / production deployment
must get right. Add to this doc as work lands; fold the items into the release
runbook ([release.md](./release.md)) when the release is actually cut, then
reset this doc.

## Audit required

- [ ] **Audit every change since `minds-v0.3.11`** (the last production
  release; `main` is already ~750 commits past it). The items below were
  collected while working on the web-client branches and are NOT exhaustive --
  walk the per-project changelogs (`apps/minds/changelog/`,
  `apps/remote_service_connector/changelog/`, `libs/mngr/changelog/`, and
  default-workspace-template's changelogs) for other deploy-relevant details.

## Web workspaces (browser create)

- [ ] Add a `[web_workspaces]` block to `envs/staging/deploy.toml` and
  `envs/production/deploy.toml` (they have none today, which is what disables
  web create on those tiers):

  ```toml
  [web_workspaces]
  template_repo = "github.com/imbue-ai/default-workspace-template"
  template_ref = "<the minds release tag>"   # never a branch on these tiers
  ```

  Optionally pin a blessed shape (`cpus` / `memory_gb`); unset leaves the
  lease unconstrained.
- [ ] Bake those tiers' pools from the SAME tag the pin names. Claims lease
  only exact attribute matches, so the pin and the bake must move together
  (a mismatch 503s every web create).
- [ ] Dev keeps a branch pin (`envs/dev/deploy.toml`); whenever the dwt branch
  advances, bump the pin and re-bake the dev pool together.

## Accounts / cookies

- [ ] **Every existing signed-in session on every tier must sign in once
  more** after the deploy: the accounts session cookie changed to
  `SameSite=None; Secure; Partitioned`, and only a fresh login sets the new
  attributes. Fold into the release notes / announcement.

## Production chrome domain

- [ ] Provision `minds.imbue.com` as a second Modal custom domain on the
  production connector app (nothing in the repo configures custom domains
  yet -- this is a manual Modal + DNS step; document it here once done).

## Relays

- [ ] Dev/ci tiers now use per-env relays (region label = env name; `minds
  env deploy` overrides the sharing secret; `just provision-dev-relay`
  stands the relay up). Staging / production relays (`us1` / `us2` style
  regions) are unaffected, but confirm each tier's `SHARE_RELAY_ENDPOINTS`
  Vault entry matches the relays actually deployed before cutting over.
- [ ] The old shared `dev1` relay (currently pointed at dev-josh-2's
  connector) becomes obsolete once each dev env has its own relay: destroy
  the instance (`just list-share-relays` / `just destroy-share-relay`) and
  remove the `dev1` records, and re-enable sharing on any workspace whose
  share row was created with region `dev1`.

## default-workspace-template

- [ ] The dwt branches `mngr/final-web-details` and
  `mngr/hopefully-last-web-details` must merge to dwt `main`, then: vendored
  mngr sync, release tag pair (mngr + dwt), pool re-bakes from the tag. See
  [release.md](./release.md) and the `release-minds` skill.
- [ ] owner-exec grants endpoints gained compare-and-swap (`revision` /
  `base_revision`); only pools baked from a tag containing that change serve
  the CAS contract. Older slices still accept blind writes (the contract is
  backward compatible), but note the mixed fleet while it lasts.

## mngr

- [ ] Client SSH keygen switched from RSA-4096 to Ed25519 (OpenSSH format).
  No migration needed -- existing on-disk RSA keys keep working; only fresh
  key dirs generate Ed25519. Workspaces created before the release keep RSA
  keys and therefore cannot sign owner-exec envelopes until recreated.
