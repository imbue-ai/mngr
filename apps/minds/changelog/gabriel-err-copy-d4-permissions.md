Permission failures now say what happened in plain words, with the technical detail behind "See details" and a "Report a bug" button:

- The request popup no longer prints raw JSON or tool output: a failed save, an unfinished sign-in (or access left unticked on the consent screen), a workspace that refused the permission and an unsyncable folder each get their own sentence, with "Restart Imbue Studio" when the permission service is not running.

- An approval the workspace refused after it was recorded no longer leaves the agent waiting: the agent is told, and the popup offers "Close", as it does for a request already answered elsewhere. A request the popup can't show offers "Deny" (for a permission it doesn't recognize) or "Close", and doesn't ask the user to try again.

- Denying a request for a service Imbue Studio cannot set up at all (no browser sign-in, no credential form, no usable account on the workspace's machine) tells the agent why, so it does not just ask again.

- Refused typed credentials say to check them and try again, with the service's reason behind "See details".

- The Permissions tab says why it could not load, leading with the fix: "Restart Imbue Studio" (with "Try again"), "Update Imbue Studio" alone, or "Try again". Refused changes are worded for the user, including a permission whose definition is gone ("Ask the agent to request it again", which says so while the agent starts and shows why under the error if it can't, and is withdrawn once the workspace refuses the request). On a computer with no device id, "Add file" and "Add folder" are greyed out with "Imbue Studio can't share files from this computer.", and the request popup holds Approve while a shared path is edited.

- The notification feed reads "Still waiting" rather than "Closed" for a request whose workspace is not listed, and a stale notification says the request is no longer pending.

- A failed Chrome cookie import says what failed instead of the import tool's output, and drops "Try again" when Chrome is missing. An unreadable Chrome profile keeps its steps, with "Open System Settings" beside them until opening it fails; once System Settings is open (or has to be opened by hand), "Reopen Imbue Studio" leads on macOS, since the Full Disk Access grant applies only after a relaunch. Beside that button, the cookie offer's "Continue to sign in" is drawn secondary.

- Every permission error uses the shared error display. The request popup shows a spinner while it re-reads a request list that would not load, and a refused sign-in in the Permissions tab opens the app's error dialog.

Every one of these is drawn to the shared error design: the alert mark leads a black sentence in a tinted box, "See details" trails that sentence, and the step a sentence asks for ("*Restart Imbue Studio and try again.*", "*Check them and approve again.*") is set apart from the rest of it. What is too small for a notice -- a path outside the shareable folders, a computer that can't share files, a folder sync that failed -- keeps red text at the smaller size with the same mark. The request popup's "Imbue Studio couldn't load your requests." reads as a warning that is still trying rather than as a failure, since the popup keeps asking on its own.
