Declared the AWS provider's `allowed_ssh_cidrs` as a replace-by-default field so a
developer's `settings.local.toml` can tighten it to their own IP without tripping the
settings-narrowing guard.

The default is now a non-empty `["0.0.0.0/0"]`, so a higher-precedence config layer that
set `allowed_ssh_cidrs` to a specific CIDR used to be rejected as "narrowing" (silently
dropping `0.0.0.0/0`), which broke every `mngr` command for anyone who tightened the value
-- exactly the security-conscious case. `allowed_ssh_cidrs` is now typed `ScalarStrTuple`,
a tuple that the narrowing guard treats as a single scalar value (combining CIDRs across
config layers is never the intent), so a local override cleanly replaces the default.
