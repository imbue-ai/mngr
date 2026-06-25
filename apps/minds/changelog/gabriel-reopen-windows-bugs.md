Fixed window-reopening bugs:

Windows restored on app relaunch now reopen to the mind they were showing, instead of all landing on the main page. (Workspace windows were persisted with the agent subdomain stripped off, so they reopened against the minds backend root; they are now persisted and restored by agent identity.)

A reopened "create workspace" window whose creation no longer exists (after an app restart, or a failed creation) now redirects to the landing page instead of getting stuck on a black "Unknown agent creation" screen.

Quitting with "Shut down all" stops the docker state container that holds local minds' host records; the app now restarts it early on the next launch, before discovery runs. Without this, reopening right after "Shut down all" could find the state backend still down, discover zero local minds, and drop the restored windows onto the create-workspace form.

Launching with Docker paused (or otherwise unable to start that state container) no longer crashes the app with "Failed to start minds". The launch-time restart is best-effort, but the failure was escaping wrapped in a ConcurrencyExceptionGroup that the surrounding handler could not catch; the docker cleanup helpers now raise their DockerCleanupError outside the concurrency-group scope, so the handler catches it and startup proceeds (discovery degrades gracefully rather than aborting). The same wrapping affected the quit-time stop and env teardown paths, which are fixed too.

Window restore no longer drops a window just because its mind is missing from the first discovery snapshot. Providers enumerate at different speeds on cold start (a cloud mind can appear in ~1s while local docker minds take ~15-20s), so the first "complete" snapshot could be missing the local minds entirely. Restore now keeps a window if its mind is known from the persisted last-good topology (carried across restarts), dropping it only on positive evidence the mind was destroyed -- so a slow provider can no longer cost you a window.
