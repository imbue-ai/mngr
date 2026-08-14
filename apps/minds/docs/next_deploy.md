# Next deployment: running checklist

A scratchpad collecting everything the next staging / production deployment
must get right. Add to this doc as work lands; fold the items into the release
runbook ([release.md](./release.md)) when the release is actually cut, then
reset this doc.

The full audit of changes since `minds-v0.3.11` (the last production release)
has been completed; this checklist is its distillation. Guiding decisions from
that audit:

- Minimal changes. Old workspaces may need `update-self` before certain
  features (notably sharing) work again; that is acceptable.
- Old workspaces MUST remain at least accessible under the new desktop client.
- Re-sharing a workspace should self-heal once the workspace has been updated
  to the newest default-workspace-template release via `update-self`.
- The web surface (hosted web chrome, `/hosts/claim`) IS deployed and active
  on staging and production, but unadvertised: we are its only users, for
  testing. It gets no public migration/compat guarantees yet, but it must
  work.

## Code that must land before the release is cut

- [x] **Connector: temporary `/account` compat fields for v0.3.11 clients.**
  Done: `/account`, plan-switch, and admin account responses serve hardcoded
  `max_tunnels` / `max_services_per_tunnel` / `tunnels` zeros (CLEANUP-marked;
  removal is on the post-deploy list).
- [x] **Desktop: refuse to enable sharing on a pre-share-gateway workspace
  with an actionable message.** Done: the enable path probes for
  `system/services/share_gateway` first and refuses with the update-self
  pointer; re-sharing after `update-self` self-heals (CLEANUP-marked).
- [x] **default-workspace-template: retire cloudflared state on update.**
  Done (default-workspace-template#414): bootstrap removes the stale
  `data/.secrets/cloudflare_tunnel.env` idempotently at boot. Cloudflare-side
  records are still cleaned centrally (see post-deploy cleanup).
- [x] **RSA -> Ed25519 client-key migration.** Done for per-host-keyed
  (imbue_cloud) workspaces: the desktop migrates RSA-keyed RUNNING hosts in
  the background (append on both layers, verify, swap, rollback on failure;
  once per host), and `minds migrate-ssh-keys` runs the same pass manually.
  Lima's shared root key is out of scope for now (each VM's authorized_keys
  is overwritten from its baked lima.yaml every boot); lima workspaces gain
  web-drivability later via per-host keys.
- [x] **`[web_workspaces]` defaults from the release tag.** Done:
  `template_repo`/`template_ref` are optional, resolved as env var
  (`MINDS_WEB_TEMPLATE_REPO`/`REF`) > deploy.toml pin > default (canonical
  repo key + `FALLBACK_BRANCH`, now in `build_info.py`); staging and
  production carry `[web_workspaces]` blocks, so web create is active there
  (unadvertised).

## Phase 0: independent preparation and verification

Everything here can be done and verified BEFORE any change that could affect
existing staging or production users, and should be. Verify each item against
a dev/ci env first where possible.

- [ ] **Relays.** Provision + deploy + DNS the staging and production relay
  fleets (`us1`/`us2`-style regions) with `share-relay`; confirm each tier's
  `SHARE_RELAY_ENDPOINTS` Vault entry matches the relays actually deployed.
  Confirm the content domains' Public-Suffix-List situation (each region is
  one wildcard DNS record and one PSL entry; PSL propagation is slow and
  affects cross-user cookie isolation between shared workspaces).
- [ ] **Vault entries per tier.** Confirm before deploying: the new
  `relay-ssh` operator-only entry; `OVH_CLOUD_PROJECT_ID`;
  `AUTH_WEBSITE_DOMAIN` (required -- the connector refuses to start without
  it); `ACCOUNTS_COOKIE_DOMAIN`; Turnstile keys for hosted signup;
  `BROKER_GOOGLE_CLIENT_ID`/`SECRET` (share-broker Google sign-in; button is
  hidden when unset); `MINDS_ADMIN_KEY` (the `MINDS_PAID_ADMIN_KEY` spelling
  is deprecated); `OAUTH_REDIRECTOR_URL` where applicable.
- [ ] **OAuth redirector.** Deploy the `oauth_redirector` app for the tiers
  that use it and register its URL as the redirect URI on the Google OAuth
  client.
- [ ] **Pinned Modal images.** Run the image-requirements freshness preflight
  cleanly (`just export-image-requirements` committed and matching
  `uv.lock`); confirm `pnpm` is available on the deploy machine (`minds env
  deploy` now builds two connector frontend bundles). Note the first deploy
  after the pinning change bumps in-container package versions to the current
  lock resolution (e.g. fastapi 0.139.2) -- treat the staging deploy as the
  canary for behavior drift.
- [ ] **Cloudflare R2 token.** Confirm staging and production
  `CLOUDFLARE_API_TOKEN` are the account-owned (`cfat_`) token with the
  documented permission set (the bucket routes refuse user-owned tokens).
- [ ] **Full dev-tier rehearsal.** Deploy a fresh dev/ci env with
  production-shaped config and run the deployment tests end to end (accounts,
  create, share, backups) before touching staging.
- [ ] **Old-workspace compatibility smoke test.** Run the new desktop client
  against workspaces created at `minds-v0.3.11` (at least docker + lima, and
  an imbue_cloud slice if available): the workspace must open and be usable
  (bare-origin fallback routing, permission cards, terminals), sharing must
  fail with the actionable too-old message, and `update-self` followed by
  re-share must produce a working share.
- [ ] **Reboot-resilience backfill sweep.** The sweep now exists
  (`just backfill-autostart`, wrapping `minds server backfill-autostart`;
  start with `--dry-run`) -- staging-test it here, run it post-deploy.

## Phase 1: staging

- [ ] Deploy connector + LiteLLM proxy to staging (`minds env deploy
  --yes-i-mean-staging`). Migrations 018-023 run here; `020` drops the tunnel
  entitlement columns, so the `/account` compat shim above must be deployed
  with (not after) it.
- [ ] Verify on staging: sign-in (browser flow and the deprecated JSON path a
  v0.3.11 client uses), account page from a v0.3.11 client build, create,
  share + revoke, backups, deployment tests.
- [ ] Existing signed-in sessions must sign in once more (the partitioned
  cookie is only set at login) -- verify, and fold into the release notes.

## Phase 2: production

- [ ] default-workspace-template release: merge the outstanding dwt branches
  to dwt `main`, vendored mngr sync, release tag pair (mngr + dwt) -- see
  [release.md](./release.md) and the `release-minds` skill.
- [ ] Deploy connector + LiteLLM to production (same shim/migration coupling
  as staging).
- [ ] Cut and publish the desktop release promptly after the connector
  deploy: v0.3.11 clients keep working for access and accounts (via the
  compat shim) but their sharing surface 404s until they update.
- [ ] Re-bake the production pool from the release tag. Desktop fast-path
  leases match `repo_branch_or_tag` exactly (the app pins `FALLBACK_BRANCH`),
  and web claims match the tier's `[web_workspaces]` pin -- which now
  defaults to the same tag -- so one re-bake serves both; until it happens,
  desktop creates silently take the slow rebuild path and web creates 503.
- [ ] Verify web create end to end on staging (and then production): sign in
  at the hosted chrome, create, open, destroy.
- [ ] `just prep-server` on every existing production box (box-level slice
  autostart unit + the unattended-upgrades no-auto-reboot pin; also applies
  any pending lima upgrade) -- from
  [reboot-resilience-rollout.md](./reboot-resilience-rollout.md).

## Post-deploy cleanup

- [ ] **In-VM reboot-resilience backfill.** Run the backfill sweep script
  (written and staging-tested in Phase 0) over every existing slice VM --
  see [reboot-resilience-rollout.md](./reboot-resilience-rollout.md) Step 2.
- [ ] **Old `dev1` relay.** Destroy the instance (`just list-share-relays` /
  `just destroy-share-relay`), remove the `dev1` DNS records, and re-enable
  sharing on any workspace whose share row was created with region `dev1`.
- [ ] **Cloudflare account cleanup.** Delete the orphaned tunnel-era
  resources for previously shared workspaces: tunnels, DNS CNAMEs, Access
  applications, service tokens, and Workers KV entries. No product code can
  tear these down anymore, and a not-yet-updated workspace's cloudflared
  keeps its tunnel alive until this cleanup (or its `update-self`) severs it.
- [ ] **Remove the `/account` compat fields** (`max_tunnels`,
  `max_services_per_tunnel`, `tunnels`) once the desktop fleet is on the new
  release.
- [ ] Consider dropping the orphaned tunnel-era DB tables in a later
  migration (harmless meanwhile).

## Accounts / cookies

- [ ] **Every existing signed-in session on every tier must sign in once
  more** after the deploy: the accounts session cookie changed to
  `SameSite=None; Secure; Partitioned`, and only a fresh login sets the new
  attributes. Fold into the release notes / announcement.

## Production chrome domain

- [ ] Provision `minds.imbue.com` as a second Modal custom domain on the
  production connector app (a Modal dashboard custom-domain entry pointing at
  the connector function, plus the DNS record) as part of this deployment.
  The hosted web chrome is path-served under `/web` on the connector origin,
  so the domain change is routing only; confirm `AUTH_WEBSITE_DOMAIN` /
  `ACCOUNTS_COOKIE_DOMAIN` / `SHARE_CHROME_ORIGIN` agree with whichever
  origin users are sent to. (Phase 0: safe to set up before any deploy.)

## mngr

- Client SSH keygen switched from RSA-4096 to Ed25519 (OpenSSH format).
  Existing on-disk RSA keys keep working for SSH; only fresh key dirs
  generate Ed25519. Pre-release workspaces' RSA keys cannot sign owner-exec
  envelopes, so the RSA -> Ed25519 migration (see "code that must land") is
  in scope for this release to make existing workspaces drivable from the
  web chrome.
- Records synced by updated installs carry Ed25519 keys; v0.3.11 installs can
  only materialize RSA keys, so a multi-device user with one un-updated
  device cannot open a workspace created from an updated device until that
  device updates. Accepted (client-side limitation; fixed by updating).

## Known mixed-fleet states (accepted, no action)

- Pool slices baked from the old tag accept blind grants writes (no CAS)
  until re-baked; the contract is backward compatible.
- Old workspaces keep their label-less service registrations until
  `update-self` restarts their services, at which point `forward_port.py`
  mints origin labels for legacy rows automatically; meanwhile the forwarder
  and desktop route them by service name.

- Old workspaces' system_interface still renders service panels (terminal,
  browser) as iframes at `/service/<name>/...` on its own origin, behind a
  service-worker bootstrap whose `document.cookie` write the new desktop's
  partitioned content embedding rejects -- without mitigation the panel
  reloads forever (found in the dev-josh-1 rehearsal of this deployment).
  The forward proxy now 307-redirects those navigations to the service's own
  origin (CLEANUP-marked in `mngr_forward/server.py`), so pre-update
  workspaces keep working terminals; `update-self` retires the whole
  mechanism per workspace.
