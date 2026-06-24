Fixed window-reopening bugs:

Windows restored on app relaunch now reopen to the mind they were showing, instead of all landing on the main page. (Workspace windows were persisted with the agent subdomain stripped off, so they reopened against the minds backend root; they are now persisted and restored by agent identity.)

App startup now holds the loading screen until the first full discovery snapshot arrives, so restored windows and the landing page show the complete set of minds rather than an empty/partial list that fills in a few seconds later. Previously this cold-start gap could also cause restored windows to be dropped (collapsing to a single landing-page window).

A reopened "create workspace" window whose creation no longer exists (after an app restart, or a failed creation) now redirects to the landing page instead of getting stuck on a black "Unknown agent creation" screen.

Quitting with "Shut down all" stops the docker state container that holds local minds' host records; the app now restarts it early on the next launch, before discovery runs. Without this, reopening right after "Shut down all" could find the state backend still down, discover zero local minds, and drop the restored windows onto the create-workspace form.
