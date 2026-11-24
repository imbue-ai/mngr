# Start flow

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

The *start flow* is the first-run sign-in page at "/start" that an installation which has never been taken past it lands on (when it does is specified in `home-page/`).
It offers one way in through an Imbue account and one way without; it ends when a workspace create goes out, or when the user leaves for the home page having signed in to an account that already has workspaces.

## Terms

The *landed account* is the account the browser sign-in started from the page reports, or, when it reports none, an account that was not signed in when the sign-in started and is signed in once it finishes.

The *reported workspace count* is the number of workspaces the landed account has, as the browser sign-in itself reports it when the account lands.
A sign-in can land without one.

The *record sync* is the app's first fetch of the landed account's workspaces after it lands, which the page waits on only when there is no reported workspace count.

A *first cloud workspace* is the workspace create the page sends itself under the landed account, on Imbue Cloud, when the landed account has no workspaces.

*Stranded* means no sequence of actions available to the user -- pressing what the page offers, closing dialogs, signing in and verifying email in the browser -- leads to a workspace create.

## Out of scope

- Which screen the app shows at launch and when "/" hands over to the start flow -- specified in `home-page/` and by the Electron shell.
- The create form's own contents.
- The creation page the flow hands over to once a create goes out, including the welcome a first cloud workspace is shown.
- The hosted account pages the sign-in opens in the browser, which are where an unverified email is verified.
