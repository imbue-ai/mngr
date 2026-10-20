Document the connector's new `ACCOUNT_EXISTS_WITH_OTHER_METHOD` auth status in the desktop client's `AuthResult` model.

The connector now refuses signups that would create a second account for an email already registered under a different login method (password vs OAuth). The desktop client's sign-in/sign-up forms already surface the server's message for unrecognized statuses, so no behavior change was needed beyond the doc sync.
