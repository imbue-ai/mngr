The desktop app now genuinely waits for email verification after creating an email/password account. Previously the "Check your email" page's poll endpoint hardcoded "verified", so it flashed "Email verified. Redirecting..." seconds after signup regardless of whether the link was clicked, and the unverified account was fully activated locally (default account, provider registration) while every Imbue Cloud call failed with a cryptic auth error.

- An account no longer counts as signed in until its email is verified: signup/signin of an unverified account defers all local signin bookkeeping, and the check-email page polls the real verification status (via the new `mngr imbue_cloud auth is-verified` plugin command), completing the signin only once the link is clicked.

- The "Resend verification email" button actually sends an email now (it previously reported "Sent" after a local no-op) and surfaces the server-side cooldown when one was sent moments ago. The forgot-password flow likewise sends a real reset email, and the legacy in-app reset link redirect points at the connector's reset page instead of a broken empty URL.

- The verification flow is pinned to the explicit account email instead of a "latest account" heuristic that actually picked the alphabetically-last signed-in account; the auth status API and account settings page now prefer the plugin's active account.

- The sign-in modal's panel-restore detour no longer skips the check-email page, auto-verified (paid-list) signups skip the verification detour entirely, and an OAuth sign-in that returns an unverified email fails with an actionable message instead of activating a broken account.

- Post-sign-in navigation is smoother: every successful sign-in routes through /post-login so the consent gate and destination resolve in one hop (no more welcome-screen flash followed by a late consent screen), the sign-in modal stays up with a "Signing you in..." status until the destination page has actually committed, and the check-email page's copy was tightened ("Click the link sent to <address>", "Check your spam folder (if necessary)").
