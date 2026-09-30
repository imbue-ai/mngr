An app window no longer stays on "Loading workspace" after the forward's events stream for its workspace loses its reader. The `mngr event` child now exits when its reader dies (it used to keep running without it), so the forward's existing respawn replaces it and new app registrations arrive again.

The "Resolved no backend" warning now says how long the workspace's events stream has run and when it last printed a line, or that none is running. A newly started stream is logged at info.
