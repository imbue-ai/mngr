# Vault ACL policy: `minds_staging`
#
# Grants CRUDL on every Vault entry the staging minds tier reads or
# writes. Bound to the `minds_staging` OIDC role, whose `bound_claims`
# allowlist is the source of truth for who can assume the policy.
#
# Push to Vault with:
#
#     vault policy write minds_staging .minds/policies/minds_staging.hcl
#
# A parallel `minds_production` policy / role pair exists for the
# production tier. Each tier has its own policy + role so an operator
# session is scoped to a single tier at a time -- picking the wrong
# `role=...` at login is the protection against "I meant staging".

path "secrets/data/minds/staging/*"     { capabilities = ["create", "read", "update", "delete", "list", "patch"] }
path "secrets/metadata/minds/staging/*" { capabilities = ["create", "read", "update", "delete", "list", "patch"] }
