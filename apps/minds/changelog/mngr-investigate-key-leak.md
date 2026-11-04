The ToDesktop build upload excludes `.env` files from `apps/minds`, so a developer's local credentials can never ride into a packaged app.

The Vault setup doc's `secrets/mngr/ci` inventory lists the CI LiteLLM proxy leaves (`LITELLM_PROXY_URL`, `LITELLM_MASTER_KEY`) that TMR and the flake sweep now mint their agents' keys from, and no longer names those jobs as consumers of the shared Anthropic key.
