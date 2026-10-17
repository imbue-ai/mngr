- The start flow's account step now leads with "Sign in" (the emphasized button) and keeps "Create an account" as the quieter option, with copy that points at the account the app was downloaded with (`specs/minds-waitlist-signup-codes/spec.md`).

- Every tier's `deploy.toml` gains the zero-quota `[plans.guest]` block, `max_shared_workspaces` on every plan, and a `[waitlist] is_enabled` switch (true on production and staging, false on dev and ci) that `minds-admin env deploy` stamps into the connector as `MINDS_WAITLIST_ENABLED`.
