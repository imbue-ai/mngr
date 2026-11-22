Imbue Studio has a new top bar, two switcher menus, and one Settings that holds every setting.

- The top bar is 44px tall. Its left side is a trail: the Studio wordmark (Home), the window's account by its email, and the workspace on screen ("Open workspace" on the home page). Its right side is the bell, Report a bug, one Settings gear, and a Share button in workspaces you own. The three workspace icons and the docked options panel are gone.

- The account and workspace buttons open menus that list every choice, with the current one marked by a faint tint and a check, and both take the arrow keys and Enter. Every row has an icon in one column: a person for an account, a computer for "This device", the workspace's colour dot, a plus for "New account" and "New workspace", which sit in their lists as entries. The workspace menu has a search field over your workspaces and the workspaces and apps shared with you. The account menu ends with "Manage accounts".

- A shortcut opens the workspace menu from anywhere in the app, inside a workspace included: Cmd+K on macOS, Ctrl+Shift+K elsewhere. In the desktop app, Settings > Studio > Keyboard shortcuts changes it or turns it off.

- Settings has three tabs: Studio, Accounts (in place of the Manage Accounts page), and Workspace & apps (in place of the options panel), which shows General, Permissions, Sharing, Backup, and Updates for whichever workspace you pick. The gear carries the dot for a waiting permission request, and the request opens as a centered dialog.

- Sharing is a page per target (the whole workspace, or one app; an app's is titled "Share the <name> app"), with a QR code of the link, a folded list of who has access that is left out while nobody has, and "Invite an agent" as a section of its own. "Email me a Mobile Access Link" is on every target's page, and on an app's page it emails that app's own link in a message about that app.

- A Sharing page no longer waits for Imbue Cloud before it draws. Its title and sections appear at once; the people invited, and the link and QR code when the app already knows the share's address, appear as soon as the workspace has answered; only the switch and each person's invitation status wait for Imbue Cloud, behind a spinner of their own.

- A workspace or app shared with you has an About page in Settings, whose "Leave workspace" or "Remove app" hides it from your lists without changing the access its owner gave.

- Old links (`/accounts`, `/workspace/<id>/options`, `/workspace/<id>/settings`, `/sharing/<id>`) land on their place in Settings, and the embed contract is unchanged.

- The backend's event log gains, at debug level, one line per API request with its status and duration, and the Sharing page's read logs its lock wait and its read of the workspace's share state, so a slow page can be read from the log.

- The docs describe the new layout, and the behavior corpus gains the `top-bar/` and `settings/` areas.
