# Template for the `supertokens-<env>` Modal secret.
#
# When adding or removing a variable here, mirror the change in every per-env
# file (e.g. .minds/production/supertokens.sh). `scripts/push_modal_secrets.py`
# treats this file as the canonical list of expected keys and errors out if
# the target env file is missing any of them.
#
# Fill in values in a per-env copy, not here. Empty values are skipped on push
# (an empty `export KEY=` line declares the key but leaves it unset on Modal).

# SuperTokens core connection URI (required).
export SUPERTOKENS_CONNECTION_URI=

# SuperTokens core API key (required in most deployments).
export SUPERTOKENS_API_KEY=

# Public base URL embedded in password-reset and email-verification links.
# Must match the URL Modal assigns to the deployed function; if unset, the
# app derives `https://{workspace}--cloudflare-forwarding-<env>-fastapi-app.modal.run`
# as a fallback, which is only correct for the default Modal workspace.
# Same value as the minds client's CLOUDFLARE_FORWARDING_URL.
export AUTH_WEBSITE_DOMAIN=

# Optional: Google OAuth provider overrides. Leave blank to use the
# providers configured on the SuperTokens core itself.
export GOOGLE_CLIENT_ID=
export GOOGLE_CLIENT_SECRET=

# Optional: GitHub OAuth provider overrides. Leave blank to use the
# providers configured on the SuperTokens core itself.
export GITHUB_CLIENT_ID=
export GITHUB_CLIENT_SECRET=
