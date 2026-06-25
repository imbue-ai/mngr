# Staging tier bring-up checklist

End-to-end checklist for standing up the `staging` minds tier from
scratch. Assumes the operator has previously deployed a dev env (so
`vault`, `modal`, `uv`, `gh`, `psql`, and `mngr` are already wired up
on the machine) but has never touched staging or production cloud
state.

Companion reading:
- [environments.md](./environments.md) -- per-tier model + the deploy CLI
- [vault-setup.md](./vault-setup.md) -- Vault paths each tier expects
- [host-pool-setup.md](./host-pool-setup.md) -- pool hosts for leased mode

Production bring-up follows the same shape; substitute `staging` ->
`production`, `minds-staging` -> `minds-production`, and
`--yes-i-mean-staging` -> `--yes-i-mean-production` throughout.

The desktop installer (ToDesktop build) is intentionally out of scope:
the goal here is "I can `minds run` against `staging` locally and
create a workspace through the Electron dev shell".

---

## 0. One-time prerequisites on the operator's machine

- [ ] `vault` CLI installed; logged in against the imbue HCP cluster
  (`VAULT_ADDR=...`, `VAULT_NAMESPACE=admin`) with the right OIDC
  role for the tier you're bringing up:
  ```bash
  # For staging work:
  vault login -method=oidc role=minds_staging
  # For production work:
  vault login -method=oidc role=minds_production
  ```
  The default `vault login -method=oidc` (no `role=`) lands on the
  `employee` role, which is denied on `secrets/minds/{staging,production}/*`.
  See [Vault access control](#vault-access-control-recap) below for the
  policy layout and how to add a teammate to the per-tier allowlist.
  Cluster URL is in `vault-setup.md`.
- [ ] `modal` CLI installed.
- [ ] `psql` available locally (for the optional sanity check in step 5).
- [ ] Logged in to `gh` (for FCT-template clone during the pool bake).
- [ ] Repo checkout with this branch (`mngr/minds-staging`) on disk; all
  subsequent `uv run` commands run from the monorepo root.

---

## 1. Stand up the per-tier cloud accounts

Every step here is "click around in a vendor console" -- minds does
not touch them. Capture the values listed under each bullet; they
become Vault entries in step 4.

- [ ] **Modal workspace `minds-staging`.** Create the workspace at
  <https://modal.com/settings/workspaces>. The name must match
  `modal_workspace` in `apps/minds/imbue/minds/config/envs/staging/deploy.toml`
  exactly (currently `minds-staging`). Then on the operator's machine:
  ```bash
  modal token set --profile minds-staging
  ```
  Verify a `[minds-staging]` block landed in `~/.modal.toml`. The
  `MODAL_PROFILE` export in `minds env activate --deploy staging` (see
  step 6) pins every subsequent `modal` shellout to this profile -- the
  account you're logged into via `active = true` is irrelevant. The
  presence of the `[minds-staging]` block is only checked when you pass
  `--deploy` (which pre-validates `~/.modal.toml` and fails fast with a
  `modal token set --profile minds-staging` hint if the block is
  missing). Plain `minds env activate staging` -- use-only activation --
  does not need the block and never reads `~/.modal.toml`.

- [ ] **Neon project for staging.** Create a single project under your
  Neon org (any name; the staging tier uses `creates_resources=false`
  so minds never creates or names it). Inside the project, create two
  databases: `host_pool` and `litellm_cost`. Capture:
  - Pooled `DATABASE_URL` for `host_pool` (becomes `neon/DATABASE_URL`)
  - Pooled `DATABASE_URL` for `litellm_cost` (becomes
    `litellm/DATABASE_URL`)
  - Direct (non-pooled) DSN for `host_pool` (for the optional manual
    sanity check; `minds env deploy` also runs migrations through
    the pooled URL, but the direct one is handy for `psql`)
  - `NEON_PROJECT_ID` (visible in the project URL or settings)
  - A `NEON_API_TOKEN` with branch-create + restore scope on the
    project (org-create scope is NOT required for staging -- that's
    a dev-tier-only need). Generate at Account Settings -> API Keys.

- [ ] **SuperTokens core for staging.** Either reuse the existing
  SuperTokens core under a new app id (`staging`) or stand up a
  separate core. Capture the core's connection URL (without any
  `/appid-` suffix) and an admin API key. The deploy will append
  `/appid-staging` automatically when computing
  `SUPERTOKENS_CONNECTION_URI`.

- [ ] **Cloudflare account / zone.** Create or pick a zone for staging
  (must be distinct from production). Capture:
  - Account ID
  - Zone ID
  - Base domain (e.g. `staging.minds.example.com`)
  - API token with **Tunnel Write** and **DNS Write** scoped to the
    zone (`https://dash.cloudflare.com/profile/api-tokens`).
  - Optional: comma-separated list of identity-provider UUIDs to
    allowlist on Cloudflare Access apps (`CLOUDFLARE_ALLOWED_IDPS`).

- [ ] **Google OAuth client** for staging. Create a new client at
  <https://console.cloud.google.com/apis/credentials>. Redirect URI:
  `https://minds-staging--rsc-staging-api.modal.run/auth/callback/google`
  (matches the committed `staging/client.toml`). Capture the client
  id and secret.

- [ ] **GitHub OAuth client** for staging. Create at
  <https://github.com/settings/developers>. Callback URL:
  `https://minds-staging--rsc-staging-api.modal.run/auth/callback/github`.
  Capture the client id and secret.

- [ ] **Bare-metal box supplier credentials** for staging (currently
  OVH; also used to tear down legacy OVH VPS hosts). Generate the
  AK/AS/CK trio at <https://api.us.ovhcloud.com/createApp> for the
  `ovh-us` endpoint with the scopes the pool flows need (see
  `host-pool-setup.md`). These can be skipped on the first deploy (the
  entry is optional -- deploy logs a warning and proceeds), but pool
  host creation and `minds env destroy` cleanup both require it.

- [ ] **Anthropic API key** for the staging LiteLLM proxy backend. Either
  mint a dedicated key under a staging-tagged Anthropic account or
  reuse an existing one; just don't share it with production.

- [ ] **Pool-management SSH keypair.**
  ```bash
  mkdir -p .minds/staging/pool_management_key
  ssh-keygen -t ed25519 -f .minds/staging/pool_management_key/id_ed25519 -N ""
  ```
  The directory is gitignored (it sits inside `.minds/` which is
  already excluded). The private key goes into Vault in step 4; the
  public key file is referenced by step 7.

- [ ] **LiteLLM master key.** Any high-entropy string:
  ```bash
  openssl rand -hex 32
  ```
  Captured for the `litellm/LITELLM_MASTER_KEY` Vault leaf. Treat as
  a secret; this key has admin authority over the LiteLLM proxy.

---

## 2. Fill in the committed staging `deploy.toml`

`apps/minds/imbue/minds/config/envs/staging/deploy.toml` ships with
one known placeholder -- update it in this branch and commit:

- [ ] `cloudflare_domain = "CHANGE_ME.example.com"` -> the real staging
  zone (e.g. `"staging.minds.example.com"`). Read by the connector at
  runtime and used by tunnel creation.

The other fields (`modal_workspace`, `vault_path_prefix`, the
`[secrets]` services list, `[lifecycle]`, `[min_containers]`) are
already correct for staging. Do not edit them.

OAuth provider client ids + secrets live exclusively in
`secrets/minds/<tier>/supertokens` (see step 4); there's no
corresponding `deploy.toml` field to mirror them.

`apps/minds/imbue/minds/config/envs/staging/client.toml` already
contains the deterministic Modal URLs
(`https://minds-staging--rsc-staging-api.modal.run` and
`https://minds-staging--llm-staging-proxy.modal.run`) -- leave it
alone unless step 6's deploy log reports different URLs (which would
mean Modal truncated the hostname under DNS's 63-char limit; not
expected with these short names).

---

## 3. Apply pool-hosts schema migrations to the staging `host_pool` DB

`minds env deploy` runs the schema migrations automatically as part
of the deploy (against the pooled `DATABASE_URL` from the `neon`
Vault entry once step 4 is done). No manual `psql` pass is required
on first bring-up.

- [ ] (Optional sanity check, before step 4 if you want to verify
  the DB is reachable from your laptop:)
  ```bash
  psql "$NEON_DB_DIRECT_HOST_POOL" -c "SELECT 1"
  ```

---

## 4. Push every Vault entry the staging deploy reads

For each service below: copy the template, fill in values, push,
shred. The push script validates that every key declared by the
template is present in the filled file (empty values are fine --
they're skipped on Modal push).

```bash
cp .minds/template/<service>.sh /tmp/staging-<service>.sh
$EDITOR /tmp/staging-<service>.sh
uv run scripts/push_vault_from_file.py staging <service> /tmp/staging-<service>.sh
shred -u /tmp/staging-<service>.sh
```

Modal-pushed entries (consumed by the deployed apps at runtime):

- [ ] **`secrets/minds/staging/cloudflare`** -- `CLOUDFLARE_API_TOKEN`,
  `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ZONE_ID`,
  `CLOUDFLARE_DOMAIN` (same as `deploy.toml`'s `cloudflare_domain`),
  optionally `CLOUDFLARE_ALLOWED_IDPS`.

- [ ] **`secrets/minds/staging/litellm`** -- `ANTHROPIC_API_KEY`,
  `DATABASE_URL` (pooled DSN for the `litellm_cost` DB),
  `LITELLM_MASTER_KEY`. The proxy URL the connector hands back to
  agents is deploy-time-derived (`<workspace>--llm-<tier>-proxy.modal.run`)
  and lives in the `litellm-connector-<tier>` Modal Secret -- no
  matching Vault key, no entry to fill in here.

- [ ] **`secrets/minds/staging/neon`** -- `DATABASE_URL` (pooled DSN
  for the `host_pool` DB).

- [ ] **`secrets/minds/staging/pool-ssh`** -- `POOL_SSH_PRIVATE_KEY`.
  Push via the `@<path>` syntax so the key file never leaves your
  laptop:
  ```bash
  vault kv put -mount=secrets minds/staging/pool-ssh/POOL_SSH_PRIVATE_KEY \
      value=@.minds/staging/pool_management_key/id_ed25519
  ```
  (Or fill the template file with the multi-line value if you'd
  rather route through `push_vault_from_file.py`.)

- [ ] **`secrets/minds/staging/supertokens`** --
  `SUPERTOKENS_CONNECTION_URI` (core base URL, no `/appid-` suffix --
  the deploy appends `/appid-staging`), `SUPERTOKENS_API_KEY`,
  `AUTH_WEBSITE_DOMAIN`
  (`https://minds-staging--rsc-staging-api.modal.run`),
  `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GITHUB_CLIENT_ID`,
  `GITHUB_CLIENT_SECRET`, `MINDS_PAID_ADMIN_KEY` (the fixed key for
  the `/paid/*` admin API; leave empty to disable it),
  `MINDS_PAID_LIST_CACHE_TTL_SECONDS` (optional; default 60).

Operator-only entries (read by `minds env deploy` on the laptop;
never pushed to Modal):

- [ ] **`secrets/minds/staging/neon-admin`** -- `NEON_API_TOKEN`,
  `NEON_PROJECT_ID`. Leave `NEON_ORG_ID` empty (only the dev tier
  needs it).

- [ ] **`secrets/minds/staging/ovh`** -- `OVH_APPLICATION_KEY`,
  `OVH_APPLICATION_SECRET`, `OVH_CONSUMER_KEY` (the bare-metal box
  supplier credentials, currently OVH; also used to tear down legacy
  OVH VPS hosts). Skippable on first deploy (deploy warns and
  continues), but required before `mngr imbue_cloud admin pool create`
  or `minds env destroy` can succeed.

After every push:

- [ ] Spot-check the keys with `vault kv list -mount=secrets minds/staging/<service>`.

---

## 5. Verify Modal CLI talks to `minds-staging`

```bash
eval "$(uv run minds env activate --deploy staging)"
echo "$MODAL_PROFILE"   # expect: minds-staging
modal environment list  # should NOT error; no envs needed yet for SHARED tier
```

If `modal` complains about missing auth, re-run `modal token set
--profile minds-staging`. The `MODAL_PROFILE` export the `--deploy`
activation emits is what pins every `modal` shellout below to this
workspace. Without `--deploy`, `MODAL_PROFILE` is not exported (and
plain `activate` actively unsets it) -- that mode is for *using* the
deployed tier, not deploying it.

---

## 6. First-time tier deploy

```bash
eval "$(uv run minds env activate --deploy staging)"
uv run minds env deploy --yes-i-mean-staging
```

This is the safety-gated command from `environments.md`. What it does:

1. Snapshots the Neon project's default branch (so recover can roll
   back).
2. Runs the pool-hosts schema migrations against the `host_pool` DB
   (via `secrets/minds/staging/neon/DATABASE_URL`).
3. Mints a tier generation id and stores it at
   `secrets/minds/staging/generation` in Vault.
4. Pushes a Modal Secret per service in `[secrets].services`
   (`<service>-staging-<deploy-id>`), plus a code-driven
   `litellm-connector-staging-<deploy-id>` Secret.
5. Runs the LiteLLM Prisma schema migration against the
   `litellm_cost` DB.
6. `modal deploy` both apps into the `main` Modal env of the
   `minds-staging` workspace.
7. Polls both apps' `/health` endpoints for 200.
8. GCs old timestamped Secrets (keeps the latest 10 per service).

Watch the deploy logs. On the first run, expect:

- `WARNING: Vault read for ovh failed ...` if you skipped the
  bare-metal box supplier credentials entry. Safe to ignore.
- Per-app deploy lines ending with
  `https://minds-staging--rsc-staging-api.modal.run` and
  `https://minds-staging--llm-staging-proxy.modal.run`. The deploy
  asserts these match the committed `client.toml` -- if they
  differ, update the committed `staging/client.toml` and commit
  before retrying.

On failure, the CLI prints a 5-second countdown and then auto-runs
`minds env recover`. Hit Ctrl-C during the countdown if you'd rather
fix in place; otherwise let recover roll back to the pre-deploy
state.

- [ ] Deploy finished cleanly; both URLs printed match the committed
  `staging/client.toml`.

---

## 7. (Optional) bake one staging pool host

Only needed if you want the staging desktop client to use IMBUE_CLOUD
launch mode. DOCKER mode (`--template main --template docker`) works
without any pool hosts.

Pool hosts are baked as bare-metal **slices** (baking new OVH classic VPS
pool hosts is deprecated and no longer supported). You first need a
bare-metal box that is registered + prepped (`status=ready`) via the
`mngr imbue_cloud admin server` commands -- see
[host-pool-setup.md](./host-pool-setup.md) step 5. With staging activated,
bake onto a `ready` box via the canonical justfile recipe:

```bash
just bake-slice-prod US-WEST-OR v0.3.0 1 --server-id <bare-metal-server-id>
```

`just bake-slice-prod <region> <tag> [count] [extra flags]` wraps
`minds pool create --backend slice`, which derives the pool SSH key from
the tier's Vault entry and -- for staging/production -- reads the host_pool
DSN from `secrets/minds/staging/neon`. You do NOT export any of those by
hand. See [host-pool-setup.md](./host-pool-setup.md) step 5 for the full
breakdown.

`region` is the lease-region **label** stamped on each row (what the
connector region-matches at lease time, e.g. `US-WEST-OR` / `US-EAST-VA`) --
not the box's raw datacenter code. The baked version comes from the bake
source (`<tag>` here, e.g. `v0.3.0`), NOT from `--attributes`.

The bare-metal box itself is ordered ahead of time via `mngr imbue_cloud
admin server order` (using the supplier credentials). A common box-order
failure with the current OVH supplier is ``OVH API POST
/order/cart/.../checkout returned error: You do not have preferred payment
method`` -- the supplier account needs a default payment method (OVH manager
UI: Billing -> Payment methods -> add -> mark as default) before any box
order can go through.

- [ ] `just list-pool-hosts` shows the row.

---

## 8. Smoke-test the staging deploy locally

With staging still activated:

```bash
just minds-start
```

(Or `uv run minds run`; `just minds-start` is the workspace shortcut
in this repo.)

The terminal should print a `login_url`. Open it in a browser.

- [ ] Sign in with the Google OAuth client you wired up in step 1.
  Verify the redirect lands you back on the desktop client with a
  signed session cookie.
- [ ] Create a workspace from a template repo URL (e.g.
  `https://github.com/imbue-ai/forever-claude-template`). Watch the
  `/creating/<agent-id>` page; expect it to flip to `DONE` and
  redirect to the agent.
- [ ] The agent's dockview UI loads and the `web` service is
  reachable through `<agent-id>.localhost:<port>/service/web/`.
- [ ] Open the Share modal on a service and verify the global
  Cloudflare URL is generated and reachable (gated on the email you
  signed in with).

Tear-down (only if you want to start over -- staging destroy wipes
the SuperTokens users + the Neon DB schema, NOT the underlying
resources):

```bash
uv run minds env destroy --yes-i-mean-staging
```

---

## 9. Commit + open the staging-bringup PR

- [ ] `apps/minds/imbue/minds/config/envs/staging/deploy.toml`
  with the real Cloudflare domain + OAuth client ids.
- [ ] (If the deployed URLs disagreed with the committed values:)
  the updated `apps/minds/imbue/minds/config/envs/staging/client.toml`.
- [ ] An `apps/minds/changelog/<branch-name>.md` entry describing the
  staging tier bring-up (required by CI); plus a `dev/changelog/<branch-name>.md`
  entry if the PR also touches root-level files (scripts, CI, etc.).

The `.minds/staging/pool_management_key/` directory should NOT land
in the commit (it's covered by the existing `.minds/` gitignore, but
double-check `git status` before pushing).

---

## Vault access control recap

Three Vault ACL policies gate access to the minds tiers:

- **`employee`** -- bound by the default `employee` OIDC role.
  Every signed-in employee lands here. Denies
  `secrets/{data,metadata}/minds/{staging,production}/*`; allows the
  rest of `secrets/*`.
- **`minds_staging`** -- bound by the `minds_staging` OIDC role.
  Grants CRUDL on `secrets/{data,metadata}/minds/staging/*`.
- **`minds_production`** -- bound by the `minds_production` OIDC role.
  Grants CRUDL on `secrets/{data,metadata}/minds/production/*`.

The policy text is committed under `.minds/policies/`; push edits with
`vault policy write <name> .minds/policies/<name>.hcl`.

Each tier role's allowlist is a `bound_claims.email` list on the OIDC
role. Adding a teammate to staging:

```bash
VAULT_ADDR='...' VAULT_NAMESPACE='admin' vault write auth/oidc/role/minds_staging - <<'EOF'
{
  "user_claim": "email",
  "bound_audiences": ["848426076477-10ceo4ek7i9m1p3gm7vkb3gautms9ma2.apps.googleusercontent.com"],
  "oidc_scopes": ["email"],
  "bound_claims": {"email": ["josh@imbue.com", "alice@imbue.com"]},
  "bound_claims_type": "string",
  "allowed_redirect_uris": [
    "https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200/ui/vault/auth/oidc/oidc/callback",
    "http://localhost:8250/oidc/callback"
  ],
  "token_policies": ["minds_staging"],
  "token_ttl": "168h",
  "token_explicit_max_ttl": "168h"
}
EOF
```

(The CLI's `key=value` parsing can't represent the map-shaped
`bound_claims`; piping a JSON body via `- <<EOF` is the supported
workaround.)

Rotating an OIDC role's allowlist is non-destructive -- existing
sessions keep their tokens (TTL 168h) until they expire. To force
a re-auth, revoke matching tokens via
`vault list auth/token/accessors` + `vault token revoke -accessor=...`.

The four operators on `employee_unrestricted` / `employee_all_secrets`
retain full access regardless; those overlay roles are for break-glass
debugging, not routine deploys.
