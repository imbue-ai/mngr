# Notifications and the windows they land in

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

A *notification* is an entry in the app's notification feed: a workspace's permission request, a chat agent's message to the user, or an event the app reports on the user's behalf.
The feed backs the bell and its count, the dock count, the *toast* (an in-app card that flashes in a window and then retires), and the *banner* (the operating system's own notification).

A *main window* is a desktop window that shows the app's pages; a *pulled-out window* shows one window of a workspace on its own.
A main window has *focus* when the operating system says it is the window in front of the user.

A chat is *watched* when some page showing that chat is displayed, its document has focus, and it has said so recently.
The workspace decides which chats are watched and names the watching pages on each message it sends; the app decides what a watched message does.

## Out of scope

- What the workspace counts as a page showing a chat, and how it measures focus and recency; those belong to the workspace.
- Browser mode, where the app has no banners and no control over windows or tabs.
