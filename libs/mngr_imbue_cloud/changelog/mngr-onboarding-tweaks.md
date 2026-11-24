An account is now signed in only once its email is verified: no `mngr imbue_cloud auth` command saves a session for an unverified account.

- `auth login` no longer exchanges the code of a browser hand-off that says the email is unverified. The callback page shows the verification reminder, and the command fails with `ImbueCloudEmailNotVerifiedError` (`code: email_not_verified`, plus the email); a code exchange the connector refuses for the same reason fails the same way.

- `auth login`'s JSON output drops `is_email_verified` and gains `active_workspace_count`: how many active workspaces the hosted page said the account has, or null when it did not say.

- `auth signin` asks the connector whether the email is verified before saving anything. For an unverified account it revokes the session it was just given and fails with `ImbueCloudEmailNotVerifiedError`; if the check cannot be made it fails with the check's error. Either way nothing is saved.

- `auth signup` makes the same check and now reports `is_signed_in`. For the usual new account, whose email is not verified yet, it exits 0 with `"is_signed_in": false` and a message saying to click the emailed link and then run `auth signin`; no session is saved. Scripts that relied on `auth signup` leaving a signed-in session need an account that is already verified.
