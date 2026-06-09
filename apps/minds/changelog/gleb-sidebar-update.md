The sidebar is now a floating menu: dark teal panel with rounded corners,
shadow, and a colored dot per workspace, matching the Figma "Space switcher
menu" design. The Electron sidebar WebContentsView is transparent so the
panel reads as a floating overlay above the workspace content.

The current workspace row now carries an "Open in new window" button and
a per-workspace settings gear; non-current rows reveal the open-in-new
button on hover. Two new rows at the bottom of the menu: "New workspace"
(navigates to /create) and "Manage account(s)" / "Log in" (replaces the
account button that used to sit in the titlebar). The titlebar no longer
shows the account button.
