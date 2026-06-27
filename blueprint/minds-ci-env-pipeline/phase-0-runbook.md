# Phase 0 runbook — Vault + GitHub setup for the minds CI env pipeline

This is the one-time setup that the rest of the pipeline (Phase 1+) depends on.
Some of it is already done; the rest needs privileges only a human/admin has
(`terraform apply` against the HCP Vault cluster, and creating a GitHub
Environment on `imbue-ai/mngr`).

## Already done (by the agent)

- **Terraform changes committed + draft PR opened** on `imbue-ai/vault`:
  - Branch `mngr/minds-ci-envs`, draft PR: https://github.com/imbue-ai/vault/pull/1
  - Adds OIDC role `minds_ci_env_gh` (gated on the `minds-ci-env` GitHub
    Environment), defined **inline** (its own `vault_jwt_auth_backend_role` +
    `vault_policy`, NOT via the shared `jwt_role_and_policy` module); reads the
    `minds/ci/*` service secrets `minds env deploy`/`destroy` need, and gets
    read+write+delete on `minds/ci/runs/*`; token TTL 30m.
  - Expands `minds_ci_test_gh` (still module-based) to also read
    `minds/ci/paid-accounts/*` and `minds/ci/runs/*`.
  - The shared `jwt_role_and_policy` module is left **untouched** (an earlier
    attempt to extend it re-rendered every consumer's policy string and caused
    no-op churn across the sculptor/vault-repo roles; the role is inline to
    avoid that). `terraform fmt`/`validate` pass; `terraform plan` against the
    committed state is **2 to add, 1 to change, 0 to destroy** with no other
    roles touched.
- **Vault values written** (KV v2, namespace `admin`, mount `secrets/`):
  - `secrets/minds/ci/paid-accounts/CI_TEST_USER_EMAIL = minds-ci-test@imbue.com`
  - `secrets/minds/ci/paid-accounts/CI_TEST_USER_PASSWORD = <generated strong value>`
  - Written via `vault kv patch`, so the pre-existing `PAID_ACCOUNT_SUFFIXES`
    key was preserved. The `@imbue.com` email is paid out of the box because the
    `ci` tier's `deploy.toml [paid]` already seeds `paid_domains = ["imbue.com"]`.

## What YOU need to do to deploy

### 1. Review + merge the Vault PR

Review https://github.com/imbue-ai/vault/pull/1, mark it ready, and merge (or
apply directly from the branch — see step 2).

### 2. `terraform apply` (needs Vault admin / HCP creds)

```bash
# Authenticate to Vault first (OIDC), as documented in apps/minds/docs/vault-setup.md:
export VAULT_ADDR="https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200"
export VAULT_NAMESPACE="admin"
vault login -method=oidc   # or however the vault repo expects auth for terraform

# From a checkout of imbue-ai/vault on the merged branch:
cd terraform
terraform init
terraform plan      # expect: + module.minds_ci_env_gh (role+policy), ~ module.minds_ci_test_gh (policy)
terraform apply
```

Expected plan: `Plan: 2 to add, 1 to change, 0 to destroy` — create the
`minds_ci_env_gh` role + policy, update the `minds_ci_test_gh` policy. No other
roles change (if you see sculptor/vault-repo policies churning, the shared
module was modified — it should not be).

### 3. Create the `minds-ci-env` GitHub Environment on `imbue-ai/mngr`

The Vault role's `environment` claim is only a real gate once this exists.
Mirror the existing `minds-ci-test` environment: all same-repo branches, **no
required reviewers** (so the jobs run unattended on every push).

```bash
# Create the environment (no protection rules = all branches, no reviewers):
gh api -X PUT repos/imbue-ai/mngr/environments/minds-ci-env

# Confirm minds-ci-test already exists (it should):
gh api repos/imbue-ai/mngr/environments/minds-ci-test --jq .name
```

### 4. (Optional) Rotate / re-set the CI test-user credentials

Already written, but if you want to set your own values:

```bash
export VAULT_ADDR="https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200"
export VAULT_NAMESPACE="admin"
vault kv patch secrets/minds/ci/paid-accounts \
  CI_TEST_USER_EMAIL="minds-ci-test@imbue.com" \
  CI_TEST_USER_PASSWORD="<a strong password: upper+lower+digit+special>"
```

(Use `kv patch`, not `kv put`, to preserve `PAID_ACCOUNT_SUFFIXES`. The email
must be `@imbue.com` — or another domain seeded into `paid_domains` — so the
account can mint LiteLLM keys.)

## Notes

- **No `minds/ci/runs/*` setup needed**: those per-run dynamic-secret paths are
  created/read/deleted at runtime by the CI jobs (and the local orchestrator).
  The role policies above grant the necessary capabilities.
- **Modal auth is not in Vault**: the new jobs reuse the existing
  `MODAL_TOKEN_ID` (GH var) + `MODAL_TOKEN_SECRET` (GH secret), already present
  for `test-minds-snapshot`. Phase 1 writes a throwaway `~/.modal.toml`
  `minds-dev` profile from those at job start.
- **Security note**: `minds_ci_env_gh` is the first CI role with **write**
  access to Vault (scoped to `secrets/minds/ci/runs/*` only) and a longer 30m
  token TTL. This is required for the build job to hand per-env secrets to the
  separate test job. Confirm you are comfortable with this when applying.
