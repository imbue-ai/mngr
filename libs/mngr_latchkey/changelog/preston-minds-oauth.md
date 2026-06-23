Bump the pinned latchkey CLI version installed on remote VPS environments (the secondary gateway) to 2.18.0.

Add thin `Latchkey` primitives for the Minds Google OAuth flow: `auth_list` (which services have a registered client), `auth_prepare` (register an OAuth client id/secret for a service), and `auth_browser_login` (a bare `auth browser` sign-in with no self-setup fallback). Also add the Minds Google OAuth client constants and the `google-` service-name prefix used to gate the new flow.

Raise the minimum supported latchkey CLI version (`LATCHKEY_MIN_VERSION`) to 2.18.0, the first release with the `auth prepare` subcommand the new flow depends on. The package now refuses to operate against gateways older than 2.18.0.
