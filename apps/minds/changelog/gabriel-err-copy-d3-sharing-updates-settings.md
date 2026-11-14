Plainer messages in the share panel, with a button for the next step wherever there is one:

- Every sharing failure now carries why it failed, and the panel says it in its own words, with the underlying text behind "See details" and a "Report a bug" button where nothing else explains it.

- A stopped workspace is no longer told it is too old to be shared: one notice says "This workspace is stopped. Start it, then try again." with a "Start workspace" button, which comes back to the Share tab once the workspace answers. A running workspace that can't be reached gets one notice too, naming what couldn't be done (setting up sharing, saving a change, sending an invitation), with "Try again" when turning sharing on. Reopening the Share tab after the workspace answers no longer shows the earlier failures. A workspace that really is too old says so, with an "Update workspace" button.

- Sharing that needs an account says which step is missing: sign in (with "Sign in"), sign in to the account that owns the workspace, link the workspace to an account (with a button to its account settings), or that the list of signed-in accounts could not be read.

- An expired Imbue Cloud sign-in, a waitlisted account, and a plan's shared-workspace limit ("Your plan allows 3 shared workspaces and 3 are shared already...") each get their own sentence instead of command output. A full plan also offers "Review shared workspaces"; a waitlisted account, which no freed slot would help, does not.

- A workspace whose sharing could not be checked is no longer drawn as unshared; the panel says it couldn't check, with "Try again" and "Report a bug", or asks you to sign in to the account that owns the workspace.

- Turning sharing on or off when it fails for a reason the panel can't name, an Imbue Cloud failure among them, offers "Try again".

- A link that never answers is no longer shown as ready after five minutes: the panel says "This link still isn't answering." with a "Try again" that turns sharing off and on, "Ask the agent to fix it" and "Report a bug". The setup steps no longer mention the relay.

- An app with no link yet explains that it gets one the first time it starts (or that the workspace needs starting) instead of spinning forever.

- A person whose access could not be saved says "Couldn't add" or "Couldn't remove — still has access" on their card, with "See details" and "Retry"; a refused entry says why and offers "Remove" instead of a Retry that would be refused again. A refused invitation gets "See details" too.

- An unreadable sharing list inside the workspace offers "Ask the agent to fix it" as its main button.

- Invitation and mobile-link failures say plainly what went wrong, including "This workspace's link isn't ready yet. Invite again in a minute." instead of "This person has already joined."

- A failed move to a new sharing address offers "Restart sharing" and keeps the switch drawn as on.

- Inviting, listing and revoking visiting agents say what failed in plain words, with "Start workspace", "Update workspace", "Open account settings" or "Restart sharing" where they help. A list that couldn't be read offers "Try again", or only "Start workspace" when the workspace is stopped.

- The shared-workspaces page says when an account's shares couldn't be read, with "Try again" for that account (or "Sign in"), and says plainly when a share couldn't be removed.

Plainer update messages:

- A failed update check says "Couldn't check for updates — check your internet connection, then try again." for a network failure or timeout, and the fallback sentence with "Report a bug" otherwise, in an error box whose main button is "Try again" (in place of the panel's "Check now") and with the updater's text behind "See details". A failed download now says the new version couldn't be downloaded and will be tried again at the next check, instead of reading as a failed check.

- An update that didn't go out, on a workspace's Updates tab, ends with what to do next and keeps the workspace's output behind "See details", with "Try again" and (only where nothing explains the failure) "Report a bug" in one row; a workspace too old to update itself leads with "Ask its agent", then "Create a new workspace", and offers no "Try again", which can't work there.

- Scheduling, cancelling or skipping an update, and opening the sign-in chat for one, say what to do when they fail ("Make sure it's running, then try again.", "Try again in a moment."), a schedule refused because the workspace is stopped leads with "Start workspace" (with "Try again" beside it), and a missed update window no longer blames "another update flow".

- The automatic-updates switch, the one-time auto-update offer, "Use Studio settings" and a workspace's own automatic-updates choice say plainly when a change wasn't saved, with "Try again" and the details collapsed, instead of an HTTP status.

- The no-backups question explains that the update keeps a history of your files and can be rolled back, instead of mentioning git.

- A Linux copy run from source shows the install command that updates it, and an AppImage started without its file says how it gets updates, instead of "Updates are disabled in dev builds."

- A refused "Schedule all updates" or "Update all now" shows its failure under the buttons instead of in a native alert, with "Restart Imbue Studio" when this window needs reconnecting.

- While an update error shows its own main button (a too-old workspace's "Ask its agent", "Restart Imbue Studio" for a window that needs reconnecting), the Updates tab's "Schedule update" or "Update now", the kernel card's restart, the strip's "Schedule all updates" and, beside a failed update check, the Settings panel's install button are drawn as secondary buttons, so the fix is the one main button.

- The Updates panel and the "ready to install" card say what failed in their own words (with "Try again" on a failed load) instead of a stringified error.

Plainer workspace settings and Settings page messages:

- Asking a workspace's agent for help ("Ask its agent", "Ask the agent to fix it") starts the workspace first when it is stopped. When an agent can't be started, a workspace too old to start a helper offers "Update workspace", and other failures offer "Try again", with the details collapsed.

- An expired Imbue Cloud sign-in no longer reads as an unreachable workspace when renaming or recoloring it: it says to sign in again, with "Sign in".

- A size an Imbue Cloud plan has no room for says "Your plan doesn't have room for this size right now. Pick a smaller size.", with the quota behind "See details"; saving the size a machine already has says "That's already this machine's size." for both the size picker and the typed sizes.

- Renaming a workspace to a name another workspace has says "A workspace named '<name>' already exists. Pick a different name."

- Loading a workspace's settings or your Settings, renaming, resizing, recoloring, linking or unlinking its account, saving notification, update-window and sign-in-browser settings, and changing the master password each say what failed in plain words, with "Try again" or "Restart Imbue Studio" where they help. A master-password change some accounts didn't take lists them as an error, with each account's reason behind "See details" and "Report a bug".

- Folder sync says "Pick a folder on this computer to sync.", says "workspace" rather than "machine", and a folder no longer set up to sync offers its switch instead of a "Try again" that cannot work.

Every one of these errors is drawn to the shared error design: the alert mark leads a black sentence in a tinted box, "See details" trails that sentence, and the step a sentence asks for is set apart from the rest of it. Smaller refusals -- a name already taken, an invalid colour, a size that isn't a whole number, a refused share entry, a refused invitation, a zoom the app wouldn't take -- keep red text at the smaller size with the same mark, rather than reading as full notices. The permissions pane's own error line carries the mark too.

Where a surface used to state a failure in a line of its own and put the error under it, the error now carries that sentence: the Updates tab's status line is dropped for the update that didn't start and for a sign-in the workspace refused, so the failure is said once, with the tab's heading carrying the mark and the sentence under it flush. The update-ready card's install failure spans the card under its content row. Help's "Couldn't start an agent" puts the mark on that heading and sits the sentence flush under it, with "Back to report" below as the surface's own button. A failed "Ask the agent to fix it" start is said on the tint of the error whose button started it, rather than in a box of its own inside that one.
