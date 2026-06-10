# Vault ACL policy: `employee`
#
# Default policy bound by the `employee` OIDC role (the role
# `vault login -method=oidc` selects when no `role=` is passed).
# Every imbue employee who logs in via Google OIDC lands here.
#
# This file is the source of truth for the policy text. Push to Vault
# with:
#
#     vault policy write employee .minds/policies/employee.hcl
#
# The `employee_unrestricted` / `employee_all_secrets` overlay roles
# bypass the denies below for the per-user allowlists configured on
# each OIDC role (`vault read auth/oidc/role/<name>`).

# `restricted/*` is the long-standing carve-out for secrets that are
# never appropriate for the broad employee population (PII, prod
# credentials, etc.).
path "secrets/data/restricted/*"     { capabilities = ["deny"] }
path "secrets/metadata/restricted/*" { capabilities = ["deny"] }

# minds staging + production tiers are restricted-by-default. The
# dev tier intentionally stays open so every employee can stand up
# their own dev env via `minds env deploy`. Push access to staging /
# production goes through dedicated OIDC roles + per-user allowlists
# (see `apps/minds/docs/staging-bringup.md`).
path "secrets/data/minds/staging/*"        { capabilities = ["deny"] }
path "secrets/metadata/minds/staging/*"    { capabilities = ["deny"] }
path "secrets/data/minds/production/*"     { capabilities = ["deny"] }
path "secrets/metadata/minds/production/*" { capabilities = ["deny"] }

path "secrets/*" { capabilities = ["read", "create", "update", "delete", "list", "patch"] }
