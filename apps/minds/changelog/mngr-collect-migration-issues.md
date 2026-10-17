Restructured `docs/next_deploy.md` from a raw scratchpad into the actual
deployment checklist for the next staging/production release, based on a
completed audit of every change since `minds-v0.3.11` (per-project changelogs
in this repo and in default-workspace-template, plus targeted diff
verification).

The checklist is now organized so that everything independent is prepared and
verified BEFORE any change that could affect existing staging or production
users: a "code that must land before the release" section (the connector
`/account` compat fields for v0.3.11 clients, the desktop guard that refuses
to share a pre-share-gateway workspace with an update-self pointer, and the
cloudflared state cleanup in the workspace template), a Phase 0 of
independent preparation (relays + PSL, Vault entries, the oauth redirector,
pinned-image preflights, the Cloudflare R2 token migration, a dev-tier
rehearsal, an old-workspace compatibility smoke test, and the
reboot-resilience in-VM backfill script), then staging, then production
(including the pool re-bake and the fleet sweeps), then a post-deploy cleanup
list (the old `dev1` relay, orphaned Cloudflare tunnel-era resources, and
removal of the temporary `/account` compat fields).

Also records the audit's accepted no-action outcomes: old label-less service
registrations self-heal on `update-self`, v0.3.11 installs that never update
cannot materialize Ed25519-keyed records (fixed by updating), and lima's
shared client key stays RSA for now. (The checklist was subsequently revised
as decisions landed: the web surface IS active -- unadvertised -- on staging
and production, and the code items below shipped on this branch.)

Deployment-prep code changes on the same branch:

One-off RSA -> Ed25519 client SSH key migration: the desktop client now
rotates per-host-keyed (imbue_cloud layout) workspaces' RSA client keys to
Ed25519 in the background -- the owner-exec channel the hosted web chrome
drives only accepts Ed25519 signatures. The new public key is authorized on
both layers (container via `mngr exec`, outer host via `mngr exec --outer`)
before the local files are swapped, with an end-to-end verify and automatic
rollback to the preserved RSA pair, so the rotation can never lock the
install out. Runs against RUNNING hosts only, once per host (marker under
the minds data dir, per-session attempt cap); `minds migrate-ssh-keys` runs
the same pass manually. The lima provider's shared root key is deliberately
out of scope (its VMs overwrite authorized_keys from the baked lima.yaml on
every boot).

Enabling sharing on a workspace created from a pre-share-gateway template
(minds-v0.3.11 and older) is now refused up front with an actionable message
(update the workspace via update-self, then share again) instead of going
active on the connector and silently never becoming reachable. Marked
CLEANUP for removal once no supported workspaces predate the share gateway.

The `[web_workspaces]` deploy.toml pins are now optional: `minds env deploy`
resolves the web-create template as `MINDS_WEB_TEMPLATE_REPO`/`REF` env vars
> the deploy.toml pin > the defaults (the canonical default-workspace-template
repo key and the app's pinned release tag). `FALLBACK_BRANCH` moved to
`build_info.py` (re-exported via `desktop_client/workspace_defaults.py`) so
deploy-time code shares the desktop app's pin without importing the desktop
client.
Staging and production now carry `[web_workspaces]` blocks, so web create is
active (unadvertised) on both tiers.

New `minds server backfill-autostart` (and `just backfill-autostart`):
env-aware wrapper for the reboot-resilience in-VM backfill sweep (see
`docs/reboot-resilience-rollout.md`, which now points at the sweep instead of
saying it must be written).
