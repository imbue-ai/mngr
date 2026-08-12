All sign-up/sign-in moves to the web: accounts are created and signed in on the connector's hosted browser page, not in the app.

- Every auth entry point (the welcome splash, Add account, Sign in again, the create flow's Imbue Cloud preset, the sharing sign-in nudge) now launches `mngr imbue_cloud auth login` -- the hosted page in the system browser -- and the new Mithril waiting modal narrates the flow with a copy-the-sign-in-link fallback and error handling. Sign-in lands back in the app automatically.

- The legacy JinjaX auth surface is deleted: the in-app sign-up/sign-in pages and modal, `static/auth.js`, the check-your-email verification flow (email verification is now non-blocking; only opening a workspace shared with you and the ally plan require a verified email), and the Electron shell's auth-page special-casing. The retired `/auth/login` / `/auth/signup` URLs redirect into the SPA, which starts the browser flow.

- Signing out of the app now revokes only this device's session; the account's browser session and other devices stay signed in ("Sign out of all devices" lives on the hosted account page).

- `minds env deploy` now builds the connector's accounts frontend bundle (`apps/remote_service_connector/frontend`, pnpm + Vite) before `modal deploy`, and fails fast with a clear error when pnpm is missing.

- New deployment tests cover the hosted surface: an HTTP-level walk of the browser-login contract (cookie signup, confirmed authorize, PKCE code exchange, device-scoped sign-out) and a Playwright pass over the real pages.

- Switching plans with an unverified email now shows a contextual prompt on the Accounts page: the app auto-sends the verification email on the connector's structured `email_not_verified` refusal and offers a resend button (new `POST /accounts/<user_id>/resend-verification` route).
