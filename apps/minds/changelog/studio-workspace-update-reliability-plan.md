Workspace updates now run inside the workspace, so a scheduled update no longer needs Imbue Studio open or the laptop awake.

- "Schedule" writes a schedule file, a dispatcher, and a cron line into the workspace. The workspace's own cron starts the update inside the window, in your time zone, and records why when a window passes without one. A stopped workspace does not update; the workspace has to be running during the window.

- "Update now" creates the update chat inside the workspace in one step, and a workspace that was stopped stays running afterwards. The app no longer starts or stops workspaces for a schedule, and no longer holds an update back while chats are working.

- The update chat is named for its release ("Update to v0.9.0") in its chat list, and the update window and the workspace's settings call it "the update chat" rather than by its internal agent name. Its first message carries the release and the app's ceiling, so the update never has to ask the app for its version.

- A watchdog in the workspace reminds an update agent that sits idle without a result, and closes out one that is gone, releasing its leases and keeping its worker's branch.

- New auto-update setting, off until you choose it, offered once in a prompt. When it is on, Studio schedules every workspace that is a release behind and quiets the out-of-date notices; a workspace it cannot schedule (no cron, too old, unreachable) says why on its row instead. "Skip v0.9.0" holds one workspace back until the next release, after a confirmation that says what skipping means; a skipped workspace shows no out-of-date notice, and its row says the release was skipped.

- An update that cannot keep something you built now ends waiting for your answer, with a button into its chat; dismissing it skips that release. "Updated to X", failed updates, and an update that is still running also get an "Open chat" button, and a workspace with no AI account signed in shows a "Sign in" button.

- The notice over a workspace opens the update's chat while the update is preparing or waiting on you, and offers "Open chat" and "Dismiss" once it has ended.

- The update window shows one situation at a time, with only the buttons that situation needs: a run in progress shows only "Open chat", a run that ended shows its outcome and "Open chat" or "Dismiss" before any new update is offered, and a scheduled update's controls are gone once its run, or one started with "Update now instead", is under way.

- A stopped workspace says nothing about its update until it is running again: whether it is behind, scheduled or skipped, and how its last update ended, all come back on the first read after it starts. An update that is landing, an update Studio is starting the workspace for, an update that never went out, and a workspace too old to update in place are still shown.

- A workspace whose update is running can no longer be scheduled, so a schedule cannot outlive the run and start a fresh one after it.

- "Skip" in a workspace's Updates settings asks the same confirmation as the update window, and those settings, like the window, drop a scheduled update's controls while an update is under way.

- With auto-update on, you can still schedule any update by hand. "Schedule update" appears on a workspace unless it is already scheduled to the version Imbue Studio supports, and "Schedule" beside a specific version appears on every workspace. Over a schedule for another version, "Schedule update" replaces it with one naming the supported version. A schedule you made says "Scheduled to update" and offers "Cancel schedule"; only one auto-update made says "No action is needed" and offers a skip. Auto-update leaves your schedule as you made it, and a schedule you make after skipping a release shows as scheduled rather than skipped.

- Clicking a workspace's "Preparing update…", "Updating…" or "Needs your answer" badge while its update is under way opens the update's chat directly, instead of a window whose only button opened that chat. A run still starting, before its chat exists, opens the update window as before.

- A stuck update now reads "The update ran into a problem it couldn't recover from." in the update window, instead of saying it couldn't clean up after itself, which was rarely true, and a refused one reads "The update wasn't applied." instead of "The update didn't run.", since a refused update often ran and was rolled back. The watchdog's reminder to an idle update agent now asks it to record whichever result fits, with stuck reserved for a fault it cannot recover from, so an update waiting for your decision is not recorded as stuck.

- An update whose chat stopped, as a workspace restart stops every chat, now carries on: the workspace starts the chat again and tells the update agent how many times it has had to, and the agent gives up (recording the update as stuck) only if it keeps stopping for a reason it cannot fix. The row keeps saying the update is under way meanwhile. To cancel an update under way, delete its chat or tell its agent to stop; an agent told to stop ends the update without applying it.

- Deleting an update's chat no longer blocks updating that workspace for 15 minutes ("An update is already starting in this workspace"), and no longer bounces the row back to "Preparing update". The update reports that it didn't finish, and its window says the chat was deleted and offers no chat to open.

- "This workspace's update didn't finish." now offers "See details", which opens the update window, instead of "Open chat".

- The workspace's Updates settings now say that running chats pause for a few minutes while an update is applied, as the update window does.

- A workspace's update dispatcher keeps finding the template's environment wrapper after the Routines rework moved it.
