# CLI signup restricted to dev/CI tiers

- `mngr imbue_cloud auth signup` now works only against dev/CI tiers: production and staging connectors refuse account creation through the JSON API (status `SIGNUP_DISABLED`) so every new account goes through the browser flow (`mngr imbue_cloud auth login`), which carries the bot-mitigation gate. Signing in to an existing account is unchanged on every tier.

- Creating a remote workspace on Imbue Cloud now requires a verified email: the connector refuses `mngr create` against an unverified account with a message directing the user to the verification email it just sent (check the inbox and spam folder), rate-limited server-side.
