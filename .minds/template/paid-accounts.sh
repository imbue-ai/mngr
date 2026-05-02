# Template for the `paid-accounts-<env>` Modal secret.
#
# Kept as its own Modal secret (separate from supertokens-<env>) so the
# email allowlist can be rotated without touching SuperTokens / OAuth
# credentials, and so toggling who has paid-feature access doesn't require
# re-deploying any other config.
#
# When adding or removing a variable here, mirror the change in every per-env
# file (e.g. .minds/production/paid-accounts.sh). `scripts/push_modal_secrets.py`
# treats this file as the canonical list of expected keys and errors out if
# the target env file is missing any of them.
#
# Fill in values in a per-env copy, not here. Empty values are skipped on push
# (an empty `export KEY=` line declares the key but leaves it unset on Modal).

# Comma-separated list of email-domain suffixes (case-insensitive) whose
# accounts are allowed to use "paid" remote-service-connector features:
# leasing pool hosts (`/hosts/*`) and creating/managing LiteLLM virtual keys
# (`/keys/*`). Non-matching accounts get HTTP 403 from those routes. Cloudflare
# forwarding (`/tunnels/*`) is intentionally NOT gated by this -- any email-
# verified account can still create tunnels and forward services. Leaving
# this unset (or empty) disables paid features entirely; every match is by
# `email.lower().endswith(suffix.lower())` so include the leading `@` if you
# want to require an exact domain (e.g. `@imbue.com,@example.org,bob@gmail.com`).
export PAID_ACCOUNT_SUFFIXES=
