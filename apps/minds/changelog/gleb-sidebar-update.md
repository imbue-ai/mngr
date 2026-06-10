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

The sidebar behaves like a modal: clicking anywhere outside the menu (or
pressing Escape) closes it. The menu's height comes from its own flex
layout -- no JS measurement or per-bundle bounds math.

Each window now hosts three WebContentsView surfaces instead of four:
chrome (titlebar), content (workspace), and a single shared overlay used
by both the sidebar and the inbox. The sidebar URL (/_chrome/sidebar) is
loaded into the same modalView that hosts /inbox, so dismissal,
titlebar-drag suppression, transparent background, and Escape handling
all come from the existing modal infrastructure.
