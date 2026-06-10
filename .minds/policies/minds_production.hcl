# Vault ACL policy: `minds_production`
#
# Grants CRUDL on every Vault entry the production minds tier reads or
# writes. Bound to the `minds_production` OIDC role, whose
# `bound_claims` allowlist is the source of truth for who can assume
# the policy.
#
# Push to Vault with:
#
#     vault policy write minds_production .minds/policies/minds_production.hcl
#
# A parallel `minds_staging` policy / role pair exists for the staging
# tier. Each tier has its own policy + role so an operator session is
# scoped to a single tier at a time -- picking the wrong `role=...` at
# login is the protection against "I meant staging".

path "secrets/data/minds/production/*"     { capabilities = ["create", "read", "update", "delete", "list", "patch"] }
path "secrets/metadata/minds/production/*" { capabilities = ["create", "read", "update", "delete", "list", "patch"] }
