Bump the pinned latchkey CLI version installed on remote VPS environments (the secondary gateway) to 2.19.1.

Add thin `Latchkey` primitives for the Minds Google OAuth flow: `auth_list` (which services have a registered client), `auth_prepare` (register an OAuth client id/secret for a service), and `auth_browser_login` (a bare `auth browser` sign-in with no self-setup fallback). Also add an explicit set of Minds-OAuth Google services (`MINDS_GOOGLE_OAUTH_SERVICES`) used to gate the new flow -- `google-directions` is deliberately excluded because it authenticates with an API key, not OAuth, so it must not go through the Minds OAuth client. Plus the Minds Google OAuth client id/secret as hardcoded constants (an installed/desktop-app OAuth client, where the "secret" is not truly confidential since it ships inside the distributed client).

Raise the minimum supported latchkey CLI version (`LATCHKEY_MIN_VERSION`) to 2.19.1, kept in lockstep with the version installed/bundled. The package now refuses to operate against gateways older than 2.19.1 (2.18.0 introduced the `auth prepare` subcommand the new flow depends on).

Add a `Latchkey.auth_clear` primitive (`latchkey auth clear -y <service>`) used by the Minds Google OAuth fallback to discard a failed client registration so the self-setup flow can start clean.
