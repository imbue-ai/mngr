Updated the next-deploy runbook (docs/next_deploy.md) with corrections discovered while preparing the 0.3.14 staging rollout rehearsal:

- Relay fleet sequencing: relays can only be *provisioned* before the connector deploy -- registration and frps deploy need the new connector's relays table/endpoints (migration 025), so they run immediately after each tier's deploy, with a short accepted gap where new shares cannot be created.

- Public-Suffix-List entries for the share content domains are explicitly deferred until user volume justifies them; regions run on their wildcard DNS record sets alone for now.

- The staging deploy runs migrations 018-025 (not 018-023): 024 adds workspace stop/start (requires the tier `storage` Vault entry, now called out in the Phase 0 Vault checklist) and 025 creates the relays table.

- The OAuth redirector item is marked dev/CI-only (staging/production register their stable accounts domain directly), and the chrome-domain section is rewritten as the full two-host origin cutover for BOTH tiers (see below).

Added the `[origins]` deploy.toml block (accounts_origin / chrome_origin / cookie_domain): staging and production now commit their user-facing origin layout (accounts.imbue-staging.com + minds.imbue-staging.com on the imbue-staging.com apex; accounts.imbue.com + minds.imbue.com on imbue.com), and `minds env deploy` attaches both hosts as Modal custom domains on the connector and stamps AUTH_WEBSITE_DOMAIN, ACCOUNTS_BASE_URL, ACCOUNTS_COOKIE_DOMAIN, and SHARE_CHROME_ORIGIN from the block (winning over Vault). Tiers without the block (dev/ci) are unchanged.

Hardened `minds env deploy` secret handling after the staging rehearsal caught a silent failure (a lingering `vault kv delete` tombstone made the whole `sharing` directory read fail, and the deploy shipped a placeholder secret while exiting 0):

- The Vault reader now skips soft-deleted leaves (listed but dataless) with a warning instead of failing the entire service read.

- On staging/production, every service declared in `[secrets].services` is required: an unreadable Vault entry or one missing template-declared keys (per `.minds/template/<service>.sh`; empty values still allowed) aborts the deploy before anything is pushed. Dev/ci envs keep the bootstrap placeholder path, now logged at error level.

- The deploy log states how many values each pushed Modal Secret carries, so a gutted secret is visible at a glance.

- vault-setup.md and workspace-stop-start.md updated to match (the previously documented template validation did not actually exist).
