# Account windows

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

Every window of the desktop client is for exactly one account: the *window's account*.
An account here is either an Imbue account signed in on this installation (identified by its user id) or the device itself, identified as `local`, which is the not-signed-in state and owns the workspaces associated with no account.
A workspace's account is the account its workspace record associates it with, so a window showing a workspace is for that workspace's account.

The window's account is in the address of every hub page, "/account/<account_id>/..." (the home page is specified in `home-page/`), and a page that floats over whatever the window shows (e.g. (wlog) the settings or the notification feed) carries it as the `account` query parameter so a reload keeps it.
The *account switcher* is the control in the titlebar that names the window's account and moves the window to another account.
The *recently shown* order is the installation's record of which accounts were last shown in a window and, per account, which of its workspaces were last opened in one; it is kept per installation, not per account.

## Out of scope

- The contents of a home page, and where "/" lands -- specified in `home-page/`.
- The start flow, which runs in a window for the device -- specified in `start-flow/`.
- The hosted web chrome, which is one account per browser and has no switcher.
