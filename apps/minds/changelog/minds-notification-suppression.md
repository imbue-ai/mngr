# Notifications surface once, and a workspace keeps one main window

- A new notification now surfaces in one way the user is expected to see: while any main window has focus it flashes as an in-app toast in every main window and no OS banner appears; with no main window focused (the app in the background, minimized, or only a pulled-out window in front) the OS banner fires. The `os` delivery style still gets the banner while focused, and a locked screen (macOS and Windows) always gets it.

- A chat agent's notification may carry `watched_by`, the chat pages the workspace says are showing that chat to a focused reader. A watched notification is recorded already read: no toast, no banner, no bell or dock count, only a receipt in the feed. A locked screen overrides it. Workspaces that do not send the field get the rules above.

- Agent messages are no longer deleted when read, and only the chat actually being read is read. Watched on arrival, or read later through the new `POST /api/v1/agents/<agent_id>/notifications/read` route (which the workspace's chat app calls when a chat becomes watched), they stay in the feed as receipts below everything unread. Showing a workspace no longer marks all its agents' messages read; a workspace on an older template never reports a chat read, so its messages stay unread until opened or cleared. Reading a chat also closes the OS banners still showing for it.

- Pulled-out windows no longer show the feed's toasts.

- A workspace has at most one main window. "Open in new window", navigating a window onto a workspace another window shows (history steps back and forward included), notification clicks, and session restore all raise the workspace's existing window instead of opening or retargeting another. A notification for a workspace with no window opens a new window for it rather than taking over a window showing another workspace. Blank windows and pulled-out windows are not limited; browser mode is unchanged.
