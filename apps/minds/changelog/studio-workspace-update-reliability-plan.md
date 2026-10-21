Workspace updates now run inside the workspace, so a scheduled update no longer needs Imbue Studio open or the laptop awake.

- "Schedule" writes a schedule file, a dispatcher, and a cron line into the workspace. The workspace's own cron starts the update inside the window, in your time zone, and records why when a window passes without one. A stopped workspace does not update; the workspace has to be running during the window.

- "Update now" creates the update chat inside the workspace in one step, and a workspace that was stopped stays running afterwards. The app no longer starts or stops workspaces for a schedule, and no longer holds an update back while chats are working.

- The update chat is named for its release ("Update to v0.9.0") in its chat list, and the update window calls it "the update chat" rather than by its internal agent name. Its first message carries the release and the app's ceiling, so the update never has to ask the app for its version.

- A watchdog in the workspace reminds an update agent that sits idle without a result, and closes out one that is gone, releasing its leases and keeping its worker's branch.

- New auto-update setting, off until you choose it, offered once in a prompt. When it is on, Studio schedules every workspace that is a release behind and quiets the out-of-date notices; a workspace it cannot schedule (no cron, too old, unreachable) says why on its row instead, and one read behind that has since stopped or stopped answering still shows that it is behind. "Skip v0.9.0" holds one workspace back until the next release, after a confirmation that says what skipping means; a skipped workspace shows no out-of-date notice, and its row says the release was skipped.

- An update that cannot keep something you built now ends waiting for your answer, with a button into its chat; dismissing it skips that release. "Updated to X", failed updates, and an update that is still running also get an "Open chat" button, and a workspace with no AI account signed in shows a "Sign in" button.

- The notice over a workspace opens the update's chat while the update is preparing or waiting on you, and offers "Open chat" and "Dismiss" once it has ended.

- The update window shows one situation at a time, with only the buttons that situation needs: a run in progress shows only "Open chat", a run that ended shows its outcome and "Open chat" or "Dismiss" before any new update is offered, and a scheduled update's controls are gone once its run, or one started with "Update now instead", is under way.
