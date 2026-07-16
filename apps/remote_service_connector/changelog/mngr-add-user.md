Paid users no longer get stuck behind email verification:

- A paid user who signs up with email/password is now auto-verified at signup (no verification email, and their first session is already verified) instead of being asked to verify their email.

- Adding an email to the paid list (`mngr imbue_cloud admin paid email add` / `minds paid add`) now also marks any pre-existing account for that email as verified, so a user who signed up before being made paid isn't left locked out. This is best-effort: it never fails the paid-list write.

- The admin auth guard now determines email-verification from a live SuperTokens lookup rather than trusting the (possibly stale) claim baked into the access token. Verification now takes effect on the user's very next request instead of only after their token refreshes.
