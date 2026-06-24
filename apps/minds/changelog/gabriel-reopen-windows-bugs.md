Fixed two window-reopening bugs:

Windows restored on app relaunch now reopen to the mind they were showing, instead of all landing on the main page. (Workspace windows were persisted with the agent subdomain stripped off, so they reopened against the minds backend root; they are now persisted and restored by agent identity.)

A reopened "create workspace" window whose creation no longer exists (after an app restart, or a failed creation) now redirects to the landing page instead of getting stuck on a black "Unknown agent creation" screen.
