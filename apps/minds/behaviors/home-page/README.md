# Home page

Understanding this behavior corpus calls for the tmr-behaviors skill; consult it when reading this file.

The home page is "/" -- the first thing a browser lands on once a session is authenticated on the *browser authorization component* (defined in `browser-authorization/`).
This folder specifies what an already-*authenticated* user sees at "/", and where "/post-login" sends them once they sign in.

## Boundary with browser-authorization

Authentication is a precondition here, not a subject: every scenario begins with an already-authenticated user.
What a browser with *no* session sees at "/" is the authorization boundary, specified in `browser-authorization/` (the `@unauthenticated-home` illustration of the no-user-data-without-a-session invariant).
Deciding where a just-signed-in user lands is specified here (`post-login-destination.feature`), but its two access-control facts -- the session gate on "/post-login", and the confinement of a caller-supplied return destination to the origin -- are specified in `browser-authorization/` and referenced, not restated, here.

## How the routing works

Once a session is authenticated, a background process discovers the user's workspaces; "initial workspace discovery" is its first complete pass.
Before anything else, the one-time "Help improve Minds" consent screen is shown once per installation, overriding the normal home content until it is answered.
While initial discovery is still running, "/" shows a self-refreshing progress page.
After it finishes, "/" lists the user's workspaces if they have any, or shows the new-workspace form if they have none -- optionally pre-filled from a deep link.
The progress page names the unit by the corpus's workspace-vs-agent convention: its user-facing string reads "Discovering workspaces".

## Out of scope

- The authorization boundary at "/" (an unauthenticated visitor), and the access-control facts about "/post-login" (its session gate and the confinement of a return destination to the origin) -- specified in `browser-authorization/`.
- The internals of the new-workspace form and the workspace-creation flow beyond "the form is shown or pre-filled".
- The contents of the workspace rows beyond the fact that each workspace is listed.
