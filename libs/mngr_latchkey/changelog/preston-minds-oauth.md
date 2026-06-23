Bump the pinned latchkey CLI version installed on remote VPS environments (the secondary gateway) to 2.18.0.

Add thin `Latchkey` primitives for the Minds Google OAuth flow: `auth_list` (which services have a registered client), `auth_prepare` (register an OAuth client id/secret for a service), and `auth_browser_login` (a bare `auth browser` sign-in with no self-setup fallback). Also add the Minds Google OAuth client constants and the `google-` service-name prefix used to gate the new flow.
