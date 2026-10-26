# Home page

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

The home page is where a browser lands once a session is authenticated on the *browser authorization component* (defined in `browser-authorization/`).
Every window is for one account (the *window's account*, specified in `account-windows/`), and an account's home page is "/account/<account_id>/"; "/" redirects to the home page of the account most recently shown in any window, which is the device's, `local`, until an account has been shown.
This folder specifies what an already-*authenticated* user sees on a home page, and where "/post-login" sends them once they sign in.

## Boundary with browser-authorization

Authentication is a precondition here, not a subject: every scenario begins with an already-authenticated user.
What a browser with *no* session sees at "/" is the authorization boundary, specified in `browser-authorization/` (the `@unauthenticated-home` illustration of the no-user-data-without-a-session invariant).
Deciding where a just-signed-in user lands is specified here (`post-login-destination.feature`), but its two access-control facts -- the session gate on "/post-login", and the confinement of a caller-supplied return destination to the origin -- are specified in `browser-authorization/` and referenced, not restated, here.

## How the routing works

Once a session is authenticated, a background process discovers the user's workspaces; "initial workspace discovery" is its first complete pass.
A home page concerns its account's workspaces only: the workspaces associated with that account, or, on the device's home page, the workspaces associated with no account.
Before anything else, the one-time "Help improve Imbue Studio" consent screen is shown once per installation, overriding the normal home content until it is answered. Its one question is a checkbox, checked to start, whose answer becomes the error-reporting setting. A new install answers the same checkbox in the start flow, on the question that picks where to run the first workspace, so the screen is for an installation that has never answered it.
While initial discovery is still running, the home page shows a self-refreshing progress page.
After it finishes, the home page lists the account's workspaces if it has any; with none, it shows the new-workspace form once the installation's onboarding is complete, and otherwise hands over to the *start flow* -- the first-run chat at "/start" that creates the first workspace (the start flow's own contents are out of scope here).
The home page of an account that is signed out offers to log in where it would offer to create a workspace.
The progress page names the unit by the corpus's workspace-vs-agent convention: its user-facing string reads "Discovering workspaces".

## Out of scope

- The authorization boundary at "/" (an unauthenticated visitor), and the access-control facts about "/post-login" (its session gate and the confinement of a return destination to the origin) -- specified in `browser-authorization/`.
- The internals of the new-workspace form and the workspace-creation flow beyond "the form is shown or pre-filled".
- Which account a window is for, and how it changes -- specified in `account-windows/`.
- The contents of the workspace rows beyond the fact that each workspace is listed.
